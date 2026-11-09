# 010 — Extract the Atlas TypeScript client and types

- Status: in-progress
- Type: epic — three bounded, ordered slices; 010a implementation approved
- Owner agent: haas-lead (coordination); plugin-engineer for 010a/b, client-engineer for 010c
- Epic: [001](001-atlas-extraction-sdk-epic.md)
- ADR: [ADR-18](../adr/0018-domain-client-package-ownership.md) — accepted

## Goal

Move the existing Atlas session facade and domain types from `haas_sdk_ts`
into `services/atlas-explorer/client-ts`, then remove those APIs from
`@haas/sdk` in a coordinated breaking cutover. Preserve every non-Atlas public
SDK API. Verify the service-owned package and generic SDK independently without
starting Atlas, core, a database, or an authenticated service transport.

## Authority and compatibility

The user explicitly confirmed removal of the earlier dirty change in
`haas_sdk_ts/src/lib/atlasClient.ts` and lifted its protection on 2026-11-09.
That path may be inspected and changed by the implementation slice below.
This supersedes only that file restriction in 001/004, not their completed
test evidence or unrelated worktree protections. Capture a fresh baseline;
never restore an old revision over current work.

The user selected **coordinated breaking compatibility**: no deprecation shim,
core re-export, forwarding package, or permanent duplicate domain types after
010c. Temporary coexistence in 010a/b is deliberate and bounded.
`@haas/sdk` currently declares `0.1.0`; this is not evidence of a repository
breaking-version convention. The user accepted ADR-18 and selected
`@haas/sdk` `1.0.0` with `@haas/atlas-explorer-client` `0.1.0`, final peer
range `^1.0.0`. Gate G1 is resolved. For 010a/b, use transitional peer range
`^0.1.0 || ^1.0.0` so the package can be verified against the current SDK;
narrow it to `^1.0.0` during 010c. No `npm publish`, tag, release, or remote
registry action is authorized.

## Boundary classification

| Change | Owner | Would it exist without Atlas? |
|---|---|---|
| Domain session manager, factory and five domain types | Plugin service | No — `services/atlas-explorer/client-ts` |
| Generic client APIs/types and their regression tests | Client/core | Yes — plugins can use runs/threads/SSE; web-research can also compose a client on these contracts |
| Remove domain exports/source from core | Client/core cleanup | Yes — a plugin-agnostic SDK boundary remains necessary without Atlas; ADR-18 |
| Client package/migration documentation | Plugin service | No — keep domain instructions beside the owning client |

No missing core capability is needed for this move. Dependency direction is
service client → public `@haas/sdk` only; no `app.*`, SDK source/deep imports,
core→service edge, or new plugin-specific behavior in core.

## Inspected evidence and dependencies

- 002 supplies a health-only service; its README explicitly denies business
  routes, authentication, storage and domain readiness. Do not change that claim.
- 004 is completed: reported 10 TypeScript tests/build passed; ESLint lacked
  configuration. These are historical results, not a fresh baseline.
- Current `atlasClient.ts` is 273 lines and imports `../client` and `../types`.
  It defines its own five domain types, also declared in `src/types.ts`.
  Root `src/index.ts` exports domain types from `types.ts` but runtime facade
  from `lib/atlasClient.ts`. Reconcile duplicate declarations before deleting.
- Runtime surface: `AtlasSessionManager`, `createAtlasSessionManager`.
  Type surface: `DrilldownRequest`, `AtlasLayerNode`, `AtlasLayerEnvelope`,
  `AtlasRunEvent`, `AtlasEventHandler`.
- Methods currently create/branch HaaS threads, submit runs, wrap SSE, delegate
  resume/status/cancel. The source comment about a published agent revision is
  not proof that such a revision is submitted.
- Core package uses TypeScript 5 / Vitest 1, `build: tsc`, `test: vitest run`;
  root export only, `dist/index.js` and `dist/index.d.ts`. Existing build has
  `rootDir: src`, so tests are not included in package declaration checking.
- Core contract test explicitly expects the two Atlas runtime exports.
  Other REST/configuration/error/SSE tests must remain intact.
