# 012 — Move Atlas UI into its service

- Status: in-progress — 012a–k done; 012l blocked by plan 013; whole core web retirement requires separate plan 013
- Type: epic; first full slice: 012a
- Owners: haas-lead coordinates; plugin-engineer owns service additions;
  client-engineer owns core cleanup; verifier/reviewer independently gate each slice
- Parent: [001](001-atlas-extraction-sdk-epic.md)
- Decisions: [ADR-18](../adr/0018-domain-client-package-ownership.md), accepted;
  [010](010-atlas-typescript-client-extraction.md), [011](011-marketplace-atlas-client-imports.md)

## Goal and authority

Relocate the existing UI, styles, entry and compile contract into
`services/atlas-explorer/web`, without redesigning behavior. Ultimately
`web/marketplace` must contain no Atlas client dependency/import, component,
fixture, branding or service-path bridge.

On 2026-11-09 the user selected **move UI to services/atlas-explorer/web**
(preferred) after reviewer CHANGES_REQUESTED on 011: Atlas boundary errors
**134→154**, overall **155→175**, warnings unchanged. This is an ownership
regression, not an acceptable checker exception. The destination decision is
resolved. The user approved 012a, selected offline-only artifact checks, and
chose to retire the entire core web package instead of retaining a generic
shell. That retirement is a separate plan/gate; 012 must not delete or replace
the core web app until the retirement scope and consumer impact are inventoried
and explicitly approved.

011's imports, dependency/lock and compile fixture are **unaccepted dirty
implementation**, not a completed prerequisite. Preserve them now. During
approved 012 implementation they serve only as the existing temporary bridge:
copy their public-package imports/fixture into the service, then supersede
and remove their core copies during the cleanup below. Do not revert them
in planning, replay 011, mark it accepted, or remove the dependency early.
012 replaces 011's consumer-migration route; it does not rewrite 011 history.

No publication. Current core SDK is still `0.1.0` and contains Atlas exports.
Atlas client is `@haas/atlas-explorer-client@0.1.0`, transitional SDK peer
`^0.1.0 || ^1.0.0`. Keep that combination here. Selected SDK `1.0.0` / final
peer `^1.0.0` cutover and removal of core exports remain exclusively **010c**,
after its full consumer inventory and rerun of service UI checks.

## Ownership and boundary

| Change | Deletion test / owner |
|---|---|
| Atlas app, cards, CSS, fixture, UI manifest and deployment instructions | Would not exist without Atlas: plugin service |
| Remove domain code/dependency from core web | Core-client cleanup under ADR-18; no new domain behavior |
| Retain a minimal generic web entry after relocation | Would exist without Atlas: client-owned shell, not a new extension point. Plugins can host separate public-SDK UIs; web-research can use the same pattern |
| Generic SDK factory/run/thread/event contracts | Existing public core surface; unchanged. Plugins can compose caller-created clients, including web-research |

No missing core capability, UI slot, registry, iframe, redirect, core→service
import, runtime integration or new ADR is needed. ADR-18 covers public client
composition and boundary cleanup. No checker exclusions or source aliases.
The service remains **health-only**. UI HaaS calls still use a caller-created
public client; the Atlas facade does not call an authenticated Atlas backend.
Auth, producer support, database readiness and plan 008 are not delivered here.

## Inspected baseline and unknowns

Bounded inspection: marketplace tree, package, HTML/main/App, Vite and both
tsconfigs; source sizes/import graph; 011 fixture; service README/client manifest;
ADR-18 and marketplace design (target, not implemented).

- `build` is `tsc && vite build`; preview is `vite preview`. React/Vite tooling
  is local; no new UI framework is needed.
- Existing SDK TS paths entry is `../haas_sdk_ts/dist` (misleading relative
  path). Do not copy it into the service; use installed public package entries.
