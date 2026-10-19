import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from terminal_control_mcp.errors import TerminalControlError
from terminal_control_mcp.filesystem import RemoteFilesystem, _ResolvedPath


class MemoryFile:
    def __init__(self, storage: dict[str, bytes], path: str, mode: str):
        self._storage = storage
        self._path = path
        self._mode = mode
        self._buffer = bytearray(storage.get(path, b""))
        self._offset = 0

    async def __aenter__(self) -> "MemoryFile":
        return self

    async def __aexit__(self, *_: object) -> None:
        if "w" in self._mode:
            self._storage[self._path] = bytes(self._buffer)

    async def write(self, content: bytes) -> None:
        self._buffer.extend(content)

    async def read(self, size: int) -> bytes:
        content = bytes(self._buffer[self._offset : self._offset + size])
        self._offset += len(content)
        return content


class MemorySftp:
    def __init__(self):
        self.storage: dict[str, bytes] = {}

    async def __aenter__(self) -> "MemorySftp":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def open(self, path: str, mode: str, encoding: object = None) -> MemoryFile:
        del encoding
        return MemoryFile(self.storage, path, mode)

    async def chmod(self, path: str, mode: int) -> None:
        del path, mode

    async def posix_rename(self, source: str, target: str) -> None:
        self.storage[target] = self.storage.pop(source)

    async def remove(self, path: str) -> None:
        self.storage.pop(path, None)


class MemoryConnection:
    def __init__(self, sftp: MemorySftp):
        self._sftp = sftp

    def start_sftp_client(self) -> MemorySftp:
        return self._sftp


class MemoryTransport:
    def __init__(self):
        self.sftp = MemorySftp()

    @asynccontextmanager
    async def operation(self, target: object) -> AsyncIterator[MemoryConnection]:
        del target
        yield MemoryConnection(self.sftp)

    @staticmethod
    def translate_error(error: Exception) -> Exception:
        return error


def test_local_upload_rejects_outside_root(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    filesystem = RemoteFilesystem(MemoryTransport(), (approved,))

    with pytest.raises(TerminalControlError, match="outside allowed local"):
        filesystem._resolve_local_upload(str(outside))


def test_local_upload_rejects_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    link = tmp_path / "link.bin"
    link.symlink_to(source)
    filesystem = RemoteFilesystem(MemoryTransport(), (tmp_path,))

    with pytest.raises(TerminalControlError, match="cannot be a symlink"):
        filesystem._resolve_local_upload(str(link))


async def test_upload_is_atomic_and_checksum_verified(
    app_config: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "release.tar.gz"
    source.write_bytes(os.urandom(2 * 1024 * 1024 + 17))
    transport = MemoryTransport()
    filesystem = RemoteFilesystem(transport, (tmp_path,))

    async def resolve(*_: object, **__: object) -> _ResolvedPath:
        return _ResolvedPath("/home/nvidia/release.tar.gz", "/home/nvidia", None)

    filesystem._resolve = resolve  # type: ignore[method-assign]
    target = app_config.groups["bit-test"].media
    result = await filesystem.upload(
        type("Target", (), {"config": target})(),
        str(source),
        "~/release.tar.gz",
    )

    assert result["bytes_written"] == source.stat().st_size
    assert len(result["sha256"]) == 64
    assert transport.sftp.storage["/home/nvidia/release.tar.gz"] == source.read_bytes()
    assert not any(".tmp" in path for path in transport.sftp.storage)
