# Operations, reliability and migration plan

[Design index](README.md) | [SDD](sdd.md) | [Threat model](threat-model.md)

## 1. Deployment profiles

| Profile | Intended use | Required qualification |
|---|---|---|
| CURRENT Compose | Local development and trusted operator-managed demonstrations | Private access, reviewed plugins, nonproduction data/credentials |
| TARGET shared cells | Multi-tenant service with pooled trusted control plane and per-run sandboxes | All tenancy, recovery, egress and secret controls; production gates below |
| TARGET dedicated cells | Higher-assurance tenant execution | Separate cell identities/broker/keys/network; documented remaining shared services |
| Future dedicated regional stack | Contractual full-stack isolation or residency | Separate control/data plane, operational identity and regional dependencies |

No Kubernetes manifests, managed-service provisioning or production gateway are
provided by this documentation. Infrastructure must implement the diagrammed boundaries.

The base Compose stack keeps Redis and PostgreSQL unpublished and omits Flower.
`docker-compose.dev.yml` adds loopback-only developer ports and Flower. The
`docker-compose.prod.yml` selector activates fail-closed settings; today the API
intentionally refuses to start in that profile because `X-Tenant-ID` is not an
authenticated identity. Complete Phase 1 before using a production deployment.

## 2. Proposed SLOs and measurement

These are initial engineering targets, **not current benchmarks or commitments**.
Qualify them with representative tenant workloads before publishing an SLA.

| SLI | Initial target | Measurement boundary |
|---|---|---|
| Valid control-plane requests | 99.9% monthly availability | Authorized non-quota-rejected requests return correct non-5xx responses |
| Create/status/decision API latency | p95 below 300 ms, p99 below 1 s | Gateway receipt to durable response; excludes provider execution |
| Accepted work starts | p95 below 5 s in normal capacity | Admission commit to first authorized worker lease; separate overload metric |
| User-visible event freshness | p95 below 1 s | Durable event commit to delivery to a connected authorized subscriber |
| Approval worker occupancy | No worker held while awaiting human | Zero executing worker/sandbox allocation required solely by pause |
| Supported idempotent effect replay | Zero duplicate business effects in qualification | Crash/redelivery matrix, stable destination effect key |
| Tenant-boundary violations | Zero accepted | Release gate and incident policy, not a statistical error budget |
| Data recovery | RPO at most 5 min; RTO at most 60 min | Quarterly measured restore/failover drill for durable state |
| Sandboxed cancellation | Stop dispatch immediately; sandbox termination p95 below 10 s | Decision commit to fenced work and verified sandbox exit |

Provider latency and unavailability are reported separately but still appear in
end-to-end customer experience. Do not hide dependency failure by returning a
success-shaped fallback. Report queue age, rejection, failure and uncertainty explicitly.

## 3. Capacity and fair scheduling

For steady state, use Little's law as a starting approximation:

```text
orchestrator concurrency ~= model-step arrival rate * average model-step occupancy
tool concurrency         ~= tool-call arrival rate * average tool occupancy
sandbox capacity         ~= active executing run count (not all paused runs)
SSE capacity             ~= connected authorized subscribers * per-stream resource use
```

Example only: 2 model steps/s at 4 seconds mean occupancy needs roughly 8 active
orchestrator slots before headroom/retry reserve. This is not proof the default
8-process worker can sustain any specific throughput.

* Reserve tenant concurrency before dispatch and apply weighted fair scheduling.
* Bound tools per model response, input/output tokens, artifacts, egress bytes,
  wall time and connected streams; a step cap alone does not bound fan-out.
* Keep sandbox capacity independent from trusted orchestrator capacity.
* Set autoscaling against oldest runnable queue age, runnable depth, CPU/memory,
  provider rate limits and admission reservations, not CPU alone.
