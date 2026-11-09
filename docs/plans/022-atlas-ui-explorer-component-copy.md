# 022 — Stage Atlas explorer component

- Status: done
- Type: slice
- Owner agent: plugin-engineer
- Epic: [012](012-atlas-ui-service-extraction.md)

## Goal

Copy AtlasExplorer byte-for-byte into the inactive Atlas service UI tree.
Preserve the existing marketplace source and leave staging unactivated.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| AtlasExplorer UI | Plugin service | This component exists only for Atlas. |

## Scope

**Allowed paths:** the destination file below, this plan, plan 012 evidence,
and `docs/plans/README.md`.
**Out of scope:** marketplace edits/deletions, UI activation, other component
copies, dependency/config changes, runtime or backend behavior.

**Planned files:**
- `services/atlas-explorer/web/src/components/atlas/AtlasExplorer.tsx` — exact copy

## Acceptance criteria

- [x] Destination is byte-identical to its source counterpart.
- [x] At most 400 handwritten additions; no other runtime paths change.
- [x] Service staging build, strict fixture compile and offline bundle pass;
  no staged component is activated.
- [x] Marketplace Vite bundle passes and its full build retains only the five
  recorded baseline TypeScript diagnostics.
- [x] Boundary baseline remains Atlas 154/1 and full repository 175/9, with no
  new findings.
- [x] Independent reviewer approves; unrelated worktree changes are preserved.

## Verification rungs

- Rung 5: run plugin and full boundary checks; compare with the baseline below.
- Rung 8: TypeScript SDK and Atlas client builds/tests remain green.
- Rung 9: service build, strict fixture compile and Vite bundle; marketplace
  Vite bundle and full-build diagnostic comparison.
- Source parity: `cmp` source and destination and inspect scoped status.

## Baseline

```text
2026-11-09 before edits
AtlasExplorer.tsx 350 lines, SHA256 4940671193a1f1adfadaed4ac7172afd6df43993e5e9040a951449907715f425
AtlasExplorer.tsx is already user-modified in the worktree; preserve and copy
its current content. Do not edit the source.
Marketplace TS baseline: 5 known diagnostics; Vite bundle passed.
Boundary baseline: Atlas 154/1; full repository 175/9.
```

## Risks and rollback

- Concurrent source changes → re-check the source hash before copying and stop
  to rebaseline if it differs.
- Rollback removes only the added service file; never revert marketplace or
  unrelated worktree changes.

## Decisions needed

None. This slice copies existing UI without activation or deletion.

## Log

- 2026-11-09 lead: started after plan 021 reviewer approval; source hash and
  current worktree state recorded above.
- 2026-11-09 verifier: source parity passed; service build, strict fixture
  compile, offline bundle, SDK/client tests and builds passed. Marketplace Vite
  bundle passed; full marketplace build retained the same five baseline
  diagnostics. Boundary counts remained Atlas 154/1 and full repository
  175/9. Review is pending.
- 2026-11-09 reviewer: APPROVE; no findings. Destination matches its current
  marketplace source, staging remains inactive, and scope and budget are
  respected.
