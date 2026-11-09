# 015 — Stage Atlas comparison and specialist components

- Status: done
- Type: slice
- Owner agent: plugin-engineer
- Epic: [012](012-atlas-ui-service-extraction.md)

## Goal

Copy `ComparisonRow` and `SpecialistProfile` with the comparison stylesheet
byte-for-byte into the inactive Atlas service UI tree. Preserve the marketplace
source and do not activate the partial service UI.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Atlas comparison and specialist UI | Plugin service | These components exist only for Atlas. |

## Scope

**Allowed paths:** the three destination files below, this plan, plan 012
evidence, and `docs/plans/README.md`.
**Out of scope:** marketplace edits/deletions, UI activation, other component
copies, dependency/config changes, runtime or backend behavior.

**Planned files:**
- `services/atlas-explorer/web/src/components/atlas/ComparisonRow.tsx` — exact copy
- `services/atlas-explorer/web/src/components/atlas/ComparisonRow.css` — exact copy
- `services/atlas-explorer/web/src/components/atlas/SpecialistProfile.tsx` — exact copy

## Acceptance criteria

- [x] All three destinations are byte-identical to their source counterparts.
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
ComparisonRow.tsx 121 lines, SHA256 19a25bdf90bfcf874293a7415fcd595becc24f87a3187379b09c0c1a6b18e81f
ComparisonRow.css 87 lines, SHA256 5527e82da2dbfaf9e5ac0772c834cd6ab3a4e60b65b8a5b0feff3350e7265a7f
SpecialistProfile.tsx 130 lines, SHA256 cc19c195142baeeda8904d423103e405e30f0ce8744338369f79539b289a7983
Marketplace TS baseline: 5 known diagnostics; Vite bundle passed.
Boundary baseline: Atlas 154/1; full repository 175/9.
The two TSX sources are already user-modified in the worktree; preserve and
copy their current contents. Do not edit the sources.
```

## Risks and rollback

- Concurrent source changes → re-check hashes before copying and stop to
  rebaseline if any source differs.
- Rollback removes only the three added service files; never revert marketplace
  or unrelated worktree changes.

## Decisions needed

None. The user directed completion of the remaining extraction; this slice
copies existing UI without activation or deletion.

## Log

- 2026-11-09 lead: approved as the next sequential copy slice after 012b
  review; source hashes and current worktree state recorded above.
- 2026-11-09 verifier: byte parity, service build/fixture/bundle, marketplace
  Vite bundle and known five TypeScript diagnostics verified; boundaries stayed
  154/1 and 175/9.
- 2026-11-09 reviewer: APPROVE; no findings. The 341-line destination copy is
  within budget, source files retain their pre-existing edits, and staging is
  inactive.
