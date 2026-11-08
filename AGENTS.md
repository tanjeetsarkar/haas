# AGENTS.md — HaaS operating guide for AI agents

This file is binding for every agent (human-invoked or subagent) working in this repo.
Specialist roles live in [.github/agents/](.github/agents/), reusable procedures in
[.github/skills/](.github/skills/). When this file and an agent file disagree, this file wins.

## 1. Product philosophy (non-negotiable)

HaaS = **Harness as a Service**: a server-side, multi-tenant, *extensible* agent harness.

- **Core is a kernel, not a product.** Core owns: run lifecycle and orchestration, tool dispatch and
  envelopes, plugin discovery/loading, tenancy/identity/authorization, secrets, sandboxing, events/streams,
  storage of core entities, public API/SDK contracts, and **generic extension points**.
- **Everything domain-specific is an extension.** An extension is either
  - a **plugin pack** (`plugins/<id>/`: `plugin.json`, skills, tools, endpoints — see
    [docs/plugin_architecture.md](docs/plugin_architecture.md)), and/or
  - a **plugin service**: an independently deployable service with its own code, storage, migrations,
    compose file, UI and client library. It talks to core only through public contracts (REST/SSE API,
    SDK, webhooks) and is exposed to the model through endpoint tools (`endpoints/*.json`, HTTP/MCP).
- **Core never knows a plugin by name.** No plugin ids, tool names, tables, env vars, UI components or SDK
  types for a specific plugin in core paths (§2). Enforced by
  [check_boundaries.py](.github/skills/core-boundary-check/scripts/check_boundaries.py).
- **Missing capability ⇒ core extension point, never a special case.** If a plugin needs something core does
  not offer (storage, events, UI slots, config), design a *generic* facility usable by every plugin, record it
  in an ADR, and ship it as a core change. Example: plugins needing persistence get a plugin-scoped storage
  API, not raw `ctx.store` access to core tables.
- **Dependency direction:** `plugins/*`, plugin services → public surfaces (`haas_plugin_sdk`, REST API,
  `haas_sdk`, `@haas/sdk`) → core internals. Never the reverse. Existing `from app.core...` imports in plugins
  are legacy debt: don't add new ones; migrate them when a slice touches that file and an SDK equivalent exists.

## 2. Repository map and ownership

| Path | Kind | Owner agent |
|---|---|---|
| `app/`, `migrations/`, `alembic.ini`, `Dockerfile`, `docker-compose*.yml`, `deploy/` | Core | `core-engineer` |
| `haas_plugin_sdk/` | Core public plugin surface | `core-engineer` |
| `haas_sdk/`, `haas_sdk_ts/`, `haas_cli/`, `web/` (generic marketplace UI) | Core clients | `client-engineer` |
| `docs/openapi-v1.json`, `docs/asyncapi.yaml`, `docs/contracts/` | Core contracts | `core-engineer` (generated/verified via scripts) |
| `plugins/<id>/`, `services/<id>/` (plugin services) | Extensions | `plugin-engineer` |
| `docs/adr/`, `docs/plans/`, `docs/design/` | Decisions & plans | `architect` |
| `tests/` | Mirrors owner of code under test | owner of the slice |
| `AGENTS.md`, `.github/agents|skills|instructions|hooks/` | Agent governance | **user only** (agents propose diffs) |

Plugin services live under `services/<id>/` with their own `README.md`, compose file, dependency manifest,
tests and migrations. Core compose files must not declare plugin services; each service ships its own
`docker-compose.yml` that joins the core network.

## 3. Agent roster

| Agent | Use for | Edits |
|---|---|---|
| `haas-lead` | Entry point. Breaks goals into slices, delegates, enforces budgets, integrates | none (git only) |
| `architect` | Boundary decisions, ADRs, slice plans, contracts for new extension points | `docs/adr`, `docs/plans`, `docs/design` |
| `core-engineer` | Kernel and generic extension points, migrations, public plugin SDK | core paths |
| `plugin-engineer` | Plugin packs and standalone plugin services, extractions | `plugins/`, `services/` |
| `client-engineer` | Python/TS SDKs, CLI/TUI, generic web UI | client paths |
| `verifier` | Runs the verification ladder, compares to baseline, reports evidence | none |
| `reviewer` | Independent review against this file, boundaries, security, tests | none |

## 4. Slice lifecycle

Work is delivered in **slices**: one coherent, independently verifiable change.

1. **Frame** (`haas-lead`): restate goal, record the baseline (§8), pick the next slice.
2. **Plan** (`architect`): write `docs/plans/<NNN>-<slug>.md` from the
   [slice-planning skill](.github/skills/slice-planning/SKILL.md). Core-touching slices also need an ADR
   (new or existing). Skip the plan only for trivial fixes (≤ ~30 lines, single owner, no contract change).
3. **Build** (one engineer per slice): implement the plan *and its tests*; run the fast gates (ruff on
   touched files, targeted pytest) before handing off.
4. **Verify** (`verifier`): run the ladder for the touched areas and diff against baseline.
5. **Review** (`reviewer`): verdict `APPROVE` or `CHANGES_REQUESTED` with concrete, numbered findings.
6. **Integrate** (`haas-lead`): update plan status, propose a commit (one per slice, Conventional Commits,
   e.g. `refactor(atlas): move entity cache out of core migrations`). Commit only when the user has asked
   for commits; never push.

