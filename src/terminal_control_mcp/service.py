"""Application service shared by MCP tools and the validation CLI."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from terminal_control_mcp import __version__
from terminal_control_mcp.config import load_config
from terminal_control_mcp.credentials import CredentialStore
from terminal_control_mcp.errors import TerminalControlError, error_response, ok_response
from terminal_control_mcp.filesystem import RemoteFilesystem
from terminal_control_mcp.models import AppConfig, Target
from terminal_control_mcp.registry import TargetRegistry
from terminal_control_mcp.tasks import TaskManager
from terminal_control_mcp.transport import AsyncSshTransport

logger = logging.getLogger(__name__)

TargetOperation = Callable[[Target], Awaitable[dict[str, Any]]]


class TerminalControlService:
    """Coordinate target resolution, SSH operations, and response envelopes."""

    def __init__(
        self,
        config: AppConfig,
        credentials: CredentialStore | None = None,
        transport: AsyncSshTransport | None = None,
    ):
        self.config = config
        self.registry = TargetRegistry(config)
        self.credentials = credentials or CredentialStore()
        self.transport = transport or AsyncSshTransport(self.registry, self.credentials)
        self.tasks = TaskManager(self.transport)
        self.filesystem = RemoteFilesystem(
            self.transport,
            config.allowed_local_read_roots,
        )

    @classmethod
    def from_config(cls, path: str | None = None) -> "TerminalControlService":
        """Build a service from a validated local inventory."""
        return cls(load_config(path))

    async def close(self) -> None:
        """Release pooled SSH sessions."""
        await self.transport.close()

    @staticmethod
    def _log(operation: str, selector: str, outcome: str) -> None:
        logger.info("operation=%s target=%s outcome=%s", operation, selector, outcome)

    async def _apply(
        self,
        operation: str,
        selector: str,
        function: TargetOperation,
        *,
        allow_broadcast: bool = True,
    ) -> dict[str, Any]:
        try:
            targets = self.registry.resolve(selector, allow_broadcast=allow_broadcast)
        except TerminalControlError as exc:
            self._log(operation, selector, exc.code)
            return error_response(exc, selector)

        if len(targets) == 1:
            try:
                data = await function(targets[0])
            except TerminalControlError as exc:
                self._log(operation, selector, exc.code)
                return error_response(exc, selector)
            except Exception:
                logger.exception(
                    "operation=%s target=%s outcome=INTERNAL_ERROR", operation, selector
                )
                return error_response(
                    TerminalControlError("INTERNAL_ERROR", "Unexpected terminal-control failure"),
                    selector,
                )
            self._log(operation, selector, "ok")
            return ok_response(selector, data=data)

        async def run_one(target: Target) -> dict[str, Any]:
            try:
                return {"node": target.node, "status": "ok", "data": await function(target)}
            except TerminalControlError as exc:
                return {
                    "node": target.node,
                    "status": "error",
                    "error": {"code": exc.code, "message": exc.message},
                }
            except Exception:
                logger.exception(
                    "operation=%s target=%s outcome=INTERNAL_ERROR",
                    operation,
                    target.target_id,
                )
                return {
                    "node": target.node,
                    "status": "error",
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "Unexpected terminal-control failure",
                    },
                }

        results = list(await asyncio.gather(*(run_one(target) for target in targets)))
        outcome = "ok" if all(item["status"] == "ok" for item in results) else "partial"
        self._log(operation, selector, outcome)
        return ok_response(
            targets[0].base_id,
            broadcast=True,
            node_count=len(results),
            results=results,
        )

    def health_check(self) -> dict[str, Any]:
        """Return local server health without opening network connections."""
        return ok_response(
            server="terminal-control",
            version=__version__,
            registered_groups=self.registry.count,
            registered_hosts=self.registry.host_count,
            registered_tasks=len(tuple(self.tasks.registered_tasks())),
        )

    def list_targets(self) -> dict[str, Any]:
        """Return the secret-free configured inventory."""
        groups = self.registry.list_groups()
        hosts = self.registry.list_hosts()
        return ok_response(
            data={
                "groups": groups,
                "hosts": hosts,
                "group_count": len(groups),
                "host_count": len(hosts),
            }
        )

    async def get_target_status(self, target: str) -> dict[str, Any]:
        """Check SSH reachability and basic identity for one node or both nodes."""

        async def get_status(target: Target) -> dict[str, Any]:
            result = await self.transport.execute(
                target,
                "hostname; id -un; pwd",
                timeout=min(target.config.connect_timeout + 5, 30),
            )
            lines = result["stdout"].splitlines()
            if result["exit_code"] != 0 or len(lines) < 3:
                return {
                    "online": False,
                    "node": target.node,
                    "latency_ms": result["duration_ms"],
                    "error": "Remote identity check failed",
                }
            return {
                "online": True,
                "node": target.node,
                "hostname": lines[0],
                "username": lines[1],
                "cwd": lines[2],
                "latency_ms": result["duration_ms"],
            }

        return await self._apply("get_target_status", target, get_status)

    async def run_command(
        self,
        target: str,
        command: str,
        background: bool = False,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Run a foreground or detached shell command on selected targets."""

        async def run(target: Target) -> dict[str, Any]:
            if background:
                return await self.tasks.start(target, command)
            return await self.transport.execute(target, command, timeout=timeout)

        return await self._apply("run_command", target, run)

    async def task_control(
        self,
        target: str,
        action: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """List, inspect, or cancel locally registered background tasks."""

        async def control(target: Target) -> dict[str, Any]:
            return await self.tasks.control(target, action, task_id)

        return await self._apply(
            "task_control",
            target,
            control,
            allow_broadcast=False,
        )

    async def file_write(
        self,
        target: str,
        file_path: str,
        content: str,
    ) -> dict[str, Any]:
        """Atomically write text within configured safe roots."""

        async def write(target: Target) -> dict[str, Any]:
            return await self.filesystem.write(target, file_path, content)

        return await self._apply("file_write", target, write)

    async def file_edit(
        self,
        target: str,
        file_path: str,
        old_string: str,
        new_string: str,
    ) -> dict[str, Any]:
        """Atomically replace one unique text occurrence within safe roots."""

        async def edit(target: Target) -> dict[str, Any]:
            return await self.filesystem.edit(target, file_path, old_string, new_string)

        return await self._apply("file_edit", target, edit)

    async def file_upload(
        self,
        target: str,
        local_path: str,
        remote_path: str,
    ) -> dict[str, Any]:
        """Atomically upload one local binary file to an allowed remote root."""

        async def upload(target: Target) -> dict[str, Any]:
            return await self.filesystem.upload(target, local_path, remote_path)

        return await self._apply("file_upload", target, upload)
