# 003 — Characterize the existing Python SDK contracts

- Status: done
- Approval: approved by user on 2026-11-08; decision 1 resolved as option A
- Type: slice
- Owner agent: client-engineer
- Epic: [001](001-atlas-extraction-sdk-epic.md)
- ADR: none — tests only; no API or behavior decision is authorized

## Goal

Add focused tests that characterize the current public Python SDK contract from
the implementation and actual API producers. Lock existing import paths,
signatures, return-model types and error mapping for both `Haas` and `AsyncHaas`.
This is a compatibility baseline, not a bug-fix or redesign slice.

## Boundary classification

| Change | Owner | Why |
|---|---|---|
| Generic Python SDK contract tests | Client/core | The SDK remains useful if Atlas is deleted; other plugins such as web-research and incident-triage can use it |
| API producer and SDK implementation inspection | Read-only inputs | Tests must reflect current behavior, not aspirational examples |

No core or plugin runtime change is planned, so no core ADR is required.

## Scope and paths

**Only edit:** `tests/sdk/test_sdk_contract.py`,
`tests/sdk/test_sdk_client.py`, `tests/sdk/test_sdk_models.py`, and this plan's
progress/evidence. Prefer extending these existing tests over adding fixtures or
snapshot infrastructure. Read-only contract sources are `haas_sdk/**`,
`app/main.py`, the relevant existing producer tests, and `docs/contracts/**`.

The inspected SDK root currently exports `Haas`, `AsyncHaas`, run handles,
models, errors, `Secret`, `constants` and `webhooks` through `haas_sdk.__all__`.
The sync constructor accepts keyword-only transport/configuration options,
including base URL, credentials, tenant selector, timeout/retries, reconnect
settings, headers, transport/client injection and compatibility checks. Derive
the precise sync and async method/signature cases from the current implementations
and existing tests at execution time; do not copy signatures from design prose.

The existing `tests/sdk/test_sdk_contract.py` exercises `AsyncHaas` against the
real FastAPI app using `httpx.ASGITransport`, with a stubbed store. Reuse its
producer-backed approach and existing test-local transport/fixtures where
appropriate. Use actual route responses and schemas from the current app,
producer tests and contract files as inputs. No external network is needed.

**Out of scope:** any edit to `haas_sdk/**`, `app/**`, `docs/contracts/**`,
TypeScript/CLI/web clients, Atlas service or plugins; API/schema changes,
signature fixes, behavior changes, generated clients, dependency changes,
new public names, or changes inferred only from aspirational docs. In particular,
do not read, edit, format, stage or otherwise touch the user-owned
`haas_sdk_ts/src/lib/atlasClient.ts`.

## Implementation sequence

1. Verifier records `git status --short`, captures the protected TS path's
   existing diff read-only, and runs the baseline commands below before any
   engineer edits. Preserve all existing working-tree changes; the engineer
   receives the recorded baseline before starting characterization tests.
2. Compare root exports and current public sync/async entry points with existing
   tests. Derive expected signatures and outcomes from executable implementation
   and producer responses; do not invent expected values.
3. Add only characterization assertions in the three allowed test files.
   Cover importability, sync/async public signatures, representative successful
   response-to-model results, and representative producer error-to-SDK-exception
   mappings already established by current behavior.
4. Run focused and complete SDK tests, lint, type and boundary gates; record
   exact counts and ensure the protected TS diff is unchanged.

## Acceptance criteria

- [x] Diff contains only the three named `tests/sdk/` files and this plan's
  progress/evidence; at most 400 changed non-generated lines. No runtime,
  contract, package, fixture-infrastructure or TypeScript file is changed.
- [x] Tests pin the existing `haas_sdk` root exports and supported import paths
  for `Haas`, `AsyncHaas`, models, errors and handles; no new SDK names are added.
- [x] Tests pin the current public sync and async constructor/method signatures
  from the actual implementation, including parameter names/defaults and
  sync-versus-async calling shape for the covered operations.
- [x] Producer-backed cases assert the current returned model classes and key
  parsed values, plus existing status/error-to-exception types and useful
  request context where present. Fixtures are traceable to current producer
  responses; no docs-only contract is treated as implemented.
- [x] `.venv/bin/pytest tests/sdk -q` introduces no test failures versus the
  recorded baseline (95 passed, the same 8 pre-existing failures). Focused SDK
  tests pass.
- [x] Ruff passes on changed test files; pyright introduces no new SDK
  diagnostics; boundary checker counts do not increase.
