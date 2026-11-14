# 043 — Atlas autosave, history, scoped dives and execution drawer

- Status: in-progress — implementation/review DONE; draft/history browser verified, live research journey acceptance PARTIAL / NOT VERIFIED
- Type: epic — each row below is a separate ≤400-line slice
- Owner agent: plugin-engineer
- Epic: [038](038-atlas-guided-research-epic.md)
- ADR: none — Atlas UI/client only

## Goal / boundary

Turn Atlas into a durable workspace without unsolicited research spend.
History, branch navigation and display are extension-owned; consume only public
SDK and Atlas storage APIs. No core UI slots, tables or imports.

## Scope and ordered slices

Allowed paths under `services/atlas-explorer/` only:

| Slice | Planned files / independently verifiable outcome | Dependency |
|---|---|---|
| 043a | new `web/src/workspaceSaveQueue.ts`, adjacent tests, `AtlasWorkspace.tsx` integration: serial durable autosave and visible receipts | 042 verified |
| 043b | new workspace history component/tests, `AtlasWorkspace.tsx`: catalog, Start afresh/Continue and hard-reload recovery | 043a |
| 043c | client navigation/research helpers/tests, workspace/column row handlers/tests: scoped click dives and cached revisit | 041, 043b |
| 043d | new run drawer component/tests/styles, workspace integration: public-field progress, history and usage | 041, 043b |

Components live under `web/src/components/`; follow adjacent style/test files.
Each row includes its own tests/README updates. Split before exceeding budget.
No new dependency, storage schema migration, legacy reader removal, phases,
external execution, hidden polling or broad component rewrite.

## Exact UI/storage contracts

- One ordered queue per workspace. Assign stable mutation ID and expected
  revision at send time, retain identical body/ID for retry of an unacknowledged
  request. Coalesce only unsent draft checkpoints; never submitted queries,
  operation transitions or branch commits. Never reorder acknowledged revisions.
- Saving/Saved/Save failed/Conflict reflect server receipts, not local React
  state. Edits arriving during a save remain dirty after that save's receipt.
  409 stops dispatch, fetches authorized latest revision and offers explicit
  reload/keep-local-copy; no blind overwrite or silent merge of graph/action
  revisions. Local recovery copies must not dispatch runs/actions.
- Save root/child query and operation_prepared before createRun. If storage
  fails, do not dispatch. Unknown submission remains visible and blocked;
  reconcile using recorded authoritative run/thread/idempotency information
  only. Switching workspace aborts observation, not remote execution.
- Start afresh creates a new workspace without deletion. Continue/hard reload
  restores drafts, selections, branch graph revisions and run IDs, attaches to
  the recorded nonterminal run, and creates zero research/action requests.
- Hide manual Save new artifact snapshot only after 042 durable path is
  verified and enabled. Keep artifact import/export and old snapshot readers;
  unavailable storage shows an explicit failure, not fake successful autosave.
- Meaningful draft checkpoint on blur/idle, not every keystroke. Explicit
  navigation offers flush/wait or a clear unsaved warning; do not promise
  browser-unload network writes are durable. No new localStorage token cache.

## Click/dive contract

Root Ask returns one useful immediate level, not recursive descendants. A new
entity/category/facet selection creates exactly one bounded dive for that
subject using selected ancestry, committed public thread context and the exact
user query. No siblings/descendants researched in background.

Revisiting a researched branch restores cached details and history with **zero
new runs**; Research again explicitly prepares a new operation/revision.
Sources show evidence without research. Candidates resolve identity before
claims; unavailable rows explain the reason. Dedupe double clicks against the
same in-flight operation; preserve sibling/parent results on child failure.
Action buttons stop row-click propagation and never trigger a dive.

## Execution drawer contract / parity matrix

| UI display | Authoritative public source |
|---|---|
| submitted query, exact application-generated prompt/selected context | saved Atlas operation; not private system prompt |
| run/thread/operation IDs, status/timestamps/model | public run response and saved binding |
| steps and tool invocation/status/argument/result | SDK run steps + authorized thread history, correlated public events |
| tokens/cost | public run usage; unknown is unavailable, not zero |
| active skills | explicit public activation event/state if present; otherwise unavailable |
| elapsed time | timestamps, labeled live estimate until terminal |
| reconnect/approval/error | 041 lifecycle + public status/approval data |

