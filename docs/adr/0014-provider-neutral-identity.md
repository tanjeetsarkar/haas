# ADR-14: Provider-neutral identity and secret-manager boundary

* Status: partially accepted
* Roadmap items: `p1-authn`, `p1-tenant-registry`, `p4-credential-broker`
* Related requirements/threats: SEC-01, SEC-02, SEC-26, TM-01

## Context

HaaS needs to authenticate human and service callers without trusting a caller-selected
tenant header. The initial identity provider should not lock the API or client SDK to a
single commercial IdP. The current API is development-only and historically accepted
`X-Tenant-ID` as authority.

## Decision

* Validate OIDC JWTs using configured exact issuer, audience and JWKS URL. The first
  verifier accepts RS256, requires `iss`, `sub`, `aud`, `exp` and `iat`, caches signing
  keys, and refreshes JWKS when an unknown key ID appears.
* Support high-entropy tenant-scoped API keys with `hk_live_` / `hk_test_` prefixes.
  Persist only SHA-256 digests, compare candidate digests in constant time, and check
  expiry, revocation, principal, tenant and active membership on use.
* Treat `X-Tenant-ID` only as a selector. Every production identity must have an active
  membership in the selected active tenant. Strip caller-supplied internal identity
  headers before routing.
* Keep raw header tenancy as the local development fallback only. A supplied
  `Authorization` credential still follows the verifier in development; test keys are
  rejected by the production profile.
* Bootstrap the first tenant administrator from an exact configured issuer and subject.
  The one-time command refuses an existing tenant slug, and bootstrap inputs should be
  removed from the deployment environment afterward.
* Production API startup remains fail-closed until RBAC and tenant policy are deployed.
  Full revocation workflows and API-key issuance belong to the later administration work.
* Authentication establishes a principal and active membership; a separate
  deny-by-default action policy decides which routes that role may use. A role alone
  does not grant audit-content or break-glass access.

The secret-manager abstraction remains open and is not decided by this ADR.

## Consequences

The verification boundary is IdP-neutral and can be tested against local/mock JWKS
servers. Deployments must configure stable issuer/audience/JWKS values and provision
memberships; the platform does not auto-enroll verified users. Key issuance and role
authorization are not implied by successful authentication. The initial algorithm set
is intentionally narrow; support for another signing algorithm requires an explicit
security review and tests.

## Alternatives considered

* Hard-code one IdP SDK: rejected to avoid coupling the API contract and client SDK to a
  vendor.
* Trust `X-Tenant-ID` behind deployment convention: rejected because direct ingress or
  header spoofing would bypass identity.
* Store API-key plaintext or reversible encryption: rejected; only a digest is needed
  to verify a high-entropy credential.