- Plan 008 / ADR-17 is **not a dependency for relocation**. It remains a
  prerequisite for a future authenticated service consumer, not authorized here.
- G2's full local TS/JS and npm-manifest search was completed on 2026-11-09.
  No application imports, type imports, or deep imports of the seven Atlas
  names from `@haas/sdk` or `haas_sdk_ts/src/lib/atlasClient` remain.
  `web/marketplace/src` imports the domain facade/types from
  `@haas/atlas-explorer-client`; `web/marketplace/tests/atlas-imports.contract.ts`
  also imports that package. The only remaining core-side consumer reference is
  the string-based export assertion in `haas_sdk_ts/tests/sdk.contract.test.ts`.
  The core SDK source and root barrel still define/export the names intentionally
  pending 010c. This inventory does not complete the UI relocation: 010c remains
  gated on 012k service UI build/artifact evidence and coordinated cutover checks.

## Gates and documented defaults

**G1 — package identity/version agreement (resolved 2026-11-09).** Package:
`@haas/atlas-explorer-client`, `private: true` for local verification. Versions:
core `@haas/sdk` `1.0.0`, Atlas client `0.1.0`; final peer range `^1.0.0`.
010a/b use transitional range `^0.1.0 || ^1.0.0`; 010c narrows it. No
publication is authorized.

**G2 — consumer cutover (before 010c).** Search only TS/JS imports and dependency
manifests for the seven public names and `lib/atlasClient` deep imports. Record
paths/import specifiers, not unrelated diff contents. If runtime consumers
outside the allowed paths need migration (including `web/`), stop and request a
bounded consumer slice/owner before removal. Do not widen this plan or silently
leave broken consumers. Export removal approval is already given; do not ask
again for a shim policy.

**G3 — independent tooling (before implementation verification).** The new
package must declare its own TypeScript/Vitest/typecheck tooling, without
borrowing core `node_modules`, using source aliases, or changing core tooling.
Provision its dependencies with maintainer approval if unavailable. Record
package manager/lockfile convention; default to npm and a service-local lock
if none exists. Missing dependencies are BLOCKED, never a pass.

## Scope and ordered slices

Each slice is independently verifiable and ≤400 changed non-generated lines,
zero migrations. Count removed and added lines separately, including tests/docs;
do not hide copied runtime behind the generated-file exclusion. If the measured
diff exceeds budget, stop and re-plan before implementation; do not omit tests.
One writer/owner per slice. No core extension point is required first.

| ID | Owner | Depends on | Budget target | Status |
|---|---|---|---|---|
| 010a — service package and canonical domain types | plugin-engineer | ADR-18 accepted, G1/G3 | ≤350 lines | done |
| 010b — relocate facade behavior into service package | plugin-engineer | 010a | ≤400 lines, including ~200–230 relocated runtime lines after types/import cleanup | done |
| 010c — coordinated core export/source removal | client-engineer | 010b, G2, explicit versions, 011 | ≤400 lines, including 273-line facade deletion and ~68-line type deletion | proposed |

### 010a — full package/type slice

**Allowed paths:** `services/atlas-explorer/client-ts/{package.json,package-lock.json,tsconfig.json,tsconfig.tests.json,README.md}`,
`services/atlas-explorer/client-ts/src/{index.ts,types.ts}`,
`services/atlas-explorer/client-ts/tests/types.contract.test.ts`, this plan's evidence.

Create a standalone package, root export map/types, scripts `build`, `test`,
`typecheck` (strict `tsc --noEmit -p tsconfig.tests.json`), declaration output and
own dev dependencies. Use current core TS/Vitest major versions; no production
dependency except a peer on public `@haas/sdk`. No workspace setup or core
manifest/tooling changes. Document local packed-SDK setup.

Move the five domain types into a **single** `src/types.ts`; use type-only
public SDK imports where appropriate. Compare both old declarations; preserve
assignability, callback shapes and optional fields, not merely their names.
Keep unrelated generic types (including declarations after the Atlas block)
in core. If duplicates disagree semantically, stop for a decision.

