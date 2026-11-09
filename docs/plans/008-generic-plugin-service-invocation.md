# 008 — Generic authenticated plugin-service invocation

- Status: proposed
- Type: epic
- Owner agent: core-engineer (one engineer per implementation slice)
- Epic: [001 Atlas extraction and SDK stabilization](001-atlas-extraction-sdk-epic.md)
- ADR: [ADR-17](../adr/0017-trusted-plugin-service-invocation.md) — proposed; limited 008a2a scope authorized, all broader implementation gated
- Design: [proposed security profile](../design/plugin-service-invocation.md) — unresolved operational parameters remain; only the internal injected-key primitive below is authorized
- Slice state: **008a1 completed** (contract only, user-reported); **008a2 BLOCKED** on the custody, distribution/revocation and destination replay gates below. **008a2a DONE** (internal primitive only; not production-ready). No other implementation or later slice is authorized.

## Goal

Design and, only after a separate future implementation-approval gate, deliver a generic core
capability for plugins to call independently deployed services with kernel-derived,
service-verifiable invocation scope and actually mediated egress. Atlas and
web-research are plausible consumers; incident-triage is another. This epic adds
no Atlas-specific core behavior and does not implement any consumer.

The 007 audit found no existing public path proving the complete identity,
credential, egress and approval contract. The completed first slice is
limited to the invocation identity contract and its tests; the capability is not
usable for non-sandbox service dispatch until 008b3 passes with secret forwarding
disabled; sandbox-runner use additionally requires 008c3. In
particular, 007 did not prove actual egress enforcement for
plugins executed by the existing sandbox subprocess/runner: passing policy data
to that subprocess is not proof that its network requests use the active core
egress enforcement path. Service invocation is disabled for plugins executed
through that runner until the distinct sandbox integration slice below passes
review and verification. This plan does not assert that the existing runner is
an OS/network security sandbox. Approval-dependent calls through this capability are rejected initially.
Atlas inquiry remains on its existing approved core path. Any future
approval-proof slice requires separate design and approval.

## Current mechanisms and trust gap

The audit's source anchors are recorded in [007](007-atlas-service-invocation-audit.md):

- `haas_plugin_sdk/__init__.py` exposes local context identity, `get_secret` and
  an untyped HTTP client, but no authenticated service-call/egress contract.
- `app/core/plugins.py` sends endpoint arguments with trace/effect metadata and
  optional secret-based auth/body HMAC. Those fields do not independently prove
  tenant, run, plugin, invocation or approval authority to a remote service.
- `app/core/envelope.py` and `app/worker/tasks.py` authenticate the internal
  worker handoff. `app/core/credentials.py` issues tool-audience grants. Neither
  internal trust boundary is proven to be a remotely verifiable,
  service-audience invocation grant.
- `app/core/egress.py` implements destination checks and mediated request logic,
  but 007 did not prove that native `http_client`, endpoint HTTP/MCP or sandbox
  transports all use that enforcement path. URL validation before a separate
  client is not actual egress mediation. The sandbox subprocess currently
  receives serialized egress-policy data; that alone does not establish
  transport wiring or enforced network egress. Also, the existing
  `q_tools_sandbox` routing label is not a separate worker or security boundary.
- `app/core/workflow.py` binds approvals to core dispatch state, but no tested
  public service path conveys exact approved arguments/destination for remote
  verification.

The gap is end-to-end proof, not absence of every component and not a claim that
the current components are exploitable. The new helper must be a distinct,
fail-closed path; the existence of these legacy mechanisms must not be used to
infer trust or to silently upgrade old callers.

### Custody and replay inventory

- [`pyproject.toml:21–22`](../../pyproject.toml) already declares
  `cryptography>=44.0.0` and `PyJWT[crypto]>=2.10.0`; library presence does not
  prove JOSE interoperability or correct verification.
- [`app/core/envelope.py:61–77`](../../app/core/envelope.py) and
  [`deploy/helm/haas/values.yaml:145–147,182–184`](../../deploy/helm/haas/values.yaml)
  describe an internal envelope signing key on the orchestrator and a
  verify-only public key on workers. Never reuse, export or derive the service
  assertion key from the envelope signing key.
- Helm [`values.yaml:3–7`](../../deploy/helm/haas/values.yaml) describes
  out-of-band Secrets via External Secrets Operator, Vault agent,
  sealed-secrets or Terraform. Terraform
  [`main.tf:97–112,129–141`](../../deploy/terraform/modules/haas-platform/main.tf)
  optionally provisions Kubernetes Secrets and recommends External
  Secrets/Vault in production; [`variables.tf:148–152,175–183`](../../deploy/terraform/modules/haas-platform/variables.tf)
  describes out-of-band provisioning and only the envelope signing/verify
  pair. These are provisioning/transport capabilities, not proof of a KMS/HSM
  API, service issuer integration, key rotation or revocation guarantee.
- [`app/core/webhooks.py:1–4,27–75,137–145`](../../app/core/webhooks.py)
  uses encrypted DB-backed tenant keys for HMAC webhooks with rotation support;
  that is not the proposed asymmetric assertion issuer.
- [007](007-atlas-service-invocation-audit.md) records Atlas as health-only,
  without business routes, DB/schema or service trust; no destination replay
  store is established. Inspected sources establish no service assertion key
  registry, lifecycle/distribution/revocation mechanism or durable atomic
  destination replay store. External deployment capabilities remain unknown.

**User-selected proposed directions, not implementation authorization:**
operator-managed KMS/HSM custody for the dedicated issuer key; operator-
distributed versioned public-key bundles with measured refresh and revocation
bounds; and each destination's own transactional database with durable atomic
consume before effects. No provider, key custodian, bundle distributor,
destination verifier operator or replay-store owner has been named. The
repository has no current KMS/HSM integration and no destination DB/replay
store established for Atlas, nor a bundle-distribution integration. External
Secret provisioning is not itself a KMS/HSM integration, key rotation or
revocation guarantee.

**Accepted proposed test targets (unverified):** assertion lifetime at most
60 seconds; key-bundle freshness at most 60 seconds; signing-key rotation
every 30 days; old public-key retention through the maximum assertion lifetime
plus clock skew of at most 5 seconds; destination replay-row retention through
`exp` plus clock skew; and atomic unique `(iss, aud, jti)` consume before any
effect. The user accepted these as targets for tests only, not verified
requirements or operational guarantees. The key custodian/provider, bundle
distributor, destination verifier operators and replay-store owners are all
**Unknown**. Owners and supporting mechanisms must be established and these
targets measured before 008a2 can proceed.

