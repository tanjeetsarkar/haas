# 013 — Retire the entire core marketplace web package

- Status: proposed
- Type: bounded epic; first full slice: 013a (inventory/approval only)
- Owners: haas-lead coordinates; client-engineer owns core web retirement;
  plugin-engineer owns relocated service UI; verifier/reviewer gate each slice
- Depends on: [012](012-atlas-ui-service-extraction.md), especially 012k
- Related: [010](010-atlas-typescript-client-extraction.md),
  [011](011-marketplace-atlas-client-imports.md),
  [ADR-18](../adr/0018-domain-client-package-ownership.md)

## Goal and authority

The user selected retirement of **all of `web/marketplace`**, not a generic
shell after Atlas relocation. Record the exact candidate scope and gates here;
conceptual approval is not approval to delete unidentified consumers.
This planning turn changes only this new file and its proposed index row.
No runtime/tests, removals, core composition, deployment or governance changes.

012 moves Atlas-owned UI into `services/atlas-explorer/web`. 013 retires the
old **package**, including its otherwise generic entry/toolchain/assets.
These are different outcomes: relocating Atlas does not prove that the old
package has no consumers, nor authorize deleting marketplace backend features.
No shell, redirect, UI registry, replacement core app or new extension point.
Existing ADR-18 supplies the ownership/composition context, not blanket
authorization for setting/API removal. Any newly required core change needs
its own bounded plan and ADR coverage before implementation.

## Boundary and source ownership

| Surface | Deletion test / owner |
|---|---|
| Atlas components, app behavior, styling, fixture and client integration | Would not exist without Atlas: service-owned under 012; copy/verify before retiring core copies |
| Generic core web entry, npm manifest/lock, Vite/TS config and icon | Could exist without Atlas: core client assets; retire the product surface, do not move generic shell ownership into kernel |
| Generic public SDK | Exists without Atlas: core public client; retain it and all unrelated consumers |
| Marketplace REST API, catalog/signing/install backend, migrations and CLI/SDK | Exists without Atlas: core; explicitly retained |
| Core composition/settings, deployment and governance references | Respective core/operator/user owners; evidence and separate approval required, not client-engineer edits here |

Plugins can own independently built UIs using public SDK composition; both
Atlas Explorer and web-research can use that existing pattern. No new generic
core facility is proposed.

**Auth is deferred. The Atlas Python service remains health-only.** Moving or
bundling UI proves neither authenticated service invocation nor a working Atlas
producer. Plan 008, production routing/auth, database readiness and backend
capability remain outside this retirement.

## Inventory: observed files and dependencies

Inspection used the visible web tree, exact package/config/entry reads and
bounded repository searches, including explicit hidden workflow paths.
This is not a git-tracked/dirty inventory or exhaustive consumer certification.
`web/` visibly contains only `marketplace`; this plan does not authorize deleting
other future web packages or the parent by wildcard.

Let **C** = `web/marketplace`, **A** = `C/src/components/atlas`.
Braces below enumerate exact files, not recursive deletion permission.

| Candidate files observed | Intended disposition after gates |
|---|---|
| C/`package.json`, C/`package-lock.json` | Retire private `@haas/marketplace@0.1.0` and this package's dependency graph only |
| C/`index.html`, C/`vite.config.ts`, C/`tsconfig.json`, C/`tsconfig.node.json` | Retire HTML/build/dev configuration; do not transplant its SDK paths alias |
| C/src/`main.tsx`, C/src/`App.tsx`, C/src/`App.css` | Retire old mount/app/style after 012 parity evidence |
| A/`AtlasExplorer.tsx`, A/`AtlasExplorer.css` | Retire verified old copies |
| A/`MillerColumn.tsx`, A/`MillerColumn.css` | Same |
| A/`EntityCard.tsx`, A/`EntityCard.css` | Same |
| A/`ComparisonRow.tsx`, A/`ComparisonRow.css` | Same |
| A/`SpecialistProfile.tsx`, A/`SpecialistProfile.css` | Same |
| A/`PricingTierCard.tsx`, A/`PricingTierCard.css` | Same |
| A/`ChecklistCard.tsx`, A/`ChecklistCard.css` | Same |
| A/`HITLApprovalDrawer.tsx`, A/`HITLApprovalDrawer.css`, A/`index.ts` | Same, including public type barrel |
| C/tests/`atlas-imports.contract.ts` | Retire core fixture only after service fixture proof; preserve dirty 011 content until then |
| C/public/`vite.svg` | Retire old icon after 012k copies/verifies it |
| C/`dist`, C/`node_modules` (directories observed) | Generated/local state, contents and ownership unverified; preserve, not recursive-cleanup targets |

