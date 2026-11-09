# Atlas slice D: fixture-only Miller-column preview

Status: done — independent reviewer **APPROVE** after review fix round 1. The
legacy web experience remains the default; the preview remains opt-in at
`?atlasPreview=1`.

## Goal

Add a polished, responsive and accessible Atlas exploration preview at `?atlasPreview=1`.
It uses the additive `@haas/atlas-explorer-client` exploration snapshot and navigation
APIs, with web-owned synthetic fixtures only. The preview does not create an HaaS
client, perform live research, persist data, or invoke actions. Existing Atlas UI
continues to work as before and links to the preview; the preview links back.

## Scope and acceptance

- Owned paths: `services/atlas-explorer/web/**` and this plan only.
- Two synthetic journeys: computers with adaptive facets and source-backed example
  details; fictional people joined across branches by a reused entity reference.
- Clearly mark all content synthetic/unverified; use reserved `example.test` URLs
  that are never fetched. Arbitrary search terms produce a truthful samples-only
  message rather than being mapped to a fixture.
- Render entity, facet, and source rows distinctly; expose claims, evidence,
  conflicts, relationships, and unsupported/research-needed states in an inspector.
- Selection opens a child column while retaining parent ancestry; visited branches
  remain switchable in memory through the client branch-navigation APIs.
- Keyboard navigation, live status, visible selection/focus, desktop horizontal
  rail, and narrow-screen one-column/back navigation. Opening columns does not
  programmatically move keyboard focus.
- External actions are non-executable and explain approval requirements.
- Component tests cover query behavior, branch/path navigation, target rendering,
  evidence/conflicts, and mobile/back controls. No test framework is added unless
  existing infrastructure proves insufficient.

## Baseline (measured before edits)

Working tree already had unrelated client contract files in progress. No web files
or this plan were modified at baseline. Commands run from repository root:

| Command | Result |
|---|---|
| `npm --prefix services/atlas-explorer/web run typecheck` | Fails with exactly 5 existing diagnostics, all in `src/components/atlas/AtlasExplorer.tsx`: unused `AtlasExplorerState`, unused `initialQuery`, missing `nodes` property on layer envelope, and two missing `DimensionKey` references. These legacy diagnostics are out of scope. |
| `npm --prefix services/atlas-explorer/web run bundle` | Pass; Vite 8.3.5 transformed 40 modules and emitted JS/CSS successfully. |

The web package has no React DOM test runner. Inspect package-owned tooling before
choosing a minimal test approach; do not use the linked client's `node_modules`.

## Implementation and verification

Keep all changes within the owned paths above. Use the client package's exported
`parseExplorationSnapshot`, `selectExplorationTarget`, and
`activateExplorationPath` APIs; do not duplicate contract or navigation logic.
Expected checks, run from repository root:

```sh
npm --prefix services/atlas-explorer/web run typecheck
npm --prefix services/atlas-explorer/web run bundle
npm --prefix services/atlas-explorer/web run test
```

Compare typecheck with the five-diagnostic baseline: no new diagnostics are allowed.
Browser validation is handled separately by the parent. Do not repair legacy
`AtlasExplorer.tsx` errors as part of this slice.

## Implementation evidence

- Added an opt-in preview route and entry/return links, isolated from the legacy
  app's HaaS client initialization. Web-owned fixture snapshots are parsed by the
  client contract and use the client's navigation/breadcrumb APIs.
- Added sample journeys for computers and fictional Avery Rowan/Northstar Lab/
  Morgan Vale, with reused entity references, adaptive facet states, sourced and
  conflicting claims, unverified `example.test` sources, and disabled external
  actions. Unknown free queries hide fixture rows and state that live research is
  unavailable.
- Added responsive horizontal desktop columns, mobile one-column/back navigation,
  selected ancestry, in-memory visited-path switching, keyboard row navigation,
  live status, and an evidence/relationship inspector.
- Baseline lacked web-owned DOM testing. Added Vitest, jsdom, Testing Library and
  jest-dom as web dev dependencies with a package-owned `test` script.
- `npm --prefix services/atlas-explorer/web run test`: 9 tests pass.
- `npm --prefix services/atlas-explorer/web run bundle`: passes; 118 modules
  transformed and production assets emitted.
- `npm --prefix services/atlas-explorer/web run typecheck`: still fails with the
  exact same five baseline diagnostics, all in untouched
  `src/components/atlas/AtlasExplorer.tsx`; no new preview diagnostics.
- `python .github/skills/core-boundary-check/scripts/check_boundaries.py
  --plugin atlas-explorer`: exit 1 with 154 existing core-reference findings and
  1 existing plugin-to-core warning. Findings are in core/legacy
  `web/marketplace`/plugin paths, not this slice's `services/.../web` files.
  No core files were touched. This checker scope was not run pre-edit; the prior
  epic's 175/9 full-check count is a different scope, so a numeric baseline delta
  is unavailable.
