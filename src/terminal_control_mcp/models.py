"""Core immutable configuration and target models."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class NodeConfig:
    """Resolved SSH configuration for one terminal target."""

    host: str
    port: int
    username: str
    credential_ref: str | None
    client_key_file: Path | None
    known_hosts_file: Path
    connect_timeout: int
    allowed_write_roots: tuple[str, ...]
    via: str | None = None
    host_key_alias: str | None = None


@dataclass(frozen=True)
class GroupConfig:
    """Media and main nodes belonging to one target group."""

    group_id: str
    media: NodeConfig
    main: NodeConfig


@dataclass(frozen=True)
class AppConfig:
    """Validated application configuration."""

    version: int
    groups: dict[str, GroupConfig]
    source_path: Path
    allowed_local_read_roots: tuple[Path, ...] = ()
    hosts: dict[str, NodeConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class Target:
    """A single resolved node target."""

    base_id: str
    node: str
    config: NodeConfig

    @property
    def target_id(self) -> str:
        return f"{self.base_id}:{self.node}"
