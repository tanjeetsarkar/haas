# 034 — Generic artifact CRUD against the canonical schema

- Status: in-progress — browser round trip reported; empty-cursor fix and independent review pending
- Type: slice
- Owner agent: core-engineer
- Epic: none
- ADR: [ADR-0019](../adr/0019-artifact-multipart-validation-tenant-context.md), extended for canonical-schema access

## Goal

Complete and verify the existing generic artifact API's database round trip against
the current PostgreSQL schema. A valid multipart upload must succeed, and authorized
clients must be able to list artifacts, retrieve metadata, download the exact bytes,
and soft-delete artifacts while pagination and tenant isolation remain correct.
Keep the public API shape, authentication and authorization unchanged. Do not add a
migration, alter or replace existing tables, weaken RLS, or add plugin-specific
behavior.

The reported regression is after multipart parsing: a real artifact `POST` reaches
the database and fails with `UndefinedColumn` for `tenants.tenant_uuid`; the current
tenant key is `tenants.tenant_id` (UUID). `retention_overrides` is also not a current
table; quota overrides live in `retention_policies`. Artifact ownership must retain
the existing `artifacts.tenant_id` text key and composite run foreign key, alongside
the canonical `artifacts.tenant_uuid` UUID. Audit writes use the canonical UUID.
JSONB metadata decoding and the colon-delimited cursor round trip also require
verification/fixes as needed.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Generic artifact create/list/get/download/delete queries and tenant scoping | Core | Artifact storage and public API remain necessary if any plugin is removed; document-ingestion and evaluation plugins can both use them. |
| PostgreSQL-backed API regression coverage | Core | It verifies the generic API, current schema, and tenant boundary, independent of plugin behavior. |
| Browser-native upload round trip | Client/API integration evidence | It exercises the existing public contract using browser `fetch`, `FormData`, and `Blob`, without introducing a plugin-specific client or core browser framework. |

## Scope

**Allowed paths:** `app/api/artifacts.py`, `tests/test_artifact_api_database.py`,
`tests/test_artifact_upload.py`, this plan, ADR-0019 and its index row, and the
plans index. No implementation or test changes are authorized to the architect.

**Out of scope:** migrations or schema/table changes; authentication, authorization,
RLS, tenant selection, or audit-integrity weakening; new dependencies or generic
browser/test frameworks; public endpoint or response-shape changes; plugin-specific
behavior; unrelated artifact retention/erasure work; commits.

**Planned files:**

- `app/api/artifacts.py` — align reads/writes with existing schema and preserve both
  tenant identifiers: resolve canonical `tenants.tenant_id` UUID for current
  tenant/policy/audit lookups, obtain the run's stored text `tenant_id` for the
  artifact row and composite run foreign key, query `retention_policies`, and keep
  artifact operations scoped to the authorized tenant. Make JSONB response handling
  work for the database driver's decoded JSON value and any supported string value.
  Make emitted pagination cursors round-trip safely despite timestamps containing
  colons; retain the existing cursor string field and ordering semantics.
- `tests/test_artifact_api_database.py` — use the existing disposable PostgreSQL
  qualification fixture to exercise a real authenticated HTTP upload and the full
  artifact round trip against migrated current-schema tables. Extend focused cases
  for canonical UUID and configured slug selectors as applicable.
- Existing browser test/manual smoke harness, if one exists — exercise vanilla
  browser `fetch` + `FormData` + `Blob` against the API, without an SDK. Do not add
  a browser dependency or framework as part of this slice; report the actual
  harness and evidence.
- `tests/test_artifact_upload.py` — retain multipart parse/validation regressions;
  extend only if the database failure reveals a distinct API-level regression.

## Acceptance criteria

- [x] An authenticated multipart `POST /v1/artifacts` using valid browser/SDK-style
  metadata and file content returns the existing successful response, persists a
  row in the existing `artifacts` table, and records the audit event with the
  canonical tenant UUID.
- [ ] Queries use the current schema: resolve `tenants.tenant_id`; use the
  `retention_policies.tenant_uuid` key; preserve the artifact row's text
  `tenant_id`, UUID `tenant_uuid`, and composite run ownership. No reference to
  `tenants.tenant_uuid` or a nonexistent `retention_overrides` table remains in
  the touched artifact CRUD paths.
- [ ] Listing for a run returns the expected artifacts over multiple pages with
  no duplicates or omissions. An emitted cursor can be consumed even when its
  timestamp contains colons; invalid cursors return a client error.
- [ ] Item metadata is returned with JSON metadata decoded as an object whether
  the database driver supplies a decoded JSONB value or a string.
- [ ] Download returns bytes identical to those uploaded; metadata, list, and
  download operations for another tenant do not disclose the artifact. Cross-tenant
  delete also does not affect it.
- [ ] Authorized delete retains existing soft-delete semantics; the stored
  artifact row and object are not destructively removed by this API operation.
