# ADR-0017: Trusted plugin-service invocation

* Status: proposed — 007 is final reviewer-approved and confirms the public-path
  proof gap; the user selected proposed design directions below, but neither this
  ADR nor plan 008 authorizes implementation
* Roadmap items: existing credential/egress and per-invocation capability work;
  exact roadmap mapping to be confirmed before ADR acceptance
* Related requirements/threats: ADR-04, ADR-08, ADR-14; TM-02, SEC-01;
  tenant isolation, credential attenuation, SSRF and approval integrity

### Limited implementation exception (2026-11-08)

The user explicitly approved only [008a2a](../plans/008-generic-plugin-service-invocation.md#child-slice-008a2a--internal-injected-key-ed25519-primitive):
a generic internal Ed25519 compact-JWS sign/verify primitive over existing v1
claims, with injected dedicated private/public key objects and independent
vectors. Plugins can eventually benefit from this authentication foundation;
web-research and incident-triage are plausible consumers, but receive no
general signer, key interface or new public surface in this child.

This narrow exception qualifies the blanket implementation-gate statements
elsewhere in this ADR; **ADR-17 and 008 remain proposed, 008a2 stays blocked,
and 008a2a is proposed with its limited scope user-approved**. Exact API,
canonical bytes, strict validation, closed files and acceptance are in the
child plan. No production key source/custody, replay, issuance, dispatch,
network, catalog, egress, sandbox or consumer is authorized. Custody,
distribution/revocation and destination replay owners remain Unknown.
Injected in-memory private keys are not an accepted production alternative
to the proposed KMS/HSM direction. The child cannot prove provenance,
authorization, freshness or replay safety and must remain unwired.

## Context

Plugins can call independently deployed services, but local context identity is
not remote authentication. The current HTTP/MCP endpoint handler sends tool
arguments, tracing, effect keys and optional secret-based authentication/body
HMAC. It does not transmit kernel-authenticated tenant/run/plugin/approval scope.
Public `ToolContext` exposes identity and an untyped HTTP client, not a safe
service invocation API. Internal single-use credential grants target tools;
they are not shown to be service-verifiable invocation assertions.

The bounded [007 audit](../plans/007-atlas-service-invocation-audit.md) found
that the public SDK exposes no safe service-call helper; stock endpoint wire
metadata does not establish trusted tenant/run/approval authority; tool credential
grants are not proven service-verifiable delegation; and the existence of
`egress_request` does not prove that native, endpoint and sandbox transports all
use it. The audit reports source anchors, available test results and missing
proofs. This confirms a prerequisite gap, not an implementation design approval.
Existing broker/egress machinery should be extended rather than duplicated, but
its required enforcement points and tests must be established in separately
approved slices. In particular, sandbox policy data passed to a subprocess does
not prove that its actual network transport is mediated. Plan
[008](../plans/008-generic-plugin-service-invocation.md) now requires a distinct
sandbox transport/enforcement slice with real deny/allow/failure tests, and
keeps service invocation disabled for plugins executed through the existing
sandbox runner until that slice passes. The runner/routing label itself is not
claimed to be an OS/network security boundary. Plan 008 remains proposed and
does not approve implementation.

The [proposed security profile](../design/plugin-service-invocation.md) records
the user's selected directions: dedicated Ed25519 JWS with explicit key
lifecycle and operator-managed KMS/HSM custody; operator-distributed versioned
public-key bundles with measured refresh/revocation bounds; each destination's
own transactional DB with atomic replay consume before effects; operator-owned
exact-destination catalog with strict HTTPS and fail-closed behavior; no
provider-credential forwarding initially; rejection of approval-dependent
calls through this capability while Atlas inquiry stays on its existing
approved core path; and authenticated host-broker IPC for sandbox use only
after real enforcement tests pass. These are user-selected **proposed design
decisions**, not accepted ADR decisions or implementation authorization.
The design note and plan 008 describe candidate parameters: fixed EdDSA JWS
header and scoped claims, exact-body request binding, HTTPS/443 exact catalog
paths, 60-second assertion and key-bundle-freshness targets, 30-day key
rotation, old-public-key retention through maximum assertion lifetime plus at
most 5 seconds of clock skew, destination replay-row retention through `exp`
plus skew, atomic unique `(iss,aud,jti)` consumption before effects, and a
host-created process-bound IPC channel. The user accepted the assertion/key
freshness, rotation, retention and atomic-consume values as proposed test
targets only, not verified requirements or operational guarantees. 007 found
no public safe-service helper; it also reported
current egress behavior that does not meet the proposed strict profile, while
pinning/TLS/proxy/budget enforcement and actual caller wiring remain unproven.
Its sandbox evidence describes a one-shot exchange, not authenticated live
broker IPC. Therefore the proposed numbers and mechanisms require independent
verification.

Open gates include JOSE dependency and verifier interoperability; key
provisioning/custody/rotation/revocation and compromise response; replay-store
atomicity/retention and at-least-once effect handling; catalog owner/source,
tenant/plugin scope and revocation; exact egress controls; and IPC
authentication, process binding and deployment enforcement. See [008](../plans/008-generic-plugin-service-invocation.md)
and the [security profile](../design/plugin-service-invocation.md) for
alternatives and required evidence.

### Custody and replay inventory

[`pyproject.toml:21–22`](../../pyproject.toml) already declares
`cryptography>=44.0.0` and `PyJWT[crypto]>=2.10.0`; this does not verify the
proposed JOSE profile. [`app/core/envelope.py:61–77`](../../app/core/envelope.py)
and [`deploy/helm/haas/values.yaml:145–147,182–184`](../../deploy/helm/haas/values.yaml)
describe internal envelope signing on the orchestrator and public verification
on workers. Service assertions must never reuse, export or derive from the
envelope signing key.

Helm [`values.yaml:3–7`](../../deploy/helm/haas/values.yaml) describes Secrets
provisioned out of band through External Secrets Operator, Vault agent,
sealed-secrets or Terraform. Terraform
[`main.tf:97–112,129–141`](../../deploy/terraform/modules/haas-platform/main.tf)
optionally creates Kubernetes Secrets and recommends External Secrets/Vault in
production; [`variables.tf:148–152,175–183`](../../deploy/terraform/modules/haas-platform/variables.tf)
describes out-of-band provisioning and the envelope key pair, not service
assertion custody. These references are provisioning/transport capabilities,
not evidence of a KMS/HSM API, integrated service issuer, rotation or
revocation guarantee. [`app/core/webhooks.py:1–4,27–75,137–145`](../../app/core/webhooks.py)
has encrypted DB-backed tenant webhook HMAC keys and rotation support; this
is not the asymmetric assertion issuer.

[007](../plans/007-atlas-service-invocation-audit.md) records Atlas's service
as health-only, with no business routes, DB/schema or service trust contract.
The inspected evidence establishes no invocation key registry, key lifecycle
or public-key distribution/revocation, nor destination durable replay store;
external deployment support is unknown.

The user-selected proposed custody direction is operator-managed KMS/HSM for a
dedicated issuer key, with operator-distributed versioned public-key bundles
and measured refresh/revocation bounds. The user also selected each
destination's own transactional DB for durable atomic replay consume before
effects. Key custodian/provider, bundle distributor, destination verifier
operators and replay-store owners are all **Unknown**. The repository has no
KMS/HSM implementation, bundle-distribution integration, or destination
DB/replay store established for Atlas. External Secret provisioning is not
itself KMS/HSM custody, key rotation or a revocation guarantee. These
directions and test targets do not claim that integrations, providers or
stores exist.

The accepted proposed test targets—not verified requirements or operational
guarantees—are: assertion lifetime at most 60 seconds; key-bundle freshness at
most 60 seconds; signing-key rotation every 30 days; old public-key retention
through maximum assertion lifetime plus clock skew of at most 5 seconds;
destination replay-row retention through assertion `exp` plus skew; and
atomic unique `(iss,aud,jti)` consume before any effect. Their support must be
established and measured with accountable owners before operational use.

**008a1 is completed** as a contract-only slice per the user; this turn did not
rerun its tests. **008a2 is BLOCKED** pending named key custodian/provider and
verified signing integration; named bundle distributor and destination
verifier operators; agreed measurable key lifecycle, signing-availability,
bundle-refresh and revocation-propagation requirements; and a named
replay-store owner and confirmed durable atomic consume contract for each
intended destination, including availability, retention, outage and
crash/recovery requirements. Establish the supporting mechanisms and measure
the accepted proposed test targets above. Record evidence and obtain separate
explicit implementation approval before proceeding. No owner or operational
guarantee is invented here.

Plan 008 and this ADR remain proposed. A separate, future explicit user approval
of implementation is mandatory after the unresolved design and verification
gates are addressed. Remote approval proof and credential forwarding require
separate future approval and are not part of the initial capability.

## Decision

**Proposed generic capability:** plugins can invoke an independently authenticated,
configured service with kernel-derived attenuated invocation scope and mediated
safe egress. Web-research and incident-triage could both use it. No plugin IDs,
domain fields, routes, schemas or host defaults belong in core.

007 confirmed the gap. Plan a bounded generic core capability before a plugin
consumes it; implementation remains blocked on a separate explicit user
approval after the open design and verification gates are resolved. Required
contract properties:

- Kernel determines tenant, run, thread, plugin/revision, tool/invocation and
  effect identity; callers/model args cannot override them.
- Remote verification must bind verified kernel dispatch to the intended
  service and request. The user-selected proposed trust direction is dedicated
  Ed25519 JWS with explicit key lifecycle. Candidate claims, canonicalization,
  digest construction, method/content limits and time bounds are documented
  for review; none is validated or accepted. Key custody, distribution,
  rotation/revocation, replay semantics and dependency support remain to be
  designed and verified. Services must not receive core worker-envelope
  signing secrets.
- Scoped provider credentials remain distinct from service identity; the
  user-selected proposed direction is no provider-value forwarding initially.
  Later export requires an explicit separate approval of the grant design and
  delivery risks. No raw store/settings objects or broad core credentials are
  exposed through the public SDK.
- The user-selected proposed direction rejects approval-dependent calls through
  this capability initially. Keep Atlas inquiry on its existing approved core
  path; do not migrate it to this service path. An approval flag, tracing header
  or effect key is not approval proof. Any future remotely enforceable approval
  binding requires separate design and approval.
- The user-selected proposed destination direction is an operator-owned
  exact-destination catalog, strict HTTPS and fail-closed behavior. A candidate
  exact HTTPS/443 operation record and tenant-policy intersection are proposed;
  catalog ownership/wiring, revision/revocation semantics and private-address
  rules remain open. Use mediated egress only when actual enforcement is
  verified. The user-selected proposed sandbox direction is authenticated
  host-broker IPC; a process-bound private channel is only a candidate. Sandbox
  service calls remain disabled until real allow/deny/failure tests prove that
  actual sandbox requests traverse enforcement. IPC authentication details
  remain open. This requirement applies to plugins executed through the
  existing runner; it does not certify that runner as a general security
  sandbox. No generic raw HTTP bypass is introduced.
- Additive public SDK/endpoint defaults preserve legacy callers and wire shapes;
  no legacy behavior is silently declared trusted or automatically granted scope.
  Trusted mode fails closed when service/audience/credential policy is missing.
- Approval-dependent consumers are deferred by proposed plan 008. Until a
  separately approved proof-bearing slice exists, approval-required invocations
  must not use this service path; existing approved core dispatch remains the
  fallback where retained.

The user-selected directions above settle direction only. Plan 008 and ADR-17
remain proposed, and implementation requires a separate explicit user approval
after the open design and verification gates are resolved. Neither this ADR nor
the selected directions authorize implementation.

Verification must include fake-plugin contract tests, tenant A/B and plugin X/Y
isolation, tampered scope/body, wrong audience/run/invocation, expired/replayed
assertions and missing approvals, plus actual outbound guard execution tests.
For plugins executed through the sandbox runner, tests must invoke through the
real subprocess and transport, observe actual egress enforcement and exercise
deny/allow/failure modes; source presence or mocked validation is not sufficient.
Duplicate deliveries need destination idempotency, not a promise of exactly-once.
Quotas/byte limits, redacted tenant/run/plugin/invocation observability and credential
leak prevention are part of the full contract.

## Consequences (including rejected alternatives)

This is a proposed prerequisite, not permission to implement a token system or
change SDK/API contracts. If an existing public path proves all requirements,
revise/supersede this ADR instead of adding redundant capability.

Rejected: trusting tenant/run model arguments; trusting caller headers; reusing
shared tool secrets as kernel identity; passing core DB access or worker signing
keys to services; adding domain-specific core dispatch; blanket private-network
allowlisting. A service-owned public API lookup remains an alternative only if
the audit proves authenticated authorization and exact approval binding.

Migration is additive opt-in, with legacy tools operational and persisted names
unchanged. No schema/data movement, table drops, UI removals, core dependency
additions or breaking API changes are accepted by this ADR.
