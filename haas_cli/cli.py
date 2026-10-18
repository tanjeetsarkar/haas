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
        if (candidate / "docker-compose.yml").is_file() and (
            candidate / "tests/e2e/docker-compose.e2e.yml"
        ).is_file():
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
    smoke = commands.add_parser("smoke", help="Run non-interactive smoke test against local stack")
    smoke.add_argument("--timeout", type=int, default=60, help="Timeout in seconds")
    _ = commands.add_parser("doctor", help="Check local stack health and configuration")
    args = parser.parse_args(argv)

    root: Path | None = None
    try:
        root = _dev_root()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    
    if root is None:
        print("error: could not determine project root", file=sys.stderr)
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
    elif args.action == "seed":
        command.extend(("exec", "-T", "haas-api", "python", "-m", "app.seed_dev"))
        if args.rotate_key:
            command.append("--rotate-key")
    elif args.action == "smoke":
        # Run smoke test against local API
        return _dev_smoke(args.timeout)
    elif args.action == "doctor":
        # Run doctor checks
        return _dev_doctor()
    else:
        print(f"error: unknown dev action: {args.action}", file=sys.stderr)
        return 2

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


def _dev_smoke(timeout: int) -> int:
    """Run non-interactive smoke test against local stack."""
    import httpx
    
    base_url = f"http://127.0.0.1:{_dev_environment()['HAAS_API_PORT']}"
    print(f"Running smoke test against {base_url}...")
    
    try:
        with httpx.Client(timeout=timeout) as client:
            # Check health
            resp = client.get(f"{base_url}/readyz")
            if resp.status_code != 200:
                print(f"FAIL: /readyz returned {resp.status_code}")
                return 1
            print("PASS: /readyz")
            
            # Check live
            resp = client.get(f"{base_url}/livez")
            if resp.status_code != 200:
                print(f"FAIL: /livez returned {resp.status_code}")
                return 1
            print("PASS: /livez")
            
            # Check capabilities
            resp = client.get(f"{base_url}/v1/capabilities")
            if resp.status_code != 200:
                print(f"FAIL: /v1/capabilities returned {resp.status_code}")
                return 1
            caps = resp.json()
            print(f"PASS: /v1/capabilities - version: {caps.get('version', 'unknown')}")
            
            # Check plugins
            resp = client.get(f"{base_url}/v1/plugins")
            if resp.status_code != 200:
                print(f"FAIL: /v1/plugins returned {resp.status_code}")
                return 1
            print(f"PASS: /v1/plugins - {len(resp.json())} plugins")
            
            # Create a test run
            resp = client.post(
                f"{base_url}/v1/runs",
                json={"prompt": "Reply with OK", "max_steps": 1},
                headers={"X-Tenant-ID": "default-tenant"}
            )
            if resp.status_code != 202:
                print(f"FAIL: /v1/runs returned {resp.status_code}: {resp.text}")
                return 1
            run_id = resp.json().get("run_id")
            print(f"PASS: /v1/runs created run {run_id}")
            
            # Wait for completion
            import time
            start = time.time()
            while time.time() - start < timeout:
                resp = client.get(f"{base_url}/v1/runs/{run_id}")
                if resp.status_code != 200:
                    print(f"FAIL: /v1/runs/{run_id} returned {resp.status_code}")
                    return 1
                status = resp.json().get("status")
                if status in ("completed", "failed", "cancelled"):
                    print(f"PASS: Run {run_id} finished with status {status}")
                    return 0 if status == "completed" else 1
                time.sleep(1)
            
            print(f"FAIL: Run {run_id} timed out after {timeout}s")
            return 1
            
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1


