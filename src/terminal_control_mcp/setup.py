"""Create a portable, owner-only starter inventory without connecting to hosts."""

import os
from pathlib import Path

import yaml

from terminal_control_mcp.config import resolve_config_path
from terminal_control_mcp.errors import TerminalControlError


def initialize_config(path: str | Path | None = None) -> Path:
    """Initialize a new inventory; existing inventories are never overwritten."""
    destination = resolve_config_path(path).absolute()
    if destination.exists() or destination.is_symlink():
        raise TerminalControlError("CONFIG_ERROR", f"Config already exists: {destination}")
    directory = destination.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise TerminalControlError("CONFIG_ERROR", "Config directory must be user-owned")
    if directory.stat().st_mode & 0o777 != 0o700:
        raise TerminalControlError("CONFIG_ERROR", "Config directory permissions must be 0700")
    uploads = directory / "uploads"
    known_hosts = directory / "known_hosts"
    if uploads.is_symlink() or known_hosts.is_symlink():
        raise TerminalControlError("CONFIG_ERROR", "Starter paths cannot be symlinks")
    uploads.mkdir(mode=0o700, exist_ok=True)
    # Keep existing verified host keys when initializing a new inventory file.
    if not known_hosts.exists():
        descriptor = os.open(known_hosts, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    inventory = {
        "version": 1,
        "allowed_local_read_roots": [str(uploads)],
        "defaults": {
            "username": "operator",
            "port": 22,
            "credential_ref": "server-default",
            "known_hosts_file": str(known_hosts),
            "connect_timeout": 10,
            "allowed_write_roots": ["~", "/tmp/terminal-control"],
        },
        "hosts": {"my-server": {"host": "192.0.2.10"}},
    }
    # O_EXCL avoids clobbering a concurrently created inventory.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        yaml.safe_dump(inventory, stream, sort_keys=False)
    return destination
