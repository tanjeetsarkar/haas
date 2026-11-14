# 038 — Atlas guided research, history and contextual actions

- Status: in-progress — live research acceptance NOT VERIFIED; 044c AI drafting BLOCKED; external/phases disabled
- Type: epic
- Owner agent: plugin-engineer; client-engineer for generic SDK streaming only
- Epic: continuation of [024](024-atlas-exploration-ux-epic.md), [029](029-atlas-live-client-snapshots.md) and [031](031-atlas-live-workspace-parity-retirement.md)
- ADR: none for Atlas-owned changes; any newly discovered kernel capability needs its own ADR and slice

## Goal

Make Atlas a saved, user-directed research workspace: one question produces one
useful level; selecting a row researches only that subject; actions are separate
interactions. Replace periodic run reads with SSE and expose the execution
information available in the TUI. Preserve research history automatically.

This is a plan, not evidence of implementation or activation. On 2026-11-10
the user approved the entire plan, including cached revisits/details and the
separate exact-intent approver constraints, and requested implementation,
migration and feature enablement where required **only on the existing local
development stack**. This supersedes this epic's earlier blanket local-apply
prohibition, not any verification, review or security gate. Production and other
shared databases, commits and pushes are not authorized. The architect performs
no runtime changes. External execution and phased research remain held until
their respective verified release gates pass.

## User decisions and relationship to existing plans

The user selected durable workspace and meaningful interaction history, including
submitted prompts, branches, drafts and selections, rather than every keystroke.
They requested research history with **Start afresh** and **Continue**, and
mandatory actions on every card, backed by an Atlas custom-action skill.
External execution is a requested end state, not permission to bypass its gates.

On approval and implementation, this plan replaces the explicit-save/no-autosave
UX requirements in 029/031 and the facet-only click behavior. It does not mark
their unfinished live, tenancy, accessibility or approval gates complete.
Public snapshot import/export remains compatible; removing the manual Save
button does not remove public client APIs or existing artifacts.
[035](035-bounded-action-initiation.md), [036](036-bounded-acquisition-finalization.md)
and [037](037-live-final-entry-authorization-rpc.md) retain their own scope and
holds. Do not enable phases to implement this UX.

## Evidence from the current implementation

| Finding | Evidence |
|---|---|
| Default live research polls every 750 ms | `client-ts/src/explorationClient.ts`: constructor, `research()` and `pause()` |
| SSE already exists in the public SDK | `haas_sdk_ts/src/client.ts`: `streamEvents()`, cursor replay and optional output deltas |
| SDK streaming needs lifecycle assessment first | No abort signal is passed to its fetch; EOF reconciliation treats `input_required` as outside the active-status list |
| Development proxy already pipes/flushed upstream streams | `web/dev/apiProxy.ts`; idle SSE is not subject to the response-header deadline |
| Root prompt already requests only one level | `client-ts/src/explorationResearch.ts`: bounded tactical plan and selected-ancestry context |
| Entity selection is currently navigation, not a deep dive | `web/src/components/AtlasWorkspace.tsx`: `open()` researches only incomplete `research_needed` facets |
| History is not durable | Same workspace: `historyData` is an in-memory Map; snapshot saves are explicit |
| Artifact storage is not a workspace catalog | `client-ts/src/explorationPersistence.ts`: requires a run ID and lists artifacts per run |
| Atlas has no application persistence/auth backend yet | `services/atlas-explorer/app/main.py` is health-only |
| Research actions are intentionally excluded | Research result contains graph fields only; research prompt forbids external actions |
| TUI is the visibility reference | `haas_cli/tui/app.py`: prompt rendering, `_on_event()`, tool cards, history and usage views |

Paths in this table are relative to `services/atlas-explorer/` unless explicitly
rooted in a repository package. These are source observations, not live proof.

The supplied validation error establishes invalid `targets[0..5]` against the
strict union. Targets must be exactly an entity reference, facet reference or
source reference. `source_id` is required only for the source variant; that
message does not mean every target needs one. AJV reports failures from every
union branch, producing the repetitive message. The actual returned JSON was
not provided: extra labels, wrong kinds or mixed reference shapes are possible,
not a verified root cause. First slice: [039](039-atlas-target-result-contract.md).

