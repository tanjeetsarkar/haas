# ADR-0021: Run tool restrictions and exact invocation intents

* Status: proposed — generic capability authorized; exact-intent release gates remain in plan 035
* Roadmap items: authorization/dispatch hardening; no new roadmap identifier assigned
* Related requirements/threats: ADR-04, ADR-05, ADR-06, ADR-08, ADR-12; model argument substitution, authority expansion, approval replay
* Implementation: [plan 035](../plans/035-bounded-action-initiation.md)

## Context

Plugins can request runs today through the public run API, but the inspected
`app/main.py:CreateRunRequest` only exposes a per-run **skill** restriction.
The inspected worker offers schemas resolved from active skills, constructs signed
invocations, and validates repaired model arguments before invoking a handler.
Neither prompt instructions nor a skill selection is an exact tool/argument grant.

Plugins can use the proposed generic capability for a reviewed inquiry submission
(Atlas) or an incident notification (a separate incident-management plugin).
Recipient selection, inquiry content, incident templates, UI, and provider credentials
belong to those extensions, not core. The generic facility would exist if either
consumer were deleted.

Existing approval authorization includes separate `submitter` and `approver` roles
and rejects an authenticated principal deciding its own run. This must not change.
No approver credential has been supplied for the reported development connection.

Source observations are bounded: create, orchestration, worker execution, authorization,
and policy search results were inspected. Full storage, delegation, grants, branch
and recovery behavior has **not** been verified. See plan 035 gates; this ADR makes no
claim that the proposed security properties already hold.

## Decision

### 1. Additive public request contract

Extend `POST /v1/runs`, its OpenAPI schema and generic Python/TypeScript SDKs:

```json
{
  "prompt": "Record the reviewed action",
  "tools": ["notify_on_call"],
  "preload_skills": ["incident_notification"],
  "invocation_intent": {
    "tool_name": "notify_on_call",
    "arguments": {"incident_id": "inc-123", "recipient": "on-call"}
  }
}
```

Names in this example are illustrative extension declarations, not built-in tools.

* `tools: list[str] | null = null`: omitted/null preserves existing behavior;
  `[]` allows no tool, including skill activation and other control tools.
  Non-null lists contain unique, nonempty exact registered names, at most 128.
  No patterns, aliases supplied by callers, or automatic companion tools.
* Effective tools are the intersection of registered tools, pinned tenant-policy
  access, skills/unlocks, any inherited restriction, and requested `tools`.
  A request exceeding authority is rejected, not silently broadened or truncated.
  Dynamic skill activation never expands the run's tool ceiling.
* `invocation_intent: {tool_name: str, arguments: object} | null = null`:
  frozen single invocation, unknown object fields forbidden. Requires an authenticated
  submitter, an explicit `tools` list exactly equal to `[tool_name]`, and the target
  already unlocked by policy-permitted `preload_skills`. Otherwise reject admission.
* Intent runs take a deterministic workflow path, with **no model call**, no
  skill-activation fallback, no extra tool calls and no continuation after the one
  result. Always use existing human approval, even for a tool whose ordinary metadata
  does not require approval. No new dispatch or catalog endpoint.
* Existing pinned-agent configuration cannot be widened: `tools` and intent are
  run-level attenuation, not agent-configuration overrides. The resulting target
  must still pass the pinned configuration's skills and policy checks.
* Existing generic run/request size limits remain; add a 64 KiB UTF-8 limit on the
  canonical intent arguments. Do not place credentials in arguments.

Use the existing tenant-filtered `GET /v1/plugins` catalog if its candidate tool
metadata suffices. Capability unavailable to the caller remains unavailable.
Do not introduce a domain-specific or alternate privileged catalog.

### 2. Exact argument semantics

An intent's arguments are a JSON object, not model text and not repaired JSON.
Accept only JSON-native values, string keys, finite numbers and schema-valid content.
Serialize with sorted keys, compact separators, UTF-8/non-ASCII preserved, and
non-finite numbers forbidden. Array order, string whitespace, case, and explicit
nulls remain significant. No Unicode normalization or string trimming.

At admission validate against the pinned target tool's argument schema using
strict validation. Serialize the validated argument object in JSON mode with
defaults included and aliases matching dispatch. Require its canonical bytes to
equal the submitted object's canonical bytes: implicit defaults, coercion,
dropped extra keys, or validator transformations cause 422. The caller must
explicitly submit the full effective argument object; never silently rewrite a
reviewed intent. Reuse an existing canonical utility only if these semantics match.

Bind a versioned digest to tenant ID, submitter ID, pinned policy revision,
registry fingerprint, plugin digest, exact tool name and canonical arguments.
Use a domain-separated SHA-256 digest; no new dependency. Bind the run ID when
constructing the invocation binding inside the run-creation transaction.
Persist the complete restriction/intent and digest as server-owned immutable run
metadata. Never reconstruct authority from messages, prompts, tool results or a
client-supplied digest.

