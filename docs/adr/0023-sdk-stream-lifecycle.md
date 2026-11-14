# ADR-0023: Generic SDK stream observation lifecycle

* Status: accepted — approved epic 038 scope; implementation/review tracked by 040
* Roadmap items: generic SDK/event consumption
* Related requirements/threats: tenant-authenticated observation, bounded reconnect,
  resource cleanup; ADR-07 SSE is not a coordinator

## Context

Plugins can observe long-lived runs, including approval pauses. Atlas and
incident-response plugins need bounded cancellation and replay semantics without
domain types in the kernel or SDK.

## Decision

Extend existing TypeScript StreamOptions additively with an AbortSignal.
Observation abort is not remote cancellation. Propagate authentication unchanged,
release stream resources on all exits, bound reconnects once per failure, and
reconcile EOF without treating input_required as terminal. Durable numeric event
IDs alone advance replay; optional live deltas are not durable state.
The exact executable contract is plan 040.

## Consequences (including rejected alternatives)

No server changes, migration, new dependency or plugin-specific SDK types.
Existing callers retain their options and return types. Reject independent
plugin SSE parsers, polling fallbacks and automatic remote cancellation.
Signals must interrupt reconciliation and backoff as well as fetch/read.
No quotas are increased; no credential storage or logging is introduced.
SDK consumers adopt the optional signal progressively; coordinated breaking
changes require separate approval.
