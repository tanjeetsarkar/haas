# 036 — Bounded acquisition and tool-free finalization

- Status: approved for implementation — 036a/036b and one reversible phase-state migration authorized; shared-database application is not authorized
- Type: epic; first slice fully specified below
- Owner agent: phase engineer (core-engineer; 036a/036b bounded files), then core-engineer (workflow/API), client-engineer (SDK), plugin-engineer (consumers)
- ADR: [ADR-0022](../adr/0022-bounded-acquisition-and-tool-free-finalization.md)

## Goal

Plugins can opt into a server-enforced acquisition phase followed by a reserved
model finalization attempt that has no tools and cannot dispatch one. Preserve
the run's existing total step, cost, invocation and deadline ceilings through
both phases and across recovery. Atlas and a web-research or incident-analysis
plugin are possible consumers; neither defines core behavior.

This is planning under the user's implementation approval, not a report that
implementation, tests, migration, review or deployment are complete. Existing
total budgets must not increase. No paid research, provider experiment,
service restart or shared-database migration application is authorized by this
plan.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Durable phase state, quota reservation, model request mode, dispatch denial, run events/errors | Core | Would exist for any plugin that needs bounded acquisition and synthesis |
| Additive generic run request/result fields in REST and SDKs | Client / Core public API | Any plugin client may opt into the same lifecycle |
| Source-quality rules and validation of final answer schema | Plugin | Depends on the research/analysis domain; core only provides a tool-free result |
| Atlas research prompt, source verification and user-facing behavior | Plugin | Does not exist if that extension is deleted |

No plugin id, tool name, content schema, source-quality policy or provider model
is to be added to core. No plugin-specific code or service extraction is planned.

## Evidence and unknowns

Bounded inspection stopped at the exploration budget. Read paths/results:

* `app/core/llm.py`: the OpenRouter gateway includes tool schemas only when the
  supplied list is nonempty; it does not establish support for a tool-choice
  prohibition or provider-specific response schema.
* `app/core/workflow.py`: `StepTicket`, durable model-step reservations and
  accounting, settlement, invocation-budget guards, run errors, and deadline
  handling are present. Full restart/retry and transition behavior was not
  traced.
* `app/worker/tasks.py`: `_step` consumes assistant tool calls and builds
  invocations. The exact before-grant / before-handler enforcement path was
  not read end to end.
* `app/core/run_restrictions.py`: `RunAuthority` is frozen, strict, and exactly
  version 1 (`tools`, `intent`). This is not a phase-progress store.
* Plan 035 reports `runs.authority_json` as immutable authority and its model
  disallows unknown fields. Its trigger rejects authority changes after insert.
  Do not edit plan 035 or ADR-0021, mutate v1, or co-own its store/workflow/
  restrictions paths.
  The migration's JSON-object check alone permits arbitrary objects, but the
  current `RunAuthority` reader requires literal version 1 and forbids extra
  fields. A new outer envelope with nested authority v1 therefore needs an
  explicit reader-compatibility review; it is not backward-compatible by
  assumption.
* Bounded schema/workflow inspection found mutable `messages_checkpoint_json`,
  `pending_tool_calls_json`, `next_action`, `step_count`, `state_version`,
  `lease_epoch`, `run_steps` and `usage_ledger`. None is a safe dedicated phase
  state field: message checkpoint is conversation data; pending calls encode
  actual dispatch/approval; next_action is a constrained queue action; counters
  fence work or account attempts/cost but do not distinguish phase/config or
  durably mark the one-shot finalizer attempt. No separate mutable run-owned
  phase state or immutable phase configuration field was found.
* A v2 wrapper around immutable authority v1 is not presumed compatible with
  035's direct `RunAuthority(version=1)` representation and does not by itself
  provide mutable phase progress. It may be considered only with explicit
  035-owner/reviewer compatibility confirmation; do not implement it silently.
* The missing durable contract is two distinct roles: immutable opt-in
  configuration (version/mode and 20% reserve) and mutable phase progress
  (phase plus a consumed/started finalizer-attempt marker/key). Neither role
  may be encoded by mutating `authority_json`.
