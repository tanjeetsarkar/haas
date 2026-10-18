# Integration patterns and usage

[Design index](README.md) | [SDD](sdd.md) | [DFD](data-flows.md) | [Plugin reference](../plugin_architecture.md)

## 1. Current API contract

All run operations use `X-Tenant-ID`, default `default-tenant`. This is **not
authentication**. These examples are for trusted local development or an API behind a
correctly configured authenticating gateway. Do not expose this interface directly
to mutually untrusted customers.

| Method / path | Request | Success | Important behavior |
|---|---|---|---|
| `GET /livez` | None | 200 `{"status":"ok"}` | Process liveness only |
| `GET /readyz` | None | 200/503 with dependency checks | Checks PostgreSQL and Redis; not provider, queue or worker health |
| `GET /healthz` | None | 200/503 with dependency checks | Backward-compatible alias for `/readyz` |
| `GET /v1/runs?limit=20&cursor=…` | Tenant | 200 `items` + opaque `next_cursor` | Descending keyset pagination, tenant scoped |
| `POST /v1/runs/{run_id}/cancel` | Tenant | 202 `cancelled` or `cancelling` / 409 terminal | Fenced; `cancelling` until outstanding tool outcomes are recorded; in-flight external side effects may not be interruptible |
| `POST /v1/runs` | Create body below; optional `Idempotency-Key` | 202 run/thread/`queued`/stream/events URLs | Admission, run, first event and dispatch outbox row commit atomically; same key + same body replays (`Idempotent-Replayed: true`), different body → 409 |
| `GET /v1/runs/{id}` | Tenant header | 200 run projection | Foreign/absent run → 404; no full conversation returned |
| `GET /v1/runs/{id}/stream` | Tenant header; optional integer `Last-Event-ID` | 200 `text/event-stream` | Durable replay; heartbeats; periodic bearer re-authentication (see §3) |
| `GET /v1/runs/{id}/events` · `/steps` · `/usage` | Tenant header | 200 | Paged event log; step attempts + tool invocations; budget + usage ledger |
| `POST /v1/runs/{id}/resume` | Resume body below; optional `Idempotency-Key` | 202 action receipt | 404 ownership; 409 if not paused, batch/digest stale or another decision won |
| `GET /v1/approvals?status=pending\|approved\|rejected\|expired\|cancelled\|all` · `GET /v1/approvals/{batch_id}` | Tenant header | 200 | Tenant approval inbox with exact calls, digest, expiry and decision history |
| `POST /v1/approvals/{batch_id}/decision` | `{"decision":"approve\|reject","batch_digest":…,"reason"?,"secrets"?}` | 202 | Digest-bound, immutable decision; requires `run:approve` and separation of duties |
| `GET /v1/plugins` | Tenant header | 200 registry projection | Skills filtered by tenant; plugin/tool metadata is global |

Tenant/member/agent administration, threads and audit APIs are documented in the
[developer platform design](developer-platform.md) and the OpenAPI snapshot. Artifact
and webhook-delivery administration endpoints do not exist yet.

### Create body

| Field | Type / default | Validation / interpretation |
|---|---|---|
| `prompt` | Required string | Minimum length 1; no explicit maximum length |
| `thread_id` | Optional string / null | Omitted creates new thread; another tenant's ID gives 404 |
| `preload_skills` | String array / `[]` | Must exist, be ready and be permitted |
| `skills` | String array / null | Can narrow tenant allowlist only; `[]` permits no domain skills |
| `primary_model` | String / null | Defaults from server settings; no tenant model allowlist today |
| `planner_model` | String / null | Defaults from server settings |
| `max_steps` | Integer / 15 by default | 1-50 |
| `max_cost_usd` | Number / 0.30 by default | Greater than 0, at most 10; a pre-dispatch reservation (`MODEL_PRICING_JSON`) refuses steps that would exceed it, and settled cost is enforced after each step |
| `deadline_sec` | Integer / `DEFAULT_RUN_DEADLINE_SEC` | 10-86400; the reconciler fails runs past their deadline |
| `max_tool_invocations` | Integer / server default | 1-1000; counted before dispatch |
| `webhook_url` | String / null | Checked by URL guard at admission and DNS-checked on delivery |
| `secrets` | String-to-string map / `{}` | Stored in Redis outside conversation; can expire before resume |

