# 041 — Atlas SSE client and progress projection

- Status: done — parent-reported final reviewer 6952 APPROVE
- Type: slice
- Owner agent: plugin-engineer
- Epic: [038](038-atlas-guided-research-epic.md)
- ADR: none — public SDK consumption only

## Goal and boundary

Replace Atlas's 750ms run polling with the generic SDK stream and a bounded
execution-progress projection. Domain projection disappears without Atlas;
run lifecycle and stream transport remain existing generic public contracts.

## Scope

Allowed under `services/atlas-explorer/client-ts/`: `src/explorationClient.ts`,
new `src/explorationProgress.ts`, `src/index.ts`, adjacent tests, README.
Read service instructions before edits. ≤400 changed lines including tests.
No SDK parser copy, UI rewrite, backend, dependencies, phase activation or
external action enablement. Split additional progress fields into a follow-up
sub-slice before crossing budget; preserve old behavior until cutover verified.

## Contracts

- Existing research submission remains one explicit run. Subscribe with
  `streamEvents(runId, {signal,lastEventId,outputDeltas})` after the returned ID.
  Remove timed GET/pause loop; GET only on initial reattach, terminal or explicit
  recovery. SDK EOF reconciliation is permitted, not another polling loop.
- Track `{workspaceId,operationId,requestId,pathKey,threadId,runId,generation}`.
  Ignore stale generations; one controller per selected run. Supersede/unmount
  abort observation. User Cancel explicitly calls public cancelRun once and
  records intent; observation abort alone never cancels remote work.
- Dedupe durable numeric event IDs per run; persist cursor only after applying
  the event. ID-less output previews remain bounded volatile text, never graph
  data/cursors. A gap/replay failure is visible; do not silently skip to latest.
- `input_required` is an approval pause, not success. Disconnect/exhaustion
  exposes reconnect with the same run/cursor, never createRun. Remote terminal
  races require authoritative run GET and existing strict result parser.
- Only validated complete successful output is projected. Existing fallback
  is limited to complete successful tool outputs with provenance. Failed child
  output preserves parent/siblings. Malformed output uses 039 diagnostics.
- Progress exposes available public events/steps/usage/history with IDs,
  timestamp, phase/status, tool name and bounded redacted preview. Missing
  public fields are unavailable, never inferred chain-of-thought.
- 042 persistence is optional at this slice boundary: expose callbacks/receipts
  for later integration; do not claim in-memory state is durable.

## Acceptance / verification (rungs 3/5)

2026-11-10 integration evidence: parent reports final reviewer 6952 APPROVE,
**51 focused / 194 eligible tests passed**. Marked done on that evidence;
architect did not rerun checks. No browser/runtime activation is claimed.

- [ ] Tests: one stream, no interval GET; replay dedupe; approval pause;
  supersede/abort; stale terminal event; failure preserving branches; terminal
  fetch/validation exactly once; disconnect/reconnect never creates a run.
- [ ] Existing exported client methods remain compatible; independent APPROVE.

Before/after:
```sh
cd services/atlas-explorer/client-ts
npm test -- tests/explorationClient.test.ts
npm test
npm run typecheck
npm run build
cd ../../..
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py
git diff --check
```

Baseline: not executed in this planning pass; record actual counts before edits.
Historical boundary 175 errors / 9 warnings; require no increase.

## Risks / rollback / decisions

Slow readers and large previews: bound volatile buffers and expose truncation.
Rollback to previous client only before SSE activation; do not add a hidden
polling fallback to the new path. No migration. No user decision needed.
Next: plugin engineer starts only after 039/040 build/review evidence.
