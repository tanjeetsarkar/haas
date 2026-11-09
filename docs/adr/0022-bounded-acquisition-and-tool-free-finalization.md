# ADR-0022: Bounded acquisition and tool-free finalization

* Status: accepted — behavior, quotas and one reversible phase-state migration authorized; shared-database application is not authorized
* Roadmap items: authorization/dispatch hardening; no new roadmap identifier assigned
* Related requirements/threats: ADR-04, ADR-05, ADR-06, ADR-07, ADR-08, ADR-09; unbounded tool loops, lost finalization opportunity, budget reset on recovery, invalid model output
* Implementation: [plan 036](../plans/036-bounded-acquisition-finalization.md)

## Context

Plugins that research or analyze information need a bounded way to acquire
observations and then ask a model to synthesize them without tools. Instructions
in a prompt are not an enforcement boundary. The model gateway currently omits
the `tools` field when its tool list is empty; a tool-free final turn must be
enforced by the server, not inferred from `tool_choice`, a provider-specific
response-format feature, or model compliance.

A reported Atlas public run illustrates the failure mode, not a core-specific
requirement: with `max_steps=6`, `max_cost_usd=0.15` and
`max_tool_invocations=12`, repeated fetches consumed the available model turns
before a final synthesis. A separate child run had budgets of 5 steps, $0.10
and 8 invocations. Prompt-only limits have not prevented this pattern. The
reported tool history included successful retrievals as well as an HTTP 403;
this ADR does not treat the number or quality of those results as a core-domain
fact.

The existing total budgets must not be increased to make room for a final
answer. The same run-level limits must cover acquisition and finalization,
including worker redelivery and restart. Plugins such as Atlas and a web-research
or incident-analysis plugin could use the generic capability; no plugin-specific
tools, result schema, source-quality rules or provider settings belong in core.

Bounded source review confirmed that `app/core/llm.py` adds `tools` only when
nonempty; `app/core/workflow.py` has durable model-step reservations, usage
settlement and global budget checks; and `app/worker/tasks.py` converts returned
tool calls into invocation batches. `RunAuthority` is frozen and strict at
version 1, with only `tools` and `intent`; plan 035 treats its persisted
authority as immutable.

Inspection of mutable `messages_checkpoint_json`, `pending_tool_calls_json`,
`next_action`, `step_count`, `state_version`, `lease_epoch`, `run_steps` and
`usage_ledger` found no safe dedicated phase-state field. Those fields hold
conversation, pending tool/approval payloads, constrained queue action,
fencing/counters, attempt records or cost entries; none stores immutable phase
configuration plus mutable phase progress and a one-shot finalizer attempt.
A v2 wrapper around authority v1 is not presumed compatible with plan 035's
direct v1 serialization and would not by itself provide mutable progress.
The user has authorized exactly one separately reviewed reversible phase-state
migration for implementation and disposable-database verification. Runtime
activation still requires later owner-coordinated admission/workflow/dispatch
slices and verification. Shared-database application, deployment and service
restart are not authorized.

Although the proposed column check accepts any JSON object, the current
`RunAuthority` reader requires literal version 1 and rejects extra fields.
Thus an outer v2 envelope with nested v1 authority is not compatible by
assumption and needs explicit 035-owner/reviewer reader-compatibility evidence.

The missing storage roles are distinct: immutable opt-in configuration
(version, mode and 20% reserve) and mutable phase progress (phase/version and
a consumed/started finalizer-attempt marker/key). The authorized migration adds
nullable `runs.phase_config_json` and nullable `runs.phase_state_json`, with an
immutable-config guard, guarded monotonic state transitions, and downgrade
refusal when phase data exists. The migration is limited to disposable
verification databases; it does not authorize applying it to any shared DB.

## Decision

1. Provide an **opt-in, generic run mode** that enforces bounded acquisition
   followed by one reserved tool-free finalization turn. The generic additive
   request field is `phase_config`; final-answer schema validation is
   client-side. Until all runtime enforcement is reviewed, the existing
   raw-presence gate rejects this field, including null, and no public request
   activates the mode. The mode cannot be implemented as prompt text alone.