**008a2 unblock criteria:** name the key custodian/provider, verify the actual
KMS/HSM signing integration and issuer authorization, and agree measurable
key lifecycle, signing-availability, bundle-refresh and revocation-propagation
requirements. Name the bundle distributor and each destination verifier
operator; confirm stale/unavailable behavior and measured refresh/revocation
bounds. For each intended destination, name the replay-store owner and confirm
its own transactional DB supports durable atomic unique `(iss,aud,jti)`
consume committed before effects, including concurrency, transaction
atomicity, retention, availability/outage and crash/recovery requirements.
Record supporting evidence; do not infer provider capabilities or invent
owners. A destination that cannot meet the contract stays disabled. Destination
service schema/migrations and deployment are outside 008's core-only paths and
require separate ownership, planning and approval. A fake store in core tests
cannot establish destination durability. Specifically, record test evidence
against the accepted proposed numerical/atomicity targets above; passing those
tests does not itself establish operational guarantees. All gates remain
design-only, and separate explicit implementation approval is still required.
See the
[design](../design/plugin-service-invocation.md) for lifecycle and replay
considerations.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Authenticated service invocation context, scoped credential handoff and enforced egress | Core, generic | This kernel trust and dispatch facility would exist without Atlas; plugins can use it, including web-research and incident-triage |
| Atlas or other domain service routes, cache schema, tools and client behavior | Plugin/service, excluded | These would not exist if their respective plugin were deleted |
| Generic fake-plugin conformance scenarios | Core tests | They verify the extension contract for distinct plugin identities without adding plugin names or behavior to core |

## Scope and guardrails

**Allowed implementation paths, slice by slice:** the exact paths listed under
each epic row. Tests are limited to the exact new test paths listed there and
existing core test files named by the owning slice. No other owner paths are
authorized.

**Out of scope:** Atlas or web-research implementation/consumption; service
routes, schemas, storage or service migrations; REST/SSE API changes; TypeScript
SDK; trusted invocation over legacy raw `http_client` or stock HTTP/MCP paths;
changing existing legacy behavior; generic raw HTTP; core DB access to plugins;
new runtime dependencies; approval-dependent service effects; data movement,
UI changes or table retirement. The sandbox slice only integrates this
service-invocation transport with actual egress enforcement; it does not claim
to make all sandbox network activity safe or to change unrelated legacy
transports.

No schema change is planned (zero migrations). Each implementation slice must
remain at or below 400 changed non-generated lines. If measured scope exceeds
that budget, stop and split the slice before broadening it. The epic may not be
marked complete, and no consumer may begin, until all required slices below
pass. Each slice is gated by fresh baselines and approvals; this proposed plan
is not permission to implement beyond the explicitly authorized 008a2a child.
Plugins executed through the sandbox runner
must remain unable to use this service-invocation capability until 008c3 has
passed; passing 008a or 008b is not sufficient.

## Security contract required at epic completion

1. **Kernel provenance and identity:** derive tenant, plugin ID and immutable
   plugin revision, run, tool, invocation/effect identity and issue/expiry from
   verified core dispatch state. Neither model/tool arguments nor caller
   headers can set or override them. Bind the assertion to one configured
   service audience and to the exact request method, destination and canonical
   body digest. The service must verify the assertion independently.
2. **Cryptography and lifecycle:** user-selected proposed direction is a
   dedicated Ed25519 JWS with explicit key lifecycle. ADR-17 remains proposed;
   this selection does not authorize implementation. Claims, canonicalization,
   digest construction, key provisioning/custody/rotation/revocation, replay
   persistence, dependency support and verification semantics remain to be
   designed and proven. Candidate values below are hypotheses, not accepted
   parameters. Never reuse
   or disclose the core worker-envelope signing secret/key as service identity.
3. **Credential separation and attenuation:** service identity proves who/what
   is invoking; it is not a provider credential. Any downstream credential is
   a separate, named value, default-deny, limited to the active run/invocation,
   plugin revision, service audience and explicitly authorized names. The
   resolved names must be an intersection of run-authorized secret scope and
   service-specific grants. The selected proposed direction is no forwarding
   initially; any later forwarding requires a separately approved design/plan.
   It is not required to enable no-secret calls.
   Do not send undeclared names, core settings, raw
   stores, broad worker credentials or the identity-signing key. Never log
   credential values; redact them from errors and audit records.
4. **Actual egress enforcement:** every request made through the trusted helper
   must use the active core egress policy and mediated request path at the
   network connection, not just validate a URL before using a raw client.
   Resolve the configured audience to an allowed destination; enforce scheme,
   host/port, DNS/IP and rebinding defenses, redirect policy/revalidation,
   timeout, response/request byte limits and redacted metering. Missing policy
   or unresolved destination fails closed. Private-network access is denied
   unless separately and explicitly configured; no service host is hardcoded in
   core. Calls outside this helper, including legacy endpoint/MCP and direct
   `http_client` calls, do not acquire this trust contract and are not fallback
   paths. Sandbox use additionally requires the dedicated sandbox integration
   slice and end-to-end evidence that the actual sandbox transport is mediated.
   The selected proposed sandbox direction is authenticated host-broker IPC,
   but use remains disabled until its real enforcement tests pass. IPC
   authentication details and transport wiring remain to be designed and
   verified. Plugins executed through the sandbox runner must be denied use of
   this capability before any outbound request until that gate passes.
5. **Approval:** identity, idempotency and an effect key are not approval proof.
   The selected proposed direction rejects every approval-dependent call
   through this capability initially. Atlas inquiry stays on its existing
   approved core path; do not migrate it to the new helper. Any later proof
   requires a separately planned and approved slice. No approval-required
   request may silently use the non-approval path.
6. **Replay, isolation and observability:** reject wrong tenant, plugin/revision,
   run, tool/invocation, audience, destination/body, expiry and replay where the
   selected protocol requires it. Duplicate delivery is possible; do not promise
   exactly-once. Require a destination idempotency contract for effects.
   Record redacted tenant/run/plugin/revision/invocation/audience/destination
   outcomes and policy denials, with bounded quotas/bytes and no secret values.

## Proposed profile parameters for design review

