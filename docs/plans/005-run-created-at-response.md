# 005 — Add persisted creation time to run submission responses

- Status: done
- Type: slice
- Owner agent: core-engineer
- Epic: [001](001-atlas-extraction-sdk-epic.md)
- ADR: [ADR-0016](../adr/0016-api-contract-toolchain.md) — additive HTTP contracts use the generated OpenAPI snapshot and API changelog

## Goal

Add the already-persisted run creation time to successful `POST /v1/runs`
responses. This is the narrow producer-side resolution authorized by Option 1
for plan 004: the existing TypeScript SDK requires `RunResponse.created_at`.
Preserve the current 202 response fields and make idempotent replays return the
same persisted timestamp.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Generic run-submission response and persistence plumbing | Core | Run lifecycle and its public REST contract exist independently of any plugin; plugins such as web-research and incident-triage can consume the generic run API |
| Producer-backed test, OpenAPI snapshot and changelog | Core contract | These verify/document a generic API response, not plugin-specific behavior |

## Scope

**Allowed paths:** `app/main.py`, `app/core/store.py`,
`tests/test_phase2_database.py`, `docs/openapi-v1.json`,
`docs/api-changelog.md`, and this plan's progress/evidence.

**Out of scope:** all SDK/client edits (especially user-owned TypeScript files),
other run response routes, migrations, new timestamps or timestamp defaults,
unrelated API changes, and plugin-specific behavior.

**Planned files:**

- `app/main.py` — preserve the existing 202 fields and include the timestamp
  returned from persistence; use a typed response shape if needed so OpenAPI
  explicitly documents this endpoint's response.
- `app/core/store.py` — derive the timestamp from the row returned by the
  persisted `runs` insert and ensure the idempotency response saved in the same
  transaction contains that same value.
- `tests/test_phase2_database.py` — extend the PostgreSQL-backed API producer
  test to compare the response timestamp with the persisted run and assert a
  same-key replay preserves the response.
- `docs/openapi-v1.json` — regenerate with the repository command and retain
  the generated contract update.
- `docs/api-changelog.md` — record the additive `created_at` response field
  under Unreleased / Added.

### Exact response contract

For a newly admitted run, HTTP 202 JSON contains all existing fields unchanged
plus:

```json
{
  "run_id": "<existing run id>",
  "thread_id": "<existing thread id>",
  "status": "queued",
  "stream_url": "/v1/runs/<run_id>/stream",
  "events_url": "/v1/runs/<run_id>/events",
  "created_at": 1762579961.125
}
```

`created_at` is a JSON number in epoch seconds, matching the required numeric
`RunResponse.created_at` in `haas_sdk_ts/src/types.ts`. Its authority is the
persisted `runs.created_at` value (`DOUBLE PRECISION`) returned by the insert;
the existing migration trigger mirrors that value to `created_at_ts`
(`TIMESTAMPTZ`). Do not use a new `time.time()` value, response-time clock,
`created_at_ts` ISO string, or another guessed timestamp. The exact literal
above is illustrative only.

The current FastAPI response annotation yields a generic object in OpenAPI.
Model the response fields without narrowing the existing extensible-object
contract, then regenerate the snapshot so the 202 response explicitly includes
the new field and retains `run_id`, `thread_id`, `status`, `stream_url`, and
`events_url`.

## Implementation sequence

1. Before editing, capture a fresh baseline with the commands below, including
   the PostgreSQL-backed target if its disposable qualification database is
   available. Record `git status --short` and preserve all unrelated dirty
   files; do not inspect, format, stage, or modify user-owned TypeScript files.
2. Read the persisted insert row's `created_at` and make it available to the
   API response. Preserve atomic admission behavior: a newly stored
   `admission_requests.response` and the initial HTTP response must carry the
   same value.
3. Ensure same-key replays return the stored timestamp unchanged. If a
   pre-deployment admission response lacking this field can still be replayed,
   enrich it only from that run's persisted `runs.created_at`; never synthesize
   one. Stop for a decision if a legacy replay cannot be resolved from
   persistence without changing unrelated error behavior.
4. Add producer-backed assertions, regenerate the OpenAPI snapshot, and update
   the changelog.

## Acceptance criteria

- [x] `POST /v1/runs` continues returning HTTP 202 with the existing
  `run_id`, `thread_id`, `status`, `stream_url`, and `events_url` fields and
  adds numeric epoch-seconds `created_at`.
