# 014 — Stage the Atlas EntityCard component

- Status: done
- Type: slice
- Owner agent: plugin-engineer
- Epic: [012](012-atlas-ui-service-extraction.md)

## Goal

Copy the current `EntityCard` component and stylesheet byte-for-byte into the
inactive Atlas service UI tree. Keep the existing marketplace source and
running entry unchanged while the staged UI is assembled in later slices.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Atlas `EntityCard` UI and styles | Plugin service | They would not exist without Atlas. |

## Scope

**Allowed paths:** the two destination files below, this plan, plan 012 evidence,
and `docs/plans/README.md`.
**Out of scope:** marketplace edits/deletions, UI activation, other component
copies, dependency/config changes, runtime or backend behavior.

**Planned files:**
- `services/atlas-explorer/web/src/components/atlas/EntityCard.tsx` — exact copy
- `services/atlas-explorer/web/src/components/atlas/EntityCard.css` — exact copy

## Acceptance criteria

- [x] Both destination files are byte-identical to their source counterparts.
- [x] Total handwritten additions remain ≤400 lines; no other runtime paths change.
- [x] Service staging build, strict public-contract fixture compile and offline
  bundle pass; no component is activated by the staging HTML.
- [x] Marketplace Vite bundle still passes; its full TypeScript build retains
  only the five recorded baseline diagnostics.
- [x] Atlas boundary report stays at 154 errors / 1 warning; full report at
  175 errors / 9 warnings, with no new findings.
- [x] Independent reviewer approves; unrelated worktree changes are preserved.

## Verification rungs

- Rung 5: run both `check_boundaries.py --plugin atlas-explorer` and the full
  boundary check; compare with the fresh baseline below.
- Rung 8: `npm --prefix haas_sdk_ts test -- --run` and SDK build.
- Rung 9: service package build, strict fixture compile, service Vite bundle,
  marketplace Vite bundle, and marketplace full build diagnostics.
- Source parity: `cmp` each source/destination pair and inspect scoped status.

## Baseline

```text
2026-11-09 before edits
Source: EntityCard.tsx 134 lines, SHA256 6df08f969009319f7f00c6b318dc6d8bad458119f8417c8d4fc51365902f434f
        EntityCard.css 197 lines, SHA256 47d805ee3ee2b07932816c0defbc2466fbd99865d693b1e9e2f4e6902735f00f
Marketplace TS baseline: 5 known diagnostics; Vite bundle passed.
Boundary baseline: Atlas 154/1; full repository 175/9.
EntityCard.tsx is already user-modified in the worktree; preserve and copy
its current content. Do not edit the source.
```

## Risks and rollback

- Source may change concurrently → compare current hashes before copying; stop
  and rebaseline if either differs.
- Rollback removes only the two newly added service destination files; never
  reset, stash, or revert marketplace or unrelated work.

## Decisions needed

None. The user directed completion of the remaining extraction; this slice
copies existing UI without activation or deletion.

## Log

- 2026-11-09 lead: approved as the next bounded copy slice after 012a review;
  source hashes and current worktree state recorded above.
- 2026-11-09 verifier: source parity, service build/fixture/bundle, marketplace
  Vite bundle, SDK tests/builds, known marketplace diagnostics and unchanged
  boundary reports confirmed.
- 2026-11-09 reviewer: APPROVE; no findings. The 331-line copy is within budget,
  exact source parity holds, and no activation or core source edits occurred.