* The reported public Atlas run (`openrouter/free`, six steps, $0.15, 12
  invocations) and child run (five steps, $0.10, 8 invocations) are user
  context, not independently verified. They demonstrate that quotas must fit
  within existing totals, not new defaults or domain rules.

Remaining implementation inventory: confirm the existing final-model budget
estimator can admit a final turn within a 20% reserve; trace exact approval,
recovery and cancellation transitions; enumerate public REST/OpenAPI/SDK
files; and establish homogeneous-version rollout. These are engineering
gates, not unresolved user choices. One reversible migration adding separate
immutable phase configuration and mutable phase progress has been explicitly
authorized. Its review and disposable-database verification are required;
shared-database application remains unauthorized.

## Contract and invariants

1. The feature is opt-in through the additive generic run request field
   `phase_config`. It
   requests bounded acquisition plus finalization; final answer schema
   validation belongs to the calling client/plugin. Existing runs remain
   unchanged. Until full enforcement is reviewed, the raw-presence gate rejects
   this field, including null; naming it does not activate the feature.
2. `max_steps`, `max_cost_usd`, `max_tool_invocations` and the existing absolute
   run deadline are the unchanged total ceilings. An acquisition quota is
   carved out of these totals; it is never an additional allowance. Exactly
   one original model step is reserved for finalization. Invocation allowance
   is acquisition-only and remains globally capped by the existing total.
   The finalizer cost reserve is exactly 20% of the requested run's unchanged
   `max_cost_usd` (root example: $0.03 of $0.15; child example: $0.02 of $0.10).
   Admission rejects when the existing model-budget estimator cannot reserve
   one final model turn within that amount. Acquisition may consume at most
   the other 80%; actual charges still obey the original total hard cap.
3. Finalization may start only if there is at least one successful
   non-control acquisition tool outcome and sufficient reserved step, cost and
   time remain. Core tests the generic outcome-presence rule, not evidence
   relevance, factuality, source quality or answer correctness.
   The distinct finalizer turn starts after a no-tool acquisition response
   following that success, or when the acquisition allowance is exhausted.
4. Persist the phase transition before issuing the final model request. The
   final request has an empty tool list; the gateway omits provider tool
   schemas. If the response nevertheless contains tool calls, reject the
   response before invocation creation, approval/grant handling, or any
   dispatch/handler/adapter call. `tool_choice: none` is not proof of this
   property.
5. A finalization attempt is not a guarantee of syntactically valid, complete
   or schema-valid output. The consuming client/plugin validates its required
   final schema. Do not assume provider response-format support, silently
   repair output, or automatically issue a paid retry after a finalizer attempt.
6. Budget and phase state survive worker restart, redelivery, approval pause and
   resume. A pending approval remains in acquisition; resume cannot reset any
   counter or extend the absolute deadline. Cancellation before dispatch
   prevents the next phase/model call. Define ambiguous in-flight finalizer
   behavior with durable attempt state; a started finalizer is never retried
   automatically or billed through an automatic paid retry.
7. Reject delegation from phase-enabled runs before dispatch. No child budget
   is claimed to aggregate into the parent.
8. Use one separately reviewed, reversible migration for nullable immutable
   `runs.phase_config_json` and nullable mutable `runs.phase_state_json`.
   Preserve `authority_json` and ADR-0021 `RunAuthority` v1 unchanged. Do not
   put phase progress in messages, pending calls, counters or authority v1.
   Migration application is authorized only in disposable verification
   databases; no shared-database apply is authorized. Any authority
   wrapper/version change is out of scope and requires 035-owner/reviewer
   compatibility review.
9. Stable run errors distinguish no successful acquisition result,
   insufficient acquisition/finalization budget, invalid finalizer tool calls,
   cancellation, unsupported delegation, and terminal finalizer failure. Exact
   codes and API mapping must be added to the contract before implementation;
   never use successful completion to disguise missing output.

## Ordered slices

Every implementation slice is separately reviewable and ≤400 changed lines
excluding generated artifacts. If it cannot meet the cap, stop and split before
continuing. The phase engineer owns only the new pure phase helper, the new
phase migration and its tests, and plan-036 evidence updates. The other
core-engineer currently owns plan-035 store/workflow/restrictions work; 036a/b
must not edit those files or plan 035. Later workflow/API integration is a
separate coordinated slice after the current owner confirms handoff.

