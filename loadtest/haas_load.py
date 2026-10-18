"""Asynchronous multi-tenant load generator for a running HaaS stack.

Each virtual user loops until the deadline: pick a tenant by weight (skewed load),
``POST /v1/runs``, then follow the run either by SSE (``GET /v1/runs/{id}/stream``)
or by polling ``GET /v1/runs/{id}`` until it is terminal, paused for approval, or the
per-run timeout expires. Every HTTP call and every run outcome is recorded into a
results JSON consumed by ``python -m scripts.slo_report``.

Only ``httpx`` (a core dependency) is required::

    python -m loadtest.haas_load --base-url http://127.0.0.1:8000 \\
        --tenant acme:8 --tenant globex:1 --duration 120 --concurrency 20 \\
        --out loadtest-results.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
SETTLED_STATUSES = TERMINAL_STATUSES | {"awaiting_approval"}
TERMINAL_EVENTS = frozenset({"run_completed", "run_error", "run_cancelled", "stream_closed"})
SETTLED_EVENTS = TERMINAL_EVENTS | {"approval_required", "run_paused"}


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    weight: float = 1.0
    token: str | None = None

    @classmethod
    def parse(cls, spec: str) -> Tenant:
        """``id[:weight[:token]]``; ``token`` may be ``env:VAR``."""
        parts = spec.split(":", 2)
        weight = float(parts[1]) if len(parts) > 1 and parts[1] else 1.0
        if weight <= 0:
            raise ValueError(f"tenant weight must be positive: {spec!r}")
        token = parts[2] if len(parts) > 2 and parts[2] else None
        if token and token.startswith("env:"):
            token = os.environ.get(token[4:]) or None
        return cls(parts[0], weight, token)


@dataclass
class LoadConfig:
    base_url: str
    tenants: list[Tenant]
    duration_sec: float = 60.0
    concurrency: int = 10
    max_runs: int | None = None
    stream_ratio: float = 0.5
    poll_interval_sec: float = 1.0
    run_timeout_sec: float = 120.0
    request_timeout_sec: float = 30.0
    prompt: str = "Load test: reply with the single word OK."
    max_steps: int = 1
    think_time_sec: float = 0.0
    seed: int | None = None


@dataclass
class RequestSample:
    op: str
    tenant: str
    started_at: float
    latency_ms: float
    status_code: int | None
    error: str | None = None


@dataclass
class RunSample:
    run_id: str | None
    tenant: str
    mode: str
    created_at: float
    final_status: str
    end_to_end_ms: float | None = None
    first_event_ms: float | None = None
    events: int = 0


@dataclass
class Results:
    requests: list[RequestSample] = field(default_factory=list)
    runs: list[RunSample] = field(default_factory=list)


class LoadGenerator:
    def __init__(
        self,
        config: LoadConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not config.tenants:
            raise ValueError("at least one tenant is required")
        self.config = config
        self.transport = transport
        self.results = Results()
        self._rng = random.Random(config.seed)
        self._started = 0
        self._deadline = 0.0

    def _headers(self, tenant: Tenant) -> dict[str, str]:
        headers = {"X-Tenant-ID": tenant.tenant_id}
        if tenant.token:
            headers["Authorization"] = f"Bearer {tenant.token}"
        return headers

    def _pick_tenant(self) -> Tenant:
        tenants = self.config.tenants
        return self._rng.choices(tenants, weights=[t.weight for t in tenants], k=1)[0]

    def _claim_run_slot(self) -> bool:
        if time.monotonic() >= self._deadline:
            return False
        if self.config.max_runs is not None and self._started >= self.config.max_runs:
            return False
        self._started += 1
        return True

    async def _timed(
        self,
        client: httpx.AsyncClient,
        op: str,
        tenant: Tenant,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response | None:
        started_wall = time.time()
        started = time.perf_counter()
        try:
            response = await client.request(method, url, headers=self._headers(tenant), **kwargs)
        except httpx.HTTPError as exc:
            self.results.requests.append(
                RequestSample(
                    op, tenant.tenant_id, started_wall,
                    (time.perf_counter() - started) * 1000, None, type(exc).__name__,
                )
            )  # fmt: skip
            return None
        self.results.requests.append(
            RequestSample(
                op, tenant.tenant_id, started_wall,
                (time.perf_counter() - started) * 1000, response.status_code,
            )
        )  # fmt: skip
        return response

    async def _create(self, client: httpx.AsyncClient, tenant: Tenant) -> str | None:
        body = {"prompt": self.config.prompt, "max_steps": self.config.max_steps}
        response = await self._timed(
            client, "create_run", tenant, "POST", "/v1/runs", json=body
        )
        if response is None or response.status_code != 202:
            return None
        return str(response.json().get("run_id") or "") or None

    async def _poll(
        self, client: httpx.AsyncClient, tenant: Tenant, run: RunSample, t0: float
    ) -> None:
        while True:
            response = await self._timed(
                client, "get_run", tenant, "GET", f"/v1/runs/{run.run_id}"
            )
            if response is not None and response.status_code == 200:
                status = str(response.json().get("status", ""))
                if status in SETTLED_STATUSES:
                    run.final_status = status
                    run.end_to_end_ms = (time.perf_counter() - t0) * 1000
                    return
            if time.perf_counter() - t0 >= self.config.run_timeout_sec:
                run.final_status = "timeout"
                return
            await asyncio.sleep(self.config.poll_interval_sec)

    async def _stream(
        self, client: httpx.AsyncClient, tenant: Tenant, run: RunSample, t0: float
    ) -> None:
        started_wall = time.time()
        started = time.perf_counter()
        status_code: int | None = None
        error: str | None = None
        event_type = ""
        try:
            async with asyncio.timeout(self.config.run_timeout_sec):
                async with client.stream(
                    "GET",
                    f"/v1/runs/{run.run_id}/stream",
                    headers={**self._headers(tenant), "Accept": "text/event-stream"},
                    timeout=httpx.Timeout(self.config.request_timeout_sec, read=None),
                ) as response:
                    status_code = response.status_code
                    if status_code != 200:
                        run.final_status = f"stream_http_{status_code}"
                        return
                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line == "" and event_type:
                            run.events += 1
                            if run.first_event_ms is None:
                                run.first_event_ms = (time.perf_counter() - t0) * 1000
                            if event_type in SETTLED_EVENTS:
                                run.final_status = _status_for_event(event_type)
                                run.end_to_end_ms = (time.perf_counter() - t0) * 1000
                                return
                            event_type = ""
            run.final_status = "stream_ended"
        except TimeoutError:
            run.final_status = "timeout"
        except httpx.HTTPError as exc:
            error = type(exc).__name__
            run.final_status = "stream_error"
        finally:
            self.results.requests.append(
                RequestSample(
                    "stream_open", tenant.tenant_id, started_wall,
                    (time.perf_counter() - started) * 1000, status_code, error,
                )
            )  # fmt: skip

    async def _one_run(self, client: httpx.AsyncClient) -> None:
        tenant = self._pick_tenant()
        mode = "stream" if self._rng.random() < self.config.stream_ratio else "poll"
        t0 = time.perf_counter()
        run = RunSample(None, tenant.tenant_id, mode, time.time(), "rejected")
        run.run_id = await self._create(client, tenant)
        if run.run_id is not None:
            if mode == "stream":
                await self._stream(client, tenant, run, t0)
            else:
                await self._poll(client, tenant, run, t0)
        self.results.runs.append(run)

    async def _virtual_user(self, client: httpx.AsyncClient) -> None:
        while self._claim_run_slot():
            await self._one_run(client)
            if self.config.think_time_sec:
                await asyncio.sleep(self.config.think_time_sec)

    async def run(self) -> dict[str, Any]:
        config = self.config
        started_wall = time.time()
        self._deadline = time.monotonic() + config.duration_sec
        async with httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.request_timeout_sec,
            transport=self.transport,
            limits=httpx.Limits(max_connections=config.concurrency * 2),
        ) as client:
            await asyncio.gather(*(self._virtual_user(client) for _ in range(config.concurrency)))
        return {
            "schema": "haas.loadtest.results/1",
            "meta": {
                "id": str(uuid.uuid4()),
                "base_url": config.base_url,
                "started_at": started_wall,
                "finished_at": time.time(),
                "concurrency": config.concurrency,
                "duration_sec": config.duration_sec,
                "stream_ratio": config.stream_ratio,
                "tenants": [{"tenant_id": t.tenant_id, "weight": t.weight} for t in config.tenants],
            },
            "requests": [asdict(sample) for sample in self.results.requests],
            "runs": [asdict(sample) for sample in self.results.runs],
        }


def _status_for_event(event_type: str) -> str:
    return {
        "run_completed": "completed",
        "run_error": "failed",
        "run_cancelled": "cancelled",
        "stream_closed": "stream_closed",
    }.get(event_type, "awaiting_approval")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--base-url", default=os.environ.get("HAAS_URL", "http://127.0.0.1:8000"))
    parser.add_argument(
        "--tenant",
        action="append",
        default=[],
        help="id[:weight[:token|env:VAR]]; repeat for skewed multi-tenant load",
    )
    parser.add_argument("--duration", type=float, default=60.0, help="seconds")
    parser.add_argument("--concurrency", type=int, default=10, help="virtual users")
    parser.add_argument("--max-runs", type=int, default=None)
    parser.add_argument("--stream-ratio", type=float, default=0.5)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--run-timeout", type=float, default=120.0)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--prompt", default=LoadConfig.prompt)
    parser.add_argument("--max-steps", type=int, default=1)
    parser.add_argument("--think-time", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--out", type=Path, default=Path("loadtest-results.json"))
    return parser


def config_from_args(args: argparse.Namespace) -> LoadConfig:
    tenants = [Tenant.parse(spec) for spec in args.tenant] or [
        Tenant("default-tenant", 1.0, os.environ.get("HAAS_TOKEN") or None)
    ]
    return LoadConfig(
        base_url=args.base_url,
        tenants=tenants,
        duration_sec=args.duration,
        concurrency=args.concurrency,
        max_runs=args.max_runs,
        stream_ratio=args.stream_ratio,
        poll_interval_sec=args.poll_interval,
        run_timeout_sec=args.run_timeout,
        request_timeout_sec=args.request_timeout,
        prompt=args.prompt,
        max_steps=args.max_steps,
        think_time_sec=args.think_time,
        seed=args.seed,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results = asyncio.run(LoadGenerator(config_from_args(args)).run())
    args.out.write_text(json.dumps(results, indent=2))
    print(
        f"wrote {args.out}: {len(results['runs'])} runs, "
        f"{len(results['requests'])} requests"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