def _dev_doctor() -> int:
    """Check local stack health and configuration."""
    import subprocess
    import shutil
    
    print("Running doctor checks...")
    all_ok = True
    
    # Check Docker
    if not shutil.which("docker"):
        print("FAIL: docker not found in PATH")
        all_ok = False
    else:
        print("PASS: docker found")
    
    # Check Docker Compose
    try:
        subprocess.run(["docker", "compose", "version"], capture_output=True, check=True)
        print("PASS: docker compose available")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("FAIL: docker compose not available")
        all_ok = False
    
    # Check project root
    root: Path | None = None
    try:
        root = _dev_root()
        print(f"PASS: Project root found at {root}")
    except ValueError as exc:
        print(f"FAIL: {exc}")
        all_ok = False
    
    if root is None:
        print("FAIL: Cannot proceed without project root")
        return 1
    
    # Check compose files
    compose_files = (
        "docker-compose.yml",
        "docker-compose.dev.yml",
        "tests/e2e/docker-compose.e2e.yml",
    )
    for cf in compose_files:
        if (root / cf).is_file():
            print(f"PASS: {cf} exists")
        else:
            print(f"FAIL: {cf} not found")
            all_ok = False
    
    # Check if stack is running
    project_name = _dev_project_name(root)
    try:
        result = subprocess.run(
            ["docker", "compose", "-p", project_name] + 
            sum([["-f", cf] for cf in compose_files], []) +
            ["--project-directory", str(root), "ps", "--format", "json"],
            capture_output=True, text=True, check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            print("PASS: Stack is running")
            # Check individual services
            import json
            for line in result.stdout.strip().split('\n'):
                if line:
                    svc = json.loads(line)
                    print(f"  - {svc.get('Service', 'unknown')}: {svc.get('State', 'unknown')}")
        else:
            print("INFO: Stack is not running (run `haas dev up`)")
    except Exception as exc:
        print(f"WARN: Could not check stack status: {exc}")
    
    # Check ports
    ports = {
        "API": _dev_environment()['HAAS_API_PORT'],
        "PostgreSQL": _dev_environment()['HAAS_POSTGRES_PORT'],
        "Redis": _dev_environment()['HAAS_REDIS_PORT'],
        "Flower": _dev_environment()['HAAS_FLOWER_PORT'],
        "Mock": _dev_environment()['HAAS_MOCK_PORT'],
    }
    for name, port in ports.items():
        try:
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex(('127.0.0.1', int(port)))
                if result == 0:
                    print(f"PASS: {name} port {port} is open")
                else:
                    print(f"INFO: {name} port {port} is not open")
        except Exception:
            print(f"WARN: Could not check {name} port {port}")
    
    if all_ok:
        print("\nAll checks passed!")
        return 0
    else:
        print("\nSome checks failed!")
        return 1


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


def _runs_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="haas runs")
    parser.add_argument("--url")
    parser.add_argument("--tenant")
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="action", required=True)

    list_cmd = commands.add_parser("list")
    list_cmd.add_argument("--limit", type=int, default=20)
    list_cmd.add_argument("--cursor")

    get_cmd = commands.add_parser("get")
    get_cmd.add_argument("run_id")

    create_cmd = commands.add_parser("create")
    create_cmd.add_argument("prompt")
    create_cmd.add_argument("--thread-id")
    create_cmd.add_argument("--agent-revision-id")
    create_cmd.add_argument("--preload-skills", nargs="*")
    create_cmd.add_argument("--primary-model")
    create_cmd.add_argument("--planner-model")
    create_cmd.add_argument("--max-steps", type=int)
    create_cmd.add_argument("--max-cost-usd", type=float)
    create_cmd.add_argument("--secrets", nargs="*", help="KEY=VALUE pairs")
    create_cmd.add_argument("--expected-history-version", type=int)

    cancel_cmd = commands.add_parser("cancel")
    cancel_cmd.add_argument("run_id")

    resume_cmd = commands.add_parser("resume")
    resume_cmd.add_argument("run_id")
    resume_cmd.add_argument("--approved", action="store_true", default=True)
    resume_cmd.add_argument("--rejected", dest="approved", action="store_false")
    resume_cmd.add_argument("--rejection-reason")
    resume_cmd.add_argument("--secrets", nargs="*", help="KEY=VALUE pairs")
    resume_cmd.add_argument("--batch-id")
    resume_cmd.add_argument("--batch-digest")

    events_cmd = commands.add_parser("events")
    events_cmd.add_argument("run_id")
    events_cmd.add_argument("--after", type=int, default=0)
    events_cmd.add_argument("--limit", type=int, default=100)

    steps_cmd = commands.add_parser("steps")
    steps_cmd.add_argument("run_id")

    usage_cmd = commands.add_parser("usage")
    usage_cmd.add_argument("run_id")

    stream_cmd = commands.add_parser("stream")
    stream_cmd.add_argument("run_id")
    stream_cmd.add_argument("--last-event-id", default="0")
    stream_cmd.add_argument("--output-deltas", action="store_true")

    return parser