| Slice | Owner | Scope / planned files | Acceptance / budget | Depends on |
|---|---|---|---|---|
| 036a | phase engineer (core-engineer) | Pure generic phase config, reserve arithmetic and transition rules: `app/core/run_phases.py`; `tests/test_run_phases.py`. No workflow wiring or persistence. | ≤250 changed lines. Exact 20% root/child examples; step/cost/invocation conservation; admission reserve predicate; no-evidence failure; both transition triggers; no runtime activation. | Settled user decisions |
| 036b | phase engineer (core-engineer) | One Alembic migration adding nullable immutable `runs.phase_config_json` and nullable mutable `runs.phase_state_json`; migration-only tests and disposable-DB upgrade/downgrade/round-trip verification. No existing store/workflow/API edits. | ≤350 changed lines, one reversible migration. Constraints enforce versioned object shapes; immutable config; monotonic phase-state version/transition and one-shot finalizer marker. Legacy null rows preserved. Shared DB apply prohibited. | 036a; current Alembic head and 035 migration coordination |
| 036c | core-engineer (after handoff) | Persisted create/decode/transition integration in `app/core/store.py` and `app/core/workflow.py`, with focused tests. | ≤350 changed lines. Tenant/run scoping, transactional state updates, fencing, restart and retry invariants; no writes to paths still owned by 035 without owner handoff. | 036a–b; migration review |
| 036d | core-engineer | Enforce quotas/finalization through workflow and worker; `app/core/llm.py`, `app/worker/tasks.py`; focused dispatch tests. | ≤350 changed lines. Empty finalizer schemas; returned tool calls denied before invocation/approval/grant/handler; total budgets conserved; no paid retry. | 036c |
| 036e | core-engineer + client-engineer | Additive opt-in API/OpenAPI and Python/TS SDK request/result contract; exact files inventoried before edits. | ≤400 lines, split if needed. Legacy requests unchanged; opt-in round-trips; answer validation remains client-side; admission rejects inadequate reserve. | 036c–d |
| 036f | core-engineer | Recovery, approval-pause, cancellation, absolute-deadline, delegation rejection and homogeneous-deployment gates. | ≤400 lines. Restart/redelivery, started-finalizer, cancel-before-dispatch and resume preserve budgets; incompatible versions reject opt-in runs. | 036c–e |
| 036g | plugin-engineer | Optional extension consumer and local answer-schema validation. | Separate extension plan/slice ≤400 lines; no core domain behavior. | Core slices reviewed and released |

## First slice: 036a

### Goal and ownership

Establish and test the pure generic phase configuration, reserve arithmetic
and transition rules. This slice does not persist or activate the feature.
Core owns the generic rules for research plugins and incident-analysis plugins.

### Allowed paths

* `app/core/run_phases.py` — new pure typed config, 20% reserve computation,
  admission reserve predicate and phase-transition rules. No provider or
  plugin-specific behavior.
* `tests/test_run_phases.py` — exact boundary and transition tests.

Do not edit plan 035 or ADR-0021. Do not add a migration, API/SDK field,
provider call, plugin change, runtime/workflow integration, schema validator,
or dispatch behavior in 036a. This pure slice can be implemented now; it does
not enable or claim durable phase behavior.

### Required behavior

* The reserve is exactly 20% of the caller's unchanged `max_cost_usd`, using
  exact decimal arithmetic without cent rounding: $0.15 → $0.03 and
  $0.10 → $0.02.
* Admission predicate rejects if the existing model estimator cannot reserve
  one final model turn within that reserve; it never raises `max_cost_usd`.
* Total step ceiling is the request's existing `max_steps`; reserve one of
  those steps for finalization. Acquisition can consume no more than
  `max_steps - 1`. If `max_steps < 2`, there is no valid acquisition-plus-
  finalization execution; return the designated insufficient-budget error.
* Total invocation ceiling remains exactly `max_tool_invocations`; it is not
  expanded or reset at phase transition. The finalizer itself has no tool
  invocations.
* Acquisition allowance is at most the other 80% of total cost and at most
  `max_steps - 1`; invocation budget remains the exact original ceiling.
