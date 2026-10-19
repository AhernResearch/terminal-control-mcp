"""Group and standalone-host target parsing and inventory projection."""

from typing import Any, ClassVar

from terminal_control_mcp.errors import TerminalControlError
from terminal_control_mcp.models import AppConfig, Target


class TargetRegistry:
    """Resolve user-facing selectors into concrete media/main nodes."""

    VALID_NODES: ClassVar[set[str]] = {"media", "main"}

    def __init__(self, config: AppConfig):
        self._config = config

    def resolve(self, selector: str, *, allow_broadcast: bool = True) -> list[Target]:
        """Resolve base, named-node, and one-group broadcast selectors."""
        if not isinstance(selector, str) or not selector:
            raise TerminalControlError("INVALID_TARGET", "group_id is required")
        host = self._config.hosts.get(selector)
        if host is not None:
            return [Target(selector, "host", host)]
        if ":" in selector:
            group_id, node = selector.rsplit(":", 1)
        else:
            group_id, node = selector, "media"

        group = self._config.groups.get(group_id)
        if group is None:
            raise TerminalControlError("INVALID_TARGET", f"Unknown group: {group_id}")
        if node == "*":
            if not allow_broadcast:
                raise TerminalControlError(
                    "INVALID_TARGET", "This tool does not support broadcast targets"
                )
            return [
                Target(group_id, "media", group.media),
                Target(group_id, "main", group.main),
            ]
        if node not in self.VALID_NODES:
            raise TerminalControlError("INVALID_TARGET", f"Unknown group node: {node}")
        node_config = group.media if node == "media" else group.main
        return [Target(group_id, node, node_config)]

    def list_groups(self) -> list[dict[str, Any]]:
        """Return secret-free inventory metadata."""
        return [
            {"group_id": group_id, "nodes": ["media", "main"]}
            for group_id in sorted(self._config.groups)
        ]

    def list_hosts(self) -> list[dict[str, Any]]:
        """Return secret-free standalone host metadata."""
        return [{"host_id": host_id} for host_id in sorted(self._config.hosts)]

    @property
    def count(self) -> int:
        return len(self._config.groups)

    @property
    def host_count(self) -> int:
        return len(self._config.hosts)