- [x] Vanilla browser `fetch`/`FormData`/`Blob` upload and subsequent API read/
  download are exercised; no SDK-only assumption masks multipart behavior.
- [ ] Tests run against a disposable PostgreSQL 16 database migrated from the
  repository's existing migrations. No migration, table replacement, or data
  cleanup outside test-owned records is introduced.
- [ ] Authentication, authorization, tenant membership checks, RLS policy, audit
  transaction semantics, and existing response/request contracts remain unchanged.
- [ ] Targeted unit and database-backed API tests pass; Ruff and Pyright report no
  new errors; boundary scan does not increase; independent review approves.

## Verification rungs

- Rung 1 — Ruff: `.venv/bin/ruff check app/api/artifacts.py tests/test_artifact_api_database.py tests/test_artifact_upload.py`
- Rung 2 — Pyright: `.venv/bin/pyright app/api/artifacts.py`
- Rung 3 — Unit and PostgreSQL API regressions:
  `.venv/bin/pytest tests/test_artifact_upload.py tests/test_artifact_api_database.py -q`
  with `HAAS_TEST_POSTGRES_DSN` pointing only to a disposable PostgreSQL 16
  qualification database. Record skipped DB tests as unavailable, not passing.
- Rung 4 — Browser-native vanilla flow: record the actual browser/harness, request
  path, response status, returned metadata, and downloaded-byte comparison. Do not
  use browser devtools or a mock as a substitute for API/database evidence.
- Rung 5 — Boundary:
  `python3 .github/skills/core-boundary-check/scripts/check_boundaries.py | tail -1`
- Contract check — no API shape change is intended; verify generated OpenAPI is
  unchanged if the touched annotations affect its snapshot.

## Baseline

```text
Reported by the user; not independently rerun while documenting this plan:
- Multipart parsing succeeds and the real POST reaches the database.
- The POST then fails with UndefinedColumn for tenants.tenant_uuid.
- retention_overrides is not present in the current schema; retention_policies is.
- tenants.tenant_id is the canonical UUID; artifacts retain text tenant_id plus
  UUID tenant_uuid and a composite run foreign key; audit writes use canonical UUID.
- PostgreSQL 16 is the target for the disposable API qualification database.
- Engineer aa42f184 is implementing generic artifact CRUD only and an actual DB/API
  test. No implementation result is asserted here.
```

The prior multipart parse failure is not the scope of this follow-up. Plan 032
tracks that correction and is complete; this plan starts at the parsed
request/database boundary. Existing production tables are preserved.

### Parent-reported verification progress — 2026-11-09

The parent reports 126 targeted tests passing, including 14 database tests.
Independent review is pending one small empty-cursor fix; do not mark the slice
done until that fix is complete and review approves it.

The actual browser-native flow used `fetch` + `FormData` + `Blob` through the
secure proxy: artifact save returned 200, list returned 200, download returned
200, and downloaded bytes matched the uploaded bytes. The exact temporary
artifact was deleted as cleanup. This resolves Decision A below; no additional
browser-harness decision is needed. No SMTP side effect occurred. These are
parent-reported results, not independently rerun during this documentation-only
update.

Multipart metadata encoding: browser FormData sends ordinary form-text metadata;
the supported SDK JSON/Blob-style metadata part is also accepted. Both forms
are validated by the same API contract. The file payload remains a file/Blob.
This clarification aligns with plan 032 and ADR-0019.

Core database tables/migrations, generic SDKs, and core marketplace `web/` are
unchanged. No migration or destructive schema operation was introduced.

## Risks and rollback

- Mixing the canonical UUID with the run's text tenant key can violate the
  composite foreign key or incorrectly scope data. Assert both stored values and
  canonical audit identity in database-backed tests.
- A query that omits tenant scoping can disclose metadata or bytes despite a
  successful upload. Exercise reads and delete under a different authenticated
  tenant, including list pagination.
- Cursor format changes can silently skip or duplicate rows. Verify stable
  ordering, full traversal, and malformed-cursor rejection.
- Rollback: revert only the artifact API query/serialization/cursor correction
  and its tests. There is no schema or data migration to roll back.

## Decisions needed

None. Decision A is resolved by the parent-reported browser-native
`fetch`/`FormData`/`Blob` flow through the secure proxy. Independent review
remains pending the empty-cursor fix; this is a verification gate, not a user
decision.

## Log

- 2026-11-09 architect: documented the approved generic CRUD follow-up and
  user-reported current-schema failure. Core-engineer implementation is in
  progress; database/API and browser verification remain pending.
- 2026-11-09 architect: recorded parent-reported 126 targeted tests (14 database
  tests), browser-native save/list/download with byte equality and exact-ID
  cleanup. Decision A is resolved. Independent review remains pending the
  empty-cursor fix; no core tables, migrations, SDKs or marketplace UI changed.