* A successful non-control tool outcome is required. After success, either a
  no-tool acquisition response or exhausted acquisition allowance triggers a
  distinct reserved finalizer; exhaustion without success is terminal failure.
* A started finalizer is one-shot. The rules never authorize a paid retry or
  delegation.

### Acceptance criteria

- [ ] `pytest -q tests/test_run_phases.py` passes.
- [ ] Tests assert exact reserve values for $0.15 and $0.10, decimal precision
  behavior, max_steps 1/2 boundaries, and no increase to any total budget.
- [ ] Tests cover insufficient estimated finalizer reserve, successful-outcome
  requirement, no-tool/exhausted-quota triggers, and no-success terminal result.
- [ ] The pure model has no integration with `RunAuthority` v1, messages,
  provider requests, run persistence or plugin-specific types.
- [ ] `check_boundaries.py` errors: baseline → no new errors.
- [ ] No new failures versus the baseline on the targeted tests; no paid
  provider calls or live database mutation.

### Verification rungs

* Verification-ladder rung 1: Ruff and targeted pytest for the named test.
* Verification-ladder boundary check: `python .github/skills/core-boundary-check/scripts/check_boundaries.py`.
* No migration or database operation in this slice.

### Baseline

Not run by the architect. Before the first edit, the engineer records
`git status --short`, targeted test results and `check_boundaries.py` output;
compare against the then-current 035 working-tree baseline and do not overwrite
or stage another owner's changes. No tests, lints, scans, or runtime operations
were performed for this planning change.

### Risks and rollback

* Pure rules alone do not enable safe runtime behavior → keep activation out of
  036a; require the migration's separate review and disposable-DB verification
  before runtime integration.
* Cost estimates may not fit the $0.03/$0.02 reserve → admission rejects rather
  than increasing totals or shrinking the agreed reserve.
* A reserved step is not a guarantee of valid model output → expose terminal
  failure, validate output in the client/plugin and do not automatically pay
  for retries.
* Rollback: revert 036a with no data impact. 036a authorizes no persistence or
  runtime activation.

## Slice 036b — phase-state schema substrate

### Goal and ownership

Add exactly one reversible Alembic migration for separate opt-in
configuration and mutable progress, without wiring admission, store, workflow,
API or dispatch. The user authorized this migration for implementation and
disposable-database verification only. **No shared-database application,
service rollout, or runtime use is authorized.**

The phase engineer initially owns only:

* `migrations/versions/<new-revision>_run_phase_state.py` — one migration,
  based on the then-current Alembic head; do not modify plan-035 migration or
  migration files.
* `tests/test_run_phase_migration.py` — new migration/schema tests using a
  disposable PostgreSQL database or repository migration fixture.
* `docs/plans/036-bounded-acquisition-finalization.md` — record exact revision,
  test commands/results and independent migration-review evidence.

Do not edit `app/core/store.py`, `app/core/workflow.py`,
`app/core/run_restrictions.py`, `app/main.py`, plan 035, or ADR-0021. The
core-engineer currently working on 035 owns the store/workflow/restrictions
paths. Coordinate the migration's `down_revision` with the actual Alembic head
after 035's migration review/merge; avoid parallel Alembic-head changes. If a
linear revision cannot be coordinated, stop and report rather than changing
035 or inventing a branch merge.

### Storage contract

Add nullable JSONB columns with object-only checks:

* `runs.phase_config_json`: absent/null means legacy run. Enabled config is
  versioned and immutable after insertion. Minimum v1 payload records
  `version: 1`, generic mode `bounded_acquisition_finalization`, and
  `reserve_basis_points: 2000`. It must not duplicate or wrap
  `authority_json`.
* `runs.phase_state_json`: absent/null means no phase progress (legacy only).
  Enabled state is versioned mutable progress: `version`, monotonic
  `state_version`, `phase` (`acquisition`, `finalization`, `completed`,
  `failed`, or `cancelled`),
  `finalizer_attempted`, and `finalizer_attempt_key` (null until claimed).
  Claiming finalization atomically records `phase=finalization`,
  `finalizer_attempted=true`, and a stable attempt key before model dispatch.
  The claim is one-shot: no valid transition resets it or changes the key.