The user-selected directions remain proposed. The detailed candidate profile
and rationale are in the [design note](../design/plugin-service-invocation.md);
the following defaults are hypotheses to accept or revise, not validated
implementation facts:

- **Assertion/request:** compact Ed25519 JWS with fixed `alg=EdDSA`, dedicated
  type and operator `kid`; candidate claims bind version, issuer/audience,
  tenant, plugin ID/digest, run, invocation, tool/operation, policy/catalog
  revision, exact method/destination/content type/body digest, issue/expiry and
  random `jti`. Candidate request digest is SHA-256 over a domain tag and
  fixed-order length-prefixed UTF-8 request fields plus exact body bytes.
  Candidate destination is the stored catalog URL, HTTPS/443, exact path,
  initially no caller query/path; methods/content initially exact catalog
  method and GET/no body or POST/application-json. Unsupported methods and
  encodings deny. Claims, encoding, JOSE library and receiver contract need
  independent vectors and review.
- **Keys/time:** user-selected proposed direction is operator-managed
  KMS/HSM custody and a dedicated issuer restricted to verified dispatch; no
  provider or integration is established, and the accountable custodian is
  **Unknown**. Never reuse worker-envelope keys. The accepted proposed test
  targets are 30-day rotation, old-key retention through maximum assertion
  lifetime plus at most 5 seconds of clock skew, ≤60-second assertion
  lifetime and ≤60-second key-bundle freshness. They are not verified
  requirements or operational guarantees. Confirm measurable lifecycle,
  signing-availability, refresh and revocation-propagation bounds with named
  operators; stale or unavailable trust state denies.
- **Replay/effects:** user-selected proposed direction is each destination's
  own transactional DB with durable atomic consume of `(iss,aud,jti)` before
  effects, retained through `exp` plus skew; outages deny. At-least-once
  retries use a fresh `jti` and stable effect key plus identical request
  digest. Persist effect idempotency separately; same key / different digest
  conflicts. Name each destination's store owner and confirm measurable
  atomicity, durability, availability, retention and recovery requirements.
  No destination DB/replay store is established for Atlas; exactly-once is not
  promised.
- **Catalog:** versioned operator-owned exact entries, immutable revision
  pinned at dispatch; candidate entry includes audience, HTTPS DNS host,
  explicit port 443, exact path, operation, method/content type and ceilings.
  Tenant entitlement intersects operator entry and plugin-declared operation;
  plugin declarations alone grant nothing. Deployment config avoids a new
  runtime DB contract but has rollout latency; DB/control-plane gives central
  updates but adds tenant isolation/RLS and availability design. Owner,
  source, provisioning, revocation/cache behavior, tenant/plugin policy
  semantics and any private-address exception remain undecided.
- **Sandbox IPC:** candidate is a host-created private per-invocation channel
  bound to the child process, preferably inherited socketpair or equivalent,
  with OS peer/channel authentication and no argv/env bearer token. A bounded,
  versioned protocol permits only authorized symbolic operations and bounded
  data; host derives scope and performs signing/network dispatch. Peer
  credential sockets/authenticated proxy are alternatives requiring platform
  review. The channel must close/fail closed on malformed input, scope/quota
  violation, process/channel/broker loss; no direct network fallback. Current
  007 source evidence shows a one-shot subprocess exchange, not authenticated
  live broker IPC. Keep the sandbox gate closed until real allow/deny/failure
  subprocess→broker→TLS evidence and deployment review pass.

These are proposed defaults to focus design review. Declared library
dependencies are inventoried above, but no source evidence verifies their
proposed JOSE behavior, KMS/HSM integration, service key lifecycle, catalog mechanism,
replay/effect transaction, IPC authentication or end-to-end network behavior.
007's audit findings and test limitations remain unchanged.

## Ordered slices

The former 008a/b/c/d groupings are split below; references to 008c passing mean
all 008c slices, through 008c3. 008a1 and child 008a2a are completed; all other
slices remain proposed, with 008a2 blocked. Each slice is ≤400 changed lines
including tests and zero migrations. Estimates are ceilings, not permission to
omit gates: stop and split further if exceeded. One engineer per row; core
capability first, no plugin consumption here. Paths are closed planned scope.
User acceptance and full slice framing/fresh baseline precede every build.

