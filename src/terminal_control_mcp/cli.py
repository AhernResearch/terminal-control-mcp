"""Operator CLI for server startup, credentials, and connectivity validation."""

import argparse
import asyncio
import getpass
import json
import sys
from collections.abc import Sequence
from typing import Any

from terminal_control_mcp import __version__
from terminal_control_mcp.config import load_config
from terminal_control_mcp.credentials import CredentialStore
from terminal_control_mcp.errors import TerminalControlError
from terminal_control_mcp.registry import TargetRegistry
from terminal_control_mcp.service import TerminalControlService
from terminal_control_mcp.setup import initialize_config


def _print_error(error: TerminalControlError) -> None:
    print(json.dumps({"status": "error", "error": error.code, "message": error.message}))


def _credentials(args: argparse.Namespace) -> int:
    store = CredentialStore()
    if args.credential_action == "check":
        exists = store.exists(args.reference)
        print(
            json.dumps(
                {
                    "status": "ok" if exists else "missing",
                    "reference": args.reference,
                    "backend": store.backend_name(),
                }
            )
        )
        return 0 if exists else 1

    if args.credential_action == "delete":
        store.delete(args.reference)
        print(json.dumps({"status": "ok", "reference": args.reference, "deleted": True}))
        return 0

    if store.exists(args.reference) and not args.force:
        answer = input(f"Credential '{args.reference}' exists. Overwrite? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print(json.dumps({"status": "cancelled", "reference": args.reference}))
            return 1
    password = getpass.getpass("SSH password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise TerminalControlError("CONFIG_ERROR", "Passwords do not match")
    store.set(args.reference, password)
    print(
        json.dumps(
            {
                "status": "ok",
                "reference": args.reference,
                "backend": store.backend_name(),
            }
        )
    )
    return 0


def _node_online(result: dict[str, Any]) -> bool:
    if result.get("status") != "ok":
        return False
    if result.get("broadcast"):
        return all(
            item.get("status") == "ok" and item.get("data", {}).get("online") is True
            for item in result.get("results", [])
        )
    return result.get("data", {}).get("online") is True


async def _validate_async(
    config_path: str | None, *, config_only: bool = False, target: str | None = None
) -> int:
    config = load_config(config_path)
    registry = TargetRegistry(config)
    selectors = (
        [target]
        if target
        else ([f"{group_id}:*" for group_id in config.groups] + list(config.hosts))
    )
    targets = [node for selector in selectors for node in registry.resolve(selector)]
    if config_only:
        print(
            json.dumps(
                {"status": "ok", "config": str(config.source_path), "target_count": len(targets)},
                indent=2,
            )
        )
        return 0
    store = CredentialStore()
    auth_targets = targets + [
        registry.resolve(f"{node.base_id}:media")[0] for node in targets if node.node == "main"
    ]
    references = sorted(
        {
            node.config.credential_ref
            for node in auth_targets
            if node.config.credential_ref is not None
        }
    )
    missing = [reference for reference in references if not store.exists(reference)]
    if missing:
        raise TerminalControlError(
            "CREDENTIAL_UNAVAILABLE",
            f"Missing credential references: {', '.join(missing)}",
        )

    service = TerminalControlService(config, credentials=store)
    try:
        semaphore = asyncio.Semaphore(4)

        async def get_status(selector: str) -> dict[str, Any]:
            async with semaphore:
                return await service.get_target_status(selector)

        results = await asyncio.gather(*(get_status(item) for item in selectors))
    finally:
        await service.close()

    if target:
        result = results[0]
        print(json.dumps(result, indent=2))
        return 0 if _node_online(result) else 1

    sanitized = []
    all_online = True
    group_results = results[: len(config.groups)]
    for group_id, result in zip(config.groups, group_results):
        online = _node_online(result)
        all_online = all_online and online
        nodes = []
        for item in result.get("results", []):
            node_result = item.get("data", {})
            nodes.append(
                {
                    "node": item.get("node"),
                    "online": item.get("status") == "ok" and node_result.get("online") is True,
                    "hostname": node_result.get("hostname"),
                    "latency_ms": node_result.get("latency_ms"),
                    "error": item.get("error") or node_result.get("error"),
                }
            )
        sanitized.append({"group_id": group_id, "online": online, "nodes": nodes})
    hosts = []
    host_results = results[len(config.groups) :]
    for host_id, result in zip(config.hosts, host_results):
        online = _node_online(result)
        all_online = all_online and online
        host_result = result.get("data", {})
        hosts.append(
            {
                "host_id": host_id,
                "online": online,
                "hostname": host_result.get("hostname"),
                "latency_ms": host_result.get("latency_ms"),
                "error": result.get("error") or host_result.get("error"),
            }
        )
    print(
        json.dumps(
            {
                "status": "ok" if all_online else "error",
                "groups": sanitized,
                "hosts": hosts,
                "credential_backend": store.backend_name(),
            },
            indent=2,
        )
    )
    return 0 if all_online else 1


async def _upload_async(args: argparse.Namespace) -> int:
    service = TerminalControlService.from_config(args.config)
    try:
        result = await service.file_upload(
            args.target,
            args.local_path,
            args.remote_path,
        )
    finally:
        await service.close()
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") == "ok" else 1


async def _run_async(args: argparse.Namespace) -> int:
    service = TerminalControlService.from_config(args.config)
    try:
        result = await service.run_command(args.target, args.shell_command, timeout=args.timeout)
    finally:
        await service.close()
    print(json.dumps(result, indent=2))
    if result.get("status") != "ok":
        return 1
    items = result.get("results", []) if result.get("broadcast") else [result]
    return (
        0
        if all(
            item.get("status") == "ok" and item.get("data", {}).get("exit_code") == 0
            for item in items
        )
        else 1
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(prog="terminal-control-mcp")
    parser.add_argument("--config", help="Path to targets.yaml")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("serve", help="Run the local STDIO MCP server")
    subparsers.add_parser("init", help="Create a private starter inventory")
    validate = subparsers.add_parser("validate", help="Validate inventory and SSH connectivity")
    validate.add_argument("--config-only", action="store_true", help="Check config without SSH")
    validate.add_argument("--target", help="Check one host, group, or group:node selector")
    subparsers.add_parser("list", help="List configured target IDs without connecting")
    run = subparsers.add_parser("run", help="Run one foreground shell command")
    run.add_argument("target")
    run.add_argument("shell_command", help="Quote the remote command as one argument")
    run.add_argument("--timeout", type=int, default=30)

    upload = subparsers.add_parser("upload", help="Upload and verify one binary file")
    upload.add_argument("target")
    upload.add_argument("local_path")
    upload.add_argument("remote_path")

    credentials = subparsers.add_parser("credentials", help="Manage local file credentials")
    credential_actions = credentials.add_subparsers(dest="credential_action", required=True)
    set_parser = credential_actions.add_parser("set", help="Prompt for and store a password")
    set_parser.add_argument("reference")
    set_parser.add_argument("--force", action="store_true", help="Overwrite without confirmation")
    for action in ("check", "delete"):
        action_parser = credential_actions.add_parser(action)
        action_parser.add_argument("reference")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the operator CLI."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            from terminal_control_mcp.server import run_server

            run_server(args.config)
            return 0
        if args.command == "init":
            path = initialize_config(args.config)
            print(json.dumps({"status": "ok", "config": str(path)}))
            return 0
        if args.command == "list":
            registry = TargetRegistry(load_config(args.config))
            print(
                json.dumps(
                    {"groups": registry.list_groups(), "hosts": registry.list_hosts()}, indent=2
                )
            )
            return 0
        if args.command == "validate":
            return asyncio.run(
                _validate_async(args.config, config_only=args.config_only, target=args.target)
            )
        if args.command == "run":
            return asyncio.run(_run_async(args))
        if args.command == "upload":
            return asyncio.run(_upload_async(args))
        return _credentials(args)
    except TerminalControlError as exc:
        _print_error(exc)
        return 1
    except OSError as exc:
        _print_error(TerminalControlError("IO_ERROR", str(exc)))
        return 1
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
