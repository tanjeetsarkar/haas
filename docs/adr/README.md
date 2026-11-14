# Architecture decision records

ADRs record decisions that constrain the [roadmap](../roadmap.md). ADR-01 to ADR-12 are
currently summarized in [SDD §7](../design/sdd.md#7-design-decisions); new decisions get a file
here named `NNNN-short-title.md` using the template below, and are added to the index.

| ID | Decision | Status |
|---|---|---|
| ADR-01 | Retain FastAPI, Celery and PostgreSQL as the production foundation | Accepted |
| ADR-02 | PostgreSQL is the authority; outbox drives Redis-backed delivery | Accepted |
| ADR-03 | Shared tenant-aware control plane with optional dedicated cells | Accepted |
| ADR-04 | Per-run sandbox, per-invocation capabilities | Accepted |
| ADR-05 | Immutable agent/plugin/policy revisions and revocation epochs | Accepted |
| ADR-06 | At-least-once transport with deduplicated effects where supported | Accepted |
| ADR-07 | SSE is an observation interface, never the workflow coordinator | Accepted |
| ADR-08 | Separate authorization from prompt instructions | Accepted |
| ADR-09 | Keep Celery as transport; workflow correctness in PostgreSQL | Accepted |
| ADR-10 | Marketplace code runs only in sandboxes; control plane uses static metadata | Accepted |
| ADR-11 | Namespaced plugins with per-run tool aliases | Accepted |
| ADR-12 | Sub-agents are child runs with attenuated delegation and budget slices | Accepted |
| ADR-13 | Sandbox runtime selection (gVisor / Kata / Firecracker) | Open — roadmap `p4-sandbox-runtime` |
| [ADR-14](0014-provider-neutral-identity.md) | Provider-neutral identity and secret-manager boundary | Partially accepted — OIDC/API-key contract; secret manager open |
| ADR-15 | Delegation token format (Biscuit vs macaroons vs signed JSON) | Open — roadmap `a-attenuation-budget` |
| [ADR-16](0016-api-contract-toolchain.md) | API contract toolchain (OpenAPI 3.1, JSON Schema events, AsyncAPI, Spectral, oasdiff, deprecation headers); docs portal and SDK generator | Partially accepted — contract toolchain accepted; portal/SDK generator open (`d-docs-portal`, `d-sdk-*`) |
| [ADR-17](0017-trusted-plugin-service-invocation.md) | Generic authenticated plugin-service invocation scope and safe dispatch | Proposed — selected directions and candidate profile recorded; claims, lifecycle, replay, catalog and IPC hypotheses unverified; separate implementation approval required |
| [ADR-18](0018-domain-client-package-ownership.md) | Domain client packages belong to extensions; public generic SDK composition and coordinated export removal | Accepted — plan 010; package/version selected; no publication authorization |
| [ADR-19](0019-artifact-multipart-validation-tenant-context.md) | Generic artifact API validation, tenant context, and canonical schema access | Accepted — plan 032 done on parent-reported tests/review; plan 034 in progress pending empty-cursor fix and independent review |
| [ADR-20](0020-generic-audit-tenant-uuid-mapping.md) | Resolve tenant UUIDs for generic audit writes | Accepted — plan 033 done on parent-reported independent approval and Docker-backed authenticated API verification |
| [ADR-21](0021-run-tool-restrictions-and-exact-intents.md) | Generic per-run tool restrictions, immutable exact invocation intents and separate approval connections | Proposed — plan 035; internal live authorization RPC selected for c2; exact-intent release gates remain |
| [ADR-22](0022-bounded-acquisition-and-tool-free-finalization.md) | Generic bounded acquisition followed by a reserved tool-free finalization attempt | Accepted — plan 036 behavior/quotas and one reversible phase-state migration authorized; shared DB application not authorized |
| [ADR-23](0023-sdk-stream-lifecycle.md) | Generic abortable, bounded SDK stream observation lifecycle | Accepted — approved epic 038 scope; plan 040 implementation/review pending |

## Template

```markdown
# ADR-NNNN: Title

* Status: proposed | accepted | superseded by ADR-XXXX
* Roadmap items: <ids>
* Related requirements/threats: FR-.., TM-.., SEC-..

## Context
## Decision
## Consequences (including rejected alternatives)
```
