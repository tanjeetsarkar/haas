---
name: verification-ladder
description: 'Ordered HaaS verification commands (ruff, pyright, targeted pytest, plugin doctor, migrations round-trip, OpenAPI snapshot, TS builds, boundary check, compose e2e) with baseline comparison. Use when: recording a baseline, verifying a slice, choosing which tests to run, deciding whether a failure is new or pre-existing.'
---

# Verification ladder

Run the **smallest rungs that cover the touched paths**; climb only when a rung is affected. Always run
the same rungs for the baseline (before the first edit) and after the change, and compare.

All Python tools come from the repo virtualenv: prefix with `.venv/bin/` (or `uv run`).

| # | Rung | Run when touching | Command |
|---|---|---|---|
| 1 | Lint | any `.py` | `.venv/bin/ruff check <touched paths>` (CI scope: `app haas_cli migrations scripts tests`) |
| 2 | Types | `app/ haas_cli/ haas_plugin_sdk/ haas_sdk/ migrations/ scripts/` | `.venv/bin/pyright <touched paths>` (full: `.venv/bin/pyright`) |
| 3 | Targeted tests | anything | `.venv/bin/pytest <tests for touched modules> -q` |
| 4 | Plugin health | `plugins/**` | `.venv/bin/haas-plugin doctor` and `.venv/bin/pytest tests/plugins -q` |
| 5 | Boundary | any | `python3 .github/skills/core-boundary-check/scripts/check_boundaries.py \| tail -1` |
| 6 | Migrations | `migrations/**`, models | `.venv/bin/alembic upgrade head && .venv/bin/alembic downgrade base && .venv/bin/alembic upgrade head` (needs PostgreSQL via `POSTGRES_DSN`) |
| 7 | API contract | `app/api/**`, `app/main.py`, response models | `.venv/bin/python -m scripts.update_openapi_snapshot && git diff --stat -- docs/openapi-v1.json` |
| 8 | TS SDK | `haas_sdk_ts/src/**` | `cd haas_sdk_ts && npm run build` |
| 9 | Web | `web/**` | `cd web/marketplace && npm run build` |
| 10 | Full suite | before integrating a core slice | `.venv/bin/pytest -q` |
| 11 | Compose E2E | compose, Dockerfile, worker wiring | `docker compose -f docker-compose.yml -f tests/e2e/docker-compose.e2e.yml up --build -d && python tests/e2e/run_e2e.py` then `docker compose ... down` (never `-v` without user approval) |

Map modules to tests by name first (`app/core/foo.py` → `tests/**/test_foo*.py`), then with a search for
the module's import path under `tests/`.

## Baseline record (paste into the plan's Baseline section)

```text
ruff:     <N errors> (<paths>)
pyright:  <N errors>
pytest:   <passed>/<failed> for <selection>; failing: <test ids>
boundary: <E errors>, <W warnings>
```

## Classifying failures

- `NEW` — fails now, passed (or did not exist) in baseline → the slice must fix it.
- `PRE-EXISTING` — failed in baseline too → Follow-ups, do not fix inside the slice.
- `FLAKY` — differs between two identical runs → report both outputs; no third run.
- `ENV` — needs an unavailable service (PostgreSQL, Redis, Docker) → report as not run, with the reason.
