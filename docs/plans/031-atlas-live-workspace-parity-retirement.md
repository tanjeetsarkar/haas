# 031 — Atlas live workspace, capability parity and legacy retirement

- Status: in-progress — default live UI and conditional service-UI retirement reported; live proof pending
- Type: bounded epic, including final verification/cutover
- Owner agent: plugin-engineer
- Workstream: UI
- Epic: [024](024-atlas-exploration-ux-epic.md), milestones G/H/I/J
- Dependencies: [029](029-atlas-live-client-snapshots.md), [030](030-atlas-loopback-proxy.md)
- ADR: none — service-local product replacement, no core changes

## Goal and boundary

Make a generic live graph workspace the default Atlas service UI, with clearly
isolated sample mode. Preserve every supported comparison, pricing, specialist,
checklist and approval workflow before retiring the legacy service UI and
proven-unused private client paths. The user approves this conditional replacement,
not deletion of core marketplace UI, generic SDKs, public v1 exports or data.

All implementation is extension-owned: deleting Atlas would remove these views,
presentation adapters and domain interactions. Core identity, orchestration,
authorization, marketplace and storage remain unchanged.

## Scope, files and writer allocation

**Allowed:** `services/atlas-explorer/web/src/**`, adjacent web tests, service-web
test configuration when coordinated with 030, and service-web instructions.

**Planned files:**
- `web/src/App.tsx`, `App.css`, `main.tsx` — default live entry and deliberate sample entry.
- `web/src/explorationAdapter.ts` — UI-owned interface to the additive 029 client,
  honest status/error mapping; no credentials or private transport.
- `web/src/graphProjection.ts`, `components/AtlasGraphColumn.tsx` — generic graph
  rows, scoped branching and inspector, not hardcoded occurrence fixture cases.
- New focused workspace/parity components under `web/src/components/` —
  comparisons, pricing, specialists, checklists, draft/approval review and live connections.
- `web/src/previewFixtures.ts`, existing preview components — isolated sample-only use.
- `web/src/components/atlas/**` — authorized deletion **only after** parity evidence.
- Adjacent workspace/projection/parity/approval tests — actual paths recorded per milestone.

Client-ts is owned by agentclient (029); Vite config/proxy by proxyagent (030).
Send unused-private-client inventory to 029's writer, who removes only proven-unused
private paths after the parity gate and reruns client tests. Do not race-edit them.
No new facade copies simply wrapping retired component behavior.

**Excluded:** core `web/`, SDK packages, app/migrations/DB tables, governance/session
plans, completed 028, production auth gateway, plan 013 and public v1 export removal.

## Ordered bounded milestones

| Milestone | Deliverable | Dependency | Budget |
|---|---|---|---|
| 031a | Generic live workspace wiring, isolated sample mode and honest states | 029 contract; 030 for local access | ≤400 lines |
| 031b | Supported comparison/pricing/specialist parity | 031a | ≤400 |
| 031c | Checklist/draft/approval workflows and permission-denial states | 031a; 029 approval path | ≤400 |
| 031d | Live relationship threads/map, responsive/keyboard/accessibility coverage | 031a | ≤400 |
| 031e | Parity inventory, full verification, conditional legacy deletion and default cutover | 031b–d; 029/030 verified | ≤400, or explicit deletion override |

Implementations are in flight, not verified completion. Each milestone must remain
independently runnable with supported behavior available. A coherent milestone may
exceed 400 lines only with a recorded `Budget override: <reason>` and measured size;
split implementation/test checkpoints rather than authorize a blind massive rewrite.
For 031e, a large obsolete-tree deletion may justify that override after parity,
but does not justify unrelated cleanup or public export removal.

## Acceptance and parity evidence

- [x] App entry defaults to the new live workspace (parent reports current default
  entry is the new live UI).
  Sample mode is explicit/labeled and isolated; sample fixtures are never silently
  used after backend failure, presented as live evidence or saved as live results.
- [ ] Columns are derived from validated entities/targets/occurrences and arbitrary
  supported facets, not fixed fixture IDs or enumerated synthetic occurrence cases.
  Root/child research uses 029; switching siblings restores retained state without
  spending on a new run. Inspector exposes provenance, conflicts and uncertainty.
- [ ] Show request/run progress, partial findings, budget/auth/network errors,
  cancellation and explicit retry/resume. Do not label stream disconnect as remote
  cancellation or rejected operations as success. Stale results never replace new selection.
- [ ] Explicit Save acknowledges a real new artifact; Load restores validated graph,
  branches and selection. Unsaved is visible until success. No local fake persistence,
  implicit overwrite/conflict dialog or invented global saved-session catalog.
- [ ] Produce a parity matrix from the legacy component tree and its reachable entry
  points **before deletion**: each supported behavior maps to a replacement component,
  test and browser assertion, with no unmapped supported feature.
- [ ] Comparisons preserve supported attribute selection, side-by-side values and
  available filtering/sorting/selection behavior discovered in that inventory.
  Pricing preserves supported sourced figures, units/currency/qualifiers and interactions;
  absence of evidence is not a zero price or invented quote.
- [ ] Specialists preserve supported details, selection and contextual interactions;
  checklists preserve supported steps, required/completed state and next actions.
  Capture the actual old affordances; these examples are not a narrower parity definition.
- [ ] Approval review retains supported draft inspection/editing and submission flow,
  destination, exact arguments/digest, approve/reject and authoritative resulting state.
  Edits invalidate stale approval as required by the existing contract; a generated action
  descriptor or logged-in principal cannot authorize execution.
