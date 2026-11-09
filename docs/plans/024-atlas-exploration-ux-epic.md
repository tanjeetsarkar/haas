# 024 — Atlas knowledge exploration UX

- Status: in-progress
- Type: epic
- Owner agent: plugin-engineer

## Goal and ownership

Replace fixed generated layers with adaptive, source-backed Miller-column
exploration, saved branches, and inspectable cross-entity relationships.
All domain changes belong to the Atlas service, web/client, and plugin pack.
Core changes, if needed, require a separate generic extension-point plan/ADR.

## Ordered slices

| Slice | Deliverable | Dependencies | Status |
|---|---|---|---|
| [025](025-atlas-exploration-contract-foundation.md) | IA, additive types and synthetic fixtures | None | done — reviewer APPROVE |
| [026 / B](026-atlas-exploration-validation.md) | Runtime schema validation and TS/Python contract tests | 025 | done — reviewer APPROVE |
| [027 / C](027-atlas-exploration-navigation.md) | Graph/path/selection state | 025 | done — reviewer APPROVE |
| [028 / D](028-atlas-miller-column-preview.md) | Fixture-only accessible responsive columns and inspector | B, C | done — independent reviewer APPROVE after review fix round 1 |
| E | Public thread/run research and validated adaptive projection | B; [029](029-atlas-live-client-snapshots.md) | in-progress — source-pair/branch-history fix independently approved; current live research graph not yet validated |
| F | Explicit immutable artifact save/load | B; 029 | in-progress — browser save/list/download round trip reported under 034; full live security proof remains pending |
| G | Default live workspace, events, cancellation/retry/resume | D, E/F; [030](030-atlas-loopback-proxy.md), [031](031-atlas-live-workspace-parity-retirement.md) | in-progress — default live UI and local proxy verified by report; remaining live gates pending |
| H | Live relationship threads and expandable connection map | G; 031 | in-progress — live graph validation pending; D's fixture neighbors do not satisfy H |
| I | Supported contextual actions and capability parity | G; 031 | in-progress — facet fix independently approved; separate-approver proof not verified |
| J | Browser/accessibility verification, default cutover and legacy retirement | H, I; 031 | in-progress — default cutover and conditional service-UI retirement reported; final live verification pending |

## Completed slices and next scope

025, B, C and D are complete with independent reviewer `APPROVE`. D's first
review requested a fix; round 1 corrected canonical-target child disclosure
while preserving distinct occurrence ancestry, and the reviewer then approved.
For D, the parent reports web tests **10 passed in 2 files**, bundle passed,
and typecheck with the exact five unchanged legacy diagnostics. Parent browser
verification covered corrected Career/Work alternate paths, ancestry, evidence
provenance, directed fixture connections, repeat-entity path labeling, keyboard
focus/navigation, mobile Back, and no overflow at 1440/768/390px. Parent also
reports client build/typecheck and 113 tests passed; service Python 19 tests,
Ruff and Pyright passed. Global boundaries remain 175 errors / 9 warnings;
Atlas remains 154 / 1. These are supplied parent/reviewer results, not checks
rerun by this docs update.

D remains complete as a **fixture-only, opt-in preview**. Its direct-neighbor
demonstrations do not establish live relationship support. No implementation
completion, durable save, deployment, or production readiness is inferred from
the new approvals or in-flight work. An in-memory branch is not persistence;
schema URL checks are not SSRF enforcement. Do not edit completed plan 028.

### Approved replacement route — 2026-11-09

The user now approves full Atlas replacement using **existing authenticated HaaS
public APIs** for live research and explicit snapshot saves. This supersedes the
earlier local-adapter decline. It is not approval of a standalone production
authentication gateway or of plan 008's trusted service invocation design.
The independently deployable production Atlas service remains separate; the
browser/client route does not need to impersonate that service.

Implementation is split into 029 (agentclient: public client/projection/snapshots),
030 (proxyagent: local development proxy), and 031 (UI: workspace/parity/retirement).
These labels identify current workstreams; their repository owner is
`plugin-engineer`. No two writers share implementation files.
029 and 030 may proceed independently; 031 integrates their contracts and records
the final verification/cutover evidence, so a separate 032 epic is unnecessary.

Local live tests are authorized within small run budgets and existing `max_cost`
limits. Credentials from `.env.haas` stay server-side; never record their values.
Production static hosting requires actual operator-configured authenticated
gateway routing. Without an authenticated backend, live mode fails closed,
without switching to fixtures or reporting success.

### Parent-reported progress — 2026-11-09

The client source-pair/branch-history fix and the 031 facet fix received
independent `APPROVE`; the local proxy review also approved (78 tests), with an
actual authenticated `/v1/me` returning 200. Latest client evidence is 158 tests
with build/typecheck passing (parent reran 157 before the final budget fix);
latest web evidence is 120 tests with typecheck/build passing (parent reran 118
before the latest fix). The legacy service UI was removed after conditional
parity work and tests, while the new live workspace is the default.

The parent reports the live-research run is currently running after restoring
the 0.15/0.10 budgets following a real policy 403; the graph is not yet validated.
Separate-approver proof and remaining live authorization assertions are
**not verified**, so E–J and this epic remain in progress. Core database
tables/migrations, generic SDKs, and core marketplace `web/` remain unchanged.
Reported tests/builds/reviews were not rerun by this documentation-only update.

## Acceptance, risks and rollback

Computer-type and person/facet journeys must preserve selected ancestry and saved
sibling branches. Repeated entities retain identity without inventing relationships.
Claims and connections expose sources, uncertainty, and conflicts. External actions
preserve authorization and approval. Cross-tenant access is denied.

Keep the legacy service UI available until the replacement demonstrates parity
for supported comparisons, pricing, specialists, checklists and approval workflows.
Then remove `services/atlas-explorer/web/src/components/atlas/` and proven-unused
private client paths as explicitly authorized. No user-visible supported feature
may disappear. Legacy public v1 client exports remain until usage is proven absent
and separate breaking-change approval exists; broad deletion is not implied.
Avoid compatibility facade copies created merely to preserve old implementation.

Core marketplace `web/`, generic SDKs, database tables/migrations and plan 013 are
unchanged. No new core capability or ADR is proposed. Plan 008 remains separately
gated for consumers that actually require trusted plugin-service invocation.
Snapshot retention, tenant authorization and artifact ownership remain core-owned;
each explicit save creates a new immutable artifact, not an overwrite needing
revision-conflict handling. Domain revision is not orchestration history version.

Each child plan records baseline, budgets and rollback. Before retirement, rollback
can restore the legacy service entry; afterward restore the prior service source
and entry together without deleting saved artifacts. This epic authorizes neither
core changes nor production gateway implementation/deployment.