## Boundary classification

| Change | Core / Plugin / Client | Why |
|---|---|---|
| Graph prompts, validation, row dives, history, action skill and UI | Plugin | Disappear if Atlas is deleted |
| Durable workspace catalog, journal and draft storage | Plugin service | Atlas-owned data, not core tables or browser-only storage |
| Abortable/reconnect-safe public SSE consumption, if needed | Generic client | Useful to every SDK consumer; no Atlas names or types |
| Run lifecycle, event authorization, artifacts and approval enforcement | Existing core | Consume public contracts; do not bypass or duplicate enforcement |
| Missing generic capabilities discovered during implementation | Separate core slice | ADR and explicit review before any Atlas consumer |

## Simplified interaction model

1. **Research history:** select an existing research workspace and Continue, or
   Start afresh to create a new workspace without deleting the old one.
   Continue restores branches, selections, drafts and execution references.
   Reopening history never submits a paid run.
2. **Ask:** save the submitted query, then start a bounded root run. Show only
   useful immediate results; do not precompute all Miller columns, sibling
   details or recursive descendants.
3. **Explore:** a new entity/category/facet row click starts one scoped deep
   dive, using selected ancestry and actual committed thread history. Preserve
   parent/sibling results. Opening a source shows its evidence, not another
   autonomous research run. Unavailable/candidate rows explain their limits;
   candidates resolve identity before attaching detailed claims.
4. **Revisit:** show previously researched details and their research history.
   Continue from that saved branch; an explicit Research again starts a fresh
   bounded dive. This is the recommended non-rebilling interpretation of the
   user's Start afresh/Continue requirement, pending confirmation.
5. **Act:** every card has an Actions affordance, relevant proposed actions and
   Custom action. Actions open their own saved interaction; they never execute
   on card selection or automatically continue research.
6. **Save automatically:** meaningful mutations enter a durable journal.
   Show Saving / Saved / Save failed with retry. Remove Save new artifact
   snapshot only when automatic persistence is verified. Recovery restores a
   workspace, not a replay of research or external side effects.

Examples: cancer hospitals in Kolkata -> selected hospital -> selected public
specialist profile -> appointment action draft; personal computers -> selected
build -> export a sourced specification and retailer-location report. Root
results need not be taxonomies: hospital search should return useful hospitals,
not force categories. Medical appointments require confirmed destination,
availability and user-supplied details; do not infer private patient data.

## Contracts and safeguards

### SSE and execution visibility

- Use authenticated `/v1/runs/{id}/stream`, not an interval of run GETs. Reuse
  the SDK parser/replay path after generic lifecycle fixes; do not add a second
  Atlas SSE parser or query-string credentials.
- Correlate workspace/request/path/thread/run IDs and deduplicate durable event
  sequences. Keep preview deltas separate from durable replay cursors and
  validated graph output. Disconnect is not cancellation or completion.
- Abort must release readers, sockets and reconnect waits. Preserve approval
  pauses; bounded reconnect or explicit reconnect must not create another run.
  Terminal/recovery reconciliation GETs are allowed; steady-state polling and
  a hidden polling fallback are not.
- Run drawer: submitted question, exact application-generated prompt and
  selected context, IDs, model/active skills, steps, tools/arguments/results,
  timestamps, elapsed time, cost/token usage and limits, approvals, cancellation,
  reconnect and error states. Match actual TUI event/history/usage fields;
  show unavailable when the public contract does not expose a value.
- Do not expose credentials, hidden chain-of-thought or private system prompts.
  Escape untrusted text, label truncated previews and cap rendering. Full
  authorized tool output comes from public history, not a truncated event.
  Output deltas may be displayed as provisional text, never committed graph data.

### Durable history and automatic saving

- Before implementation, design Atlas-owned authenticated workspace APIs and
  storage. Existing run-scoped immutable artifacts can remain graph checkpoints,
  but cannot alone provide pre-run drafts or a cross-run history index.
- Journal submitted prompts, meaningful draft checkpoints, row/path selection,
  comparisons, custom questions, cancellation/retry intent and action lifecycle;
  retain root/child execution references and saved graph revisions. Do not store
  raw DOM events, every keystroke, secrets or unnecessary sensitive payloads.
