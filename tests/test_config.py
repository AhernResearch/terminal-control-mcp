import os
from pathlib import Path

import pytest

from terminal_control_mcp.config import credential_refs, load_config
from terminal_control_mcp.errors import TerminalControlError


def write_inventory(tmp_path: Path, body: str) -> Path:
    config_dir = tmp_path / "terminal-control"
    config_dir.mkdir(mode=0o700)
    known_hosts = config_dir / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 AAAA\n", encoding="utf-8")
    path = config_dir / "vehicles.yaml"
    path.write_text(body.replace("KNOWN_HOSTS", str(known_hosts)), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


VALID_CONFIG = """
version: 1
allowed_local_read_roots: [LOCAL_ROOT]
defaults:
  username: nvidia
  port: 22
  credential_ref: nvidia-default
  known_hosts_file: KNOWN_HOSTS
  connect_timeout: 8
  allowed_write_roots: ["~", "/tmp/terminal-control"]
vehicles:
  bit-0000:
    media:
      host: 192.0.2.10
    main:
      host: 192.0.2.20
      via: media
      host_key_alias: bit-0000-main
hosts:
  my-ubuntu-server:
    host: 198.51.100.10
    username: admin1
    credential_ref: server-credential
"""


def test_loads_strict_inventory(tmp_path: Path) -> None:
    config = load_config(
        write_inventory(tmp_path, VALID_CONFIG.replace("LOCAL_ROOT", str(tmp_path)))
    )

    group = config.groups["bit-0000"]
    assert group.media.host == "192.0.2.10"
    assert group.main.via == "media"
    assert group.main.host_key_alias == "bit-0000-main"
    assert config.hosts["my-ubuntu-server"].host == "198.51.100.10"
    assert config.hosts["my-ubuntu-server"].via is None
    assert list(credential_refs(config)) == ["nvidia-default", "server-credential"]
    assert config.allowed_local_read_roots == (tmp_path.resolve(),)


def test_rejects_duplicate_keys(tmp_path: Path) -> None:
    duplicate = VALID_CONFIG.replace("      host: 192.0.2.10", "      host: one\n      host: two")

    with pytest.raises(TerminalControlError, match="Duplicate YAML key"):
        load_config(write_inventory(tmp_path, duplicate.replace("LOCAL_ROOT", str(tmp_path))))


def test_rejects_insecure_config_mode(tmp_path: Path) -> None:
    path = write_inventory(tmp_path, VALID_CONFIG.replace("LOCAL_ROOT", str(tmp_path)))
    os.chmod(path, 0o644)

    with pytest.raises(TerminalControlError, match="permissions must be 0600"):
        load_config(path)


def test_main_requires_media_tunnel_and_alias(tmp_path: Path) -> None:
    invalid = VALID_CONFIG.replace("      via: media\n", "").replace(
        "      host_key_alias: bit-0000-main\n", ""
    )

    with pytest.raises(TerminalControlError, match="requires via=media and host_key_alias"):
        load_config(write_inventory(tmp_path, invalid.replace("LOCAL_ROOT", str(tmp_path))))


def test_rejects_standalone_host_tunnel(tmp_path: Path) -> None:
    invalid = VALID_CONFIG.replace(
        "    credential_ref: server-credential\n",
        "    credential_ref: server-credential\n    via: media\n",
    )

    with pytest.raises(TerminalControlError, match="cannot use a tunnel"):
        load_config(write_inventory(tmp_path, invalid.replace("LOCAL_ROOT", str(tmp_path))))


def test_standalone_host_can_override_password_with_private_key(tmp_path: Path) -> None:
    key_file = tmp_path / "terminal-control-key"
    key_file.write_text("private-key-placeholder\n", encoding="utf-8")
    os.chmod(key_file, 0o600)
    key_config = VALID_CONFIG.replace(
        "    credential_ref: server-credential\n",
        f"    client_key_file: {key_file}\n",
    )

    config = load_config(write_inventory(tmp_path, key_config.replace("LOCAL_ROOT", str(tmp_path))))

    host = config.hosts["my-ubuntu-server"]
    assert host.credential_ref is None
    assert host.client_key_file == key_file
    assert list(credential_refs(config)) == ["nvidia-default"]


@pytest.mark.parametrize("version", ["true", "1.0", '"1"'])
def test_rejects_noninteger_version(tmp_path, version):
    body = VALID_CONFIG.replace("version: 1", f"version: {version}")
    with pytest.raises(TerminalControlError, match="version must be 1"):
        load_config(write_inventory(tmp_path, body.replace("LOCAL_ROOT", str(tmp_path))))


def test_rejects_nonstring_mapping_keys(tmp_path):
    body = VALID_CONFIG + "\n42: invalid\n"
    with pytest.raises(TerminalControlError, match="keys must be strings"):
        load_config(write_inventory(tmp_path, body.replace("LOCAL_ROOT", str(tmp_path))))