- No browser/viewport automation was run; parent handles browser validation.
- Install command used: `npm --prefix services/atlas-explorer/web install
  --save-dev vitest jsdom @testing-library/react`, then
  `npm --prefix services/atlas-explorer/web install --save-dev
  @testing-library/jest-dom`.

Budget override: necessary coherent fixture/UI/CSS/test implementation exceeds the
default ~400-line slice budget, as explicitly approved for slice D.

## Browser-feedback refinements

- Parent browser verification confirmed branch restoration and no document
  overflow at 1440/768/390px, plus working mobile back navigation.
- Visited-path buttons now expose full human-readable ancestry in visible labels,
  accessible names and titles; labels wrap instead of truncating parent context.
- Relationship source records now join claim sources in the inspector. Because
  relationships have no contract `facet_key`, web-owned fixture metadata explicitly
  scopes each connection to relevant facets. Organizations shows the fictional
  affiliation's source, while Biography does not inherit unrelated evidence.
  Relationship evidence is explicitly labeled as supporting that connection, not
  every facet claim.
- Fixed keyboard focus loss by keeping column keys stable during path changes.
  ArrowRight retains the active row's DOM/focus; ArrowLeft focuses the selected
  parent row. DOM integration tests assert `document.activeElement` via focus
  matchers rather than just checking ancestry.
- Added expandable HTML/CSS direct-connection threads with accessible textual
  relationship descriptions, incoming/outgoing direction, suggested/sourced
  distinctions, and entity-navigation buttons using the shared select API.
  Already-explored badges are based on selected entity IDs across visited paths,
  not names or occurrence IDs.
- Connection view is a fixture-only, scoped direct-neighbor demonstration. It
  performs no graph inference, live research, persistence, or authenticated
  adapter work; a shared organization never creates an inferred person-to-person
  edge. Milestone H is not implemented.
- Final refinement verification: web `run test` passes 9 tests in 2 files;
  `run bundle` passes; `run typecheck` retains exactly the five legacy errors.
  `git diff --check` passes. Atlas-specific boundary checker remains unchanged at
  154 errors/1 warning compared with the first slice-D checker invocation.

## Review fix round 1/2

- Reviewer finding (CHANGES_REQUESTED): child disclosure depended on the clicked
  occurrence's fixture descendants, so Avery › Collaborators › Morgan Vale and
  connection-button navigation showed no further steps despite known facets.
- Root-cause fix: fixture occurrences are now read-only templates keyed by canonical
  target identity (entity id, entity+facet key, or source id). Each column projects
  the template children of its selected target under the actual selected occurrence,
  retaining real ancestry. All selection still uses the client
  `selectExplorationTarget`/`activateExplorationPath` APIs; the earlier web-side
  occurrence-remapping and re-parsing workaround was removed. No client/core edits.
- Regression test drives the UI: Morgan Vale reached via Collaborators discloses
  Career and its claim; Northstar Lab reached via a connection button discloses
  Work and its source.
- Evidence: web `run test` 10 passed (previous 9 plus 1 regression); `run typecheck`
  exactly the five legacy `AtlasExplorer.tsx` errors; `run bundle` passes;
  `git diff --check` passes.

## Final review and completion evidence

- Independent reviewer verdict: **APPROVE**, following review fix round 1.
  The canonical-target template fix preserves the selected occurrence's actual
  ancestry: the Collaborators path reaches Morgan Career and its claim; the
  connection path reaches Northstar Work and its source. These are distinct
  occurrence paths to the same canonical templates; navigation remains owned
  by the client APIs.
- Parent re-ran web tests: **10 passed in 2 files**; bundle passed; typecheck
  retains exactly the same five untouched legacy errors. Parent also reports
  client build/typecheck and **113 tests passed**, and isolated service Python
  **19 tests**, Ruff and Pyright passed.
- Parent browser verification covered both corrected alternate paths, complete
  visited-path ancestry, evidence provenance, directed connection view,
  “Already explored” for the same entity reached by different paths, keyboard
  Down/Right with focus retained and Left to the parent, mobile Back, and no
  document overflow at 1440/768/390px.
- Global boundary counts remain **175 errors / 9 warnings**; Atlas-specific
  counts remain **154 / 1**. The parent reports no boundary delta.
- The preview is still fixture-only, synthetic, opt-in and not the default.
  Its connection view is a direct-neighbor demonstration, not milestone H's
  live relationship-thread capability. No live search, durable saving,
  external actions, core changes, production readiness, or deployment is
  claimed. No live adapter was authorized. Milestones E, F, live G/H, I and J
  remain blocked/not done; completing D does not complete the epic.
- Local API verification was limited to `me` and `capabilities` returning
  HTTP 200; no credentials or secrets are recorded here. The proposed local
  live adapter was explicitly declined.
- Evidence above is parent/reviewer-reported for this documentation update;
  this docs-only update did not rerun implementation checks.

Decisions needed: none for completed D. No new core capability was introduced,
so no ADR is required.
