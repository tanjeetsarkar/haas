# 032 — Generic artifact multipart validation and tenant context cleanup

- Status: done — parser/context correction reviewed; 112 targeted tests reported passing
- Type: slice
- Owner agent: core-engineer
- Epic: none
- ADR: [ADR-0019](../adr/0019-artifact-multipart-validation-tenant-context.md)

## Goal

Correct multipart validation and tenant-context cleanup on the generic artifact
upload API so SDKs and plugin services can use the existing public endpoint without
internal errors or leaked request context. Preserve existing authentication,
authorization, tenant isolation and public API shape.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Artifact multipart request validation | Core | Generic public API validation remains necessary if any plugin is removed; document-ingestion and evaluation plugins can both consume it. |
| Async tenant dependency context lifecycle | Core | Tenant context cleanup and authorization are kernel tenancy responsibilities, not behavior owned by a particular plugin. |
| Artifact upload request tests and contract evidence | Core | These verify the generic API and its tenancy boundary. |

## Scope

**Allowed paths:** `app/api/artifacts.py`, `app/api/deps.py`, `tests/test_artifact_upload.py`, `docs/api-changelog.md`, `docs/openapi-v1.json`, `docs/adr/0019-artifact-multipart-validation-tenant-context.md`, `docs/adr/README.md`, `docs/plans/032-artifact-upload-validation-fix.md`, `docs/plans/README.md`.

**Out of scope:** plugin-specific upload behavior; authentication or authorization changes; RLS/database/schema changes; new dependencies; unrelated artifact API changes; unrelated plans or status edits; commits.

**Planned files:**
- `app/api/artifacts.py` — validate supported multipart metadata through the typed request contract.
- `app/api/deps.py` — ensure tenant context is cleaned up in the same async execution context on success, error and cancellation.
- `tests/test_artifact_upload.py` — cover valid string and SDK Blob-style multipart metadata, malformed input, and tenant context lifecycle.
- `docs/api-changelog.md` — record the correction if required by the existing API documentation practice.
- `docs/openapi-v1.json` — regenerate/check the snapshot; expected to remain equal because the public contract is unchanged.

## Acceptance criteria

- [x] Multipart parsing/typed validation no longer produces the reported validation-related 500; a valid multipart request reaches the database and end-to-end upload is verified under plan 034.
- [x] Malformed multipart metadata receives a client validation response (422), not an internal error.
- [x] Tenant ContextVar cleanup is performed in the same async context for normal completion, raised errors and cancellation; tests demonstrate no tenant context leakage.
- [x] Authentication and tenant authorization are unchanged; no RLS, database, schema or dependency changes are introduced.
- [x] OpenAPI snapshot equality is preserved and the API changelog reflects the fix as appropriate.
- [x] Targeted artifact upload tests pass, including the new regression cases; Ruff and Pyright report no new errors.
- [x] `check_boundaries.py` has no increase over the recorded baseline.
- [x] Independent review approves the change; the parent reports actual authenticated artifact upload/download through the secure proxy as end-to-end integration evidence.

## Verification rungs

- Rung 1 — Ruff: `.venv/bin/ruff check app/api/artifacts.py app/api/deps.py tests/test_artifact_upload.py`
- Rung 2 — Pyright: `.venv/bin/pyright app/api/artifacts.py app/api/deps.py`
- Rung 3 — Targeted tests: `.venv/bin/pytest tests/test_artifact_upload.py -q`
- Rung 5 — Boundary: `python3 .github/skills/core-boundary-check/scripts/check_boundaries.py | tail -1`
- Rung 7 — API contract: `.venv/bin/python -m scripts.update_openapi_snapshot` and verify no snapshot diff.
- API integration: parent reports actual authenticated artifact upload/download through the secure proxy. The exact Docker refresh command was not supplied.

## Baseline

```text
Reported evidence; not rerun by the architect in this documentation-only turn:
ruff:     pass
pyright:  pass
pytest:   112 targeted tests passed (independently reported)
boundary: raw result reported as 175/9, unchanged (delta 0; count labels not supplied)
OpenAPI:  snapshot equality reported
Docker/API: actual authenticated artifact upload/download through secure proxy reported under plan 034
Review:   independent review approved
```

The original failure was a runtime multipart `POST` producing a 500 during request
validation, with tenant ContextVar cleanup attempting to reset a token from a
different context. Preserve this failure description as the regression target; do
not broaden the slice into unrelated upload behavior.

Multipart metadata encoding clarification: the API accepts metadata as ordinary
multipart form text and the SDK's supported JSON/Blob-style metadata part; the
file content itself is carried as a file/Blob. Both metadata forms undergo the
same typed validation. This is not a request to require a Blob for browser
FormData callers.

Completion evidence reported by the parent: 112 targeted tests pass and
independent review approved. Plan 034 additionally reports a real browser-native
FormData/Blob round trip through the secure proxy, including successful artifact
save and download. The implementation and verification results were not rerun
during this documentation-only update.

## Risks and rollback

- Strict validation may reject malformed metadata that previously reached a server error; return a clear client validation response and preserve valid existing string and SDK Blob-style forms.
- Tenant-context cleanup changes can expose lifecycle assumptions in request handling; test success, exception and cancellation paths without altering authorization or RLS.
- An unexpected OpenAPI diff may indicate a public contract change; stop and obtain separate review rather than silently accepting it.
- Rollback: revert only the artifact validation and dependency lifecycle changes with their regression tests; no migration or data rollback is required.

## Decisions needed

None. The user approved the generic validation and tenant-context cleanup direction. Independent review and Docker refresh are completion gates, not unresolved design choices.

## Log

- 2026-11-09 architect: recorded parent-reported completion evidence: 112 targeted
  tests pass, independent review approved, and an actual browser-native artifact
  round trip through the secure proxy is recorded in plan 034. Canonical-schema
  CRUD remains tracked separately by plan 034.