- Vite has React, port 3000, `/v1` dev proxy to localhost:8000, source maps.
  Preserve development behavior in the service, but never start/use the proxy
  for verification. The proxy is not auth or deployment readiness.
- `main.tsx` 9 lines, `App.tsx` 54, App CSS 139, component barrel 20.
  Components/CSS are inventoried in the slice table. Largest TSX is 351 lines;
  moving it alone costs **702** added+deleted lines, so copying and deletion
  must be separate slices.
- Historical 011 evidence: both dependency builds, contract compile and
  standalone Vite bundle passed; full UI build retained five TS errors:
  TS6133 `AtlasExplorerState`, TS6133 `initialQuery`, TS2339 `nodes`
  envelope/output_delta mismatch, TS2304 `DimensionKey` twice.
  Lint failed for missing ESLint config. These are not authorized fixes.
- No fresh commands, git diff/status, installs, builds or boundary execution
  were available in this planning turn. Counts above are recorded 011 evidence,
  not freshly measured results. Dirty ownership/deltas must be captured before
  implementation; preserve all unrelated work, node_modules and existing dist.
- Public assets and actual dependency availability were not exhaustively read.
  Before 012a, inventory direct `public/` files and HTML/CSS asset references.
  Observed HTML references `/vite.svg`; if present, add that exact file to the
  next full slice allowlist before copying. Stop for any other required asset;
  no wildcard migration or silent omission.

## Global scope, budgets and gates

Planning edits: **this file and `docs/plans/README.md` only**.
Implementation paths are the explicit per-slice allowlists below. No SDK,
client-ts, Python service, compose, core deployment, governance or auth edits.
No stage, reset, stash, commit, publish, dependency upgrade or unrelated fix.

Each row is a separately approved/reviewed slice, **≤400 changed lines**:
additions + deletions, including handwritten tests/docs/evidence; only generated
lockfiles/output excluded. Counts below are source counts, not full budgets.
Reserve remaining space for evidence; stop/replan if the measured total exceeds
400. Never use rename detection or minification to hide copy/delete cost.
Each later row needs a full bounded slice contract before execution, expanding
only that row's allowlist and verification, not the whole epic.

Fresh baseline: record status, scoped diff/numstat, tool versions, dependency
resolution, diagnostics, lint and both boundary reports before every slice.
Use **154 Atlas / 175 overall** only if freshly reproduced. Never increase
either findings set relative to that slice's baseline; compare rule/path
findings as well as counts. No new core domain references. Copy slices may
hold counts flat; deletion slices must decrease applicable findings; purely
CSS deletion can be flat. Final core-web Atlas findings must be zero, totals
below the pre-011 **134/155** ceiling; no claim that all repository Atlas debt
is fixed. If unrelated dirty work prevents comparison, stop and ask.

Both entry bundles must stay usable after every slice. “Usable” means Vite
can build the applicable entry; it does **not** mean the baseline `tsc && vite`
script passes. Preserve exactly the five existing diagnostics when the full
Atlas app is typechecked, or evidence their disappearance solely because the
affected source leaves core. No new diagnostics, casts, ignores, relaxed
strictness or event-shape repairs. Service staging copies are deliberately
inactive, not evidence of component readiness.

## Ordered slices and exact allowlists

Notation: **S** = `services/atlas-explorer/web`; **C** = `web/marketplace`;
**A** = `src/components/atlas`. Braced names enumerate files, not wildcards.
Copy rows only add the listed S files from the same C relative paths,
byte-for-byte, unless explicitly stated. No C edits in service-copy rows.
Small evidence updates may touch this plan/index within the remaining budget.