The 28 enumerated source/config/lock/fixture/asset candidates must be reconciled
with tracked, untracked, ignored and dirty files in 013a. Unknown additional
files block expansion; no `rm -rf`, broad `git clean`, reset, stash or overwrite.
An ignored local directory can remain after tracked package retirement.

Manifest dependencies to disappear **only from C**, not uninstall globally:
`@haas/atlas-explorer-client: file:../../services/atlas-explorer/client-ts`,
`@haas/sdk: file:../../haas_sdk_ts`, `react: ^19.3.1`,
`react-dom: ^19.3.1`. Dev dependencies:
`@types/react: ^19.3.0`, `@types/react-dom: ^19.3.0`,
`@typescript-eslint/eslint-plugin: ^8.72.0`,
`@typescript-eslint/parser: ^8.72.0`, `@vitejs/plugin-react: ^6.1.2`,
`eslint: ^10.13.0`, `typescript: ^5.8.3`, `vite: ^8.3.5`.
Retiring C's lock does not authorize changes to SDK/service locks or versions.

## Entry, route and consumer ledger

| Exact observed source/reference | Evidence and required treatment |
|---|---|
| C/`index.html` → `/src/main.tsx` → `App.tsx` → atlas barrel | Single observed React mount, title `HaaS Atlas`, imports `App.css`; no router dependency in manifest. No separate generic marketplace app identified in this bounded graph |
| C/src/`App.tsx`; seven typed Atlas component imports and A/`index.ts`; C/tests/`atlas-imports.contract.ts` | Existing public SDK/client consumers. 011 altered these imports/manifest/lock/fixture; relocation proof belongs to 012 |
| C/`vite.config.ts` | Dev port 3000, `/v1` proxy to localhost:8000, preview script and `dist`/source maps. This is not a server-side marketplace route or deploy configuration |
| `app/core/config.py:219` | `marketplace_web_dir = Path("web/marketplace")`: exact core path reference. Search found its declaration, not a proven active reader. Determine settings/env compatibility and readers before deletion; leave unchanged here |
| `app/main.py:2282,2291`, `app/api/marketplace.py` | Marketplace router composition and `/v1/marketplace/*` REST routes are backend functionality, **not** retirement candidates; retain |
| `docker-compose.yml:21`; `app/core/config.py:143` | CORS defaults include localhost ports 3000/5173. Shared generic development origins, not proof of package ownership; retain, no automatic CORS pruning |
| `.github/agents/client-engineer.agent.md:24` | Explicit core web build instruction; user-owned governance correction/exception required before obsolete command is treated as retired |
| `.github/skills/verification-ladder/SKILL.md:23` | Explicit rung-9 C build instruction; same user-owned gate, never silently skip a required check |
| `docs/plans/{010-atlas-typescript-client-extraction,011-marketplace-atlas-client-imports,012-atlas-ui-service-extraction}.md` | Historical/current migration consumers of this path; preserve history and coordinate prospective sequencing, not bulk search-replace |
| `README.md:17,20`; `docs/design/marketplace.md`; `docs/portal/index.md`; `docs/portal/migration/v2-to-v3.md`; `docs/haas-python-sdk-spec.md`; `docs/openapi-v1.json`; `docs/HaaS_Complete_Technical_Walkthrough.md` | Marketplace domain/design/API references found, not evidence they depend on this web package; retain unless an exact obsolete web instruction is separately identified |
| Root `Dockerfile`, `docker-compose{,.dev,.prod}.yml`, `deploy/`, `pyproject.toml`, `.github/workflows/ci.yml` | Targeted searches for marketplace/web/Vite/npm/ports found only the compose CORS line above. Workflow directory visibly contains `ci.yml`; full release/job/build-context semantics were not audited |

Broad searches included root docs, app, tests, scripts, services and explicit
workflow/deploy paths, but output limits and ignored/external files constrain
coverage. Root workspace/package/build-context references, indirect commands,
workflow artifacts, static hosts, reverse proxies, ingress, downstream checkouts,
bookmarks and external deployment jobs are **unknown**, not “no consumers”.
Do not delete the core setting or change core composition based on this search.

## Gates and sequencing with 012

1. **G0 / baseline and source consent:** identify every candidate's git state,
   dirty owner and source snapshot. Preserve all unrelated dirty work, especially
   unaccepted 011 imports/lock/fixture. Never mark 011 accepted as a prerequisite.
