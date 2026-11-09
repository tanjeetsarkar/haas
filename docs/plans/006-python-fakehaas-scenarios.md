# 006 — Restore Python FakeHaas scripted scenario semantics

- Status: done
- Type: slice
- Owner agent: client-engineer
- Epic: [001](001-atlas-extraction-sdk-epic.md)
- ADR: none — generic Python SDK testing helper; no server or wire-contract change

## Goal

Resolve the eight baseline failures in `tests/sdk/test_sdk_testing.py` by
verifying and correcting the public `FakeHaas` test-helper semantics. The
baseline and final evidence below record the reproduced failures, fixes and
verification outcomes; the slice does not change server behavior or SDK network
contracts.

## Boundary classification

| Change | Core / Plugin / Client | Why |
|---|---|---|
| `FakeHaas` scenario selection and testing utility tests | Client | A generic SDK test utility remains useful without Atlas and can serve plugins such as web-research and incident-triage |
| Testing utility specification | Client docs | Documents a public generic test helper, not Atlas behavior |

No core API, server behavior, plugin, data, or network contract changes are planned.

## Scope

**Allowed paths:** `haas_sdk/testing.py`, `tests/sdk/test_sdk_testing.py`,
`docs/haas-python-sdk-spec.md`, and this plan's progress/evidence.

**Out of scope:** other `haas_sdk/**` modules, server/API behavior, TS/CLI/web
SDKs, plugins/services, package/dependency changes, new public names, and the
user-owned `haas_sdk_ts/src/lib/atlasClient.ts` (do not inspect, edit, format,
stage, or otherwise touch it).

**Planned files:**

- `haas_sdk/testing.py` — if confirmed by the fresh baseline, consume
  pre-scripted scenarios deterministically in FIFO order on successive run
  submissions; use the default fake run only when no scripted scenario remains.
  Keep the existing `script_run()` signature unchanged.
- `tests/sdk/test_sdk_testing.py` — cover a non-empty submitted prompt using the
  configured result/steps, approval scenarios, one-time consumption/order for
  multiple scripts, and fault injection after a run has been admitted.
- `docs/haas-python-sdk-spec.md` — state that `script_run()` queues the next
  scenario(s), each consumed once in submission order; `fail_next()` applies to
  the next request and retry assertions require an already-admitted run.

Keep the diff below 400 non-generated lines. Split if implementation plus tests
and documentation exceed the slice budget.

## Implementation sequence

1. Verifier captures fresh status, protected TS diff hash, and focused/full SDK,
   Ruff, Pyright, and boundary baselines before edits.
2. Reproduce the named test failures and confirm their exception/result values.
   Do not assume all eight share one cause.
3. Implement only the script-assignment behavior proven by reproduction. Correct
   the fault-injection test setup so it submits/admit a run before injecting the
   next-request failure; do not weaken the test's retry assertion.
4. Add a FIFO multi-script regression and an unconsumed-script/default-fallback
   regression. Preserve idempotency, events, approval and tenant behavior.
5. Update the SDK testing utility specification and run the focused and full SDK
   gates against the fresh baseline. Stop and report any failure not explained
   by this slice rather than expanding into unrelated fixes.

## Acceptance criteria

- [x] Fresh baseline reproduces and records exact outcomes for
  `.venv/bin/pytest tests/sdk/test_sdk_testing.py -q` and
  `.venv/bin/pytest tests/sdk -q`; classify against test IDs, not aggregate count.
- [x] `fake.script_run(...)` followed by a run submission with any non-empty
  prompt uses that configured scenario and returns its configured result/cost
  and scripted steps or approval, as asserted in the named regression tests.
- [x] Multiple unconsumed scripted scenarios are selected once each in FIFO
  submission order; a submission after the queue is consumed uses the existing
  default fake-run behavior.
- [x] `fail_next()` test submits a run first, injects a 503 for the next request,
  and proves the existing retry behavior returns the admitted run; it does not
  mistake an unadmitted queued run for a completed run.
- [x] Existing SDK testing tests pass, and `.venv/bin/pytest tests/sdk -q`
  introduces no new failures relative to the captured baseline.
- [x] Ruff passes on changed Python paths; Pyright adds no diagnostics; boundary
  counts do not increase; `git diff --check` passes.
- [x] Only allowed paths change and the existing user-owned TypeScript diff is
  unchanged. No Atlas implementation or SDK network API is changed.
- [x] Any failure that requires changing a different SDK contract or server/API
  behavior is reported as a separate blocker, not repaired in this slice.

## Verification rungs

Before editing and after the slice, run:

```text
.venv/bin/pytest tests/sdk/test_sdk_testing.py -q
.venv/bin/pytest tests/sdk -q
.venv/bin/ruff check haas_sdk/testing.py tests/sdk/test_sdk_testing.py
.venv/bin/pyright haas_sdk
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas-explorer
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py
git diff --check
git status --short
```

The verifier supplied the protected TS baseline hash before implementation.
Per the explicit no-touch instruction, do not inspect or recompute it after.
Do not run full repository pytest for this SDK-only slice.

