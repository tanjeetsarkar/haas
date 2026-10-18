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
`docker-compose.prod.yml` selector activates fail-closed settings. Phase 1 identity
and Phase 2 RLS/revision/thread foundations are implemented, but this is not a GA
deployment recipe: sandbox, egress, durable workflow and operational gates remain.
The API requires complete HTTPS OIDC, a production encryption key, a qualified
non-owner database login and completed legacy cutover.

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

The first Phase 2 expansion migration preserves the current text run/thread IDs, adds
nullable target fields without scanning or rewriting existing rows, and keeps writes
working through synchronization triggers. `legacy_tenant_mappings` is the explicit,
principal-referenced mapping source for tenant strings that do not exactly equal a
registered tenant UUID or slug. Use the operator CLI below: it verifies the supplied
principal has an active tenant-administrator membership, refuses conflicting selectors
and appends an audit event atomically. Direct owner SQL bypasses that workflow and is
not proof of approval; retain an independently approved migration manifest.

Backfill in bounded batches after mappings are approved. For example, one thread batch
can use `WITH batch AS (SELECT thread_id FROM threads WHERE thread_key IS NULL OR
updated_at_ts IS NULL OR (tenant_uuid IS NULL AND
resolve_haas_tenant_uuid(tenant_id) IS NOT NULL) ORDER BY thread_id LIMIT 1000 FOR
UPDATE SKIP LOCKED) UPDATE threads AS t SET tenant_id = t.tenant_id FROM batch WHERE
t.thread_id = batch.thread_id`. Apply the equivalent pattern to runs after thread
batches, selecting rows with missing keys/timestamp/numeric shadows, resolvable tenant
strings, or a now-resolvable thread key. Commit each batch and repeat until no rows
remain eligible. Unknown tenant strings and orphan/mismatched thread references remain
visible in `phase2_schema_backfill_status` for quarantine; never map them to a default
tenant. Check its state counts before backfill writes because `NOT VALID` state/cost
constraints still enforce new or updated rows. Migration 0008 enables forced RLS while
legacy columns remain nullable; only development can use the explicit legacy policy.
Cutover validates ownership constraints and makes canonical/shadow fields required.
Production runtime startup refuses the pre-cutover schema.

### Phase 2 operator runbook

Operate on a backup-qualified maintenance window with admission stopped. The CLI is
operator-only and trusts possession of the migration login; `--actor` is a provenance
reference, not bearer authentication. Never put that credential in runtime services.

1. Inventory and drain **both running and paused** work on the old deployment; resolve
   or cancel paused decisions explicitly. Stop old workers before advancing. Migration
   0007 deliberately refuses active/paused legacy runs rather than inventing provenance.
2. Run migrations as the schema owner, with permission to create the two runtime group
   roles (or have the DBA provision their exact safe definitions beforehand):

   ```bash
   POSTGRES_DSN="$MIGRATION_POSTGRES_DSN" alembic upgrade head
   ```

3. Provision distinct logins through your secret manager/DBA. Example role membership
   SQL (supply passwords separately, never in source control):

   ```sql
   CREATE ROLE haas_api LOGIN NOSUPERUSER NOBYPASSRLS IN ROLE haas_tenant_app;
   CREATE ROLE haas_executor LOGIN NOSUPERUSER NOBYPASSRLS IN ROLE haas_execution_service;
   ```

   Neither login may own protected tables or inherit the schema owner. The API must
   **not** inherit `haas_execution_service`. Its policies are tenant-scoped.
   Execution and maintenance deliberately have explicit cross-tenant policies with
   narrow grants, not `BYPASSRLS`; the schema owner has administrator policies for
   migration/bootstrap. Principals are a global identity directory. The canonical
   tenant resolver uses a fixed search path and grants execution only to runtime
   groups. Do not grant these groups operator mapping/quarantine access.
