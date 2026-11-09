# 011 — Migrate marketplace Atlas imports to the service client

- Status: blocked — reviewer requested a UI ownership decision because the import migration adds 20 boundary errors
- Type: slice
- Owner agent: client-engineer
- Epic: [001](001-atlas-extraction-sdk-epic.md); prerequisite/cutover: [010](010-atlas-typescript-client-extraction.md)
- ADR: [ADR-18](../adr/0018-domain-client-package-ownership.md) — accepted; no new extension point

## Goal and sequencing

Move only the existing marketplace Atlas facade/type imports and re-exports
from `@haas/sdk` to `@haas/atlas-explorer-client`. Preserve UI behavior and
caller-created generic client injection.

010b implementation and independent review are complete. Plan 010 records it
as done. This approved consumer slice is the remaining known G2 migration before
core exports can be removed. The user approved this scope on 2026-11-09; do not
mark 010c done here.

Order: **010b approved → 011 migration verified/reviewed → 010c removal**.
011 uses the transitional peer `^0.1.0 || ^1.0.0` and current SDK. 010c must
then rerun these consumer checks with SDK `1.0.0`, client `0.1.0`, final peer
`^1.0.0`. This is a coordinated breaking cutover, not a shim/deprecation plan.
011 clears only the known marketplace part of 010 G2; the full consumer
inventory remains 010c's prerequisite. No publication is authorized.

## Boundary classification

| Change | Ownership / deletion test |
|---|---|
| Atlas-specific imports, component type re-exports and dependency | Extension UI integration in an existing client path: would not exist without Atlas. Remove old coupling; do not add domain behavior to generic SDK/core. |
| `createClientFromEnv` import | Generic client contract: would exist without Atlas. Plugins can compose caller-created clients; web-research can use the same pattern. Keep unchanged. |

No core API, extension point, service route, transport or authentication change.
Existing Atlas UI placement in `web/` is legacy debt, not precedent for adding
plugin-specific core facilities. ADR-18 already governs this ownership move.

## Scope and budget

**Implementation allowlist:**
- `web/marketplace/package.json` — add
  `"@haas/atlas-explorer-client": "file:../../services/atlas-explorer/client-ts"`;
  retain `"@haas/sdk": "file:../../haas_sdk_ts"`. No version bump or new tooling.
- `web/marketplace/package-lock.json` — minimal npm-generated local dependency
  graph update; preserve existing dependency versions.
- `web/marketplace/src/App.tsx` — split imports: generic factory stays in SDK,
  Atlas factory moves to service package.
- `web/marketplace/src/components/atlas/{AtlasExplorer,MillerColumn,EntityCard,ComparisonRow,PricingTierCard,SpecialistProfile,ChecklistCard}.tsx`
  — change only Atlas import declarations, using `import type` where appropriate.
- `web/marketplace/src/components/atlas/index.ts` — move four existing type
  re-exports to the service root; update adjacent ownership comment.
- `web/marketplace/tests/atlas-imports.contract.ts` — new compile-only public
  package contract fixture described below.
- This plan and `docs/plans/README.md` — evidence/status only.

Target ≤120 added + deleted implementation/test lines; **≤400 total changed
non-generated lines including this plan/index**, excluding lockfile/generated
output; zero migrations. Count additions and deletions separately. Stop and
re-plan if the measured total exceeds 400; do not drop verification.

**Out of scope:** component logic/JSX/styles/props, SDK or service package
edits, versions/peer changes owned by 010c, new UI/test frameworks, tsconfig or
Vite changes, auth/proxies/env/service URLs, boundary relocation of the UI,
fixing baseline diagnostics, dependency upgrades, publishing. Do not weaken
strictness, add casts/ignores, change envelope types, or emit fake domain types
to obtain a green build. Preserve unrelated worktree edits; no reset/stash.

## Baseline and known unknowns

Measured pre-consumer baseline on 2026-11-09, before any 011 edits:

```text
npm --prefix web/marketplace run build: FAIL, five AtlasExplorer.tsx diagnostics
TS6133: unused AtlasExplorerState
TS6133: unused initialQuery
TS2339: nodes access mismatch (output_delta/envelope shape)
TS2304: missing DimensionKey (two references)
```

