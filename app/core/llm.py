from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx


def repair_tool_json(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    parsed = json.loads(cleaned) if cleaned else {}
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("Tool arguments must be a JSON object", cleaned, 0)
    return parsed


def sanitize_assistant_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Keep only fields valid when the message is echoed back (drops `reasoning`, `refusal`, ...)."""
    clean: dict[str, Any] = {"role": "assistant", "content": msg.get("content")}
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        clean["tool_calls"] = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["function"]["name"],
                    "arguments": tc["function"].get("arguments") or "{}",
                },
            }
            for tc in tool_calls
        ]
    return clean


@dataclass
class CompletionResult:
    assistant_message: dict[str, Any]
    model_used: str
    cost_usd: float
    prompt_tokens: int
    cached_tokens: int


class OpenRouterGateway:
    def __init__(self, api_key: str, base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def select_model_for_step(
        step: int,
        last_step_had_error: bool,
        primary_model: str,
        planner_model: str,
    ) -> str:
        """
        Architect/Editor Dual-Model Routing:
        Uses the reasoning/planner model on Step 1 and immediately following any tool error;
        uses the fast/cost-effective execution model for standard tool-calling turns.
        """
        if step == 1 or last_step_had_error:
            return planner_model
        return primary_model

    async def chat_completion(
        self,
        client: httpx.AsyncClient,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        traceparent: str,
    ) -> CompletionResult:
        # Usage accounting (incl. `usage.cost`) is always returned by OpenRouter;
        # the legacy `usage: {include: true}` request flag is deprecated.
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools

        resp = await client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://haas.internal",
                "X-Title": "FastAPI-Celery-HaaS",
                "traceparent": traceparent,
            },
            json=payload,
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if "choices" not in data:
            # OpenRouter can return HTTP 200 with an `error` object for upstream failures.
            raise httpx.HTTPError(
                f"OpenRouter returned no choices: {data.get('error', data)}"
            )

        choice = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        return CompletionResult(
            assistant_message=sanitize_assistant_message(choice),
            model_used=model,
            cost_usd=float(usage.get("cost") or 0.0),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            cached_tokens=int(
                (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            ),
        )
