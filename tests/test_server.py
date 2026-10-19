from terminal_control_mcp.server import build_server


async def test_exposes_expected_generic_remote_tools() -> None:
    server = build_server("/tmp/config-is-loaded-only-during-lifespan.yaml")

    tools = {tool.name: tool for tool in await server.list_tools()}

    assert set(tools) == {
        "health_check",
        "list_targets",
        "get_target_status",
        "run_command",
        "task_control",
        "file_write",
        "file_edit",
        "file_upload",
    }
    assert tools["run_command"].inputSchema["properties"]["background"]["default"] is False
    assert tools["run_command"].inputSchema["properties"]["timeout"]["default"] == 30
    assert tools["run_command"].annotations.destructiveHint is True
    assert tools["list_targets"].annotations.readOnlyHint is True
