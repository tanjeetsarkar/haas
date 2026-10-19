# HaaS (Harness-as-a-Service) — Complete Technical Walkthrough

> Scope: every subsystem visible in the repository snapshot (version 3.0.0): the **core** service (FastAPI + Celery + PostgreSQL + Redis), the **TUI/CLI** (`haas_cli`), the **SDKs** (`haas_sdk` Python, `haas_sdk_ts` TypeScript), the plugin system, the marketplace / multi-agent / operations tracks, and the design documents (SDD, architecture, threat model, roadmap).
> Method: each feature is explained from the **code first**; design docs are used only to explain intent. Where the code and docs disagree, this file says so (see §12).
> Snippets are excerpts (`...` = elided lines), copied from the codebase.

---

## 0. Table of contents

1. What the system is (mental model)
2. Repository map
3. End-to-end flow of a run (textual sequence)
4. Core service — identity, tenancy, policy
5. Core service — plugin system (skills, tools, endpoints, artifacts)
6. Core service — durable workflow engine (leases, outbox, events, budgets, approvals)
7. Core service — execution, secrets, egress, redaction
8. Core service — LLM gateway, streaming, OpenAI-compatible facade
9. Core service — advanced tracks (artifacts/retention, marketplace, multi-agent, operations, webhooks)
10. TUI and CLI (`haas_cli`)
11. SDKs (Python and TypeScript)
12. Tooling: migrations, load test, SLO report, dev stack
13. Industry-standard alignment (table)
14. Clever tricks (catalogue)
15. Limitations and observed gaps (honest audit)
16. Adoption path and improvement roadmap

---

## 1. What the system is

HaaS is an **agent run service**: a client submits a prompt; the service executes a bounded loop of *model call → tool calls → model call …* until the model answers, with **human approval gates**, **budgets**, **multi-tenancy**, and **durable recovery**. Clients observe it via REST + Server-Sent Events (SSE).

Mental model — three planes:

| Plane | Components | Trust |
|---|---|---|
| **Control plane** | FastAPI (`app/main.py`), PostgreSQL, maintenance service (outbox relay + reconciler) | Trusted platform code |
| **Orchestration plane** | Celery `worker-orchestrator` (holds OpenRouter key and Ed25519 *signing* key) | Trusted, no tenant code should run |
| **Execution plane** | Celery `worker-tools` (queues `q_tools_fast`, `q_tools_sandbox`; holds only the *verify* key; no DB DSN, no LLM key) | Runs plugin code; least privilege |

Key principle (ADR-02 in the SDD): **PostgreSQL is the workflow authority; Redis/Celery only carry redeliverable messages.** Everything else in the code follows from that.

---

## 2. Repository map

```
app/
  main.py                FastAPI app: runs, approvals, threads, agents, admin, SSE, OpenAI facade
  api/                   Feature routers (artifacts, access, operations, marketplace, agent_mesh, isolation)
  core/
    config.py            Pydantic settings + production profile validation
    identity.py          OIDC verifier, API-key auth, IdentityMiddleware
    authorization.py     Role → Action matrix, separation-of-duties
    database_context.py  Per-request tenant ContextVar
    store.py             DistributedStore: tenancy/admin/threads/agents/runs (mixes in WorkflowMixin + area stores)
    workflow.py          Leases, fencing, outbox, events, approvals, budgets, reconciler
    plugins.py           PluginRegistry (discovery, skills, tools, endpoints, MCP adapter)
    plugin_manifest.py   Static plugin.json parsing + content hash
    plugin_artifacts.py  OCI-layout content-addressed plugin artifacts
    llm.py               OpenRouter gateway, JSON repair, stream accumulator
    envelope.py          Ed25519-signed tool envelopes
    credentials.py       Single-use sealed credential grants
    egress.py            DNS-pinned egress broker
    redaction.py         Secret redaction
    secret_vault.py      AES-256-GCM vault for run secrets
    retention.py         Retention classes, signed erasure receipts
    object_store.py      Local / S3 (SigV4) object store
    openai_compat.py     OpenAI Chat/Responses facade mapping
    output_stream.py     Live token-delta publisher (Redis streams)
    tenant_policy.py     Immutable policy rules + digest verification
    agents.py            AgentConfiguration (immutable agent revisions)
    observability.py     JSON redacting logs, OpenTelemetry
    maintenance.py       Maintenance loop
  worker/                celery_app.py, tasks.py (orchestrate / execute_tool / outcome / webhook)
  marketplace/           manifest v2, semver, signing, review, tlog, packaging, adapters, tiers
  sandbox/protocol.py    haas.sandbox/1 wire format
migrations/versions/     0001 … 0017 (Alembic, raw SQL)
plugins/                 weather, github-ops, web-research, incident-triage, data-analyst, revenue-ops, plugin-creator, _templates
haas_cli/                chat TUI (Textual), cli, plugin scaffolder, profiles, session
haas_sdk/                Python SDK (sync + async)
haas_sdk_ts/             TypeScript SDK
haas_plugin_sdk/         Plugin authoring SDK (@tool decorator)
loadtest/, scripts/      Load generator, SLO report, OpenAPI snapshot
docs/                    SDD, architecture, DFD/ERD, threat model, roadmap, TUI design, runbooks
```

---

## 3. End-to-end flow of a run (textual sequence)

This is the master narrative; later sections zoom in on each step.

### 3.1 Happy path with a tool and an approval

1. **Client → API**: `POST /v1/runs` with `Authorization: Bearer <key>`, `X-Tenant-ID`, optional `Idempotency-Key`.
2. **Middleware chain** (`app/main.py`): `RequestIdMiddleware` → `DatabaseTenantMiddleware` → `IdentityMiddleware` (verifies credential, strips forged `x-haas-*` headers, rewrites `X-Tenant-ID` to the canonical slug) → `RequestSizeLimitMiddleware`. Route dependency `require_action(RUN_SUBMIT)` enforces RBAC.
3. **Admission** (`create_run` in `main.py` → `store.create_run`): checks idempotency replay, loads the tenant's *active immutable policy*, validates models/skills/budgets/webhook host against it, optionally pins an *agent revision*, builds the system prompt (skill catalog), stores run secrets encrypted in Redis, then in **one PostgreSQL transaction**: locks the thread, inserts the run (`status='queued'`), appends the `run_queued` event, inserts an `orchestrate` **outbox row**, records the idempotency receipt, claims `threads.active_run_id`.
4. **202 response** returns `run_id`, `thread_id`, stream URLs. Fast path `_after_commit` immediately tries `relay_outbox`; if that fails the **maintenance service** relays later.
5. **Outbox relay** → Celery task `haas.orchestrate` on `q_orchestrator` with `task_id=message_id`.
6. **Orchestrator** `acquire_lease`: inserts a `consumer_inbox` row (dedupe), checks `status='queued'`, `state_version`, `next_action`, tenant still `active`, deadline; then sets `status='running'`, `lease_epoch+1`, lease expiry.
7. **Step**: `begin_step` reserves estimated model cost in `usage_ledger` (`reserved`), creates a `run_steps` attempt; `gateway.chat_completion` calls OpenRouter (planner model on step 1 / after errors, primary otherwise).
8. **commit_step** (single transaction): settles the reservation with real cost, then exactly one branch:
   - *complete* → `_finalize` (run + thread history + `run_completed` event + webhook outbox row);
   - *dispatch* → insert `tool_invocations`, write **signed envelopes** to the outbox (`destination='tool'`), emit `tool_dispatched`;
   - *approval* → insert `approval_batches` (SHA-256 digest of exact calls + expiry), status `input_required`, emit `input_required`.
9. **Approval** (if needed): approver (different principal than submitter) calls `POST /v1/approvals/{batch}/decision` with the `batch_digest` they reviewed. `decide_approval` locks the run, checks digest/expiry, inserts an immutable `approval_decisions` row, sets `next_action='decision'`, enqueues orchestrate. Orchestrator `_decision` either dispatches the tools or appends "Action Denied" tool messages and continues the model.
10. **Tool worker** `execute_tool_task`: verify Ed25519 envelope (signature, expiry, args digest) → check `haas:cancelled:{run}` → `SET NX haas:effect:{effect_key}` (idempotency) → run handler under `asyncio.wait_for` → cache result → enqueue `haas.record_tool_outcome`.
11. **Outcome recording**: `record_tool_outcome` dedupes via `consumer_inbox`, updates the invocation, and when no invocations remain `dispatched`, flips the run to `queued/aggregate`.
12. **Aggregate**: merge tool messages in ordinal order, apply identical-call circuit breaker, update active skills (activate_skill unlocks tools), `commit_aggregate` → `next_action='step'` → next model step.
13. **Observation**: the client's SSE stream replays `run_events` from PostgreSQL (`id` = integer sequence), woken by Redis stream ticks.

