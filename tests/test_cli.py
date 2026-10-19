from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from terminal_control_mcp import __version__
from terminal_control_mcp.cli import main
from terminal_control_mcp.config import load_config


def initialized(tmp_path: Path) -> Path:
    path = tmp_path / "private config" / "targets.yaml"
    assert main(["--config", str(path), "init"]) == 0
    return path


def test_init_produces_valid_private_inventory(tmp_path, capsys):
    path = initialized(tmp_path)
    config = load_config(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert config.hosts["my-server"].host == "192.0.2.10"
    assert config.allowed_local_read_roots == (path.parent / "uploads",)
    assert main(["--config", str(path), "validate", "--config-only"]) == 0
    assert main(["--config", str(path), "list"]) == 0
    assert "my-server" in capsys.readouterr().out


def test_init_never_overwrites_existing_config(tmp_path):
    path = initialized(tmp_path)
    before = path.read_bytes()
    assert main(["--config", str(path), "init"]) == 1
    assert path.read_bytes() == before


def test_init_rejects_insecure_existing_directory(tmp_path):
    directory = tmp_path / "public"
    directory.mkdir(mode=0o755)
    assert main(["--config", str(directory / "targets.yaml"), "init"]) == 1
    assert not (directory / "targets.yaml").exists()


def test_config_only_does_not_access_credentials_or_ssh(tmp_path):
    path = initialized(tmp_path)
    with patch("terminal_control_mcp.cli.CredentialStore") as credentials:
        with patch("terminal_control_mcp.cli.TerminalControlService") as service:
            assert main(["--config", str(path), "validate", "--config-only"]) == 0
    credentials.assert_not_called()
    service.assert_not_called()


def test_validate_only_requires_selected_credentials(tmp_path):
    path = initialized(tmp_path)
    inventory = yaml.safe_load(path.read_text())
    inventory["hosts"]["unrelated"] = {"host": "192.0.2.20", "credential_ref": "absent"}
    path.write_text(yaml.safe_dump(inventory))
    with patch("terminal_control_mcp.cli.CredentialStore") as credentials:
        credentials.return_value.exists.side_effect = lambda ref: ref == "server-default"
        with patch("terminal_control_mcp.cli.TerminalControlService") as service:
            service.return_value.get_target_status = AsyncMock(
                return_value={"status": "ok", "data": {"online": True}}
            )
            service.return_value.close = AsyncMock()
            assert main(["--config", str(path), "validate", "--target", "my-server"]) == 0
            service.return_value.get_target_status.assert_awaited_once_with("my-server")
            service.return_value.close.assert_awaited_once()


@pytest.mark.parametrize("exit_code,expected", [(0, 0), (7, 1)])
def test_run_returns_failure_for_remote_command_error(tmp_path, exit_code, expected):
    path = initialized(tmp_path)
    with patch("terminal_control_mcp.cli.TerminalControlService") as service:
        service.from_config.return_value.run_command = AsyncMock(
            return_value={"status": "ok", "data": {"exit_code": exit_code}}
        )
        service.from_config.return_value.close = AsyncMock()
        assert main(["--config", str(path), "run", "my-server", "true"]) == expected
        service.from_config.return_value.close.assert_awaited_once()


def test_unknown_selected_target_fails_before_connecting(tmp_path):
    path = initialized(tmp_path)
    assert main(["--config", str(path), "validate", "--target", "missing"]) == 1


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_selected_tunneled_node_requires_jump_host_credential(tmp_path):
    path = initialized(tmp_path)
    inventory = yaml.safe_load(path.read_text())
    inventory["groups"] = {
        "lab": {
            "media": {"host": "192.0.2.30", "credential_ref": "jump-secret"},
            "main": {"host": "192.0.2.40", "via": "media", "host_key_alias": "lab-main"},
        }
    }
    path.write_text(yaml.safe_dump(inventory))
    with patch("terminal_control_mcp.cli.CredentialStore") as credentials:
        credentials.return_value.exists.side_effect = lambda ref: ref == "server-default"
        with patch("terminal_control_mcp.cli.TerminalControlService") as service:
            assert main(["--config", str(path), "validate", "--target", "lab:main"]) == 1
            service.assert_not_called()


def test_run_broadcast_reports_partial_failure(tmp_path):
    path = initialized(tmp_path)
    with patch("terminal_control_mcp.cli.TerminalControlService") as service:
        service.from_config.return_value.run_command = AsyncMock(
            return_value={
                "status": "ok",
                "broadcast": True,
                "results": [
                    {"status": "ok", "data": {"exit_code": 0}},
                    {"status": "error", "error": {"code": "UNREACHABLE"}},
                ],
            }
        )
        service.from_config.return_value.close = AsyncMock()
        assert main(["--config", str(path), "run", "lab:*", "true"]) == 1