- [x] Producer-backed test proves `created_at` equals the authoritative
  persisted `runs.created_at` value; no wallclock or guessed timestamp is used.
- [x] Same-key idempotent replay returns the same `created_at` and preserves
  the other response fields.
- [x] The OpenAPI 202 response documents `created_at` and retains the existing
  response fields; rerunning the snapshot generator produces no further diff.
- [x] `docs/api-changelog.md` records this additive field. No breaking-change
  allowlist entry is added.
- [x] Only allowed paths change; no migration or SDK/client file changes, no
  unrelated dirty file is altered, and `git diff --check` passes.
- [x] Ruff, Pyright, OpenAPI snapshot test, snapshot-generator idempotency,
  focused fake-store tests and full pytest introduce no new failures against
  their fresh baselines.
- [x] DB-backed producer assertions prove persisted timestamp equality and
  same-key replay behavior.
- [x] Boundary check reports no increase from the known baseline
  (Atlas 0/0, atlas-explorer 134/1, overall 155/9).

## Verification rungs

Run baseline and post-change selections:

```text
.venv/bin/pytest tests/test_phase2_database.py::test_real_api_key_identity_and_admin_queries_under_rls -q
.venv/bin/ruff check app/main.py app/core/store.py tests/test_phase2_database.py
.venv/bin/pyright app/main.py app/core/store.py
.venv/bin/python -m scripts.update_openapi_snapshot
.venv/bin/pytest tests/test_phase0.py::test_openapi_v1_snapshot_matches -q
.venv/bin/pytest -q
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas-explorer
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py
git diff --check
git status --short
```

The targeted API test requires `HAAS_TEST_POSTGRES_DSN` pointing to the
disposable, migrated qualification database described at the top of
`tests/test_phase2_database.py`. Capture the focused test and full-suite
baselines before edits. The OpenAPI snapshot test verifies the generated
snapshot; run the generator a second time and confirm it makes no additional
snapshot changes.

## Baseline

Fresh pre-edit verifier baseline supplied by the user on 2026-11-08:

- Targeted DB selector exists: 1 skipped because `HAAS_TEST_POSTGRES_DSN`
  is unset (ENV, not pass).
- Ruff on the planned Python paths: passed.
- Pyright on `app/main.py app/core/store.py`: 1 PRE-EXISTING error at
  `store.py:1104` (`str` argument for UUID `tenant_id`).
- OpenAPI snapshot test: 1 passed. The verifier's generator changed only
  `docs/openapi-v1.json` (70 insertions/1 deletion); this generated baseline
  diff was inspected before editing and is preserved.
- Full pytest: 495 passed, 15 PRE-EXISTING failures, 41 skipped: plugin-cli
  doctor, incident tool sandbox, 8 SDK testing tests, 2 core tests,
  2 production profile tests, skill allowlist test. Not fixed in this slice.
- Boundary: Atlas 0/0, atlas-explorer 134/1, overall 155/9.
- `git diff --check`: passed.
- Dirty baseline also includes user-owned TS source, SDK tests, plan index,
  untracked plans and services. No unrelated paths are edited or staged.

## Risks and rollback

- A creation timestamp assembled outside the insert result could disagree with
  the committed record; source it from the persisted returned row and compare
  it in the producer-backed test.
- Admission replay responses are persisted transactionally. Keep the newly
  added timestamp and original fields together in the stored response; handle
  any still-live pre-change response only from its persisted run row.
- Rollback: revert only this slice's implementation, tests, OpenAPI and
  changelog changes. No migration or destructive data change is involved.
  Leave unrelated user changes and TypeScript files untouched.

## Decisions needed

None for the authorized additive field. If a live legacy idempotency record
cannot be enriched from its persisted run timestamp, stop and ask before
changing replay/error behavior.

## Log

- 2026-11-08 architect: user selected Option 1 for blocker 004; planned the
  authorized persisted-timestamp addition. Implementation awaits fresh
  baseline.
- 2026-11-08 core-engineer: implemented the authorized additive producer change.
  Plan remains in-progress for independent verification and review.
  Delegations remain 2/6; review rounds 0/2; no budget override.

## Implementation / evidence (2026-11-08)

