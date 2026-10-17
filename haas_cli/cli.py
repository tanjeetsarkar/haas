from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from haas_cli.client import HaasAPIError, HaasClient
from haas_cli.chat import settings_from_args
from haas_cli.profiles import EnvironmentProfile, ProfileStore


def _dev_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HAAS_PROFILE": "dev",
            "HAAS_API_BIND_ADDRESS": "127.0.0.1",
            "HAAS_API_PORT": environment.get("HAAS_DEV_API_PORT", "18000"),
            "HAAS_POSTGRES_PORT": environment.get("HAAS_DEV_POSTGRES_PORT", "15432"),
            "HAAS_REDIS_PORT": environment.get("HAAS_DEV_REDIS_PORT", "16379"),
            "HAAS_FLOWER_PORT": environment.get("HAAS_DEV_FLOWER_PORT", "15555"),
            "HAAS_MOCK_PORT": environment.get("HAAS_DEV_MOCK_PORT", "19000"),
            "SSRF_ALLOWED_HOSTS": "mock",
            "BILLING_MCP_URL": "http://mock:9000/mcp",
            "BILLING_WEBHOOK_URL": "http://mock:9000/hooks/refund-batches",
            "OPENROUTER_BASE_URL": "http://mock:9000/v1",
            "OPENROUTER_API_KEY": "sk-or-v1-local-dev-mock",
        }
    )
    return environment


def _dev_project_name(root: Path) -> str:
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:10]
    return f"haas-dev-{digest}"


def _dev_root() -> Path:
    cwd = Path.cwd().resolve()
    candidates = (cwd, *cwd.parents, Path(__file__).resolve().parents[1])
    for candidate in candidates:
        if (
            (candidate / "docker-compose.yml").is_file()
            and (candidate / "tests/e2e/docker-compose.e2e.yml").is_file()
        ):
            return candidate
    raise ValueError("Run `haas dev` from a HaaS source checkout.")


def _dev_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="haas dev")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("up", help="Build and start the isolated local stack")
    commands.add_parser("down", help="Stop the local stack and preserve its data")
    commands.add_parser("status", help="Show local stack service status")
    seed = commands.add_parser("seed", help="Create local tenant policy and test key")
    seed.add_argument(
        "--rotate-key",
        action="store_true",
        help="Revoke the previous local test key and issue a replacement.",
    )
    args = parser.parse_args(argv)

    try:
        root = _dev_root()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    compose_files = (
        "docker-compose.yml",
        "docker-compose.dev.yml",
        "tests/e2e/docker-compose.e2e.yml",
    )
    command = ["docker", "compose", "-p", _dev_project_name(root)]
    for compose_file in compose_files:
        command.extend(("-f", compose_file))
    command.append("--project-directory")
    command.append(str(root))
    if args.action == "up":
        command.extend(("up", "--build", "-d"))
    elif args.action == "down":
        command.extend(("down", "--remove-orphans"))
    elif args.action == "status":
        command.append("ps")
    else:
        command.extend(("exec", "-T", "haas-api", "python", "-m", "app.seed_dev"))
        if args.rotate_key:
            command.append("--rotate-key")

    try:
        result = subprocess.run(command, cwd=root, env=_dev_environment(), check=False)
    except OSError as exc:
        print(f"error: could not run Docker Compose: {exc}", file=sys.stderr)
        return 2
    if result.returncode:
        if args.action == "seed":
            print(
                "error: local seeding failed; start the stack with `haas dev up` "
                "and inspect the API container logs.",
                file=sys.stderr,
            )
        return result.returncode
    if args.action == "up":
        print(
            "Local HaaS is available at "
            f"http://127.0.0.1:{_dev_environment()['HAAS_API_PORT']}."
        )
        print("Run `haas dev seed` to create a local tenant and one-time test key.")
    return 0


