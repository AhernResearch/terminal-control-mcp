from pathlib import Path

import pytest

from terminal_control_mcp.models import AppConfig, GroupConfig, NodeConfig


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("example ssh-ed25519 AAAA\n", encoding="utf-8")
    common = {
        "port": 22,
        "username": "nvidia",
        "credential_ref": "test-credential",
        "client_key_file": None,
        "known_hosts_file": known_hosts,
        "connect_timeout": 5,
        "allowed_write_roots": ("~", "/tmp/terminal-control"),
    }
    media = NodeConfig(host="192.0.2.10", **common)
    main = NodeConfig(
        host="192.0.2.20",
        via="media",
        host_key_alias="group-test-main",
        **common,
    )
    group = GroupConfig("bit-test", media, main)
    host = NodeConfig(
        host="198.51.100.10",
        username="admin1",
        credential_ref="server-credential",
        client_key_file=None,
        port=22,
        known_hosts_file=known_hosts,
        connect_timeout=5,
        allowed_write_roots=("~", "/tmp/terminal-control"),
    )
    return AppConfig(
        version=1,
        groups={"bit-test": group},
        source_path=tmp_path / "x",
        allowed_local_read_roots=(tmp_path,),
        hosts={"my-ubuntu-server": host},
    )
