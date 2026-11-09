# 033 — Resolve tenant UUIDs for generic audit writes

- Status: done
- Type: slice
- Owner agent: core-engineer
- Epic: none
- ADR: [ADR-0020](../adr/0020-generic-audit-tenant-uuid-mapping.md)

## Goal

Fix the generic audit write path so an authorized thread-create request using a
configured tenant slug records its audit event with the canonical tenant UUID
instead of failing with a PostgreSQL UUID `DataError`. Preserve authorization,
tenant-scoped queries, audit integrity, and transaction/RLS behavior; do not add a
database migration.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Resolve the tenant UUID at the generic audit-write boundary | Core | Tenant identity resolution and audit persistence remain kernel responsibilities if a plugin is deleted; Atlas Explorer and web-research can both use tenant-scoped threads. |
| Regression coverage for UUID and slug selectors and audit persistence | Core | These tests verify generic storage, tenant isolation, and audit invariants, not plugin behavior. |

## Scope

**Allowed paths:** `app/core/store.py`, `tests/test_phase2_database.py`, the
existing thread/API or identity integration test module identified by the engineer,
and this plan/ADR files for necessary documentation status updates. Do not edit
other plans or their statuses.

**Out of scope:** database schema or migration changes; authn/authz, tenant
membership, tenant selection, API/SDK contract, RLS policy, audit schema or
append-only/hash semantics changes; plugin-specific behavior; unrelated
`_insert_audit_event` caller refactors; commits.

**Planned files:**

- `app/core/store.py` — use the existing tenant UUID resolution path for the
  generic audit write that receives a legacy selector; keep unknown mappings
  fail-closed and retain the operation/audit transaction.
- `tests/test_phase2_database.py` — cover tenant slug and UUID selectors, the
  canonical `audit_events.tenant_id` value, and fail-closed/transaction behavior.
- Existing thread/API or identity integration test module — exercise the actual
  authenticated `POST /v1/threads` path and verify the configured-tenant success
  and mismatched-tenant rejection remain unchanged. The precise module was not
  identified during this documentation-only planning pass; locate it before
  editing rather than assuming a test file.
- Tests covering existing audit callers — retain behavior for all relevant
  `_insert_audit_event` call sites; inspect the caller inventory before changing
  the producer signature or semantics.

## Acceptance criteria

- [x] An authorized `POST /v1/threads` with a configured tenant slug succeeds and
  writes its thread-created audit event with the canonical tenant UUID.
- [x] The same regression is covered for a caller supplying the canonical tenant
  UUID; stored `audit_events.tenant_id` equals that UUID.
- [x] Unknown/unmapped tenant selectors fail closed, do not fall back to another
  tenant, and do not leave a partial thread or audit event when the operation and
  audit write share a transaction.
- [x] Existing authentication behavior is unchanged: configured tenant identity
  remains successful and mismatched tenant authorization remains forbidden. No
  authn/authz or tenant-selection behavior is changed to make the failing request
  pass.
- [x] The engineer inventories all generic audit producer callers (including
  thread creation/branching, administrative mutations, workflow and artifact
  paths) before changing any shared interface; unaffected callers retain their
  behavior.
- [x] Audit append-only guarantees, existing hash/integrity behavior, canonical
  details serialization, transaction atomicity and tenant RLS context are
  unchanged. No schema or migration is introduced.
- [x] Targeted regression and existing audit/identity tests pass; Ruff and
  Pyright report no new errors; `check_boundaries.py` does not increase.
- [x] Independent review approves the implementation; only then may the plan
  status move from `in-progress`.

## Verification rungs

- Rung 1 — Ruff: `.venv/bin/ruff check app/core/store.py tests/test_phase2_database.py <identified-thread-or-identity-test>`
- Rung 2 — Pyright: `.venv/bin/pyright app/core/store.py`
- Rung 3 — Targeted tests: `.venv/bin/pytest tests/test_phase2_database.py <identified-thread-or-identity-test> -q`
- Rung 5 — Boundary: `python3 .github/skills/core-boundary-check/scripts/check_boundaries.py | tail -1`
- Database-backed verification must use the repository's configured test database and exercise the real transaction/RLS path; record when unavailable rather than replacing it with a mock-only claim.

## Baseline

```text
Reported runtime reproduction (not independently rerun by the architect):
- Authenticated GET /v1/me: 200 for the configured tenant.
- Mismatched tenant authorization: 403.
- Browser POST /v1/threads: 500.
- Failure path: app/core/store.py create_thread (reported near line 1102)
  -> _insert_audit_event (reported near line 749); asyncpg DataError because
  "$2" received "local-dev" where audit_events.tenant_id expects UUID.
- No database migration is required or approved.

Completion evidence reported by the user: an actual Docker-backed authenticated
`POST /v1/threads` returns 201; the configured-tenant identity path returns 200
and mismatched-tenant authorization returns 403. The new direct database and
authenticated API tests pass, and independent review approved. These results
were not independently rerun during this documentation-only update; do not infer
a commit or additional test coverage.
```

## Risks and rollback

- Resolving through an unintended selector path could associate an event with the
  wrong tenant. Use only the existing canonical UUID resolver and test both slug
  and UUID inputs plus unknown selectors.
- A shared producer change could disrupt administrative, workflow, or artifact
  audits. Inventory all callers and preserve their current contract and query
  behavior.
- An out-of-transaction audit write could leave an unaudited thread or an audit
  event for a rolled-back operation. Preserve and test the existing transaction
  boundary.
- Rollback: revert the generic producer correction and its regression coverage;
  no database rollback is required.

## Decisions needed

None. The user approved the generic core correction and explicitly ruled out a
database migration.

## Log

- 2026-11-09 architect: initial in-progress handoff; superseded by the completion
  evidence below.
- 2026-11-09 architect: marked done on user-reported independent approval and
  actual Docker-backed `POST /v1/threads` 201; configured identity 200 and
  mismatched-tenant 403 were also confirmed. Direct-DB and authenticated API
  tests were reported passing. No independent rerun or commit is claimed.