## Baseline and verification evidence

Fresh pre-edit baseline captured by the verifier:

```text
.venv/bin/pytest tests/sdk/test_sdk_testing.py -q:
  4 passed, 8 failed, 1 warning
.venv/bin/pytest tests/sdk -q:
  95 passed, 8 failed, 5 warnings
  failures/results:
    test_fake_completes_a_scripted_run: done, not sunny
    test_fake_approval_flow_with_handler: done, not refunded
    test_fake_digest_mismatch_is_conflict, test_fake_single_winner_decision,
      test_fake_wait_without_handler_raises_approval_required,
      test_fake_stale_review_refused_locally: expected waiting-for-approval exceptions absent
    test_fake_fail_next_injects_fault: queued, not completed
    test_fake_rejected_decision_cancels_run: expected RunFailed absent
.venv/bin/ruff check haas_sdk/testing.py tests/sdk/test_sdk_testing.py: passed
.venv/bin/pyright haas_sdk: 0 diagnostics
boundaries: atlas 0/0; atlas-explorer 134/1; overall 155/9
git diff --check: passed
protected TS diff hash provided for baseline: cb14fd38219926cb569c149fe00ec5f0ded0473520689ee80dbf6b20f68d4f81
```

Final verification, after the reviewer-requested assertion correction:

```text
.venv/bin/pytest tests/sdk/test_sdk_testing.py -q:
  13 passed, 1 warning (baseline: 4 passed, 8 failed, 1 warning)
.venv/bin/pytest tests/sdk -q:
  104 passed, 5 warnings (baseline: 95 passed, 8 failed, 5 warnings)
.venv/bin/ruff check haas_sdk/testing.py tests/sdk/test_sdk_testing.py: passed
.venv/bin/pyright haas_sdk: 0 diagnostics
boundaries: atlas 0/0; atlas-explorer 134/1; overall 155/9 (all unchanged)
git diff --check: passed
protected TypeScript Atlas diff SHA:
  cb14fd38219926cb569c149fe00ec5f0ded0473520689ee80dbf6b20f68d4f81
  unchanged; file not inspected
```

The verifier reported the protected TypeScript diff SHA unchanged. The file was
not inspected or recomputed. All eight failures recorded in the baseline for
`tests/sdk/test_sdk_testing.py` are resolved. The overall SDK suite and Atlas
extraction/stabilization effort remain in progress.

Review and verification decisions:

```text
Review round 1: APPROVE with one minor finding—the test did not prove
`fail_next()` was consumed. The test added `assert not fake.failures`.
Final verifier: GREEN.
Final reviewer: APPROVE, no findings.
```

Final reported slice change: 57 insertions and 14 deletions across the three
allowed implementation/test/spec files, plus the existing untracked plan; within
the 400-line slice budget. One-time Budget override: 7 specialist delegations
exceeded the default limit of 6 because one additional independent verification
was needed after the reviewer-requested test assertion. This preserved the
required review gate integrity.

## Risks and rollback

- FIFO scenario selection is observable to tests that preconfigure multiple
  runs. Pin the order explicitly in tests and documentation.
- A later failure may reveal that one of the eight old expectations is invalid
  or has a different cause. Preserve its failure evidence and stop rather than
  broadening the fix.
- Rollback only this slice's helper/test/spec changes. No persisted data or
  service state is affected.

## Decisions needed

1. Scenario assignment: **Resolved — A selected.** Consume pre-scripted
   scenarios FIFO on successive submissions. The existing documented API has
   no prompt argument. The supplied fresh baseline is recorded above.

## Log

- 2026-11-08 architect: inspected the recorded failing tests, `FakeHaas` source,
  run submission client, pytest fixture, and SDK testing spec. Source indicates
  a prompt-selection mismatch and a fault test that does not admit its run;
  neither cause has been freshly reproduced. Proposed bounded slice only.
- 2026-11-08 user: selected decision A; pre-scripted scenarios are consumed FIFO
  by successive submissions. Approved the slice to proceed in-progress.
- 2026-11-08 client-engineer: used the provided fresh baseline; implemented FIFO
  scenario consumption only after idempotency replay checks, plus default
  fallback. Added FIFO/replay/fallback coverage and moved fault injection after
  admission. Focused and complete SDK suites now pass. The stale-review test
  refreshes the review before asserting local refusal, matching its intended
  condition. Plan remains in-progress for verification; the protected TS hash
  was not re-read per the explicit no-touch instruction.
- 2026-11-08 reviewer, round 1/2: APPROVE with one minor actionable test
  assertion: after retry, explicitly assert that the injected failure was
  consumed. Client-engineer applied this narrow test-only correction
  (`assert not fake.failures`); no production changes. Follow-up verification
  and final review were completed as recorded above.
- 2026-11-08 integration: final verifier GREEN and final reviewer APPROVE with
  no findings. Slice done; evidence and resolved baseline failures are recorded
  above. Budget override: 7 specialist delegations (default 6), including one
  extra independent verification after the reviewer-requested assertion to
  preserve review-gate integrity.