- `DistributedStore.create_run` returns `float(run_row["created_at"])` after
  committing the existing insert. No new clock, default, migration or dependency.
- The HTTP producer opts into `include_created_at`; admission response JSON
  receives that value before insertion in the same transaction. The initial
  response uses the returned value. Other internal admission consumers retain
  their existing response shape via the default-false keyword.
- Early and admission-race replays use the same timestamp-aware store lookup.
  Legacy responses missing the field are enriched from a tenant-scoped join
  through `(admission_requests.tenant_id, run_id)` to the persisted run.
  Migration 0009 already enforces that composite foreign key; association is
  reliable, so no decision is needed. Stored timestamps are not replaced.
- `CreateRunResponse` documents six required fields and
  `additionalProperties: true`. It is supplied through FastAPI's `responses`
  metadata, preserving the existing extensible dictionary runtime serializer.
  No plugin extension point or SDK change is introduced.
- Producer assertions cover unkeyed insertion, keyed insertion versus persisted
  `runs.created_at` and admission JSON, same-key replay, and a simulated live
  legacy response. These DB-backed assertions await a qualification database.
- Changelog updated under Unreleased / Added. Preserved the verifier's generated
  snapshot baseline; slice adds the response component and 202 reference only.
  All edits are in the six allowed paths; no staging/reset/SDK edits.

### Commands and results versus baseline

```text
.venv/bin/ruff check app/main.py app/core/store.py tests/test_phase2_database.py
  PASS; unchanged.
.venv/bin/pyright app/main.py app/core/store.py
  1 PRE-EXISTING error at store.py:1104; unchanged, zero new errors.
.venv/bin/pytest tests/test_phase2_database.py::test_real_api_key_identity_and_admin_queries_under_rls tests/test_phase2_database.py::test_api_revision_journey_policy_denials_and_history -q
  2 skipped (ENV: HAAS_TEST_POSTGRES_DSN unset); original target remains skipped.
.venv/bin/python -m scripts.update_openapi_snapshot
.venv/bin/pytest tests/test_phase0.py::test_openapi_v1_snapshot_matches -q
  1 passed; unchanged.
.venv/bin/pytest tests/test_skill_policy.py::test_create_run_persists_effective_allowlist_and_fingerprint tests/test_skill_policy.py::test_run_request_can_only_narrow_tenant_allowlist tests/test_skill_policy.py::test_preload_rules tests/test_tenant_policy_api.py::test_authenticated_run_uses_policy_models_budgets_and_revision tests/test_tenant_policy_api.py::test_authenticated_run_webhook_must_match_exact_policy_host -q
  5 passed; existing fake-store response serialization preserved.
.venv/bin/pytest -q
  Final: 495 passed, 15 failed, 41 skipped, 5 warnings in 35.72s; unchanged.
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas | tail -1
  0 errors / 0 warnings; unchanged.
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas-explorer | tail -1
  134 errors / 1 warning; unchanged.
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py | tail -1
  155 errors / 9 warnings; unchanged.
git diff --check
  PASS.
git status --short
  Six slice paths changed; unrelated dirty paths remain untouched.
```

Snapshot idempotency command (passed; final SHA256
`5851900dce6e232cbbdc0242503a5622e4b0615b50c2e9273d3f9a857c6c5ae9`):

```sh
.venv/bin/python -c 'import hashlib, pathlib, subprocess; p=pathlib.Path("docs/openapi-v1.json"); before=hashlib.sha256(p.read_bytes()).hexdigest(); subprocess.run([".venv/bin/python", "-m", "scripts.update_openapi_snapshot"], check=True); after=hashlib.sha256(p.read_bytes()).hexdigest(); assert before == after; print("Snapshot generator idempotent:", after)'
```

Contract smoke command (passed: required fields, numeric epoch seconds, open
schema, and extra-field preservation):

```sh
.venv/bin/python -c 'import json; from app.main import CreateRunResponse; doc=json.load(open("docs/openapi-v1.json")); response=doc["paths"]["/v1/runs"]["post"]["responses"]["202"]["content"]["application/json"]["schema"]; schema=doc["components"]["schemas"][response["$ref"].split("/")[-1]]; fields={"run_id", "thread_id", "status", "stream_url", "events_url", "created_at"}; assert fields == set(schema["required"]); assert schema["properties"]["created_at"]["type"] == "number"; assert schema["additionalProperties"] is True; body=dict(run_id="run_fake", thread_id="thd_fake", status="queued", stream_url="/stream", events_url="/events", created_at=123.125, future_field="retained"); assert CreateRunResponse.model_validate(body).model_dump() == body; print("Contract: six required fields, numeric created_at, extensible response round-trip passed")'
```