Migration constraints/trigger must preserve legacy null rows; reject malformed
non-object or unsupported-version config/state; prohibit changes to non-null
phase config; and prevent phase-state version rollback, invalid phase
regression, or clearing/changing an already-consumed finalizer marker/key.
State mutation remains run-owned and tenant-scoped. The migration does not
implement the runtime transaction/fencing protocol; slice 036c must use the
existing run lock/state-version CAS and atomically update progress with its
transition. Do not claim database triggers alone establish full workflow
authorization.

Rollback is reversible only while both phase columns are null. Downgrade must
fail closed when either contains phase data; it must never silently erase
opt-in configuration or progress. Do not add a second migration for test or
runtime convenience.

### Acceptance and storage gates

- [ ] Disposable-PostgreSQL test upgrades from the coordinated parent revision,
  preserves pre-existing legacy rows as NULL, accepts valid v1 config/state,
  and rejects malformed/unsupported shapes.
- [ ] Database tests prove config immutability and monotonic phase-state
  transitions, including rejection of phase rollback and finalizer-marker/key
  reset or replacement.
- [ ] Downgrade succeeds on an empty/null phase dataset and refuses when
  either phase column contains non-null data; a fresh upgrade after safe
  downgrade succeeds.
- [ ] Exactly one migration is added; `authority_json` and the ADR-0021 v1
  model/serialization are untouched. It chains from the coordinated current
  Alembic head and creates no parallel head.
- [ ] Migration review and disposable DB upgrade/downgrade evidence are
  recorded before any later runtime integration.
- [ ] No shared database connection or migration command is used. Migration
  commands run only against disposable PostgreSQL; no service restart or
  deployment is performed.
- [ ] `check_boundaries.py` has no new errors; changed-line budget ≤350.

036b establishes only the database schema guard. It does not create phase
configuration at run admission, persist progress from the workflow, or enable
the feature. Those require subsequent owner-coordinated slices.

## Decisions needed

Settled by the user: opt-in generic field; client-side schema validation;
20% reserve of the unchanged total; admission rejection when that reserve
cannot cover an estimated final model turn; successful non-control tool outcome
required; transition after no-tool acquisition response or exhausted quota;
distinct finalizer turn; no automatic paid retry; delegation rejected.

No remaining user decisions for the approved 036a/036b scope. Later runtime
integration is gated on 036b review, 035 owner coordination, exact admission
estimator evidence, and separate slice-level implementation review. Shared
database application remains explicitly unauthorized.

## Activation preparation — not implemented

The future `phase_config` request property maps to the existing `PhaseConfig`
v1 object (`version: 1`, `mode: bounded_acquisition_finalization`,
`reserve_basis_points: 2000`; unknown fields forbidden). Omission preserves
legacy behavior. Today raw presence, including null, is rejected before request
model validation; do not expose the property in OpenAPI or change that gate
until runtime proof and activation are approved. Activation needs an explicit
native/facade null and default-field compatibility check, admission-estimator
validation before replay, snapshot regeneration, changelog and SDK tests.

The current internal durable failure values are
`no_successful_acquisition_result`, `insufficient_budget`, `finalizer_failed`
and `finalizer_outcome_unknown`; `PhaseAdmissionError.code` also has
`finalizer_reserve_insufficient`. They are not yet a public error contract:
current `run_error.code` maps generic orchestrator failures to
`execution.failed`, and cancellation has a separate `run_cancelled` event.
For coordinated review, proposed explicit event mappings are:

| Condition | Proposed event code | Current durable distinction |
|---|---|---|
| No successful acquisition result | `phase.no_successful_acquisition_result` | `no_successful_acquisition_result` |
| Phase capacity or finalizer reserve insufficient | `phase.insufficient_budget` | `insufficient_budget`; admission separately uses `finalizer_reserve_insufficient` |
| Finalizer returned tool calls | `phase.finalizer_tool_calls` | currently `finalizer_failed`; explicit cause required |
| Finalizer failed or output missing | `phase.finalizer_failed` | `finalizer_failed` |
| Finalizer outcome unknown | `phase.finalizer_outcome_unknown` | `finalizer_outcome_unknown` |
| Delegation rejected | `phase.delegation_unavailable` | not yet wired |
| Cancellation | existing `run_cancelled` event | no new `run_error` code implied |