- Use client mutation IDs, ordered per-workspace saves, optimistic revisions
  and explicit conflicts. Preserve sibling branches; failed saves remain dirty
  and visibly retryable. A receipt means server persistence, not local memory.
- Save query and operation intent before dispatch. Recovery of an ambiguous
  submission reconciles authoritative server state; it must not blindly resubmit
  a paid run or action. Reloading a running workspace reattaches to that run.
- Define retention, access control, size/quota limits and deletion behavior.
  Tenant and principal come from verified authentication, never trusted browser
  headers or the development proxy's configured identity as production proof.
  Initial deployment is blocked on an authenticated Atlas gateway design.

### Mandatory card actions and a separate execution lane

- Add an Atlas plugin-pack skill for contextual/custom action drafting, not an
  agent-governance skill. Every card has at least one relevant safe
  option (for example inspect sources/export), plus Custom action; unsupported
  business actions show missing capability/reason instead of invented tools.
- Suggest actions proactively without running a model on every render or
  automatically spending on every card. Persist proposals against card identity
  and revision. Research graph output still cannot manufacture executable
  permissions, trusted tool mappings or approvals.
- Custom actions describe intent using only the selected card/ancestry and
  explicitly chosen user inputs. Resolve to a registered capability and strict
  argument schema. Unknown capabilities remain saved drafts, never generated
  executable code or arbitrary HTTP requests.
- Separate states: proposed -> draft -> reviewed -> awaiting approval ->
  executing -> succeeded/failed/unknown/cancelled. Bind approval to exact
  destination, arguments, capability/schema version and digest; edits invalidate
  stale approval. A suggested action is not consent.
- External execution is delivered only after 035/037 enforcement and release
  gates, independent approver proof, tenant isolation, effect deduplication,
  safe destinations and sandbox/secret checks pass. No automatic resend after
  an unknown outcome. User-requested enablement is the end goal; generic
  kernel/SDK work ships separately before the Atlas consumer.

## Ordered slices

| # | Slice | Owner | Depends on | Status |
|---|---|---|---|---|
| 039 | Strict target-result contract, prompt examples and useful validation errors | plugin-engineer | none | done — independent reviewer 806dca02 APPROVE; parent-reported 139 tests passed |
| 038a | Generic SDK stream abort/reader/reconnect/approval lifecycle | client-engineer | none | done — 040 final APPROVE |
| 038b | Atlas SSE lifecycle and terminal reconciliation | plugin-engineer | 039, 038a | done — 041 final APPROVE |
| 038c | TUI-parity run drawer and progress timeline | plugin-engineer | 038b | 043d final APPROVE 48ba90; implementation/review done, live run acceptance NOT VERIFIED |
| 038d | Authenticated Atlas history/storage contract and deployment design | architect | none | 042 implementation approved; local migration applied, storage smoke verified |
| 038e | Durable workspace catalog and mutation journal | plugin-engineer | 038d | 042 migration 001_workspace applied locally; forced RLS/least privilege verified |
| 038f | Automatic save queue, history, Start afresh/Continue and save-button replacement | plugin-engineer | 038e | browser draft revision 3/GET/hard reload, history, Start afresh/Continue and zero POST runs verified |
| 038g | Entity/category/facet click-driven dives and branch revisit history | plugin-engineer | 038b, 038f | 043 implementation/review done; live root/child/research journeys NOT VERIFIED |
| 038h | Every-card actions, Atlas drafting skill, custom/local actions | plugin-engineer | 038f, 038g | 044a/b/d approved; 044c model drafting BLOCKED on public tool-free enforcement |
| 038i | Approved external execution and receipts | plugin-engineer | 038h, 035/037 release gates | blocked; runtime remains disabled |

The authoritative numbered contracts replacing the provisional 038a–i labels:

