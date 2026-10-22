# HaaS Python SDK: Specification (v0.2)

| Field | Value |
|---|---|
| Package | `haas-sdk` (import `haas_sdk`) |
| Target version | 0.2.0 (evolves the existing 0.1.0) |
| Python | 3.11+ (CI matrix: 3.11, 3.12) |
| Runtime dependencies | `httpx` only (optional extras below) |
| Server baseline | HaaS 3.0.0, `/v1` API, `run-events.v1`, `durable_replay: true` |
| Status | Normative. Implementers follow this document; deviations require a spec change. |

Keywords **MUST**, **SHOULD**, **MAY** follow RFC 2119. Requirement IDs (`SDK-xxx-nn`) are used in tests and PRs.

---

## 0. Implementation progress

Recreation of `haas_sdk` against this spec (target 0.2.0). Each step ends with `ruff` + `pyright` green on `haas_sdk` and the relevant tests passing.

| Step | Scope | Status |
|---|---|---|
| S1 | `constants.py`, `errors.py` (3 body shapes, 409 subclasses), `secrets.py`, `_version.py` 0.2.0, `py.typed` | done (ruff+pyright green) |
| S2 | Models, `_core`, `_sse`, `_specs`, `pagination`, `compat`, `handles` (wait algorithm), `client`/`async_client`, `webhooks`, exports; `tests/sdk` updated | done (ruff+pyright green; 84 tests pass) |
| S3 | `preview/` namespaces (artifacts, webhooks admin, secrets, egress, redaction, retention, quotas, marketplace, mesh, ops, isolation) | done (ruff+pyright green; 89 tests pass) |
| S4 | `testing.py` `FakeHaas` + pytest plugin fixtures | in progress |
| S5 | `openai_compat.py` helper | pending |
| S6 | README quickstart, CHANGELOG, migration notes | pending |
| S7 | Quality gates: ruff, pyright strict, tests/sdk, parity (G2), canary (G7) | pending |

---

## 1. Purpose, goals, non-goals

### 1.1 Purpose
Give Python developers a small, typed, safe client for the HaaS `/v1` API, so that integrating durable, approval-gated, budget-bounded agent runs takes minutes and the safe path is the easy path.

### 1.2 Goals
1. **Five-minute success:** install, point at `haas dev up`, run an agent, stream events.
2. **Mirror the platform's guarantees** in the API shape: digest-bound approvals, durable replay, at-least-once delivery with explicit uncertainty, truthful budgets, tenant isolation.
3. **Sync and async parity** from a single transport-independent spec layer.
4. **Forward compatible:** `/v1` is additive; the SDK tolerates unknown fields, events and enum values.
5. **Testable by users:** ship a fake server and fixtures so integrators test without a real stack.