4. Bootstrap registered tenants/principals through the trusted bootstrap process, then
   approve mappings to real tenants. Set `MIGRATION_POSTGRES_DSN` in the operator's
   environment. Examples, with reviewed identifiers supplied by the operator:

   ```bash
   python -m app.phase2_migration report
   python -m app.phase2_migration map --legacy old-tenant \
     --tenant "$TENANT_UUID" --actor "$ADMIN_PRINCIPAL_UUID" \
     --reason "Approved migration manifest reference"
   python -m app.phase2_migration backfill --batch-size 1000
   python -m app.phase2_migration report
   ```

   Repeat bounded batches until eligible counts are zero. Each batch is transactional,
   advisory-locked and uses `SKIP LOCKED`; threads precede runs. Mapping verifies active
   administrator membership. It does not fabricate historical submitters or revisions.
   Existing public IDs/selectors remain aliases; canonical ownership permits authenticated
   users to discover and continue mapped history without rewriting those aliases.
5. Review unresolved ownership, schema and state counts. Invalid rows must not be silently
   reassigned. Quarantine terminal runs before their referencing threads, if approved:

   ```bash
   python -m app.phase2_migration quarantine --table runs --id "$RUN_ID" \
     --actor "$OPERATOR_PRINCIPAL_UUID" --reason "Reviewed orphan manifest reference"
   ```

   Only terminal runs and unreferenced threads can be quarantined. Original JSON,
   reason and operator reference are retained in `phase2_quarantine`, which is not
   tenant-readable. It may contain raw prompts, history or outputs: restrict and
   encrypt operator backups/exports and define approved retention/deletion separately.
   The Phase 5 retention service is not implemented. Do not downgrade away this archive.
6. With all admissions stopped and active/paused runs absent:

   ```bash
   python -m app.phase2_migration cutover --admission-stopped
   python -m app.phase2_migration report
   ```

   Cutover locks runs/threads, refuses unresolved counts, validates composite ownership
   and state/cost constraints, requires canonical keys/TIMESTAMPTZ/NUMERIC shadows and
   converts authoritative cost columns to NUMERIC in one transaction. It is restartable
   but may need a maintenance window for validation/type conversion. Exact cost storage
   is not a reservation/settlement ledger.
7. Configure `HAAS_API_POSTGRES_DSN` for the app login and
   `HAAS_WORKER_POSTGRES_DSN` for execution/maintenance. The production Compose migration
   service alone receives the owner `MIGRATION_POSTGRES_DSN`; its operator/bootstrap
   commands can use the same environment. Set OIDC/encryption settings separately.
   Start the API, orchestrator, tool worker and **maintenance** service with the
   production overlay, verify `/readyz`, then reopen admission after canary journeys.
   The production API does not run cross-tenant maintenance itself.

Qualify against a **disposable** PostgreSQL 16 database with all migrations applied:
`HAAS_TEST_POSTGRES_DSN=... pytest -q tests/test_phase2_database.py`. The tests deliberately
create database roles and perform schema cutover; never point them at customer data.

### Compatibility and rollback

* Preserve baseline `/v1` until authenticated clients and a versioned event/state
  contract are ready. Extend CLI/TUI credential handling and new states in the same release.
* Requiring authentication, dropping default tenancy and rejecting formerly
  unrestricted models/plugins are intentional changes with release notes and migration tools.
* Current run IDs can be retained as public aliases to new internal keys.
* Do not reinterpret old Redis entry IDs as new durable event sequences.
* Roll back admission routing/configuration before irreversible schema contraction.
  Keep expanded schemas backward-compatible during a defined window.
  The Phase 2 schema migration refuses downgrade while approved tenant-mapping rows
  remain; export and retain the reviewed mapping manifest before an explicitly
  authorized rollback. Migration 0007 also refuses downgrade while agent revisions or
  quarantine records exist. The empty-schema round trip is tested, not permission to
  discard production provenance. After production cutover prefer forward fixes.
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

Identity/API tests and the Phase 2 PostgreSQL suite now cover verified identity, RLS,
pool cleanup, immutable revisions, thread races and operator cutover. Sandbox/outbox,
destination effects and the rest of the target qualification remain outstanding.

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
