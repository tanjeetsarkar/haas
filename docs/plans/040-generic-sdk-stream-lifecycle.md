# 040 — Generic SDK stream lifecycle

- Status: done — parent-reported final reviewer c83a APPROVE; 20 SDK tests passed
- Type: slice
- Owner agent: client-engineer
- Epic: [038](038-atlas-guided-research-epic.md)
- ADR: [0023](../adr/0023-sdk-stream-lifecycle.md)

## Goal and boundary

Plugins can observe authenticated runs through an abortable, bounded,
reconnect-safe public SDK stream. Atlas and incident-response plugins can use
this; no plugin names, events, UI types or business behavior enter SDK code.

## Scope

Allowed: `haas_sdk_ts/src/client.ts`, `src/types.ts`, adjacent generic stream
tests under `haas_sdk_ts/tests/`, SDK README only. Architect owns plans/ADR/index.
Keep implementation plus tests ≤400 changed lines; split before exceeding.
No server, plugin, dependency, generated contract or runtime configuration edits.
Do not touch legacy `src/lib/atlasClient.ts`.

## Exact contract

1. Add optional `StreamOptions.signal?: AbortSignal`; retain existing signature,
   defaults, authentication headers and `outputDeltas`. Aborting observation
   rejects with an AbortError (preserve caller reason if it is an Error);
   never calls cancelRun, resumeRun, createRun or approvals.
2. Link the caller signal to the fetch/reader lifetime and reconnect delay.
   Already-aborted signals make zero requests. Abort while reading, fetching,
   reconciling EOF or sleeping releases resources, causes no new reconnect,
   and is not translated into HaaSConnectionError. Remove listeners/timers in
   finally. Normal iterator return/break cancels the reader/body and releases its
   lock; callers must abort before awaiting return on an outstanding next/read.
3. Keep one stream request active. Use one retry-accounting path: at most
   `streamMaxReconnects` additional attempts for a stream invocation. EOF,
   transport failure and parse failure each consume at most one attempt.
   Do not reset the budget merely on opening HTTP 200; no double increment
   when exhaustion throws. Auth/API failures propagate without retry.
4. Only valid durable numeric IDs advance Last-Event-ID. ID-less previews never
   do. Do not advance to an undelivered queued event before yielding earlier
   events. Preserve the existing public event shape and parser. Replayed
   durable duplicates do not regress the cursor; Atlas performs its own
   projection dedupe. Do not silently drop malformed durable output as success.
5. On clean EOF, reconcile status with a bounded authenticated GET using the
   same signal (local private helper/request option permitted). `input_required`
   is nonterminal, alongside queued/running/cancelling: reconnect within budget,
   or report disconnect, never imply completed. Use the actual RunStatus union
   for terminal states. No periodic GET loop or hidden polling fallback.
6. Cleanup also covers parser/JSON errors, terminal EOF and early consumer exit.
   No credentials in URL/logs/errors added by this change. Optional deltas remain
   provisional and never become durable IDs.

## Acceptance and verification

- [ ] Tests: pre-abort; abort fetch/read/reconciliation/backoff; break cleanup;
  malformed JSON; terminal EOF; input_required EOF; exact retry counts at 0/1/N;
  durable cursor across reconnect; delta without ID; auth failure no retry.
- [ ] Existing SDK contract tests pass unchanged except additive assertions.
- [ ] SDK build passes; boundary error count does not increase; independent APPROVE.

Baseline and final, from repository root:

```sh
cd haas_sdk_ts
npm test -- tests/sdk.contract.test.ts
npm test
npm run build
cd ..
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py
git diff --check
```

Rungs 3/5/8: generic SDK tests/build/boundary scan. Planning did not run commands;
175 errors / 9 warnings is historical only. Engineer records actual baseline
and exact new test filename/counts before editing.

## Risks, rollback and decisions

AbortSignal does not make an async generator's pending next interruptible by
return alone: document abort-first behavior, test it, do not promise otherwise.
Rollback implementation only; no migration or runtime enablement required.
No new user decision; breaking existing SDK behavior beyond the specified
additive lifecycle contract requires a stop and compatibility review.

## Log

- 2026-11-10 final integration update: parent reports final reviewer c83a
  APPROVE and 20 SDK tests passed, superseding the earlier pending re-review.
  Build passed as previously reported. Marked done; architect did not rerun
  checks. This is SDK completion, not runtime activation.

- 2026-11-10 architect: executable 040 contract available; no SDK files edited.
- Parent subsequently reports SDK pre-edit baseline: focused durable SSE
  **1 passed** and build passed. Architect did not run these commands; SDK owner
  records exact selection and full baseline in its handoff.
- 2026-11-10 parent integration report: review APPROVE received; subsequent
  minor BIGINT fix has **20 SDK tests passed** and build passed. Targeted
  re-review of that fix is pending; status remains in-progress until final
  verdict. Architect did not rerun checks. No runtime activation recorded.
