# 025 — Atlas exploration contract foundation

- Status: done
- Type: slice
- Owner agent: plugin-engineer
- Epic: [024](024-atlas-exploration-ux-epic.md)

## Goal and scope

Define IA and additive v2 types with synthetic computer/person journeys.
Allowed: Atlas client types/exports/tests, IA documentation, and these plans.
No runtime, backend, schema migration, dependency, UI, or v1 behavior changes.
Everything is Atlas-owned plugin-service code; no core changes or ADR needed.

## Acceptance criteria

- Existing v1 tests/exports remain intact.
- Fixtures express categories, adaptive facets, saved sibling paths, ambiguity,
  unavailable information, conflicts, and approved external-action descriptors.
- Two person occurrences share one entity; relationships retain the organization
  intermediary rather than inventing a direct person edge.
- Positive/negative compile checks pass. Runtime validation is explicitly deferred.
- Client build/typecheck/tests pass; web and boundaries add no baseline failures.

## Baseline and verification

Baseline: SDK/client build and client typecheck pass; 10 client tests pass.
Web typecheck has five pre-existing AtlasExplorer diagnostics (unused state and
initialQuery, missing output_delta.nodes, and two missing DimensionKey references).
Web bundle passes. Boundaries: 175 errors / 9 warnings.

Repeat `npm --prefix haas_sdk_ts run build`, client `build`, `typecheck`, `test`,
web `typecheck`, `bundle`, and the repository boundary scanner; `git diff --check`.
No Python paths change. Compare diagnostics, not only exit codes.

## Risks, decisions and rollback

Types alone cannot validate untrusted output: schema/reference checks are slice B.
Fixtures live under client tests rather than a separate contracts folder to avoid
changing compiler roots. No new dependencies. Rollback removes only additive files
and exports; there is no user data or runtime cutover.

## Evidence

Implementation verified: SDK/client builds and client typecheck pass; 13 tests
pass (10 legacy + 3 new). Web bundle emits the same asset hashes; its five
TypeScript diagnostics remain unchanged. Boundaries remain 175 errors / 9
warnings. `git diff --check` passes. v1 files and runtime code are unchanged.
Direct contract/export review found no blocking issue. Independent reviewer
verdict: `APPROVE` (13 client tests); acceptance is complete and this slice is
done. Evidence and approval were supplied by the parent/user, not rerun or
re-reviewed during this documentation-only update. No commit is implied.

This acceptance covers additive contracts and fixtures only: no authentication,
live research, transport, or persistence. Runtime validation and navigation are
recorded separately in [026](026-atlas-exploration-validation.md) and
[027](027-atlas-exploration-navigation.md).

Budget override: slightly over 400 lines including the epic, slice plan, and IA
documentation; keep all new behavior limited to additive types and test fixtures.
