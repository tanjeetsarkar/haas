# HaaS load test

`loadtest/haas_load.py` is an asyncio + `httpx` load generator (no extra dependencies).
`scripts/slo_report.py` turns its results JSON into an SLO pass/fail report using the
targets in [`docs/design/operations-migration.md` §2](../docs/design/operations-migration.md).

Each virtual user loops until `--duration` (or `--max-runs`) is exhausted:

1. picks a tenant by weight (`--tenant id:weight[:token|env:VAR]`, repeatable → skewed load);
2. `POST /v1/runs` (op `create_run`);
3. follows the run by SSE (`GET /v1/runs/{id}/stream`, op `stream_open`, share set by
   `--stream-ratio`) or by polling `GET /v1/runs/{id}` (op `get_run`) until it is
   terminal / `awaiting_approval` / `--run-timeout`.

## Against the local compose stack

```bash
cp .env.example .env            # set OPENROUTER_API_KEY or a cheap model
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
curl -fsS http://127.0.0.1:8000/readyz

# Legacy dev tenancy (X-Tenant-ID header only); for OIDC stacks pass a token per tenant:
#   --tenant acme:8:env:ACME_TOKEN --tenant globex:1:env:GLOBEX_TOKEN
.venv/bin/python -m loadtest.haas_load --base-url http://127.0.0.1:8000 \
    --tenant acme:8 --tenant globex:1 --tenant initech:1 \
    --concurrency 20 --duration 120 --stream-ratio 0.5 --out loadtest-results.json

.venv/bin/python -m scripts.slo_report loadtest-results.json            # markdown, exit 1 on fail
.venv/bin/python -m scripts.slo_report loadtest-results.json --format json --out slo.json
```

Override targets with `--targets my-targets.json` (same shape as
`scripts.slo_report.DEFAULT_TARGETS`, e.g. `{"latency": {"get_run": {"p95_ms": 150}}}`).
Checks with fewer than `min_samples` samples report `insufficient_data` (exit 1 unless
`--allow-insufficient`).

## Interpreting results

* Latencies are measured client-side, so they upper-bound the server-side SLI boundaries.
* Availability excludes 401/403/429 (unauthorized and quota-rejected requests are not
  in the SLI denominator); transport errors and 5xx count as failures.
* `first_event` (admission → first SSE event) is the client-observable proxy for the
  "accepted work starts p95 < 5 s" SLO; it includes provider latency when the first
  event is model output, so run it with a fast/cheap model.
* Model-provider cost: every run calls the configured model. Use `--max-steps 1` and a
  short `--prompt`, and never point this at production without a tenant budget.