| Order | Slice | Allowed files / scope | Depends on / exit |
|---|---|---|---|
| 008a1 | **Completed** — typed claims and request binding (≤250) | `haas_plugin_sdk/__init__.py`, `app/core/service_invocation.py`, `tests/test_plugin_service_identity.py` | User reports contract-only implementation completed; no issuance/network. This documentation turn did not rerun or verify tests. |
| 008a2 | **BLOCKED** — dedicated signing and independent verification (≤350) | `app/core/service_invocation.py`, new `app/core/service_assertion.py`, `tests/test_plugin_service_identity.py` | a1; named custodian and actual key source; verified distribution/rotation/revocation; named owner and durable atomic consume contract at each destination; verified JOSE support and separate implementation approval. Test against the accepted proposed targets (≤60s assertion lifetime and bundle freshness, 30-day rotation, old-key retention through max assertion lifetime plus ≤5s skew, replay-row retention through `exp` plus skew, atomic unique `(iss,aud,jti)` consume before effects); these are test targets, not operational guarantees. Destination durability needs destination-owned evidence. No network enablement. |
| ↳ 008a2a | **DONE** — internal injected-key Ed25519 v1 primitive; implementation and verification complete, primitive only (250–300) | New `app/core/service_assertion.py`, new `tests/test_plugin_service_assertion.py`; framing in this plan only | a1; limited scope user-approved and completed. No key source, issuance, replay or dispatch. Passing this child does **not** unblock a2 or satisfy any operational gate. |
| 008b1 | Verified dispatch + exact catalog resolution (≤350) | `app/core/service_invocation.py`, new `app/core/service_catalog.py`, `app/core/context.py`, `app/worker/tasks.py`, new `tests/test_plugin_service_dispatch.py` | a2; catalog owner/provisioning/wiring/revision and tenant/plugin intersection reviewed; reject unknown/approval-required state and all secret export. Disabled transport. |
| 008b2 | Strict broker service profile (≤350) | `app/core/egress.py`, `app/core/service_invocation.py`, new `tests/test_plugin_service_egress.py` | b1; approved limits/private exceptions; no inherited exemptions, redirects, raw clients or missing meters; pre-send request budget tests. Disabled public helper. |
| 008b3 | Real native transport proof and enablement gate (≤300) | `app/core/service_invocation.py`, `app/core/context.py`, `app/worker/tasks.py`, `tests/test_plugin_service_dispatch.py`, `tests/test_plugin_service_egress.py` | b2; real TLS service verifies bytes/scope/replay; actual broker DNS/connect/TLS, limits and failure matrix pass. Native no-secret helper only after review/user gate; HTTP/MCP and sandbox remain disabled for trusted mode. |
| 008c1 | Live IPC contract, host/channel identity checks (≤250) | `app/sandbox/protocol.py`, new `app/sandbox/service_transport.py`, new `tests/test_plugin_service_sandbox_egress.py` | b3 and designed/verified channel authentication, process binding, permissions, framing, quota/lifetime and shutdown; protocol tests only, sandbox still disabled. |
| 008c2 | Subprocess-to-host broker wiring (≤350) | `app/sandbox/execution.py`, `app/sandbox/runner.py`, `app/sandbox/launcher.py`, `app/sandbox/service_transport.py`, `app/core/service_invocation.py`, `tests/test_plugin_service_sandbox_egress.py` | c1; bounded channel, host derives scope; no child key/credential, no direct fallback; sandbox still disabled. |
| 008c3 | Real sandbox allow/deny/failure and deployment gate (≤300) | `app/sandbox/service_transport.py`, `app/core/service_invocation.py`, `tests/test_plugin_service_sandbox_egress.py` | c2; real subprocess→broker→TLS tests and reviewed transport deployment; child spoof, channel/broker loss, no denied service hits. Explicit approval before sandbox use. |
| 008d1 | Future service grants and atomic redemption (≤300) | `app/core/credentials.py`, `app/core/service_invocation.py`, new `tests/test_plugin_service_credentials.py` | b3; separate export approval/authority and narrowing design. Off by default; service-audience grant is not a tool grant. |
| 008d2 | Future credential injection/redaction (≤300) | `app/core/service_invocation.py`, `app/core/credentials.py`, `tests/test_plugin_service_credentials.py` | d1; fixed TLS-only headers, scope/expiry/partial failure/leak tests; no raw public accessor. |
| 008d3 | Future actual credential transport proof (≤250) | `app/core/service_invocation.py`, `tests/test_plugin_service_credentials.py`, `tests/test_plugin_service_sandbox_egress.py` | d2; real intended-service delivery/no redirect leak. Sandbox variants require c3; separate review/user gate. Approval proof still excluded. |

**Completed slice 008a1:** core-engineer owned the three listed paths; ≤250 lines,
zero migrations. Its contract scope was typed required claims and deterministic
binding-byte/digest construction from already verified context. Acceptance:
two fake plugins share the contract; absent required claims and ambiguous URLs
reject; same emitted bytes give the same digest, changing method/destination/
content type/body changes it; no caller identity override, signer, secrets or
outbound call. Run the identity module and touched-file lint/type/boundary gates
against a fresh baseline. Rollback is additive contract reversion; all dispatch
stays disabled. The user reports completion; this turn has not rerun its gates.
Cryptographic, replay and kernel-dispatch evidence belongs to later rows and
does not follow from a1 completion.

### Child slice 008a2a — internal injected-key Ed25519 primitive

- Status: **DONE — implemented and verified (primitive only); not production-ready**.
- Authorization: on 2026-11-08 the user explicitly approved implementation of
  this limited primitive only. This completion does not approve full 008a2,
  any later slice or ADR-17.
- Type: child slice under 008a2, in epic 008; no new numbered plan.
- Owner: core-engineer; ADR: [ADR-17](../adr/0017-trusted-plugin-service-invocation.md).
- Budget: target 250 changed lines, **hard ceiling 300**, including tests;
  zero migrations. Stop and return for a smaller split if the ceiling cannot
  cover all acceptance criteria; never omit negative tests to fit.

#### Goal and boundary classification

Prove deterministic Ed25519 compact-JWS signing and strict verification of the
existing v1 claims with caller-injected dedicated keys only. Plugins can
eventually benefit from independently authenticated service assertions;
web-research and incident-triage are other plausible consumers. This primitive
is generic kernel cryptography and would exist if Atlas were deleted. It is
**not** a plugin extension API, issuer, authorization decision or service call.
The normal public-extension-surface step is intentionally deferred: plugins
receive no signer callable, private key, key resolver or new SDK capability.

#### Scope — closed allowed file list

Implementation may add **only**:

- `app/core/service_assertion.py` — core-only primitive and private helpers.
- `tests/test_plugin_service_assertion.py` — deterministic offline unit/vector
  and rejection tests, with fixtures embedded in this file.

Plan/evidence updates may edit only
`docs/plans/008-generic-plugin-service-invocation.md`. Existing
`app/core/service_invocation.py`, `haas_plugin_sdk/__init__.py` and
`tests/test_plugin_service_identity.py` are read-only dependencies, not edit
permission. Do not edit dependency manifests/lockfiles or add dependencies:
`cryptography>=44.0.0` and `PyJWT[crypto]>=2.10.0` already exist.

Explicit exclusions: production key source/custody adapters, KMS/HSM, env/file
key loading, generated runtime keys, bundles/caches/distribution/revocation,
replay store or consumption, nonce generation, issuance or dispatch-derived
claims, dispatch imports/wiring, network, catalog, egress, sandbox, consumers,
public SDK exports, settings, logs/telemetry, database and deployments.
No general signing callable is available to plugins. Never inspect or touch
`haas_sdk_ts/src/lib/atlasClient.ts`; no TS build or protected-boundary scan.
Preserve all pre-existing staged/unstaged/untracked work; no stash, clean,
reset, checkout, commit or unrelated repair.

#### Exact accepted internal API

These are the only callable entry points added, in `app.core.service_assertion`
only (no re-export, registration, context injection or plugin imports):

```text
sign_service_assertion(
    claims: ServiceInvocationClaims, *,
    kid: str, private_key: Ed25519PrivateKey
) -> str

verify_service_assertion(
    token: str, *,
    trusted_keys: Mapping[str, Ed25519PublicKey],
    expected_issuer: str, expected_audience: str,
    now: int, clock_skew: int = 5
) -> ServiceInvocationClaims
```