### 3.2 Failure paths in one paragraph each

- **Worker dies mid-step**: lease expires → `reconcile()` bumps `lease_epoch`, requeues with `run_recovering` event (bounded attempts). The stale worker's writes fail `_fenced`.
- **Tool worker dies after side effect**: redelivery reuses the same `effect_key` (sent as `Idempotency-Key`); if no outcome ever arrives, invocation becomes `unknown` after `TOOL_OUTCOME_TIMEOUT_SEC` and is **never auto-retried**.
- **Broker publish fails after DB commit**: outbox row stays `pending`; relay retries with exponential backoff until `dead`.
- **Cancel**: `request_cancel` bumps lease epoch, cancels child runs, either finalizes `cancelled` or enters `cancelling` while tools are outstanding.
- **Approval expires / deadline passes**: reconciler finalizes `failed` with `approval_expired`/`budget.deadline`.

---

## 4. Core — identity, tenancy, policy

### 4.1 Settings and profile gating (`app/core/config.py`)

A single `Settings` (pydantic-settings) drives everything. `validate_profile_settings` is a **fail-closed production gate**: with `HAAS_PROFILE=prod` the process refuses to start without complete HTTPS OIDC, a non-dev AES key, signing/verify keys, sandbox mode, etc.

```python
if role == "orchestrator":
    if not config.envelope_signing_key:
        errors.append("HAAS_ENVELOPE_SIGNING_KEY is required in production")
    elif config.envelope_signing_key == DEV_SIGNING_SEED_B64:
        errors.append("HAAS_ENVELOPE_SIGNING_KEY must not use the development key")
if role == "tools":
    if not config.envelope_verify_key:
        errors.append("HAAS_ENVELOPE_VERIFY_KEY is required in production")
```

*Standards*: twelve-factor config, secure-by-default profiles. *Trick*: roles (`api|orchestrator|tools`) get different required keys, so the tool worker is **provably** not configured with the signing key.

### 4.2 Authentication (`app/core/identity.py`)

Two credential types, both resolved to an `AuthenticatedIdentity(tenant_id, principal_id, roles, …)`:

1. **API keys** (`hk_live_…` / `hk_test_…`): prefix (first 16 chars) is an index; SHA-256 hash compared constant-time; `hk_test_` rejected in prod.
2. **OIDC JWT**: RS256 only, JWKS cached 5 min with forced refresh on unknown `kid`, required claims `exp iat iss sub aud`, and a **revocation check**: `iat < credential_valid_after` → 401.

```python
if header.get("alg") != "RS256" or not isinstance(kid, str):
    raise IdentityError(401, "Unsupported bearer credential.")
...
claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audience,
                    issuer=self.issuer, options={"require": ["exp","iat","iss","sub","aud"]})
```

`IdentityMiddleware` first **strips spoofable identity headers** (`x-haas-*`, `x-user-id`, `x-forwarded-user`, …), requires exactly one tenant header and one `Authorization`, then rewrites `X-Tenant-ID` to the canonical slug so downstream code can't be confused by UUID-vs-slug aliases:

```python
scope["headers"] = [
    (key, value) for key, value in scope.get("headers", [])
    if not key.lower().startswith(_INTERNAL_HEADER_PREFIXES)
    and key.lower() not in _INTERNAL_HEADERS
]
...
scope["headers"] = [
    (key, identity.tenant_id.encode("utf-8") if key.lower() == b"x-tenant-id" else value)
    for key, value in scope["headers"]
]
```

*Dev mode*: `allow_legacy_header_tenancy` lets a request without `Authorization` use `X-Tenant-ID` as a plain selector (not authentication — repeatedly called out in docs).

*Standards*: OIDC/JWT (RFC 7519/7517), constant-time compare, header-injection defence (confused-deputy). *Limit*: single JWKS key-set, no `nbf`/DPoP, RS256 only.

### 4.3 Authorization (`app/core/authorization.py`)

Deny-by-default role → action matrix. Unknown role or action ⇒ no access. `break_glass` is an **actionless** role until a time-limited workflow exists.

```python
"approver": frozenset({Action.RUN_APPROVAL_READ, Action.RUN_APPROVE}),
"auditor": frozenset({Action.AUDIT_READ, Action.USAGE_READ}),
"break_glass": frozenset(),
```

**Separation of duties** — a submitter can never approve their own run, and runs without submitter provenance can't be approved by authenticated users:

```python
if identity.principal_id == str(submitted_by_principal_id):
    raise HTTPException(status_code=403,
        detail="Run submitters cannot approve or reject their own pending actions.")
```

Routes use `_require_action(Action.X)` as a FastAPI dependency. `GET /v1/runs/{id}` is dual-purpose: approver-only identities get a *reduced projection* (no thread/result).

*Standards*: RBAC (NIST), four-eyes principle (SOX-style SoD).

### 4.4 Multi-tenancy in PostgreSQL

Three layers:

**(a) Request context** — `DatabaseTenantMiddleware` puts the selected tenant into a `ContextVar`.

