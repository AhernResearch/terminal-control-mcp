"""Strict, secret-free terminal target inventory loading."""

import os
import re
import stat
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path
from typing import Any

import yaml

from terminal_control_mcp.errors import TerminalControlError
from terminal_control_mcp.models import AppConfig, GroupConfig, NodeConfig

DEFAULT_CONFIG_PATH = Path("~/.config/terminal-control-mcp/targets.yaml").expanduser()
_TARGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TOP_KEYS = {
    "version",
    "defaults",
    "groups",
    "vehicles",
    "hosts",
    "allowed_local_read_roots",
}
_DEFAULT_KEYS = {
    "username",
    "port",
    "credential_ref",
    "client_key_file",
    "known_hosts_file",
    "connect_timeout",
    "allowed_write_roots",
}
_NODE_KEYS = _DEFAULT_KEYS | {"host", "via", "host_key_alias"}


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader which rejects duplicate mapping keys."""


def _construct_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> MutableMapping[str, Any]:
    mapping: MutableMapping[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise TerminalControlError("CONFIG_ERROR", "YAML mapping keys must be strings")
        if key in mapping:
            raise TerminalControlError("CONFIG_ERROR", f"Duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def resolve_config_path(explicit_path: str | Path | None = None) -> Path:
    """Resolve CLI, environment, and default configuration precedence."""
    if explicit_path is not None:
        return Path(explicit_path).expanduser()
    configured = os.getenv("TERMINAL_CONTROL_MCP_CONFIG")
    return Path(configured).expanduser() if configured else DEFAULT_CONFIG_PATH


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise TerminalControlError(
            "CONFIG_ERROR", f"Unknown {context} fields: {', '.join(unknown)}"
        )


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TerminalControlError("CONFIG_ERROR", f"{context} must be a mapping")
    return value


def _check_secure_path(path: Path) -> None:
    """Require an operator-owned, non-symlink 0600 file under a 0700 directory."""
    if path.is_symlink() or not path.is_file():
        raise TerminalControlError("CONFIG_ERROR", f"Config is missing or unsafe: {path}")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise TerminalControlError("CONFIG_ERROR", f"Config directory is unsafe: {parent}")

    file_stat = path.stat()
    parent_stat = parent.stat()
    if file_stat.st_uid != os.getuid() or parent_stat.st_uid != os.getuid():
        raise TerminalControlError("CONFIG_ERROR", "Config path must be owned by current user")
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise TerminalControlError("CONFIG_ERROR", "Config file permissions must be 0600")
    if stat.S_IMODE(parent_stat.st_mode) != 0o700:
        raise TerminalControlError("CONFIG_ERROR", "Config directory permissions must be 0700")


def _as_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        raise TerminalControlError("CONFIG_ERROR", f"{field} must be a non-empty string")
    return value


def _as_path_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise TerminalControlError("CONFIG_ERROR", f"{field} must be a non-empty path")
    return value


def _as_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise TerminalControlError(
            "CONFIG_ERROR", f"{field} must be an integer in [{minimum}, {maximum}]"
        )
    return value


def _as_roots(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise TerminalControlError("CONFIG_ERROR", "allowed_write_roots must be a non-empty list")
    roots = tuple(_as_path_string(item, "allowed_write_roots item") for item in value)
    if any(root != "~" and not root.startswith("/") for root in roots):
        raise TerminalControlError(
            "CONFIG_ERROR", "allowed_write_roots entries must be '~' or absolute paths"
        )
    return roots


def _as_local_roots(value: Any, source: Path) -> tuple[Path, ...]:
    if value is None:
        return (source.parent.resolve(),)
    if not isinstance(value, list) or not value:
        raise TerminalControlError(
            "CONFIG_ERROR", "allowed_local_read_roots must be a non-empty list"
        )
    roots: list[Path] = []
    for item in value:
        raw = _as_path_string(item, "allowed_local_read_roots item")
        root = Path(raw).expanduser()
        if not root.is_absolute():
            raise TerminalControlError(
                "CONFIG_ERROR", "allowed_local_read_roots entries must be absolute paths"
            )
        if root.is_symlink() or not root.is_dir():
            raise TerminalControlError("CONFIG_ERROR", f"Unsafe local read root: {root}")
        roots.append(root.resolve())
    return tuple(roots)


def _build_node(
    raw: Mapping[str, Any],
    defaults: Mapping[str, Any],
    context: str,
) -> NodeConfig:
    _reject_unknown(raw, _NODE_KEYS, context)
    merged: dict[str, Any] = dict(defaults)
    merged.update(raw)
    if "client_key_file" in raw and "credential_ref" not in raw:
        merged.pop("credential_ref", None)
    if "credential_ref" in raw and "client_key_file" not in raw:
        merged.pop("client_key_file", None)
    required = {
        "host",
        "username",
        "port",
        "known_hosts_file",
        "connect_timeout",
        "allowed_write_roots",
    }
    missing = sorted(required - set(merged))
    if missing:
        raise TerminalControlError(
            "CONFIG_ERROR", f"Missing {context} fields: {', '.join(missing)}"
        )

    auth_fields = {field for field in ("credential_ref", "client_key_file") if field in merged}
    if len(auth_fields) != 1:
        raise TerminalControlError(
            "CONFIG_ERROR",
            f"{context} must define exactly one of credential_ref or client_key_file",
        )

    via = merged.get("via")
    if via is not None and via != "media":
        raise TerminalControlError("CONFIG_ERROR", f"{context}.via must be 'media'")
    alias = merged.get("host_key_alias")
    if alias is not None:
        alias = _as_nonempty_string(alias, f"{context}.host_key_alias")

    known_hosts = Path(
        _as_path_string(merged["known_hosts_file"], f"{context}.known_hosts_file")
    ).expanduser()
    if not known_hosts.is_file():
        raise TerminalControlError(
            "CONFIG_ERROR", f"Known-hosts file does not exist: {known_hosts}"
        )

    credential_ref = None
    client_key_file = None
    if "credential_ref" in merged:
        credential_ref = _as_nonempty_string(merged["credential_ref"], f"{context}.credential_ref")
    else:
        client_key_file = Path(
            _as_path_string(merged["client_key_file"], f"{context}.client_key_file")
        ).expanduser()
        _check_private_key(client_key_file, context)

    return NodeConfig(
        host=_as_nonempty_string(merged["host"], f"{context}.host"),
        port=_as_int(merged["port"], f"{context}.port", 1, 65535),
        username=_as_nonempty_string(merged["username"], f"{context}.username"),
        credential_ref=credential_ref,
        client_key_file=client_key_file,
        known_hosts_file=known_hosts,
        connect_timeout=_as_int(merged["connect_timeout"], f"{context}.connect_timeout", 1, 120),
        allowed_write_roots=_as_roots(merged["allowed_write_roots"]),
        via=via,
        host_key_alias=alias,
    )


def load_config(
    path: str | Path | None = None,
    *,
    check_permissions: bool = True,
) -> AppConfig:
    """Load and strictly validate the configured terminal inventory."""
    source = resolve_config_path(path)
    if check_permissions:
        _check_secure_path(source)
    elif not source.is_file():
        raise TerminalControlError("CONFIG_ERROR", f"Config file does not exist: {source}")

    try:
        raw = yaml.load(source.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except TerminalControlError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise TerminalControlError("CONFIG_ERROR", f"Failed to read config: {exc}") from exc

    root = _require_mapping(raw, "config")
    _reject_unknown(root, _TOP_KEYS, "config")
    if type(root.get("version")) is not int or root["version"] != 1:
        raise TerminalControlError("CONFIG_ERROR", "Config version must be 1")

    defaults = _require_mapping(root.get("defaults"), "defaults")
    _reject_unknown(defaults, _DEFAULT_KEYS, "defaults")
    if "groups" in root and "vehicles" in root:
        raise TerminalControlError("CONFIG_ERROR", "Use groups or vehicles, not both")
    groups_raw = _require_mapping(root.get("groups", root.get("vehicles", {})), "groups")
    hosts_raw = _require_mapping(root.get("hosts", {}), "hosts")
    if not groups_raw and not hosts_raw:
        raise TerminalControlError("CONFIG_ERROR", "At least one group or host is required")

    groups: dict[str, GroupConfig] = {}
    for group_id, raw_group_value in groups_raw.items():
        if not isinstance(group_id, str) or not _TARGET_ID_RE.fullmatch(group_id):
            raise TerminalControlError("CONFIG_ERROR", f"Invalid group ID: {group_id}")
        raw_group = _require_mapping(raw_group_value, f"group {group_id}")
        _reject_unknown(raw_group, {"media", "main"}, f"group {group_id}")
        if set(raw_group) != {"media", "main"}:
            raise TerminalControlError(
                "CONFIG_ERROR", f"Group {group_id} must define media and main"
            )
        media = _build_node(
            _require_mapping(raw_group["media"], f"{group_id}.media"),
            defaults,
            f"{group_id}.media",
        )
        main = _build_node(
            _require_mapping(raw_group["main"], f"{group_id}.main"),
            defaults,
            f"{group_id}.main",
        )
        if media.via is not None:
            raise TerminalControlError("CONFIG_ERROR", f"{group_id}.media cannot use a tunnel")
        if main.via != "media" or not main.host_key_alias:
            raise TerminalControlError(
                "CONFIG_ERROR",
                f"{group_id}.main requires via=media and host_key_alias",
            )
        groups[group_id] = GroupConfig(group_id, media, main)

    hosts: dict[str, NodeConfig] = {}
    for host_id, raw_host_value in hosts_raw.items():
        if not isinstance(host_id, str) or not _TARGET_ID_RE.fullmatch(host_id):
            raise TerminalControlError("CONFIG_ERROR", f"Invalid host ID: {host_id}")
        if host_id in groups:
            raise TerminalControlError(
                "CONFIG_ERROR", f"Target ID is used by both a group and host: {host_id}"
            )
        host = _build_node(
            _require_mapping(raw_host_value, f"host {host_id}"),
            defaults,
            f"host {host_id}",
        )
        if host.via is not None:
            raise TerminalControlError("CONFIG_ERROR", f"Host {host_id} cannot use a tunnel")
        hosts[host_id] = host

    return AppConfig(
        version=1,
        groups=groups,
        source_path=source,
        allowed_local_read_roots=_as_local_roots(root.get("allowed_local_read_roots"), source),
        hosts=hosts,
    )


def credential_refs(config: AppConfig) -> Iterable[str]:
    """Return unique configured credential references."""
    return sorted(
        {
            node.credential_ref
            for group in config.groups.values()
            for node in (group.media, group.main)
            if node.credential_ref is not None
        }
        | {host.credential_ref for host in config.hosts.values() if host.credential_ref is not None}
    )


def _check_private_key(path: Path, context: str) -> None:
    """Require an owner-only regular private-key file."""
    if path.is_symlink() or not path.is_file():
        raise TerminalControlError("CONFIG_ERROR", f"Private key is missing or unsafe: {path}")
    metadata = path.stat()
    if metadata.st_uid != os.getuid():
        raise TerminalControlError("CONFIG_ERROR", f"{context}.client_key_file must be user-owned")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise TerminalControlError("CONFIG_ERROR", f"{context}.client_key_file must have mode 0600")