The approval preview, stored invocation, signed envelope, argument validation and
actual dispatched payload must describe the same canonical object. Include the
intent binding inside the approval's existing digested tool-call data and signed
envelope; do not break legacy batch digest verification. In the worker's intent
path, do not call `repair_tool_json`; revalidate strictly and compare canonical
bytes immediately before the native/HTTP/MCP/sandbox adapter handoff.
Any mismatch fails closed before credential redemption or handler execution.

### 3. Enforcement and recovery

Plugins can narrow their runs, never grant new authority. Apply the same restriction
helper to catalog/schema offering, invocation construction, transactional dispatch,
credential-grant issuance, worker verification, and final handler/adapter entry.
No exemptions for control tools, retry, approval resume, or dynamic skill unlock.
Approval is necessary but never overrides policy, revocation, schema or digest checks.

Restricted model runs may use multiple permitted tools. Exact intent runs may
execute only their one stored invocation. The effect key is stable across delivery,
lease changes and approval retries; reuse existing outbox/invocation machinery.
Unknown external outcomes are not blindly retried as new actions. This facility
does not promise provider-level exactly-once effects.

Children of restricted runs inherit the parent ceiling and can only narrow it;
server-side derivation is mandatory. Intent runs cannot delegate. If the existing
child-run API cannot persist trustworthy inheritance within this budget, reject
delegation from restricted parents until it can. Thread branches must not infer
permission from history; explicitly derived runs inherit attenuation, whereas a
new authenticated independent run receives fresh policy authorization.

Inspection found no suitable migrated run-owned JSON/config field. The operator
subsequently authorized one additive, reversible `runs.authority_json` migration,
nullable for legacy runs. Review and disposable-database migration round-trip
verification precede any shared-database upgrade. Never overload message history,
secret storage, or mutable agent configuration.
Mixed versions must reject restricted/intent work, not interpret absent metadata
as unrestricted. Legacy runs without the feature remain compatible.

### 4. Separate approval connection

Plugins can configure a server-only approval connection separate from submission.
Both inquiry and incident-notification consumers can use the existing approval
read/decision endpoints with a distinct approver identity.
No core role, authentication bypass, credential issuance or new approval API is
needed. No submitter fallback, impersonation, automatic approval or dev self-approval.

An extension's local server may accept an operator-provided
`HAAS_APPROVER_KEY` from its existing secret-loading mechanism. This is extension
configuration, not a new core setting. Keep it out of browser bundles, responses,
logs, local storage and source control. Where a shared browser server is used, do
not give submitter sessions a credential-switch button: require an independently
authenticated approver session/operator-only listener and enforce that boundary
server-side before forwarding any request.

Absent an operator-provided credential **and** a proven approver-session boundary,
approval review/decision remains unavailable on that connection. A run stays
pending or expires according to the existing workflow. Ask the operator for the
credential location, never read it opportunistically, mint it or reseed identity.

## Consequences (including rejected alternatives)

* Stronger run attenuation is reusable across extensions; core contains no inquiry,
  incident, recipient or provider behavior.
* Deterministic intents avoid asking a model to reconstruct reviewed arguments.
  Prompt whitelists, schema visibility alone, approval alone and skill-only
  restrictions were rejected as insufficient enforcement.
* Strict full-object arguments may require clients to obtain defaults from a
  public schema before review. No silent canonicalization after review.
* No new public/user endpoint, domain-specific API, runtime dependency, role or
  sandbox relaxation is proposed. The internal authorization RPC selected in
  the addendum below is not public and does not complete or supersede ADR-17
  service trust work.
* Credential, egress, revocation and endpoint trust gaps remain separate release
  gates. An exact payload is not proof of provider authorization or delivery.
* Rollback disables new admission first and drains/rejects pending restricted work;
  never replay it through workers that ignore restrictions.

## Addendum: live worker authorization for exact intents

* Status: user-selected design direction for plan 035c2; implementation is gated
  by an existing worker service-auth identity and the slice acceptance proof.
* Decision record: the user approved an authenticated internal core authorization
  RPC, with narrowly scoped tool-worker network reachability and service
  authentication. This addendum does not authorize public admission, intent
  scheduling, removal of existing 035 holds, shared-database changes, or
  deployment.

### Context

The current Redis credential grant is one-time (`GETDEL`) and checks grant
audience, plugin digest, invocation id, expiry and ciphertext integrity. It does
not check current run authority, approval or policy revocation. The Ed25519
tool envelope is an expiring dispatch-time snapshot; worker signature
verification does not establish current policy. Production tool workers have
Redis and configured egress, but no PostgreSQL network/DSN. Do not silently add
database access to workers.

### Selected contract

#### Approved concrete worker identity (2026-11-09)

User approved a dedicated Ed25519 request JWT, five settings below, key
distribution and HTTPS scope. Provisioning/deployment remain unauthorized.
This supersedes only the missing-service-identity pre-edit blocker above.