These codes are the planned normative phase categories but are not yet wired or
published. `app/core/phase_failures.py` supplies the pure projection contract;
its presence alone does not change run events, webhooks or API responses.
Supply explicit structured codes at known failure sites; do not infer them from
arbitrary provider exception text or add a broad exception catch. Coordinate
delegation denial and error/event wiring with the 035 owner; retain its
authority and restricted-resume guards.

### Coordinated delegation and error wiring (not implemented)

* 035 owner: `DistributedStore._check_parent_authority` is called under the
  existing tenant-scoped derivation-thread locks by both `create_run` and
  `agent_mesh.create_delegation`. Include `phase_config_json` and
  `phase_state_json` in the locked parent/lineage check; reject phase-bearing
  parents before any child run, delegation, outbox, invocation or revision
  lookup. Fail closed on either non-null column; do not decode malformed state
  as legacy. Preserve existing 035 restrictions, lock order, foreign-parent
  404 behavior and legacy null/null behavior. Review whether the existing
  ancestor traversal must also block legacy descendants of a phase run; do not
  silently widen unrelated thread-branch semantics.
* `agent_mesh.create_delegation` currently maps every `StoreConflictError` to
  `restriction.derivation_unavailable`; coordinate a distinct phase denial
  mapping without changing 035's existing error. The route and direct
  `create_run(parent_run_id=...)` need real PostgreSQL no-write tests, legacy
  and foreign-tenant regressions, and a lock-order concurrency test. Prepared
  tests are in `tests/test_run_phase_delegation.py` and currently fail pending
  that owner hook; they do not activate scheduling.
* Phase error integration must set a structured code at each known phase
  failure transition in `workflow.py`, `tasks.py`, and
  `acquisition_runtime.py`; `_append_events` already preserves explicit codes.
  `_finalize` webhook generation and `main.py` run/facade projections currently
  derive codes from result text, so event-only changes are insufficient.
  Coordinate a consistent durable source for events, webhooks and API views;
  finalizer tool-call rejection and generic finalizer failure currently share
  durable `finalizer_failed`, so their distinct public codes need a reviewed,
  restart-safe representation. The proposed no-migration mechanism uses the
  claimed finalizer's existing `run_steps.error` as a server-owned exact marker
  `phase.finalizer_tool_calls`, written during `_settle` in the same transaction
  as failed phase state. All other failure categories project from
  `phase_failure_code(PhaseState)`; on recovery/API reads fetch only the
  tenant/run-scoped finalizer step whose generated attempt key exactly matches
  `phase_state.finalizer_attempt_key` (never the most recent arbitrary step).
  Pass that step error to `phase_failure_code`; only the exact
  `phase.finalizer_failed` marker maps to generic finalizer failure. Explicitly
  pass the resulting code to `_append_events` and `_finalize`'s webhook code
  path; add a tenant-scoped store projection for the main facade's
  `_unsettled_error` and `_response_view`. Keep the writes and output
  projection consistent under transaction/restart tests. Run events alone are
  not the durable source: their retention policy allows deletion. The generic
  retention contract retains the claimed `run_steps` row with its run without
  independent step purge. Current repository cleanup does not purge steps and
  runtime roles lack step DELETE; `error` is writable, not immutable against
  privileged or authorized updates. A missing, unrecognized or contradictory
  claimed marker fails closed. `phase_failure_projection.py` implements the
  tenant-scoped read-only projection; no API view uses it yet. Preserve legacy
  error codes and cancellation semantics; never parse provider exception text.
  This still requires the atomic owner write and independent end-to-end review.
* Activation remains a later reviewed API/client slice: implement native and
  facade admission, regenerate OpenAPI and changelog, update SDKs with the client
  owner, and prove budget, delegation, recovery and homogeneous rollout before
  removing any raw-presence or scheduling hold.

## Slices (epic only)