Defaults can be changed by settings. Request `traceparent` is accepted only when it
matches the implementation's version-00 lowercase-hex pattern; otherwise a new value
is generated. This is trace correlation, not authentication or a full tracing backend.

Example 202 response:

```json
{
  "run_id": "run_example",
  "thread_id": "thd_example",
  "status": "queued",
  "stream_url": "/v1/runs/run_example/stream",
  "events_url": "/v1/runs/run_example/events"
}
```

Status projection fields are `run_id`, `thread_id`, `status`, `step_count`,
`total_cost_usd` (rounded to 6 decimals), `active_skills`, `pending_tool_calls`
and `result`. The persisted pending calls use provider tool-call structure
(`id`, `function.name`, `function.arguments`), unlike the flattened approval event.

### Resume body

```json
{
  "approved": true,
  "rejection_reason": null,
  "secrets": {},
  "batch_id": "6f1c…",
  "batch_digest": "<64 hex chars from input_required or GET /v1/runs/{id}.approval>"
}
```

`approved` is required. Rejection appends denial observations and continues the
model; it does not cancel the run. A nonempty `secrets` dictionary **replaces** the
stored dictionary, so send all still-needed values. Approval is for the entire
pending batch, not an individual tool.

Approved receipt: `{"run_id":"run_example","status":"queued","batch_id":"…","replayed":false,"action":"approved_and_dispatched"}`.
Rejected receipt uses `action: "rejected_and_resumed"`.
`batch_id`/`batch_digest` bind the decision to the exact calls the approver reviewed; the
production profile rejects unbound resumes. Decisions are immutable, attributed to the
authenticated approver, expire after `APPROVAL_TTL_SEC`, and exactly one concurrent
decision wins (others get 409). Approved calls are dispatched under signed, expiring
execution envelopes with stable `effect_key`s for downstream idempotency.

## 2. Current usage recipes

### 2.1 Start a development stack

Follow the root [setup instructions](../../README.md#run): copy the example environment
file locally, configure the model API key, then start Compose. Never commit that local
environment file or put real tokens in examples.

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d
curl --fail-with-body http://localhost:8000/readyz
```

The API binds port 8000 on the host, while Redis, PostgreSQL and Flower ports are
loopback-bound in the sample. Local setup is not a production perimeter.

### 2.2 Submit a bounded, read-only weather task

The following requires `curl`, `jq` and the default enabled weather plugin:

```bash
BASE_URL=http://localhost:8000
TENANT=demo-weather

RUN_JSON=$(curl --fail-with-body -sS -X POST "$BASE_URL/v1/runs" \
  -H 'Content-Type: application/json' \
  -H "X-Tenant-ID: $TENANT" \
  -d '{
    "prompt": "What is the weather forecast in Tokyo today?",
    "skills": ["weather"],
    "preload_skills": ["weather"],
    "max_steps": 6,
    "max_cost_usd": 0.20
  }')
RUN_ID=$(printf '%s' "$RUN_JSON" | jq -er '.run_id')
THREAD_ID=$(printf '%s' "$RUN_JSON" | jq -er '.thread_id')
printf '%s\n' "$RUN_JSON"

curl --fail-with-body -N -H "X-Tenant-ID: $TENANT" \
  "$BASE_URL/v1/runs/$RUN_ID/stream"
curl --fail-with-body -sS -H "X-Tenant-ID: $TENANT" \
  "$BASE_URL/v1/runs/$RUN_ID" | jq .
```

Preloading avoids relying on a first activation call. Do not submit a second run
on the same thread until the first finishes: concurrent history updates are not
serialized in the baseline.

### 2.3 Continue the conversation

After completion, reuse the returned thread:

```bash
jq -n --arg thread "$THREAD_ID" '{
  prompt: "Summarize that forecast in one sentence.",
  thread_id: $thread,
  skills: ["weather"],
  max_steps: 4,
  max_cost_usd: 0.10
}' | curl --fail-with-body -sS -X POST "$BASE_URL/v1/runs" \
  -H 'Content-Type: application/json' -H "X-Tenant-ID: $TENANT" \
  --data-binary @-
