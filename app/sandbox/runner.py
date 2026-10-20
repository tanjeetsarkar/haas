"""Sandbox runner for the ``haas.sandbox/1`` protocol.

A tool invocation is executed in a **separate process** so that plugin code never
runs inside the trusted tool worker. The host speaks the protocol over the
process' stdin/stdout: exactly one JSON request line in, exactly one JSON
response line out.

Secrets travel in the request line only -- never in argv, never in the
environment -- so a co-resident process cannot read them out of ``/proc``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.sandbox.protocol import (
    MAX_REQUEST_BYTES,
    ProtocolError,
    SANDBOX_ERROR,
    TIMEOUT,
    SandboxRequest,
    SandboxResponse,
    decode_response,
    error,
)
from app.core.egress import EgressPolicy

#: Environment variables scrubbed from the child process.
_SCRUBBED_ENV = (
    "OPENROUTER_API_KEY",
    "HAAS_ENVELOPE_SIGNING_KEY",
    "HAAS_ENVELOPE_VERIFY_KEY",
    "HAAS_SECRET_ENCRYPTION_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


def _child_env() -> dict[str, str]:
    """A minimal environment: no broker, no vault and no signing material."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    for key in ("HAAS_PROFILE", "HAAS_PLUGINS_DIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


async def run_in_sandbox(
    request: SandboxRequest,
    *,
    timeout_sec: float = 30.0,
) -> SandboxResponse:
    """Execute one tool invocation in a sandboxed subprocess.

    Returns a :class:`SandboxResponse`; failures are reported through the
    protocol's error codes rather than raised.
    """
    payload = request.encode()
    if len(payload) > MAX_REQUEST_BYTES:
        return error(SANDBOX_ERROR, "Sandbox request exceeds the size limit.")

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.sandbox.launcher",
            request.plugin_root,
            request.tool,
            json.dumps(request.args, default=str),
            json.dumps(request.secrets, default=str),
            json.dumps(request.context, default=str),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(),
            cwd=request.plugin_root if Path(request.plugin_root).is_dir() else None,
            limit=MAX_REQUEST_BYTES,
        )
    except Exception as exc:  # pragma: no cover - process spawn failure
        return error(SANDBOX_ERROR, f"Failed to spawn sandbox: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(payload), timeout=timeout_sec
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return error(TIMEOUT, f"Sandbox exceeded its {timeout_sec:g}s deadline.")
    except Exception as exc:  # pragma: no cover - transport failure
        return error(SANDBOX_ERROR, f"Sandbox transport failure: {exc}")

    text = stdout.decode("utf-8", errors="replace")
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return decode_response(line)
        except ProtocolError:
            continue

    detail = stderr.decode("utf-8", errors="replace").strip()[-500:]
    return error(SANDBOX_ERROR, f"Sandbox produced no valid response. {detail}".strip())


async def run_in_sandbox_materialized(
    plugin_id: str,
    plugin_root: Path | str,
    tool_name: str,
    args: dict[str, Any],
    secrets: dict[str, str],
    *,
    run_id: str = "",
    thread_id: str = "",
    tenant_id: str = "",
    traceparent: str = "",
    active_skills: set[str] | None = None,
    skill_allowlist: set[str] | None = None,
    allowed_secrets: set[str] | None = None,
    effect_key: str = "",
    egress_policy: EgressPolicy | None = None,
    timeout_sec: float = 30.0,
) -> SandboxResponse:
    """Build a protocol request for ``tool_name`` and run it in the sandbox."""
    context: dict[str, Any] = {
        "run_id": run_id,
        "thread_id": thread_id,
        "tenant_id": tenant_id,
        "traceparent": traceparent,
        "active_skills": sorted(active_skills or ()),
        "skill_allowlist": (
            sorted(skill_allowlist) if skill_allowlist is not None else None
        ),
        "allowed_secrets": sorted(allowed_secrets or ()),
        "effect_key": effect_key,
    }
    if egress_policy is not None:
        context["egress_policy"] = {
            "allow_hosts": list(egress_policy.allow_hosts),
            "allow_cidrs": list(egress_policy.allow_cidrs),
            "max_bytes_per_run": egress_policy.max_bytes_per_run,
            "version": egress_policy.version,
        }
    request = SandboxRequest(
        op="invoke",
        plugin_id=plugin_id,
        plugin_root=str(plugin_root),
        tool=tool_name,
        args=args,
        secrets=secrets,
        deadline_ms=int(timeout_sec * 1000),
        context=context,
    )
    return await run_in_sandbox(request, timeout_sec=timeout_sec)