| # | Slice | Owner | Depends on | Status |
|---|---|---|---|---|
| 036a | Pure phase rules and 20% quota arithmetic | phase engineer (core-engineer) | Settled user decisions | approved for implementation |
| 036b | One reversible config/progress schema migration and disposable DB tests | phase engineer (core-engineer) | 036a; coordinate Alembic head with 035 owner | approved for implementation; no shared DB apply |
| 036c | Enforced quotas, tool-free finalizer and before-handler denial | core-engineer | 036a–b | planned |
| 036d | Additive REST/OpenAPI/SDK contract | core-engineer + client-engineer | 036a–c | planned |
| 036e | Recovery, approval, cancellation, deadline, delegation and homogeneous rollout | core-engineer | 036b–d | planned |
| 036f | Optional plugin consumer and domain validation | plugin-engineer | Core release; separate plan | future |

## Log

- 2026-11-09 core engineer: quota review round 2 treats the atomic
  run-locked acquisition ticket and usage reservation commit as the logical
  dispatch authorization, as for finalization. A cancellation that commits
  first prevents a claim; one after the claim cannot undo the authorized
  potentially in-flight call. A late acquisition result after cancellation
  does not revive the run; its reservation remains uncertain and is not
  automatically retried. API and scheduling holds remain in place.
- 2026-11-09 architect: user confirmed 20% reserve, transition rules,
  client-side validation, no paid retry and delegation rejection; then
  explicitly authorized one separately reviewed reversible phase-config/state
  migration. Phase engineer scope is limited to the new pure helper, migration,
  migration tests and plan-036 evidence; store/workflow/restrictions remain
  owned by the 035 core-engineer. No shared DB apply is authorized. No
  implementation evidence claimed.
- 2026-11-09 core engineer: user reports 036 foundation and the downgrade
  lock fix independently approved, and 035 owner released `store.py` for
  phase-only work. Added held-only internal create/decode and tenant-scoped
  phase-state CAS; no workflow, worker, API or public admission activation.
  Disposable PostgreSQL combined phase/authority storage and migration tests:
  41 passed in each file order (one Alembic deprecation warning); independent
  store instances reload config and claimed attempt after checkpoint/cancel.
  Reconciliation and forged `none`/`step` wakeups do not lease held rows.
  This does not prove runtime dispatch, cancellation-phase transitions,
  provider retry behavior or activation readiness; those remain later slices.
- 2026-11-09 core engineer: added an early raw-presence rejection for
  `phase_config` in native and facade requests, including facade `haas`
  options and null/unknown values. It executes before
  idempotency, previous-response and agent lookups. Phase gate plus existing
  API regression tests: 141 passed (baseline 83 passed); Ruff passed, Pyright
  0 errors; generated OpenAPI matches the existing snapshot in a read-only
  comparison. The API changelog entry was subsequently added.
  Prospective workflow integration paths after owner handoff:
  `app/core/workflow.py` (fenced phase CAS, budgets, cancellation, recovery),
  `app/worker/tasks.py` (tool-free finalizer, dispatch denial, delegation),
  `app/core/llm.py` (request-mode proof), focused tests under `tests/`, and
  possibly `app/core/store.py` for coordinated held-run activation. No such
  paths were edited for this gate.
- 2026-11-09 user: the final generic public field spelling is `phase_config`;
  this does not authorize API activation. Split runtime work into independently
  reviewed bounded subslices, gated on explicit path-owner release:
  1. **036c-ledger** — `app/core/workflow.py` and new
     `tests/test_run_phase_ledger.py`; ≤400 non-generated changed lines.
     Under the existing tenant/run lock and lease/state-version fence, couple
     quota checks and finalizer claim to the existing step ticket and reserved
     usage ledger in one transaction. Reuse `PhaseConfig`, `PhaseBudget`,
     `PhaseState`, `calculate_quota`, `validate_phase_transition` and
     `StepTicket`; do not widen budgets or introduce a second paid attempt.
     Prove strict step/cost conservation, one claimed attempt key, stale
     fences/CAS, rollback, restart and uncertain in-flight failure on
     disposable PostgreSQL. Do not remove the hold or activate admission.
  2. **036d-worker** — `app/worker/tasks.py`, `app/core/llm.py` only if
     required, and new `tests/test_run_phase_dispatch.py`; ≤400 non-generated
     changed lines. Separately review a one-provider-attempt finalizer path,
     empty schemas and pre-invocation rejection of tool calls/delegation,
     including restart/cancel boundaries. Do not activate API admission.
  Additional store/workflow recovery or release wiring requires explicit owner
  coordination and separately scoped review; no path release is presumed.