Tests/typecheck import all five types through the service package entry,
construct representative layer/drilldown/event/callback values, and reject
invalid schema/component/trust-tier values using checked negative assertions.
These fixtures prove TypeScript contracts, not server validation/production.

**Acceptance:** service test/build/strict test typecheck pass with a locally
packed current public SDK; generic SDK test/build baseline unchanged; core
source untouched; new package contains no core-relative imports. Boundary
counts unchanged, because the legacy implementation remains until cutover.

### 010b — full facade relocation slice

**Allowed paths:** `services/atlas-explorer/client-ts/src/{index.ts,atlasClient.ts}`,
`services/atlas-explorer/client-ts/tests/atlasClient.test.ts`,
`services/atlas-explorer/client-ts/README.md`, this plan's evidence.

Copy the current implementation into the owning package, replace imports with
public `@haas/sdk` imports plus canonical local domain types, remove duplicate
type declarations and unused imports. Export the same manager/factory names.
Do not redesign constructor, method signatures, runtime behavior or defaults.
Keep core copy untouched until 010c. Comments/docs must describe a legacy
HaaS orchestration facade, not an implemented Atlas HTTP service client.

Use stubbed public client methods/controlled async generators and fake timers:

1. Factory/constructor and root exploration: same thread metadata, prompt,
   preload skills, budgets (6 steps / $0.15 / 12 tools), return IDs/unsubscribe.
2. Drilldown: parent thread/exact history version, branch-result thread, prompt
   and budgets (5 steps / $0.10 / 8 tools), return shape.
3. SSE: initial cursor `'0'`, `outputDeltas: true`, cursor retention on retry,
   callback mapping including `sequence: 0`, exact existing terminal spellings
   (`run_completed`, `run_error`, `run_cancelled`, `stream_closed`).
   Characterize unsubscribe preventing later delivery/retry; do not claim it
   aborts an in-flight network operation. Exercise a controlled failure/retry;
   drain generators/timers and restore spies to avoid hanging tests.
4. Resume/status/cancel delegate unchanged, including `batch_digest` and
   rejection reason. No actual approval/authentication success is inferred.

**Acceptance:** both runtime exports plus all five type exports are present;
tests/build/typecheck pass without any server. Generic package test/build and
boundary counts remain baseline-equivalent. No network request to Atlas is
implemented or tested as real. Any observed mismatch with durable event schemas,
reconnection design or agent revision behavior is recorded as legacy debt,
not fixed or described as conformant in this move.

### 010c — full core cleanup/cutover slice

**Allowed paths:** `haas_sdk_ts/src/{index.ts,types.ts,lib/atlasClient.ts}`,
`haas_sdk_ts/tests/sdk.contract.test.ts`, `haas_sdk_ts/package.json`,
`services/atlas-explorer/client-ts/{package.json,package-lock.json,README.md}`,
this plan's evidence. No other consumer or runtime edits.

Delete core `lib/atlasClient.ts`, only the five Atlas declarations in
`types.ts`, and their root exports. Update the existing runtime-export
expectation to the preserved generic names, without weakening any unrelated
test. Apply the agreed core version bump and final service peer range.
Keep every other root export, public signature, package entry mapping, generic
type and behavior unchanged. No SDK runtime dependency is added.

README supplies old→new imports for all seven names, caller-created
`HaaSClient` injection, the exact version pair, coordinated installation/local
verification, and an explicit health-only/auth-deferred warning. No shim,
service URL/env option, route, web migration or release publication.

**Acceptance:** fresh core output and declarations contain no Atlas client or
type residue; negative type assertions prove all seven names are unavailable
from the packed core root, while service-root imports compile. Snapshot the
pre-slice public export names and compare final declarations: exactly those
seven names removed, no other public API loss. Service tests/build/typecheck
pass against the **post-cutover** packed SDK, not a stale installed version.
Core focused/full test/build pass without the service package installed.
Boundary findings for SDK source decrease; every finding on the deleted file/
declarations disappears, no new errors/warnings, unrelated findings unchanged.
Do not require a repository-wide zero: this is not the whole Atlas extraction.

