# 027 — Atlas exploration navigation state

- Status: done
- Type: slice (C)
- Owner agent: plugin-engineer
- Epic: [024](024-atlas-exploration-ux-epic.md)
- Depends on: [025](025-atlas-exploration-contract-foundation.md)
- Validation companion: [026](026-atlas-exploration-validation.md)

## Goal and boundary

Provide immutable, local graph/path selection for the fixture-only preview,
preserving selected ancestry, repeated-entity identity and saved sibling branches.
Without Atlas this domain state would not exist: plugin-service ownership only.
No core changes, generic extension point or ADR.

## Scope and files

- `services/atlas-explorer/client-ts/src/explorationNavigation.ts` —
  selection, saved-path activation, back, breadcrumbs, navigation reducer and
  pre-application update guard.
- Client exports, README and focused tests under
  `services/atlas-explorer/client-ts/`.

Excluded: UI implementation, requests/runs/actions, authentication, live research,
stream application, persistence, backend/core changes and migrations.
An in-memory saved branch is not a tenant-persisted session.

## Acceptance criteria — met

- `selectExplorationTarget` requires a selected active-path parent (or null at
  root); an exact saved prefix/target restores the longest saved suffix, with
  stored-order tie breaking. Otherwise it creates a path and unique occurrence.
- `activateExplorationPath` restores the exact branch;
  `getExplorationBreadcrumbs` returns its selected chain;
  `previousExplorationPath` selects the prefix while retaining the full branch.
  `reduceExplorationNavigation` handles select/activate/back.
- Navigation is immutable, changes revision only when state changes, preserves
  siblings and shared ancestor prefixes, and does not merge entities or invent
  relationships. Repeated targets retain entity identity with distinct occurrences.
- `acceptsExplorationUpdate` checks the newest issued request per path/parent,
  loading/partial state, exact exploration/request/run/thread/history/revision
  correlation and a strictly newer safe sequence; stale, superseded and terminal
  updates fail. This guard neither applies nor persists an update.
- Existing v1 behavior remains intact; build/typecheck/tests pass with no new
  web or boundary failures.

## Baseline, verification and review

Before B/C: client build/typecheck pass and 13 client tests pass; isolated service
tests: 3 pass. Global boundaries: 175 errors / 9 warnings; Atlas: 154 / 1.
The five pre-existing web TypeScript diagnostics remain outside this slice.

Commands confirmed from the client README, from repository root:

```sh
npm --prefix services/atlas-explorer/client-ts run build
npm --prefix services/atlas-explorer/client-ts run typecheck
npm --prefix services/atlas-explorer/client-ts test
```

Supplied combined B/C evidence: build/typecheck pass; **113 client tests pass**
(13 preserved + 100 new across B/C). Isolated Python tests: **19 pass**;
Ruff/Pyright pass; doctor: 0 errors / 0 warnings. Global boundary counts remain
175 / 9 and Atlas 154 / 1; the five web diagnostics are unchanged.
The Python commands and cross-language evidence scope are recorded in
[026](026-atlas-exploration-validation.md). Engineer-reported implementation
`git diff --check` passes. These checks were not rerun in this docs-only update.

Independent reviewer: **APPROVE**, read-only review, no bugs reported.
The client README's pending-review wording predates this supplied approval.
C remains within the ~400-line soft budget; B's override does not apply here.

## Risks, rollback and decisions

Correlation guards do not prove authenticated event origin, implement a live
stream, or authorize actions. Local branch retention does not prove save/resume
across reloads or tenant isolation. Those remain later gated milestones.
Rollback removes additive navigation APIs/tests; v1 and user data are unchanged.
Decisions needed: none. Next consumer is
[028 / D](028-atlas-miller-column-preview.md), fixture-only; no live adapter,
research or persistence is authorized by this completion.