| ID / owner | Exact allowed files and operation | Source-line cost |
|---|---|---|
| 012a / plugin | Scaffold/fixture listed in full below | ≤320 total |
| 012b / plugin | S/A/EntityCard.{tsx,css}: copy | 333 |
| 012c / plugin | S/A/{ComparisonRow.tsx,ComparisonRow.css,SpecialistProfile.tsx}: copy | 341 |
| 012d / plugin | S/A/{SpecialistProfile.css,PricingTierCard.tsx,PricingTierCard.css}: copy | 333 |
| 012e / plugin | S/A/ChecklistCard.{tsx,css}: copy | 322 |
| 012f / plugin | S/A/HITLApprovalDrawer.tsx and S/src/App.css: copy | 253 |
| 012g / plugin | S/A/{HITLApprovalDrawer.css,index.ts}: copy | 292 |
| 012h / plugin | S/A/MillerColumn.tsx: copy | 280 |
| 012i / plugin | S/A/{MillerColumn.css,AtlasExplorer.css}: copy | 339 |
| 012j / plugin | S/A/AtlasExplorer.tsx: copy | 351 |
| 012k / plugin | S/src/{App.tsx,main.tsx}: copy; S/{index.html,tsconfig.json,README.md}, S/public/vite.svg: activate full app/checks | ≤220 total |
| 012l / client | Blocked: whole core web retirement is governed by separate plan 013; no core UI replacement/deletion authorized by 012 | 0 |
| 012m / client | C/A/AtlasExplorer.tsx: delete | 351 |
| 012n / client | C/A/MillerColumn.tsx: delete | 280 |
| 012o / client | C/A/EntityCard.{tsx,css}: delete | 333 |
| 012p / client | C/A/{ComparisonRow.tsx,ComparisonRow.css,SpecialistProfile.tsx}: delete | 341 |
| 012q / client | C/A/{SpecialistProfile.css,PricingTierCard.tsx,PricingTierCard.css}: delete | 333 |
| 012r / client | C/A/ChecklistCard.{tsx,css}: delete | 322 |
| 012s / client | C/A/HITLApprovalDrawer.tsx: delete | 114 |
| 012t / client | C/A/HITLApprovalDrawer.css: delete | 272 |
| 012u / client | C/A/{MillerColumn.css,AtlasExplorer.css}: delete | 339 |
| 012v / client | C/{package.json,package-lock.json}: remove Atlas dependency; C/tests/atlas-imports.contract.ts: delete; S/README.md and services/atlas-explorer/README.md: final ownership/run/deployment notes | ≤250 total |

Dependencies are strictly table order. Copies preserve the old runnable app;
012a–j expose only a static staging page in S. Service tsconfig selects only
its compile fixture until 012k, so unresolved staged component graphs are not
silently treated as typechecked. At 012k switch `include` to `src` and run the
separate fixture check. Never expose a half-copied UI.

The user selected full core web retirement instead of the originally proposed
generic shell. Do not replace the core app or delete any marketplace route in
012. After the separately planned/approved retirement scope is ready, coordinate
removal of the core Atlas UI and 011 bridge with its exact consumer/deployment
plan; keep all existing files until that gate. Never leave dangling imports.

No new container is required for static UI ownership. Service README must say
the current Python Dockerfile/compose build only the health skeleton and do
not serve this UI. Document independent static artifact deployment, same-origin
`/v1` routing as an operator prerequisite, and no credentials in bundles.
Adding a UI container/proxy, auth or production routing is a separate plan.

## 012a — full first slice: independent staging build and public contract

**Status:** clean offline SDK provisioning verified; 012a independent re-review pending.
**Owner:** plugin-engineer.
**Depends on:** available local dependencies and fresh baseline. Zero migrations.

**Exact allowed paths (all new):**

- S/`package.json`, `package-lock.json`
- S/`index.html`, `vite.config.ts`, `tsconfig.json`, `tsconfig.node.json`
- S/`tests/atlas-imports.contract.ts`
- S/`README.md`
- `haas_sdk_ts/package-lock.json` only: generated from its unchanged manifest
  to provision the linked SDK's runtime dependency reproducibly. No SDK source,
  manifest, or installed dependency tree changes.
- This plan/index: short status/evidence only