**(b) Transaction-local setting** — every DB access goes through `store.connection()`, which opens a transaction and `set_config(..., true)` (transaction-local, so pooled connections can't leak a tenant):

```python
await conn.execute(
    "SELECT set_config('haas.tenant', $1, true), "
    "set_config('haas.legacy_dev', $2, true)",
    tenant_context.get(), "on" if settings.haas_profile == "dev" else "off")
```

**(c) Forced RLS with three policy roles** (`migrations/0008`): `haas_tenant_app` (tenant-scoped), `haas_execution_service` (cross-tenant but narrow grants, **not** `BYPASSRLS`), and the schema owner.

```sql
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_boundary ON {table} TO haas_tenant_app
    USING (tenant_uuid = resolve_haas_tenant_uuid(current_setting('haas.tenant', true)))
    WITH CHECK (...same...);
CREATE POLICY execution_service ON {table} TO haas_execution_service USING (true) WITH CHECK (true);
```

`resolve_haas_tenant_uuid` is a `SECURITY DEFINER` function with a pinned `search_path` that maps slug / UUID / approved legacy alias → canonical UUID. **Composite foreign keys** (`FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id)`) make cross-tenant references structurally impossible. A **production startup self-check** in `DistributedStore.init` refuses to run if the login is superuser/BYPASSRLS/table owner or any `RLS_TABLES` entry lacks forced RLS:

```python
"SELECT NOT rolsuper AND NOT rolbypassrls AND pg_has_role(current_user, $1, 'member') ..."
"AND bool_and(relrowsecurity AND relforcerowsecurity) ..."
```

*Standards*: defence-in-depth multi-tenancy (app-level RBAC + DB RLS), least-privilege DB roles. *Clever*: `RLS_TABLES` is assembled by concatenating each feature area's tuple (`*_isolation.RLS_TABLES, *_access.RLS_TABLES …`), so adding a table to a new area automatically adds it to the startup assertion.

### 4.5 Tenant policy (`app/core/tenant_policy.py`)

Policy = immutable, versioned JSON (`tenant_policy_revisions`, update/delete blocked by trigger) with a SHA-256 digest recomputed on every load; mismatch raises `TenantPolicyIntegrityError`.

```python
if not isinstance(digest, str) or not hmac.compare_digest(digest, policy_digest(raw_rules)):
    raise TenantPolicyIntegrityError("Active tenant policy digest does not match its rules.")
```

Rules: allowed model providers/models, per-plugin grants **bound to the plugin's content digest**, exact webhook hostnames (IDNA-normalised, wildcards rejected), max steps/cost, retention class, marketplace tiers. At admission `create_run` only *narrows* (`resolve_run_allowlist` never widens). `activate_admin_policy` verifies each grant matches a currently loaded plugin digest.

### 4.6 Thread and agent-revision model

- **Threads** carry `history_version`, `active_run_id` (partial unique index `runs_one_active_thread_idx` guarantees one live run per thread) and branch ancestry (`parent_thread_id`, `parent_history_version`). Completion commits history only if `history_version` still matches the version captured at admission (optimistic concurrency).
- **Agent revisions** (`app/core/agents.py`, migration 0007): `draft → validated → published`, a trigger forbids any other mutation. Admission with `agent_revision_id` re-checks policy revision + registry fingerprint equality and refuses overrides:

```python
if req.model_fields_set.intersection(AgentConfiguration.model_fields):
    raise HTTPException(422, "Pinned agent configuration cannot be overridden.")
```

---

## 5. Core — plugin system

### 5.1 Pack layout and static manifest

A plugin pack is `plugins/<id>/plugin.json` + `skills/`, `tools/*.py`, `endpoints/*.json`. `read_plugin_manifest` is **metadata-only** (never imports code), rejects unknown keys, path escapes (`..`, symlinks out of the pack), id ≠ directory name.

`hash_plugin` makes a content fingerprint over id, version and every file's SHA-256 → used for policy grants, run pinning, OCI artifacts.

```python
h.update(f"{manifest.id}\0{manifest.version}\0".encode())
for f in plugin_fingerprint_files(manifest):
    h.update(f.relative_to(manifest.root).as_posix().encode() + b"\0")
    h.update(hashlib.sha256(f.read_bytes()).digest())
```

### 5.2 Discovery in two phases (`PluginRegistry.discover_all`)

- **Phase A (no code runs)**: read manifests from `plugins_dir` + extra roots, apply precedence (shadowing), `PLUGINS_ALLOW/DENY`, `enabled`.
- **Phase B**: topological load by `requires.plugins`, then `_load_plugin`, which **snapshots and rolls back** the registry on any failure and verifies `provides` matches what was actually registered:

```python
tools_before, skills_before = dict(self.tools), dict(self.skills)
...
if declared is not None and sorted(declared) != actual:
    raise ValueError(f"`provides.{what}` {sorted(declared)} does not match what the plugin registered {actual}")
...
except BaseException as exc:
    self.tools, self.skills = tools_before, skills_before
    for mod_name in modules: sys.modules.pop(mod_name, None)
```

A failed plugin is isolated; healthy ones still serve unless `PLUGINS_STRICT`. `registry.fingerprint` (hash of loaded plugin digests) is stamped on runs and agent revisions to detect **drift** between API and workers.

*Clever trick*: modules are registered in `sys.modules` **before** executing, otherwise Pydantic can't resolve `from __future__ import annotations` types — documented in the template.

### 5.3 Skills — three-tier progressive disclosure (agentskills.io)

| Tier | Content | When in model context |
|---|---|---|
| 1 | `name` + `description` | Always, in the system prompt catalog (`render_catalog`, compacts descriptions to `SKILLS_PROMPT_MAX_CHARS`) |
| 2 | `SKILL.md` body | After `activate_skill` |
| 3 | `references/*.md` | On demand via `read_skill_reference` |

Frontmatter example (`plugins/weather`):

```yaml
---
name: weather
description: "Gets current weather ... Use when the user asks about the weather ..."
metadata:
  required_tools: "get_weather_forecast"
  allowed_secrets: ""
---
```

**Tool unlocking** is the capability model: only `is_core` tools + `required_tools` of *active, ready, allow-listed* skills are in the schema sent to the model **and** re-checked in the tool worker:

```python
def resolve_unlocked_tool_names(self, active_skills, allowed_skills=None):
    unlocked = {name for name, t in self.tools.items() if t.is_core}
    for s_name in self.effective_skills(active_skills, allowed_skills):
        unlocked.update(self.skills[s_name].required_tools)
    return {n for n in unlocked if n in self.tools}
```

`resolve_sorted_tool_schemas` sorts tools by name so the prompt prefix is stable → **provider KV-cache friendly** (documented in the v2 RFC).

`activate_skill` hides unavailable skills by returning the same "unknown" message as for non-existent ones (no inventory leak). `read_skill_reference` defeats path traversal with `Path.is_relative_to` (the original spec used a bypassable `startswith`).

### 5.4 Tools — native Python

Registered through a decorator; arguments are Pydantic models whose `Field(description=…)` text **is prompt text**. Example (`plugins/github-ops`): read tools are open, writes carry `requires_approval=True`.

```python
@registry.tool(
    name="github_create_issue",
    description="Creates a new GitHub issue. WRITES to GitHub and requires human approval.",
    args_schema=CreateIssueArgs,
    requires_approval=True,
)
async def github_create_issue(ctx: RunContext, args: CreateIssueArgs) -> str: ...
```

`RunContext.get_secret` only releases secrets listed in an *active* skill's `allowed_secrets`:

```python
if key not in self.allowed_secrets:
    raise SecurityViolationError(f"Access denied: Secret '{key}' is not declared in any active skill's `allowed_secrets`.")
```

Sample packs show patterns: SSRF-safe web fetch with manual redirect re-validation (`web-research`), read-only SQLite via **authorizer + progress handler** (`data-analyst`), deterministic log analysis with regex normalisation and severity matrix (`incident-triage`), seeded demo DB + approval-gated refund (`revenue-ops`).

*Data-analyst trick* — SQLite authorizer allow-list + time-boxed progress handler:

```python
ALLOWED_ACTIONS = {SQLITE_SELECT, SQLITE_READ, SQLITE_FUNCTION, SQLITE_RECURSIVE}
conn.set_authorizer(_authorizer)
conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
```

### 5.5 Endpoint tools — HTTP and MCP

`endpoints/*.json` declares a remote tool; `register_external_manifest` builds a Pydantic model dynamically (`create_model`), expands `${ENV:-default}`, validates the URL (SSRF) at load and again with DNS at call time, signs the body with HMAC-SHA256 + bearer, and forwards the stable **effect key** as `Idempotency-Key`:

```python
if ctx.effect_key:
    headers["Idempotency-Key"] = ctx.effect_key
    headers["X-HaaS-Effect-Key"] = ctx.effect_key
...
if protocol == "mcp_http":
    headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
    headers["Mcp-Method"] = "tools/call"
    request_id = uuid.uuid4().hex   # response id is verified against this
```

`_parse_mcp_response` accepts JSON **or** SSE (Streamable HTTP), JSON-RPC errors and `isError` become `ToolRemediationError` so the model can self-correct. *Limit* (acknowledged in `integrations-usage.md`): this is a tool-call adapter, not a full MCP client (no initialize/session, discovery, OAuth); the HMAC covers `json.dumps(sort_keys=True)`, not raw bytes.

### 5.6 Plugin artifacts (`app/core/plugin_artifacts.py`)

Deterministic OCI-image-layout artifacts: tar layer (mtime 0, uid 0, fixed mode) + config + manifest (`artifactType: application/vnd.haas.plugin.v1`). The artifact digest is the SHA-256 of the manifest, "transitively pinning every byte". `materialize()` refuses to extract unless the (re)packed digest equals the run's pin; extraction is read-only (0444/0555), path-traversal checked (`_safe_member`).

```python
packed = pack_plugin(manifest)
if packed.digest != pinned:
    raise ArtifactMismatchError(f"plugin '{manifest.id}' artifact {packed.digest} does not match the pinned {pinned}")
```

*Standards*: OCI image-layout, content-addressable storage, reproducible builds. `ArtifactStore.put` uses `fcntl.flock` + atomic rename.

### 5.7 Plugin authoring SDK and scaffolding

- `haas_plugin_sdk.tool(...)` decorator stores a frozen `ToolSpec` on the function (`__haas_tool__`), validates signature/annotations, keeps the function directly unit-testable. Targets the `haas.sandbox/1` ABI (tools never import `app`).
- `haas-plugin` CLI (`haas_cli/plugin.py`): `new plugin|skill|tool|endpoint` (from `plugins/_templates` with `{{placeholders}}`; `_write_new` refuses to overwrite), `list`, `inspect`, `doctor` (loads like the server and cross-checks: TODO left, missing refs, secrets read by code not in `allowed_secrets` via AST walk of `get_secret("…")`, tool not referenced by any skill, catalog over budget), `migrate` (flat layout → packs using **union-find** to merge skills that share tool files).

```python
def secrets_used(source: str) -> set[str]:
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_secret" and node.args
            and isinstance(node.args[0], ast.Constant)): ...
```

---

## 6. Core — durable workflow engine (`app/core/workflow.py`, `store.py`, `worker/tasks.py`)

### 6.1 State machine

`queued → running → (input_required | completed | failed | cancelled | cancelling)`. Every transition is a **compare-and-set** on `(state_version, lease_epoch)` and writes events/outbox in the same transaction.

### 6.2 Global lock ordering + fencing

Lock order is **thread, then run** everywhere (admission, cancel, finalize) to avoid deadlocks:

```python
async def _lock_run(self, conn, run_id, tenant_id=None):
    """Lock thread then run, the global lock order shared with admission and cancel."""
    ...
    await conn.execute("SELECT 1 FROM threads WHERE thread_id = $1 FOR UPDATE", thread_id)
    return await conn.fetchrow("SELECT * FROM runs WHERE run_id = $1 FOR UPDATE", run_id)
```

Any write by a worker re-validates the lease; a stale worker gets `LeaseLostError` and stops silently:

```python
async def _fenced(self, conn, lease):
    row = await self._lock_run(conn, lease.run_id)
    if (row is None or row["status"] != "running"
        or row["lease_epoch"] != lease.epoch or row["state_version"] != lease.version):
        raise LeaseLostError(lease.run_id)
```

*Standards*: fencing tokens (Kleppmann), optimistic concurrency, pessimistic lock ordering.

### 6.3 Transactional outbox + inbox

State, events and the dispatch intent commit atomically; a relay publishes with `FOR UPDATE SKIP LOCKED` (multiple relays can run); failures back off exponentially up to `OUTBOX_MAX_ATTEMPTS` then `dead`. Consumers dedupe with `consumer_inbox (consumer, tenant_id, message_id)`.

```python
rows = await conn.fetch("""
    SELECT message_id, tenant_id, run_id, destination, payload, attempts
    FROM outbox_messages WHERE state = 'pending' AND next_attempt_at <= now() ...
    ORDER BY created_at LIMIT $2 FOR UPDATE SKIP LOCKED""", ids, limit)
```

The `dedup_key` unique index (`orch:{run}:{version}:{action}`, `tool:{effect_key}:{epoch}`) makes enqueue idempotent (`ON CONFLICT (dedup_key) DO NOTHING`). `_after_commit` runs **post-commit side effects** collected in an `Effects` dataclass (wakeups, cancel flags, secret clearing, fast-path relay) — never inside the transaction.

### 6.4 Durable event log and SSE

`_append_events` increments `runs.event_sequence` and inserts gapless `run_events` rows; the table is append-only by trigger (except the retention-purge path). The SSE generator (`stream_run_events`) polls PostgreSQL after cursor, then blocks on a Redis stream **only as a wake-up**; a lost wake-up merely adds latency:

```python
entries = await store.rds.xread(streams, count=50, block=2000)
...
# A timeout re-polls PostgreSQL, so lost wakeups only add latency.
need_poll = not entries
```

Other details: `Last-Event-ID` integer cursor; `stream_gap` frame when the cursor is ahead; heartbeat comments every `SSE_HEARTBEAT_SEC`; periodic **re-authentication** (`SSE_REAUTH_SEC`) emitting `stream_closed` when a credential is revoked; stream stops at terminal event or at the *current* `input_required` but streams past old pauses when replaying.

*Standards*: W3C SSE (`id`, `Last-Event-ID`, `: keep-alive`), at-least-once delivery with dedup (Enterprise Integration Patterns), transactional outbox (microservices.io).

### 6.5 Budget: reserve → settle → uncertain

`begin_step` refuses a model call if `spent + reserved + estimate > max_cost`; the estimate comes from `MODEL_PRICING_JSON` using chars/3 ≈ tokens:

```python
prompt_tokens = (len(json.dumps(messages)) + len(json.dumps(tools))) / 3
usd = (prompt_tokens * price.get("input_per_mtok", 0)
       + settings.reservation_output_tokens * price.get("output_per_mtok", 0)) / 1_000_000
```

`usage_ledger` has `UNIQUE (tenant_id, source_attempt_key, unit)`; abandoned attempts flip to `uncertain` and **still count as spent** so a lost response never frees budget. `commit_step` additionally enforces `max_tool_invocations` and the wall-clock `deadline_at`. `GET /v1/runs/{id}/usage` exposes settled/uncertain/reserved/remaining.

### 6.6 Approvals (HITL)

`batch_digest` hashes run id, step and the exact `(id,name,arguments)` of every call:

```python
def batch_digest(run_id, step, tool_calls):
    return sha256_hex({"run_id": run_id, "step": step,
        "calls": [{"id": tc["id"], "name": tc["function"]["name"],
                   "arguments": tc["function"].get("arguments") or ""} for tc in tool_calls]})
```

Rules enforced in `decide_approval`: one pending batch per run (partial unique index), `409` on stale batch id/digest, expiry, run not `input_required`; production requires binding (`require_binding`); decision rows immutable; idempotency-key replay returns the same receipt; one concurrent decision wins (row lock). An approval pauses the **entire** batch from a model response (a documented limitation: not per-tool).

### 6.7 Cancellation

`_cancel_locked` increments `lease_epoch` (fencing the running worker), recursively cancels child runs, and finalizes immediately if no tools are in-flight, else moves to `cancelling` until outcomes arrive. A Redis flag `haas:cancelled:{run}` lets tool workers skip not-yet-started work. Documented limit: an external call already in flight can't be recalled.

### 6.8 Reconciler (`WorkflowMixin.reconcile`)

Repairs every crash boundary, safe to run concurrently: expired approvals → failed; deadlines → failed/cancelled; dispatched-but-silent tools → `unknown`; expired leases or stalled `queued` runs → new epoch + `run_recovering` + re-enqueue (bounded by `MAX_RECOVERY_ATTEMPTS`); orphan `reserved` ledger rows → `uncertain`; prune inbox/idempotency tables.

```python
"UPDATE runs SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL, "
"recovery_attempts = recovery_attempts + 1, "
"lease_epoch = lease_epoch + CASE WHEN status = 'running' THEN 1 ELSE 0 END, ..."
```

`maintenance_loop` runs it with the outbox relay and each area's `maintenance_<area>()` hook discovered via `getattr`. In dev the API hosts it; in prod a separate `maintenance` service (needs `haas_execution_service`).

### 6.9 Orchestrator tasks (`worker/tasks.py`)

`orchestrate_task` → `_orchestrate`: acquire lease → `aggregate`/`decision`/`step`. `_step` selects the model, builds tool schemas for active skills, calls the model with retries (`_call_model`, falling back to the primary model when a reasoning model rejects tool schemas with 400/404), builds invocations (`build_invocation`) and calls `commit_step` with an `outcome` dict. Drift between run-stamped and worker registry fingerprint is logged and flagged on `step_started`.

---

## 7. Core — execution, secrets, egress, redaction

### 7.1 Signed tool envelopes (`app/core/envelope.py`)

The orchestrator signs; tool workers only verify:

```python
def sign_envelope(body, config=settings):
    body = {**body, "v": ENVELOPE_VERSION}
    signature = _private_key(config).sign(canonical_json(body))
    return {"body": body, "signature": base64.b64encode(signature).decode()}
```

`verify_envelope` checks signature, expiry and recomputes `arguments_digest = sha256({"name", "arguments"})`. A broker writer can't forge, replay-after-expiry, or edit arguments. Canonical JSON (`sort_keys`, compact separators) avoids signature malleability. Dev derives a fixed key from a constant; production refuses it.

### 7.2 Exactly-once-ish effects

`effect_key = sha256({tenant, run, step, ordinal, call_id})` is stable across redeliveries. The tool worker claims it in Redis before running and caches results:

```python
if not await redis.set(f"haas:effect:{key}", body["invocation_id"], nx=True, ex=EFFECT_TTL_SEC):
    cached = await redis.get(f"haas:effect-result:{key}")
    if cached: return json.loads(cached)
    return "unknown", _outcome(..., UNKNOWN_OUTCOME, True)  # started earlier, no recorded result
```

Honest semantics: at-least-once transport, **effect-idempotent only if the destination honours the key**; ambiguous cases are surfaced as `unknown` (see §15).

### 7.3 Tool execution rules (`_async_execute_tool`)

Tool must exist **and** be unlocked by active skills; arguments pass through `repair_tool_json` then Pydantic; sync handlers run via `asyncio.to_thread` under `asyncio.wait_for` (so timeouts fire); output bounded by `_bound_observation` (head+tail, spill to scratch). Errors are classified: `ToolRemediationError`/`SecurityViolationError` → "Tool Error", others → "Runtime Error" (exception class only).

### 7.4 Secret vault (`secret_vault.py`)

Run secrets live in Redis as AES-256-GCM envelopes `v1:base64(nonce||ciphertext)`, AAD bound to the run id so a ciphertext copied to another run fails to decrypt:

```python
aad = f"haas:secrets:{run_id}".encode()
ciphertext = AESGCM(_key_bytes()).encrypt(nonce, json.dumps(secrets, separators=(",",":")).encode(), aad)
```

Scoped read returns only keys allowed by active skills.

### 7.5 Credential broker (`credentials.py`) — single-use grants

Designed to replace passing raw values through the broker: each secret is **sealed with a fresh AES key**; Redis holds the ciphertext + metadata (audience, plugin digest, invocation id, TTL); the task carries only an opaque ref `<grant_id>.<key>`; redemption uses `GETDEL` (atomic, single use) and checks audience/digest/invocation/expiry with `hmac.compare_digest` **before** decrypting; AAD binds grant, audience, digest, invocation.

```python
raw = await redis.getdel(GRANT_KEY_PREFIX + gid)
...
for reason, ok in checks:
    if not ok:
        await _note(redis, {**base, "outcome": "denied", "reason": reason})
        raise GrantError(reason, f"Credential grant rejected: {reason} mismatch.")
```

Redemption/denial events are queued in Redis and drained into `credential_grants` + audit by maintenance. *Status*: implemented and unit-ready; **the shown relay path (`make_outbox_publisher`) still uses `get_scoped_secrets`** — the broker is not yet wired in (see §15).

### 7.6 Egress broker (`egress.py`)

The "only sanctioned path to the network" for tool code:

1. scheme/userinfo/host allow-list per tenant policy;
2. **resolve DNS once**, every returned address must be public (loopback, link-local, ULA, NAT64/6to4/IPv4-mapped unwrapped and re-checked, cloud metadata networks);
3. connect to the **pinned IP**, keep `Host` header and TLS SNI to the original hostname (`sni_hostname` extension) → no DNS-rebinding window;
4. redirects followed manually, each hop revalidated, auth headers stripped on cross-origin;
5. response bytes metered against a per-run cap (Redis counter) and queued to `usage_ledger`.

```python
hop_headers["Host"] = target.host_header
if target.scheme == "https":
    hop_ext["sni_hostname"] = target.host
request = http.build_request(current_method, target.connect_url, headers=hop_headers, extensions=hop_ext, ...)
```

The simpler `validate_safe_outbound_url` (`context.py`) is what current tools call; the threat model explicitly notes preflight DNS ≠ connection binding.

### 7.7 Redaction (`redaction.py`)

Recursive masking of exact live secret values (registered per run, longest-first so overlaps mask fully), `haas_canary_*` tokens (red-team canaries), bearer/basic creds, `sk-…` keys and `key=value` pairs. `observability.JsonRedactingFormatter` applies the same idea to logs and adds OpenTelemetry `trace_id/span_id`.

### 7.8 Sandbox protocol (`app/sandbox/protocol.py`)

`haas.sandbox/1`: one JSON line in (secrets travel **in the request line, never argv/env**), one JSON line out, strict decoding and error-code enum, size limits. Settings define CPU/memory/file/process/network limits. This is the contract for per-invocation subprocess isolation; the runner itself is outside the provided files (see §15).

### 7.9 Outbound webhooks (current)

`deliver_webhook_task` validates the URL with DNS, disables redirects, retries with exponential countdown. Body schema `run-terminal-webhook.v1` is created inside `_finalize`. The signed, persisted delivery pipeline (tables from migration 0015, `haas_sdk/webhooks.py` verifier) is the target replacement.

---

## 8. Core — LLM gateway, streaming and OpenAI facade

### 8.1 Model routing (`llm.py`)

Architect/Editor pattern (borrowed from Aider/OpenCode, per the v2 RFC): planner on step 1 or after an error, cheaper primary model otherwise.

```python
if step == 1 or last_step_had_error:
    return planner_model
return primary_model
```

### 8.2 Self-healing tool JSON

Open-weight models emit fenced JSON or trailing commas; `repair_tool_json` strips fences, removes trailing commas, fails with a *remediable* error instead of burning a model turn.

### 8.3 Assistant-message sanitation

`sanitize_assistant_message` keeps only `role/content/tool_calls` (drops `reasoning`, `refusal`) so echoing the message back never breaks a provider.

### 8.4 Streaming accumulator and live deltas

`_StreamAccumulator` reassembles OpenAI-style chunks including **fragmented tool-call arguments** keyed by `index`. When `on_delta` is set the gateway streams; `DeltaPublisher` batches fragments (`flush_chars` / `flush_ms`), caps characters per step and writes to a bounded Redis stream; failures set `failed=True` and never break the model call ("advisory channel"). The SSE endpoint forwards deltas only for the **current (step, attempt)** and they have **no SSE id**, so replay and the durable cursor are unaffected; `run_completed.output` always supersedes the preview.

### 8.5 OpenAI-compatible facade (`openai_compat.py`, `/v1/chat/completions`, `/v1/responses`, `/v1/models`)

Maps requests onto the same admission path as `/v1/runs` (policy, budgets, approvals, idempotency identical — SEC-24). Design choices:

- Model id grammar: `haas`, `agent/<name>[@version]`, revision UUID or a provider model id.
- Client `system`/`developer` turns are folded into the **user prompt as untrusted context** (transcript preamble) and never replace the harness system prompt.
- Client-side `tools`, `functions`, `n>1`, structured response formats are **rejected explicitly** instead of ignored.
- Non-completed outcomes map to OpenAI-shaped errors with `x-should-retry: false` so SDKs don't resubmit a settled run (`409 approval_required`, `422 budget.*`, `504 run_timeout`).
- Responses API: response id = run id; `previous_response_id` continues the thread; approval pause → `status: incomplete`, `incomplete_details.reason = approval_required`.
- Streaming is **post-hoc chunking** of the finished result with keep-alives (not true token streaming).

```python
def fold_prompt(turns, *, param):
    if not turns or turns[-1][0] != "user" or not turns[-1][1].strip():
        raise facade_error(400, "The final message must be a non-empty user message.", param=param)
```

---

## 9. Core — advanced tracks

> Track status matters here: schemas and domain libraries are substantial; several `app/api/*` routers are early scaffolding (see §15).

### 9.1 Artifacts and retention (migration 0014, `retention.py`, `object_store.py`)

- Retention classes (`minimal/short/standard/extended`); tenants may only **shorten** TTLs (`validate_overrides`).
- Legal holds (single release, immutable), erasure requests with **signed receipts**: HMAC over the manifest's canonical-JSON SHA-256 (`sign_manifest` / `verify_receipt`), tombstones append-only.
- Purges go through one privileged path: a trigger permits `DELETE` on `run_events`/`artifacts` only inside a transaction that set `haas.retention_purge='on'` and only for `haas_execution_service`.
- `object_store.py`: content-addressed tenant-prefixed keys (`tenants/<uuid>/sha256/aa/<digest>`), key validation + traversal refusal, atomic temp-file+fsync+rename, and a dependency-free **AWS SigV4** signer over httpx for S3/MinIO.

```python
def content_key(tenant_uuid, tenant_id, sha256_hex):
    return validate_key(f"{tenant_prefix(tenant_uuid, tenant_id)}/sha256/{sha256_hex[:2]}/{sha256_hex}")
```

### 9.2 Marketplace (migration 0016, `app/marketplace/*`)

Domain library (metadata-only, never imports plugin code):

- **Manifest v2** (`manifest.py`): strict Pydantic (`extra="forbid"`), namespaced ids `@publisher/name`, permissions (exact https egress origins, secret names, workspace paths, sandbox profile), per-tool `risk` classes (`read|write|destructive|financial|exfiltration-capable`), remote server pinning with **tool-schema digests (rug-pull defence)**. `upgrade_v1` converts operator packs with least-privilege defaults. `permission_diff` answers "does this update **expand** permissions → re-consent required?".
- **Signing** (`signing.py`): Ed25519 signature over canonical JSON of `{artifact_digest, manifest_digest, provenance_digest}`; in-toto-style provenance validated against the artifact digest.
- **Transparency log** (`tlog.py`, plus trigger in migration 0016): RFC 6962 Merkle tree (leaf `0x00`, node `0x01`), inclusion proofs verified per RFC 9162, plus a linear hash chain computed by DB trigger so rewriting history is detectable even without recomputing the tree.

```python
def leaf_hash(entry: bytes) -> bytes:  return hashlib.sha256(b"\x00" + entry).digest()
def node_hash(l: bytes, r: bytes) -> bytes: return hashlib.sha256(b"\x01" + l + r).digest()
```

- **Automated review** (`review.py`): AST scan for forbidden imports/calls (`subprocess`, `eval`, `ctypes`), network imports without declared egress, undeclared secret reads, URL literals vs declared egress, embedded credential regexes (AWS/GitHub/OpenAI-style/private keys/canaries), prompt-injection lint over tool descriptions and `.md`, native binary rejection. `decide()` maps report + tier to `approved|submitted|rejected`.
- **Deterministic packaging** (`packaging.py`): sorted tar, fixed mode/mtime, gzip `mtime=0` → byte-reproducible digest; safe reader (no symlinks, no `..`, size/file limits).
- **Adapters** (`adapters.py`): import MCP `tools/list`, OpenAI function JSON, and `SKILL.md` into a v2 draft without guessing permissions upward.
- **Semver** (`semver.py`): own SemVer 2.0 ordering and npm-style ranges (`^ ~ x`, comparators).
- **Trust tiers** (`tiers.py`) and consent material whose digest the admin echoes at install.
- `haas_marketplace_fanout` (SECURITY DEFINER) fans out yank/takedown notifications across tenants and optionally revokes installs.

### 9.3 Multi-agent (HACP) (migrations 0011, 0017; `docs/design/multi-agent.md`)

Sub-agents are **child runs** forming a tree. The best-engineered part is the schema:

- `sync_haas_run_lineage` derives `root_run_id`/`depth` from the parent on insert and freezes them afterwards; depth ≤ 32.
- `delegations` terms immutable; only `status/settled_at/budget_returned_usd` may change.
- `agent_messages` can only flow inside one tree (`haas_run_root`), consumed once.
- **Workspace CAS in a trigger**: new `version` must equal `max(version)+1` or the insert fails with `serialization_failure`:

```sql
IF NEW.version <> COALESCE(current_version, 0) + 1 THEN
    RAISE EXCEPTION 'workspace CAS conflict on %: expected version %, got %' ...
    USING ERRCODE = 'serialization_failure';
```

- `_cancel_locked` already cascades to `parent_run_id` children.
- Target extras (docs): attenuable biscuit/macaroon-style delegation tokens, topologies (`supervisor, swarm, graph, map_reduce, debate`), A2A and MCP-server interop.

### 9.4 Operations (migration 0015)

DB-resident control logic using `SECURITY DEFINER` functions:

- `haas_ops_run_admission()` — a **BEFORE INSERT trigger on `runs`** taking an advisory lock per tenant, rejecting inserts while the tenant is `moving` between cells (`HA503`) or over its concurrent-run cap (`HA429`).
- `haas_ops_outbox_fairness()` — delays orchestrator outbox rows of tenants over their in-flight cap using a weight: `now() + least(30, 0.5 * excess / weight)` → **weighted fair scheduling in SQL**.
- `restore_epoch` — after PITR, `haas_ops_bump_restore_epoch` fences all running runs (`lease_epoch+1`, lease expired) and resets in-flight webhook deliveries; epoch stamped on new rows. Documented in `docs/operations/backup-dr.md`.
- Cells and placement: `haas_ops_begin_move/finish_move` with epoch compare-and-swap; metrics function `haas_ops_metrics()` for cross-tenant counts without breaking RLS.

### 9.5 Isolation API, access API (`app/api/isolation.py`, `access.py`)

Catalog/pack/materialize endpoints over `plugin_artifacts`; secret-binding CRUD (inline/env providers), grant records, egress policy and redaction test endpoints. Domain logic they front is in `credentials.py`, `egress.py`, `redaction.py`.

---

## 10. TUI and CLI (`haas_cli`)

### 10.1 Layering

```
haas_cli/client.py   HaasClient: async httpx wrapper (one method per endpoint) + SSE reconnect
haas_cli/session.py  ChatSession: run lifecycle driver, approvals binding, slash commands (framework-free)
haas_cli/config.py   ChatSettings from env (HAAS_URL, HAAS_TENANT, HAAS_API_KEY, HAAS_SECRET_*)
haas_cli/profiles.py ProfileStore: non-secret endpoint profiles (0600 file, atomic write)
haas_cli/completion.py pure-function command completion
haas_cli/tui/app.py  Textual UI (workspaces, modals)
haas_cli/tui/safety.py terminal-injection sanitiser
haas_cli/chat.py / cli.py / plugin.py  entry points
```

Deliberate separation: **session and client have no UI imports**, so the same code powers `--once` scripting mode and Textual pilot tests.

### 10.2 Client SSE with durable resume (`client.py`)

```python
cursor = last_event_id if STREAM_ID_RE.fullmatch(last_event_id) else "0"
...
headers={**self._headers, "Last-Event-ID": cursor}
...
if ev.event == "stream_closed":
    self.auth_expired = True
    raise HaasAPIError(f"Event stream closed by the server: {reason ...}", 401)
...
if ev.event in TERMINAL_EVENTS or ev.event == "input_required": return
```

Reconnects up to `max_reconnects`; when the server closes cleanly with no terminal event it asks `GET /runs/{id}` whether the run is still active. A 401 clears the in-memory credential and sets `auth_expired`.

### 10.3 Run driver (`session.py::_follow_run`)

A loop: stream events → on `input_required` record `ApprovalBinding(batch_id, batch_digest, expires_at)` → call `on_approval(calls)` → `client.resume(... batch_id, batch_digest ...)`; a `409` (decided elsewhere / stale) is swallowed because the durable log tells the truth on re-follow. Cost is deduplicated per run (`_costed_runs`). If a stream ends without a final event it falls back to `GET /v1/runs/{id}` and synthesises the terminal event so UIs render consistently.

### 10.4 Textual workbench (`tui/app.py`)

Workspaces switched via `ContentSwitcher`: **Chat** (Markdown answers, tool cards, live preview), **Runs** (DataTable, cursor paging, attach/inspect/cancel), **Approvals** (inbox with status filter), **Admin** (role-gated memberships/API keys/policy/audit). Command palette (`NavigationCommands(Provider)` with discovery + fuzzy search) is the discoverable path next to slash commands.

Distinctive implementations:

- **`PromptInput(TextArea)`** — Enter sends, Ctrl+J newline, Tab completes without leaving focus, Up/Down navigates completion vs history depending on context, Enter completes a partial slash command instead of sending.
- **`ApprovalScreen`** — modal showing batch id, digest prefix, expiry, per-call JSON (syntax-highlighted, **sanitised**), disables Approve for oversize arguments (`MAX_APPROVAL_ARGUMENTS`) and when the identity lacks the `approver` role or the run was just submitted by the same session (`decisions_enabled`). `Esc` = leave pending (never reject).
- **Live preview** — `Markdown.get_stream` for incremental rendering; new `(step, attempt, call)` key resets the buffer; settles (dimmed) when the next durable event arrives and is discarded on retry/`run_completed`.
- **Bounded transcript** — `MAX_CHAT_WIDGETS` trims old widgets (server keeps history).
- **Capability-driven UI** — `_has_action` reads `capabilities.actions` from `/v1/me`, falls back to legacy roles for older servers; `_validate_identity` rejects incompatible event schemas.
- **Safety** (`safety.py`) strips ANSI/OSC/control chars before anything is rendered:

```python
_ANSI_SEQUENCE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
```

- Secrets: `/secret set` values stay in memory, masked on list, `PromptInput.remember` skips `/secret` lines, `_run_command` shows `/secret …`.

### 10.5 Slash commands (single table in `session.py::COMMAND_HELP`)

`/agent draft|show|validate|publish|use|diff|clear`, `/agents`, `/threads`, `/history`, `/branch`, `/approvals`, `/approve`, `/inspect`, `/events`, `/status`, `/live`, `/skills`, `/tools`, `/preload`, `/secret`, `/model`, `/planner`, `/steps`, `/budget`, `/thread`, `/login`, `/logout`, `/tenant`, `/profile`, `/admin`, `/new`, `/clear`, `/quit`. Help, completion and palette derive from the same list (single source of truth).

`/agent diff` uses `difflib.unified_diff` over canonical JSON of configuration + policy rules.

### 10.6 Unified `haas` CLI (`cli.py`)

Subcommands: `chat`, `profile`, `admin`, `runs`, `agents`, `approvals`, `webhooks`, `dev`. `haas dev up|down|status|seed|smoke|doctor` composes `docker-compose.yml + dev + e2e` into an isolated project named by `sha256(root)[:10]`, mock model and billing, loopback ports, and a seed (`app/seed_dev.py`) that creates a tenant, admin/submitter and **separate approver** with hashed expiring keys (printed once, never persisted) — precisely to demonstrate the no-self-approval rule.

*Observed*: `main()` repeats several dispatch branches (profile/admin/dev checked twice) — harmless dead code.

### 10.7 Profiles

`ProfileStore` stores **only** endpoint metadata (name, URL, tenant, env), validates URL (no credentials/paths), writes atomically (`mkstemp` + `os.replace`), directory 0700 / file 0600. Production profiles show a "PRODUCTION" reverse-video banner in the status bar.

---

## 11. SDKs

### 11.1 Python SDK (`haas_sdk`) — sync `Haas` and async `AsyncHaas`

Architecture: **spec/transport split**. `_specs.py` builds `Op(call, parse)` objects (what to send, how to parse); `client.py`/`async_client.py` only perform I/O. This halves duplicated logic.

```python
@dataclass
class Op(Generic[T]):
    call: Call
    parse: Callable[[httpx.Response], T]
```

Features:

- **Retry policy** (`_core.retry_delay`): retries only idempotent calls (GET/DELETE or keyed by `Idempotency-Key`), or connection refusals for anything; honours `Retry-After` (capped 60 s); full-jitter exponential backoff.
- **Auto idempotency**: `idempotency_headers` generates `sdk-<uuid>` once per logical call so retries reuse it.
- **Exactly-once event iteration**: `stream()` reconnects with `Last-Event-ID` and drops frames with `seq <= last_seq`.

```python
seq = event.sequence
if seq is not None:
    if last_seq is not None and seq <= last_seq: continue
    last_seq, last_id, failures = seq, event.id, 0
yield event
```

- **Typed models**: frozen dataclasses (`Run`, `RunEvent`, `ApprovalBatch`, `Thread`, `AgentRevision`, `Artifact`, `Page[T]`) keep the full `raw` JSON so additive server fields remain reachable ("/v1 is additive").
- **Pagination**: `_paginate` generator over cursors; `runs.iter()`, `approvals.iter()` etc.
- **Digest-safe approvals**: `Approvals.decide` fetches the current digest if you don't pass the reviewed one (docstring warns to pass your reviewed digest).
- **Typed errors**: status → class mapping (`NotFoundError`, `ConflictError`, `RateLimitError`…), request id from body or `x-request-id`.
- **Trace propagation**: injects OpenTelemetry `traceparent` when `opentelemetry` is installed.
- **Webhooks** (`webhooks.py`): Stripe-style `HaaS-Signature: t=<ts>,v1=<hex>` = HMAC-SHA256(secret, `"{t}."+body`); supports **multiple `v1`** values and multiple secrets for rotation; evaluates all candidates to avoid timing leaks; 300 s tolerance.

```python
for secret in _secrets(secrets):
    expected = compute_signature(payload, secret, timestamp)
    matched = False
    for candidate in signatures:
        matched |= hmac.compare_digest(expected, candidate)
```

- **Resolve order**: explicit args → `HAAS_URL/HAAS_API_KEY|HAAS_BEARER_TOKEN/HAAS_TENANT`.

### 11.2 TypeScript SDK (`haas_sdk_ts`)

A fetch-based `HaaSClient` with the full endpoint surface (runs, approvals, threads, agents, artifacts, webhooks, quotas, retention, secrets, egress, marketplace, agent mesh, ops) and a large `types.ts`. Retries with exponential backoff, `Retry-After` annotation, `401` clears the key, `createClientFromEnv`. Strict `tsconfig` (NodeNext, `strict`, declarations).

*Maturity note*: the TS client is a **first draft**: see §15 (SSE parsing is not wired to the generator, several request/response shapes don't match the server, `retries` apply to non-idempotent POSTs).

### 11.3 Plugin SDK (`haas_plugin_sdk`)

Documented in §5.7: `@tool`, `ToolContext` protocol, `ToolError`, `ABI_VERSION="haas.sandbox/1"`.

---

## 12. Tooling

- **Migrations** (`migrations/versions/0001…0017`): hand-written SQL in Alembic with *expand → backfill → contract* for Phase 2 (`phase2_migration.py`: advisory-locked bounded batches with `SKIP LOCKED`, tenant-mapping with admin verification + audit, quarantine of orphans, cutover that validates `NOT VALID` constraints and flips columns to `NUMERIC`/`NOT NULL`). Downgrades **refuse** when data would be lost (e.g. non-terminal runs, agent revisions, mappings). `env.py` takes a global advisory lock so concurrent migrators serialise.
- **Dev stack**: base Compose publishes no DB/Redis ports; `docker-compose.dev.yml` binds loopback and adds Flower; `docker-compose.prod.yml` activates fail-closed settings and the maintenance service; tool worker is `read_only`, `cap_drop: ALL`, `no-new-privileges`, tmpfs `/tmp`.
- **Load test** (`loadtest/haas_load.py`): asyncio virtual users, weighted multi-tenant skew, SSE vs polling modes, records per-request latency and per-run first-event / end-to-end.
- **SLO report** (`scripts/slo_report.py`): nearest-rank percentiles, availability excluding 401/403/429 from the denominator, checks flagged `insufficient_data` below `min_samples`, exit codes 0/1/2.
- **OpenAPI snapshot** script, deprecation helpers (`deprecation.py`, RFC 9745 `Deprecation` + RFC 8594 `Sunset`, minimum 180-day sunset enforced in `__post_init__`).
- **Observability**: FastAPI/HTTPX/Celery OpenTelemetry instrumentation, JSON logs with trace ids and redaction.

---

## 13. Industry-standard alignment

| Feature | Standard / pattern | Where |
|---|---|---|
| Async work API (202 + status + stream) | HTTP async request-reply | `POST /v1/runs` |
| Idempotency | IETF `Idempotency-Key` draft | `create_run`, `decide_approval`, SDK |
| Errors | RFC 9457 problem+json | `core/http.py` |
| Pagination | Opaque keyset cursors | `_keyset_page`, SDK `Page` |
| Streaming | W3C SSE, `Last-Event-ID`, heartbeats | `stream_run_events` |
| Delivery semantics | Transactional outbox, inbox dedup (EIP) | `workflow.py` |
| Concurrency | Fencing tokens, optimistic/pessimistic locking | `lease_epoch`, `state_version` |
| AuthN | OIDC/JWT RS256, JWKS rotation, API keys (hashed) | `identity.py` |
| AuthZ | RBAC, deny-by-default, separation of duties | `authorization.py` |
| Tenancy | Row-level security + composite FKs | migrations 0008+ |
| Secrets | AES-256-GCM + AAD binding; single-use grants | `secret_vault`, `credentials` |
| Signing | Ed25519 envelopes; in-toto provenance; Merkle log (RFC 6962/9162) | `envelope`, `marketplace/*` |
| Supply chain | OCI layout, content addressing, SBOM field, reproducible tar | `plugin_artifacts`, `packaging` |
| SSRF defence | DNS pinning, IP classification, redirect revalidation | `egress.py` |
| Agent skills | agentskills.io SKILL.md frontmatter + progressive disclosure | `plugins.py` |
| MCP | Streamable HTTP JSON-RPC `tools/call` | `register_external_manifest` |
| OpenAI compat | Chat Completions & Responses API shapes | `openai_compat.py` |
| Webhooks | Stripe-style timestamped HMAC with rotation | `haas_sdk/webhooks.py` |
| Observability | OpenTelemetry, W3C `traceparent` | `observability.py`, SDK |
| Resilience | Retry w/ full jitter, `Retry-After` | `_core.py` |
| Cost control | Reserve/settle ledger | `usage_ledger` |
| Retention | Legal hold, signed erasure receipts, tombstones | `retention.py` |
| DR | PITR + restore-epoch fencing | `backup-dr.md`, migration 0015 |
| Deprecation | RFC 9745 / 8594 headers | `deprecation.py` |
| 12-factor | Env-driven config, fail-closed prod profile | `config.py` |

---

## 14. Clever tricks (catalogue)

1. **Identity header laundering**: strip spoofable headers, then rewrite `X-Tenant-ID` to canonical slug (`IdentityMiddleware`).
2. **Transaction-local tenant GUC** (`set_config(..., true)`) so pooled connections can't carry tenants over.
3. **Startup RLS self-audit** composed from per-area `RLS_TABLES` tuples.
4. **Policy digest recomputed on every load** to detect DB tampering of "immutable" JSON.
5. **Plugin rollback snapshots** and `provides` verification.
6. **Sorted tool schemas** for stable prompt prefix / KV-cache hits.
7. **Hidden = unknown** in `activate_skill` (no inventory leak).
8. **Reserve-then-settle ledger with `uncertain`** counted as spent.
9. **Lock ordering (thread→run)** shared by all writers.
10. **Wake-ups, not data, over Redis** — SSE correctness lives in PostgreSQL.
11. **Dedup keys encode version+action** (`orch:{run}:{version}:{action}`), making the reconciler idempotent.
12. **`unknown` outcome status** instead of silent retry for ambiguous effects.
13. **AES-GCM AAD bound to run id**; **grant AAD bound to audience+digest+invocation**.
14. **GETDEL** for atomic single-use credentials.
15. **DNS pin + SNI** to close the rebinding window.
16. **Canary tokens** in redaction for red-team leak tests.
17. **Triggers as policy engines**: admission quota/move gate, outbox fairness, CAS versioning for workspaces, hash-chained log.
18. **Retention purge guarded by a transaction GUC** and role grant.
19. **Deterministic tar + gzip(mtime 0)** → digest equality across machines.
20. **SQLite authorizer + progress handler** for safe analyst SQL.
21. **Union-find** in `haas-plugin migrate` to merge skills sharing tool files.
22. **Spec/transport split** in the Python SDK; `raw` payload kept in every model.
23. **Compare-all signature candidates** to avoid timing oracles in webhook verification.
24. **TUI treats `Esc` as detach/leave-pending**, never as reject; Approve disabled for oversize or non-approver contexts.
25. **Facade folds client system prompts into untrusted context** and sets `x-should-retry: false` on settled-run errors.
26. **`doctor` AST-checks secrets** read by code vs declared `allowed_secrets`.

---

## 15. Limitations and observed gaps (honest audit)

These come from reading the actual code; some are acknowledged by the docs, others are not.

### 15.1 Security / architecture (acknowledged in docs)

- Plugins are **imported into API and orchestrator processes** in the shown code (`discover_all`), i.e. trusted operator code only. `sandbox_mode`/static-catalog settings and `haas.sandbox/1` protocol exist but the per-invocation runner is not in the provided files and `tasks.py` still executes tools in-process.
- `credentials.prepare_tool_dispatch` and `egress.egress_request` are implemented but **not wired** into `make_outbox_publisher` / tool execution; tools use `ctx.get_secret` + `validate_safe_outbound_url`. Raw scoped secret values still ride the Celery broker.
- Approvals are **batch-level**: one gated tool pauses all calls from that response.
- Destination idempotency is the destination's job; otherwise `unknown`.
- Cancellation cannot stop an in-flight external call.
- MCP adapter is not a full client; HMAC signs sorted JSON, not raw body, and has no timestamp/nonce.
- Single Redis instance hosts broker, results, secrets, wake-ups (not independent fault/security domains).
- Callback webhooks are unsigned today (`deliver_webhook_task` posts plain JSON).

### 15.2 Code-level issues found in the feature routers (`app/api/*`)

| Area | Finding |
|---|---|
| Tenant selection | Many handlers use `(SELECT tenant_id FROM tenants WHERE status = 'active' LIMIT 1)` instead of the authenticated tenant. Under forced RLS for `haas_tenant_app` this returns only the caller's tenant, so it works by accident; with a role that sees all tenants (dev/owner) it picks an arbitrary one. |
| AuthZ | These routers carry **no `require_action` dependencies** (unlike `main.py`); only authentication middleware applies. `Action.ARTIFACT_*`, `WEBHOOK_ADMIN`, `PLATFORM_OPERATE` etc. are defined but not enforced there. |
| Actor attribution | `created_by`/`updated_by`/`placed_by` use the literal `"current_user"` against UUID columns → inserts would fail at runtime. |
| Artifacts | `create_artifact` mixes a JSON body model with `UploadFile` (FastAPI can't bind both); stores to a local path rather than `object_store`; ignores quota/retention/legal-hold logic in `retention.py`. |
| Multi-agent API | `run_tree` casts `run_id::uuid` (ids are `run_xxxx`), inserts child runs with `status='pending'` (violates the status CHECK), and writes to `workspaces(run_id, agent_id, key…)` which doesn't match migration 0011's schema (`workspace_id`, `root_run_id`, versioned entries). Delegation does not use the attenuation/budget triggers' contract. |
| Marketplace API | `create_revision` doesn't call `run_review`, signature verification, or append to the transparency log (it computes `tlog_seq` from `max(seq)+1` without inserting the entry, violating the FK). The rich logic in `app/marketplace/*` is therefore not exercised by the HTTP layer. |
| Operations API | `begin_tenant_move` result handling, `set_quota` returning a composite via `SELECT function(...)` mapped to `TenantQuotaResponse(**dict(row))` would not match column names. |
| Isolation API | Hard-coded default `PLUGINS_ROOT` path (`/home/voldemort/work/harness/plugins`), sandbox `execute` is an explicit stub. |
| Area maintenance jobs | e.g. `maintenance_agent_mesh` marks any `active` delegation older than 1 hour as `failed` (would trip the settled-delegation immutability trigger semantics); several jobs are counters/placeholders. |

### 15.3 SDK / client gaps

- **Python SDK ↔ server drift**: `artifact_list` calls `/v1/runs/{id}/artifacts`, `artifact_content` calls `/v1/artifacts/{id}/content`, `artifact_upload` posts to `/v1/runs/{id}/artifacts` — the server exposes `/v1/artifacts`, `/v1/artifacts/{id}/download`. `Run.from_dict` expects `run_id` (list endpoint returns it; OK) but `result` key differs from server's `result`/`result_text` projection in some views.
- **TypeScript SDK**: `streamEvents` builds an `EventSourceParser` with an empty callback and never yields (the `parseSSEEvents` helper is a stub), the `eventsource-parser` import shape doesn't match v1's `createParser`; `createArtifact` posts `FormData` with `Content-Type: application/json` default header; automatic retries apply to POSTs without checking idempotency; many types (`MeResponse`, `ApprovalInfo`, `Run*`) don't match server payloads (e.g. `/v1/me` returns `roles`, `capabilities` object, no `issuer`).
- **CLI**: duplicated dispatch branches; `_dev_smoke` posts with `X-Tenant-ID: default-tenant` and no credentials while the dev stack seeds `local-dev`; `_run_runs` for `stream` returns `None` after printing.
- **TUI**: Admin buttons call endpoints documented in `tui-experience.md` under different paths than the server (`/v1/admin/audit` vs `/v1/audit-events`) — the **code** uses the correct server paths; docs are stale.
- **Docs drift**: README still lists `BRAVE_API_KEY` for `web-research` though the plugin now uses SearXNG; `integrations-usage.md` says artifact/webhook administration endpoints "do not exist yet" while `app/api` has them (scaffolded).

### 15.4 Scale / operational limits

- Reconciler scans with `LIMIT 100` loops; at high run counts it needs indexing review and sharding.
- `begin_step` estimates tokens as `chars/3`; unpriced models reserve a flat default (`0.001`), so overshoot is bounded only post-hoc.
- Keyset pagination with in-API filters has a scan budget (`_LIST_SCAN_BUDGET`) and approvals inbox uses a widening-window workaround because the store query has no keyset parameter.
- SSE connections are not quota-limited yet (`sse_connections` column exists in `tenant_quotas`, enforcement not shown).
- Output-delta previews are per-process Redis streams; no back-pressure beyond buffer cap.

---

## 16. Adoption path and improvements

### 16.1 Suggested adoption stages

1. **Internal / trusted pilot** — Compose + dev profile, vetted plugins only (`weather`, `web-research`, `incident-triage`, `data-analyst`). Use the TUI; treat X-Tenant-ID as local selector only.
2. **Authenticated single organisation** — enable OIDC + tenant policy, run Phase-2 cutover runbook, deploy the maintenance service, rotate envelope keys, use `haas dev smoke`-style checks in CI.
3. **Approval-gated write workflows** — `github-ops`, `revenue-ops` style tools; distinct approver principals; set `MODEL_PRICING_JSON`; adopt `Idempotency-Key` in clients; wire destination services to honour `X-HaaS-Effect-Key`.
4. **Multi-tenant SaaS** — only after sandbox runner + credential broker + egress broker are wired, signed callbacks shipped, quotas enforced, and the SEC acceptance matrix passes.
5. **Ecosystem** — marketplace (signed artifacts, review, tlog) and multi-agent runs after the API layer is rebuilt on the existing schema.

### 16.2 Highest-value improvements

| Priority | Improvement | Why |
|---|---|---|
| P0 | Wire the **sandbox runner**, **credential broker**, **egress broker** into `tasks.py` | Closes the biggest trust gaps; code already exists |
| P0 | Rebuild `app/api/*` routers on the authenticated tenant + `require_action` + `_insert_audit_event`; remove `LIMIT 1` tenant subqueries and `"current_user"` literals | Real authz/attribution |
| P0 | Make marketplace publish call `run_review`, `signing.verify`, `tlog` append transactionally | Delivers advertised supply-chain guarantees |
| P1 | Align SDK paths/types with the OpenAPI snapshot; generate SDKs from `docs/openapi-v1.json` and add contract tests | Eliminates drift |
| P1 | Finish TS SSE with `eventsource-parser`'s `createParser` + reconnect, idempotency-aware retries | Usable TS client |
| P1 | Signed, persisted webhook delivery (tables exist) with SDK verifier | Replace unsigned callback |
| P1 | Per-tool (not per-batch) approvals; approval diff views | UX + least interruption |
| P2 | Real token streaming through the facade (reuse `DeltaPublisher`) | OpenAI-SDK parity |
| P2 | Per-tenant SSE/connection quotas; fair scheduling dashboards from `haas_ops_metrics` | Noisy-neighbour control |
| P2 | Separate Redis roles (broker / secrets / wake-ups) with ACLs | Fault and security domains |
| P2 | DPoP/`nbf`, multiple JWKS issuers, SCIM-style membership sync | Enterprise IdP fit |
| P3 | Full MCP client (initialize, sessions, OAuth) and A2A server | Interop |
| P3 | Optional Temporal/workflow-engine backend behind the same lease/outbox contracts (ADR-09) | If reconciler complexity grows |

### 16.3 Verification checklist for adopters

- `haas-plugin doctor` clean; `registry.fingerprint` identical across API and workers.
- Phase-2 `report` shows zero unresolved rows; startup RLS self-check passes.
- Crash-matrix test (kill worker at each boundary) leaves no stuck `running`/duplicated effects.
- SLO report (`scripts.slo_report`) passes under skewed multi-tenant load.
- Canary secrets (`haas_canary_*`) never appear in events, logs, webhook bodies or TUI.

---

*End of document.*