```

Only completed runs update reusable thread history. New runs resolve a fresh skill
allowlist; an old system catalog in the thread is informational, not authorization.

### 2.4 Inspect and decide a pending batch

For a run using an approval-required capability, inspect status and verify the
arguments/destination before submitting a decision:

```bash
APPROVAL=$(curl --fail-with-body -sS -H "X-Tenant-ID: $TENANT" \
  "$BASE_URL/v1/runs/$RUN_ID" | jq -c '.approval')
BATCH_ID=$(jq -r .batch_id <<<"$APPROVAL")
curl --fail-with-body -sS -H "X-Tenant-ID: $TENANT" \
  "$BASE_URL/v1/approvals/$BATCH_ID" | jq '.tool_calls, .batch_digest, .expires_at'

jq -n --arg d "$(jq -r .batch_digest <<<"$APPROVAL")" \
  '{decision: "approve", batch_digest: $d}' |
curl --fail-with-body -sS -X POST "$BASE_URL/v1/approvals/$BATCH_ID/decision" \
  -H "X-Tenant-ID: $TENANT" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: approve-$BATCH_ID" --data-binary @-

# Alternative decision, only while the batch is pending:
# {"decision":"reject","batch_digest":"…","reason":"Destination or amount not authorized."}
```

Exactly one concurrent decision wins; others receive 409, as does a digest that no
longer matches the pending batch. The decision, its outbox dispatch rows and the
`hitl_approved` event commit atomically, and each approved call carries a stable
`effect_key`. If a tool worker is lost after dispatch, the reconciler records an
explicit `unknown` outcome rather than silently re-executing a non-idempotent effect.
After deciding, reconnect SSE from the last processed id. Long pauses can require
re-supplying the complete secret dictionary in the decision.

### 2.5 TUI

```bash
haas-chat --url http://localhost:8000 --tenant demo-weather --preload weather
```

See the [TUI guide](../tui_usage.md) for `/secret`, `/preload`, models and approvals.
The current Python client sends tenant headers but has no built-in bearer-token
configuration. Its create helper exposes preload/models/budgets/secrets, but does not
currently expose the REST `skills` or `webhook_url` fields.

## 3. SSE observation pattern (CURRENT)

SSE is a replayable **run-event** stream (`run-events.v1`), not a token-by-token model
completion stream. Every frame is read from the durable PostgreSQL `run_events` log, and
its `id` is the run's monotonically increasing integer `sequence`. Redis only carries
advisory wakeups, so a lost wakeup, API replica restart or reconnect never loses events.

```text
id: 2
event: step_started
data: {"step":1,"model":"example/planner","active_skills":["weather"],"unlocked_tools_count":3}

```

The model and tool count above are illustrative, not asserted defaults.

| Event | Current payload / meaning |
|---|---|
| `run_queued` | `run_id`, `thread_id` |
| `step_started` | Step, model, active skills, unlocked count; optional `registry_drift` |
| `tool_dispatched` | Tool call ID, tool name, target queue (also emitted for approved calls) |
| `tool_result` | Tool call ID/name, error flag, first 300 observation characters |
| `input_required` | Step, `batch_id`, `batch_digest`, `expires_at`, pending calls, resume and decision endpoints |
| `hitl_approved` / `hitl_rejected` | Recorded decision for the batch (rejection includes the reason) |
| `approval_expired` | The batch expired undecided (`APPROVAL_TTL_SEC`); the run fails |
| `run_cancelling` | Cancel accepted while tools are outstanding (`outstanding_tools`) |
| `run_recovering` | Reconciler action (`action`, `attempt`, `reason`) after a lost or stalled step |
| `run_completed` | Output, steps used, observed USD total, cached tokens |
| `run_error` / `run_cancelled` | Terminal failure (including budget/deadline denials) or cancellation |

Reconnect with the last processed ID; events are delivered at most once per cursor:

```bash
# Replace this illustrative cursor with the last successfully processed SSE id.
LAST_EVENT_ID=7
curl --fail-with-body -N -H "X-Tenant-ID: $TENANT" \
  -H "Last-Event-ID: $LAST_EVENT_ID" "$BASE_URL/v1/runs/$RUN_ID/stream"
