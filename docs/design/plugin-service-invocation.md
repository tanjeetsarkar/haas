# Plugin-service invocation — proposed security profile

Status: proposed, design-only (2026-11-08). The user selected the directions
below as proposed design decisions; this does not accept ADR-17 or approve
implementation. [008](../plans/008-generic-plugin-service-invocation.md) and
[ADR-17](../adr/0017-trusted-plugin-service-invocation.md) remain proposed.
Implementation requires a separate explicit approval gate after unresolved
parameters and verification requirements are documented. These selections are
not authorization, including for AGENTS §7 hard stops.
Plugins can use this generic facility; web-research and incident-triage are
plausible consumers besides Atlas. No consumer or runtime change is authorized.

### Limited primitive exception (2026-11-08)

The user subsequently authorized only the internal injected-key Ed25519 v1
sign/verify primitive defined in [child 008a2a](../plans/008-generic-plugin-service-invocation.md#child-slice-008a2a--internal-injected-key-ed25519-primitive).
Its exact API and canonical JWS bytes are a bounded implementation contract,
not acceptance of this full security profile. This exception qualifies the
blanket no-implementation statements in this document: 008 and ADR-17 remain
proposed; 008a2a is proposed with limited scope approved; full 008a2 stays
blocked. No plugin signer/public key-loading interface, production custody,
distribution, replay store, issuance, dispatch, network, catalog, egress,
sandbox or consumer is authorized. Accountable operational owners remain
Unknown. Primitive verification alone is not service authorization or replay
admission, and no runtime wiring is permitted.

## Evidence and limits

[007](../plans/007-atlas-service-invocation-audit.md) remains done with its reported
test limitations unchanged. Narrow source reads confirm:

- `credentials.py:141–181,291–350` redeems tool grants and prepares scoped tool
  dispatch. These are not remote service assertions; service grants need a distinct
  audience and explicit tenant/run checks, not reuse of a tool grant reference.
- `egress.py:97–140,215–263,346–437` accepts HTTP as well as HTTPS, empty host
  patterns allow hosts, configured SSRF exemptions skip address checks, and
  requests follow redirects. Request bytes are charged after sending; these
  defaults do not meet the proposed strict service profile.
- The broker constructs pinned-IP requests with Host/SNI and streams responses.
  Actual TLS hostname verification, proxy behavior, budget concurrency and
  callers' use of that transport still need tests, not inference from comments.
- `sandbox/runner.py:63–110,140–165` launches a subprocess and serializes policy.
  It has a single request/response exchange, not a proven live service broker
  channel. It also passes secrets in launcher arguments despite its introductory
  comment; that is a leakage risk to assess, not a channel to copy.
- 007's SDK/native HTTP, stock HTTP/MCP and workflow anchors remain the evidence
  for their trust gaps. No new execution or network evidence was obtained here.

### Custody and replay inventory (2026-11-08)

- [`pyproject.toml:21–22`](../../pyproject.toml) already declares
  `cryptography>=44.0.0` and `PyJWT[crypto]>=2.10.0`. This is dependency
  inventory, not proof of Ed25519 JOSE interoperability or strict verification.
- [`app/core/envelope.py:61–77`](../../app/core/envelope.py) uses
  `HAAS_ENVELOPE_SIGNING_KEY` and `HAAS_ENVELOPE_VERIFY_KEY` for worker
  envelopes. [`deploy/helm/haas/values.yaml:3–7,145–147,182–184`](../../deploy/helm/haas/values.yaml)
  references the signing secret for the orchestrator and a verify-only public
  key for workers. This is a different trust boundary: **never reuse, export
  or derive service assertion keys from the envelope signing key**.
- The same Helm values describe out-of-band Kubernetes Secret provisioning via
  External Secrets Operator, Vault agent, sealed-secrets or Terraform.
  [`deploy/terraform/modules/haas-platform/main.tf:97–112,129–141`](../../deploy/terraform/modules/haas-platform/main.tf)
  offers optional Kubernetes Secrets and recommends External Secrets/Vault for
  production; [`variables.tf:148–152,175–183`](../../deploy/terraform/modules/haas-platform/variables.tf)
  describes out-of-band provisioning and the envelope key pair, not a service
  assertion key. These are provisioning/transport references, **not evidence
  of a KMS/HSM signing API, an integrated Vault issuer, automatic rotation or
  revocation guarantees**.
- [`app/core/webhooks.py:1–4,27–75,137–145`](../../app/core/webhooks.py)
  implements tenant webhook HMAC signing and encrypted DB-backed signing keys,
  with rotation support. That symmetric webhook mechanism is not the selected
  asymmetric service assertion issuer or its lifecycle.
- [007's readiness inventory](../plans/007-atlas-service-invocation-audit.md)
  describes Atlas's service as health-only, with no business routes, DB/schema
  or service trust contract; it establishes no destination replay store.
  The inspected sources do not establish an invocation key registry, rotation,
  revocation or public-key distribution mechanism, KMS/HSM API integration,
  or durable destination replay storage. Absence of evidence here is not a
  claim that no external deployment could provide them.

### User-selected custody and replay directions — proposed only

The user selected operator-managed KMS/HSM custody for the dedicated issuer
key, operator-distributed versioned public-key bundles with measured refresh
and revocation bounds, and a destination-owned transactional database at each
destination for atomic replay consumption before effects. These directions
remain proposed; no provider, operator or store owner has been named, and they
do not establish that the required integrations or stores exist.

The key custodian/provider must be named and demonstrate the KMS/HSM signing
interface, issuer authorization, availability behavior, rotation and emergency
revocation procedures. The bundle distributor and each destination verifier
operator must be named, and agree measurable refresh, revocation-propagation
and stale/unavailable behavior. The user has accepted the numerical targets
below for proposed tests only; they are not verified requirements, service
levels or operational guarantees. A separate revocation-propagation bound
remains unselected. External Secret provisioning is not itself KMS/HSM
integration, key rotation or a revocation guarantee.

For each destination, name the replay-store owner and confirm that the
destination's own transactional DB can durably and atomically consume a unique
`(iss, aud, jti)` before any effect, including concurrency, transaction,
retention, availability/outage and crash-recovery requirements. Duplicate,
unavailable, non-durable or ambiguous admission denies before effect; a
separate effect record/recovery protocol is still required, and no exactly-once
guarantee follows. The repository has no current KMS/HSM integration and no
destination DB or replay store established for Atlas. Destination schema,
migrations and deployment remain outside 008's core-only paths and require
their own owner, plan and approval; a fake store in core tests cannot establish
real destination durability.

**008a2 is BLOCKED** until the user and accountable operators name the key
custodian/provider, the bundle distributor and destination verifier operators,
and each destination replay-store owner. Confirm measurable key lifecycle,
signing and distribution availability, refresh/revocation bounds, and
destination atomicity, durability, retention and outage/recovery requirements
with evidence. Then obtain separate explicit implementation approval; meeting
these gates alone is not approval.

### Accepted proposed test targets (unverified)

The user accepted these values as targets for tests in a future, separately
approved implementation; acceptance does not make them verified requirements
or operational guarantees:

- Assertion lifetime: at most 60 seconds.
- Key-bundle freshness: at most 60 seconds.
- Signing-key rotation interval: 30 days.
- Retain an old public key through the maximum assertion lifetime plus clock
  skew of at most 5 seconds.
- Retain destination replay rows through assertion `exp` plus clock skew.
- Atomically consume a unique `(iss, aud, jti)` before any effect.

The targets require implementation and operational support to be established
and measured. The key custodian/provider, bundle distributor, each destination
verifier operator, and each replay-store owner are **Unknown**. The repository
has no KMS/HSM implementation, no bundle-distribution integration, and no
Atlas replay database/store established. Accordingly, 008a2 remains **BLOCKED**
until accountable owners and supporting mechanisms are established and the
targets are measured; test targets alone do not unblock it.

## 1. Assertion and trust — selected direction, parameters open

**User-selected proposed direction:** a dedicated Ed25519 JWS for service
invocation assertions, with explicit key lifecycle. This is not an accepted
implementation decision. The following profile is a concrete starting
hypothesis, not a validated implementation or a statement of current support.
The claim candidates address the 007 findings that public context identity is
local, endpoint trace/effect metadata does not authenticate tenant/run/plugin
scope remotely, and tool-audience grants do not establish service-audience
delegation. 007 did not validate these proposed claims or a JWS implementation.

Proposed JWS header: exactly `alg=EdDSA`, dedicated
`typ=haas-service-invocation+jws`, and operator-assigned `kid`; reject `none`,
algorithm negotiation, token-provided keys/URLs (`jku`, `x5u`), and unknown
critical extensions. Verify JOSE-library Ed25519 support and strict header
handling before selecting a library.

Proposed required claims (exact names for version 1): `v`, `iss`, `aud`,
`tenant_id`, `plugin_id`, `plugin_digest`, `run_id`, `invocation_id`,
`tool_name`, `operation`, `effect_key`, `policy_revision`, `catalog_revision`,
`method`, `destination`, `content_type`, `request_sha256`, `iat`, `exp`,
`jti`. Use integer `v=1` and integer NumericDate `iat`/`exp`; other claims are
strings. Encode the 32-byte body digest and 16-byte random `jti` as
case-preserving, unpadded base64url. Verifiers must reject non-alphabet
characters, padding, incorrect decoded lengths, and any value that does not
round-trip to the identical encoded string. Require issuer/service
cross-language vectors for both values, including bytes whose encodings contain
uppercase and lowercase letters. Reject duplicate JSON keys, missing/wrongly
typed claims and unknown claims for this version; version evolution must be
explicit. The
`effect_key` is signed but non-authoritative deduplication metadata. No secret,
approval boolean, caller URL or arbitrary header is a claim. The receiver must
separately authorize tenant/resource scope: signature validity alone does not
grant access.

Proposed body binding: SHA-256 over the fixed ASCII domain tag
`HaaS-Service-Request-v1` followed by fields in this order: protocol version
string `1`, uppercase method, canonical destination, content type, exact raw
body bytes. Prefix each field with its unsigned 32-bit big-endian byte length;
text fields are UTF-8 and body is unmodified bytes. Sign the resulting digest
in the JWS.
The broker serializes once and emits those same body bytes; the receiver hashes
the raw bytes before parsing. Do not include the JWS header itself in the
digest. Whether to bind other effect-relevant headers remains open. This
proposal avoids ad-hoc JSON canonicalization, but the domain tag, encoding and
cross-language vectors still require independent verifier review.

Proposed destination encoding uses the catalog's stored canonical URL, never a
caller URL: HTTPS, lowercase ASCII DNS hostname, explicit port 443 and exact
configured path. Initially allow only ASCII path characters from the
unreserved set plus `/`; reject IP literals, userinfo, fragments, dot
segments, percent escapes, non-ASCII host spellings, caller-generated
path/query, and redirects. Client, signer, broker and verifier must agree on
the exact string/bytes.

Proposed initial operation constraints: only the method configured for the
operation; initial supported forms are `GET` with no body or `POST` with the
exact content type `application/json` and a bounded body. Reject
method-override headers, content-encoding, multipart/streaming bodies,
duplicate or ambiguous content-type/length, and caller-supplied Host/auth
headers. `PUT`/`PATCH`/`DELETE`, query templates and other media types remain
alternatives only if a consumer and verifier contract justify them. These
constraints are proposals, not facts about current egress.

Proposed assertion lifetime test target is at most 60 seconds, bounded by
dispatch expiry, with a clock-skew test target of at most 5 seconds. These are
accepted proposed test targets, not verified requirements or operational
guarantees. Test rejection of future-issued, expired or overlong assertions;
measure issuer/service clock behavior and demonstrate support before treating
the targets as operationally usable. All identity, policy and approval
classification must come from verified kernel dispatch and pinned registry
state. Plugin/model input may provide application data and an authorized
operation selector only. Missing authoritative state rejects issuance. Claims
and canonicalization require independently implemented verifier tests; 007
did not establish them.

### Key lifecycle proposal (not validated)

The user-selected proposed direction is operator-managed KMS/HSM custody and a
dedicated issuer operation that signs only assertions constructed from verified
dispatch. No provider, key custodian or KMS/HSM signing integration is currently
established. External Secret provisioning is not an equivalent KMS/HSM
integration or a rotation guarantee. Never reuse, export or derive from the
worker-envelope signing key, and never expose private material or a general
signing oracle to plugins, sandbox children or services.

Propose publishing a new verification key before signing with it and retaining
the old key through the maximum assertion lifetime plus a clock-skew target of
at most 5 seconds. The accepted proposed test targets are 30-day signing-key
rotation and key-bundle freshness of at most 60 seconds. These targets are not
verified requirements or operational guarantees; demonstrate their support
and measure them with named owners before operational use. The user-selected
direction also requires operators to agree and measure revocation propagation;
stale or unavailable trust state denies. On compromise:
stop issuance, revoke the affected `kid`, remove it from trusted key sets,
invalidate verifier caches within the agreed bound, recover custody, rotate,
and assess accepted assertions and effects. Availability targets, measurable
bounds and incident steps require named owners, platform support, testing and
recovery exercises.

## 2. Replay and effect idempotency — proposed semantics (not validated)

The user-selected proposed direction is a destination-owned transactional
database for each destination, with a fresh cryptographically random 128-bit
`jti` per signed attempt and durable atomic consume-before-effect keyed by
`(iss, aud, jti)`. The accepted proposed test targets are atomic unique consume
before effects and replay-row retention through `exp` plus clock skew. These
are not verified requirements or operational guarantees. Concurrent duplicate
consumption or replay-store unavailability denies before effect. No destination
replay-store owner, transaction contract, availability requirement or
demonstrated implementation is established; confirm these for each
destination. The repository has no destination DB/replay store established for
Atlas.

ADR-06's at-least-once transport means JWS/nonce consumption cannot promise
exactly-once effects. Require a separate effect record keyed by tenant, plugin
digest, invocation, catalog operation and stable effect key, storing the
request digest. Same key/same digest returns recorded outcome or safe
in-progress status; same key/different digest is a conflict. Retain effect
identity for the documented retry/duplicate horizon. An external irreversible
destination must provide idempotency or reconciliation; otherwise automatic
retry is not allowed.

For an authorized retry, mint a fresh `jti` while preserving the logical effect
key and exact request digest. Re-presenting the same assertion is rejected;
never reset a consumed nonce after timeout. Choose whether replay consumption
and effect admission share a transaction or use a recoverable state machine.
Test crashes around admission/effect boundaries; existing source does not prove
service-side atomicity.

## 3. Audience, destination and actual egress — proposed catalog profile

**User-selected proposed direction:** an operator-owned exact-destination
catalog, with strict HTTPS and fail-closed behavior. This does not claim an
existing catalog or settle its owner, source of truth, provisioning, runtime
wiring, revision pinning or revocation mechanism. Those parameters require
design and verification before implementation.

Propose a versioned operator-owned record for each service operation: audience,
exact HTTPS DNS hostname, explicit port, exact path, operation, allowed method
and content type, request/response/time ceilings and tenant-policy availability.
Use port 443 only initially; alternate ports require explicit per-entry review.
No wildcard, IP literal, caller URL/path/query, implicit private-network
exception or redirect. Tenant policy grants/denies catalog operations;
plugin metadata may declare symbolic operations but grants no authority.
Authorize only the intersection of operator catalog entry, tenant entitlement
and installed plugin/revision's declared operation.

Propose immutable catalog revisions pinned at dispatch. Operator edits create
a new revision; revocation denies new calls without silently changing an
in-flight signed request. Exact cache freshness and in-flight revocation
behavior remain open. Deployment configuration avoids a new runtime database
contract but may have rollout latency; a DB/control-plane catalog allows
central updates but requires separately reviewed tenant isolation/RLS, audit
and failure semantics. Catalog source, owner, provisioning and on-call
responsibility are not established by existing evidence.

Trusted native helper calls go through the kernel issuer/dispatcher and existing
egress broker's strict service profile, not `ctx.http_client`. Stock HTTP/MCP
remain untrusted legacy paths; no trusted endpoint opt-in in this epic. Future
HTTP/MCP adapters must route each actual HTTP connection (including MCP session
traffic) through the broker; metadata or preflight validation is insufficient.
No raw-client fallback on denial, issuer, catalog, meter or broker failure.

The source audit reports current `egress.py` accepts HTTP as well as HTTPS,
empty host patterns may allow hosts, configured SSRF exemptions exist,
redirects are followed, and request bytes are charged after sending. It also
reports pinned-IP request construction, but does not prove TLS hostname
verification, proxy behavior, concurrent budget reservation or all callers'
use of that transport. This is not evidence that the proposed profile is
enforced. Require tests of actual TLS, hostname verification, DNS validation/
pinning, redirect denial before another connection, byte/time limits and meter
failure. Missing policy, catalog, issuer, meter or broker denies; never fall
back to a raw client or weaken legacy SSRF checks.

## 4. Credentials — separate, optional lane

**User-selected proposed direction:** no provider-secret forwarding initially.
Service identity is not a provider credential and grants no secret access.
Any future provider-secret delivery must be separately designed and explicitly
approved. It may be considered only where the user permits
the service to receive raw provider values; a reusable provider API key cannot
be made single-use merely by wrapping its delivery in a single-use grant.
Prefer provider-minted short-lived attenuated tokens where supported.

The following are risk considerations, not a design for future forwarding and
not authorization to build it. Any future credential lane needs a separately
approved design and must revisit its authority, scope and delivery contract.

Future allowed names must intersect plugin/active-skill declarations,
tenant-approved service export names, run-authorized secret scope and exact
operation grants. Bind each separate grant to tenant, plugin ID/digest, run,
invocation, audience, operation, config revision, request digest, named secret
binding/version and nonce; expiry no later than the assertion. Core resolves
names, atomically redeems once, and injects into fixed catalog-defined headers
over verified TLS, never into caller data, URLs, assertions or child argv/env.
Missing/partial/wrong-scope/expired/replayed grants reject the entire request,
not silently omit a credential. Redact before logging/errors/output; service
retention, onward destination restrictions and provider revocation belong to an
explicit service agreement. Raw values after delivery cannot be cryptographically
confined to one run. Until that risk/authority is approved, deny all forwarding;
no broad core credential, store or settings surface is exposed.

## 5. Approval and sandbox gates

**User-selected proposed direction:** reject approval-dependent calls through
this new service-invocation capability initially. Leave Atlas inquiry dispatch
on its existing approved core path; do not migrate, remove or relabel it as
remotely proven. A local approved flag is not remote proof, and unknown approval
classification denies.

Alternative future proof requires a dedicated signed type and verifier contract:
approval record/decision ID, authenticated approver authority and decision time,
tenant/run/plugin digest, invocation/batch and policy revision, exact approved
argument digest AND emitted request digest, audience/destination/method, effect
identity, issue/expiry, nonce, and live decision revocation/consumption semantics.
Prove transformation from approved arguments to emitted bytes cannot change the
effect; reject stale policy, changed destination/args, denied/revoked decisions
and duplicates atomically before effects. A flag/effect key is never proof.
Future gate: separate user-approved ADR amendment/plan, persisted-state tests
and independent service-verifier mutation/replay/revocation tests with actual
transport. Current workflow test collection does not satisfy this gate.

**User-selected proposed direction:** sandbox use remains disabled unless and
until authenticated host-broker IPC has passed real enforcement tests. IPC
authentication, peer binding, framing, lifetime, quotas, shutdown and deployment
details remain to be designed and verified; do not infer them from the current
one-shot protocol. The host must mediate the actual request; the child receives
neither signer nor provider credential, and there is no direct outbound
fallback. Tests must demonstrate real subprocess→broker→TLS allow, deny and
failure behavior, including no service hit on denial/broker loss and no child
credential/key leakage. This proves only this helper's transport, not isolation
of all arbitrary subprocess networking. If IPC cannot be enforced, keep sandbox
service use disabled.

### Proposed host-broker IPC profile (not validated)

Propose a host-created per-invocation private channel bound to the exact child
process, preferably an inherited socketpair or equivalent private IPC. Do not
put bearer material in argv/environment or expose a general broker socket.
Authenticate the peer through OS channel/process ownership; if application
authentication is additionally needed, use a per-channel random capability
held only by the host and that child. Validate that the supported launcher can
create and bind this channel; current source evidence describes only a
one-shot request/response and does not prove a live broker channel or peer
authentication. Alternatives are peer-credential-authenticated Unix sockets
or an authenticated local proxy, subject to platform/threat-model review.

Propose a versioned, length-bounded request/response protocol allowing only a
catalog operation and bounded application payload. Host derives tenant, run,
plugin, invocation, policy and catalog scope from channel state, never child
fields; child selects only an authorized symbolic operation. Host validates
messages and performs catalog lookup, signing and network dispatch. No
arbitrary URL, headers, raw socket, signer, credential, redirect, retry or
generic CONNECT capability crosses IPC. Bound concurrency, bytes and time per
invocation. Close and deny on malformed framing, scope mismatch, quota excess,
version mismatch, child exit, host cancellation, channel closure or broker
failure. There is no direct-network fallback.

IPC mediation proves only that this helper uses the host broker; it does not
prove that all child networking is blocked. Before enabling sandbox consumers,
require real subprocess-to-broker-to-test-service allowed traffic and prove
forged scope, forbidden operation/destination, malformed/oversize frame,
channel loss, broker loss and shutdown produce no service-side hit. Review
deployment network policy and demonstrate no child access to signing keys or
provider credentials. Keep the sandbox gate closed until these tests and
deployment review pass. These are design hypotheses, not current guarantees.

## Selected directions and separate implementation gate

The user-selected directions in this document are proposed design decisions,
not approval of implementation. The concrete parameters above are proposals,
not validated facts. Resolve the following choices and verification
requirements before plan 008 or ADR-17 can become approved; even then,
implementation requires separate explicit user approval.

1. **JWS/verifier:** accept or revise claims, header, digest encoding,
   destination canonicalization, method/content constraints, expiry/skew and
   JOSE library. Verify independent issuer/service test vectors and mutations
   of signature, claims, method, destination, content type, raw body, expiry
   and key ID.
2. **Key operations:** the selected direction is operator-managed KMS/HSM
   custody, not a Kubernetes Secret or an assumed secret-manager substitute.
   Name the key custodian/provider and issuer authorization; verify an actual
   supported signing integration and no worker-envelope key reuse. Confirm
   measurable lifecycle, rotation, overlap, signing availability, bundle
   refresh and emergency-revocation bounds. The user accepted 30-day rotation,
   ≤60-second assertion lifetime, ≤60-second bundle freshness and old-key
   retention through maximum assertion lifetime plus ≤5 seconds of clock skew
   as proposed test targets only; they are not verified requirements or
   operational guarantees. Test key rotation, stale key set, compromise
   response, issuer outage and recovery.
3. **Replay/effects:** the selected direction is each destination's own
   transactional DB with durable atomic consume before effects. Name the store
   owner for every destination; confirm measurable atomicity, durability,
   retention, availability/outage and crash/recovery requirements. Define
   effect-record retention and destination idempotency/reconciliation. Test
   concurrency, replay, same-effect retry with fresh `jti`, digest conflict,
   crash boundaries and at-least-once redelivery. No exactly-once claim.
4. **Catalog/egress:** choose deployment-config or DB/control-plane source,
   owner, update/revocation/cache semantics, tenant entitlement and plugin
   operation intersection, ports/private-address rules and per-operation
   destination/method/content/limit schema. Test cross-tenant/plugin denial,
   stale/revoked entries and actual TLS/egress allow/deny/failure behavior.
5. **IPC/sandbox:** choose supported IPC/authentication mechanism; verify
   process binding, framing, permissions, quotas, close behavior and deployment
   isolation. Run real subprocess → broker → TLS tests for allow, spoof,
   malformed/oversize input, denied destination, child/channel/host loss and
   shutdown, confirming no service-side hit on denials. Keep sandbox disabled
   until review and all evidence pass.
6. **Selected exclusions:** verify approval-dependent calls reject before
   network dispatch and Atlas inquiry remains on its existing approved core
   path. Provider-credential forwarding remains out of scope. Either future
   capability requires a separate plan and explicit approval.

These are blocking design/evidence gates, not implementation authorization.
Plan 008 and ADR-17 remain proposed during design work; a later explicit user
decision must approve implementation.