The bounded pre-012a asset inspection found `web/marketplace/public/vite.svg`,
referenced by its `index.html`; copy that exact asset in 012k (the 012k allowlist
includes `services/atlas-explorer/web/public/vite.svg`). No other public assets
were found. Do not copy all of `public/`.

**Implementation:**

1. Capture the baseline below and asset inventory; coordinate dirty paths.
   Do not modify the existing C fixture/imports or installed dependency trees.
2. Create private `@haas/atlas-explorer-web@0.1.0`, npm-local lock. Keep current
   marketplace React/tooling versions, no new framework or testing dependency.
   Dependencies: `@haas/sdk: file:../../../haas_sdk_ts` and
   `@haas/atlas-explorer-client: file:../client-ts`, plus React/react-dom.
   Use own dev dependencies/binaries, not C/node_modules or source aliases.
   Prefer approved cached/offline provisioning; missing cache blocks, never
   falls back to registry traffic without separate permission.
   The linked public SDK's own runtime dependencies must be provisioned from
   its committed lock before service checks; the SDK lock is the only allowed
   core path in this slice.
3. Scripts: `dev`, `build` (`tsc && vite build`), `preview`, `typecheck`
   (`tsc --noEmit`), `bundle` (`vite build`). Copy existing strict TS options
   but omit SDK `paths`/`baseUrl`. Initially include only `tests` in main
   tsconfig; retain separate Vite config tsconfig. No source entry yet.
4. HTML has a root and plain staging notice, no script/network assets.
   This is temporary staging, not Atlas availability. Vite React config
   retains current local-dev proxy/port and output settings. Build staging
   HTML successfully; document that full component checking starts at 012k.
5. Copy the existing 22-line compile fixture verbatim. Exported factory must
   never execute. Compile against built public SDK/client entries.
6. README records local dependency build order, staging state, commands,
   deferred auth/health-only backend, no publication and independent static
   deployment. Do not claim a working exploration backend.

**Acceptance:**
- [x] Only allowlisted new files; ≤320 target, ≤400 actual handwritten lines.
- [x] Independent local `npm ls`, SDK/client builds, fixture typecheck and
  staging HTML bundle pass. Lock contains no unrelated upgrades.
- [x] C bundle/diagnostics and both boundary reports unchanged; no new findings.
- [x] Staging artifacts have no script that mounts Atlas or invokes a client.
- [x] Reviewer APPROVE; dirty work outside this slice preserved.

## Verification ladder (012a and subsequent full slices)

Run from repo root; capture complete exit codes/diagnostics, no tracked output
redirection. These are **instructions**, not commands executed by this planner.

```sh
git status --short
git diff --numstat -- web/marketplace services/atlas-explorer/web docs/plans
git diff --check
node --version
npm --version
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas-explorer
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py
npm --prefix haas_sdk_ts run build
npm --prefix services/atlas-explorer/client-ts run build
npm --prefix web/marketplace ls @haas/sdk @haas/atlas-explorer-client
npm --prefix web/marketplace run build
npm --prefix web/marketplace run lint
(cd web/marketplace && ./node_modules/.bin/vite build --outDir "$(mktemp -d)")
```

Once S exists, provision only with permission using
`npm --prefix services/atlas-explorer/web install --offline --ignore-scripts --no-audit --no-fund`;
inspect generated lock. Repeat with `ci --offline` in a disposable local copy
to prove lock reproducibility without altering the dirty source tree.

```sh
npm --prefix services/atlas-explorer/web ls @haas/sdk @haas/atlas-explorer-client
npm --prefix services/atlas-explorer/web run build
(cd services/atlas-explorer/web && ./node_modules/.bin/tsc --noEmit --strict --skipLibCheck --target ES2020 --module ESNext --moduleResolution bundler --noUnusedLocals --noUnusedParameters tests/atlas-imports.contract.ts)
(cd services/atlas-explorer/web && ./node_modules/.bin/vite build --manifest --outDir "$(mktemp -d)")
```

