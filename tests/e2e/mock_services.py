"""Deterministic stand-in for OpenRouter + the billing MCP/webhook endpoints (E2E only)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
CALLS: list[dict[str, Any]] = []
ENDPOINT_HITS: list[dict[str, Any]] = []
WEBHOOK_SECRET = "whsec_live_123"


def _tc(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def decide(messages: list[dict[str, Any]]) -> dict[str, Any]:
    last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
    prompt = messages[last_user]["content"]
    tools = [m for m in messages[last_user + 1 :] if m["role"] == "tool"]
    names = [m["name"] for m in tools]
    by_name = {m["name"]: m["content"] for m in tools}

    def say(text: str) -> dict[str, Any]:
        return {"role": "assistant", "content": text}

    def call(*tcs: dict[str, Any]) -> dict[str, Any]:
        return {"role": "assistant", "content": None, "tool_calls": list(tcs)}

    if "LOOP" in prompt:
        return call(_tc("activate_skill", {"skill_names": ["revenue-ops"]}))
    if "BADTOOL" in prompt and not names:
        return call(
            _tc("query_revenue_metrics", {"quarter": "2026-Q3"})
        )  # not unlocked yet -> error
    if "BADTOOL" in prompt and names == ["query_revenue_metrics"]:
        return say("Recovered after tool error.")
    if "SLOW" in prompt and not names:
        return call(_tc("activate_skill", {"skill_names": ["revenue-ops"]}))
    if not names:
        return call(_tc("activate_skill", {"skill_names": ["revenue-ops"]}))
    if names == ["activate_skill"]:
        return call(
            _tc("query_revenue_metrics", {"quarter": "2026-Q3"}),
            _tc("lookup_customer_ledger", {"customer": "acme", "month": "2026-09"}),
        )
    if "prepare_refund_batch" not in by_name:
        return call(
            _tc(
                "prepare_refund_batch",
                {"invoice_ids": ["inv_3002"], "reason": "duplicate charge"},
            )
        )
    refund = by_name["prepare_refund_batch"]
    if refund.startswith("Action Denied"):
        return say(f"Refund was rejected by the operator: {refund}")
    if "notify_billing_system" not in by_name:
        batch_id = json.loads(refund)["batch_id"]
        return call(
            _tc("notify_billing_system", {"batch_id": batch_id, "note": "q3 duplicate"})
        )
    return say(
        f"Refund batch prepared and billing notified. Webhook said: {by_name['notify_billing_system']}"
    )


@app.post("/v1/chat/completions")
async def chat(request: Request) -> JSONResponse:
    body = await request.json()
    CALLS.append(
        {
            "model": body["model"],
            "auth": request.headers.get("authorization"),
            "n_messages": len(body["messages"]),
            "tool_names": [t["function"]["name"] for t in body.get("tools", [])],
            "last_role": body["messages"][-1]["role"],
        }
    )
    message = decide(body["messages"])
    # extra field real providers add; the harness must not echo it back
    message["reasoning"] = "mock reasoning"
    return JSONResponse(
        {
            "id": "gen-mock",
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cost": 0.002,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
        }
    )


@app.post("/mcp")
async def mcp(request: Request) -> Any:
    body = await request.json()
    ENDPOINT_HITS.append(
        {
            "kind": "mcp",
            "protocol": request.headers.get("mcp-protocol-version"),
            "method": request.headers.get("mcp-method"),
            "name": request.headers.get("mcp-name"),
            "accept": request.headers.get("accept"),
            "auth": request.headers.get("authorization"),
            "meta_version": body["params"]["_meta"].get(
                "io.modelcontextprotocol/protocolVersion"
            ),
            "traceparent": request.headers.get("traceparent"),
        }
    )
    result = {
        "jsonrpc": "2.0",
        "id": body["id"],
        "result": {
            "content": [
                {"type": "text", "text": json.dumps({"customer": "acme", "entries": 2})}
            ]
        },
    }

    # Answer as SSE to exercise the client's second allowed response mode.
    def stream():
        yield f"event: message\ndata: {json.dumps(result)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/hooks/refund-batches")
async def webhook(request: Request) -> JSONResponse:
    raw = await request.body()
    payload = json.loads(raw)
    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        json.dumps(payload, sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()
    ok = (
        request.headers.get("x-haas-signature-256") == f"sha256={expected}"
        and request.headers.get("authorization") == f"Bearer {WEBHOOK_SECRET}"
    )
    ENDPOINT_HITS.append({"kind": "webhook", "signature_valid": ok, "payload": payload})
    return JSONResponse({"accepted": ok}, status_code=200 if ok else 401)


@app.post("/run-webhook")
async def run_webhook(request: Request) -> JSONResponse:
    ENDPOINT_HITS.append({"kind": "run-webhook", "payload": await request.json()})
    return JSONResponse({"ok": True})


@app.get("/_calls")
async def calls() -> dict[str, Any]:
    return {"llm": CALLS, "endpoints": ENDPOINT_HITS}
