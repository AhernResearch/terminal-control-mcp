"""AsyncSSH transport with connection pooling and bounded execution."""

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncssh

from terminal_control_mcp.credentials import CredentialStore
from terminal_control_mcp.errors import TerminalControlError
from terminal_control_mcp.models import Target
from terminal_control_mcp.registry import TargetRegistry

OUTPUT_LIMIT = 50_000
COMMAND_TIMEOUT_MIN = 1
COMMAND_TIMEOUT_MAX = 300


@dataclass
class _PooledConnection:
    connection: asyncssh.SSHClientConnection
    last_used: float


class AsyncSshTransport:
    """SSH transport for password or key-authenticated direct and tunneled nodes."""

    def __init__(
        self,
        registry: TargetRegistry,
        credentials: CredentialStore,
        *,
        idle_timeout: float = 300,
        acquire_timeout: float = 10,
        per_target_limit: int = 4,
        global_limit: int = 16,
    ):
        self._registry = registry
        self._credentials = credentials
        self._idle_timeout = idle_timeout
        self._acquire_timeout = acquire_timeout
        self._connections: dict[str, _PooledConnection] = {}
        self._connection_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._target_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(per_target_limit)
        )
        self._global_semaphore = asyncio.Semaphore(global_limit)
        self._home_dirs: dict[str, str] = {}

    async def close(self) -> None:
        """Close all pooled SSH connections."""
        pooled = list(self._connections.values())
        self._connections.clear()
        self._home_dirs.clear()
        for item in pooled:
            item.connection.close()
        await asyncio.gather(
            *(item.connection.wait_closed() for item in pooled),
            return_exceptions=True,
        )

    async def _close_target(self, target_id: str) -> None:
        item = self._connections.pop(target_id, None)
        self._home_dirs.pop(target_id, None)
        if item:
            item.connection.close()
            await item.connection.wait_closed()
        if target_id.endswith(":media"):
            main_id = target_id.removesuffix(":media") + ":main"
            main = self._connections.pop(main_id, None)
            self._home_dirs.pop(main_id, None)
            if main:
                main.connection.close()
                await main.connection.wait_closed()

    async def _close_idle(self) -> None:
        now = time.monotonic()
        stale = [
            target_id
            for target_id, item in self._connections.items()
            if item.connection.is_closed() or now - item.last_used >= self._idle_timeout
        ]
        for target_id in stale:
            if target_id in self._connections:
                await self._close_target(target_id)

    async def _password(self, credential_ref: str) -> str:
        return await asyncio.to_thread(self._credentials.get, credential_ref)

    async def _connect(self, target: Target) -> asyncssh.SSHClientConnection:
        tunnel: asyncssh.SSHClientConnection | tuple[()] = ()
        if target.node == "main":
            media = self._registry.resolve(f"{target.base_id}:media")[0]
            tunnel = await self.connection(media)

        kwargs: dict[str, Any] = {
            "host": target.config.host,
            "port": target.config.port,
            "username": target.config.username,
            "known_hosts": str(target.config.known_hosts_file),
            "agent_path": None,
        }
        if target.config.credential_ref is not None:
            kwargs.update(
                password=await self._password(target.config.credential_ref),
                client_keys=[],
                public_key_auth=False,
                password_auth=True,
                kbdint_auth=True,
            )
        else:
            kwargs.update(
                client_keys=[str(target.config.client_key_file)],
                public_key_auth=True,
                password_auth=False,
                kbdint_auth=False,
            )
        if target.config.host_key_alias:
            kwargs["host_key_alias"] = target.config.host_key_alias
        if tunnel:
            kwargs["tunnel"] = tunnel

        try:
            connection = await asyncio.wait_for(
                asyncssh.connect(**kwargs),
                timeout=target.config.connect_timeout,
            )
        except Exception as exc:
            raise self.translate_error(exc) from exc
        connection.set_keepalive(30, 3)
        return connection

    async def connection(self, target: Target) -> asyncssh.SSHClientConnection:
        """Return a live pooled connection for a single target."""
        await self._close_idle()
        lock = self._connection_locks[target.target_id]
        async with lock:
            item = self._connections.get(target.target_id)
            if item and not item.connection.is_closed():
                item.last_used = time.monotonic()
                return item.connection
            if item:
                await self._close_target(target.target_id)
            connection = await self._connect(target)
            self._connections[target.target_id] = _PooledConnection(
                connection=connection,
                last_used=time.monotonic(),
            )
            return connection

    def _touch(self, target: Target) -> None:
        item = self._connections.get(target.target_id)
        if item:
            item.last_used = time.monotonic()

    @asynccontextmanager
    async def operation(
        self,
        target: Target,
    ) -> AsyncIterator[asyncssh.SSHClientConnection]:
        """Acquire bounded global/target capacity and yield a connection."""
        target_semaphore = self._target_semaphores[target.target_id]
        global_acquired = False
        target_acquired = False
        try:
            await asyncio.wait_for(self._global_semaphore.acquire(), timeout=self._acquire_timeout)
            global_acquired = True
            await asyncio.wait_for(target_semaphore.acquire(), timeout=self._acquire_timeout)
            target_acquired = True
        except asyncio.TimeoutError as exc:
            if target_acquired:
                target_semaphore.release()
            if global_acquired:
                self._global_semaphore.release()
            raise TerminalControlError("BUSY", "SSH concurrency limit reached") from exc

        try:
            yield await self.connection(target)
        finally:
            self._touch(target)
            if target_acquired:
                target_semaphore.release()
            if global_acquired:
                self._global_semaphore.release()

    async def home_directory(
        self,
        target: Target,
        connection: asyncssh.SSHClientConnection | None = None,
    ) -> str:
        """Resolve and cache the remote login home directory."""
        cached = self._home_dirs.get(target.target_id)
        if cached:
            return cached
        conn = connection or await self.connection(target)
        try:
            async with conn.start_sftp_client() as sftp:
                home = await sftp.realpath(".")
        except Exception as exc:
            raise self.translate_error(exc) from exc
        if not isinstance(home, str):
            raise TerminalControlError("INTERNAL_ERROR", "Remote home path is not text")
        self._home_dirs[target.target_id] = home
        return home

    @staticmethod
    async def _read_limited(reader: Any) -> tuple[str, bool]:
        parts: list[str] = []
        captured = 0
        truncated = False
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            remaining = OUTPUT_LIMIT - captured
            if remaining > 0:
                parts.append(chunk[:remaining])
                captured += min(len(chunk), remaining)
            if len(chunk) > remaining:
                truncated = True
        text = "".join(parts)
        if truncated:
            text += "\n... [truncated]"
        return text, truncated

    async def execute(
        self,
        target: Target,
        command: str,
        *,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Execute a foreground command using the remote account shell."""
        if not isinstance(command, str) or not command:
            raise TerminalControlError("CONFIG_ERROR", "command must be non-empty")
        timeout = max(COMMAND_TIMEOUT_MIN, min(timeout, COMMAND_TIMEOUT_MAX))
        started = time.monotonic()
        timed_out = False

        async with self.operation(target) as connection:
            cwd = await self.home_directory(target, connection)
            try:
                process = await connection.create_process(
                    command,
                    encoding="utf-8",
                    errors="replace",
                )
                process.stdin.write_eof()
                stdout_task = asyncio.create_task(self._read_limited(process.stdout))
                stderr_task = asyncio.create_task(self._read_limited(process.stderr))
                try:
                    await asyncio.wait_for(process.wait_closed(), timeout=timeout)
                except asyncio.TimeoutError:
                    timed_out = True
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait_closed(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait_closed()
                except asyncio.CancelledError:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait_closed(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                    raise
                stdout_result, stderr_result = await asyncio.gather(
                    stdout_task,
                    stderr_task,
                )
            except TerminalControlError:
                raise
            except Exception as exc:
                raise self.translate_error(exc) from exc

        stdout, stdout_truncated = stdout_result
        stderr, stderr_truncated = stderr_result
        exit_status = process.exit_status
        exit_code = exit_status if isinstance(exit_status, int) else -1
        return {
            "command": command,
            "exit_code": -1 if timed_out else exit_code,
            "stdout": stdout,
            "stderr": stderr if not timed_out else stderr or f"Command timed out after {timeout}s",
            "cwd": cwd,
            "truncated": stdout_truncated or stderr_truncated,
            "timed_out": timed_out,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    @staticmethod
    def translate_error(exc: Exception) -> TerminalControlError:
        """Map AsyncSSH and network failures to stable public errors."""
        if isinstance(exc, TerminalControlError):
            return exc
        if isinstance(exc, asyncio.TimeoutError):
            return TerminalControlError("CONNECT_TIMEOUT", "SSH connection timed out")
        if isinstance(exc, asyncssh.PermissionDenied):
            return TerminalControlError("AUTH_FAILED", "SSH authentication failed")
        if isinstance(exc, asyncssh.HostKeyNotVerifiable):
            return TerminalControlError(
                "HOST_KEY_ERROR", "SSH host key is missing or does not match"
            )
        if isinstance(
            exc,
            (
                asyncssh.ChannelOpenError,
                asyncssh.ConnectionLost,
                asyncssh.DisconnectError,
                OSError,
            ),
        ):
            return TerminalControlError("UNREACHABLE", "SSH target is unreachable")
        return TerminalControlError("INTERNAL_ERROR", f"SSH operation failed: {type(exc).__name__}")
