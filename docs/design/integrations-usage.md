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
| `POST /v1/runs/{run_id}/cancel` | Tenant | 202 cancelled / 409 terminal | Cancellation is durable; in-flight external side effects may not be interruptible |
| `POST /v1/runs` | Create body below | 202 run/thread/status/stream URL | Writes `running`, publishes queued event, schedules work |
| `GET /v1/runs/{id}` | Tenant header | 200 run projection | Foreign/absent run → 404; no full conversation returned |
| `GET /v1/runs/{id}/stream` | Tenant header; optional `Last-Event-ID` | 200 `text/event-stream` | Ownership before stream; no per-event reauthorization |
| `POST /v1/runs/{id}/resume` | Resume body below | 202 action receipt | 404 ownership; 409 if not paused or claim lost |
| `GET /v1/plugins` | Tenant header | 200 registry projection | Skills filtered by tenant; plugin/tool metadata is global |

No tenant/member/agent administration, run listing, cancel, artifact, durable audit or
webhook-delivery administration endpoints exist yet.

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
| `max_cost_usd` | Number / 0.30 by default | Greater than 0, at most 10; observed guard, not hard ceiling |
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
  "status": "running",
  "stream_url": "/v1/runs/run_example/stream"
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
  "secrets": {}
}
```

`approved` is required. Rejection appends denial observations and continues the
model; it does not cancel the run. A nonempty `secrets` dictionary **replaces** the
stored dictionary, so send all still-needed values. Approval is for the entire
pending batch, not an individual tool.

Approved receipt: `{"run_id":"run_example","status":"running","action":"approved_and_dispatched"}`.
Rejected receipt uses `action: "rejected_and_resumed"`.
There is no approval ID, expected version, expiration or authenticated approver
attribution in the current contract.

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
curl --fail-with-body -sS -H "X-Tenant-ID: $TENANT" \
  "$BASE_URL/v1/runs/$RUN_ID" | jq '.status, .pending_tool_calls'

curl --fail-with-body -sS -X POST "$BASE_URL/v1/runs/$RUN_ID/resume" \
  -H "X-Tenant-ID: $TENANT" -H 'Content-Type: application/json' \
  -d '{"approved":true}'

# Alternative decision, only while a run is paused:
# {"approved":false,"rejection_reason":"Destination or amount not authorized."}
```

A second concurrent resume should receive 409 after one claimant wins. That only
protects the status claim; it is not a guarantee that dispatch/effects survive every
crash exactly once. After resume reconnect SSE. Long pauses can require re-supplying
the complete secret dictionary.

### 2.5 TUI

```bash
haas-chat --url http://localhost:8000 --tenant demo-weather --preload weather
```

See the [TUI guide](../tui_usage.md) for `/secret`, `/preload`, models and approvals.
The current Python client sends tenant headers but has no built-in bearer-token
configuration. Its create helper exposes preload/models/budgets/secrets, but does not
currently expose the REST `skills` or `webhook_url` fields.

## 3. SSE observation pattern (CURRENT)

SSE is a replayable **run-event** stream, not a token-by-token model completion stream.
Each frame carries Redis's stream entry ID:

```text
id: 1770000000000-0
event: step_started
data: {"step":1,"model":"example/planner","active_skills":["weather"],"unlocked_tools_count":3}

```

The model and tool count above are illustrative, not asserted defaults.

| Event | Current payload / meaning |
|---|---|
| `run_queued` | `run_id`, `thread_id` |
| `step_started` | Step, model, active skills, unlocked count; optional `registry_drift` |
| `tool_dispatched` | Tool call ID, tool name, target queue; emitted on ordinary ungated dispatch |
| `tool_result` | Tool call ID/name, error flag, first 300 observation characters |
| `input_required` | Step, flattened pending calls and resume endpoint |
| `hitl_approved` | Number of dispatched tools; separate per-tool dispatch events are not emitted by resume |
| `hitl_rejected` | Rejection reason |
| `run_completed` | Output, reported steps used, observed USD total, cached tokens |
| `run_error` | Error summary |

Reconnect with the last processed ID, preserving application-side deduplication:

```bash
# Replace this illustrative cursor with the last successfully processed SSE id.
LAST_EVENT_ID=1770000000000-0
curl --fail-with-body -N -H "X-Tenant-ID: $TENANT" \
  -H "Last-Event-ID: $LAST_EVENT_ID" "$BASE_URL/v1/runs/$RUN_ID/stream"
```

Constraints:

* Invalid cursor syntax becomes `0-0`; old/pruned cursors do not get an explicit gap error.
* Streams are approximately capped and expire 24 hours after the last event.
  An expired stream cannot reconstruct lost events from PostgreSQL today.
* Terminal completion/error closes the stream. A pause event closes it if the run is
  still paused; stale pause events can be streamed past if already resumed.
* Idle terminal detection performs a final nonblocking read, but does not synthesize
  a missing terminal event. Use GET status if the stream ends without one.
* There is no application heartbeat frame; configure proxy idle timeouts appropriately.
* The CLI reconnects on transport failure, but returns to its caller on a pause.
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
bootstrap command; immutable agent revisions remain a Phase 2 capability, so the
current `/v1` examples do not provision agents. Production API startup accepts only a
complete HTTPS OIDC configuration and the production encryption-key requirements;
authenticated run admission still fails closed when no valid active tenant policy exists.
Development examples continue to use the local-only `X-Tenant-ID` compatibility path.

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