These are **pre-existing failures**, not migration failures or authorized fixes.
The baseline command was rerun after plan creation and reproduced the same five
diagnostics. Compare codes, symbol, message and count afterwards (import shifts
may change line numbers).
Additional pre-edit failures must be recorded and triaged, not silently fixed.
Do not claim the marketplace build passes while these remain.

Inspected: npm lockfile/local SDK link; build `tsc && vite build`; lint script
exists but no local ESLint config found; no test script or existing tests found
in the bounded marketplace inspection. TS has strict/unused checks and an
existing `@haas/sdk` paths entry `../haas_sdk_ts/dist`; do not repair that
unrelated entry or add service source aliases. Vite has React and `/v1` proxy;
leave both untouched. New package exports both runtime names and all five
domain types, is private `0.1.0`, and declares the transitional SDK peer.

Captured after plan creation and before edits:

```text
Node v24.15.0; npm 11.14.1
npm ls: @haas/sdk@0.1.0 linked; Atlas client not installed
Standalone Vite build: passed, 38 modules
check_boundaries.py --plugin atlas-explorer: 134 errors, 1 warning
Full boundary count: not freshly captured in this 011 baseline
```

The full marketplace TypeScript build reproduced the same five diagnosed
baseline issues above; no source files have been edited for 011 yet.
Missing tools/dependencies block their checks; they never count as passes.

## Implementation and contract test

1. Capture the baseline below before editing; coordinate overlapping dirty
   paths with their owner. Build the already-reviewed dependency packages.
2. Make only the allowlisted manifest/import changes. Run the lock/install
   command below with dependency provisioning permission; inspect lock delta
   and reject unrelated churn. No registry publication or global link setup.
3. Add a compile-only fixture outside `src` (not loaded by the UI). Import both
   runtime exports and all five type exports from the service root, and
   `createClientFromEnv` from SDK. Export a zero-argument function returning
   `AtlasSessionManager` via `createAtlasSessionManager(createClientFromEnv())`;
   export the five types so unused checks can remain enabled.
   Never execute this function or make a network request. This proves root
   exports and SDK/client assignability, not service connectivity.
4. Run the contract compile, import scan, standalone bundle and full build.
   Preserve the five baseline diagnostics; stop on new migration-caused
   failures. Do not use the failing full build to skip bundler verification.

## Verification rungs — exact commands

Run from repository root; record exit codes and complete diagnostics, before
and after unless marked otherwise. Do not redirect output into tracked files.