```

Semantics:

* A missing or non-integer `Last-Event-ID` replays from the start (`0`). The full log is
  retained with the run, so replay after any delay reproduces the same sequence.
* A cursor ahead of the latest sequence receives a synthetic `stream_gap` frame
  (`requested`, `latest`, no `id`) and continues from `latest`.
* Terminal events close the stream. `input_required` closes it only while that pause is
  still the run's latest state; replays of resumed runs stream past old pauses.
* Idle streams receive `: keep-alive` comments every `SSE_HEARTBEAT_SEC` (default 15 s).
* Bearer-authenticated streams are re-authenticated every `SSE_REAUTH_SEC`; a revoked or
  expired credential receives `stream_closed` (`reason`) and the stream ends.
* `GET /v1/runs/{id}/events?after=N&limit=M` pages the same log (`items`, `next_after`,
  `latest_sequence`) for pull-based consumers; `/steps` and `/usage` expose step
  attempts, tool invocations and the budget/usage ledger.
* HTTP clients must send headers. Browser `EventSource` cannot generally attach
  arbitrary bearer/tenant headers; use an authenticated same-origin cookie session
  or a fetch-based SSE client in the target, not secrets embedded in URLs.

## 4. Capability integration patterns (CURRENT)

| Pattern | Execution boundary | Credentials / errors | Appropriate use |
|---|---|---|---|
| Native Python tool | Handler in Celery tool worker; module imported at registry startup everywhere | `RunContext.get_secret`; typed args; bounded observation | Trusted operator-installed SDK/database/compute integrations |
| Plain HTTP manifest | POST to configured endpoint | Optional bearer + HMAC header; HTTP errors become tool observations | Thin stateless domain API adapter |
| `mcp_http` manifest | JSON-RPC `tools/call` over HTTP | Same optional auth; JSON or SSE response parser; RPC / `isError` surfaced | Known remote MCP tool endpoint |
| Run completion callback | Separate orchestrator delivery task | Currently unsigned JSON; bounded retries | Notification to a trusted receiver; reconcile by GET status |
| CLI/TUI | REST submission + SSE observation + resume | Header tenant identity, client-held secrets | Development/operator interaction |

### 4.1 Plugin lifecycle and policy

1. Operator creates a pack using `haas-plugin`, reviews native code and runs `doctor`.
2. Static manifest discovery applies root precedence, deny/allow policy and dependency order.
3. Runtime loading imports modules, registers handlers/endpoints and verifies declared capabilities.
4. Failed registrations are rolled back and reported; strict mode fails startup.
5. Readiness requires all skill tools to exist. Hidden skills are preload-only.
6. Authenticated effective skill access comes from the active tenant policy's exact
   plugin digest and skill grants, narrowed by request; active skills unlock tools.
   Development header mode continues to use `TENANT_SKILL_ALLOWLISTS`.
7. Restart API/workers to reload. Runs pin the policy revision and keep the existing
   global registry fingerprint diagnostic. Admission compares each grant with the
   installed plugin digest, but workers do not yet revalidate policy revocation or
   safely load immutable artifacts.

See [authoring templates](../../plugins/_templates/README.md). Installed packs are not
untrusted tenant uploads. Direct native network calls must implement their own safe
destination checks; the manifest guard is not a universal network sandbox.

### 4.2 HTTP manifest wire contract

* Request body is the validated arguments dictionary, excluding absent/null fields.
* Headers include JSON content type, JSON/SSE accept and `traceparent`.
* Durable tool invocations also send `Idempotency-Key` and `X-HaaS-Effect-Key` set to
  the invocation's stable `effect_key`. A redelivered task (for example after a tool worker
  crash) repeats the same key; receivers that perform side effects should deduplicate on it.
  Native handlers read the same value from `ctx.effect_key`.
* If `secret_key` is configured, the same secret is used for bearer authorization and
  `X-HaaS-Signature-256: sha256=<hex>`.
* The HMAC input is the UTF-8 encoding of Python
  `json.dumps(arguments, sort_keys=True)`, **not the raw transmitted HTTP body**.
  Cross-language receivers must match that serialization, including separators and
  escaping. Prefer a versioned raw-body signature contract in the target.
* Endpoint URLs are expanded from operator environment, validated at registration,
  then DNS-checked before request. Redirect following is disabled.
* There is no timestamp, nonce, destination idempotency key or replay protection.

Do not confuse this tool-manifest signature with the **run completion callback**:
the current callback sender does not add that signature or bearer header.

### 4.3 MCP adapter wire contract

The repository constant is `MCP_PROTOCOL_VERSION = "2026-07-28"`. This documents
the adapter's current wire value, not a claim of compatibility with every MCP server.
Validate the supported server/version pair before deployment.

The adapter sends JSON-RPC `tools/call`, `MCP-Protocol-Version`, `Mcp-Method` and
`Mcp-Name` headers plus `_meta` protocol/client/capability information. JSON-RPC `id`
currently equals the run ID, so sibling invocations do not have distinct request IDs.
Responses can be JSON or parsed SSE; text content is joined, otherwise structured
content is serialized. RPC errors and `isError` become remediation errors.

This is a **tool-call adapter**, not a complete general-purpose MCP client: it does
not implement a full initialization/session lifecycle, dynamic discovery, resumable
server sessions, arbitrary sampling, or MCP OAuth. Add these only as an explicit
versioned connector feature with destination-specific tests.

### 4.4 Current run callbacks

Completion payload: `run_id`, `status: completed`, `output`. Failure payload:
`run_id`, `status: failed`, `error`. Neither includes a durable event/delivery ID.

The task uses a 10-second HTTP timeout, no redirects and up to three retries after
the first attempt with exponential countdown. Unsafe/DNS-invalid targets are logged
and skipped; exhausted delivery is logged, not persisted in a delivery ledger.
The scheduling write is not atomic with finalization.

Receivers should deduplicate by run/status and reconcile via authenticated status
access. An unsigned callback is not sufficient evidence to authorize a business action.

## 5. Production contracts (TARGET; not accepted by current API)

API conventions, SDKs, CLI and the A2A / MCP-server / OpenAI-compatible surfaces are specified
in the [developer platform design](developer-platform.md). Marketplace registry and multi-agent
contracts are in the [marketplace](marketplace.md) and [multi-agent](multi-agent.md) designs.

### Admission

Require verified bearer/session identity, selected tenant membership, an
`Idempotency-Key`, and `agent_revision_id`. Replace raw `secrets` with authorized
secret-binding references. Policy bounds requested limits, models, callback
destinations and plugin revisions.

Request-size/token-count limits, 429 quota errors, 401 identity failures and 403
action-denied errors must have stable codes and correlation IDs. Continue using
404 for foreign/absent resource IDs. A duplicate key and same digest returns the
original run receipt; a different digest returns 409.

### Tenant onboarding and usage journey

1. Platform operator provisions a tenant in its approved region/cell, bootstraps a
   verified administrator membership, and assigns quota and retention policy.
2. Tenant administrator grants narrow user/service roles, approved plugin/model
   revisions and outbound destinations. Secret values are registered directly with
   the managed secret service; the application receives authorized binding references.
3. Agent owner drafts and validates an agent configuration, then publishes an immutable
   revision. Test it against nonproduction integrations before enabling write actions.
4. Tenant application authenticates, selects its tenant and submits that agent revision
   with prompt, bounded limits, authorized secret references and an idempotency key.
5. Application observes the returned run through authorized status/SSE. A distinct
   approver reviews exact high-risk actions when tenant policy requires separation.
6. Application receives a signed terminal notification and resolves authorized result/
   artifact references. It reconciles failures/unknown effects rather than blindly
   resubmitting the original request under a new idempotency key.
7. Tenant auditor reviews redacted usage and decision history; administrators rotate
   bindings, revoke capabilities or suspend service without editing past run provenance.

Tenant identity, membership, API-key, policy and audit APIs are implemented with
authenticated `haas admin` and TUI workflows. Tenant provisioning starts with the
bootstrap command. Phase 2 adds immutable agent draft/validate/publish APIs,
revision-pinned admission and scoped thread/history/branch APIs. Production API startup accepts only a
complete HTTPS OIDC configuration and the production encryption-key requirements;
it also requires non-owner RLS runtime logins and completed legacy cutover. Authenticated
run admission fails closed when no valid active tenant policy exists.
Development examples continue to use the local-only `X-Tenant-ID` compatibility path.
The terminal [agent/history guide](../tui_usage.md#agent-revisions-and-thread-history)
and generated [OpenAPI contract](../openapi-v1.json) describe the additive current API.
Phase 3 adds `Idempotency-Key` admission/decisions, the durable run-event log, digest-bound
approvals, fenced cancellation and budget reservations. Secret-binding references and
signed terminal callbacks in the target journey above remain later-phase work.

### Execution envelope

```json
{
  "schema_version": "1",
  "message_id": "opaque-message-id",
  "tenant_id": "opaque-tenant-id",
  "run_id": "opaque-run-id",
  "step_id": "opaque-step-id",
  "invocation_id": "opaque-invocation-id",
  "cell_id": "opaque-cell-id",
  "lease_epoch": 4,
  "expected_state_version": 12,
  "policy_digest": "sha256:example",
  "plugin_digest": "sha256:example",
  "deadline_at": "2026-10-17T17:00:00Z",
  "credential_grant_reference": "opaque-short-lived-reference",
  "traceparent": "00-00000000000000000000000000000001-0000000000000001-01"
}
```

An envelope is signed/authenticated by platform workload identity, short-lived and
compared with durable state. It contains no raw credentials. A reference is useful
only to the correct workload for the correct invocation/tenant; possession alone
must not authorize dereference.

### Event envelope and SSE replay

```json
{
  "schema_version": "1",
  "event_id": "opaque-event-id",
  "tenant_id": "opaque-tenant-id",
  "run_id": "opaque-run-id",
  "sequence": 8,
  "type": "run.completed",
  "occurred_at": "2026-10-17T16:30:00Z",
  "data": {"result_reference": "opaque-result-id"}
}
```

Commit event + state + outbox atomically. Redis is a hot projection, not the source
of replay truth. Maintain monotonic per-run sequence and versioned cursors; return a
documented retention-gap response with status/replay recovery instructions.
Revalidate long-lived stream authorization periodically and on tenant revocation.

Version event names explicitly rather than silently replacing current underscore names.
Clients deduplicate by event ID and do not equate HTTP 202 with successful execution.

### Signed completion delivery

Use HTTPS, registered tenant-owned destinations, a durable `delivery_id` and `event_id`,
timestamp, key ID and versioned signature. Suggested signing input:

```text
v1.<unix_timestamp>.<delivery_id>.<exact_raw_body_bytes>
```

Specify the exact byte encoding and HMAC-SHA256/base64url format in the implementation
contract. Receivers verify signature in constant time, key scope, a short timestamp
window and duplicate delivery ID before applying effects. Redeliveries retain event
identity but have recorded attempt metadata. Rotate keys with overlap, use bounded
backoff/jitter, and persist exhausted deliveries for explicit replay.

### Remote effects

Pass a stable invocation-derived idempotency key to destinations that support one.
Retry only within policy. After an ambiguous timeout, first query the effect receipt;
do not blindly repeat a payment, issue creation or deployment. Unsupported systems
require an explicit `unknown` result and reconciliation/compensation procedure.