async def _run_runs(args: argparse.Namespace, client: HaasClient) -> object:
    if args.action == "list":
        return await client.list_runs(limit=args.limit, cursor=args.cursor)
    if args.action == "get":
        return await client.get_run(args.run_id)
    if args.action == "create":
        secrets = {}
        if args.secrets:
            for pair in args.secrets:
                k, v = pair.split("=", 1)
                secrets[k] = v
        return await client.create_run(
            prompt=args.prompt,
            thread_id=args.thread_id,
            agent_revision_id=args.agent_revision_id,
            preload_skills=args.preload_skills,
            primary_model=args.primary_model,
            planner_model=args.planner_model,
            max_steps=args.max_steps,
            max_cost_usd=args.max_cost_usd,
            secrets=secrets or None,
            expected_history_version=args.expected_history_version,
        )
    if args.action == "cancel":
        return await client.cancel_run(args.run_id)
    if args.action == "resume":
        secrets = {}
        if args.secrets:
            for pair in args.secrets:
                k, v = pair.split("=", 1)
                secrets[k] = v
        return await client.resume(
            run_id=args.run_id,
            approved=args.approved,
            rejection_reason=args.rejection_reason,
            secrets=secrets or None,
            batch_id=args.batch_id,
            batch_digest=args.batch_digest,
        )
    if args.action == "events":
        return await client.run_events(args.run_id, after=args.after, limit=args.limit)
    if args.action == "steps":
        return await client.run_steps(args.run_id)
    if args.action == "usage":
        return await client.run_usage(args.run_id)
    if args.action == "stream":
        async for event in client.stream_events(args.run_id, last_event_id=args.last_event_id, output_deltas=args.output_deltas):
            print(json.dumps({"event": event.event, "data": event.data, "id": event.id}))
        return None


def _agents_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="haas agents")
    parser.add_argument("--url")
    parser.add_argument("--tenant")
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="action", required=True)

    list_cmd = commands.add_parser("list")
    list_cmd.add_argument("--cursor")

    get_cmd = commands.add_parser("get")
    get_cmd.add_argument("revision_id")

    draft_cmd = commands.add_parser("draft")
    draft_cmd.add_argument("name")
    draft_cmd.add_argument("--primary-model", required=True)
    draft_cmd.add_argument("--planner-model", required=True)
    draft_cmd.add_argument("--max-steps", type=int)
    draft_cmd.add_argument("--max-cost-usd", type=float)
    draft_cmd.add_argument("--skills", nargs="*")
    draft_cmd.add_argument("--preload-skills", nargs="*")

    validate_cmd = commands.add_parser("validate")
    validate_cmd.add_argument("revision_id")

    publish_cmd = commands.add_parser("publish")
    publish_cmd.add_argument("revision_id")

    return parser


async def _run_agents(args: argparse.Namespace, client: HaasClient) -> object:
    if args.action == "list":
        return await client.list_agent_revisions(cursor=args.cursor)
    if args.action == "get":
        return await client.agent_revision(args.revision_id)
    if args.action == "draft":
        config = {
            "primary_model": args.primary_model,
            "planner_model": args.planner_model,
        }
        if args.max_steps:
            config["max_steps"] = args.max_steps
        if args.max_cost_usd:
            config["max_cost_usd"] = args.max_cost_usd
        if args.skills:
            config["skills"] = args.skills
        if args.preload_skills:
            config["preload_skills"] = args.preload_skills
        return await client.draft_agent(args.name, config)
    if args.action == "validate":
        return await client.transition_agent(args.revision_id, "validate")
    if args.action == "publish":
        return await client.transition_agent(args.revision_id, "publish")