## Verification rungs and exact commands

Run from the repository root unless shown otherwise. Record fresh results
before and after each slice; no results below are claimed as executed.

**Rung 0 — worktree/boundary baseline (verifier):**

```sh
git status --short
git diff --stat -- haas_sdk_ts services/atlas-explorer/client-ts docs/plans docs/adr
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas-explorer
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py
```

Nonzero boundary exits are expected for existing debt; save complete findings
and compare path/rule deltas, not only totals. Historical epic baseline was
134 errors/1 warning filtered, 155/9 overall; current totals are **unknown**.
Preserve all unrelated worktree changes. If an overlapping path becomes dirty
again, agree how to preserve its content before editing; no reset/stash/format.

**Rungs 1–3 — generic SDK tests/build; new package tests/build/typecheck:**

```sh
(cd haas_sdk_ts && ./node_modules/.bin/vitest run tests/sdk.contract.test.ts)
npm --prefix haas_sdk_ts run test
npm --prefix haas_sdk_ts run build
npm --prefix haas_sdk_ts run lint
npm --prefix services/atlas-explorer/client-ts run test
npm --prefix services/atlas-explorer/client-ts run build
npm --prefix services/atlas-explorer/client-ts run typecheck
```

The last three apply after 010a/tool provisioning. Lint's known missing-config
failure is baseline debt, not permission to repair tooling. Tests and build
must actually run; no new failures are acceptable. Python checks/service
container boots are not substitutes for these TS gates; no Python behavior is
touched, so database/auth tests are not part of this bounded slice.

**Rung 4 — clean packed-core integration, no registry publication:**

Existing `tsc` does not clean `dist`; never pack stale generated Atlas files.
Create a disposable output directory rather than deleting user build output:

```sh
ARTIFACT_DIR="$(mktemp -d)"
mkdir -p "$ARTIFACT_DIR/core"
cp haas_sdk_ts/package.json "$ARTIFACT_DIR/core/package.json"
(cd haas_sdk_ts && ./node_modules/.bin/tsc --outDir "$ARTIFACT_DIR/core/dist")
npm --prefix "$ARTIFACT_DIR/core" pack --pack-destination "$ARTIFACT_DIR"
```

Set `CORE_TARBALL` to the exact absolute `.tgz` path printed by that command
(not an assumed version/filename), then:

```sh
npm --prefix services/atlas-explorer/client-ts install --no-save --package-lock=false "$CORE_TARBALL"
npm --prefix services/atlas-explorer/client-ts ls @haas/sdk
npm --prefix services/atlas-explorer/client-ts run test
npm --prefix services/atlas-explorer/client-ts run build
npm --prefix services/atlas-explorer/client-ts run typecheck
```

Dependency installation needs G3 satisfied; preserve any existing dependency
state and do not rewrite tracked manifests via the local tarball. Verifier
compiles a temporary consumer fixture importing all seven service-root names,
all prior non-Atlas core-root names, and (010c only) seven checked
`@ts-expect-error` core imports. Use the service's strict test compiler and
installed public package; do not import `haas_sdk_ts/src`. Inspect tar contents
with `tar -tzf "$CORE_TARBALL"`; at 010c scan the clean emitted output:

```sh
rg -n 'Atlas|DrilldownRequest|atlasClient' "$ARTIFACT_DIR/core/dist"
rg -n 'Atlas|DrilldownRequest|atlasClient' haas_sdk_ts/src haas_sdk_ts/tests
rg -n 'haas_sdk_ts|app/|@haas/sdk/' services/atlas-explorer/client-ts/src
git diff --check -- docs/plans docs/adr haas_sdk_ts services/atlas-explorer/client-ts
```

Expected scans: no matches (exit 1) at final cutover; record matches instead of
silently filtering. Negative assertions live in the disposable fixture, not as
new domain names in core tests. Test/build public entry import resolution too;
if existing module packaging prevents packed usage, stop and plan a generic
packaging fix, rather than broaden this slice or hide it with TS path aliases.

## Baseline and completion record

