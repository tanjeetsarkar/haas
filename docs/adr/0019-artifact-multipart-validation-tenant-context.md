# ADR-0019: Generic artifact API validation, tenant context, and canonical schema access

* Status: accepted
* Roadmap items: none
* Related requirements/threats: AGENTS.md §§1–2, 4, 9; tenant isolation; public API contract stability

## Context

The generic artifact upload endpoint is consumed by clients that send metadata and
content using multipart requests, including SDKs and plugin services. A runtime
failure surfaced when multipart request data reached validation: a request that
should be validated by the API could produce an internal error. The same request
path also exposed tenant-context cleanup that could attempt to reset a ContextVar
token from a different execution context.

These are API and tenancy lifecycle concerns that remain if any one plugin is
deleted. They are kernel responsibilities, not plugin-specific behavior. Other
artifact clients, such as document-ingestion and evaluation plugins, must be able to
use the same generic upload and retrieval contract. A database-backed request also
exposed artifact CRUD queries that did not match the current schema: tenant UUIDs
are stored as `tenants.tenant_id`, artifact rows retain both the legacy text tenant
key and canonical UUID, and quota overrides are in `retention_policies`. The
existing public artifact API must work with that schema rather than relying on
nonexistent columns or tables.

## Decision

1. Validate artifact multipart metadata through the existing typed API contract.
   Ordinary multipart form-text metadata and the supported SDK JSON/Blob-style
   metadata part are both accepted and validated strictly; malformed input is
   rejected as a client validation error rather than surfacing as an internal
   server error. The file content remains a file/Blob part.
2. Keep tenant context setup and cleanup within the same asynchronous dependency
   execution context. Cleanup must cover normal completion, exceptions and
   cancellation without weakening authentication, tenant authorization or
   tenant-scoped database policy.
3. Keep the existing public artifact API and authorization model. This correction
   does not introduce a new upload capability, alter artifact ownership, or add
   plugin-specific code to core.
4. Make the existing generic artifact create, list, metadata, download, and
   soft-delete paths use the canonical current schema. Resolve the tenant UUID
   through the existing tenant resolver; preserve the run's stored text tenant
   key for the artifact's composite ownership relationship; use the existing
   `retention_policies` table; and write audits with the canonical UUID. Preserve
   current API contracts, authorization, RLS, and soft-delete behavior. Do not
   add or replace tables or introduce a migration for this correction.

## Consequences (including rejected alternatives)

- SDKs and plugins can upload artifacts through the same generic public API with
  predictable validation errors. Document-ingestion plugins and evaluation plugins
  are examples of independent consumers.
- Malformed multipart metadata is rejected rather than being accepted implicitly or
  producing a server error. Valid existing upload forms remain supported.
- Tenant context is reliably released on success and failure while preserving
  existing authentication, authorization and RLS behavior.
- A plugin-specific workaround or a weakened authentication path was rejected:
  neither addresses the generic API and context-lifecycle defects safely.
- No database schema, dependency, or OpenAPI contract change is intended. The
  existing OpenAPI snapshot must remain equal; any unexpected public-contract
  difference requires separate review and approval.
- This ADR accepts the generic design and its implementation direction. Plan 032
  records the parent-reported parser/context verification, 112 targeted tests and
  independent approval. Plan 034 tracks canonical-schema CRUD; its browser-native
  artifact round trip is reported complete, while the empty-cursor fix and
  independent review remain pending. These reported implementation results were
  not rerun by the architect during the documentation-only update.