If a slice needs both core and plugin changes, split it: **core extension point first** (generic, tested,
plugin-agnostic), **then** the plugin slice that consumes it.

## 5. Handoff contract

Every agent ends its turn with exactly this block (it is the only thing the caller is guaranteed to read):

```markdown
## Handoff
- Slice: <NNN-slug> — <one-line goal>
- From → To: <agent> → <agent | user>
- Status: DONE | PARTIAL | BLOCKED | NEEDS_DECISION
- Changed: <files, or "none">
- Evidence: <commands run + pass/fail counts vs baseline>
- Boundary: <core touched? yes → ADR link | no>; check_boundaries delta: <before → after>
- Decisions needed: <numbered questions with a recommended option, or "none">
- Follow-ups (out of scope, not done): <bullets or "none">
- Next: <the single next action and who does it>
```

Rules: no "should work" — every claim of success cites a command result. `PARTIAL`/`BLOCKED` must say what
was tried. Keep it under ~40 lines; link to files instead of pasting them.

## 6. Delegation rules

- **Depth 1.** Only `haas-lead` delegates. Specialists never invoke other agents; they hand back to the lead
  with `Next:`. When a user drives a specialist directly, the specialist hands back to the user.
- **One writer per path at a time.** Never run two editing agents on overlapping paths in parallel.
  Read-only agents (`architect` research, `verifier`, `reviewer`) may run in parallel.
- **Self-contained prompts.** A delegation includes: slice id, plan path, goal, allowed paths, acceptance
  criteria, baseline, and budget. Subagents do not see the caller's conversation.
- **No blind retries.** Re-delegating the same objective requires stating what changed (new info, narrower
  scope, different approach). Identical re-delegation is forbidden.
- **Lead does not implement.** If the lead is tempted to edit, it delegates; if no specialist fits, it asks
  the user.

## 7. Budgets and stop rules

Budgets are **defaults, not cages**: an agent may exceed one soft budget once per slice if it writes
`Budget override: <reason>` in its handoff. Hard stops are never overridden.

**Soft budgets**
| Budget | Default |
|---|---|
| Exploration before producing a plan or first edit | ~25 file reads / searches |
| Slice size | ~400 changed lines (excl. generated files, lockfiles, snapshots), ≤ 1 migration |
| Attempts to fix the same failing check | 3, each with a *different* stated hypothesis |
| Review ↔ fix rounds per slice | 2 |
| Specialist delegations per slice | 6 |

**When a budget is exhausted:** stop, return `Status: BLOCKED` with the hypotheses tried and evidence, and
let the lead/user decide. Do not "try one more thing".

**Hard stops (always ask the user first)**
- Destructive or irreversible actions: deleting data/volumes, `git push`, force/reset, history rewrite,
  dropping tables outside a reviewed migration, removing a plugin's user-visible feature.
- Public API/SDK breaking changes (see [docs/api-deprecation-policy.md](docs/api-deprecation-policy.md)).
- Adding a plugin-specific reference to core, or weakening tenancy/RLS, secrets scoping, envelope signing,
  SSRF guards or sandbox restrictions.
- New runtime dependencies in core `pyproject.toml`.
- Editing agent governance files (§2).

**Anti-loop signals** — if any appears, stop and hand off instead of continuing:
the same error twice after a "fix"; editing a file back to a previous state; widening scope to make a test
pass; fixing failures that exist in the baseline; tests changed to match broken behaviour.

## 8. Baseline first

Before the first edit of a slice, record the baseline for the areas you will touch (see the
[verification-ladder skill](.github/skills/verification-ladder/SKILL.md)): ruff, pyright, targeted pytest and
`check_boundaries.py`. Pre-existing failures are **not** yours to fix; list them under Follow-ups. A slice is
green when it introduces no new failures and meets its own acceptance criteria.

## 9. Definition of done

- Acceptance criteria in the plan are met and demonstrated by evidence.
- Tests added/updated next to the change (unit for logic, integration for DB/queues, contract for API).
- No new ruff/pyright errors; targeted and affected tests pass; `check_boundaries.py` error count did not
  increase (and decreased for extraction slices).
- API changes: OpenAPI snapshot regenerated (`python -m scripts.update_openapi_snapshot`), changelog entry in
  [docs/api-changelog.md](docs/api-changelog.md); migrations round-trip (upgrade → downgrade → upgrade).
- Docs updated where behaviour changed; plan status set; reviewer verdict `APPROVE`.

## 10. Engineering conventions

- Python ≥ 3.11, typed, async-first; Pydantic v2 models for all tool args and API payloads. Use `.venv/bin/…`
  tools. Lint config lives in [pyproject.toml](pyproject.toml).
- Multi-tenancy: every persisted row carries `tenant_id` with forced RLS; every query is tenant-scoped.
- Secrets only via `RunContext.get_secret` scoped by skill `allowed_secrets`; outbound HTTP only after
  `validate_safe_outbound_url`.
- Migrations are additive and reversible; contract changes are additive unless an ADR says otherwise.
- Prefer stdlib and existing dependencies; follow patterns in neighbouring code before inventing new ones.
- Design references: [docs/design/README.md](docs/design/README.md), ADR index
  [docs/adr/README.md](docs/adr/README.md), roadmap [docs/roadmap.md](docs/roadmap.md).