### 1.3 Non-goals
- No server-side imports. The SDK never imports `app.*`.
- No agent runtime, tool execution, or plugin loading (that lives in `haas_plugin_sdk`).
- No client-side authorization decisions. The server is the authority.
- No persistence of credentials, run state or transcripts.
- No TUI. (`haas_cli` stays separate; it SHOULD migrate to this SDK's transport later.)

---

## 2. Normative principles

| ID | Principle | Consequence in the SDK |
|---|---|---|
| P1 | The server is the authority | Capabilities from `/v1/me` drive UX hints only. The SDK MUST NOT pre-deny an action by role. |
| P2 | Approvals bind to what was reviewed | A decision can only be built from an `ApprovalReview` (frozen batch id + digest). No "approve whatever is pending" API. |
| P3 | At-least-once, explicit uncertainty | Tool outcome `unknown` is first-class. The SDK MUST NOT auto-retry any side effect. |
| P4 | Durable replay | Every event reader resumes from an integer cursor and de-duplicates by sequence. |
| P5 | Secrets live in memory only | `Secret` masks itself everywhere; no disk cache. |
| P6 | Untrusted content stays data | Model output and tool previews are plain strings; the SDK never evals, renders or follows them. |
| P7 | `/v1` is additive | Unknown fields kept in `raw`; unknown event types yielded as generic `RunEvent`. |
| P8 | Truthful money | Budget exposes settled, uncertain, reserved separately. Never a single merged "cost" in the high-level model. |
| P9 | Idempotent by default | Every mutating call sends an `Idempotency-Key`, generated once per logical call and reused on retry. |

---

## 3. Supported surface

Stable means covered by semver. Preview lives under `haas.preview.*`, may change in minor releases, and emits a one-time `PreviewWarning`.

| Namespace | Server routes | Status | Notes |
|---|---|---|---|
| (client) | `/v1/me`, `/v1/capabilities`, `/livez`, `/readyz` | Stable | |
| `runs` | `/v1/runs`, `/v1/runs/{id}`, `cancel`, `resume`, `stream`, `events`, `steps`, `usage` | Stable | |
| `approvals` | `/v1/approvals`, `/v1/approvals/{id}`, `/decision` | Stable | |
| `threads` | `/v1/threads`, `/{id}`, `/{id}/branches` | Stable | |
| `agents` | `/v1/agents/revisions`, `/{id}`, `/{id}/validate`, `/{id}/publish` | Stable | |
| `plugins` | `/v1/plugins` | Stable | Tenant-scoped catalog |
| `admin` | `/v1/admin/memberships`, `/api-keys`, `/policy`, `/v1/audit-events` | Stable | RBAC enforced server-side |
| `openai` | `/v1/chat/completions`, `/v1/responses`, `/v1/models` | Stable | Helper returns an OpenAI client |
| `preview.artifacts` | `/v1/artifacts*` | Preview | Server paths differ from SDK 0.1 (see §19) |
| `preview.webhooks` | `/v1/webhooks/*` | Preview | Server lacks per-route RBAC today |
| `preview.secrets`, `egress`, `redaction`, `retention`, `quotas` | `/v1/secrets`, `/v1/egress`, ... | Preview | |
| `preview.marketplace`, `mesh`, `ops`, `isolation` | `/v1/marketplace/*`, `/v1/agent-mesh/*`, `/v1/ops/*`, `/v1/isolation/*` | Preview | Promotion gated on server auth fixes (§19) |

**SDK-SCOPE-01:** A route is Stable only if the server enforces `require_action` on it and the OpenAPI snapshot contains it.

---

## 4. Package layout

```
haas_sdk/
  __init__.py        # public re-exports (see §4.1)
  _version.py
  _core.py           # ClientOptions, Call, retry policy, error construction
  _specs.py          # Op builders + response parsers (no I/O)
  _sse.py            # SSEParser
  client.py          # Haas (sync)
  async_client.py    # AsyncHaas
  handles.py         # RunHandle / AsyncRunHandle
  models.py          # dataclasses (frozen, with `raw`)
  errors.py          # exception hierarchy + code mapping
  constants.py       # statuses, event names, error codes (single source of truth)
  secrets.py         # Secret, redaction log filter
  webhooks.py        # signature verification
  pagination.py      # Page, iterator helpers
  compat.py          # capability negotiation
  openai_compat.py   # haas.openai helper (optional dep)
  testing.py         # FakeHaas, fixtures
  preview/           # unstable namespaces
  py.typed
```

### 4.1 Public exports
`Haas`, `AsyncHaas`, `Secret`, `RunHandle`, `AsyncRunHandle`, models (`Run`, `Budget`, `ApprovalReview`, `Decision`, `PendingCall`, `Invocation`, `RunEvent`, `EventPage`, `Page`, `Thread`, `AgentRevision`, `WebhookEvent`, ...), all errors, `webhooks`, `constants`, `__version__`.

### 4.2 Extras
`haas-sdk[openai]` (adds `openai`), `haas-sdk[otel]` (adds `opentelemetry-api`), `haas-sdk[keyring]` (opt-in credential storage helper), `haas-sdk[test]`.

---

## 5. Client construction and configuration

```python
Haas(
    *,
    base_url: str | None = None,       # env HAAS_URL, default http://localhost:8000
    api_key: str | Secret | None = None,  # env HAAS_API_KEY, then HAAS_BEARER_TOKEN
    tenant: str | None = None,         # env HAAS_TENANT, default "default-tenant"
    timeout: float = 30.0,
    max_retries: int = 2,
    stream_max_reconnects: int = 5,
    stream_reconnect_delay: float = 1.0,
    default_headers: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    http_client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
)
```
`AsyncHaas` is identical with async equivalents (`httpx.AsyncBaseTransport`, `Awaitable` sleep). Both are context managers (`with` / `async with`) and expose `close()`.

| ID | Requirement |
|---|---|
| SDK-CFG-01 | Explicit arguments override environment variables. |
| SDK-CFG-02 | Setting both `HAAS_API_KEY` and `HAAS_BEARER_TOKEN` to different values raises `ConfigError`. |
| SDK-CFG-03 | `base_url` MUST be absolute http(s) without credentials, path, query or fragment; else `ConfigError`. |
| SDK-CFG-04 | Sending requests with no credential emits one `UserWarning`: "X-Tenant-ID is a selector, not authentication." (dev mode only). |
| SDK-CFG-05 | An `hk_test_` key with an `https://` base URL whose host is not loopback raises `ConfigError` ("test keys are rejected in production"). Opt out with `allow_test_keys=True`. |
| SDK-CFG-06 | `repr(client)` MUST NOT contain the credential. |

### 5.1 Identity and compatibility
```python
me = haas.me()              # -> Identity(tenant_id, principal_id, roles, credential_type,
                            #             environment, capabilities)
caps = haas.capabilities()  # -> Capabilities(version, actions, event_schemas, features, durable_replay)
haas.require_compat()       # raises IncompatibleServerError
```
**SDK-COMPAT-01:** `require_compat()` passes only if `capabilities.version == "1"`, `"run-events.v1"` in `event_schemas`, and `durable_replay is True`. It is NOT called implicitly; `Haas(..., verify_server=True)` calls it on first request.

**SDK-COMPAT-02:** `capabilities.has_feature("output-deltas.v1")` and `can("run.approve")` exist for UX hints only (P1).

---

## 6. Transport

### 6.1 Headers (every request)
| Header | Value |
|---|---|
| `Authorization` | `Bearer <credential>` when set |
| `X-Tenant-ID` | configured tenant |
| `Accept` | `application/json` (SSE: `text/event-stream`) |
| `User-Agent` | `haas-sdk-python/<version>` |
| `traceparent` | injected from current OpenTelemetry context when `opentelemetry` is importable |
| `Idempotency-Key` | on mutating calls (§6.3) |

Credentials MUST only appear in headers, never URLs or logs (SDK-SEC-01).

### 6.2 Retries
| Condition | Retry? |
|---|---|
| Connection refused (`ConnectError`) | Yes, any method |
| Other transport error | Only if call is `idempotent` |
| 408, 425, 500, 502, 503, 504 | Only if `idempotent` |
| 429 | Yes for all calls (request was rejected, not executed). Honor `Retry-After`, capped at 60 s |
| Other 4xx | Never |

Backoff: full jitter, `uniform(0, min(8, 0.5 * 2**attempt))`. `idempotent` = GET/DELETE, or any call carrying an `Idempotency-Key`. **SDK-NET-01:** the retry decision lives in `_core.retry_delay` and is shared by sync and async clients.

### 6.3 Idempotency
Mutating methods accept `idempotency_key: str | None`. When omitted, the SDK generates `sdk-<uuid4>` **once per logical call** and reuses it on every retry. Keys MUST match `^[A-Za-z0-9._:-]{1,255}$` (validated client-side). A replayed response sets `result.replayed = True` (from `Idempotent-Replayed: true`).

| Mutation | Key scope on server |
|---|---|
| `runs.create/start` | tenant + principal + request digest; same key + different body gives 409 |
| `runs.resume`, `approvals.decide` | per run decision; same key + different decision gives 409 |

### 6.4 Timeouts
`timeout` applies to connect/write/pool and to non-streaming reads. Streaming requests use `read=None` (server heartbeats every 15 s with `: keep-alive`).

### 6.5 Request correlation
The SDK reads `X-Request-ID` from responses. Every `APIError` carries `request_id`. Callers MAY pass `request_id=` to any method to set `X-Request-ID` (must match `^[A-Za-z0-9._-]{8,128}$`).

### 6.6 Auth lifecycle
On 401: clear the in-memory credential, set `client.auth_expired = True`, raise `AuthenticationError`. The SDK MUST NOT retry a 401. `client.set_credential(...)` resets the flag.

---

## 7. Errors

```
HaasError
├─ ConfigError
├─ IncompatibleServerError
├─ HaasConnectionError                     # after retries
├─ APIError(status, code, message, request_id, body, retry_after)
│   ├─ BadRequestError          400
│   ├─ AuthenticationError      401
│   ├─ PermissionDeniedError    403
│   ├─ NotFoundError            404       # also foreign-tenant resources (not discoverable)
│   ├─ ConflictError            409
│   │   ├─ ThreadBusyError            "Thread has an active run"
│   │   ├─ HistoryChangedError        "Thread history changed"
│   │   ├─ ApprovalStaleError         "Approval batch is stale" / "digest does not match"
│   │   ├─ ApprovalExpiredError       "Approval batch expired"
│   │   ├─ ApprovalAlreadyDecidedError "No pending approval batch" / not input_required
│   │   ├─ PolicyChangedError         "policy changed"
│   │   └─ IdempotencyConflictError   "Idempotency-Key was already used"
│   ├─ ValidationError          422
│   ├─ RateLimitError           429       # .retry_after
│   └─ ServerError              5xx
├─ StreamClosedError                        # server stream_closed (auth revoked)
├─ WebhookVerificationError
├─ RunFailed(run, code)                     # terminal failed/cancelled
└─ ApprovalRequired(run)                    # wait() hit input_required with no handler
```

### 7.1 Parsing
**SDK-ERR-01:** `api_error()` MUST handle all three body shapes:
1. RFC 9457 problem: `{type,title,status,detail,instance,errors?}`.
2. OpenAI facade envelope: `{"error": {message,type,param,code,haas?}}` on `/v1/chat/completions`, `/v1/responses`, `/v1/models`.
3. Non-JSON: truncated text (500 chars).

`code` is taken from `body.code`, else `body.error.code`, else `None`. The specific 409 subclasses are selected by `code` when present, else by `detail` prefix (table above). The prefix table lives in `errors.py` with a unit test per entry.

### 7.2 Messages
`str(APIError)` = `"<status>: <message> (request_id=<id>)"`. Bodies are retained on `.body` but MUST be passed through `redact()` before inclusion in `repr`.

### 7.3 Stable run failure codes
From `run_error.code`, exposed as `constants.RunErrorCode`:
`budget.max_steps`, `budget.max_cost`, `budget.max_tool_invocations`, `budget.deadline`, `approval.expired`, `recovery.exhausted`, `execution.failed`, `run.failed`. Unknown codes pass through as strings.

---

## 8. Models

All models are `@dataclass(frozen=True, slots=True)`, expose `raw: dict` (excluded from `repr` and `==`), and have `from_dict()`. Parsing MUST be total: a missing optional field yields `None`, an unknown field is kept in `raw`, and a malformed required field raises `HaasError("malformed <Model>")`.

### 8.1 Constants (`constants.py`)
```python
RUN_STATUSES   = {"queued","running","input_required","cancelling","completed","failed","cancelled"}
TERMINAL_STATUSES = {"completed","failed","cancelled"}
ACTIVE_STATUSES   = {"queued","running","cancelling"}     # input_required is PAUSED, not active
TERMINAL_EVENTS   = {"run_completed","run_error","run_cancelled"}
APPROVAL_STATUSES = {"pending","approved","rejected","expired","cancelled"}
TOOL_OUTCOMES     = {"succeeded","failed","unknown","cancelled"}
```
**SDK-CONST-01:** No other module may define status strings (prevents drift such as `awaiting_approval` vs `input_required`).

### 8.2 Core models
```python
@dataclass(frozen=True, slots=True)
class Budget:
    max_cost_usd: float
    settled_usd: float
    uncertain_usd: float        # counts as spent (lost model responses)
    reserved_usd: float         # in-flight reservation
    remaining_usd: float
    steps_used: int; max_steps: int
    tool_invocations_used: int; max_tool_invocations: int
    deadline_at: datetime | None
    @property
    def spent_usd(self) -> float: return self.settled_usd + self.uncertain_usd

@dataclass(frozen=True, slots=True)
class PendingCall:
    id: str; name: str; arguments: dict | str
    requires_approval: bool | None; queue: str | None; allowed_secrets: tuple[str, ...]
    @property
    def parsed_arguments(self) -> dict: ...      # json.loads when str; never raises (returns {"_raw": s})

@dataclass(frozen=True, slots=True)
class ApprovalRef:               # embedded in Run
    batch_id: str; batch_digest: str; expires_at: datetime; decision_endpoint: str

@dataclass(frozen=True, slots=True)
class Run:
    run_id: str; status: str
    thread_id: str | None; result: str | None
    step_count: int | None; total_cost_usd: float | None
    state_version: int | None; event_sequence: int | None
    active_skills: tuple[str, ...]
    pending_calls: tuple[PendingCall, ...] | None
    approval: ApprovalRef | None
    budget: Budget | None
    deadline_at: datetime | None; parent_run_id: str | None; traceparent: str | None
    created_at: float | None
    # properties: is_terminal, is_active, waiting_for_approval, succeeded

@dataclass(frozen=True, slots=True)
class RunReceipt:                # POST /v1/runs, 202
    run_id: str; thread_id: str; status: str
    stream_url: str; events_url: str; replayed: bool

@dataclass(frozen=True, slots=True)
class ApprovalReview:            # what a human was shown (P2)
    batch_id: str; batch_digest: str; run_id: str; step_number: int
    calls: tuple[PendingCall, ...]
    expires_at: datetime; submitted_by_principal_id: str | None
    status: str; expired: bool
    def approve(self, *, secrets: Mapping[str,str] | None = None) -> Decision: ...
    def reject(self, reason: str | None = None) -> Decision: ...

@dataclass(frozen=True, slots=True)
class Decision:                  # an intent bound to a specific review
    review: ApprovalReview; approve: bool; reason: str | None; secrets: Mapping | None

@dataclass(frozen=True, slots=True)
class DecisionReceipt:
    run_id: str; batch_id: str; decision: str; replayed: bool

@dataclass(frozen=True, slots=True)
class Invocation:
    invocation_id: str; step_number: int; ordinal: int
    tool_name: str; status: str; queue: str
    effect_key: str               # hand to destinations for reconciliation
    attempt: int; preview: str | None
    # property: needs_reconciliation -> status == "unknown"

@dataclass(frozen=True, slots=True)
class StepAttempt:
    step_number: int; attempt: int; model: str; status: str
    cost_usd: float | None; cost_reported: bool | None
    prompt_tokens: int | None; completion_tokens: int | None; cached_tokens: int | None
    error: str | None

@dataclass(frozen=True, slots=True)
class UsageEntry:
    unit: str; quantity: float; cost_usd: float; settlement_status: str  # reserved|settled|released|uncertain

@dataclass(frozen=True, slots=True)
class RunEvent:
    type: str; data: dict; sequence: int | None   # None for advisory deltas
    event_id: str | None; occurred_at: datetime | None; schema_version: int | None
    # properties: is_terminal, is_delta, is_pause

@dataclass(frozen=True, slots=True)
class EventPage:
    items: tuple[RunEvent, ...]; next_after: int; latest_sequence: int
    @property
    def has_more(self) -> bool: return self.next_after < self.latest_sequence

@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: tuple[T, ...]; next_cursor: str | None
    @property
    def has_more(self) -> bool: return self.next_cursor is not None
```
Also: `Identity`, `Capabilities`, `Thread`, `ThreadHistory(items, history_version, active_run_id, next_offset)`, `AgentRevision`, `AgentConfiguration`, `Skill`, `Tool`, `PluginRecord`, `Membership`, `ApiKey`, `IssuedApiKey(credential: Secret, ...)`, `PolicyRevision`, `AuditEvent`, `WebhookEvent`.

### 8.3 Input types
```python
class AgentConfiguration(TypedDict, total=False):
    primary_model: str; planner_model: str; skills: list[str]
    preload_skills: list[str]; max_steps: int; max_cost_usd: float
```
`runs.start()` accepts either plain `dict` or the typed dict. Numeric money inputs are `float | Decimal`.

---

## 9. Resource API

All methods exist on both `Haas` (sync) and `AsyncHaas` (`async def`, same names and arguments). Signatures below are sync form.

### 9.1 `runs`

```python
runs.start(prompt, *, thread_id=None, agent_revision_id=None,
           expected_history_version=None, preload_skills=(), skills=None,
           primary_model=None, planner_model=None, max_steps=None,
           max_cost_usd=None, deadline_sec=None, max_tool_invocations=None,
           webhook_url=None, secrets=None, idempotency_key=None) -> RunHandle

runs.create(prompt, **same) -> RunReceipt          # low-level; no handle
runs.attach(run_id) -> RunHandle                   # no network until used
runs.get(run_id) -> Run
runs.list(*, limit=None, cursor=None, status=None, thread_id=None,
          created_after=None, created_before=None) -> Page[Run]
runs.iter(*, page_size=None, **filters) -> Iterator[Run]
runs.cancel(run_id) -> CancelReceipt               # status: cancelled | cancelling
runs.events(run_id, *, after=0, limit=None) -> EventPage
runs.stream(run_id, *, last_event_id=None, output_deltas=False) -> Iterator[RunEvent]
runs.steps(run_id) -> RunSteps                     # .steps: [StepAttempt], .invocations: [Invocation]
runs.usage(run_id) -> RunUsage                     # .budget: Budget, .entries: [UsageEntry]
runs.inspect(run_id) -> RunInspection              # get + steps + usage composed (3 calls, documented)
runs.resume(run_id, *, approved, batch_id, batch_digest, ...) -> DecisionReceipt  # low-level, discouraged
```

| ID | Requirement |
|---|---|
| SDK-RUN-01 | `prompt` length is validated only server-side (`max_prompt_chars`); the SDK does not duplicate limits. |
| SDK-RUN-02 | `secrets` values accept `str` or `Secret`; they are sent once in the body, never stored on the handle, never logged. |
| SDK-RUN-03 | When `agent_revision_id` is set, passing `primary_model`, `planner_model`, `skills`, `preload_skills`, `max_steps` or `max_cost_usd` raises `ValueError` client-side (server returns 422 "Pinned agent configuration cannot be overridden"). |
| SDK-RUN-04 | `max_cost_usd` must satisfy `0 < x <= 10`, `max_steps` 1..50, `deadline_sec` 10..86400, `max_tool_invocations` 1..1000; violations raise `ValueError` before any request. These mirror `CreateRunRequest` and are table-driven from `constants.LIMITS`. |
| SDK-RUN-05 | `runs.inspect` MUST NOT call a nonexistent `/inspect` route; it composes `get`, `steps`, `usage`. |
| SDK-RUN-06 | `runs.list(status=...)` accepts `str | Iterable[str]` and serializes comma-separated. |
| SDK-RUN-07 | `runs.cancel` on a terminal run raises `ConflictError` (server 409); the handle's `cancel()` swallows it and returns the final state. |

### 9.2 `RunHandle` (the central ergonomic type)

```python
class RunHandle:
    run_id: str; thread_id: str | None; receipt: RunReceipt | None

    def get(self) -> Run
    def events(self, *, output_deltas=False, from_sequence=None) -> Iterator[RunEvent]
    def wait(self, *, timeout: float = 300.0, poll_interval: float = 1.0,
             on_approval: ApprovalHandler | None = None,
             on_event: Callable[[RunEvent], None] | None = None) -> Run
    def wait_until_paused(self, *, timeout: float = 300.0) -> Run     # returns when status == input_required (or terminal)
    def cancel(self) -> Run
    def steps(self) -> RunSteps
    def usage(self) -> RunUsage
    def reconciliation_needed(self) -> list[Invocation]               # status == "unknown"
```
`ApprovalHandler = Callable[[ApprovalReview], Decision | None]` (async handlers allowed on `AsyncRunHandle`).

**`wait()` algorithm (normative):**
1. Open the event stream from `from_sequence` (default 0), forwarding each event to `on_event`.
2. Track the last sequence; on disconnect resume with `Last-Event-ID` (max `stream_max_reconnects`, linear backoff).
3. On `run_completed` / `run_error` / `run_cancelled`: fetch `runs.get` once for the authoritative `Run`; return it if completed, else raise `RunFailed(run, code)`. Cancelled runs raise `RunFailed(code="run.cancelled")`.
4. On `input_required`: call `approvals.review(batch_id)`.
   - No handler: raise `ApprovalRequired(run)`.
   - Handler returns `None`: return the paused `Run` (left pending on the server).
   - Handler returns a `Decision`: submit via `approvals.decide`, then continue from the saved cursor.
5. On `409` from decide: re-read `runs.get`. If status is no longer `input_required`, someone else decided: continue streaming. If batch id/digest differ: raise `ApprovalStaleError`. If expired: raise `ApprovalExpiredError`.
6. If the stream ends cleanly without a terminal event, poll `runs.get`; terminal status resolves as in step 3; active status reconnects.
7. If `timeout` elapses: raise `TimeoutError` (the run keeps running on the server; documented).

| ID | Requirement |
|---|---|
| SDK-HDL-01 | Events are yielded exactly once per sequence even across reconnects (dedupe by `seq <= last_seq`). |
| SDK-HDL-02 | Advisory `run.output.delta` events never advance the cursor and are only emitted when `output_deltas=True`. |
| SDK-HDL-03 | `stream_gap` is surfaced as a `RunEvent` and the cursor moves to `latest` from the payload. |
| SDK-HDL-04 | `stream_closed` raises `StreamClosedError` (authorization revoked/expired), never silently reconnects. |
| SDK-HDL-05 | `wait()` never auto-approves. |
| SDK-HDL-06 | The handle holds no secrets. |
| SDK-HDL-07 | A submitter handle on a run it submitted cannot approve (server 403: `PermissionDeniedError`, message mentions separation of duties). The SDK does not hide this. |

### 9.3 `approvals`

```python
approvals.list(*, status="pending", run_id=None, limit=None, cursor=None) -> Page[ApprovalReview]
approvals.iter(*, page_size=None, **filters) -> Iterator[ApprovalReview]
approvals.review(batch_id) -> ApprovalReview            # fetch + freeze digest (P2)
approvals.decide(review: ApprovalReview, decision: Literal["approve","reject"] | Decision,
                 *, reason=None, secrets=None, idempotency_key=None) -> DecisionReceipt
```
| ID | Requirement |
|---|---|
| SDK-APR-01 | `decide` takes the `ApprovalReview` object, not a bare id. The request always sends `batch_digest = review.batch_digest`. |
| SDK-APR-02 | There is NO overload that fetches the current digest implicitly. (Replaces 0.1 behavior of `decide(batch_id, decision)` fetching the digest; 0.1 signature emits `DeprecationWarning` for one minor then is removed.) |
| SDK-APR-03 | If `review.expired` or `review.status != "pending"`, `decide` raises `ApprovalStaleError` locally without a request. |
| SDK-APR-04 | `secrets` on a decision are documented as **replacing** the run's stored secret dict; callers must resend all still-needed values. |
| SDK-APR-05 | `decision` literals map to wire values `"approve"` / `"reject"`; reason max 2000 chars (validated). |
| SDK-APR-06 | Wire path is `POST /v1/approvals/{batch_id}/decision`; `runs.resume` remains available for compatibility. |

### 9.4 `threads`
```python
threads.list(*, search=None, limit=None, cursor=None) -> Page[Thread]
threads.iter(...) -> Iterator[Thread]
threads.get(thread_id, *, offset=None, limit=None, history_version=None) -> ThreadHistory
threads.branch(thread_id, *, expected_history_version) -> Thread
```
`threads.get(..., history_version=N)` raises `HistoryChangedError` (409) if the thread changed while paging. Starting a run on a thread with an active run raises `ThreadBusyError`; the documented recovery is wait, cancel, or `branch`.

### 9.4.1 `agents`
```python
agents.list(*, agent_name=None, state=None, limit=None, cursor=None) -> Page[AgentRevision]
agents.get(revision_id) -> AgentRevision
agents.create(agent_name: str, configuration: AgentConfiguration) -> AgentRevision   # POST body {agent_name, configuration}
agents.validate(revision_id) -> AgentRevision
agents.publish(revision_id) -> AgentRevision
agents.diff(left_id, right_id) -> str          # client-side unified diff of configuration + policy_rules + registry_fingerprint
```
State machine: `draft -> validated -> published` (forward only). Errors: `PolicyChangedError` when the active policy changed since the draft.

### 9.5 `plugins`
`plugins.catalog() -> PluginCatalog` with `.fingerprint`, `.plugins`, `.skills` (name, version, description, required_tools, allowed_secrets, references, plugin, category, hidden, ready), `.tools` (name, queue, requires_approval). Helper: `catalog.skill("revenue-ops").allowed_secrets` to tell callers which secrets to supply.

### 9.6 `admin` (RBAC enforced server-side; SDK does not pre-check)
```python
admin.memberships.list() -> list[Membership]
admin.memberships.grant(*, subject, role, kind="human") -> Membership
admin.memberships.revoke(principal_id, role) -> None
admin.api_keys.list() -> list[ApiKey]
admin.api_keys.create(*, principal_id, name, expires_in_days=90) -> IssuedApiKey   # .credential is Secret, shown once
admin.api_keys.revoke(key_id) -> None
admin.policy.get() -> PolicyRevision | None
admin.policy.activate(rules: PolicyRules | dict) -> PolicyRevision
admin.audit.list(*, limit=50, cursor=None) -> Page[AuditEvent]
admin.audit.iter(...) -> Iterator[AuditEvent]
```
`role` in `{"tenant_admin","submitter","approver","auditor"}`; `kind` in `{"human","service"}` (validated client-side from `constants`). `IssuedApiKey.credential` is a `Secret`; the response has `Cache-Control: no-store` and the SDK MUST NOT log it. `PolicyRules` TypedDict mirrors `TenantPolicyRules` (`model_providers, models, default_primary_model, default_planner_model, plugin_grants[{plugin_id,digest,skills}], destination_hosts, max_steps, max_cost_usd, retention_class, marketplace_tiers`).

### 9.7 `openai` (extra `[openai]`)
```python
haas.openai.client(**openai_kwargs) -> openai.OpenAI          # AsyncOpenAI on AsyncHaas
haas.openai.models() -> list[str]                              # GET /v1/models ids
haas.openai.model_for(agent_name, version=None) -> str         # "agent/<name>[@<n>]"
```
The client is configured with `base_url=<base>/v1`, `api_key=<credential>`, and a default `X-Tenant-ID` header. HaaS options go via `extra_body={"haas": {...}}` (`thread_id, preload_skills, skills, max_steps, max_cost_usd, deadline_sec, max_tool_invocations`; `extra="forbid"` server side). Documented behaviors: client tools and `n>1` are rejected (400 `unsupported_parameter`); an approval pause returns 409 `approval_required` with `x-should-retry: false` and `haas.approval` metadata, which the SDK documents how to resolve through `approvals`.

### 9.8 `artifacts` (Preview)
Implementation MUST follow the server's actual routes: `GET /v1/artifacts?run_id=`, `GET /v1/artifacts/{id}`, `GET /v1/artifacts/{id}/download`, `DELETE /v1/artifacts/{id}`, `POST /v1/artifacts` (multipart). SDK 0.1 paths (`/runs/{id}/artifacts`, `/content`) are removed.

### 9.9 `webhooks` admin (Preview) and verification (Stable)
Admin CRUD for endpoints, signing keys (create/rotate/revoke) and deliveries (list/get/replay) lives in `preview.webhooks`. Verification is stable (§13).

### 9.10 Other preview namespaces
`preview.{secrets,egress,redaction,retention,quotas,marketplace,mesh,ops,isolation}` are thin typed wrappers generated from the OpenAPI snapshot. Promotion criteria are in §19.

---

## 10. Streaming and events

### 10.1 Event catalog (`run-events.v1`)
| Event | Meaning | Key `data` fields |
|---|---|---|
| `run_queued` | admitted | `run_id, thread_id` |
| `step_started` | model call begins | `step, attempt, model, active_skills, unlocked_tools_count, reserved_usd, registry_drift?` |
| `tool_dispatched` | invocation sent | `tool_call_id, tool, queue, invocation_id, effect_key` |
| `tool_result` | outcome recorded | `tool_call_id, tool, status, invocation_id, is_error, preview` |
| `input_required` | **pause**: approval needed | `step, batch_id, batch_digest, expires_at, pending_tool_calls, resume_endpoint, decision_endpoint` |
| `hitl_approved` / `hitl_rejected` | decision recorded | `batch_id, dispatched_tools` / `batch_id, reason` |
| `approval_expired` | batch expired; run fails | `run_id` |
| `run_cancelling` | cancel accepted, tools outstanding | `outstanding_tools` |
| `run_recovering` | reconciler requeued | `action, attempt, reason` |
| `run_completed` | **terminal** | `output, steps_used, total_cost_usd, cached_tokens` |
| `run_error` | **terminal** | `error, code` |
| `run_cancelled` | **terminal** | `run_id, reason?` |
| `stream_gap` | cursor ahead of server (no `id`) | `requested, latest` |
| `stream_closed` | auth revoked (no `id`) | `reason` |
| `run.output.delta` | advisory preview (no `id`) | `schema, step, attempt, call, index, text, final, truncated` |

**SDK-EVT-01:** `RunEvent.data` is always a `dict`; non-dict JSON is wrapped as `{"value": ...}`; invalid JSON as `{"raw": "<text>"}` (parser behavior from 0.1, retained).
**SDK-EVT-02:** Event names not in the catalog are yielded unchanged (P7).
**SDK-EVT-03:** Event `data` is untrusted (P6). `tool_result.preview` is a 300-char prefix and MUST NOT be treated as the full output.

### 10.2 Delta semantics
Deltas are keyed `(step, attempt, call)`. A new key resets the preview; the final authoritative text is `run_completed.output`. Helper `haas_sdk.events.LivePreview` assembles deltas by key and exposes `.text` and `.reset_on_new_call`.

### 10.3 Cursors
Cursors are integers (as strings on the wire). Non-numeric `last_event_id` values are normalized to `"0"` (legacy `N-N` cursors are not supported).

---

## 11. Pagination

- Lists return `Page[T]` with opaque `next_cursor` (base64url JSON; never parsed by the SDK).
- `iter()` loops until `next_cursor` is `None` or repeats.
- **SDK-PAG-01:** Never infer end-of-list from a short page. Filtered scans can return fewer than `limit` items with a cursor.
- Event paging uses `EventPage.next_after` / `latest_sequence`, not cursors.
- Thread history uses `offset` + `history_version`; `next_offset` is `None` at end.

---

## 12. Secrets and security

| ID | Requirement |
|---|---|
| SDK-SEC-01 | Credentials appear only in `Authorization`; never in URLs, query strings, exception text, or `repr`. |
| SDK-SEC-02 | `Secret` (wraps `str`): `__repr__`/`__str__` return `Secret('••••')`; `.reveal()` returns the value; `__eq__` uses `hmac.compare_digest`; not JSON-serializable by default (explicit encoder in transport only). |
| SDK-SEC-03 | `haas_sdk.secrets.install_log_redaction()` adds a `logging.Filter` that masks `Bearer <tok>`, `hk_live_*`, `hk_test_*`, `sk-*`, and `key=value` credential pairs (patterns mirror `app.core.redaction`). It is installed on the `haas_sdk` logger by default. |
| SDK-SEC-04 | No on-disk caches of credentials or responses. The optional `[keyring]` helper stores nothing unless called explicitly. |
| SDK-SEC-05 | TLS verification is on; disabling requires passing your own `http_client`. |
| SDK-SEC-06 | Redirects are not followed for authenticated requests (`follow_redirects=False`). |
| SDK-SEC-07 | Run `secrets` are sent only on `runs.create/start`, `runs.resume`, `approvals.decide`. They are never echoed in `RunHandle`, events or errors. |
| SDK-SEC-08 | The SDK never evaluates or renders model/tool text. Docs warn to treat it as untrusted before use in shells, SQL, HTML or terminals. |
| SDK-SEC-09 | Canary test (§16): a canary token supplied as credential and as a run secret MUST NOT appear in captured logs, exceptions, `repr` of any object, or exported fixtures. |

---

## 13. Webhooks

### 13.1 Current server reality
`deliver_webhook_task` and run callbacks currently send **unsigned** JSON. Signed delivery (`HaaS-Signature`) is a P5 target. The SDK ships the verifier now but documents the safe pattern:

> Treat callbacks as hints. Reconcile with `runs.get(run_id)` before acting; dedupe on `run_id` + status.

### 13.2 Verifier (stable)
```python
webhooks.verify(payload, header, secrets, *, tolerance=300, now=None) -> bool
webhooks.verify_signature(...) -> int                     # raises WebhookVerificationError; returns timestamp
webhooks.construct_event(payload, headers, secrets, *, tolerance=300) -> WebhookEvent
webhooks.sign(payload, secrets, *, timestamp=None) -> str  # tests/tools
```
Scheme: `HaaS-Signature: t=<unix>,v1=<hex>[,v1=<hex>...]`, `hex = HMAC-SHA256(secret, f"{t}." + raw_body)`. Headers: `HaaS-Webhook-Id`, `HaaS-Key-Id`.

| ID | Requirement |
|---|---|
| SDK-WH-01 | Verify against the **raw bytes**, before JSON parsing. |
| SDK-WH-02 | Constant-time comparison across every candidate (no early exit). |
| SDK-WH-03 | Reject timestamps outside tolerance (default 300 s; `0` disables, documented as unsafe). |
| SDK-WH-04 | Accept multiple secrets and multiple `v1=` values for rotation. |
| SDK-WH-05 | `WebhookEvent.type` read from payload `type` or `event` (the run terminal payload carries `schema: "run-terminal-webhook.v1"`, `run_id`, `status`, and `output` or `error`/`code`). |
| SDK-WH-06 | Framework adapters (`haas_sdk.webhooks.fastapi`, `.flask`) are thin and optional; docs show raw-body access for each. |

---

## 14. Testing utilities (`haas_sdk.testing`)

```python
fake = FakeHaas(tenant="t1", strict=True)
fake.script_run(
    steps=[Step(model="m", tool_calls=[Call("get_weather_forecast", {"location": "Pune"})])],
    approval=Approval(calls=[Call("prepare_refund_batch", {"invoice_ids": ["inv_1"]})]),
    result="done", cost_usd=0.002)
haas = fake.client()           # real Haas over httpx.MockTransport (AsyncHaas: fake.async_client())
fake.decisions                 # recorded decisions (batch_id, digest, approve)
fake.last_request              # for header assertions
fake.fail_next(status=503)     # fault injection (429 with Retry-After, dropped stream, etc.)
```
| ID | Requirement |
|---|---|
| SDK-TEST-01 | The fake implements the semantics that matter: integer sequences, resume by `Last-Event-ID`, `stream_gap`, digest checking (409 on mismatch), single-winner decisions, idempotency replay + conflict, `input_required` closing the stream. |
| SDK-TEST-02 | `strict=True` raises on any route not in the OpenAPI snapshot. |
| SDK-TEST-03 | `tests/` fixtures are generated from `docs/openapi-v1.json` plus hand-authored SSE transcripts. |
| SDK-TEST-04 | For end-to-end use, docs recommend `haas dev up` + `haas dev seed` (deterministic mock model, no provider key). |
| SDK-TEST-05 | pytest plugin (`pytest11` entry point, extra `[test]`) provides `haas_fake` and `async_haas_fake` fixtures. |

---

## 15. Versioning, deprecation, compatibility

- Semver. Stable API changes: patch = fixes, minor = additive, major = breaking.
- SDK minor tracks server `/v1` capabilities; SDK 0.2 targets server 3.0.x. `User-Agent` carries the version.
- Deprecations emit `DeprecationWarning` for at least one minor release and are listed in `CHANGELOG.md`.
- Preview APIs may break in a minor release (documented, warned once).
- Server `Deprecation` / `Sunset` / `Link: rel="successor-version"` response headers are surfaced as `DeprecationWarning` once per route (SDK-VER-01).
- Public type hints are complete; `py.typed` shipped; `pyright --strict` passes on `haas_sdk` (excluding `preview/`).

---

## 16. Quality gates

| Gate | Description |
|---|---|
| G1 Contract | Generated checks compare every Stable `Op` (method, path, params, body keys) with `docs/openapi-v1.json`. Drift fails CI. (Would have caught the artifacts-path and `/inspect` bugs.) |
| G2 Parity | A test enumerates public methods of `Haas` and `AsyncHaas`; names and signatures MUST match (async-ness aside). |
| G3 Retry matrix | Table-driven tests for §6.2 including `Retry-After` and idempotency-key reuse across retries. |
| G4 Stream | Reconnect, duplicate replay, `stream_gap`, `stream_closed`, delta resets, terminal-after-pause, resume after approval. |
| G5 Approvals | Digest always sent; stale/expired local refusal; 409 mapping; no implicit digest fetch; handler returning `None` leaves pending. |
| G6 Errors | Every row of the §7 prefix table; problem+json and OpenAI envelope bodies. |
| G7 Secrets | Canary test (SDK-SEC-09), `Secret` behaviors, redaction filter. |
| G8 Models | Total parsing with unknown fields, missing optionals, malformed required fields. |
| G9 Types | `pyright --strict` on stable modules; `ruff`. |
| G10 Integration | CI job against `haas dev up` stack: quickstart, approval flow with separate approver, thread conflict, cancel, replay. |
| G11 Coverage | >= 90% lines on stable modules. |
| G12 Docs | Every public symbol has a docstring with an example; doctests run; README quickstart executes in G10. |

---

## 17. Documentation deliverables

1. **README:** install, 5-minute quickstart (against `haas dev`), sync + async.
2. **Concepts:** runs and threads, statuses, approvals and separation of duties, budgets (settled/uncertain/reserved), unknown effects, idempotency, durable events, tenancy and roles.
3. **Guides:** approvals; streaming and reconnection; budgets and limits; threads and branching; agent revisions as code; admin onboarding; OpenAI SDK drop-in; webhooks; testing; production checklist; error handling.
4. **Cookbook:** see Appendix C (each example is a runnable file under `examples/`, executed in G10 where feasible).
5. **API reference:** generated from docstrings.
6. **Migration 0.1 to 0.2:** table of renamed/removed items (§18).
7. **Troubleshooting:** table mapping errors to causes (e.g., `403 separation of duties`, `409 digest`, `404 on a valid-looking run id = wrong tenant`).

Documentation tone: show the safe path first; explain *why* (e.g., why there is no auto-approve).

---

## 18. Migration from 0.1

| 0.1 | 0.2 |
|---|---|
| `runs.create()` returns `dict` | `runs.create()` returns `RunReceipt`; `runs.start()` returns `RunHandle` |
| `runs.events()` returns `list[RunEvent]` | returns `EventPage` (adds `next_after`, `latest_sequence`) |
| `approvals.decide(batch_id, ...)` fetches digest if missing | `approvals.decide(review, ...)`; old form deprecated then removed |
| `Run` lacks budget/pending calls | adds `budget`, `pending_calls`, `approval`, `state_version`, `deadline_at`, `traceparent` |
| `artifacts.list(run_id)` -> `/runs/{id}/artifacts` | `preview.artifacts.list(run_id)` -> `/v1/artifacts?run_id=` |
| `artifacts.content()` -> `/content` | `preview.artifacts.download()` -> `/download` |
| status strings scattered (`awaiting_approval`) | `constants` only (`input_required`) |
| `models.TERMINAL_*` | re-exported from `constants` |

---

## 19. Server dependencies and open issues

The SDK cannot promote these until the server changes. Track as server tickets.

| # | Server gap | SDK consequence |
|---|---|---|
| S1 | `access`, `artifacts`, `operations`, `marketplace`, `agent_mesh` routers: several handlers lack `require_action`, use placeholder SQL (`principal.tenant_id` inside SQL text, `"TRUE -- tenant isolation"`), and reference columns not in migrations | Keep under `preview`; no stable promotion |
| S2 | Webhook/callback delivery unsigned (P5 target) | Verifier shipped; docs mandate reconciliation |
| S3 | `problem+json` bodies lack stable `code` on most errors | SDK falls back to message-prefix mapping (fragile). Ask server to add `code` as promised in `developer-platform.md` |
| S4 | `/v1/runs/{id}/tree` implementation groups by `thread_id`, not `parent_run_id`/`root_run_id` | `runs.tree()` stays preview |
| S5 | `POST /v1/artifacts` takes both a JSON model and a file in one route | Preview until a multipart contract is defined |
| S6 | Rate-limit headers (`RateLimit-*`, `Retry-After`) are specified but not implemented | SDK honors `Retry-After` if present; no quota introspection yet |
| S7 | Two inconsistent tenant headers in feature routers (`x-haas-tenant` vs `X-Tenant-ID`) | SDK sends `X-Tenant-ID` only; preview routers may need both |

**Promotion criteria (preview to stable):** route has `require_action`, appears in the OpenAPI snapshot, has server integration tests, and passes G1/G10 in the SDK.

---

## 20. Delivery plan

| Milestone | Scope | Exit criteria |
|---|---|---|
| **0.2.0** | Constants; models (Budget, ApprovalReview, ...); `RunHandle`; approvals review/decide; `EventPage`; `plugins`; `admin`; errors rewrite (3 body shapes, 409 subclasses); `Secret` + redaction; `testing.FakeHaas`; move artifacts/webhooks admin to preview; docs 1-3, 6 | G1-G9, G11 green; G10 quickstart + approval flow |
| **0.3.0** | `openai` helper; `require_compat`; deprecation-header surfacing; `LivePreview`; pytest plugin; full guides + cookbook | G10 complete; docs gate G12 |
| **0.4.0** | Signed-webhook receiver adapters (after server P5); `preview.webhooks` hardened | Server S2 closed |
| **1.0.0** | Promote preview namespaces whose server criteria are met; freeze Stable surface | §19 criteria met per namespace |

### Definition of done (per PR)
- [ ] Requirement IDs referenced in PR description and tests
- [ ] Sync and async parity test passes
- [ ] No new status/event/code literals outside `constants.py`
- [ ] Docstring with example on every public symbol
- [ ] Secret canary test passes
- [ ] CHANGELOG entry; migration note if behavior changed

---

## Appendix A: Endpoint map (Stable)

| SDK call | Method and path | Success | Idempotent |
|---|---|---|---|
| `me()` | `GET /v1/me` | 200 | yes |
| `capabilities()` | `GET /v1/capabilities` | 200 | yes |
| `health()` | `GET /readyz` | 200/503 | yes |
| `runs.create` | `POST /v1/runs` | 202 | via key |
| `runs.get` | `GET /v1/runs/{id}` | 200 | yes |
| `runs.list` | `GET /v1/runs` | 200 | yes |
| `runs.cancel` | `POST /v1/runs/{id}/cancel` | 202 / 409 terminal | yes |
| `runs.resume` | `POST /v1/runs/{id}/resume` | 202 | via key |
| `runs.events` | `GET /v1/runs/{id}/events?after&limit` | 200 | yes |
| `runs.stream` | `GET /v1/runs/{id}/stream` (+ `Last-Event-ID`, `output_deltas`) | 200 SSE | yes |
| `runs.steps` | `GET /v1/runs/{id}/steps` | 200 | yes |
| `runs.usage` | `GET /v1/runs/{id}/usage` | 200 | yes |
| `approvals.list/review` | `GET /v1/approvals`, `/v1/approvals/{id}` | 200 | yes |
| `approvals.decide` | `POST /v1/approvals/{id}/decision` | 202 | via key |
| `threads.list/get` | `GET /v1/threads`, `/{id}` | 200 | yes |
| `threads.branch` | `POST /v1/threads/{id}/branches` | 201 | no (key not supported server-side; not auto-retried) |
| `agents.list/get` | `GET /v1/agents/revisions`, `/{id}` | 200 | yes |
| `agents.create` | `POST /v1/agents/revisions` | 201 | no |
| `agents.validate/publish` | `POST /v1/agents/revisions/{id}/{op}` | 200 | no |
| `plugins.catalog` | `GET /v1/plugins` | 200 | yes |
| `admin.memberships.*` | `GET/POST /v1/admin/memberships`, `DELETE /{principal_id}/{role}` | 200/201 | list yes |
| `admin.api_keys.*` | `GET/POST /v1/admin/api-keys`, `DELETE /{key_id}` | 200/201 | list yes |
| `admin.policy.*` | `GET/PUT /v1/admin/policy` | 200/201 | get yes |
| `admin.audit.*` | `GET /v1/audit-events` | 200 | yes |
| `openai.*` | `POST /v1/chat/completions`, `/v1/responses`; `GET /v1/responses/{id}`, `/v1/models` | 200 | via key |

## Appendix B: Status semantics

| Status | Class | SDK behavior |
|---|---|---|
| `queued`, `running`, `cancelling` | active | keep following |
| `input_required` | **paused** | `wait()` invokes handler or raises `ApprovalRequired` |
| `completed` | terminal ok | return `Run` |
| `failed` | terminal | raise `RunFailed(code=...)` |
| `cancelled` | terminal | raise `RunFailed(code="run.cancelled")` |

Tool outcome `unknown`: "dispatched, no outcome recorded before timeout. The effect may have happened; it is never retried." Always surfaced through `Invocation.needs_reconciliation` with the `effect_key`.

## Appendix C: Required cookbook examples (each a runnable file)

1. `quickstart.py`: `haas dev up`, run, print result (sync and async).
2. `stream_events.py`: typed streaming, live deltas, resume from a saved cursor.
3. `approval_separation_of_duties.py`: submitter + approver clients, review, decide, wait.
4. `approval_handler_cli.py`: interactive approve/reject via `wait(on_approval=...)`.
5. `budgets_and_limits.py`: per-run caps, reading `Budget`, handling `budget.max_cost`.
6. `unknown_effects.py`: reconcile `unknown` invocations by `effect_key`.
7. `threads_and_branching.py`: continue, `ThreadBusyError`, branch with version.
8. `agents_as_code.py`: create, validate, publish, run pinned, diff revisions.
9. `async_fanout.py`: bounded concurrency, idempotency keys, `RateLimitError`.
10. `fastapi_webhook_receiver.py`: raw-body verify, dedupe, reconcile via `runs.get`.
11. `openai_dropin.py`: `agent/<name>` model, `extra_body`, handling `approval_required`.
12. `admin_onboarding.py`: memberships, policy with plugin digest grants, API keys, audit paging.
13. `plugins_secrets.py`: read catalog to learn a skill's `allowed_secrets`, supply via `Secret`.
14. `testing_with_fake.py`: `FakeHaas` scenario with approval and fault injection.
15. `ci_smoke.py`: minimal `haas dev` smoke (readyz, capabilities, one run) for pipelines.

Each example MUST: use environment variables for credentials, never print secrets, handle the specific errors it demonstrates, and state which server roles it needs in its header comment.
