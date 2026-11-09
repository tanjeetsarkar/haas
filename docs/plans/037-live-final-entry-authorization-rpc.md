# 037 — Live final-entry authorization RPC (035c2)

- Status: identity foundation, deny-only endpoint and isolated HTTPS client independently approved; worker hooks next
- Type: slice plan for 035c2, split to keep the first implementation slice ≤400 changed lines
- Owner agent: core-engineer (after architect handoff)
- Epic: [035 — Bounded external-action initiation](035-bounded-action-initiation.md)
- ADR: [ADR-0021 addendum](../adr/0021-run-tool-restrictions-and-exact-intents.md#addendum-live-worker-authorization-for-exact-intents)

## Goal

### Superseding identity decision / foundation slice (2026-11-09)

User approved the concrete dedicated Ed25519 request-JWT contract recorded in
ADR-0021, including its five settings and HTTPS/key-distribution scope.
Production provisioning, deployment and shared-DB changes remain unauthorized.
The existing-identity search found none; no tenant/envelope credential is reused.

Identity foundation precedes endpoint c2a and requires independent review.
Allowed files: new `app/core/worker_identity.py`, settings-only additions in
`app/core/config.py`, new `tests/test_worker_identity.py`, this plan and ADR-0021.
No endpoint/router/client/network-policy edits. All 035 holds remain ON.
Uses existing PyJWT/cryptography; no dependency or migration. <=400 changed lines.
Signing reads owner-only PEM files; verification reloads non-writable public
trust mapping, authenticates the exact request and consumes its Redis replay ID.
No token/key/claim/path values appear in denial messages. Missing configuration
denies even in development. TLS client checks are deferred, not implemented.

Baseline: `.venv/bin/ruff check app/core/config.py` passed;
`.venv/bin/pyright app/core/config.py`: 0 errors; `.venv/bin/pytest -q
tests/test_identity.py tests/test_run_restrictions.py
tests/test_run_restrictions_api.py`: **120 passed**. Boundary baseline:
**175 errors / 9 warnings**. New module/tests were N/A before creation.

Foundation final evidence: `.venv/bin/ruff check app/core/config.py
app/core/worker_identity.py tests/test_worker_identity.py` passed; pyright on
the two runtime modules: **0 errors**. Targeted baseline command plus
`tests/test_worker_identity.py`: **157 passed** (120 baseline + 37 new).
Boundary **175 errors / 9 warnings → unchanged**; `git diff --check` passed.
One test-clock correction moved relative timestamp construction from collection
to execution; no runtime retry/fix was needed. Synthetic temporary keys only.
Runtime/settings/tests: 327 added lines; with this contract/evidence under 400.
No live credentials, DB operations, provisioning, deployment or hold removal.
Next: independent foundation review before endpoint c2a.

### 035c2a endpoint staging evidence (2026-11-09)

Foundation independent **APPROVE** reported by parent. Added only
`app/api/internal_run_authorization.py`, focused endpoint tests and two router
registration lines in `main.py`; `store.py` remains released to the phase owner.
The internal route independently authenticates the dedicated worker JWT, then
validates strict request fields and scopes its read-only run/invocation/approval
query to the authenticated canonical tenant UUID; supplied tenancy headers do
not select DB scope. Context is restored after the read.

Evaluator compares available persisted intent/provenance/argument and approval/
status/deadline fields, but **never returns allow**. Even matching fixture state
returns `restriction.unavailable`: persisted envelope expiry, digested approval
intent binding and live registry/plugin eligibility integration remain future
c2c/035e work. No hypothetical fixture-only release path exists. Deny response
echoes request/stage and canonical binding digest; validity is immediately closed.
DB outage returns generic 503, service-auth failure generic 401, malformed
authenticated contract generic 422. No secrets/grants/handlers invoked.

Baseline: main lint/types clean; identity/API/facade selection **120 passed**.
Final selection with endpoint tests **147 passed**; touched-file ruff passed,
pyright 0 errors; boundary **175 errors / 9 warnings → unchanged**.
Commands: `.venv/bin/ruff check app/api/internal_run_authorization.py app/main.py
tests/test_internal_run_authorization.py`; `.venv/bin/pyright` on those runtime
paths; `.venv/bin/pytest -q tests/test_internal_run_authorization.py
tests/test_worker_identity.py tests/test_run_restrictions_api.py
tests/test_openai_compat.py`. Public OpenAPI unchanged: internal router is excluded.
One typing fix casts the existing async Redis client to the reviewed replay
protocol, without changing identity foundation. All 035 holds remain ON.
No live keys, provisioning/deployment, network changes or shared DB operations.
Endpoint is staged, not completed current-state authorization; independent review
pending before client or allow-path work. Total sub-slice remains <=350 lines.

### 035c2b-client isolated sub-slice (2026-11-09)

User authorized isolated client implementation while endpoint is read-only under
review. Files: new `app/core/worker_authorization.py`,
`tests/test_worker_authorization.py`, and this evidence only. No endpoint,
identity-foundation, worker-hook, deployment or network-policy edits.

Client constructs a fresh request ID/JWT for each operation, validates fixed
HTTPS path/host without credentials/query/fragment, retains existing SSRF/DNS
guard, uses default certificate verification plus optional configured CA,
`trust_env=False`, no redirects, 2-second timeouts and no automatic retry.
Internal private destinations require the existing operator SSRF host allowlist;
this code does not add a private-host bypass or alter network reachability.
Response body is capped at 8192 bytes and duplicate JSON keys are rejected.
Only strict version/stage/nonce/full-binding allow responses with finite,
short-lived (<=5 seconds), unexpired timestamps within envelope expiry return.
Any denial/status/timeout/TLS/binding/parse/config failure raises one generic
error; no response previews, arguments, URLs or token material are exposed.

Baseline identity+endpoint tests: **64 passed**; existing identity lint/types
clean, boundaries **175 errors / 9 warnings**. New paths N/A before creation.
Final commands: `.venv/bin/ruff check app/core/worker_authorization.py
tests/test_worker_authorization.py`; `.venv/bin/pyright
app/core/worker_authorization.py`; `.venv/bin/pytest -q
tests/test_worker_authorization.py tests/test_worker_identity.py
tests/test_internal_run_authorization.py`. Tests use MockTransport and synthetic
temporary identity keys; no live TLS handshake, live keys or deployed RPC proof.
Worker hooks, native/HTTP/MCP/sandbox ordering, real endpoint allow integration,
network-policy rendering and deployment remain subsequent slices, not complete.
All 035 public/scheduling/lease holds remain ON. Independent client review pending.
Final expanded selection (the three files above plus
`tests/test_run_restrictions_api.py tests/test_openai_compat.py`): **177 passed**
(147 existing + 30 new client tests). Ruff passed; pyright 0 errors;
boundary **175 errors / 9 warnings → unchanged**; `git diff --check` passed.
Runtime/tests 267 lines, plus this evidence below the 400-line cap. No fixes
to reviewed identity/endpoint code and no live processes started.

Provide a narrow, authenticated internal core authorization RPC so production
tool workers can obtain a fresh point-in-time decision without PostgreSQL
network access. Call it once immediately before credential-grant redemption
and again immediately before final handler/adapter entry. Preserve exact
argument, intent, tenant/run/invocation and expiry binding. Deny when the
service, decision, current policy or binding is unavailable, stale or
mismatched.

The user approved this authorization boundary and its narrowly scoped worker
network path. This is not a global transaction or a guarantee that revocation
cannot race an already-authorized external effect. No public/user API, new
database migration, service restart, shared-DB change or deployment is
authorized here.

## Boundary classification

| Change | Owner | Would exist if the extension were deleted? |
|---|---|---|
| Generic authorization-decision endpoint and evaluator | Core | Yes; every plugin using restricted exact-intent execution needs it |
| Tool-worker to internal core endpoint network path | Core/deployment | Yes; it protects all generic tool execution authority checks |
| Request/response binding types and worker checks | Core | Yes; extension-independent enforcement |
| Domain arguments, destination authorization and effect semantics | Plugin | No; remain extension-owned |

Plugins such as Atlas and an incident-notification extension can consume the
same core decision. No plugin identifiers, tools or domain schema enter core.

## Evidence and bounds

The plan-035 c1 note reports approval checks at transactional dispatch and at
the grant publisher, while exact-intent release remains held. The bounded
source review for this decision found:

* `credentials.redeem_grant` consumes Redis grants with `GETDEL`, then checks
  audience, plugin digest, invocation id and expiry and decrypts the secret.
  It does not read current run authority, approval or policy revocation.
* `credentials.prepare_tool_dispatch` can load run and policy state in the
  publisher's service context, but that is earlier than the tool worker's
  final handler entry.
* `app/core/envelope.py` verifies an Ed25519 signed, expiring dispatch snapshot
  and argument digest. Tool workers hold the verify key, not the signing key.
  The envelope is not a live revocation query; do not reuse its signing key as
  caller authentication or claim that a signature makes it fresh.
* Helm NetworkPolicy gives `tool-worker` Redis and configured outbound access,
  not PostgreSQL. Production Compose withholds the database DSN and envelope
  signing key from tool workers. No worker-to-core service authentication
  mechanism was located in the bounded search. Reuse an existing authenticated
  workload/service identity if confirmed; do not assume user bearer auth,
  tenant API keys, envelope signing keys or dev defaults qualify.

The exact service-auth implementation is an explicit pre-edit gate. If no
existing service-auth identity can authenticate the worker to this internal
route, stop and ask for the specific auth mechanism/configuration decision;
do not add an environment variable, dependency, key, credential-distribution
path or network exception by inference.

## RPC contract

Internal-only route: `POST /internal/v1/run-authorizations/check`. It is not
registered as a public `/v1` user endpoint, exposed in OpenAPI/public SDKs, or
authorized by tenant user roles. Network policy narrowly allowlists the
tool-worker workload to the core API service/port; Kubernetes NetworkPolicy is
L3/L4 and cannot distinguish this HTTP path. Route-level service authentication
and authorization are mandatory. The network rule does not grant tool-worker
PostgreSQL access. Route authentication derives the caller principal from the
verified service-auth context, never a request field.

Request v1 (illustrative wire shape; field names are normative for this plan):

```json
{
  "version": 1,
  "operation": "pre_grant",
  "request_id": "opaque-random-per-check",
  "tenant_id": "canonical-tenant-uuid",
  "run_id": "run-id",
  "invocation_id": "invocation-id",
  "tool_name": "registered-tool-name",
  "arguments_sha256": "lowercase-hex-sha256-of-strict-canonical-json-arguments",
  "intent_binding": "persisted-domain-separated-intent-digest",
  "policy_revision_id": "pinned-policy-revision",
  "registry_fingerprint": "pinned-registry-fingerprint",
  "plugin_digest": "pinned-plugin-digest",
  "envelope_expires_at": 0
}
```

`operation` is exactly `pre_grant` or `final_entry`. `request_id` is unique
per check and is not an authorization credential. Arguments are not sent;
the RPC carries only their canonical SHA-256 digest. The server independently
loads the tenant-scoped run/invocation and compares every binding value with
persisted authority, exact intent, invocation and current policy. It validates
the pinned policy revision remains current/authorized, plugin digest and
registry fingerprint are consistent, run is active/not cancelled/not expired,
approval and invocation state are eligible for the requested stage, and the
exact tool/arguments match the immutable intent. Missing, malformed,
unsupported-version, legacy-unrestricted, held or revoked state denies.

Success response is bound to the exact request and stage:

```json
{
  "version": 1,
  "request_id": "same-opaque-random-per-check",
  "decision": "allow",
  "operation": "pre_grant",
  "binding_sha256": "sha256-of-canonical-request-binding",
  "checked_at": 0,
  "valid_until": 0
}
```

`binding_sha256` covers version, operation, request id, canonical tenant UUID,
run id, invocation id, tool name, arguments digest, intent binding, pinned
policy revision, registry fingerprint, plugin digest and envelope expiry.
`valid_until` is server-generated, short-lived, and no later than envelope
expiry. It is an operation-scoped point-in-time authorization decision, not a
reusable bearer permit or transaction lock. The response is trusted only over
the authenticated service channel; do not invent a second signature/key
protocol. Denials return a stable generic denial code without leaking other
tenant/run data.

### Ordering and fail-closed behavior

1. Worker verifies the existing envelope signature, expiry, invocation identity
   and raw argument digest.
2. Worker strictly parses/canonicalizes exact-intent arguments and checks the
   canonical digest and intent binding match the signed/persisted intent.
3. Worker makes `pre_grant` RPC and verifies response authentication, matching
   request id, operation, binding digest, `allow`, and unexpired `valid_until`.
   Only then may it redeem scoped grants. RPC timeout, unavailable route,
   authentication failure, malformed/stale response or mismatch denies before
   any grant redemption/decryption.
4. After grant redemption, immediately before native, HTTP/MCP or sandbox
   adapter handoff, worker repeats strict argument/binding checks and makes a
   fresh `final_entry` RPC. Deny before handler/adapter invocation on any error
   or mismatch. It must not use a cached `pre_grant` decision.
5. Any check failure returns a generic authorization denial, produces no
   handler call, and cannot fall back to envelope-only permission, legacy
   unrestricted behavior, DB access, or a retry with new arguments.

The second check is point-in-time. Policy can change after `final_entry` returns
allow and before/during an external side effect; this design cannot recall an
effect or promise global atomicity with PostgreSQL revocation. A stronger
“no effects after revocation completes” guarantee requires a separately
designed permit lease plus revocation-drain/in-flight-handler protocol, outside
this slice. Homogeneous rollout excludes old workers but does not close this
race.

## Ordered slices

### 035c2b worker-hook bounded implementation

User authorized actual worker pre-grant/final-entry integration after client
APPROVE and the authority downgrade correction. Allowed: `app/worker/tasks.py`,
new `app/core/worker_restrictions.py`, new `tests/test_worker_restrictions.py`,
this evidence. No store, endpoint, main, network/config or deployment edits.
Budget <=400 implementation/test lines. Baseline worker ruff/types clean;
phase3 units + skill policy + HTTPS client: **52 passed**; boundary 175/9.

Signed envelope carries `authority_json` only for persisted non-null authority.
Presence requires strict exact intent; null/malformed/tools-only cannot fall
back to legacy. Before GETDEL and again at native/endpoint-handler/sandbox
handoff, compare canonical full arguments/schema, intent, plugin and registry
revision plus tenant/run binding, then call the approved client with fresh
operation-scoped authorization. Restricted path must not open PostgreSQL or
repair arguments; sandbox setup failure must not downgrade isolation.
Legacy envelopes omit the field and retain existing behavior.

Endpoint remains deny-only. Destination identity resolution, real registry
fingerprint format integration (current registry uses 16 hex, RPC requires
64), static-schema support, deterministic/approval integration and release
proof remain later work, not permission to activate. No SSRF bypass: existing
host allowlist remains required; no new generic allowlist behavior is added.

Worker-hook implementation evidence: authority is copied from persisted
`authority_json` into the signed envelope, never prompts/messages. The actual
worker effect path validates it before `redeem_grants`; the shared native/
endpoint handler and sandbox handoff revalidate and make a separate
`final_entry` call. Denials never enter a handler; sandbox setup failure cannot
fall back in-process. Restricted execution does not instantiate a PostgreSQL
store. Tools-only/null/malformed authority denies rather than becoming legacy.
Static/catalog-only validators remain unsupported and deny explicitly.
This is enforcement staging, not operational exact-action availability.

Final ruff on worker/helper/tests and migration/tests passed; pyright on the
three runtime/migration modules: **0 errors**. Expanded authority/API/identity/
endpoint/client/worker/legacy selection: **272 passed / 1 PG-only skipped**.
The originally recorded worker/client/skill baseline was **52 passed**; the
new worker file adds **28 passing cases**. Boundary remains **175 errors /
9 warnings**. `git diff --check` passed. One new test fixture correction used
the existing two-argument `GrantError` constructor; no runtime retry fix.
Actual missing-client-config test proves zero grant/handler calls; allowing
tests mock the RPC solely to exercise both worker barriers, not to enable an
endpoint allow path. Existing effect cache prevents duplicate handler effects.
No live TLS/deployed-network/real approval allow proof is claimed.

Implementation/tests: **393 added/deleted lines**. Budget override: retaining
the separately reviewable plan/evidence above takes the documentation-inclusive
slice beyond 400; implementation/tests remain bounded below 400.
All admission/scheduling/lease holds remain ON. No store edits, config/SSRF
relaxation, shared DB apply, live keys, network policy or deployment changes.
Existing `redeem_grants` can swallow individual invalid-grant failures; this
slice gates authorization before redemption, not complete credential-set
validation. That behavior remains a release-proof item before any allow path.
Next: independent worker-hook review, then coordinated deterministic intent/
approval/inheritance and endpoint integration, not removal of holds.

Each child slice remains ≤400 changed lines. Do not remove plan-035 admission,
scheduling or lease holds until all 035 release proof is independently
approved. Exact-intent work remains non-public and unavailable meanwhile.

| Child | Scope | Owner / paths | Acceptance |
|---|---|---|---|
| 035c2a | Internal core decision endpoint, service-auth principal binding, persisted-state evaluator and endpoint tests. No worker client or network-policy edit in this child. | core-engineer; new internal API module, minimal router include, new API/evaluator tests. Exact path inventory after service-auth pre-edit gate. | ≤350 lines. Allow only authenticated tool-worker service principal; anonymous/user/other-service denied. Test exact binding and stale/revoked/cancelled/expired/approval mismatch denials; no secret/grant/handler effects. No public OpenAPI route. |
| 035c2b | Worker RPC client, pre-grant and final-entry call ordering, exact argument recheck and denial behavior; narrowly scoped worker-to-core service/port NetworkPolicy. | core-engineer; worker authorization client, `app/worker/tasks.py`, Helm network policy/template tests, focused worker tests. | ≤400 lines; two distinct calls, fail closed before grant and before handler; request/response field tamper and unavailable-RPC tests; assert zero grant redemption/handler on denial; worker still has no PostgreSQL DSN/egress. Rendered policy proves only the tool-worker workload gains API service/port ingress; route auth protects path-level access. |
| 035c2c | Exact-intent binding/approval-state integration and end-to-end disposable DB/outbox/grant proof; release hold review. | core-engineer; only after coordinated 035e implementation; exact paths inventoried. | ≤400 lines; persisted authority/invocation/approval and current policy agree at both checks; no grant/handler on stale state; old worker excluded; all public admission/scheduling/lease holds remain until complete proof. |

## First implementation slice: 035c2a

### Pre-edit gate and allowed paths

Before any code edit, identify and cite the existing service-auth mechanism
that produces a verified tool-worker workload identity at core. The repository
search found none. If absent or inadequate, stop for user selection; no new
auth environment setting, package, secret, credential provisioning, or network
rule is part of 035c2a.

After that gate, allowed paths are limited to:

* New `app/api/internal_run_authorization.py` — internal request/response
  schemas, route and current-state authorization decision helper; no public
  user identity fallback.
* Minimal `app/main.py` router registration only if needed.
* New focused tests for the endpoint and decision helper, at exact paths
  inventoried immediately before editing.

Do not edit `app/core/workflow.py`, `app/core/credentials.py`,
`app/core/run_restrictions.py`, `app/worker/tasks.py`, migration files, SDKs,
OpenAPI snapshots, network policy or plan 035 implementation files in c2a.
Do not add dependencies, settings/environment variables, database migrations,
or grant/handler behavior.

### Testable acceptance

- [ ] Caller identity comes only from the verified existing service-auth
  principal; no caller-supplied identity, tenant user bearer, dev fallback or
  envelope signing credential can authenticate the endpoint.
- [ ] If exact-intent invocation/approval state is not yet persisted by 035e,
  evaluator denies it; endpoint tests use explicit fixtures and c2c adds real
  state integration only after that contract exists.
- [ ] Public user auth alone cannot call the internal route; non-worker service
  principal cannot call it.
- [ ] Request validation rejects unknown version/operation, missing binding,
  malformed digest, expired envelope, and malformed identifiers without a
  policy allow.
- [ ] Decision compares tenant/run/invocation/tool/canonical-args digest/
  intent binding/pinned policy revision/registry fingerprint/plugin digest/
  expiry to authoritative persisted records and current eligibility.
- [ ] Unit/API tests deny missing run/invocation, wrong tenant, intent mismatch,
  argument mismatch, stale policy/plugin, inactive/cancelled/expired run,
  ineligible approval/invocation stage, and unavailable authority; no detail
  leaks across tenants.
- [ ] Response binds request id, operation and full binding digest; decision
  validity never exceeds signed envelope expiry. It is non-reusable and does
  not claim to lock global policy state.
- [ ] Internal route absent from public OpenAPI and generic SDK routes.
- [ ] No changes to worker grants, handlers, network policy, migrations,
  service settings or deployment; ≤350 changed lines.
- [ ] Targeted tests and boundary check introduce no failures vs recorded
  baseline; record the service-auth pre-edit evidence and exact test commands.

## Risks and rollback

* No existing worker service auth is found → stop before edits and ask for the
  auth mechanism; never substitute a user token or envelope signing key.
* RPC outage/stale response → deny; no fallback to DB/network or signed
  snapshot alone.
* NetworkPolicy is port-level, not URL-path-level → limit worker ingress to the
  API service/port and require service auth on the internal route; do not claim
  the policy itself isolates one route.
* Revocation after a point-in-time allow can race an external effect → state
  this residual risk; a global no-effects-after-revocation guarantee is not
  claimed.
* Rollback c2a removes the internal route and keeps 035 scheduling/public
  holds enabled. Never release a restricted run to workers without c2b/c2c.

## Decisions needed

No new conceptual seam decision remains: the user approved internal core RPC,
worker network access and service authentication. The concrete service-auth
identity remains an evidence gate: reuse an existing authenticated worker
identity if present; if absent, stop and obtain a specific user choice before
adding credentials, environment configuration or a dependency.

## Log

- 2026-11-09 architect: user approved the internal authorization RPC and
  required network/service authentication. Existing grant redemption and
  envelope are not live revocation checks. Exact auth mechanism not found in
  bounded repository search; c2a must verify/reuse one or stop for user choice.