At 012k and final 012v: end-to-end **UI build / offline preview-artifact**
verification builds local SDK → client → real HTML/main/App/component/CSS graph,
then reads the disposable output HTML, Vite manifest and every referenced
local JS/CSS asset. Confirm root, entry references, nonempty chunks/styles,
no unresolved package specifiers or missing assets; compare copied files to
their captured source. Service full build must reproduce only the same five
TS diagnostics; standalone bundle and separate contract must pass. Do not
silently change `build` to bypass TS. Core must bundle throughout and finally
typecheck without the removed Atlas diagnostics.

This is **not** a live browser, HTTP, service integration or auth test.
No core/Atlas server, Vite preview listener, curl, browser mount, fetch,
session-manager execution or network invocation is authorized. `npm run
preview` remains a documented manual command, not executed evidence. An actual
interactive preview requires separate permission; do not claim one passed.
Python tests/Ruff/Pyright: N/A (no Python edits). Missing tooling is BLOCKED.

Final negative scan (expected no matches; rg exit 1 is success here):

```sh
rg -n -i 'atlas|atlas-explorer-client|services/atlas-explorer' web/marketplace/src web/marketplace/tests web/marketplace/package.json web/marketplace/package-lock.json web/marketplace/index.html web/marketplace/vite.config.ts web/marketplace/tsconfig.json web/marketplace/tsconfig.node.json
```

Also read the tracked core-web file inventory for residual domain assets.
If references outside these bounded paths appear, stop for an exact new
allowlist, not a broad deletion. Final 010 G2 inventory remains mandatory.

## Risks, rollback and decisions needed

- Duplicate inactive files can drift: capture source hashes and compare before
  activation; any concurrent UI edits require rebaseline/review, not overwrite.
- Relocation exposes baseline TS failures in a second package: preserve them
  explicitly, never claim a green full build or backend readiness.
- Rollback before entry switch: leave original runnable app intact; discard
  only approved slice additions with owner permission. After switch, restore
  only reviewed slice changes in dependency-reverse order, restoring dependency
  before imports and components before App. No reset of the dirty worktree;
  coordinate with 010c before any export-dependent restoration.

1. **Approve this bounded execution route?** A: approve 012a then review each
   expanded row (recommended); B: hold/replan. Destination is already decided.
2. **Core web disposition:** user selected retirement of the entire core web
   package, not a generic shell. A separate bounded plan must inventory all web
   consumers/routes and impacts before proposing removal. Until approved,
   012l's shell replacement is not authorized; 012 UI cleanup must coordinate
   with that plan and must not remove the whole web application.
3. **Preview scope:** user selected offline build/artifact checks only. No
   server, browser, preview listener, or network request is authorized.

## Log

- 2026-11-09 architect: recorded user destination decision and reviewer blocker;
  proposed bounded strangler copy/activate/delete sequence and full 012a.
  Only plan/index edited; no runtime/test changes, staging or rollback performed.
- 2026-11-09 user: approved 012a and offline-only checks; selected retirement
  of the whole core web package. A separate inventory/plan/approval is required;
  012 no longer replaces the core app with a shell.
- 2026-11-09 user: approved 012a; selected whole-core-web retirement instead of
  retaining a generic shell, and offline-only artifact verification. Full web
  retirement requires a separate plan and approval; no server or network test.
- 2026-11-09 plugin-engineer: added the approved inactive staging package files
  and confirmed the copied fixture is byte-identical; offline npm install
  stopped with missing cached `rolldown@~1.2.16` (ETARGET). No registry fallback,
  lockfile, service build, fixture compile or bundle was attempted; boundary
  baselines remained 154/1 and 175/9.
- 2026-11-09 user: requested removal of the new 012a UI staging scaffold only.
  The seven files under `services/atlas-explorer/web` were removed; earlier
  Atlas service/client work and marketplace changes were left untouched.
  012a remains blocked pending an offline dependency source and renewed direction.