During implementation, an initial misplaced row read/type inference issue was
corrected, then fast checks returned to baseline. First full-suite run:
490 passed / 20 failed / 41 skipped. The five NEW failures were caused by
runtime response-model validation of existing fake stores returning `None`.
Using documentation-only response metadata preserved existing serialization
without synthesizing timestamps; the focused five tests and final full-suite
rerun confirm those failures are resolved. No PRE-EXISTING failure was fixed.

Exact PRE-EXISTING full-suite failure IDs (final matches supplied baseline):

```text
tests/cli/test_plugin_cli.py::test_repo_plugins_doctor_cleanly
tests/plugins/test_incident_tools.py::test_tools_are_sandboxed_and_unsecreted
tests/sdk/test_sdk_testing.py::test_fake_completes_a_scripted_run
tests/sdk/test_sdk_testing.py::test_fake_approval_flow_with_handler
tests/sdk/test_sdk_testing.py::test_fake_digest_mismatch_is_conflict
tests/sdk/test_sdk_testing.py::test_fake_single_winner_decision
tests/sdk/test_sdk_testing.py::test_fake_fail_next_injects_fault
tests/sdk/test_sdk_testing.py::test_fake_wait_without_handler_raises_approval_required
tests/sdk/test_sdk_testing.py::test_fake_rejected_decision_cancels_run
tests/sdk/test_sdk_testing.py::test_fake_stale_review_refused_locally
tests/test_core.py::test_discover_sample_plugins
tests/test_core.py::test_execute_tool_paths
tests/test_phase0.py::test_production_profile_requires_identity_and_encryption
tests/test_phase0.py::test_production_api_can_pass_phase_one_configuration_gate
tests/test_skill_policy.py::test_dispatch_blocks_tools_of_skills_outside_allowlist
```

### Initial database verification state (superseded)

The initial verifier confirmed `HAAS_TEST_POSTGRES_DSN` was unset. The two
producer-backed database tests were skipped (2 skipped); no database was
contacted or set up in that pass. This was superseded by the user-authorized
disposable local database verification below.

### Independent verification and review evidence (2026-11-08)

- Reviewer verdict: **APPROVE source, no findings**.
- Ruff: passed.
- Pyright: 1 error, same pre-existing baseline error.
- OpenAPI snapshot test: 1 passed; snapshot generator is idempotent.
- Full suite: 495 passed, 15 failed, 41 skipped; same pre-existing failure IDs
  listed above, no new failures.
- Boundaries unchanged: Atlas 0 errors/0 warnings; atlas-explorer 134/1;
  overall 155/9.
- Initial DB run: 2 tests skipped because `HAAS_TEST_POSTGRES_DSN` was unset.

### User-authorized DB-backed verification

- The user approved provisioning an isolated, disposable local PostgreSQL 16
  container plus applying the project's migrations. A new local test database
  was provisioned and fully migrated; no existing database or data was changed.
- Required selectors passed against that database:
  `test_api_revision_journey_policy_denials_and_history` and
  `test_real_api_key_identity_and_admin_queries_under_rls` — **2 passed**.
- The DB-backed checks cover persisted `created_at` equality, equality with
  the stored admission response, same-key replay, and legacy response
  enrichment from the persisted run timestamp.
- The temporary database container was stopped and removed after verification.
  No database remains running from this test setup.
- Ruff passed. Pyright retained the same pre-existing `app/core/store.py:1104`
  error; no new Pyright error. Overall boundary result remained 155 errors /
  9 warnings, unchanged from baseline. Reviewer verdict remains **APPROVE,
  no findings**.

No migration round-trip is applicable (zero migrations). Planned file-list
deviations: none.

### Integration reconciliation

- Slice 005 specialist task invocations totaled **9/6** when counting all
  specialist invocations. **Budget override:** the initial DSN was unavailable;
  after the user authorized provisioning a disposable, fully migrated local
  PostgreSQL 16 container, an additional verifier pass and plan reconciliation
  were needed to establish DB-backed evidence.
