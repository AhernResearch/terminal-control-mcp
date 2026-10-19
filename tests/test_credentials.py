import os
from pathlib import Path

import pytest

from terminal_control_mcp.credentials import CredentialStore
from terminal_control_mcp.errors import TerminalControlError


@pytest.fixture
def credential_store(tmp_path: Path) -> CredentialStore:
    config_dir = tmp_path / "terminal-control"
    config_dir.mkdir(mode=0o700)
    return CredentialStore(config_dir / "credentials")


def test_stores_reads_and_deletes_owner_only_credential(
    credential_store: CredentialStore,
) -> None:
    credential_store.set("vehicle-default", "correct horse battery staple")

    path = credential_store.directory / "vehicle-default"
    assert credential_store.get("vehicle-default") == "correct horse battery staple"
    assert credential_store.exists("vehicle-default") is True
    assert stat_mode(path.parent) == 0o700
    assert stat_mode(path) == 0o600
    assert credential_store.backend_name() == "local-file-0600"

    credential_store.delete("vehicle-default")
    assert credential_store.exists("vehicle-default") is False


def test_rejects_path_traversal_reference(credential_store: CredentialStore) -> None:
    with pytest.raises(TerminalControlError, match="Invalid credential reference"):
        credential_store.set("../outside", "secret")


def test_rejects_insecure_file_permissions(credential_store: CredentialStore) -> None:
    credential_store.set("vehicle-default", "secret")
    path = credential_store.directory / "vehicle-default"
    os.chmod(path, 0o644)

    with pytest.raises(TerminalControlError, match="permissions must be 0600"):
        credential_store.get("vehicle-default")


def test_overwrite_is_atomic_and_preserves_mode(credential_store: CredentialStore) -> None:
    credential_store.set("vehicle-default", "first")
    credential_store.set("vehicle-default", "second")

    path = credential_store.directory / "vehicle-default"
    assert credential_store.get("vehicle-default") == "second"
    assert stat_mode(path) == 0o600
    assert not list(path.parent.glob("*.tmp"))


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