- 2026-11-09 user: renewed direction to complete the remaining extraction.
  Plugin engineer recreated the inactive 012a scaffold using exact Vite 8.3.5,
  whose `rolldown@~1.2.14` range is satisfied by cached 1.2.15; all package
  provisioning remained offline. Offline install and disposable `npm ci`,
  SDK/client builds, package build, strict fixture compile and staging bundle
  passed. The fixture is byte-identical to the marketplace fixture and was not
  executed. Parent independently confirmed package dependency resolution/build,
  strict fixture compile, staging bundle, unchanged marketplace five-error
  baseline, and unchanged Atlas/overall boundary reports (154/1, 175/9).
  No core files or marketplace files were changed by this slice; independent
  review is pending. No UI components, app entry, or activation were added.
- 2026-11-09 reviewer: CHANGES_REQUESTED because the service lock represents
  `@haas/sdk` as a local link and omits its `eventsource-parser` dependency.
  A clean offline SDK install could not be verified: the SDK has no committed
  lockfile and npm cache metadata is missing (`graphemer`/`wrappy`); a
  package-lock generated from the existing installed tree was incomplete and
  was discarded. No network access was used. 012a remains blocked until an
  offline reproducible provisioning route is available or separately approved.
- 2026-11-09 user: authorized npm registry access only to generate and verify
  the SDK lockfile. Scope is limited to that lockfile and a disposable install;
  no manifest/source changes, publication, or other registry use.
- 2026-11-09 lead: generated `haas_sdk_ts/package-lock.json` from the unchanged
  SDK manifest using the authorized registry access; 252 lock entries include
  resolved metadata. In a clean disposable copy, `npm ci --offline`, SDK build,
  10 SDK tests and a second offline `npm ci` plus build passed. A separate clean
  hierarchy then provisioned the SDK from its lock and the service package from
  its lock; `npm ls` showed the linked SDK's `eventsource-parser@1.1.2`, while
  service build and strict fixture compile passed. No source/manifest changes or
  installed-tree modifications were made; re-review requested.
- 2026-11-09 reviewer: APPROVE after lock provisioning fix; staging remains
  inert. 012a accepted with SDK/service clean-copy install evidence and
  unchanged boundary counts.
- 2026-11-09 user: “Lets complete the rest.” Lead created plan 014 for the
  next copy-only slice, 012b; no core UI cleanup or activation is included.
- 2026-11-09 reviewer: APPROVE plan 014; EntityCard files are byte-identical
  copies, staged UI remains inactive, and no findings remain.
- 2026-11-09 reviewer: APPROVE plan 015; comparison and specialist files are
  byte-identical copies, staged UI remains inactive, and no findings remain.
- 2026-11-09 lead: started plan 016 for 012d, copying the specialist stylesheet
  and pricing card files without changing marketplace sources or activating the
  staging UI.
- 2026-11-09 verifier: plan 016 source parity, service build/fixture/bundle,
  TypeScript SDK and Atlas client checks passed. Marketplace bundle passed and
  its full build retained the same five baseline diagnostics. Boundary counts
  remained Atlas 154/1 and full repository 175/9. Independent review pending.
- 2026-11-09 reviewer: APPROVE plan 016; specialist/pricing files are
  byte-identical copies, staging remains inactive, and no findings remain.
- 2026-11-09 lead: started plan 017 for 012e, copying the checklist card into
  the inactive service UI tree while preserving its pre-existing modified
  marketplace source.
- 2026-11-09 verifier: plan 017 source parity, service build/fixture/bundle,
  TypeScript SDK and Atlas client checks passed. Marketplace bundle passed and
  its full build retained the same five baseline diagnostics. Boundary counts
  remained Atlas 154/1 and full repository 175/9. Independent review pending.