`ServiceInvocationClaims` is the existing SDK claim type; key types come from
`cryptography.hazmat.primitives.asymmetric.ed25519`. Inject actual Ed25519 key
objects, not PEM/raw bytes, a generic signer callable, provider or resolver.
Verification accepts public keys only; reject private/wrong-algorithm objects
and empty mappings or invalid map entries (including invalid `kid` names).
Snapshot the supplied mapping for the call; it is
caller-provided trust input, not a verified bundle or key registry.
All invalid input/signature/decoding failures surface as sanitized `ValueError`,
without token/claim/key values or chained sensitive exception text.

Use existing `validate_service_invocation_claims` without weakening/changing
008a1. Signing accepts only already-built claims, copies/validates them, and
adds no claims, nonce, timestamps or defaults. It cannot attest provenance.
Verifier returns a fresh validated claims mapping, not an authorization result.
It checks exact issuer/audience and time; resource/tenant/plugin/request
authorization and replay rejection remain absent and out of scope.

#### Canonical bytes and strict validation

1. Protected header has exactly `alg`, `kid`, `typ`; values are `EdDSA`,
   injected `kid`, `haas-service-invocation+jws`. `kid` must be ASCII and match
   `[A-Za-z0-9_-]{1,128}`. Require exact case-sensitive membership in injected
   trusted keys; no fallback, prefix matching, token keys or network resolution.
   Reject extras, including `crit`, `b64`, `jku`, `jwk`, `x5u`, `x5c`,
   wrong/missing `typ`, any other algorithm and duplicate header keys.
2. Header and payload bytes are UTF-8 of Python JSON encoding with
   `sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=True`,
   `allow_nan=False`, no BOM/newline. Header byte form is
   `{"alg":"EdDSA","kid":"<kid>","typ":"haas-service-invocation+jws"}`.
   Payload contains exactly the existing 20 v1 claim names:
   `v`, `iss`, `aud`, `tenant_id`, `plugin_id`, `plugin_digest`, `run_id`,
   `invocation_id`, `tool_name`, `operation`, `effect_key`, `policy_revision`,
   `catalog_revision`, `method`, `destination`, `content_type`,
   `request_sha256`, `iat`, `exp`, `jti`; lexicographic encoding order.
   No Unicode normalization or claim rewriting.
3. Require exactly three nonempty compact segments; canonical unpadded
   base64url alphabet and identical decode/re-encode for each segment.
   Signature is exactly 64 decoded bytes. Signing input is ASCII
   `base64url(header_bytes) + "." + base64url(claims_bytes)`, signed directly
   with Ed25519 (not a separately prehashed input). No detached/unencoded JWS.
4. Strict JSON object decoding rejects duplicate keys at every level,
   arrays/scalars, malformed UTF-8/JSON, non-finite numbers, extra/missing
   claims and wrong types. Require both decoded JSON byte sequences to equal
   their canonical re-encoding; even validly signed noncanonical JSON denies.
   Existing validation enforces integer `v=1` (not bool), integer dates
   (not bool/float/string), canonical request fields, 32-byte digest and
   16-byte `jti` encodings. Additionally require every string claim nonempty
   and at most 2048 characters, and compact token ASCII length at most 16384;
   signer enforces the same limits. No new GET/POST/catalog policy is imposed.
5. `expected_issuer` and `expected_audience` are required nonempty strings,
   compared exactly, with a scalar string `aud`, never an audience array.
   `now` is an explicit nonnegative integer, never a system-clock read.
   `clock_skew` is an integer in `[0,5]`, not bool. Require nonnegative dates,
   `0 < exp - iat <= 60`, `iat <= now + clock_skew` and
   `exp > now - clock_skew`. Validate lifetime on signing too; signing has
   no wall-clock admission check. These are primitive test bounds only.
6. Verify signature with only the exact selected public key before returning
   any claims. No auto-negotiation or JWT-library implicit defaults. Prefer
   `cryptography` for exact-byte Ed25519 operations and strict stdlib parsing;
   use existing `PyJWT[crypto]` as the independent fixture/interoperability
   producer, not as a substitute for canonical/schema checks.

#### Key separation and limits of evidence

Keys must be dedicated service-assertion test material injected by the trusted
core caller; never reuse, derive from, export or load envelope signing keys,
tool-grant/webhook HMAC keys, provider credentials or any other secret.
Tests use fixed published/test-only keys, never repository/operator secrets.
The primitive cannot detect reuse of identical key material: separation is
an architectural caller obligation, not a claimed cryptographic guarantee.
No private material appears in output/errors, SDK/context, logs or child
processes. No key is fetched/generated at import time or retained globally.
Repeated verification may succeed: this slice deliberately provides **no**
replay defense, issuer provenance, bundle freshness, revocation guarantee,
effect admission or production readiness.

#### Acceptance criteria

- [x] Exact internal API above, only the two new runtime/test files, no
  production importer/wiring or SDK export; review the exact diff.
- [x] Hard-coded independently produced full-profile known vector records
  test seed/public key, canonical header/payload bytes, signing-input bytes
  and expected compact token/signature with producer/version/provenance.
  Generate/reference it with independent PyJWT tooling, not the production
  signer or its helpers; embed final expected bytes, do not calculate the
  oracle during the test. Primitive signing equals it byte-for-byte and
  verification accepts it. Independently verify primitive output using PyJWT
  with explicit `algorithms=["EdDSA"]`, issuer/audience and fixed-time fixture
  handling. RFC 8037 Appendix A supplies Ed25519 JWS reference context;
  its differently shaped header/claims must not bypass our strict profile.
- [x] Two injected dedicated key pairs: correct `kid`/key succeeds;
  absent/unknown/case-changed `kid`, wrong public key, wrong key type,
  empty/invalid key map, invalid/missing/oversized `kid` fail. No fallback.
- [x] Header, payload/scope/digest and signature bit tampering fail; validly
  signed disallowed headers also fail (`none`, HS256/RS256, missing/wrong
  `typ`, extras/key URLs/critical extensions, duplicate keys).
- [x] Validly signed invalid claims fail: missing/extra names, v2/bool v,
  date coercions, audience array, empty/oversized strings, bad digest/jti
  alphabet/padding/length/case-preserving round-trip, noncanonical URL/method,
  wrong expected issuer/audience and malformed/duplicate/noncanonical JSON.
- [x] Future/expired/overlong/nonpositive lifetime and skew/type failures;
  exact 60-second and 5-second boundaries; expiry equality rejects.
  Compact segment count, padding/noncanonical base64url, bad signature
  length, UTF-8 and token-size failures; rejection errors contain no inputs.
