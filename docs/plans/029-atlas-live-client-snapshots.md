# 029 — Atlas live public-API client and immutable snapshots

- Status: in-progress — implementation review approved; live public-API validation pending
- Type: bounded epic
- Owner agent: plugin-engineer
- Workstream: agentclient
- Epic: [024](024-atlas-exploration-ux-epic.md), milestones E/F and client support for G/I
- ADR: none — existing public contracts only, no core changes

## Goal

Deliver root and scoped-branch live research through authenticated public HaaS
thread/run APIs, projecting validated graph results into exploration state.
Provide real cancellation/approval integration and explicit immutable artifact
save/load, without fake persistence or requiring trusted service invocation.
This route is user-approved and supersedes the earlier local-adapter decline.

## Boundary classification

| Change | Ownership | Boundary test |
|---|---|---|
| Research prompt, graph validation/projection and exploration persistence format | Atlas extension client | Would not exist without Atlas |
| Thread/run/history, approval and artifact transport consumption | Atlas extension client using public SDK/REST | Domain orchestration belongs here; existing generic APIs stay unchanged |
| Identity, authorization, artifact retention/storage | Existing core, unchanged | Exists without Atlas; no implementation work authorized here |

## Scope and writer allocation

**Allowed:** `services/atlas-explorer/client-ts/src/**` and
`services/atlas-explorer/client-ts/tests/**` only; package metadata only if needed
for existing exports/test commands, no new dependency without explicit justification.

**Planned files:**
- `src/atlasClient.ts` — additive live research/save/load operations using public contracts.
- `src/explorationResearch.ts` — validated result schema, root/branch context,
  provenance and projection; in-flight file, not completion evidence.
- `src/explorationTypes.ts`, `src/index.ts` — minimal additive client contracts/exports.
- `tests/explorationResearch.test.ts`, `tests/explorationLive.test.ts`,
  `tests/explorationSnapshots.test.ts` — planned focused tests, including adverse paths.
- Existing validation/navigation tests — extend only for affected invariants.

**Excluded:** UI adapter implementation (031), Vite proxy (030), service Python,
core APIs/SDKs, DB/migrations, new session store, public v1 export removal, production
auth gateway, plan 008 implementation. Keep `atlas-layer.v1` public compatibility.
No facade copies or private `app.*` imports. Proposed test filenames may be
consolidated with existing adjacent tests; report the actual inventory.

## Ordered bounded milestones

| Milestone | Outcome | Depends on | Budget |
|---|---|---|---|
| 029a | Public root run lifecycle and validated graph projection | Existing B/C contracts | ≤400 changed lines |
| 029b | Scoped branch research, committed history versions, stale/cancel safety | 029a | ≤400 |
| 029c | Explicit immutable artifact save/load and malformed/denied/expired handling | 029a | ≤400 |
| 029d | Approval lifecycle and integration regression evidence | 029b/c | ≤400 |

One writer, independently tested milestones; no blind rewrite. If one coherent
milestone including tests exceeds the default, record `Budget override: <reason>`
and actual line count in its handoff, retaining the sub-milestone checkpoints.
No migrations. Live acceptance and security verification remain pending; code-level
review of the source-pair/branch-history fix does not establish live completion.

## Acceptance criteria

- [ ] Root query uses public thread/run submission and actual successful tool results;
  schema-invalid output, absent/invalid references and invented citations fail visibly.
  Model graph output cannot manufacture execution IDs, actions or permissions.
- [ ] Child research sends selected ancestry/context, not unrelated sibling research;
  stable identities and saved sibling paths survive. Candidate identity is resolved
  before claims/facet research; person research obeys the IA public-information limits.
- [ ] Thread/branch operations use actual committed server history versions and public
  response identifiers; never substitute depth, local snapshot revision or guessed versions.
  Conflicts are surfaced/reconciled, not silently resubmitted as duplicate paid runs.
- [ ] Correlate request/run/thread/event IDs; ignore stale/replayed updates and preserve
  parent content on failure. Cancellation calls the remote run cancellation API;
  disconnect alone is not cancellation. Late completion cannot overwrite a newer branch.