2. The existing run totals remain hard ceilings: `max_steps`, `max_cost_usd`,
   `max_tool_invocations` and the run's existing absolute deadline. Acquisition
   receives only the unreserved portion. A finalization attempt consumes one of
   the original model steps and its actual accounted cost; tool invocations are
   never added to or reset between phases. Recovery and retries must consult
   durable server-owned state and the same totals. There is no automatic paid
   retry when finalization has already been attempted. Reserve exactly 20% of
   unchanged `max_cost_usd` for the final model turn: $0.03 of $0.15 and $0.02
   of $0.10, calculated in exact decimal arithmetic without cent rounding.
   Admission rejects if the existing model estimator cannot reserve
   one final turn within that amount. Acquisition may consume at most the
   other 80%; actual charges remain subject to the original total hard cap.
3. Before finalization, the server must establish at least one successful
   acquisition result (the generic criterion is presence of a successful
   non-control tool outcome, not semantic evidence quality). If there is no such
   result, the run fails with a stable machine-readable error and does not issue
   a final model call. If no acquisition or finalization capacity remains, fail
   with a budget-specific error; never manufacture a turn or exceed a total.
   After a successful result, either a no-tool acquisition response or
   exhausted acquisition allowance triggers the distinct finalizer. Exhaustion
   without a successful non-control result is terminal failure.
4. The final model request contains no tool schemas. The server persists the
   finalization phase before dispatch and rejects any returned tool call before
   creating an invocation, redeeming a grant, or entering a handler/adapter.
   This is independent of provider `tool_choice` behavior. The minimum guarantee
   is one bounded tool-free finalization **attempt**, not valid, complete,
   truthful or schema-conforming output.
5. Final answer schema validation is a client/extension responsibility. Core
   does not promise provider-specific structured-output support or silently
   coerce a response. A consuming plugin may validate the returned result
   locally and report a validation failure; it cannot turn that failure into an
   automatic paid retry.
6. Preserve cancellation and existing approval behavior. Cancellation wins
   before a phase dispatch. An approval pause during acquisition does not enter
   finalization or reset any budget; resumption continues from durable phase
   state and the same deadline. A finalization turn itself cannot dispatch an
   approval or other tool. Never automatically retry a started finalizer.
7. Reject delegation from phase-enabled runs before dispatch. Parent/child
   budgets are not described as aggregated.
8. Do not change immutable authority v1 or implicitly extend its serialized
   format. The user authorizes exactly one separately reviewed, reversible
   migration adding nullable immutable `runs.phase_config_json`
   (version/mode/reserve) and nullable mutable `runs.phase_state_json`
   (phase/version/one-shot attempt marker and key). Preserve ADR-0021 v1.
   Any v2 wrapper is out of scope unless the 035 owner/reviewer confirms
   compatibility. Test migration only against disposable databases. Shared DB
   application, deployment and service restart remain unauthorized.

## Consequences (including rejected alternatives)

* Plugins can request an enforced acquisition/finalization lifecycle without
  core knowing their tools or domain. Research/analysis extensions and an
  incident-analysis extension are independent potential consumers.
* Total budgets remain unchanged, but reserving a finalizer can leave less
  capacity for acquisition. A turn reservation alone cannot ensure that a
  provider returns a valid answer.
* A success-count criterion is deliberately weaker than a domain-specific
  evidence-quality test. Extensions remain responsible for source validation,
  provenance and output-schema validation.
* Prompt-only limits, hiding tools only in UI, trusting `tool_choice: none`,
  assuming provider-specific `response_format` support, increasing total
  budgets, and automatic paid retries are rejected.
* The 20% reserve, behavior, migration direction and public field spelling
  `phase_config` are settled. Admission, workflow, dispatch, recovery, SDK
  contracts and rollout gates remain later implementation work. This ADR does
  not authorize shared DB application or deployment.
