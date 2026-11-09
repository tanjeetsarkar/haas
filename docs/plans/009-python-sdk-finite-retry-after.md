# 009 — Finite Python SDK Retry-After handling

- Status: done — implementation verified and focused reviewer approved
- Type: slice
- Owner agent: client-engineer
- Epic: [001](001-atlas-extraction-sdk-epic.md) (independent focused SDK fix)
- ADR: [ADR-16](../adr/0016-api-contract-toolchain.md) — existing generic
  contract/compatibility decision; no new kernel extension point

## Goal

Prevent non-finite server-provided `Retry-After` values from reaching Python SDK
retry sleeps or response-derived public error metadata, while preserving all
finite numeric behavior. This is a generic client bug fix, not a server or wire
contract change.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Retry scheduling and response-derived `APIError.retry_after` | Generic core client | The public SDK exists without any particular plugin; plugins such as web-research and incident-triage can use it. |
| Mock-transport regressions | Client tests | Exercise shared Python client behavior, not plugin-specific logic. |

This fixes an existing generic SDK behavior, not a new kernel capability or
plugin extension point. ADR-16 is existing contract context; no new ADR or
server/core-runtime change is proposed.

## Scope

**Allowed implementation paths:** `haas_sdk/_core.py`, `haas_sdk/errors.py`,
`tests/sdk/test_sdk_client.py`. **Planning paths:** this plan and
`docs/plans/README.md` for progress/evidence. No other edits. Keep the slice
under 400 changed non-generated lines; no migration or new dependency.

**Planned files:**

- `haas_sdk/_core.py` — parse the header, reject non-finite floats before the
  existing `[0, 60]` clamp, and retain the existing malformed/missing fallback.
- `haas_sdk/errors.py` — reject non-finite parsed header floats before returning
  response-derived error metadata; keep finite values exactly as currently
  parsed. Correct the misleading `RateLimitError` cap docstring in this file if
  needed: error metadata is currently *not* capped.
- `tests/sdk/test_sdk_client.py` — extend the existing mock-transport retry and
  error tests with deterministic sync and async regressions. **This file is
  user-reported dirty:** preserve and integrate its existing edits. The current
  working file was inspected, including its retry and error tests; no comparison
  against its pre-existing diff was available in this planning session. Before
  editing, inspect and record its path-specific diff and coordinate overlapping
  changes; never reset or overwrite them.

**Out of scope:** server behavior, HTTP-date support, new public names or
signatures, TS/CLI/web, plugins/services, SDK configuration validation,
arbitrary user-constructed `APIError(retry_after=...)`, and changes outside the
listed paths. Do not inspect, read, hash, diff, format, stage, or otherwise
touch `haas_sdk_ts/src/lib/atlasClient.ts`, including indirectly through broad
tools. Do not use global `git diff`, `git diff --check`, formatters or scans.

## Current evidence and intended semantics

- [`_core.py:169–179`](../../haas_sdk/_core.py) converts `float(value)` and
  clamps with `min(max(...))`. The user reports a local `NaN` probe returning
  `nan`; that probe was not rerun here. `retry_delay` uses the parsed value
  directly or existing jittered backoff if it is `None`.
- [`errors.py:257–266`](../../haas_sdk/errors.py) separately returns
  `float(value)` without a clamp. `api_error` uses this for all response-derived
  `APIError` instances, not just 429. Its existing `RateLimitError` docstring
  says capped, but the actual parser does not cap.
- [`test_sdk_client.py:221–240`](../../tests/sdk/test_sdk_client.py) already
  covers a valid `2`-second hint and idempotency key reuse;
  [`:406–414`](../../tests/sdk/test_sdk_client.py) covers `1` as error metadata.
  Shared `retry_delay` is used by both sync and async clients.

The following table defines header behavior; it does not change directly
constructed exceptions. “Fallback” means existing exponential full-jitter
backoff with normal finite defaults, not “no retry.”

| Header | Scheduling hint / retry sleep | Response-derived error `retry_after` |
|---|---|---|
| `NaN`, `+inf`, `-inf`, malformed, absent or empty | Ignore; finite fallback if otherwise retryable | `None` |
| `-2` | `0.0` | `-2.0` (preserve) |
| `2` | `2.0` | `2.0` |
| `120` | `60.0` | `120.0` (preserve, do not introduce a metadata cap) |

Reject any other float spelling or overflow that parses to a non-finite value
(e.g. `1e309`) in both paths. Preserve existing status eligibility, retry
limits, jitter, idempotency behavior, and error types. No promise is made for
non-finite caller-supplied backoff configuration; that is a separate issue.
Use stdlib `math.isfinite` after conversion and before clamping. Avoid importing
`_core` from `errors`: `_core` already imports `errors`. Small local validation
in both existing parsers is acceptable; avoid a new module or public helper.

## Implementation sequence

1. Obtain focused reviewer approval of this proposed plan. Before runtime/test
   edits, record fresh baseline and path-specific dirty-file diff as below.
2. Implement the two finite checks without changing the existing behavior for
   any finite number; update only the inaccurate in-file docstring if needed.