Implementer verifies actual SDK method/field names before binding each row;
missing public values are unavailable, not a reason to import core internals.
Fetch full authorized tool result on demand through public history; event
previews are labeled truncated. Cap preview rendering at 16KiB per item and
200 visible timeline entries with incremental public-page loading. Escape
untrusted text; no raw HTML, secret values or hidden reasoning. Deltas are
provisional until strict validated final output.

## Acceptance / verification

**Final 2026-11-10 parent sync:** 043d final reviewer **48ba90 APPROVE** after
binding/cap fixes; **229 UI tests**, types/build passed. Implementation and
review are done; this supersedes earlier drawer-review-pending entries below.
Independent final checks reported SDK **20**, Atlas **206**, service **56 passed /
1 DB test skipped**, plugin **24**, doctor **0 errors / 0 warnings**; disposable
DB **2 passed previously**, local migration applied.

Real default-browser verification: history loads, manual Save count **0**,
draft server revision **3** confirmed through GET and restored after hard
reload. Start afresh clears / Continue restores with **0 POST /v1/runs**.
Draft workspace CSS has no overflow at **1440/768/390px**.

**Live journey acceptance remains partial / NOT VERIFIED:** no paid root/child
runs, research journeys or real-run network/SSE checks were exercised. Draft
viewport and recovery results must not be presented as live research or drawer
event acceptance. Whole 038 remains open; 044c AI drafting is blocked and
external execution/phases remain disabled. Architect did not rerun checks.

Latest 2026-11-10 parent report supersedes the earlier checkpoint below:
cutover reviewer **f1534 APPROVE**, **228 UI tests passed before the current
drawer change**; manual Save is hidden on the durable path. Fetch-receiver fix
verified against the real catalog (**200**); real browser draft persisted at
server revision **3**. 042 local migration is now applied. Browser/drawer/history
agent **139e5** is still finishing, then review is required; 228 is not a test
result for that unfinished drawer change. The epic remains in-progress.
Architect did not rerun checks. No full live research acceptance or paid runs
are claimed; external execution and phases remain disabled.

Earlier 2026-11-10 checkpoint evidence: parent reports reviewer c838 APPROVE,
**219 UI tests passed**, types/build passed. This approves the checkpoint,
not the entire epic: **full history paging, browser verification and manual-save
acceptance remain pending**. Local storage application/integration is also
pending under 042. Architect did not rerun checks; no activation is claimed.

- [ ] Tests: overlapping edits/receipt race, duplicate retry, 409, quota/storage
  failure, flush, hard reload, unknown submission and no resubmit.
- [ ] Tests: one root, one selected-child run, no source/revisit/action-button
  runs, sibling preservation, duplicate click suppression, Research again.
- [ ] Drawer tests map every row above to public fixtures or unavailable;
  injected HTML is inert, redaction/truncation remain visible.
- [ ] Browser 1440/768/390px + keyboard: hospital→specialist and PC→build
  journeys; count requests, test hard reload/reconnect and confirm one SSE,
  zero interval GET loop, zero run creation on restore.
- [ ] Independent APPROVE per row, no new failures/boundary errors.

Rungs 3/5 and browser integration, baseline/final:
```sh
cd services/atlas-explorer/client-ts
npm test && npm run typecheck && npm run build
cd ../web
npm test && npm run typecheck && npm run build
cd ../../..
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py
git diff --check
```

Baseline not run by architect; historical 175 errors / 9 warnings only. Engineer
records fresh totals and browser request counts; screenshots alone are not proof.

## Risks / rollback / decisions

Keep old artifact readers and disable new UI activation to roll back; retain
durable workspaces and never replay pending effects. Main risk is lost saves or
duplicate spend: receipt-driven queues and explicit unknown state are mandatory.
Cached revisit/details and separate approver are already approved: no new user
decision. Unavailable public fields are engineering limitations to report.