| Plan | Supersedes provisional rows | Current integration status |
|---|---|---|
| [040](040-generic-sdk-stream-lifecycle.md) | 038a | done — final APPROVE c83a; 20 SDK tests |
| [041](041-atlas-sse-client-progress.md) | 038b, progress projection | done — final APPROVE 6952; 51 focused / 194 eligible tests |
| [042](042-atlas-durable-history-storage.md) | 038d/e | approved implementation; local 001_workspace APPLIED, RLS/runtime-role and authenticated storage smoke verified |
| [043](043-atlas-autosave-history-dives-drawer.md) | 038c/f/g | implementation/review done; 043d final APPROVE 48ba90, 229 UI tests/types/build; draft browser acceptance verified, live research acceptance partial |
| [044](044-atlas-card-actions-drafting-skill.md) | 038h | 044a/b/d approved; 044c model drafting BLOCKED, no dispatch/files |

038i has **no release authorization from this plan** while verified core gates
are absent. Parent reports both phase and external runtime release NO-GO:
public raw holds, 037 deny-only, incomplete tests/reviews/release TLS proof.
SSE and one-level research require neither feature. Local migration authority
does not authorize hold removal or a provider-effect experiment.

Current integration evidence (2026-11-10, parent-reported; architect did not
rerun checks): 039 remains done with reviewer 806dca02 APPROVE, 132 baseline →
139 passed, types/build/diff check passed and boundaries unchanged at 175/9.
No live incident reproduction is claimed. 040 final reviewer c83a APPROVE:
20 SDK tests, build passed. 041 final reviewer 6952 APPROVE: 51 focused /
194 eligible tests. 042 final reviewer 59f32 APPROVE: service 56 passed /
1 skipped, actual disposable DB 2 passed, independent verifier 6e4a green,
final size-fix DB selection 2 passed. Latest report: **local migration
001_workspace APPLIED** to dedicated `atlas_explorer` on existing local harness
PostgreSQL; forced RLS/runtime least privilege verified, health/readiness 200,
authenticated public me 200, proxy CRUD 201/200/200/200, core containers unchanged.
Runtime environment reported git-ignored and mode 0600; no contents recorded.
043 cutover reviewer f1534 APPROVE: 228 UI tests **before the current drawer
change**, manual Save hidden on durable path; fetch-receiver fix real catalog
200, real browser draft server revision 3 verified. Browser/drawer/history
agent 139e5 finishing, then review; epic/full acceptance remains pending.
044a final reviewer 53d053 APPROVE: 21 tests; 044b approved: 24 plugin tests,
doctor 0 errors / 0 warnings. 044d reviewer 75c412 APPROVE: 222 UI tests, then
228 at cutover. 044c agent 6df13 BLOCKED, no files/model dispatch: public
tool-free enforcement unavailable (`max_tool_invocations >= 1` and
`activate_skill` unlocks tools). No drafting-model bypass is authorized.
039/040/041/044a/044b are complete; 044d is approved, while 042/043/044 and
the whole 038 epic remain open for remaining integration/acceptance/blockers.
Local storage migration/cutover does not enable phases or external execution:
both remain disabled. No full live research acceptance or paid runs claimed.

### Final integration update — remaining blockers are not waived

Latest parent report supersedes earlier pending drawer-review status above:
**043d final APPROVE 48ba90**, binding/cap fixes, **229 UI tests** and types/build
passed. 043 implementation and review are done, but live journey acceptance
remains partial. Independent final checks: **SDK 20**, **Atlas 206**,
**service 56 passed / 1 DB test skipped**, **plugin 24**, doctor **0 errors /
0 warnings**. Disposable DB **2 passed** previously; dedicated local database
is migrated. The skipped DB test is not a new successful DB run.

Parent's real default-browser checks: history loads; manual Save count **0**;
draft server revision **3** confirmed by GET and restored after hard reload;
Start afresh clears and Continue restores with **0 POST /v1/runs**; draft
workspace has no overflow at **1440/768/390px**. These prove draft/history UX,
not live research behavior. **No paid root/child runs, research journeys or
real-run network/SSE verification were exercised: live research acceptance
remains NOT VERIFIED.** 044c AI drafting remains BLOCKED on server-enforced
tool-free contract; external execution and phases remain disabled.

