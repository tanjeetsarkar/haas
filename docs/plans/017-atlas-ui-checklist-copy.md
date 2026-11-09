# 017 — Stage Atlas checklist component

- Status: done
- Type: slice
- Owner agent: plugin-engineer
- Epic: [012](012-atlas-ui-service-extraction.md)

## Goal

Copy the checklist card component and stylesheet byte-for-byte into the
inactive Atlas service UI tree. Preserve existing marketplace sources and keep
the partial service UI unactivated.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Atlas checklist UI | Plugin service | These files exist only for Atlas. |

## Scope

**Allowed paths:** the two destination files below, this plan, plan 012
evidence, and `docs/plans/README.md`.
**Out of scope:** marketplace edits/deletions, UI activation, other component
copies, dependency/config changes, runtime or backend behavior.

**Planned files:**
- `services/atlas-explorer/web/src/components/atlas/ChecklistCard.tsx` — exact copy
- `services/atlas-explorer/web/src/components/atlas/ChecklistCard.css` — exact copy

## Acceptance criteria

- [x] Both destinations are byte-identical to their source counterparts.
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
- Source parity: `cmp` each pair and inspect scoped status.

## Baseline

```text
2026-11-09 before edits
ChecklistCard.tsx 158 lines, SHA256 7024fa3d90f56c5af98faf7f51e601188d3f05f3d65af39626dfcb2489b6f5fc
ChecklistCard.css 162 lines, SHA256 559d5b4f8fd81d03be00aa617a3a4a549ab0b39af1481ef26dc341c52b5572af
ChecklistCard.tsx is already user-modified in the worktree; preserve and
copy its current content. Do not edit the source.
Marketplace TS baseline: 5 known diagnostics; Vite bundle passed.
Boundary baseline: Atlas 154/1; full repository 175/9.
```

## Risks and rollback

- Concurrent source changes → re-check hashes before copying and stop to
  rebaseline if any source differs.
- Rollback removes only the two added service files; never revert marketplace
  or unrelated worktree changes.

## Decisions needed

None. This slice copies existing UI without activation or deletion.

## Log

- 2026-11-09 lead: approved as the next sequential copy slice after plan 016
  approval; source hashes and current worktree state recorded above.
- 2026-11-09 verifier: source parity passed; service build, strict fixture
  compile, offline bundle, SDK/client tests and builds passed. Marketplace Vite
  bundle passed; full marketplace build retained the same five baseline
  diagnostics. Boundary counts remained Atlas 154/1 and full repository
  175/9. Review is pending.
- 2026-11-09 reviewer: APPROVE; no findings. Both destinations are byte-identical
  to the current marketplace sources, staging remains inactive, and the copy
  stays within budget.