- [ ] Test approval-required, pending, approved, rejected, stale/digest mismatch,
  unauthorized and self-approval/separate-authorizer denial states. UI never hides a 403
  by changing auth routing or silently using a stronger credential.
- [ ] Live relationships include labeled directed threads, expandable map and textual
  equivalent, evidence inspection and repeat-entity ancestry; no inferred direct edge
  from multi-hop connection. Fixture-only neighbors do not satisfy this criterion.
- [ ] Keyboard focus/traversal, announced loading/errors, mobile Back, repeat-entity
  path labeling and no overflow pass at 1440/768/390px. Computer-type and person/facet
  journeys cover root, branch, sibling restore, source inspection and save/load.
- [ ] No authenticated backend yields an actionable closed error, not sample fallback.
  Production build needs operator-configured authenticated gateway routing; local
  Vite proxy success is not a production deployment claim.
- [x] Only after mapped parity tests/browser checks pass, remove
  `services/atlas-explorer/web/src/components/atlas/`, its dead imports/styles/tests
  and proven-unused private paths. Parent reports this service-local UI was removed
  after conditional parity work and tests; this does not assert that every remaining
  live security/authorization gate below has passed. Parent reports the latest
  web typecheck passes after the removal; the five legacy TS diagnostics were not
  suppressed or addressed through unrelated core changes.
- [ ] Public legacy v1 client exports remain. Unused-export searches alone cannot
  authorize a public breaking change; require separate approval even if no local users.
- [ ] No supported capability lost, no new failures, no boundary increase, independent
  reviewer APPROVE and verifier evidence before marking 031/024 complete.

## Baseline and verification/cutover gate

Supplied parent/reviewer baseline, **not rerun in this docs update**:
A–D independently APPROVE; client build/typecheck and 113 tests pass; Python 19,
Ruff/Pyright pass; web 10 tests in two files and bundle pass; web typecheck has
exactly five legacy diagnostics. Boundaries global 175 errors/9 warnings,
Atlas 154/1. Record actual dirty-tree baseline before each implementation checkpoint;
do not erase other in-flight writers' changes.

Run from `services/atlas-explorer/web`: `npm test`, `npm run typecheck`,
`npm run bundle`, and after retirement `npm run build` (must pass without those
five diagnostics). Run full 029 client tests/build/typecheck and existing service
Python tests (`.venv/bin/pytest services/atlas-explorer/tests` from root with its
documented service environment; record exact isolation command if required).
Run touched Python Ruff/Pyright only if Python actually changed; this plan does not
authorize Python edits. Repeat root boundary scanner:
`python .github/skills/core-boundary-check/scripts/check_boundaries.py`;
target no increase from 175/9 globally or 154/1 Atlas.

Verifier records redacted live public-path evidence for root/branch research,
actual server history versions, remote cancellation, separate-authorizer denial
and authorized decision, artifact round-trip/new-ID-per-save and real cross-tenant
denials (029). Local tests may use small existing `max_cost` budgets; stop on budget
failure instead of increasing spend automatically. If principals/approver/backend
are unavailable, report the relevant gate blocked, never invent tenant claims.

Verify 030's loopback/remote/CSRF tests and secret-free build/preview evidence.
Browser-check the parity matrix and responsive journeys, then repeat after deletion.
Record before/after feature inventory, diagnostic counts, deletion inventory, tests
and reviewer verdict in this plan. No separate 032 file is needed for these gates.
Production deployment remains unclaimed until actual operator gateway configuration
and deployment smoke tests exist; it does not block verifying the approved local route.

### Parent-reported progress — 2026-11-09

The new live workspace is the default, and the legacy service UI was removed only
after conditional parity work and tests. The 031 facet fix received independent
`APPROVE`. Latest parent-reported web evidence is 120 tests with typecheck/build
passing; the parent reran 118 tests before the latest fix. Latest engineer-reported
client evidence is 158 tests with build/typecheck passing; the parent reran 157
before the final budget fix. These are reports, not commands rerun in this
documentation update.

The legacy service UI removal is service-local. Core marketplace `web/`, generic
SDKs, and core database tables/migrations remain unchanged. The live research run
after restoring the 0.15/0.10 budgets has not yet had its graph validated.
Separate-approver proof and any remaining live authorization assertions are
explicitly **not verified**; 031 and epic 024 remain in progress pending the
applicable live proof and final verification.

## Risks, unknowns and rollback

- Exact legacy interaction inventory is not fully enumerated by this docs pass;
  031e must capture it before any deletion. Missing support blocks retirement,
  not permission to redefine parity.
- Adapter/client response shapes must converge with 029's public types. Extend the
  extension contract minimally; do not introduce a second incompatible model or
  unnecessary compatibility facade copy.
- Early deletion could hide the five diagnostics while dropping behavior → require
  capability evidence first, then rerun the complete build/tests after deletion.
- Before retirement rollback restores legacy service entry while preserving new files.
  After retirement restore prior service component tree and entry together from the
  recorded source state; no core/table changes and no snapshot deletion.
  Never reset others' work or modify shared history to perform rollback.

## Decisions needed

None for approved replacement and conditional service-local retirement. Public SDK
breaking changes, loss of a supported feature or production gateway implementation
require separate user approval and are not implicit in this plan.

## Log

- 2026-11-09 architect: recorded in-flight live UI ownership, full parity requirements,
  authorized conditional deletion and integrated verification/cutover gates.
- 2026-11-09 architect: recorded parent-reported default-live cutover, conditional
  legacy service UI retirement, latest test/build/typecheck counts, and independent
  approval of the facet fix. Core marketplace, generic SDKs and core tables remain
  unchanged. Live research graph validation and separate-approver proof remain open.