def _approvals_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="haas approvals")
    parser.add_argument("--url")
    parser.add_argument("--tenant")
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="action", required=True)

    list_cmd = commands.add_parser("list")
    list_cmd.add_argument("--status", choices=["pending", "approved", "rejected", "expired"], default="pending")
    list_cmd.add_argument("--limit", type=int, default=50)

    get_cmd = commands.add_parser("get")
    get_cmd.add_argument("batch_id")

    decide_cmd = commands.add_parser("decide")
    decide_cmd.add_argument("batch_id")
    decide_cmd.add_argument("--approve", action="store_true", default=True)
    decide_cmd.add_argument("--reject", dest="approve", action="store_false")
    decide_cmd.add_argument("--batch-digest", required=True)
    decide_cmd.add_argument("--reason")
    decide_cmd.add_argument("--secrets", nargs="*", help="KEY=VALUE pairs")

    return parser


async def _run_approvals(args: argparse.Namespace, client: HaasClient) -> object:
    if args.action == "list":
        return await client.list_approvals(status=args.status, limit=args.limit)
    if args.action == "get":
        return await client.get_approval(args.batch_id)
    if args.action == "decide":
        secrets = {}
        if args.secrets:
            for pair in args.secrets:
                k, v = pair.split("=", 1)
                secrets[k] = v
        return await client.decide_approval(
            batch_id=args.batch_id,
            approve=args.approve,
            batch_digest=args.batch_digest,
            reason=args.reason,
            secrets=secrets or None,
        )


def _webhooks_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="haas webhooks")
    parser.add_argument("--url")
    parser.add_argument("--tenant")
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="action", required=True)

    # Endpoints
    endpoints = commands.add_parser("endpoints")
    endpoints_cmds = endpoints.add_subparsers(dest="endpoint_action", required=True)
    endpoints_cmds.add_parser("list")
    create_ep = endpoints_cmds.add_parser("create")
    create_ep.add_argument("url")
    create_ep.add_argument("--events", nargs="*", default=["run.terminal"])
    create_ep.add_argument("--enabled", action="store_true", default=True)
    create_ep.add_argument("--description")
    get_ep = endpoints_cmds.add_parser("get")
    get_ep.add_argument("endpoint_id")
    update_ep = endpoints_cmds.add_parser("update")
    update_ep.add_argument("endpoint_id")
    update_ep.add_argument("--url")
    update_ep.add_argument("--events", nargs="*")
    update_ep.add_argument("--enabled", action="store_true")
    update_ep.add_argument("--disabled", dest="enabled", action="store_false")
    update_ep.add_argument("--description")
    delete_ep = endpoints_cmds.add_parser("delete")
    delete_ep.add_argument("endpoint_id")

    # Signing keys
    keys = commands.add_parser("keys")
    keys_cmds = keys.add_subparsers(dest="key_action", required=True)
    keys_cmds.add_parser("list")
    create_key = keys_cmds.add_parser("create")
    create_key.add_argument("key_id")
    create_key.add_argument("secret_envelope")
    rotate_key = keys_cmds.add_parser("rotate")
    rotate_key.add_argument("key_id")
    rotate_key.add_argument("new_key_id")
    rotate_key.add_argument("new_secret_envelope")
    rotate_key.add_argument("--retire-at")
    revoke_key = keys_cmds.add_parser("revoke")
    revoke_key.add_argument("key_id")

    # Deliveries
    deliveries = commands.add_parser("deliveries")
    deliveries_cmds = deliveries.add_subparsers(dest="delivery_action", required=True)
    deliveries_cmds.add_parser("list")
    get_del = deliveries_cmds.add_parser("get")
    get_del.add_argument("delivery_id")
    replay_del = deliveries_cmds.add_parser("replay")
    replay_del.add_argument("delivery_ids", nargs="+")

    return parser


