"""STDIO MCP server exposing generic SSH-based terminal controls."""

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings
from mcp.types import ToolAnnotations

from terminal_control_mcp.service import TerminalControlService

logger = logging.getLogger(__name__)

# MCP 1.x leaves this generic forward reference unresolved on Python 3.13.
Settings.model_rebuild()


def build_server(config_path: str | None = None) -> FastMCP:
    """Build a standalone MCP server bound to one local inventory."""
    service: TerminalControlService | None = None

    @asynccontextmanager
    async def lifespan(_) -> AsyncIterator[None]:
        nonlocal service
        service = TerminalControlService.from_config(config_path)
        logger.info(
            "terminal-control started with %d groups and %d standalone hosts",
            service.registry.count,
            service.registry.host_count,
        )
        try:
            yield
        finally:
            await service.close()
            service = None
            logger.info("terminal-control stopped")

    def get_service() -> TerminalControlService:
        if service is None:
            raise RuntimeError("terminal-control service is not running")
        return service

    mcp = FastMCP(
        name="terminal-control",
        instructions=(
            "Control configured Linux terminals over SSH. Targets are standalone host IDs, "
            "group IDs, group:node, or group:* where supported. "
            "Use background mode for commands which may run longer than 30 seconds."
        ),
        lifespan=lifespan,
        log_level="WARNING",
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Terminal control health",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    async def health_check() -> dict[str, Any]:
        """Check the local terminal-control process without contacting targets."""
        return get_service().health_check()

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List terminal targets",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    async def list_targets() -> dict[str, Any]:
        """List configured target groups and standalone hosts without secrets."""
        return get_service().list_targets()

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get target status",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    async def get_target_status(target: str) -> dict[str, Any]:
        """Check SSH reachability and identity for a host, group node, or group broadcast."""
        return await get_service().get_target_status(target)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Run target command",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        )
    )
    async def run_command(
        target: str,
        command: str,
        background: bool = False,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Execute an unrestricted shell command on a target.

        Use foreground mode only for short commands. Use background=True for long-running work,
        then inspect it with task_control. Foreground timeout is clamped to 1-300 seconds.
        Supported selectors are host, group, group:media, group:main, and group:*.
        """
        return await get_service().run_command(target, command, background, timeout)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Control target task",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        )
    )
    async def task_control(
        target: str,
        action: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """List, inspect, or cancel a background task on one concrete target.

        action is list, status, or cancel. task_id is required for status and cancel.
        Task registration is local and is cleared when the MCP server restarts.
        """
        return await get_service().task_control(target, action, task_id)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Write target file",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    async def file_write(
        target: str,
        file_path: str,
        content: str,
    ) -> dict[str, Any]:
        """Atomically write UTF-8 text under configured safe roots on a target."""
        return await get_service().file_write(target, file_path, content)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Edit target file",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        )
    )
    async def file_edit(
        target: str,
        file_path: str,
        old_string: str,
        new_string: str,
    ) -> dict[str, Any]:
        """Atomically replace old_string when it occurs exactly once in a safe UTF-8 file."""
        return await get_service().file_edit(target, file_path, old_string, new_string)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Upload target file",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    async def file_upload(
        target: str,
        local_path: str,
        remote_path: str,
    ) -> dict[str, Any]:
        """Atomically upload and SHA256-verify one local file on a target."""
        return await get_service().file_upload(target, local_path, remote_path)

    return mcp


def run_server(config_path: str | None = None) -> None:
    """Run the MCP server over STDIO; protocol output remains isolated from logs."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("terminal_control_mcp").setLevel(logging.INFO)
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    build_server(config_path).run(transport="stdio")