2. **G1 / relocation:** 012a–k reviewed; real service HTML/App/components/CSS,
   icon and independent fixture/package resolution verified offline. Staging
   HTML alone is insufficient. Capture exact source parity before any deletion.
3. **G2 / consumer decision:** complete local ledger and operator confirmation
   of external/deploy consumers, old URL disposition and rollback artifact.
   Unknown consumers mean HOLD, not implied consent.
4. **G3 / ownership and plan reconciliation:** lead records an approved amendment
   to 012 before execution. 013's whole-package retirement replaces blocked
   012l and overlaps 012m–v's core removals; never execute both deletion tracks.
   Keep 012's service documentation obligations. No generic-shell requirement.
   Resolve core setting and user-owned governance gates through their owners.
5. **G4 / release authorization:** approve exact reconciled deletion list and
   per-slice budgets. Any needed core setting/deploy/CI changes get separate
   contracts/ADR coverage; this epic does not authorize them. No publication.

010c's full consumer inventory, SDK export/version break and peer cutover remain
separately gated. Retirement of the known C consumer does not clear all 010c
consumers. No SDK/client package version or public API changes in 013.

## Bounded ordered slices

Every implementation row requires its own approved full contract and fresh
numstat first: **≤400 added + deleted handwritten lines**, including evidence/
docs/tests; generated lock/output excluded; zero migrations. Counts borrowed
from 012 are estimates, not measured retirement budgets. Stop/repartition if
actual totals exceed 400; do not hide deletions with rename detection.

| ID / owner | Exact candidate operation | Depends on / budget |
|---|---|---|
| 013a / architect + lead | Inventory/decision record in this plan and index only; no deletion authority | First full slice below; ≤400 |
| 013b / client | Retire C/`package.json`, `package-lock.json`, `index.html`, `vite.config.ts`, `tsconfig.json`, `tsconfig.node.json`, src/`main.tsx`, src/`App.tsx`, tests/`atlas-imports.contract.ts`, public/`vite.svg` | G0–G4; measure asset/entry counts, ≤400 |
| 013c / client | Delete A/`AtlasExplorer.tsx` | 013b; estimated 351 source lines |
| 013d / client | Delete A/`MillerColumn.tsx` | 013c; 280 |
| 013e / client | Delete A/`EntityCard.{tsx,css}` | 013d; 333 |
| 013f / client | Delete A/`{ComparisonRow.tsx,ComparisonRow.css,SpecialistProfile.tsx}` | 013e; 341 |
| 013g / client | Delete A/`{SpecialistProfile.css,PricingTierCard.tsx,PricingTierCard.css}` | 013f; 333 |
| 013h / client | Delete A/`ChecklistCard.{tsx,css}` | 013g; 322 |
| 013i / client | Delete A/`HITLApprovalDrawer.tsx`, C/src/`App.css` | 013h; 253 |
| 013j / client | Delete A/`{HITLApprovalDrawer.css,index.ts}` | 013i; 292 |
| 013k / client | Delete A/`{MillerColumn.css,AtlasExplorer.css}`; final evidence in this plan/index | 013j; 339 plus evidence ≤400 |

013b is the atomic package/entry retirement: no active core entry is left with
dangling imports. Later rows remove inactive orphan sources, not runnable
features. Until 013b, keep the old core bundle available; afterward the release
contract must explicitly say the old web package is absent. Never require
building a deleted manifest or treat its missing command as a passing check.
Core API and independent service UI stay releasable at every step.

## 013a — first full slice (documentation checkpoint only)

Allowed edits: this plan and `docs/plans/README.md` only. Owners provide
read-only local evidence; no installs, runtime/test changes or removals.
Record exact tracked/untracked/ignored candidates, line counts, dirty owners,
consumer dispositions, relocation evidence and decisions. Any newly discovered
file/reference requires explicit scope review, not recursive inclusion.

Local-only evidence commands for the executing owner, **not run by this planner**:

```sh
git status --short --untracked-files=all
git diff --numstat -- web/marketplace services/atlas-explorer/web docs/plans
git diff -- web/marketplace
git ls-files -- web/marketplace
git ls-files --others --exclude-standard -- web/marketplace
git ls-files --others --ignored --exclude-standard -- web/marketplace
rg -n --hidden -g '!.git/**' -g '!**/node_modules/**' -g '!**/dist/**' 'web/marketplace|@haas/marketplace|marketplace_web_dir' .
git diff --check -- docs/plans/013-retire-core-marketplace-web.md docs/plans/README.md
```

