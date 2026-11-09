# 023 — Activate the Atlas service UI

- Status: in-progress
- Type: slice
- Owner agent: plugin-engineer
- Epic: [012](012-atlas-ui-service-extraction.md)

## Goal

Activate the complete Atlas UI in the standalone service web package after
the component-copy slices. Keep the existing core marketplace runnable and
unchanged; this slice does not publish or deploy the service UI.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Atlas app entry and service web configuration | Plugin service | The UI exists only for Atlas. |

## Scope

**Allowed paths:** `services/atlas-explorer/web/src/App.tsx`,
`src/main.tsx`, `index.html`, `tsconfig.json`, `README.md`, and
`public/vite.svg`; this plan, plan 012 evidence, and `docs/plans/README.md`.
**Out of scope:** marketplace changes/deletions, service deployment or
production routing, authentication, publishing, backend changes, dependency
changes, and changes to staged Atlas components.

**Planned files:**
- `services/atlas-explorer/web/src/App.tsx` — exact copy of current marketplace app
- `services/atlas-explorer/web/src/main.tsx` — exact copy of marketplace entry
- `services/atlas-explorer/web/index.html` — load the app entry, title and icon
- `services/atlas-explorer/web/tsconfig.json` — typecheck the complete `src` tree
- `services/atlas-explorer/web/README.md` — replace staging-only status with the
  local app usage and existing deployment/auth constraints
- `services/atlas-explorer/web/public/vite.svg` — exact copy of the referenced asset

## Acceptance criteria

- [x] App and entry files are byte-identical to the captured marketplace
  sources; reviewer confirmed parity against the current modified App.tsx.
- [x] Service `npm run bundle` and strict compile-fixture check pass with `src`
  included in the TypeScript project. `npm run build`/`typecheck` report only
  the same five existing AtlasExplorer diagnostics as the marketplace baseline;
  no compiler settings are weakened.
- [x] Service output contains app JS/CSS and its HTML references the local entry
  and icon; no credentials are embedded in generated assets.
- [x] Marketplace Vite bundle passes and its full build retains only the five
  recorded baseline TypeScript diagnostics; marketplace sources remain unchanged.
- [x] TypeScript SDK and Atlas client tests/build/typecheck remain green.
- [x] Boundary baseline remains Atlas 154/1 and full repository 175/9, with no
  new findings.
- [x] Independent reviewer approves; unrelated worktree changes are preserved.

## Verification rungs

- Rung 5: run plugin and full boundary checks; compare with the baseline below.
- Rung 8: TypeScript SDK and Atlas client builds/tests remain green.
- Rung 9: service app typecheck/build/bundle, strict compile fixture, marketplace
  Vite bundle and full-build diagnostic comparison.
- TypeScript checks are expected to return non-zero for the five pre-existing
  marketplace AtlasExplorer diagnostics listed in the baseline comparison;
  the activation must not add diagnostics.
- Inspect built HTML/assets for local app entry, icon path and absence of
  credentials. No browser/deployment/auth claims in this slice.

## Baseline

```text
2026-11-09 before edits
Marketplace src/App.tsx 53 lines, SHA256 8e85665318a7a41603ab33beb04eaf0ad3bbf4abad64f71f9f1852334fb1c6fc (pre-existing modified source; copy as-is)
Marketplace src/main.tsx 8 lines, SHA256 71f965a0100e0523a6873bc985b6ddca0264b1c6cadb3e348d642100564c299a
Service index.html 12 lines, SHA256 74040bde8d7ab9f95e589b148f85bfd626c4cbe74d256510e6b5ef98c7930604
Service tsconfig.json 21 lines, SHA256 ea915f1f02207303c4d3c88709563edb289a44f8d7a4049f252c42bfd463ad24
Service README.md 34 lines, SHA256 144743c54752911df351080998e9e4c6b8a70451b1855feb3d691836653883e8
Marketplace index.html 12 lines, SHA256 6d7ccdb491db3a0cb88f9cdfdc44ee1ba3f1fc7d6ca59c8a44cf489ec2c98c58
Marketplace tsconfig.json 24 lines, SHA256 1d78b68bf262218aa6a6493037f51283e45d1bf8b9a22ceec70269c4e706c522
Marketplace public/vite.svg SHA256 30d667a8f140f70dad798c239bf057e48ae1574b36ffe6379bfb4fb4d7d5e508
Marketplace TS baseline: 5 known diagnostics; Vite bundle passed.
Boundary baseline: Atlas 154/1; full repository 175/9.
```

## Risks and rollback

- Full-source typechecking may surface existing component diagnostics; compare
  to the current marketplace baseline and do not weaken compiler settings.
- The app uses the existing SDK defaults and Vite local `/v1` proxy; this does
  not establish production routing or authentication.
- Rollback removes only the two new app entry files and restores the staged
  index/config/README/asset state; marketplace and unrelated changes remain.

## Decisions needed

None. The user-approved 012k scope explicitly activates the UI in the service
package while deferring auth, production routing, publication and core cleanup.

## Log

- 2026-11-09 lead: started after reviewer approval of 012j; baseline and exact
  source hashes recorded above.
- 2026-11-09 verifier: App/main/favicon source parity passed; service bundle
  and strict fixture compile passed, with JS/CSS assets and local icon emitted
  and no credential-shaped literals found. Service build/typecheck and
  marketplace build each report the same five baseline AtlasExplorer
  diagnostics; marketplace Vite bundle passes. SDK (10 tests) and Atlas client
  (10 tests), builds and typecheck pass. Boundary counts remain Atlas 154/1 and
  repository 175/9. Independent review pending.
- 2026-11-09 lead: independently confirmed the service build/typecheck produce
  only the five listed baseline diagnostics; bundle emits local JS/CSS and
  `/vite.svg`, with no credential-shaped literal values in non-map assets.
  App/main/favicon parity and `git diff --check` pass. Review pending.
- 2026-11-09 reviewer: APPROVE; no blocking findings. Confirmed App/main
  parity, favicon and bundle references, README scope, and boundary counts.
  Reviewer noted the parity acceptance checkbox needed closure; updated above.
