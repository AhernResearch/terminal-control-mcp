"""Strict local-file credential storage independent of desktop services."""

import os
import re
import stat
import uuid
from pathlib import Path

from terminal_control_mcp.errors import TerminalControlError

DEFAULT_CREDENTIAL_DIR = Path("~/.config/terminal-control-mcp/credentials").expanduser()
MAX_PASSWORD_BYTES = 4096
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class CredentialStore:
    """Store one password per owner-only file in an owner-only directory."""

    def __init__(self, directory: str | Path | None = None):
        configured = os.getenv("TERMINAL_CONTROL_MCP_CREDENTIALS_DIR")
        self._directory = Path(directory or configured or DEFAULT_CREDENTIAL_DIR).expanduser()

    @property
    def directory(self) -> Path:
        """Return the configured credential directory without exposing credential content."""
        return self._directory

    @staticmethod
    def _validate_directory(path: Path, label: str) -> None:
        if path.is_symlink() or not path.is_dir():
            raise TerminalControlError("CREDENTIAL_UNAVAILABLE", f"{label} is missing or unsafe")
        path_stat = path.stat()
        if path_stat.st_uid != os.getuid():
            raise TerminalControlError(
                "CREDENTIAL_UNAVAILABLE", f"{label} must be owned by current user"
            )
        if stat.S_IMODE(path_stat.st_mode) != 0o700:
            raise TerminalControlError(
                "CREDENTIAL_UNAVAILABLE", f"{label} permissions must be 0700"
            )

    @staticmethod
    def _validate_reference(credential_ref: str) -> None:
        if not isinstance(credential_ref, str) or not _REFERENCE_RE.fullmatch(credential_ref):
            raise TerminalControlError("CONFIG_ERROR", "Invalid credential reference")

    def _path(self, credential_ref: str) -> Path:
        self._validate_reference(credential_ref)
        return self._directory / credential_ref

    def _ensure_directory(self, *, create: bool) -> bool:
        parent = self._directory.parent
        self._validate_directory(parent, "Credential parent directory")
        if not self._directory.exists():
            if not create:
                return False
            try:
                self._directory.mkdir(mode=0o700)
            except OSError as exc:
                raise TerminalControlError(
                    "CREDENTIAL_UNAVAILABLE", "Failed to create credential directory"
                ) from exc
        self._validate_directory(self._directory, "Credential directory")
        return True

    @staticmethod
    def _validate_file(path: Path) -> os.stat_result:
        try:
            path_stat = path.lstat()
        except FileNotFoundError as exc:
            raise TerminalControlError(
                "CREDENTIAL_UNAVAILABLE", f"Credential '{path.name}' is not configured"
            ) from exc
        if not stat.S_ISREG(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
            raise TerminalControlError("CREDENTIAL_UNAVAILABLE", "Credential file is unsafe")
        if path_stat.st_uid != os.getuid():
            raise TerminalControlError(
                "CREDENTIAL_UNAVAILABLE", "Credential file must be owned by current user"
            )
        if stat.S_IMODE(path_stat.st_mode) != 0o600:
            raise TerminalControlError(
                "CREDENTIAL_UNAVAILABLE", "Credential file permissions must be 0600"
            )
        return path_stat

    def get(self, credential_ref: str) -> str:
        """Return a configured password after strict path and permission checks."""
        path = self._path(credential_ref)
        if not self._ensure_directory(create=False):
            raise TerminalControlError(
                "CREDENTIAL_UNAVAILABLE",
                f"Credential '{credential_ref}' is not configured",
            )
        expected = self._validate_file(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                    raise TerminalControlError(
                        "CREDENTIAL_UNAVAILABLE", "Credential file changed while opening"
                    )
                data = os.read(descriptor, MAX_PASSWORD_BYTES + 1)
            finally:
                os.close(descriptor)
        except TerminalControlError:
            raise
        except OSError as exc:
            raise TerminalControlError(
                "CREDENTIAL_UNAVAILABLE", "Failed to read credential file"
            ) from exc
        if not data or len(data) > MAX_PASSWORD_BYTES or b"\x00" in data:
            raise TerminalControlError(
                "CREDENTIAL_UNAVAILABLE", "Credential file content is invalid"
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TerminalControlError(
                "CREDENTIAL_UNAVAILABLE", "Credential file content is not UTF-8"
            ) from exc

    def exists(self, credential_ref: str) -> bool:
        """Check whether a valid credential exists without returning it."""
        path = self._path(credential_ref)
        if not self._ensure_directory(create=False) or not path.exists():
            return False
        self._validate_file(path)
        return True

    def set(self, credential_ref: str, password: str) -> None:
        """Atomically store a non-empty UTF-8 password with mode 0600."""
        path = self._path(credential_ref)
        if not isinstance(password, str) or not password or "\x00" in password:
            raise TerminalControlError("CONFIG_ERROR", "Password must be non-empty")
        data = password.encode("utf-8")
        if len(data) > MAX_PASSWORD_BYTES:
            raise TerminalControlError("CONFIG_ERROR", "Password exceeds 4096 bytes")
        self._ensure_directory(create=True)
        if path.exists() or path.is_symlink():
            self._validate_file(path)

        temp_path = self._directory / f".{credential_ref}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(temp_path, flags, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temp_path, path)
            directory_fd = os.open(self._directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            raise TerminalControlError(
                "CREDENTIAL_UNAVAILABLE", "Failed to write credential file"
            ) from exc

    def delete(self, credential_ref: str) -> None:
        """Delete one validated credential file."""
        path = self._path(credential_ref)
        if not self._ensure_directory(create=False):
            raise TerminalControlError(
                "CREDENTIAL_UNAVAILABLE",
                f"Credential '{credential_ref}' is not configured",
            )
        self._validate_file(path)
        try:
            path.unlink()
        except OSError as exc:
            raise TerminalControlError(
                "CREDENTIAL_UNAVAILABLE", "Failed to delete credential file"
            ) from exc

    @staticmethod
    def backend_name() -> str:
        """Return a secret-free credential backend identifier."""
        return "local-file-0600"