Fresh pre-implementation baseline (2026-11-09):

```text
npm --prefix haas_sdk_ts test: 10 passed
npm --prefix haas_sdk_ts run build: passed
npm --prefix haas_sdk_ts run lint: exit 2; ESLint configuration is absent
check_boundaries.py --plugin atlas-explorer: 134 errors, 1 warning
check_boundaries.py --plugin atlas: 0 errors, 0 warnings
check_boundaries.py: 155 errors, 9 warnings
```

The service package does not yet exist; it has no preexisting test baseline.
Per slice: measured line count; commands/counts; baseline→after findings;
public-export comparison; packed dependency/version evidence; reviewer verdict.

## Risks and rollback

- Duplicate domain definitions or stale generated output → compare definitions,
  canonicalize once in service, verify clean packed output independently.
- Legacy SSE assertions differ from producers → preserve and disclose behavior;
  no claim of real event/schema compatibility or server implementation.
- Unknown downstream imports/version convention → G1/G2 stop cutover, not scope creep.
- Three slices may strain budget → measure before editing, stop/re-plan if >400.
- Rollback: propose reverting only this work's commits in reverse order, restoring
  core exports before removing the service package. Never reset/stash unrelated
  work or publish a rollback release. No data migration/destructive action.

## Decisions needed

1. G2 consumer inventory is complete; 010c still requires 012k service UI
   relocation evidence and coordinated public-package cutover review. Re-run
   the consumer search immediately before removal and do not remove exports
   while a consumer remains.

## Log

- 2026-11-09 architect: proposed bounded three-slice extraction; recorded lifted
  file restriction and approved breaking policy; versions/tooling/consumer gates
  remain explicit. Runtime/tests untouched.
- 2026-11-09 user: accepted ADR-18, selected
  `@haas/atlas-explorer-client` private package, and selected `@haas/sdk` `1.0.0`
  with Atlas client `0.1.0` and final peer range `^1.0.0`; no publication.
- 2026-11-09 lead: recorded fresh TypeScript baseline (10 tests/build pass,
  lint unavailable due missing ESLint config; boundaries atlas-explorer 134/1,
  atlas 0/0, overall 155/9). Found `web/marketplace/src` consumer imports;
  final export removal is gated on their migration.
- 2026-11-09 lead: ADR-18 accepted; package/version gates resolved. Started
  010a, limited to the independent package manifest, canonical domain types,
  and type contract tests. No facade move or core export removal in 010a.
- 2026-11-09 integration: 010a completed at 271 changed non-generated lines.
  New package type contract test 1/1, build and strict typecheck passed; current
  core SDK test 10/10 and build passed. Reviewer approved with no findings.
  Filtered boundary count 134/1 matched baseline; overall count 155/9. Five
  npm audit advisories were reported during install and remain untriaged.
- 2026-11-09 lead: began 010b after a fresh baseline: Atlas client package test
  1/1, build/typecheck passed; generic SDK test 10/10 and build passed. Scoped
  worktree check confirms no staged/tracked changes to the old facade, exports,
  or types; the pre-existing untracked core contract test is preserved.
- 2026-11-09 integration: 010b passed package test (10/10), build and strict
  typecheck; core SDK test (10/10) and build remained green. Expanded SSE tests
  after reviewer feedback for all terminal spellings and unsubscribe during
  retry backoff. Reviewer approved with no remaining findings. Atlas-explorer
  boundary count remained 134 errors/1 warning; marketplace build still shows
  the same five pre-existing TS diagnostics. Plan 011 is required before 010c.
- 2026-11-09 lead: completed G2 inventory across repository TS/JS imports and
  npm manifests. No application consumer imports Atlas APIs from the generic
  SDK or its deep source path; marketplace imports the extracted client package.
  Remaining core-side test reference is the explicit export-presence assertion
  in `haas_sdk_ts/tests/sdk.contract.test.ts`. Fresh verification passed:
  generic SDK test 10/10 and build; extracted Atlas client tests 10/10, build,
  and strict typecheck. 010c still waits for 012k UI relocation evidence and
  coordinated cutover review.
