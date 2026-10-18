"""``haas.sandbox/1`` wire format: one JSON request line in, one JSON response line out.

Request (host -> runner, stdin)::

    {"abi": "haas.sandbox/1", "op": "invoke" | "describe",
     "tool": "<name>", "args": {...}, "secrets": {"KEY": "value"}, "deadline_ms": 25000,
     "plugin": {"id": "<plugin id>", "root": "<path of the extracted plugin>"},
     "context": {"run_id": ..., "thread_id": ..., "tenant_id": ..., "traceparent": ...,
                 "effect_key": ..., "active_skills": [...], "skill_allowlist": [...] | null,
                 "allowed_secrets": [...]}}

Response (runner -> host, stdout)::

    {"abi": "haas.sandbox/1", "ok": true, "result": <json>}
    {"abi": "haas.sandbox/1", "ok": false, "error": {"code": "<code>", "message": "..."}}

Secrets only ever travel in the request line (never argv or the environment). A request
with a different ``abi`` is answered with ``abi_mismatch``; unknown keys are ignored so
minor additions stay compatible within the major version.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

ABI = "haas.sandbox/1"
OPS = ("invoke", "describe")
MAX_REQUEST_BYTES = 8 * 1024 * 1024

BAD_REQUEST = "bad_request"
ABI_MISMATCH = "abi_mismatch"
LOAD_ERROR = "load_error"
NOT_FOUND = "not_found"
INVALID_ARGUMENTS = "invalid_arguments"
TOOL_ERROR = "tool_error"
SECURITY_VIOLATION = "security_violation"
RUNTIME_ERROR = "runtime_error"
TIMEOUT = "timeout"
OUTPUT_LIMIT = "output_limit"
SANDBOX_ERROR = "sandbox_error"  # host side: crash, kill, malformed/no response, policy
ERROR_CODES = frozenset(
    {
        BAD_REQUEST,
        ABI_MISMATCH,
        LOAD_ERROR,
        NOT_FOUND,
        INVALID_ARGUMENTS,
        TOOL_ERROR,
        SECURITY_VIOLATION,
        RUNTIME_ERROR,
        TIMEOUT,
        OUTPUT_LIMIT,
        SANDBOX_ERROR,
    }
)
CONTEXT_KEYS = (
    "run_id",
    "thread_id",
    "tenant_id",
    "traceparent",
    "effect_key",
)


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class SandboxRequest:
    op: str
    plugin_id: str
    plugin_root: str
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    deadline_ms: int = 25_000
    context: dict[str, Any] = field(default_factory=dict)

    def encode(self) -> bytes:
        body = {
            "abi": ABI,
            "op": self.op,
            "tool": self.tool,
            "args": self.args,
            "secrets": self.secrets,
            "deadline_ms": self.deadline_ms,
            "plugin": {"id": self.plugin_id, "root": self.plugin_root},
            "context": self.context,
        }
        return json.dumps(body, separators=(",", ":")).encode() + b"\n"


@dataclass
class SandboxResponse:
    ok: bool
    result: Any = None
    code: str = ""
    message: str = ""

    def encode(self) -> bytes:
        body: dict[str, Any] = {"abi": ABI, "ok": self.ok}
        if self.ok:
            body["result"] = self.result
        else:
            body["error"] = {"code": self.code, "message": self.message}
        return json.dumps(body, separators=(",", ":"), default=str).encode() + b"\n"


def ok(result: Any) -> SandboxResponse:
    return SandboxResponse(True, result=result)


def error(code: str, message: str) -> SandboxResponse:
    return SandboxResponse(False, code=code, message=message)


def _load_line(line: bytes | str) -> dict[str, Any]:
    if len(line) > MAX_REQUEST_BYTES:
        raise ProtocolError(BAD_REQUEST, "message exceeds the size limit")
    try:
        body = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError(BAD_REQUEST, f"malformed JSON line: {exc}") from exc
    if not isinstance(body, dict):
        raise ProtocolError(BAD_REQUEST, "message must be a JSON object")
    if body.get("abi") != ABI:
        raise ProtocolError(
            ABI_MISMATCH, f"unsupported abi {body.get('abi')!r}; expected {ABI!r}"
        )
    return body


def decode_request(line: bytes | str) -> SandboxRequest:
    body = _load_line(line)
    op = body.get("op")
    if op not in OPS:
        raise ProtocolError(BAD_REQUEST, f"unknown op {op!r}")
    plugin = body.get("plugin")
    if not (
        isinstance(plugin, dict)
        and isinstance(plugin.get("id"), str)
        and plugin["id"]
        and isinstance(plugin.get("root"), str)
        and plugin["root"]
    ):
        raise ProtocolError(BAD_REQUEST, "`plugin` must carry string `id` and `root`")
    tool = body.get("tool") or ""
    args = body.get("args") or {}
    secrets = body.get("secrets") or {}
    context = body.get("context") or {}
    deadline = body.get("deadline_ms", 25_000)
    if not isinstance(tool, str) or (op == "invoke" and not tool):
        raise ProtocolError(BAD_REQUEST, "`tool` must be a non-empty string")
    if not isinstance(args, dict):
        raise ProtocolError(BAD_REQUEST, "`args` must be an object")
    if not (
        isinstance(secrets, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in secrets.items())
    ):
        raise ProtocolError(BAD_REQUEST, "`secrets` must map strings to strings")
    if not isinstance(context, dict):
        raise ProtocolError(BAD_REQUEST, "`context` must be an object")
    if isinstance(deadline, bool) or not isinstance(deadline, int) or deadline <= 0:
        raise ProtocolError(BAD_REQUEST, "`deadline_ms` must be a positive integer")
    return SandboxRequest(
        op=op,
        plugin_id=plugin["id"],
        plugin_root=plugin["root"],
        tool=tool,
        args=args,
        secrets=secrets,
        deadline_ms=deadline,
        context=context,
    )


def decode_response(line: bytes | str) -> SandboxResponse:
    body = _load_line(line)
    if body.get("ok") is True:
        return ok(body.get("result"))
    err = body.get("error")
    if body.get("ok") is not False or not isinstance(err, dict):
        raise ProtocolError(BAD_REQUEST, "response needs `ok` and `result`/`error`")
    code = err.get("code")
    if code not in ERROR_CODES:
        raise ProtocolError(BAD_REQUEST, f"unknown error code {code!r}")
    return error(str(code), str(err.get("message", "")))