- [x] The user-owned TypeScript file's diff is byte-for-byte unchanged.
- [x] If any assertion would require changing server/API behavior, SDK behavior,
  signatures, models or error semantics to pass, stop and report that contract
  mismatch as a blocker. Do not repair or redefine the contract in this slice.

## Baseline and verification protocol

Baseline has not been run for this approval update. Verifier must establish and
report it before the engineer's first test edit. Record the working tree and run:

```text
.venv/bin/pytest tests/sdk -q
.venv/bin/ruff check tests/sdk
.venv/bin/pyright haas_sdk
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas-explorer
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py
```

The first three are the focused Python SDK baseline and gates; the boundary
commands are required to establish no change to unrelated ownership coupling.
Record pre-existing failures/diagnostics and compare after. The initial focused
contract command is:

```text
.venv/bin/pytest tests/sdk/test_sdk_contract.py tests/sdk/test_sdk_client.py tests/sdk/test_sdk_models.py -q
```

Also run the full SDK test command above after changes. Run ruff on the changed
files and pyright on `haas_sdk`; do not widen to full repository pytest for this
test-only slice. If the current baseline command is unavailable or already
failing, report its exact result and do not claim a green gate.

## Risks, unknowns and rollback

- The exact boundary between stable public entry points and incidental
  implementation details must be grounded in current exports and usage. Test
  public exports and exposed client/resource methods only; if an import or
  signature's public status is ambiguous, ask rather than locking a private
  symbol.
- Current API producer responses may expose a mismatch with aspirational docs.
  Preserve the executable contract for this characterization and report any
  required behavior/API change as a blocker for a separately approved slice.
- Rollback is limited to reverting the added/changed characterization tests and
  plan evidence. Runtime behavior and persisted data are unaffected.

## Decisions needed

None.

## Resolved decisions

1. **Public-contract mismatch requiring behavior/API change:** user approved
   recommended **option A — characterization-only** on 2026-11-08. If a test
   requires a behavior/API change, stop and request a separately scoped plan.
   Option B (changing behavior in this slice) is not authorized. The previously
   approved additive compatibility recommendation remains unchanged; it does
   not authorize runtime changes in slice 003.

## Log

- 2026-11-08 architect: proposed as the next standalone client-owned slice under
  epic 001; no implementation or API changes authorized.
- 2026-11-08 architect: user confirmed continuing the epic, approved option A,
  and authorized slice 003 to proceed; marked approved and in-progress. This
  update changes planning documents only; no baseline or runtime checks were
  run. Verifier establishes the baseline before engineer edits. All existing
  changes, including `haas_sdk_ts/src/lib/atlasClient.ts`, must be preserved.
- 2026-11-08 client-engineer: inspected `haas_sdk.__all__`, client
  constructors/run operations, `app.main` run list/events/404 producers, the
  existing ASGI SDK contract fixture, client/model tests, and
  `tests/test_phase0.py::test_http_errors_are_problem_json`. Added root export
  and import-path assertions, sync/async constructor and run-operation
  signature assertions, and producer-backed `Run`/`RunEvent` values plus the
  existing `NotFoundError` mapping. No runtime or contract files changed.
- Verification: focused SDK selection — **80 passed, 4 warnings**;
  `.venv/bin/pytest tests/sdk -q` — **95 passed, 8 failed, 5 warnings**.
  The same eight pre-existing failures remain in `tests/sdk/test_sdk_testing.py`
  (no change to its test file); the baseline was 93 passed, those 8 failed,
  and 5 warnings.
- Gates: Ruff on the three changed test files passed; `.venv/bin/pyright
  haas_sdk` reported 0 diagnostics; `git diff --check` passed. Boundaries:
  atlas 0 errors/0 warnings; atlas-explorer 134 errors/1 warning; overall
  155 errors/9 warnings — unchanged from baseline.
- Protected `haas_sdk_ts/src/lib/atlasClient.ts` diff SHA-256 remained
  `cb14fd38219926cb569c149fe00ec5f0ded0473520689ee80dbf6b20f68d4f81`.
  The verifier reported GREEN and the reviewer APPROVE; integrated 2026-11-08.
  Evidence: focused selection 80 passed; full SDK 95 passed and the same 8
  pre-existing failures in unchanged `tests/sdk/test_sdk_testing.py`; Ruff
  passed; Pyright 0 diagnostics; boundaries unchanged (atlas 0/0,
  atlas-explorer 134/1, overall 155/9). The protected TS file diff hash
  remained `cb14fd38219926cb569c149fe00ec5f0ded0473520689ee80dbf6b20f68d4f81`.
  Total slice diff was 354 changed lines including this plan.