- [ ] Surface partial/budget/failed/approval-required states. Retry/resume follows actual
  server state and user intent; no automatic unbounded spending.
- [ ] Approval list/detail/decision uses public contracts and authoritative status/digest;
  401/403, separate-authorizer and self-approval restrictions remain intact.
  Caller authentication is never treated as approval authorization.
- [ ] Explicit save serializes a validated snapshot to a **new artifact ID every time**
  through public artifact upload; acknowledge only after success. No autosave,
  localStorage substitute, fake saved state or overwrite revision-conflict mechanism.
- [ ] Load fetches metadata/content through public paths and validates before activation.
  New artifact saves do not mutate old bytes. Round-trip graph, selection, branches and
  execution references; opening a snapshot does not replay an expired stream or run.
- [ ] Artifact ownership/retention/quota/expiry stay core-owned. Report 403/404/expiry,
  malformed bytes and quota failures honestly. Recent items are public run-scoped artifact
  listing, not a fictional global session index.
- [ ] Real authenticated principals exercise cross-tenant artifact metadata/download,
  run/thread access and approval denial through the public path. No invented tenant claims
  or mocked header-only proof; unavailable second-principal evidence blocks that gate.
- [ ] Existing public v1 exports/tests remain. Boundary counts do not increase.

## Baseline and verification

Supplied parent/reviewer evidence, **not rerun by this documentation update**:
latest engineer report: client build and typecheck pass with 158 tests; the parent
reran 157 tests before the final budget fix. The source-pair and branch-history
fix received independent `APPROVE`. These latest results are reported, not
independently rerun here. Earlier baseline: client 113 tests; service Python 19
passed with Ruff/Pyright; web 10 tests passed and bundle passed, with five known
legacy TS diagnostics; global boundary 175 errors / 9 warnings, Atlas 154 / 1.
A–D independently APPROVE.

Before editing each milestone, record actual dirty-tree baseline and commands:
`cd services/atlas-explorer/client-ts && npm test`, `npm run build`,
`npm run typecheck` (confirm existing script availability; otherwise use the repository's
existing compiler command and record it). Run targeted new tests plus full client suite.
Run `python .github/skills/core-boundary-check/scripts/check_boundaries.py`
from repository root; require no increase from 175/9 globally and 154/1 Atlas.
Run affected service tests if wire formats change; do not modify Python to make them pass.
Use authorized small live runs capped by existing `max_cost`; record redacted endpoint,
status, IDs and assertions, never credentials. Test adverse cases with deterministic
public-transport tests as well as real-path tenant/approval checks.

## Risks, unknowns and rollback

- Public SDK artifact upload shape and thread branch/version semantics must be checked
  against actual server responses before claiming success; do not guess a private endpoint.
- Available live tools, second-tenant test identity, separate approver and retained artifact
  policy are environment prerequisites, not facts established by `me` returning 200.
  If absent, report the exact blocked criterion; do not weaken authorization.
- Untrusted graph output/cost amplification → runtime validation, tool provenance,
  bounded runs and no automatic sibling expansion.
- Rollback: restore prior extension client behavior/entry wiring; retain public v1 exports,
  core records and all saved artifacts. Do not delete test/live data without authorization.

## Decisions needed

None for this approved scope. Missing public capability or a public breaking change
requires a separate decision/plan, not widening this implementation.

## Log

- 2026-11-09 architect: recorded approved public-API route and in-flight work;
  live acceptance and real-path security verification remain pending.
- 2026-11-09 architect: parent reports the source-pair and branch-history fix
  independently approved; latest client report is 158 tests with build/typecheck
  passing (parent reran 157 before the final budget fix). Legacy budget limits
  were restored to 0.15/0.10 after a real policy 403. A live research run is
  currently running after the restoration; its graph has not yet been validated.
  Do not claim live research, artifact authorization, cross-tenant denial, or
  separate-approver proof complete until the corresponding live assertions pass.
