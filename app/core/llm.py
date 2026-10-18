from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
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
    cost_reported: bool = True
    completion_tokens: int = 0


DeltaSink = Callable[[str], Awaitable[None]]


class _StreamAccumulator:
    """Rebuilds one assistant message from OpenAI-compatible `chat.completion.chunk`s."""

    def __init__(self) -> None:
        self.content: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, Any] = {}
        self.saw_choice = False

    def feed(self, chunk: dict[str, Any]) -> str:
        if chunk.get("error"):
            raise httpx.HTTPError(f"OpenRouter stream error: {chunk['error']}")
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        text = ""
        for choice in chunk.get("choices") or []:
            self.saw_choice = True
            if choice.get("finish_reason") == "error":
                raise httpx.HTTPError("OpenRouter stream finished with an error.")
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                self.content.append(piece)
                text += piece
            for tc in delta.get("tool_calls") or []:
                slot = self.tool_calls.setdefault(
                    int(tc.get("index", 0)),
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                function = tc.get("function") or {}
                if function.get("name"):
                    slot["function"]["name"] += function["name"]
                if function.get("arguments"):
                    slot["function"]["arguments"] += function["arguments"]
        return text

    def message(self) -> dict[str, Any]:
        if not self.saw_choice:
            raise httpx.HTTPError("OpenRouter stream returned no choices.")
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(self.content) or None,
        }
        if self.tool_calls:
            msg["tool_calls"] = [self.tool_calls[i] for i in sorted(self.tool_calls)]
        return msg


def _usage_fields(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "cost_usd": float(usage.get("cost") or 0.0),
        "cost_reported": usage.get("cost") is not None,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "cached_tokens": int(
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        ),
    }


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
        on_delta: DeltaSink | None = None,
    ) -> CompletionResult:
        """One model call. With `on_delta`, streams and forwards content text as it arrives.

        Deltas are advisory previews: the returned message is the only authoritative output.
        """
        # Usage accounting (incl. `usage.cost`) is always returned by OpenRouter;
        # the legacy `usage: {include: true}` request flag is deprecated.
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://haas.internal",
            "X-Title": "FastAPI-Celery-HaaS",
            "traceparent": traceparent,
        }
        if on_delta is not None:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
            return await self._stream_completion(
                client, model=model, payload=payload, headers=headers, on_delta=on_delta
            )

        resp = await client.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
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
        return CompletionResult(
            assistant_message=sanitize_assistant_message(choice),
            model_used=model,
            **_usage_fields(data.get("usage") or {}),
        )

    async def _stream_completion(
        self,
        client: httpx.AsyncClient,
        *,
        model: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_delta: DeltaSink,
    ) -> CompletionResult:
        acc = _StreamAccumulator()
        async with client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            headers={**headers, "Accept": "text/event-stream"},
            json=payload,
            timeout=httpx.Timeout(60.0, read=60.0),
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                resp.raise_for_status()
            if "text/event-stream" not in resp.headers.get("content-type", ""):
                # Providers may ignore `stream`; fall back to a single JSON body.
                await resp.aread()
                data = resp.json()
                if "choices" not in data:
                    raise httpx.HTTPError(
                        f"OpenRouter returned no choices: {data.get('error', data)}"
                    )
                msg = data["choices"][0]["message"]
                if msg.get("content"):
                    await on_delta(str(msg["content"]))
                return CompletionResult(
                    assistant_message=sanitize_assistant_message(msg),
                    model_used=model,
                    **_usage_fields(data.get("usage") or {}),
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue  # comments (": OPENROUTER PROCESSING"), ids, blank lines
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                text = acc.feed(chunk)
                if text:
                    await on_delta(text)
        return CompletionResult(
            assistant_message=sanitize_assistant_message(acc.message()),
            model_used=model,
            **_usage_fields(acc.usage),
        )