- [x] Input claims/key map are unchanged; returned claims are a fresh copy.
  Same canonical inputs give identical tokens; changing a claim changes the
  signature. Repeated verify succeeds, explicitly documenting no replay store.
- [x] Targeted tests and lint/types introduce no new failures. Scoped Python
  boundary counts do not increase; full/protected TS scan is **not run**.
  No freshness/rotation/replay/dispatch acceptance is inferred from a pass.

#### Fresh baseline and verification rungs

Before the first runtime edit, record `git status --short` and scoped diffs
without inspecting protected file contents; retain the initial worktree.
Rungs 1/2/3/5 are scoped below. No TS/API/migration/network/DB rung applies.
Run baseline:

```text
git status --short
git diff --stat -- app/core/service_invocation.py app/core/service_assertion.py tests/test_plugin_service_identity.py tests/test_plugin_service_assertion.py docs/plans/008-generic-plugin-service-invocation.md
.venv/bin/ruff check app/core/service_invocation.py tests/test_plugin_service_identity.py
.venv/bin/pyright app/core/service_invocation.py
.venv/bin/pytest tests/test_plugin_service_identity.py -q
```

Boundary rung: the normal checker has no safe path-exclusion CLI; do **not**
run it normally or with `--plugin` (that filters tokens, not scanned paths).
Use this same read-only, closed Python-path scan before and after; no governance
file edits and no protected TS reads:

```text
.venv/bin/python -c 'import runpy; m=runpy.run_path(".github/skills/core-boundary-check/scripts/check_boundaries.py", run_name="scoped_boundary"); original=m["iter_files"]; m["scan"].__globals__["iter_files"]=lambda paths, skip, exts: original(["app/core/service_invocation.py", "app/core/service_assertion.py"], skip, exts); f=m["scan"](None); e=sum(x.level=="ERROR" for x in f); w=sum(x.level=="WARN" for x in f); print(f"Scoped boundary: {e} errors / {w} warnings"); raise SystemExit(bool(e))'
```

Post-change, repeat baseline commands (including scoped boundary), then:

```text
.venv/bin/ruff check app/core/service_assertion.py tests/test_plugin_service_assertion.py
.venv/bin/pyright app/core/service_assertion.py
.venv/bin/pytest tests/test_plugin_service_identity.py tests/test_plugin_service_assertion.py -q
git diff --check -- app/core/service_assertion.py tests/test_plugin_service_assertion.py docs/plans/008-generic-plugin-service-invocation.md
git diff --numstat -- app/core/service_assertion.py tests/test_plugin_service_assertion.py docs/plans/008-generic-plugin-service-invocation.md
git status --short
```

Account separately for new/untracked file lines (git diff alone omits them);
no staging merely to measure size. Record baseline and final error counts,
test IDs/pass/fail counts, installed crypto-library versions and actual vector
producer. New-file gates have no pre-existing baseline; all must pass.
Full-suite rung 10 remains an integration prerequisite for core changes:
if its environment is unavailable, record ENV/not run and do not claim
integration-ready. Never widen this child to fix existing failures.

Baseline before edit: identity plus phase-3 tests **34 passed**; baseline Ruff
passed; invocation Pyright **0 diagnostics**. The pre-edit scoped boundary
scan was **0 errors / 9 warnings**. Parent verification reports **74 combined
identity, assertion and phase-3 tests passed**; identity-only **21 passed**.
Ruff, Pyright (**0 diagnostics**) and `git diff --check` are clean; scoped
boundary remains **0 errors / 9 warnings**. PyJWT **2.15.1**, cryptography
**50.0.2**. The frozen vector was generated with PyJWT `jwt.encode` for EdDSA,
seed `00..1f`, test-only public key recorded in the test, sorted v1 claims,
and matches the primitive byte-for-byte. New runtime/test files total exactly
**300 lines** (145 + 155). Full pytest suite and normal/protected boundary scan
were not run; no production readiness, custody, bundle or replay guarantee is
inferred. Operational owners remain Unknown. These results complete the
008a2a acceptance criteria only; 008a2 remains BLOCKED.

#### Risks, rollback and decisions

- Round-trip-only tests can share a bug → independent frozen byte vector plus
  explicit-key PyJWT interoperability, strict negative tests.
- Internal signer mistaken for issuance/trust → no import/wiring/public
  surface; review dependencies and state the missing operational guarantees.
- Runtime injected private key mistaken for production KMS custody → tests/
  internal primitive only; future HSM interface may differ and needs approval.
- Rollback: remove/revert only this child's additive changes, preserving all
  unrelated work and the completed a1 contract. No migrations, flags, rollout
  or existing behavior to undo; use a reviewed inverse patch if uncommitted.
- Decisions needed for this child: **none**; limited scope already approved.
  Operational key custodian/provider, bundle distributor/verifier operators
  and destination replay owners remain **Unknown**. Full 008a2 stays
  **BLOCKED**; do not choose owners/providers or authorize any other slice.

### Epic acceptance criteria

- [ ] A fake Atlas-shaped plugin and a fake web-research-shaped plugin use the
  same generic public contract; core contains no plugin IDs, route names,
  domain fields or service host defaults.
- [ ] Remote verification proves the authenticated tenant, plugin/revision,
  run, tool/invocation, exact configured audience and request binding; caller-
  controlled arguments/headers cannot alter any claim.
- [ ] Tenant A cannot invoke under tenant B; plugin X cannot use plugin Y's
  identity or service grants; wrong audience, run, invocation, revision,
  destination, body, expired and replayed requests are rejected as required by
  the approved protocol.
- [ ] Identity assertions contain no provider credentials; initial helper denies
  every export request. The following credential gates apply only to future 008d:
  only the exact
  authorized, separately scoped credential names reach the intended audience;
  undeclared, expired, wrong-run and wrong-audience grants fail closed. No
  credential value or core signing secret appears in logs/errors.
- [ ] A real call through the new helper reaches the configured test service
  only via the active egress enforcement path. Tests demonstrate denial for
  missing policy, disallowed/private destination, DNS/rebinding violations,
  forbidden redirect, disallowed port/scheme and request/response limit
  violations; safe permitted traffic succeeds. Tests must exercise enforcement,
  not merely mock the URL validator.