Inspect root package/workspace manifests if present, build contexts/copy rules
and complete local `ci.yml`/deploy files; absence of text matches is only search
evidence. Record each consumer as retain/migrate/retire/unknown, its owner and
approval. No live network, server, proxy, browser navigation or secret inspection.

Acceptance:
- [ ] Exact 28-candidate list reconciled against git/local state; additions
  reviewed; dirty 011 and unrelated work preserved, owner consent recorded.
- [ ] G1 source/fixture/asset evidence linked; no staging-only readiness claim.
- [ ] External/deploy ledger and numbered decisions resolved or explicitly HOLD.
- [ ] G3 amendments/owner approvals recorded; no competing deletion authority.
- [ ] Only these two documentation paths changed; whitespace check result
  recorded; reviewer APPROVE before requesting 013b authorization.

## Implementation verification and baseline requirements

Each later full contract must capture versions, scoped diffs and both boundary
reports before/after. Baseline commands include:
`python3 .github/skills/core-boundary-check/scripts/check_boundaries.py`
and the same command with `--plugin atlas-explorer`. Never add checker exclusions.
No new rule/path findings; final core-web Atlas findings zero, without claiming
repository-wide cleanup. Historical 011 counts are 154 Atlas / 175 overall,
not fresh measurements or permission to restore debt removed by 012.

Before 013b, repeat 012k's offline SDK → service-client → service-UI fixture and
disposable Vite bundle/manifest/asset checks using installed local tools.
Run C's existing build/lint/bundle baseline while it still exists. Historical
full build has five TS diagnostics and lint lacks config; preserve/compare,
do not fix or conceal them. After retirement, repeat independent service
checks; verify the exact retired tracked files are absent and retained API/
SDK/composition files unchanged. Service TS failures remain explicit; disappearance
of core diagnostics due to deletion is not a service typecheck pass.
Use approved temporary output, preserve existing dist/dependencies. Missing
local tools/cache blocks evidence: no registry fallback, `npx` download,
publish, package upgrades or unrelated Python tests. Plan-only checks do not
establish runtime readiness; verifier selects affected retained test rungs
in the later full contracts.

Planning baseline: repository `view`/`glob`/`rg` only; no git status/diff,
builds, installs, tests or boundary execution available in this turn. Pass/fail
counts and dirty deltas are unmeasured, not zero. No live network used.

## Safe release, risks and rollback

- Unknown hosts/links/build jobs → HOLD until operator inventory and explicit
  old-URL decision. Prefer separately deployed service artifact, no core redirect.
  Any actual host/routing rollout requires separate operator authorization.
- Data/feature loss → retain backend routes/tables/SDKs; prove UI parity through
  012 and obtain exact removal approval. Auth/backend readiness stays deferred.
- Release → retain the last approved old web artifact and its provenance locally;
  record source snapshots including dirty 011 ownership, resolved dependency
  versions and old deployment instructions. Do not publish or deploy in this plan.
  Service rollout/acceptance precedes old package withdrawal; build-only evidence
  must not be represented as operational acceptance.
- Rollback → restore only approved retirement changes in reverse slice order,
  from captured sources/reviewed commits with owner consent; never reset/stash
  the worktree or discard other work. Restore old entry/manifest last, after
  its complete component graph. Operators may separately restore the retained
  old artifact; no live rollback action is authorized here. Do not resurrect
  unaccepted 011 as accepted architecture, undo 012, or reverse 010c implicitly.

## Decisions needed

1. **Unknown consumers:** A) HOLD removal pending named operator/deployment
   confirmation; B) explicitly accept a documented unknown-consumer break.
   Recommend A. Whole-package retirement is already selected, not reopened.
2. **Core setting:** A) retain `marketplace_web_dir` pending a separate
   core-owner compatibility audit; B) authorize a separately planned removal.
   Recommend A until readers/env/deployment usage are known; no setting edit here.
3. **Obsolete governance commands:** A) user updates the two exact agent/skill
   references, or supplies an explicit approved transition; B) defer retirement.
   Recommend A before G4; agents cannot edit these files.
4. **012 coordination:** A) approve a lead-recorded amendment replacing its
   core cleanup track with 013b–k while retaining service obligations;
   B) defer 013 until an alternative non-overlapping sequence is approved.
   Recommend A after 012k evidence; neither track may delete files now.

## Log

- 2026-11-09 architect: proposed inventory-led retirement epic; conceptual
  whole-package decision recorded; runtime, dirty work and composition untouched.
