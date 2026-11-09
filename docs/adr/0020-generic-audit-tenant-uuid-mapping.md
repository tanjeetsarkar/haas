# ADR-0020: Resolve tenant UUIDs for generic audit writes

* Status: accepted
* Roadmap items: none
* Related requirements/threats: AGENTS.md §§1–2, 4, 9; tenant isolation; audit integrity

## Context

An authenticated browser request to create a thread can reach the generic audit
producer with a legacy tenant selector such as `local-dev`. The audit table's
`tenant_id` is a UUID, so PostgreSQL rejects that selector when the thread-created
event is inserted. This turns an otherwise authorized `POST /v1/threads` into a
500. The same request's authenticated identity endpoint succeeds for the configured
tenant, and a mismatched tenant is rejected; those authorization results are not
the defect.

Tenant identity resolution and generic audit persistence remain kernel concerns if
any plugin is removed. Core must resolve a tenant selector to its canonical UUID
using the existing resolver, not add plugin-specific behavior or infer an identity
from an untrusted selector. Plugins such as Atlas Explorer and web-research can
both use tenant-scoped threads and the same generic audit path.

## Decision

For generic audit writes that receive a legacy tenant selector, core uses the
existing tenant UUID resolution path and persists the canonical UUID. Resolution
must fail closed: an unknown or unmappable selector must not be assigned to a
default tenant or produce an audit record under a guessed identity.

Keep the existing audit event contract and transaction boundary. Audit rows remain
append-only and retain existing integrity/hash behavior, canonical detail
serialization, tenant RLS context, and atomicity with the operation they record.
Preserve authorization and existing tenant-scoped query behavior. This correction
does not add plugin-specific logic, change API contracts, or introduce a database
schema change or migration.

## Consequences (including rejected alternatives)

- Authorized thread creation can record its audit event with the canonical tenant
  UUID when the caller uses a configured tenant slug or UUID.
- Unknown tenant selectors continue to fail closed. No fallback tenant, weaker
  authorization, or best-effort audit write is permitted.
- The change stays in the generic audit producer / existing tenant-resolution
  boundary. Other audit callers must keep their current behavior and type contract.
- No migration, schema change, new dependency, or public API change is expected.
- Passing the raw selector into a UUID column, dropping the audit event, changing
  audit immutability/integrity behavior, or resolving identity from request input
  alone were rejected.
- [Plan 033](../plans/033-audit-tenant-uuid-mapping.md) records completion based
  on parent-reported independent approval, direct-database/authenticated API
  tests, and an actual Docker-backed thread-create request. This evidence was not
  independently rerun during the documentation-only update.