```sh
git status --short
git diff --stat -- web/marketplace docs/plans
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

Pre-edit `npm ls` need not find the new client; post-edit it must resolve both
local packages with no invalid/unmet peer. Dependency builds must pass.
Use disposable Vite output to preserve existing `dist`. If the standalone
bundle already fails, record its baseline cause; any dependency/entry-resolution
failure blocking the new imports must be resolved by the owning slice, not
hidden with aliases. An unavailable bundle proof blocks completion.

After manifest edits (installation requires maintainer permission):

```sh
npm --prefix web/marketplace install --ignore-scripts --no-audit --no-fund
```

After adding the fixture, run its isolated compiler without the UI tsconfig:

```sh
(cd web/marketplace && ./node_modules/.bin/tsc --noEmit --strict --skipLibCheck --noUnusedLocals --noUnusedParameters --target ES2020 --module ESNext --moduleResolution bundler tests/atlas-imports.contract.ts)
rg -n -U "(?s)(?:import|export)\b[^;]*?(?:from\s*|import\s*\()['\"]@haas/sdk(?:/[^'\"]*)?['\"]" web/marketplace/src
rg -n '@haas/atlas-explorer-client|@haas/sdk' web/marketplace/package.json web/marketplace/src web/marketplace/tests/atlas-imports.contract.ts
git diff --check -- web/marketplace docs/plans
git diff --numstat -- web/marketplace docs/plans
```

Inspect the SDK scan's declarations, including multiline imports/re-exports:
only `createClientFromEnv` may remain SDK-owned in current UI source; none of
the seven Atlas names or SDK deep imports may remain. Fixture must cover
`AtlasSessionManager`, `createAtlasSessionManager`, `DrilldownRequest`,
`AtlasLayerNode`, `AtlasLayerEnvelope`, `AtlasRunEvent`, `AtlasEventHandler`.
Repeat all post-edit commands after 010c's fresh package builds/install, with
`npm ls` evidence of SDK `1.0.0` / Atlas client `0.1.0` and valid final peer.
That repeat is a 010c integration gate, not permission for 011 to change them.

## Acceptance criteria

- [x] 010b reviewer approval and 011 approval recorded before implementation.
- [x] Manifest/lock resolve both public local packages; SDK stays a direct
  dependency; no source aliases, SDK deep imports or core re-export shim.
- [x] Import scans show all existing Atlas imports/re-exports moved; generic
  factory unchanged. Diff inspection shows no UI behavior changes.
- [x] Contract compile passes; standalone Vite bundle passes with migrated
  public entries. No server, auth flow or live service invoked.
- [x] Full build still has exactly the five recorded baseline diagnostics,
  with **zero new** diagnostics. A baseline change from another slice requires
  a separately evidenced rebaseline, not a hidden fix or blanket waiver.
- [ ] Lint and boundary findings have no new path/rule findings versus fresh
  baseline. Lint reproduces the baseline missing ESLint config. Boundary counts
  rose by 20 errors (Atlas: 134→154; full: 155→175), with warnings unchanged.
  New matches include the approved service dependency and compile-only fixture.
  Independent review found this is a real ownership regression, not acceptable
  transitional debt under this plan.
  Python tests/Ruff/Pyright: N/A, no Python edits.
- [ ] Diff allowlist, whitespace and ≤400-line budget checked; independent
  reviewer APPROVE. Allowlist, whitespace and budget checks pass; reviewer
  requested changes on boundary ownership. 010c remains gated on its full inventory and post-cutover consumer
  verification; this slice alone does not complete Atlas extraction.

## Risks, rollback and decisions

- Stale linked package output or peer duplication → rebuild both packages and
  check `npm ls`, isolated contract compile and actual Vite entry resolution.
- Baseline errors mask regressions → compare individual diagnostics, not only
  exit codes; keep independent contract and bundle gates.
- Rollback: reverse only this slice's approved dependency/import/test changes,
  preserving pre-existing work. If 010c has landed, coordinate restoration of
  core exports first; never leave old imports pointing at removed exports.
- Decisions needed: resolve Atlas UI ownership in the core `web/marketplace`
  path. Recommended: move the Atlas-specific UI and its compile fixture to an
  extension-owned service path rather than add a boundary allowlist exception.
- Follow-ups: five existing TS errors; existing tooling/boundary debt; 010c
  full inventory/version cutover; authenticated service work separately gated.

## Log

- 2026-11-09 architect: proposed bounded consumer migration; user-reported
  baseline and pending 010b review recorded. Runtime/tests untouched.
- 2026-11-09 lead: independently confirmed 010b reviewer approval and
  remeasured the marketplace build baseline; the same five diagnostics remain.
  No marketplace files changed. 011 implementation awaits explicit plan approval.
- 2026-11-09 user: approved the bounded marketplace import/dependency migration.
- 2026-11-09 lead: captured Node/npm, package resolution, standalone Vite bundle,
  and Atlas boundary baseline before implementation; 010b is done and reviewed.
- 2026-11-09 client-engineer: added the local Atlas client dependency, migrated
  Atlas facade/type imports, and added the compile-only public-entry contract.
  Both package builds, contract compile, package resolution, import scans, Vite
  bundle and whitespace check passed. The full marketplace build retained
  exactly the five baseline TS errors; lint retained the baseline missing-config
  error. Boundary findings rose by 20 errors (134→154 plugin-specific;
  155→175 full) with warnings unchanged. Implementation line budget and
  allowlist pass.
- 2026-11-09 reviewer: CHANGES_REQUESTED. Marketplace is treated as core by the
  boundary checker; Atlas dependency/import references increase plugin-specific
  findings. Move the UI/fixture to extension ownership instead of accepting or
  waiving the regression. No UI logic issue found.