- [ ] Plugins executed through the sandbox runner cannot use service invocation
  before slice 008c passes; attempts are rejected before outbound dispatch.
  After 008c, a test executes a
  tool through the real sandbox subprocess and real helper-to-egress transport:
  the configured test service observes allowed traffic, while denied requests
  produce no service-side hit and fail closed. Exercise missing/disallowed
  policy or destination, applicable DNS/rebinding and redirect denials, limits,
  permitted traffic, and transport/broker unavailable or failed modes. Assert
  the enforcement outcome at the transport/broker and test-service boundary;
  mocked validators, mocked transport calls, source inspection or the presence
  of policy fields in the sandbox protocol are not sufficient evidence.
- [ ] Sandbox-runner service invocation remains disabled unless the sandbox
  integration tests pass, are reviewed, and the deployment uses the verified
  transport configuration. This gate does not assert that unrelated sandbox
  network traffic or legacy HTTP/MCP paths are mediated.
- [ ] An approval-required invocation without service-verifiable approved
  binding is denied before network dispatch. The epic explicitly does not
  authorize an approval-dependent consumer; exact approval proof requires a
  separately approved plan and ADR-17 amendment.
- [ ] No changes to REST/SDK wire contracts, database schema, migrations,
  legacy tool names or existing consumer behavior; no new core dependencies.
- [ ] For each slice, all targeted tests pass and boundary counts do not
  increase from the fresh baseline. Record the pre/post counts and actual
  commands in the implementation handoff.
- [ ] `check_boundaries.py` still has no plugin-specific core references
  (baseline overall errors/warnings: 155/9; Atlas filter 0/0; atlas-explorer
  filter 134/1). Existing debt is not this epic's scope.

## Verification rungs

Fresh baseline is required immediately before each implementation slice. Exact
targeted commands (new test modules are created only by authorized
implementation):

```text
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas-explorer
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py
.venv/bin/pytest tests/test_plugin_service_identity.py -q
.venv/bin/pytest tests/test_plugin_service_dispatch.py -q
.venv/bin/pytest tests/test_plugin_service_egress.py -q
.venv/bin/pytest tests/test_plugin_service_sandbox_egress.py -q
.venv/bin/pytest tests/test_plugin_service_credentials.py -q
.venv/bin/pytest tests/test_core.py -q -k 'ssrf or secret_scoping or mcp or manifest_blocks_internal'
git diff --check -- <exact paths changed in that slice>
```

Run only the new module(s) for the slice being implemented and existing
selectors relevant to files it touches. Ruff and Pyright run on the touched
Python paths per the verification ladder. Do not run broad or DB-backed tests
without recording prerequisites and obtaining any required environment/user
approval. No database or migration test is expected because this plan adds no
schema.

## Baseline

Reported by 007 (2026-11-08; not freshly rerun for this proposed plan):

```text
Boundary check --plugin atlas:          0 errors / 0 warnings
Boundary check --plugin atlas-explorer: 134 errors / 1 warning
Overall boundary check:                 155 errors / 9 warnings
Targeted core selector:                 16 passed
Atlas plugin selector:                  18 passed
Phase-3 units:                          13 passed
Identity/approval selector:             5 passed
Phase-3 workflow:                       18 collected only; not run
Service test environment:               unavailable; services/atlas-explorer/.venv missing
DB-backed identity tests:                not run
Documentation diff-check:               passed (user-reported)
```

The 007 report did not preserve exact selector strings/IDs for all reported
credential/context, SSRF, envelope and identity tests. They are reported
counts, not independently reproducible commands from this plan. Missing service
and DB test evidence remains missing, not a pass. Before each implementation
slice, recapture the baseline; do not treat unrelated pre-existing failures as
new regressions.

## Risks and rollback

- Assertion/key design chosen prematurely → keep ADR-17 proposed and block 008a2
  until the user/security owner selects and documents format, verification and
  lifecycle.
- Existing envelope signing key reused or leaked → distinct service trust
  material is mandatory; negative tests assert non-disclosure.
- URL validation mistaken for egress enforcement → require observed mediated
  request execution with destination/DNS/redirect/byte-limit negative tests.
- Serialized sandbox policy mistaken for enforced sandbox egress → keep service
  invocation unavailable to plugins executed through the sandbox runner until
  008c demonstrates actual
  subprocess-to-enforcement transport behavior in deny, allow and failure tests.
- Credential confused with identity → two separate channels, default-deny
  names, scope and redaction tests; no broad secrets.
- Approval bypass through the helper → fail closed for approval-required
  invocation; keep inquiry and other approval-dependent consumers deferred.
- Test coverage depends on missing isolated service or database environments →
  first slices use deterministic fake services; any DB-backed requirement is
  explicit and cannot be marked passed when unavailable.
- Rollback: each additive core slice is independently revertible, adds no
  migration and leaves legacy paths/consumers unchanged. Disable the new helper
  by default until the full capability is reviewed; no partial slice may turn
  legacy paths into trusted dispatch.

## Decisions needed

The user has selected these directions as **proposed design decisions**; none
is an accepted ADR or implementation authorization:

- Dedicated Ed25519 JWS with explicit key lifecycle.
- Operator-owned exact-destination catalog, strict HTTPS and fail-closed.
- No provider-credential forwarding initially; any later forwarding requires
  separate approval.
- Reject approval-dependent calls through this capability initially; preserve
  Atlas inquiry on its existing approved core path.
- Authenticated host-broker IPC for sandbox use only after real enforcement
  tests pass; until then sandbox service invocation stays disabled.

The selections settle direction only. The following remain design/verification
gates and must not be filled in by assumption:

1. **Separate future implementation approval (beyond 008a2a):** A) user explicitly approves
   implementation after all design/verification gates below are resolved; B)
   continue design/audit only; C) defer the capability. **Recommend B until the
   gates are resolved.** This plan and ADR-17 remain proposed; 007's done status
   or reviewer approval does not authorize implementation. A later explicit
   approval is required even if all parameters are resolved. The user has
   already approved only the internal injected-key primitive in 008a2a above;
   it resolves no operational gate and does not advance epic sequencing.
2. **JWS profile:** A) candidate fixed EdDSA header/claim set and
   length-prefixed exact-request digest in the design note; B) a different
   deterministic encoding (for example a standards-based canonical JSON
   representation) if independent verifier/library evidence favors it.
   **Recommend A as the starting profile** because it binds exact emitted
   bytes without depending on JSON reserialization; this is a hypothesis, not
   a proven advantage until test vectors/interoperability tests pass. Resolve
   claims, URL representation, method/content set, expiry/skew, JOSE library
   and receiver contract before 008a2; a1 is completed.