def _admin_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="haas admin")
    parser.add_argument("--url")
    parser.add_argument("--tenant")
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="resource", required=True)

    memberships = commands.add_parser("memberships")
    membership_actions = memberships.add_subparsers(dest="action", required=True)
    membership_actions.add_parser("list")
    grant = membership_actions.add_parser("grant")
    grant.add_argument("--subject", required=True)
    grant.add_argument("--kind", choices=("human", "service"), default="human")
    grant.add_argument(
        "--role",
        choices=("tenant_admin", "submitter", "approver", "auditor"),
        required=True,
    )
    revoke = membership_actions.add_parser("revoke")
    revoke.add_argument("--principal-id", required=True)
    revoke.add_argument(
        "--role",
        choices=("tenant_admin", "submitter", "approver", "auditor"),
        required=True,
    )

    api_keys = commands.add_parser("api-keys")
    key_actions = api_keys.add_subparsers(dest="action", required=True)
    key_actions.add_parser("list")
    create_key = key_actions.add_parser("create")
    create_key.add_argument("--principal-id", required=True)
    create_key.add_argument("--name", required=True)
    create_key.add_argument(
        "--expires-in-days", type=int, default=90, choices=range(1, 366)
    )
    revoke_key = key_actions.add_parser("revoke")
    revoke_key.add_argument("--key-id", required=True)

    policy = commands.add_parser("policy")
    policy_actions = policy.add_subparsers(dest="action", required=True)
    policy_actions.add_parser("get")
    activate = policy_actions.add_parser("activate")
    activate.add_argument("--file", required=True, type=Path)

    audit = commands.add_parser("audit")
    audit.add_argument("--limit", type=int, default=50, choices=range(1, 101))
    audit.add_argument("--cursor")
    return parser


async def _run_admin(args: argparse.Namespace, client: HaasClient) -> object:
    if args.resource == "memberships":
        if args.action == "list":
            return await client.list_memberships()
        if args.action == "grant":
            return await client.grant_membership(
                subject=args.subject, kind=args.kind, role=args.role
            )
        return await client.revoke_membership(args.principal_id, args.role)
    if args.resource == "api-keys":
        if args.action == "list":
            return await client.list_api_keys()
        if args.action == "create":
            return await client.create_api_key(
                principal_id=args.principal_id,
                name=args.name,
                expires_in_days=args.expires_in_days,
            )
        return await client.revoke_api_key(args.key_id)
    if args.resource == "policy":
        if args.action == "get":
            return await client.get_tenant_policy()
        try:
            rules = json.loads(args.file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read policy JSON from {args.file}.") from exc
        if not isinstance(rules, dict):
            raise ValueError("Policy JSON must be an object.")
        return await client.activate_tenant_policy(rules)
    return await client.list_audit_events(limit=args.limit, cursor=args.cursor)


def _admin_main(argv: list[str]) -> int:
    parser = _admin_parser()
    args = parser.parse_args(argv)
    settings = settings_from_args(args)
    if not settings.credential:
        print(
            "error: set HAAS_API_KEY or HAAS_BEARER_TOKEN before using admin commands.",
            file=sys.stderr,
        )
        return 2
    client = HaasClient(
        settings.base_url, settings.tenant, credential=settings.credential
    )

    async def execute() -> object:
        try:
            return await _run_admin(args, client)
        finally:
            await client.aclose()

    try:
        result = asyncio.run(execute())
    except (HaasAPIError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


def _profile_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="haas profile")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="List saved endpoint profiles")

    add = commands.add_parser("add", help="Save endpoint metadata (never credentials)")
    add.add_argument("name")
    add.add_argument("--url", required=True)
    add.add_argument("--tenant", required=True)
    add.add_argument(
        "--environment",
        choices=("unknown", "dev", "staging", "prod"),
        default="unknown",
    )
    add.add_argument("--activate", action="store_true")

    use = commands.add_parser("use", help="Select the active endpoint profile")
    use.add_argument("name")
    remove = commands.add_parser("remove", help="Remove a saved profile")
    remove.add_argument("name")
    args = parser.parse_args(argv)
    store = ProfileStore()

    if args.action == "list":
        active = store.active()
        for profile in store.profiles():
            marker = "*" if active and profile.name == active.name else " "
            print(
                f"{marker} {profile.name}\t{profile.environment}\t"
                f"{profile.base_url}\t{profile.tenant}"
            )
        return 0
    if args.action == "add":
        profile = EnvironmentProfile(args.name, args.url, args.tenant, args.environment)
        store.save(profile, activate=args.activate)
        print(f"Saved profile {profile.name!r}. No credentials were stored.")
        return 0
    if args.action == "use":
        profile = store.activate(args.name)
        print(f"Active profile: {profile.name}")
        return 0
    store.remove(args.name)
    print(f"Removed profile {args.name!r}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "chat":
        from haas_cli.chat import main as chat_main

        return chat_main(args[1:])
    if args and args[0] == "profile":
        try:
            return _profile_main(args[1:])
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args and args[0] == "admin":
        try:
            return _admin_main(args[1:])
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args and args[0] == "dev":
        return _dev_main(args[1:])

    parser = argparse.ArgumentParser(
        prog="haas",
        description=(
            "HaaS developer CLI. Use `haas chat`, `haas profile`, `haas admin`, "
            "or `haas dev`."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("chat", "profile", "admin", "dev"),
        help=argparse.SUPPRESS,
    )
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