async def _run_webhooks(args: argparse.Namespace, client: HaasClient) -> object:
    if args.action == "endpoints":
        if args.endpoint_action == "list":
            return await client.list_webhook_endpoints()
        if args.endpoint_action == "create":
            return await client.create_webhook_endpoint(
                url=args.url,
                events=args.events,
                enabled=args.enabled,
                description=args.description,
            )
        if args.endpoint_action == "get":
            return await client.get_webhook_endpoint(args.endpoint_id)
        if args.endpoint_action == "update":
            return await client.update_webhook_endpoint(
                endpoint_id=args.endpoint_id,
                url=args.url,
                events=args.events,
                enabled=args.enabled,
                description=args.description,
            )
        if args.endpoint_action == "delete":
            return await client.delete_webhook_endpoint(args.endpoint_id)

    if args.action == "keys":
        if args.key_action == "list":
            return await client.list_webhook_signing_keys()
        if args.key_action == "create":
            return await client.create_webhook_signing_key(
                key_id=args.key_id,
                secret_envelope=args.secret_envelope,
            )
        if args.key_action == "rotate":
            return await client.rotate_webhook_signing_key(
                key_id=args.key_id,
                new_key_id=args.new_key_id,
                new_secret_envelope=args.new_secret_envelope,
                retire_at=args.retire_at,
            )
        if args.key_action == "revoke":
            return await client.revoke_webhook_signing_key(args.key_id)

    if args.action == "deliveries":
        if args.delivery_action == "list":
            return await client.list_webhook_deliveries()
        if args.delivery_action == "get":
            return await client.get_webhook_delivery(args.delivery_id)
        if args.delivery_action == "replay":
            return await client.replay_webhook_deliveries(delivery_ids=args.delivery_ids)


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
    if args and args[0] == "runs":
        parser = _runs_parser()
        parsed = parser.parse_args(args[1:])
        settings = settings_from_args(parsed)
        if not settings.credential:
            print("error: set HAAS_API_KEY or HAAS_BEARER_TOKEN before using runs commands.", file=sys.stderr)
            return 2
        client = HaasClient(settings.base_url, settings.tenant, credential=settings.credential)
        try:
            result = asyncio.run(_run_runs(parsed, client))
        except (HaasAPIError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        finally:
            asyncio.run(client.aclose())
        if result is not None:
            print(json.dumps(result, indent=2, default=str))
        return 0
    if args and args[0] == "agents":
        parser = _agents_parser()
        parsed = parser.parse_args(args[1:])
        settings = settings_from_args(parsed)
        if not settings.credential:
            print("error: set HAAS_API_KEY or HAAS_BEARER_TOKEN before using agents commands.", file=sys.stderr)
            return 2
        client = HaasClient(settings.base_url, settings.tenant, credential=settings.credential)
        try:
            result = asyncio.run(_run_agents(parsed, client))
        except (HaasAPIError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        finally:
            asyncio.run(client.aclose())
        if result is not None:
            print(json.dumps(result, indent=2, default=str))
        return 0
    if args and args[0] == "approvals":
        parser = _approvals_parser()
        parsed = parser.parse_args(args[1:])
        settings = settings_from_args(parsed)
        if not settings.credential:
            print("error: set HAAS_API_KEY or HAAS_BEARER_TOKEN before using approvals commands.", file=sys.stderr)
            return 2
        client = HaasClient(settings.base_url, settings.tenant, credential=settings.credential)
        try:
            result = asyncio.run(_run_approvals(parsed, client))
        except (HaasAPIError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        finally:
            asyncio.run(client.aclose())
        if result is not None:
            print(json.dumps(result, indent=2, default=str))
        return 0
    if args and args[0] == "webhooks":
        parser = _webhooks_parser()
        parsed = parser.parse_args(args[1:])
        settings = settings_from_args(parsed)
        if not settings.credential:
            print("error: set HAAS_API_KEY or HAAS_BEARER_TOKEN before using webhooks commands.", file=sys.stderr)
            return 2
        client = HaasClient(settings.base_url, settings.tenant, credential=settings.credential)
        try:
            result = asyncio.run(_run_webhooks(parsed, client))
        except (HaasAPIError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        finally:
            asyncio.run(client.aclose())
        if result is not None:
            print(json.dumps(result, indent=2, default=str))
        return 0
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
            "`haas dev`, `haas runs`, `haas agents`, `haas approvals`, `haas webhooks`."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("chat", "profile", "admin", "dev", "runs", "agents", "approvals", "webhooks"),
        help=argparse.SUPPRESS,
    )
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
