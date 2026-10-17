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
| ADR-14 | Identity provider and secret manager abstraction | Open — roadmap `p1-authn`, `p4-credential-broker` |
| ADR-15 | Delegation token format (Biscuit vs macaroons vs signed JSON) | Open — roadmap `a-attenuation-budget` |
| ADR-16 | Docs portal and SDK generation toolchain | Open — roadmap `d-api-contract` |

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
