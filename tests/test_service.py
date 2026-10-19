from typing import Any

from terminal_control_mcp.errors import TerminalControlError
from terminal_control_mcp.service import TerminalControlService


class FakeTransport:
    def __init__(self, failing_node: str | None = None):
        self.failing_node = failing_node
        self.calls: list[tuple[str, str, int]] = []

    async def execute(self, target, command: str, *, timeout: int = 30) -> dict[str, Any]:
        self.calls.append((target.target_id, command, timeout))
        if target.node == self.failing_node:
            raise TerminalControlError("UNREACHABLE", "SSH target is unreachable")
        return {
            "command": command,
            "exit_code": 0,
            "stdout": f"{target.base_id}-{target.node}\nnvidia\n/home/nvidia\n",
            "stderr": "",
            "cwd": "/home/nvidia",
            "truncated": False,
            "timed_out": False,
            "duration_ms": 12,
        }

    async def close(self) -> None:
        return None


async def test_status_broadcast_has_per_node_results(app_config) -> None:
    service = TerminalControlService(app_config, transport=FakeTransport())

    result = await service.get_target_status("bit-test:*")

    assert result["status"] == "ok"
    assert result["broadcast"] is True
    assert [item["node"] for item in result["results"]] == ["media", "main"]
    assert all(item["data"]["online"] for item in result["results"])


async def test_broadcast_contains_stable_partial_error(app_config) -> None:
    service = TerminalControlService(app_config, transport=FakeTransport(failing_node="main"))

    result = await service.get_target_status("bit-test:*")

    assert result["status"] == "ok"
    assert result["results"][1]["error"] == {
        "code": "UNREACHABLE",
        "message": "SSH target is unreachable",
    }


async def test_run_command_uses_default_media_node(app_config) -> None:
    transport = FakeTransport()
    service = TerminalControlService(app_config, transport=transport)

    result = await service.run_command("bit-test", "printf hello", timeout=17)

    assert result["status"] == "ok"
    assert transport.calls == [("bit-test:media", "printf hello", 17)]


async def test_run_command_supports_standalone_host(app_config) -> None:
    transport = FakeTransport()
    service = TerminalControlService(app_config, transport=transport)

    result = await service.run_command("my-ubuntu-server", "printf hello", timeout=17)

    assert result["status"] == "ok"
    assert transport.calls == [("my-ubuntu-server:host", "printf hello", 17)]


async def test_invalid_target_returns_error_envelope(app_config) -> None:
    service = TerminalControlService(app_config, transport=FakeTransport())

    result = await service.run_command("missing", "true")

    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_TARGET"