3. **Key custody/source:** the user-selected proposed direction is
   operator-managed KMS/HSM custody. Name the KMS/HSM provider and accountable
   key custodian; demonstrate the actual signing integration, issuer
   authorization, lifecycle/rotation and availability/compromise procedures.
   No provider or owner is currently identified, and the repository has no
   current KMS/HSM integration. External Secret provisioning is not KMS/HSM
   custody or a rotation guarantee. If this direction cannot be provided,
   return for a user decision rather than silently substituting another
   mechanism. Never use envelope or webhook keys.
4. **Public-key distribution/revocation:** the user-selected proposed
   direction is operator-distributed versioned bundles. Name the distributor
   and each destination verifier operator; establish measurable bundle refresh,
   revocation-propagation and stale/unavailable behavior, including rotation
   and compromise evidence. Numerical bounds remain unselected; keep
   invocation disabled until agreed bounds can be demonstrated.
5. **Replay ownership/storage:** the user-selected proposed direction is each
   destination's own transactional DB with atomic unique consume before effect.
   Name the owner and store for every intended destination; establish
   measurable transaction atomicity, durability, availability, retention,
   outage and crash/retry recovery requirements. Atlas currently has no
   destination DB/replay store established by repository evidence. A
   destination that cannot prove the contract remains disabled; destination
   service work needs its own plan and approval.
6. **Catalog and egress wiring:** determine catalog owner/source, provisioning,
   immutable revision pinning, runtime wiring, policy/tenant narrowing, exact
   entry fields and operational revocation. Options are deployment config
   (simpler/no new DB contract, with rollout latency) or DB/control-plane
   catalog (central updates but new tenant isolation/RLS/availability work).
   **Recommend deployment config initially if it meets revocation needs;**
   otherwise return for a separately scoped catalog design. Verify actual
   TLS, DNS/pinning, redirects, proxy behavior, limits, metering and
   concurrency. Exact owner and policy fields remain open.
7. **Sandbox IPC:** specify authentication/peer binding, framing, channel
   lifetime, quotas, shutdown and deployment behavior. Options include a
   private host-created process-bound socketpair/channel, peer-credential
   authenticated local socket, or authenticated proxy; select only after
   platform review. **Recommend private host-created channel if process
   binding is demonstrable; otherwise keep disabled.** Prove real
   subprocess-to-broker allow/deny/failure behavior; do not substitute
   unverified proxy/fallback behavior.

The selected credential and approval directions are not reopened as choices
here: any later credential forwarding or approval-dependent service invocation
requires a separate plan and explicit approval. If any gate is unsupported
within the listed paths/budget, revise or split the plan and return for a
decision; do not add dependencies, schema or fallback behavior by assumption.

## Log

- 2026-11-08 architect: proposed after 007 final reviewer approval. ADR-17
  remains proposed. No implementation authorization implied.
- 2026-11-08 architect: add a dedicated sandbox transport/enforcement slice
  after generic identity and broker dispatch; sandbox service invocation stays
  disabled until that slice's real-transport deny/allow/failure tests pass.
  ADR-17 remains proposed; no runtime changes or implementation authorization.
- 2026-11-08 architect: design-only security profile added; recommend dedicated
  JWS identity/exact catalog, no initial credential export, approval rejection,
  and gated host IPC; split implementation groups into bounded dependent rows.
  No commands run; 007 status/evidence unchanged; all implementation prohibited
  pending separate user acceptance and slice authorization.
- 2026-11-08 user: selected the five directions recorded above as proposed
  design decisions only. Plan 008 and ADR-17 remain proposed; claims and
  canonicalization, key lifecycle details, replay persistence, catalog
  ownership/wiring and IPC authentication remain unresolved. Separate explicit
  implementation approval is still required; no runtime work authorized.
- 2026-11-08 user: chose continue_design. Candidate parameter proposals and
  their alternatives/source limits are documented here and in the security
  profile. This does not change plan/ADR status or authorize implementation.
- 2026-11-08 architect: record supplied a1 completion and custody/replay
  inventory; a2 is blocked on named custody/source, key distribution and
  revocation, and destination-owned durable atomic replay contracts. Discussion
  defaults are not selections; no commands run or implementation authorized.
- 2026-11-08 user: selected operator-managed KMS/HSM custody, operator-
  distributed versioned public-key bundles with measured refresh/revocation
  bounds, and each destination's own transactional DB with atomic consume
  before effects. Accountable operators/owners and measurable
  lifecycle/availability/atomicity requirements remain unconfirmed; 008a2
  remains blocked. No runtime work authorized.
- 2026-11-08 user: accepted assertion lifetime ≤60s, key-bundle freshness
  ≤60s, 30-day signing-key rotation, old-key retention through max assertion
  lifetime plus ≤5s clock skew, replay-row retention through `exp` plus skew,
  and atomic unique `(iss,aud,jti)` consume before effects as proposed test
  targets only. These are not verified requirements or operational guarantees.
  Key custodian/provider, bundle distributor/verifier operators and replay-store
  owners remain Unknown; no KMS/HSM implementation, bundle distribution
  integration or Atlas replay DB/store exists in repo. 008a2 remains blocked
  pending established owners/support and measured targets; 008/ADR-17 remain
  proposed and separate implementation approval is required.
- 2026-11-08 user/architect: user explicitly approved only the internal
  injected-key Ed25519 v1 sign/verify primitive. Added proposed child 008a2a
  inside this epic, exact closed API/files and 250–300-line budget, canonical
  JWS bytes, independent-vector/negative acceptance, fresh baseline commands
  and scoped Python-only boundary check. No runtime edits or commands run;
  pre-existing worktree preserved. 008/ADR-17 remain proposed, 008a2 blocked;
  operational owners remain Unknown and no broader implementation authorized.
- 2026-11-08 core-engineer: implemented and verified only child 008a2a in its
  two authorized files; 300 runtime/test lines. Combined targeted identity,
  primitive and phase-3 parent verification: 74 passed; Ruff, Pyright (0
  diagnostics) and diff check clean; scoped boundary stayed 0 errors / 9
  warnings. PyJWT 2.15.1 and cryptography 50.0.2; new runtime/test files total
  exactly 300 lines. Full suite and normal/protected boundary scan not run.
  008a2 remains blocked; no custody, distribution, replay, issuance, dispatch
  or production-readiness claim.