3. Extend the existing dirty test file without removing its prior edits. Use
   deterministic mocked jitter and injected recording sleep callbacks, not real
   sleeps. Exercise public `Haas` and `AsyncHaas` with mock transport.
4. Run the same available verification rungs as baseline, add the new targeted
   regression selection, compare failure IDs and diagnostics, and request
   independent review. Stop on a required breaking change, unrelated failure, or
   any need to broaden scope.

## Acceptance criteria

- [x] Header cases `NaN`, `+inf`, `-inf`, malformed, negative, valid seconds,
  above-cap seconds, missing/empty and overflow-to-infinity are covered. For
  each, assert the table's scheduling and response-error results, including the
  preserved negative and above-cap metadata behavior.
- [x] For both sync and async public clients, mock a retryable response followed
  by success; assert the number of attempts and the injected sleep argument.
  Non-finite and malformed headers select a deterministic finite fallback, never
  a non-finite sleep; finite values retain existing clamp behavior. No real sleep.
- [x] Exercise response-derived errors after exhaustion or with retries disabled
  for 429 and a non-429 status; non-finite and malformed values yield `None`,
  while finite values are unchanged. Existing status classes and context remain
  unchanged. Existing valid-seconds/idempotency and error tests remain intact.
- [x] All new focused tests pass; no new failures by test ID versus fresh full
  SDK baseline. Ruff and Pyright show no new diagnostics. Preserve the existing
  dirty edits and restrict slice-owned edits to allowed paths.
- [ ] Boundary check baseline → after: **pending / unavailable under the
  protected-file restriction**, not a claimed zero delta. Record that limitation
  and independently review the allowed Python diff for plugin-specific
  references. Do not run the standard boundary scanner: its configured
  `haas_sdk_ts/src` traversal reads the protected `.ts` file.
- [x] Focused reviewer approves the final implementation and evidence; plan
  status is updated only after verification and review.

## Verification rungs

Rungs 1, 2 and 3, before edits and after implementation:

```text
.venv/bin/ruff check haas_sdk/_core.py haas_sdk/errors.py tests/sdk/test_sdk_client.py
.venv/bin/pyright haas_sdk
.venv/bin/pytest tests/sdk/test_sdk_client.py -q
.venv/bin/pytest tests/sdk -q
git status --short -- haas_sdk/_core.py haas_sdk/errors.py tests/sdk/test_sdk_client.py docs/plans/009-python-sdk-finite-retry-after.md docs/plans/README.md
git diff -- tests/sdk/test_sdk_client.py
git diff --check -- haas_sdk/_core.py haas_sdk/errors.py tests/sdk/test_sdk_client.py docs/plans/009-python-sdk-finite-retry-after.md docs/plans/README.md
```

After implementation also run a named selection for the added regressions and
review the path-limited implementation diff, distinguishing pre-existing dirty
changes from slice-owned changes. Record command exit status, counts, failure
IDs and diagnostics, not only summaries. Do not fix pre-existing failures.
Rung 5 is not runnable as configured without reading the protected file; do not
alter scanner configuration or bypass the restriction. Full repository pytest,
TS builds and broad scans are out of scope.

## Baseline

Before implementation, the SDK baseline was 104 passed with 5 warnings for
`pytest -q tests/sdk`, 46 passed with 1 warning for
`pytest -q tests/sdk/test_sdk_client.py`, Ruff passed, and Pyright reported 0
diagnostics. The dirty test-file diff was reviewed path-locally and preserved.

## Risks and rollback

- Response-derived non-finite metadata changing to `None` is an intentional
  safety correction on invalid inputs; consumers relying on NaN/infinity may
  observe a change. Ordinary finite values remain unchanged. If review finds a
  public breaking-change hard stop, stop and seek user direction.
- The protected-file restriction prevents the normal repository-wide boundary
  count; report it as unavailable rather than infer a clean result.
- Preserve the dirty test edits: isolate and revert only this slice's changes if
  rollback is required; do not reset files or overwrite pre-existing work.

## Decisions needed

None, provided the finite-input compatibility and invalid-input safety
correction are accepted as nonbreaking. Focused reviewer approval is an
implementation gate, not a request for a semantics decision.

## Log

- 2026-11-09 architect: proposed; inspected current SDK parsers and existing
  working-file tests, documented the separate metadata semantics and
  protected-file boundary-check limitation. No baseline or implementation run.
- Implementation: both Retry-After parsers now reject non-finite floats while
  preserving finite scheduling clamps and finite error metadata. Regression
  tests cover missing, empty, malformed, non-finite, overflow, negative,
  ordinary, and above-cap values; sync and async public-client retries verify
  deterministic finite fallback and finite server hints.
- Verification: focused Retry-After selection — 15 passed, 44 deselected;
  `pytest -q tests/sdk` — 117 passed, 5 warnings; Ruff passed; Pyright reported
  0 errors, 0 warnings, 0 informations; scoped `git diff --check` passed.
  Compared with the fresh baseline, the SDK suite increased from 104 to 117
  passing tests with no new failures. The protected-file restriction prevented
  a standard boundary scan; no boundary-clean result is claimed.
- Review: focused reviewer approved the parser change and sync/async coverage
  with no significant issues.