* Dedicated worker-only PEM private key; core trust JSON
  `{"keys":{"<kid>":{"subject":"haas:tool-worker","public_key_pem":"<PEM>"}}}`.
  No tenant credentials, envelope keys or dev fallback. Only EdDSA/Ed25519;
  issuer/subject `haas:tool-worker`, exact audience
  `haas:internal:run-authorizations:v1`. Key ID selects the authorized subject.
* JWT binds POST, exact RPC path, strict canonical JSON body SHA-256 and
  `jti=request_id`. Integer `iat`/`exp`, lifetime >0 and <=20 seconds, skew <=5.
  Core consumes subject/request ID through Redis `SET NX EX 60`; duplicate or
  unavailable replay state denies. Rotation cannot bypass the replay domain.
* Trust file reloads per check. Install new public key, roll workers, then remove
  old trust after token expiry/skew and rollout; emergency removal denies the
  next check. Private files must be regular and owner-only; trust files cannot
  be group/world writable. Trusted operator Secret-mount symlinks are supported.
* Authorized settings: `HAAS_INTERNAL_AUTHORIZATION_URL`,
  `HAAS_WORKER_AUTH_PRIVATE_KEY_FILE`, `HAAS_WORKER_AUTH_KEY_ID`,
  `HAAS_WORKER_AUTH_TRUST_KEYS_FILE`, `HAAS_INTERNAL_AUTHORIZATION_CA_FILE`.
  Worker receives URL/private key/kid/optional CA; core receives public trust
  mapping only. No live keys generated, inspected or provisioned by this slice.
* HTTPS certificate verification, no redirects/proxy inheritance/insecure
  fallback are mandatory for the later client. Only worker-to-core API port
  reachability is contemplated; no worker PostgreSQL access. Response trust
  derives from authenticated TLS, not another response-signing protocol.

Plugins can use a generic internal core endpoint at
`POST /internal/v1/run-authorizations/check`. It is not a public `/v1` route,
not part of public OpenAPI or SDKs, and is callable only by an authenticated
tool-worker service principal over a narrowly scoped worker-to-core network
path. Caller identity is derived from verified service authentication, never
from request fields. Reuse an existing authenticated workload/service identity
if present. The bounded repository search did not locate one; if no suitable
identity exists, stop and obtain a specific user decision before adding an
environment variable, dependency, key or credential-distribution mechanism.
Never reuse tenant-user bearer/API credentials, dev defaults or the envelope
signing key as worker authentication.

The v1 request binds:

* operation: `pre_grant` or `final_entry`;
* unique per-check request id;
* canonical tenant UUID, run id, invocation id and exact tool name;
* SHA-256 of strict canonical JSON arguments (the arguments themselves are not
  sent);
* persisted exact-intent binding;
* pinned policy revision, registry fingerprint and plugin digest; and
* signed envelope expiry.

Core loads authoritative run/invocation/intent records under tenant scope and
compares every submitted binding to persisted values. It checks current
eligibility, including live policy/revocation state, run status/cancellation/
deadline, invocation and approval stage, pinned plugin/tool authority, and
exact argument digest. Any missing, stale, malformed, legacy-unrestricted,
held, revoked or mismatched state denies.

The response echoes request id and operation, carries a digest of the complete
canonical binding, decision timestamp and a short `valid_until` no later than
the envelope expiry. It is an operation-scoped point-in-time decision, not a
reusable bearer permit, distributed lock or PostgreSQL transaction spanning an
external effect. Trust the response only over the authenticated service
channel; do not introduce a second signature key protocol.

Worker ordering is mandatory:

1. Verify the signed envelope, its expiry and raw argument digest.
2. Strictly parse/canonicalize exact-intent arguments and check the intent
   binding and canonical argument digest.
3. Call `pre_grant`; validate the authenticated response, request id, operation,
   binding digest, allow decision and live `valid_until`. On any error, deny
   before `GETDEL`, grant decryption, secret resolution or handler work.
4. Redeem only the already-scoped grant.
5. Immediately before native, HTTP/MCP or sandbox adapter handoff, recheck the
   exact argument/intent binding and make a fresh `final_entry` RPC. Never reuse
   the earlier decision. Deny before handler/adapter entry if the check fails.

An unavailable RPC, failed service authentication, denial, stale response,
expired decision or binding mismatch fails closed with zero grant redemption
at the pre-grant stage and zero handler/adapter calls at final entry. There is
no fallback to envelope-only authorization, unrestricted legacy semantics, or
worker PostgreSQL access.

### Consistency limit and consequences

Each RPC checks current authoritative state at a point in time. Revocation can
still occur after `final_entry` allow and before or during an external effect;
this is a TOCTOU window and the design does not promise global atomicity or
“no effects after revocation completes.” Closing that window requires a
separately designed authorization lease plus in-flight effect/revocation drain
protocol. Homogeneous rollout excludes old workers but does not close it.

Use the bounded implementation/test plan in
[035c2](../plans/037-live-final-entry-authorization-rpc.md). Until all 035
admission, scheduling, lease, approval, binding and worker denial proofs pass,
public admission and scheduling/lease holds remain enabled; this RPC design
alone does not release exact intents.