- 2026-11-09 reviewer: APPROVE plan 017; checklist files are byte-identical
  copies, staging remains inactive, and no findings remain.
- 2026-11-09 lead: started plan 018 for 012f, copying the approval drawer and
  app stylesheet into the inactive service UI tree.
- 2026-11-09 verifier: plan 018 source parity, service build/fixture/bundle,
  TypeScript SDK and Atlas client checks passed. Marketplace bundle passed and
  its full build retained the same five baseline diagnostics. Boundary counts
  remained Atlas 154/1 and full repository 175/9. Independent review pending.
- 2026-11-09 reviewer: APPROVE plan 018; approval drawer and app stylesheet are
  byte-identical copies, staging remains inactive, and no findings remain.
- 2026-11-09 lead: started plan 019 for 012g, copying the drawer stylesheet and
  component barrel into inactive service staging; the barrel source is already
  user-modified, so its current contents will be preserved exactly.
- 2026-11-09 verifier: plan 019 source parity, service build/fixture/bundle,
  TypeScript SDK and Atlas client checks passed. Marketplace bundle passed and
  its full build retained the same five baseline diagnostics. Boundary counts
  remained Atlas 154/1 and full repository 175/9. Independent review pending.
- 2026-11-09 reviewer: APPROVE plan 019; drawer stylesheet and export barrel
  are byte-identical copies, staging remains inactive, and no findings remain.
- 2026-11-09 lead: started plan 020 for 012h, copying the MillerColumn
  component into inactive service staging. Its marketplace source is already
  user-modified; preserve its current contents exactly.
- 2026-11-09 verifier: plan 020 source parity, service build/fixture/bundle,
  TypeScript SDK and Atlas client checks passed. Marketplace bundle passed and
  its full build retained the same five baseline diagnostics. Boundary counts
  remained Atlas 154/1 and full repository 175/9. Independent review pending.
- 2026-11-09 reviewer: APPROVE plan 020; MillerColumn is a byte-identical copy,
  staging remains inactive, and no findings remain.
- 2026-11-09 lead: started plan 021 for 012i, copying the MillerColumn and
  AtlasExplorer stylesheets into inactive service staging.
- 2026-11-09 verifier: plan 021 source parity, service build/fixture/bundle,
  TypeScript SDK and Atlas client checks passed. Marketplace bundle passed and
  its full build retained the same five baseline diagnostics. Boundary counts
  remained Atlas 154/1 and full repository 175/9. Independent review pending.
- 2026-11-09 reviewer: APPROVE plan 021; stylesheets are byte-identical copies,
  staging remains inactive, and no findings remain.
- 2026-11-09 lead: started plan 022 for 012j, copying AtlasExplorer into
  inactive service staging. Its marketplace source is already user-modified;
  preserve its current contents exactly.
- 2026-11-09 verifier: plan 022 source parity, service build/fixture/bundle,
  TypeScript SDK and Atlas client checks passed. Marketplace bundle passed and
  its full build retained the same five baseline diagnostics. Boundary counts
  remained Atlas 154/1 and full repository 175/9. Independent review pending.
- 2026-11-09 reviewer: APPROVE plan 022; AtlasExplorer is a byte-identical
  copy, staging remains inactive, and no findings remain.
- 2026-11-09 lead: started plan 023 for 012k, activating the full UI only in
  the service package. Core marketplace files and Atlas exports remain intact;
  authentication and production routing remain out of scope.
- 2026-11-09 verifier: plan 023 service bundle, fixture, package tests/builds,
  output checks, SDK/client tests/builds and boundaries passed or matched
  baseline. The service and marketplace TypeScript builds report the same five
  existing AtlasExplorer diagnostics; no new diagnostics were introduced.
- 2026-11-09 reviewer: APPROVE plan 023; confirmed source parity, bundle output,
  documentation constraints and boundary counts. Reviewer ran boundaries but
  relied on verifier evidence for builds/tests.