Touched Python ruff reported clean. Broad core lint has pre-existing unchanged
**F821 `Any` at `app/core/context.py:38`**; not fixed by this epic. All evidence
in this update is parent-reported, not rerun by architect. **Whole 038 is not
done.** No new runtime changes or commits are made by this documentation sync.

Each implementation slice gets a numbered full plan before editing; split at
approximately 400 changed lines and one migration maximum. Do not implement
all rows as one rewrite. Follow existing incomplete work, not a parallel replacement.

## Acceptance criteria and verification

- [ ] Transport tests and browser network trace show one active SSE connection,
  no timed run GET loop, replay without duplicate events and bounded reconnect.
- [ ] Cancel/supersede/unmount close streams and preserve remote cancellation
  semantics; approval pauses, gaps, stale updates and terminal races are tested.
- [ ] A TUI-to-UI parity matrix maps every requested display to a public field,
  test and browser assertion, or an explicit unavailable state.
- [ ] History restores after a hard reload and a new authenticated session;
  pre-run drafts and failed/cancelled research remain recoverable.
- [ ] Mutation tests cover disconnect, failed upload, duplicate delivery,
  conflict and out-of-order saves. Saved is shown only for acknowledged revision.
- [ ] Manual Save new artifact snapshot is absent after autosave activation;
  old artifacts still load and public snapshot exports remain.
- [ ] Hospital and PC journeys prove one root run, one run for the newly
  selected subject, no sibling/descendant runs, retained branches and separate actions.
- [ ] Every generated card exposes Actions and Custom action; keyboard actions
  do not trigger the row dive, and unavailable execution is explained.
- [ ] No external effect occurs from research, proposal, reload, SSE replay or
  stale approval. Real external execution remains blocked until its own gate.
- [ ] Tenant/ownership denial, XSS-safe rendering, prompt/tool redaction,
  responsive 1440/768/390px and keyboard/screen-reader behavior are verified.
- [ ] No new failures versus each slice baseline; independent review APPROVE.

Use ladder rungs 3/5 and service-local TS builds/types for Atlas client/UI;
rung 8 and SDK tests for generic streaming. Python/storage/plugin-pack work
adds its applicable lint/types/plugin/isolated DB gates. Browser verification
must include network counts and hard-reload recovery, not screenshots alone.

## Baseline

Planning-only baseline on 2026-11-10: clean worktree before documentation edits;
`python3 .github/skills/core-boundary-check/scripts/check_boundaries.py | tail -1`
reported **175 errors / 9 warnings**. No runtime tests or live runs performed.
Capture fresh targeted baselines before implementation; old plan counts are
historical reports, not current validation.

## Risks and rollback

- Persisting more user activity increases privacy/quota exposure: minimize stored
  data, document retention, scope access and surface storage failures.
- Reconnect or restored history can duplicate spend/effects: preserve operation
  identity, reconcile server state and never silently resubmit.
- New authenticated storage is substantial: approve its contract before code;
  do not misuse core artifacts as a fictional global workspace database.
- Roll back UI/transport slices independently; retain persisted data and public
  snapshot readers. Never roll back execution safety to make an action work.

## Decisions needed

Original decisions 1–3 are approved: cached details by default with explicit
Research again, Atlas-owned storage/auth design, and reviewed exact intents
with a separate approver. Do not ask the user to approve these again.
042 records conditional topology/retention questions only if implementation
needs a change beyond safe local database reuse and retain-with-quota defaults.

## Log

- 2026-11-10 architect: entire epic approved/in-progress; 039 in-progress with
  plugin engineer, 040 contract published for SDK owner before lifecycle edits.
  Plans 040–044 bound the work and preserve external/phase NO-GO. Authorization
  supersession applies only to existing-local-stack migration/enablement after
  review and gates, never production/shared targets or safety bypass.
  Budget override: one bounded extra inspection pass to establish SDK lifecycle,
  service dependency/compose and public identity contracts across five plans.
  No runtime commands, live auth checks, migrations, tests or boundary scan run.

- 2026-11-10 architect: proposed from source inspection and user planning choices;
  no runtime activation. Budget override: one additional bounded inspection pass
  beyond 25 reads/searches to connect live polling, schema, persistence, SDK/TUI
  evidence and existing disabled execution gates before writing the plan.
