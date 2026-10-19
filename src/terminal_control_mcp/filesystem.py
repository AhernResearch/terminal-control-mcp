"""Atomic SFTP file operations constrained to configured local and remote roots."""

import hashlib
import posixpath
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncssh

from terminal_control_mcp.errors import TerminalControlError
from terminal_control_mcp.models import Target
from terminal_control_mcp.transport import AsyncSshTransport

MAX_CONTENT_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _ResolvedPath:
    path: str
    parent: str
    attrs: asyncssh.SFTPAttrs | None


class RemoteFilesystem:
    """Safe convenience writes over SFTP without limiting raw run_command."""

    def __init__(
        self,
        transport: AsyncSshTransport,
        allowed_local_read_roots: tuple[Path, ...] = (),
    ):
        self._transport = transport
        self._allowed_local_read_roots = allowed_local_read_roots

    def _resolve_local_upload(self, local_path: str) -> tuple[Path, int, int]:
        if not isinstance(local_path, str) or not local_path or "\x00" in local_path:
            raise TerminalControlError("PATH_DENIED", "local_path must be non-empty")
        candidate = Path(local_path).expanduser()
        if candidate.is_symlink():
            raise TerminalControlError("PATH_DENIED", "Local upload source cannot be a symlink")
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError as exc:
            raise TerminalControlError(
                "FILE_NOT_FOUND", "Local upload source does not exist"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise TerminalControlError("PATH_DENIED", "Local upload source must be a regular file")
        matching_roots = []
        for root in self._allowed_local_read_roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            matching_roots.append(root)
        if not matching_roots:
            raise TerminalControlError("PATH_DENIED", "Path is outside allowed local read roots")
        root = max(matching_roots, key=lambda item: len(item.parts))
        current = root
        for part in resolved.relative_to(root).parts:
            current = current / part
            if current.is_symlink():
                raise TerminalControlError("PATH_DENIED", f"Unsafe local path component: {current}")
        if metadata.st_size > MAX_UPLOAD_BYTES:
            raise TerminalControlError("CONTENT_TOO_LARGE", "Upload exceeds 512 MiB")
        return resolved, metadata.st_size, metadata.st_mtime_ns

    @staticmethod
    async def _lstat_optional(
        sftp: asyncssh.SFTPClient,
        path: str,
    ) -> asyncssh.SFTPAttrs | None:
        try:
            return await sftp.lstat(path)
        except asyncssh.SFTPNoSuchFile:
            return None

    @staticmethod
    def _file_type(attrs: asyncssh.SFTPAttrs, path: str) -> int:
        if attrs.permissions is None:
            raise TerminalControlError("PATH_DENIED", f"Missing remote permissions: {path}")
        return attrs.permissions

    async def _ensure_directory(
        self,
        sftp: asyncssh.SFTPClient,
        path: str,
        *,
        create: bool,
    ) -> None:
        attrs = await self._lstat_optional(sftp, path)
        if attrs is None:
            if not create:
                raise TerminalControlError("FILE_NOT_FOUND", f"Directory does not exist: {path}")
            await sftp.mkdir(path)
            await sftp.chmod(path, 0o700)
            return
        permissions = self._file_type(attrs, path)
        if stat.S_ISLNK(permissions) or not stat.S_ISDIR(permissions):
            raise TerminalControlError("PATH_DENIED", f"Unsafe remote directory: {path}")

    async def _resolve(
        self,
        target: Target,
        sftp: asyncssh.SFTPClient,
        file_path: str,
        *,
        create_parents: bool,
    ) -> _ResolvedPath:
        if not isinstance(file_path, str) or not file_path or "\x00" in file_path:
            raise TerminalControlError("PATH_DENIED", "file_path must be non-empty")
        home = await self._transport.home_directory(target)
        if file_path == "~":
            candidate = home
        elif file_path.startswith("~/"):
            candidate = posixpath.join(home, file_path[2:])
        elif posixpath.isabs(file_path):
            candidate = posixpath.normpath(file_path)
        else:
            candidate = posixpath.normpath(posixpath.join(home, file_path))
        candidate = posixpath.normpath(candidate)

        roots = [
            home if root == "~" else posixpath.normpath(root)
            for root in target.config.allowed_write_roots
        ]
        matching_roots = [
            root
            for root in roots
            if candidate == root or candidate.startswith(root.rstrip("/") + "/")
        ]
        if not matching_roots:
            raise TerminalControlError("PATH_DENIED", "Path is outside allowed write roots")
        root = max(matching_roots, key=len)

        # Reject symlinks from the filesystem root through the selected allowed root.
        current = "/"
        for part in [part for part in root.split("/") if part]:
            current = posixpath.join(current, part)
            attrs = await self._lstat_optional(sftp, current)
            if attrs is None:
                if current != root or root != "/tmp/terminal-control":
                    raise TerminalControlError("PATH_DENIED", f"Allowed root is missing: {root}")
                await self._ensure_directory(sftp, current, create=True)
                continue
            permissions = self._file_type(attrs, current)
            if stat.S_ISLNK(permissions) or not stat.S_ISDIR(permissions):
                raise TerminalControlError("PATH_DENIED", f"Unsafe path component: {current}")

        parent = posixpath.dirname(candidate)
        relative_parent = posixpath.relpath(parent, root)
        current = root
        if relative_parent != ".":
            for part in relative_parent.split("/"):
                if part in {"", ".", ".."}:
                    raise TerminalControlError("PATH_DENIED", "Unsafe relative path")
                current = posixpath.join(current, part)
                await self._ensure_directory(sftp, current, create=create_parents)

        attrs = await self._lstat_optional(sftp, candidate)
        if attrs is not None:
            permissions = self._file_type(attrs, candidate)
            if stat.S_ISLNK(permissions) or not stat.S_ISREG(permissions):
                raise TerminalControlError("PATH_DENIED", "Target must be a regular file")
        return _ResolvedPath(candidate, parent, attrs)

    @staticmethod
    async def _write_atomic(
        sftp: asyncssh.SFTPClient,
        resolved: _ResolvedPath,
        content: bytes,
        mode: int,
    ) -> None:
        name = posixpath.basename(resolved.path)
        temp_path = posixpath.join(
            resolved.parent,
            f".{name}.terminal-control-{uuid.uuid4().hex}.tmp",
        )
        try:
            async with sftp.open(temp_path, "wb", encoding=None) as remote_file:
                await remote_file.write(content)
            await sftp.chmod(temp_path, mode)
            await sftp.posix_rename(temp_path, resolved.path)
        except Exception:
            try:
                await sftp.remove(temp_path)
            except (asyncssh.SFTPError, OSError):
                pass
            raise

    async def write(self, target: Target, file_path: str, content: str) -> dict[str, Any]:
        """Write UTF-8 content atomically to an allowed regular file."""
        if not isinstance(content, str):
            raise TerminalControlError("CONFIG_ERROR", "content must be text")
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_CONTENT_BYTES:
            raise TerminalControlError("CONTENT_TOO_LARGE", "File content exceeds 10 MiB")

        async with self._transport.operation(target) as connection:
            try:
                async with connection.start_sftp_client() as sftp:
                    resolved = await self._resolve(
                        target,
                        sftp,
                        file_path,
                        create_parents=True,
                    )
                    mode = 0o600
                    if resolved.attrs and resolved.attrs.permissions is not None:
                        mode = stat.S_IMODE(resolved.attrs.permissions)
                    await self._write_atomic(sftp, resolved, content_bytes, mode)
            except TerminalControlError:
                raise
            except asyncssh.SFTPPermissionDenied as exc:
                raise TerminalControlError("PATH_DENIED", "Remote write permission denied") from exc
            except Exception as exc:
                raise self._transport.translate_error(exc) from exc
        return {
            "status": "ok",
            "path": resolved.path,
            "bytes_written": len(content_bytes),
        }

    async def edit(
        self,
        target: Target,
        file_path: str,
        old_string: str,
        new_string: str,
    ) -> dict[str, Any]:
        """Atomically replace a unique UTF-8 string in an allowed regular file."""
        if not isinstance(old_string, str) or not old_string:
            raise TerminalControlError("EDIT_NOT_UNIQUE", "old_string must be non-empty")
        if not isinstance(new_string, str):
            raise TerminalControlError("CONFIG_ERROR", "new_string must be text")

        async with self._transport.operation(target) as connection:
            try:
                async with connection.start_sftp_client() as sftp:
                    resolved = await self._resolve(
                        target,
                        sftp,
                        file_path,
                        create_parents=False,
                    )
                    if resolved.attrs is None:
                        raise TerminalControlError("FILE_NOT_FOUND", "Remote file does not exist")
                    if resolved.attrs.size is not None and resolved.attrs.size > MAX_CONTENT_BYTES:
                        raise TerminalControlError(
                            "CONTENT_TOO_LARGE", "Remote file exceeds 10 MiB"
                        )

                    async with sftp.open(resolved.path, "rb", encoding=None) as remote_file:
                        raw = await remote_file.read(MAX_CONTENT_BYTES + 1)
                    if len(raw) > MAX_CONTENT_BYTES:
                        raise TerminalControlError(
                            "CONTENT_TOO_LARGE", "Remote file exceeds 10 MiB"
                        )
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise TerminalControlError(
                            "CONFIG_ERROR", "Remote file is not UTF-8"
                        ) from exc
                    count = text.count(old_string)
                    if count != 1:
                        raise TerminalControlError(
                            "EDIT_NOT_UNIQUE",
                            f"old_string must appear exactly once; found {count}",
                        )
                    updated = text.replace(old_string, new_string, 1).encode("utf-8")
                    if len(updated) > MAX_CONTENT_BYTES:
                        raise TerminalControlError(
                            "CONTENT_TOO_LARGE", "Edited file exceeds 10 MiB"
                        )

                    current = await sftp.lstat(resolved.path)
                    if (
                        current.size != resolved.attrs.size
                        or current.mtime != resolved.attrs.mtime
                        or current.mtime_ns != resolved.attrs.mtime_ns
                    ):
                        raise TerminalControlError(
                            "FILE_CHANGED", "Remote file changed during edit"
                        )
                    mode = stat.S_IMODE(resolved.attrs.permissions or 0o600)
                    await self._write_atomic(sftp, resolved, updated, mode)
            except TerminalControlError:
                raise
            except asyncssh.SFTPPermissionDenied as exc:
                raise TerminalControlError("PATH_DENIED", "Remote edit permission denied") from exc
            except Exception as exc:
                raise self._transport.translate_error(exc) from exc
        return {"status": "ok", "path": resolved.path, "replacements": 1}

    async def upload(
        self,
        target: Target,
        local_path: str,
        remote_path: str,
    ) -> dict[str, Any]:
        """Upload one binary file atomically and verify its remote SHA256."""
        source, source_size, source_mtime_ns = self._resolve_local_upload(local_path)
        digest = hashlib.sha256()

        async with self._transport.operation(target) as connection:
            try:
                async with connection.start_sftp_client() as sftp:
                    resolved = await self._resolve(
                        target,
                        sftp,
                        remote_path,
                        create_parents=True,
                    )
                    name = posixpath.basename(resolved.path)
                    temp_path = posixpath.join(
                        resolved.parent,
                        f".{name}.terminal-control-{uuid.uuid4().hex}.tmp",
                    )
                    try:
                        async with sftp.open(temp_path, "wb", encoding=None) as remote_file:
                            with source.open("rb") as local_file:
                                while chunk := local_file.read(UPLOAD_CHUNK_BYTES):
                                    digest.update(chunk)
                                    await remote_file.write(chunk)
                        current = source.stat()
                        if current.st_size != source_size or current.st_mtime_ns != source_mtime_ns:
                            raise TerminalControlError(
                                "FILE_CHANGED", "Local file changed during upload"
                            )
                        await sftp.chmod(temp_path, 0o600)
                        remote_digest = hashlib.sha256()
                        async with sftp.open(temp_path, "rb", encoding=None) as remote_file:
                            while chunk := await remote_file.read(UPLOAD_CHUNK_BYTES):
                                remote_digest.update(chunk)
                        if remote_digest.digest() != digest.digest():
                            raise TerminalControlError(
                                "CHECKSUM_MISMATCH", "Remote upload SHA256 mismatch"
                            )
                        await sftp.posix_rename(temp_path, resolved.path)
                    except Exception:
                        try:
                            await sftp.remove(temp_path)
                        except (asyncssh.SFTPError, OSError):
                            pass
                        raise
            except TerminalControlError:
                raise
            except asyncssh.SFTPPermissionDenied as exc:
                raise TerminalControlError(
                    "PATH_DENIED", "Remote upload permission denied"
                ) from exc
            except Exception as exc:
                raise self._transport.translate_error(exc) from exc
        return {
            "status": "ok",
            "local_path": str(source),
            "remote_path": resolved.path,
            "bytes_written": source_size,
            "sha256": digest.hexdigest(),
        }