* Limit pooled database connections across all API replicas and worker tasks.
  Current API pool maximum is 15 per process; tasks open short-lived pools with
  maximum 3. Include relay/recovery/SSE metadata queries in target sizing.
* Redis `noeviction` avoids silently evicting workflow data but causes write failures
  when full. Separate broker/result/event roles where needed and alert on headroom.
* Align task visibility timeout, maximum task duration, result retention and recovery
  horizon. Current 600-second visibility and 3,600-second result expiry are defaults,
  not a complete long-running workflow recovery strategy.

Load acceptance should include multiple tenants, skewed workloads, paused runs,
provider throttling, slow tools, reconnect storms and Redis/database failover.

## 4. Failure and recovery matrix

| Failure point | CURRENT consequence | TARGET recovery / owner |
|---|---|---|
| DB insert succeeds; first enqueue fails | Run may remain `running` with no task | Committed outbox intent retried; stalled admission alert |
| Secret write succeeds; create fails | Secret key remains until TTL | Admission staging cleanup; no durable raw secrets |
| Model response received; checkpoint fails | Retry may incur another model call/charge | Stable attempt record, usage uncertainty and bounded reconciliation |
| Checkpoint succeeds; chord dispatch fails | Run can remain stranded | Invocation/outbox records re-dispatched with dedupe |
| Tool succeeds; worker dies before result | Redelivery can repeat effect | Destination idempotency/receipt lookup or `unknown` for manual reconciliation |
| One chord child disappears / backend expires | Callback may never complete | Persist invocation outcomes independently; join reconciler and step deadline |
| Resume claim succeeds; process crashes before dispatch | Run now running; pending work can be stranded | Decision + state + outbox in one transaction |
| Stale aggregate/finalizer arrives | General updates have no state-version fencing | Reject old lease epoch/state version and retain audit |
| Run finalizes; thread update fails | Run/thread can disagree | Run + committed thread history transaction with thread version |
| Final DB update succeeds; event/callback scheduling fails | Missing terminal notification | Durable terminal event + outbox; replay and delivery dispatcher |
| Redis unavailable / at capacity | Broker, results, secrets and stream impacted together | Durable DB state retained; admission backpressure; rebuild projections/reconcile work |
| PostgreSQL unavailable | No authoritative progress/ownership | Fail closed; do not dispatch unverifiable work; recover leases after restore |
| Secret TTL expires during approval | Tool sees missing credential/remediation error | Refresh authorized short-lived grant at execution; explicit missing-binding status |
| Tenant revoked during execution | Current stored policy does not actively stop work | Revocation epoch, grant revoke, fenced dispatch and sandbox termination |
| Callback receiver unavailable | Retry, then log permanent failure | Persist attempts, bounded retries, DLQ, operator-triggered replay |

No recovery job may blindly resubmit side-effecting calls. It must inspect invocation
identity, effect receipt, destination capabilities and current authorization first.

## 5. Observability and operational interfaces

### Required telemetry (TARGET)

* Correlate tenant, run, step, invocation, event, delivery, cell, lease epoch and
  trace context. Use opaque IDs, never raw prompts or credentials as labels.
* Metrics: admission latency/errors, quota rejections, per-cell queue age, active
  reservations, worker crashes, lease expirations, model latency/usage/cost uncertainty,
  tool error/timeout rates, approval age, outbox age, event lag, delivery retry/DLQ,
  artifact bytes, redaction failures and sandbox cleanup age.
* Logs: structured safe event codes, verified actor/resource references, retry reason,
  policy/plugin digests and correlation IDs. Restricted diagnostics are a separate sink.
* Traces: API admission, committed outbox dispatch, model/tool calls and callback
  attempts. Async links span queue hops; do not rely on a caller-supplied trace ID
  as evidence of identity.
* Audit: membership/grant/policy changes, admission, approvals/rejections, cancellation,
  secret-grant issuance/revocation (not value), delivery replay and break-glass.

