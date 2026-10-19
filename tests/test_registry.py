import pytest

from terminal_control_mcp.errors import TerminalControlError
from terminal_control_mcp.registry import TargetRegistry


def test_resolves_default_and_named_nodes(app_config) -> None:
    registry = TargetRegistry(app_config)

    assert registry.resolve("bit-test")[0].target_id == "bit-test:media"
    assert registry.resolve("bit-test:main")[0].target_id == "bit-test:main"
    assert [item.node for item in registry.resolve("bit-test:*")] == ["media", "main"]
    assert registry.resolve("my-ubuntu-server")[0].target_id == "my-ubuntu-server:host"


def test_rejects_unknown_and_disallowed_broadcast(app_config) -> None:
    registry = TargetRegistry(app_config)

    with pytest.raises(TerminalControlError, match="Unknown group"):
        registry.resolve("missing")
    with pytest.raises(TerminalControlError, match="does not support broadcast"):
        registry.resolve("bit-test:*", allow_broadcast=False)


def test_inventory_projection_excludes_connection_details(app_config) -> None:
    registry = TargetRegistry(app_config)
    projection = registry.list_groups()

    assert projection == [{"group_id": "bit-test", "nodes": ["media", "main"]}]
    assert "host" not in str(projection)
    assert registry.list_hosts() == [{"host_id": "my-ubuntu-server"}]
