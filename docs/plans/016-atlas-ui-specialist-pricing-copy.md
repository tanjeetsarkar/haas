# 016 — Stage Atlas specialist and pricing components

- Status: done
- Type: slice
- Owner agent: plugin-engineer
- Epic: [012](012-atlas-ui-service-extraction.md)

## Goal

Copy the remaining specialist stylesheet and the pricing card component/styles
byte-for-byte into the inactive Atlas service UI tree. Preserve existing
marketplace sources and do not activate the partial service UI.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Atlas specialist and pricing UI | Plugin service | These files exist only for Atlas. |

## Scope

**Allowed paths:** the three destination files below, this plan, plan 012
evidence, and `docs/plans/README.md`.
**Out of scope:** marketplace edits/deletions, UI activation, other component
copies, dependency/config changes, runtime or backend behavior.

**Planned files:**
- `services/atlas-explorer/web/src/components/atlas/SpecialistProfile.css` — exact copy
- `services/atlas-explorer/web/src/components/atlas/PricingTierCard.tsx` — exact copy
- `services/atlas-explorer/web/src/components/atlas/PricingTierCard.css` — exact copy

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
SpecialistProfile.css 89 lines, SHA256 3478d71e8ab11b23c5600b3f8fcb03a04f324ca1eb466be68c87e117b036bb99
PricingTierCard.tsx 134 lines, SHA256 11ee101c5e1b10b770114d89ccea0cbf23086a0f1f2879902e08585f8d239df5
PricingTierCard.css 107 lines, SHA256 c3cfa06c6b892282e4af89b4d9621873fe0ba9d4c0965f0135a55e794872297d
Marketplace TS baseline: 5 known diagnostics; Vite bundle passed.
Boundary baseline: Atlas 154/1; full repository 175/9.
PricingTierCard.tsx is already user-modified in the worktree; preserve and
copy its current content. Do not edit the source.
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

- 2026-11-09 lead: approved as the next sequential copy slice after 012c
  review; source hashes and current worktree state recorded above.
- 2026-11-09 verifier: source parity passed; service build, strict fixture
  compile, offline bundle, SDK/client tests and builds passed. Marketplace Vite
  bundle passed; full marketplace build retained the same five baseline
  diagnostics. Boundary counts remained Atlas 154/1 and full repository
  175/9. Review is pending.
- 2026-11-09 reviewer: APPROVE; no findings. All three destinations match
  current sources, including the pre-existing modified pricing TSX source;
  staging remains inactive and the copy is within budget.