Alert on stalled runnable runs, oldest pending outbox, unknown effects, provider spend
anomalies, cross-tenant authorization denials, fingerprint incompatibility, unavailable
RLS context, unexpected network destinations and orphan sandboxes.

CURRENT has health checks, logs, trace-header propagation, SSE and optional Flower;
it does not have this full telemetry stack. Flower is a privileged operator surface,
not a customer dashboard or durable audit store.

### Probes and graceful shutdown

Separate liveness (process responds), readiness (can authorize and access required
stores), and dependency status. A provider outage should not restart every API pod.
On drain, stop accepting new leases, preserve in-flight invocation identity, checkpoint
where safe, and let a reconciler take over only after lease expiry/fencing.

## 6. Backup, disaster recovery and deletion

* Use managed PostgreSQL PITR with encrypted backups, tested restore and isolated
  restoration roles. Store plugin artifacts/policy revisions needed to interpret old runs.
* Recover from PostgreSQL authority first. Rebuild Redis projections and retry
  committed outbox work; never replay all historical external effects.
* Increment/fence execution epochs after restore so pre-disaster workers cannot write
  into the recovered timeline. Reconcile effects committed after the recovered DB point.
* Reapply retention/deletion tombstones before making a restored environment available.
* Redis AOF is useful for local persistence, not a replacement for tested backups or
  the durable event/invocation model.
* Document regional data placement, secret-key recovery, callback key rotation,
  restoration access and who can authorize customer-impacting replay.

## 7. Phased implementation and migration

Implement in dependency order. This is a plan, not a claim these features are present.

| Phase | Changes | Exit gate / rollback boundary |
|---|---|---|
| 0. Baseline qualification | Preserve current API/E2E behavior; document exposed ports, trusted plugins and data handling | Baseline tests pass; no untrusted production tenants |
| 1. Identity and tenant authority | Provider-neutral OIDC/API-key verification and registry foundation; complete membership/RBAC, tenant status, model/destination/skill policy, scoped inventory | SEC-01/02/13; legacy mode development-only; reject unverified internal routes |
| 2. Tenant-aware schema | Versioned migrations, tenant registry, composite constraints/RLS, exact cost types, agent/policy/plugin revision records | SEC-03 and migration count/ownership checks; expand/backfill/validate before contract |
| 3. Durable workflow correctness | Steps/invocations, thread claims, state versions/leases, approval batches/decisions, outbox/inbox/events, usage ledger and reconciliation | SEC-06/07/15; crash-boundary matrix and supported effect dedupe |
| 4. Execution and credentials | Static-only privileged catalog, immutable artifacts, per-run sandbox supervisor, credential broker, egress mediation, artifact service | SEC-04/05/08/09/12; no raw secrets on transport; all P0 controls complete |
| 5. Service operation | Signed callbacks/DLQ, replay/gap semantics, quotas/fairness, retention, telemetry, backups and cancellation | SEC-10/11/14; SLO load test and restore drill |
| 6. Dedicated cells / launch | Placement fencing, cell credentials/keys, network isolation, canary tenants, operator runbooks and support policy | Full security acceptance matrix, isolation/failover test, explicit go-live review |

Production multi-tenancy is gated by the whole acceptance set, not merely phase 1.
Trusted internal demonstrations can continue in the isolated development profile.

Three product tracks run alongside these phases: developer platform (from phase 0),
multi-agent collaboration (after phase 3 primitives) and the open marketplace (code
plugins only after phase 4 gates). Their items, dependencies and gates (SEC-16 to SEC-26)
are tracked in the living [roadmap](../roadmap.md).

### Data migration procedure

1. Back up and measure existing rows, tenant strings, ownership consistency and
   active/paused runs. Build an approved mapping from legacy tenant strings to
   canonical tenant IDs. Do not auto-assign unknown tenants to one default customer.
2. Add nullable target fields/tables and indexes using versioned, reviewed migrations.
   Backfill mappings and revision references in bounded batches.
