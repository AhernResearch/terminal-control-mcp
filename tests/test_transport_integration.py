"""SSH/SFTP regressions using an ephemeral loopback server, never production hosts."""

import asyncio
from dataclasses import replace

import asyncssh
import pytest
import pytest_asyncio

from terminal_control_mcp.credentials import CredentialStore
from terminal_control_mcp.errors import TerminalControlError
from terminal_control_mcp.service import TerminalControlService


class PasswordServer(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == "operator" and password == "test-password"


async def run_command(process):
    child = await asyncio.create_subprocess_shell(
        process.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await child.communicate()
        process.stdout.write(stdout.decode())
        process.stderr.write(stderr.decode())
        process.exit(child.returncode)
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


@pytest_asyncio.fixture
async def ssh_service(app_config, tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=PasswordServer,
        server_host_keys=[host_key],
        process_factory=run_command,
        sftp_factory=lambda channel: asyncssh.SFTPServer(channel, chroot=str(remote)),
    )
    known_hosts = tmp_path / "verified_hosts"
    known_hosts.write_bytes(
        f"[127.0.0.1]:{server.get_port()} ".encode() + host_key.export_public_key()
    )
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    credentials = CredentialStore(parent / "credentials")
    credentials.set("loopback", "test-password")
    node = replace(
        app_config.hosts["my-ubuntu-server"],
        host="127.0.0.1",
        port=server.get_port(),
        username="operator",
        credential_ref="loopback",
        known_hosts_file=known_hosts,
    )
    service = TerminalControlService(
        replace(app_config, groups={}, hosts={"loopback": node}), credentials=credentials
    )
    try:
        yield service, remote
    finally:
        await service.close()
        server.close()
        await server.wait_closed()


async def test_real_commands_reuse_connection_and_keep_exit_code(ssh_service):
    service, _ = ssh_service
    first = await service.run_command("loopback", "printf hello")
    assert first["data"]["stdout"] == "hello"
    target = service.registry.resolve("loopback")[0]
    connection = await service.transport.connection(target)
    second = await service.run_command("loopback", "printf failed >&2; exit 7")
    assert second["data"]["exit_code"] == 7
    assert second["data"]["stderr"] == "failed"
    assert await service.transport.connection(target) is connection


async def test_real_atomic_write_edit_and_binary_upload(ssh_service, tmp_path):
    service, remote = ssh_service
    result = await service.file_write("loopback", "~/notes.txt", "hello world")
    assert result["status"] == "ok"
    result = await service.file_edit("loopback", "~/notes.txt", "world", "SSH")
    assert result["status"] == "ok"
    assert (remote / "notes.txt").read_text() == "hello SSH"
    payload = tmp_path / "payload.bin"
    payload.write_bytes(bytes(range(256)) * 8193)
    result = await service.file_upload("loopback", str(payload), "~/payload.bin")
    assert result["status"] == "ok"
    assert result["data"]["bytes_written"] == payload.stat().st_size
    assert (remote / "payload.bin").read_bytes() == payload.read_bytes()
    assert not list(remote.glob("*.tmp"))


async def test_real_connection_rejects_untrusted_host_key(ssh_service):
    service, _ = ssh_service
    target = service.registry.resolve("loopback")[0]
    target.config.known_hosts_file.write_text("")
    with pytest.raises(TerminalControlError, match="host key"):
        await service.transport.connection(target)
