"""Remote background-task lifecycle implemented entirely over SSH/SFTP."""

import asyncio
import json
import posixpath
import shlex
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import asyncssh

from terminal_control_mcp.errors import TerminalControlError
from terminal_control_mcp.models import Target
from terminal_control_mcp.transport import OUTPUT_LIMIT, AsyncSshTransport

MAX_RUNNING_PER_TARGET = 8
COMPLETED_TTL_SECONDS = 3600
ORPHAN_TTL_SECONDS = 86400
BACKGROUND_FILE_LIMIT_BLOCKS = 102400  # 50 MiB with 512-byte shell ulimit blocks


@dataclass
class RemoteTask:
    """In-memory registration of a detached remote task."""

    task_id: str
    target_id: str
    command: str
    pid: int
    remote_dir: str
    started_at: float
    status: str = "running"
    exit_code: int | None = None
    finished_at: float | None = None


class TaskManager:
    """Launch, inspect, and cancel detached commands without a remote agent."""

    def __init__(self, transport: AsyncSshTransport):
        self._transport = transport
        self._tasks: dict[str, RemoteTask] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    async def _lstat_optional(
        sftp: asyncssh.SFTPClient,
        path: str,
    ) -> asyncssh.SFTPAttrs | None:
        try:
            return await sftp.lstat(path)
        except asyncssh.SFTPNoSuchFile:
            return None

    @classmethod
    async def _mkdirs(
        cls,
        sftp: asyncssh.SFTPClient,
        path: str,
        mode: int = 0o700,
    ) -> None:
        current = "/"
        for part in [part for part in path.split("/") if part]:
            current = posixpath.join(current, part)
            attrs = await cls._lstat_optional(sftp, current)
            if attrs is None:
                await sftp.mkdir(current)
                await sftp.chmod(current, mode)

    @staticmethod
    async def _write_bytes(
        sftp: asyncssh.SFTPClient,
        path: str,
        data: bytes,
        mode: int = 0o600,
    ) -> None:
        async with sftp.open(path, "wb", encoding=None) as remote_file:
            await remote_file.write(data)
        await sftp.chmod(path, mode)

    @staticmethod
    async def _read_text_optional(
        sftp: asyncssh.SFTPClient,
        path: str,
    ) -> str | None:
        try:
            async with sftp.open(path, "rb", encoding=None) as remote_file:
                data = await remote_file.read(65536)
        except asyncssh.SFTPNoSuchFile:
            return None
        return data.decode("utf-8", errors="replace").strip()

    @staticmethod
    async def _run_small(
        connection: asyncssh.SSHClientConnection,
        command: str,
        timeout: int = 10,
    ) -> asyncssh.SSHCompletedProcess:
        try:
            return await connection.run(
                command,
                check=False,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:
            raise AsyncSshTransport.translate_error(exc) from exc

    async def _is_session_alive(
        self,
        connection: asyncssh.SSHClientConnection,
        pid: int,
    ) -> bool:
        result = await self._run_small(
            connection,
            f"pgrep -s {pid} >/dev/null 2>&1",
        )
        return result.exit_status == 0

    @staticmethod
    async def _tail(
        sftp: asyncssh.SFTPClient,
        path: str,
        lines: int = 100,
    ) -> tuple[str, bool]:
        attrs = await TaskManager._lstat_optional(sftp, path)
        if attrs is None or not attrs.size:
            return "", False
        read_size = min(attrs.size, 256 * 1024)
        async with sftp.open(path, "rb", encoding=None) as remote_file:
            data = await remote_file.read(read_size, offset=attrs.size - read_size)
        text = data.decode("utf-8", errors="replace")
        output = "".join(text.splitlines(keepends=True)[-lines:])
        truncated = len(output) > OUTPUT_LIMIT or attrs.size > read_size
        output = output[:OUTPUT_LIMIT]
        if truncated:
            output += "\n... [truncated]"
        return output, truncated

    @staticmethod
    async def _remove_task_dir(sftp: asyncssh.SFTPClient, remote_dir: str) -> None:
        try:
            entries = [entry async for entry in sftp.scandir(remote_dir)]
        except asyncssh.SFTPNoSuchFile:
            return
        for entry in entries:
            name = entry.filename
            if name in {".", ".."}:
                continue
            path = posixpath.join(remote_dir, name)
            try:
                await sftp.remove(path)
            except asyncssh.SFTPNoSuchFile:
                pass
        try:
            await sftp.rmdir(remote_dir)
        except asyncssh.SFTPNoSuchFile:
            pass

    async def _cleanup(self, target: Target) -> None:
        """Lazily remove expired completed and dead orphan task directories."""
        now = time.time()
        async with self._transport.operation(target) as connection:
            home = await self._transport.home_directory(target, connection)
            root = posixpath.join(home, ".cache/terminal-control/tasks")
            async with connection.start_sftp_client() as sftp:
                if await self._lstat_optional(sftp, root) is None:
                    return
                entries = [entry async for entry in sftp.scandir(root)]
                for entry in entries:
                    task_id = entry.filename
                    if task_id in {".", ".."}:
                        continue
                    remote_dir = posixpath.join(root, task_id)
                    finished_text = await self._read_text_optional(
                        sftp, posixpath.join(remote_dir, "finished_at")
                    )
                    if finished_text:
                        try:
                            expired = now - float(finished_text) > COMPLETED_TTL_SECONDS
                        except ValueError:
                            expired = False
                        if expired:
                            await self._remove_task_dir(sftp, remote_dir)
                            self._tasks.pop(task_id, None)
                        continue

                    metadata_text = await self._read_text_optional(
                        sftp, posixpath.join(remote_dir, "metadata.json")
                    )
                    if not metadata_text:
                        continue
                    try:
                        metadata = json.loads(metadata_text)
                        started_at = float(metadata["started_at"])
                        pid = int(metadata["pid"])
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if now - started_at <= ORPHAN_TTL_SECONDS:
                        continue
                    if not await self._is_session_alive(connection, pid):
                        await self._remove_task_dir(sftp, remote_dir)
                        self._tasks.pop(task_id, None)

    async def _refresh(
        self,
        task: RemoteTask,
        target: Target,
        connection: asyncssh.SSHClientConnection,
        sftp: asyncssh.SFTPClient,
    ) -> None:
        if task.status != "running":
            return
        exit_text = await self._read_text_optional(
            sftp, posixpath.join(task.remote_dir, "exit_code")
        )
        finished_text = await self._read_text_optional(
            sftp, posixpath.join(task.remote_dir, "finished_at")
        )
        if exit_text is not None:
            try:
                task.exit_code = int(exit_text)
            except ValueError:
                task.exit_code = -1
            task.status = "completed" if task.exit_code == 0 else "failed"
            task.finished_at = float(finished_text) if finished_text else time.time()
            return
        if not await self._is_session_alive(connection, task.pid):
            task.status = "failed"
            task.exit_code = -1
            task.finished_at = time.time()

    async def start(self, target: Target, command: str) -> dict[str, Any]:
        """Launch a detached command and register it in local memory."""
        if not isinstance(command, str) or not command:
            raise TerminalControlError("CONFIG_ERROR", "command must be non-empty")
        await self._cleanup(target)
        async with self._lock:
            running = sum(
                1
                for task in self._tasks.values()
                if task.target_id == target.target_id and task.status == "running"
            )
            if running >= MAX_RUNNING_PER_TARGET:
                raise TerminalControlError(
                    "BUSY", f"Too many background tasks ({running}/{MAX_RUNNING_PER_TARGET})"
                )

        task_id = uuid.uuid4().hex[:12]
        started_at = time.time()
        async with self._transport.operation(target) as connection:
            home = await self._transport.home_directory(target, connection)
            remote_dir = posixpath.join(home, ".cache/terminal-control/tasks", task_id)
            async with connection.start_sftp_client() as sftp:
                await self._mkdirs(sftp, remote_dir)
                command_path = posixpath.join(remote_dir, "command.sh")
                runner_path = posixpath.join(remote_dir, "runner.sh")
                await self._write_bytes(
                    sftp,
                    command_path,
                    ("#!/usr/bin/env bash\n" + command + "\n").encode("utf-8"),
                    0o700,
                )
                quoted_dir = shlex.quote(remote_dir)
                runner = (
                    "#!/usr/bin/env bash\n"
                    f"ulimit -f {BACKGROUND_FILE_LIMIT_BLOCKS}\n"
                    'cd "$HOME" || exit 1\n'
                    f"bash {shlex.quote(command_path)}\n"
                    "code=$?\n"
                    f"printf '%s\\n' \"$code\" > {quoted_dir}/exit_code\n"
                    f"date +%s > {quoted_dir}/finished_at\n"
                    'exit "$code"\n'
                )
                await self._write_bytes(sftp, runner_path, runner.encode("utf-8"), 0o700)

            launch = (
                f"cd {shlex.quote(remote_dir)} || exit 1; "
                "setsid nohup bash runner.sh >stdout 2>stderr </dev/null & "
                "pid=$!; printf '%s\\n' \"$pid\""
            )
            result = await self._run_small(connection, launch)
            if result.exit_status != 0:
                raise TerminalControlError("TASK_CONTROL_FAILED", "Failed to start background task")
            try:
                pid = int(result.stdout.strip().splitlines()[-1])
            except (IndexError, ValueError) as exc:
                raise TerminalControlError(
                    "TASK_CONTROL_FAILED", "Remote task PID was not returned"
                ) from exc

            metadata = json.dumps(
                {
                    "task_id": task_id,
                    "target_id": target.target_id,
                    "pid": pid,
                    "started_at": started_at,
                },
                sort_keys=True,
            ).encode("utf-8")
            async with connection.start_sftp_client() as sftp:
                await self._write_bytes(
                    sftp,
                    posixpath.join(remote_dir, "metadata.json"),
                    metadata,
                )

        task = RemoteTask(task_id, target.target_id, command, pid, remote_dir, started_at)
        async with self._lock:
            self._tasks[task_id] = task
        return {
            "background": True,
            "task_id": task_id,
            "pid": pid,
            "command": command,
            "cwd": home,
            "hint": f"Use task_control(action='status', task_id='{task_id}') to check",
        }

    def _get_task(self, target: Target, task_id: str) -> RemoteTask:
        task = self._tasks.get(task_id)
        if task is None or task.target_id != target.target_id:
            raise TerminalControlError("TASK_NOT_FOUND", f"Task '{task_id}' not found")
        return task

    @staticmethod
    def _summary(task: RemoteTask) -> dict[str, Any]:
        result: dict[str, Any] = {
            "task_id": task.task_id,
            "command": task.command[:120],
            "status": task.status,
            "pid": task.pid,
            "started_at": task.started_at,
        }
        if task.finished_at is not None:
            result.update(
                {
                    "finished_at": task.finished_at,
                    "exit_code": task.exit_code,
                    "duration_s": round(task.finished_at - task.started_at, 1),
                }
            )
        else:
            result["elapsed_s"] = round(time.time() - task.started_at, 1)
        return result

    async def list(self, target: Target) -> dict[str, Any]:
        """List locally registered tasks for one concrete target."""
        await self._cleanup(target)
        tasks = [task for task in self._tasks.values() if task.target_id == target.target_id]
        async with (
            self._transport.operation(target) as connection,
            connection.start_sftp_client() as sftp,
        ):
            for task in tasks:
                await self._refresh(task, target, connection, sftp)
        return {"tasks": [self._summary(task) for task in tasks], "count": len(tasks)}

    async def status(self, target: Target, task_id: str) -> dict[str, Any]:
        """Return task state and the last 100 stdout lines."""
        await self._cleanup(target)
        task = self._get_task(target, task_id)
        async with (
            self._transport.operation(target) as connection,
            connection.start_sftp_client() as sftp,
        ):
            await self._refresh(task, target, connection, sftp)
            output, truncated = await self._tail(sftp, posixpath.join(task.remote_dir, "stdout"))
        result = self._summary(task)
        result.update(
            {
                "command": task.command,
                "output_type": "stdout",
                "output": output,
                "truncated": truncated,
            }
        )
        return result

    async def cancel(self, target: Target, task_id: str) -> dict[str, Any]:
        """Terminate every process in a registered remote session."""
        task = self._get_task(target, task_id)
        if task.status != "running":
            return self._summary(task)
        command = (
            f"pkill -TERM -s {task.pid} >/dev/null 2>&1 || true; "
            f"for i in 1 2 3 4 5; do pgrep -s {task.pid} >/dev/null 2>&1 || exit 0; "
            "sleep 1; done; "
            f"pkill -KILL -s {task.pid} >/dev/null 2>&1 || true"
        )
        async with self._transport.operation(target) as connection:
            await self._run_small(connection, command, timeout=10)
            task.status = "cancelled"
            task.exit_code = -1
            task.finished_at = time.time()
            async with connection.start_sftp_client() as sftp:
                await self._write_bytes(
                    sftp,
                    posixpath.join(task.remote_dir, "cancelled"),
                    b"1\n",
                )
                await self._write_bytes(
                    sftp,
                    posixpath.join(task.remote_dir, "finished_at"),
                    f"{task.finished_at}\n".encode(),
                )
        return self._summary(task)

    async def control(
        self,
        target: Target,
        action: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch list/status/cancel task actions."""
        if action == "list":
            return await self.list(target)
        if action in {"status", "cancel"} and not task_id:
            raise TerminalControlError("TASK_CONTROL_FAILED", f"task_id is required for {action}")
        if action == "status":
            return await self.status(target, task_id or "")
        if action == "cancel":
            return await self.cancel(target, task_id or "")
        raise TerminalControlError(
            "TASK_CONTROL_FAILED", "Unknown action; use list, status, or cancel"
        )

    def registered_tasks(self) -> Iterable[RemoteTask]:
        """Expose a read-only snapshot for tests and health reporting."""
        return tuple(self._tasks.values())