3. Detect orphan runs, mismatched thread/run tenants, concurrent thread writers,
   missing usage and incomplete pending batches. Quarantine ambiguity for operator
   resolution; do not fabricate principals/approvers or historical usage.
4. Existing runs lack authenticated submitter and exact plugin/approval provenance.
   Prefer drain-to-terminal on the legacy deployment before cutover. A paused run
   requires explicit reauthorization/reapproval under a known target revision.
5. Add/validate composite constraints and RLS; test pooled connections and
   maintenance roles. Enable verified-identity paths before exposing customers.
6. Dual-read/shadow-check safe metadata if needed, but never dual-dispatch tool work.
   Route each run to exactly one fenced execution version/cell.
7. Cut admission to the target after canary gates pass. Keep legacy readers for
   explicitly marked historical runs; expose provenance gaps rather than inventing it.
8. Remove old raw secret keys/task copies after their replay/retention horizon and
   disable legacy brokers/workers. Apply object/backup retention policy.
9. Contract obsolete columns/interfaces only after all supported clients migrate.

### Compatibility and rollback

* Preserve baseline `/v1` until authenticated clients and a versioned event/state
  contract are ready. Extend CLI/TUI credential handling and new states in the same release.
* Requiring authentication, dropping default tenancy and rejecting formerly
  unrestricted models/plugins are intentional changes with release notes and migration tools.
* Current run IDs can be retained as public aliases to new internal keys.
* Do not reinterpret old Redis entry IDs as new durable event sequences.
* Roll back admission routing/configuration before irreversible schema contraction.
  Keep expanded schemas backward-compatible during a defined window.
* Never roll back to header-only authorization for production. A safe rollback may
  mean admission is disabled while reads and operator recovery remain available.
* Fence new workers before any rollback/replay. Re-running an old deployment against
  target jobs or credentials is not a supported rollback strategy.

## 8. Verification plan and evidence

### Existing baseline

The root [test guide](../../README.md#tests) provides commands for unit tests and the
Docker-backed deterministic E2E stack. Relevant existing coverage includes:

| Test source | Baseline purpose |
|---|---|
| [Core tests](../../tests/test_core.py) | Model routing, JSON handling, secret/URL checks, tools and task helpers |
| [Skill policy tests](../../tests/test_skill_policy.py) | Allowlists, readiness, hidden skills and registry drift |
| [Plugin tests](../../tests/plugins/) | Discovery, pack behaviors and mocked integrations |
| [Client/TUI tests](../../tests/cli/) | Session, reconnect, approvals and UI behaviors |
| [E2E driver](../../tests/e2e/run_e2e.py) | Chords/queues, approvals, rejection, ownership, budgets, callbacks and worker config |

These existing tests do not verify target IdP/RLS/sandbox/outbox features.

### Target release qualification

1. Unit: authorization decisions, exact-batch digests, state transition matrix,
   policy narrowing/revocation, redaction and reservation/ledger arithmetic.
2. Database integration: RLS/constraints/pool context, CAS race tests, unique
   admission and inbox records, atomic state/outbox/usage commits.
3. Connector contracts: HTTP/MCP schema/version behavior, signature bytes,
   destination idempotency and ambiguous timeout handling.
4. Fault injection: terminate the exact test worker at each matrix boundary, delay/
   duplicate tasks, expire backend results, fail database/broker and rotate credentials.
5. Security: all SEC-01 through SEC-15 in the [threat model](threat-model.md).
6. Load: SLO percentiles and tenant fairness under representative skew/failure.
7. Operations: restore/PITR, cell failover/fencing, cancellation cleanup, tenant
   offboarding and callback replay with signed evidence.

Record workload, configuration, percentiles, failure counts and residual exceptions
for each release. No release should be approved solely because a diagram renders or
the happy-path agent produces a plausible answer.
