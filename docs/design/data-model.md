# Data model and entity-relationship diagrams

[Design index](README.md) | [SDD](sdd.md) | [Data flows](data-flows.md)

## 1. Physical relational model (CURRENT)

The current schema is established by [Alembic migrations](../../migrations/versions/).
It includes the run store, Phase 1 identity/policy registry and Phase 2 tenant keys,
agent revisions, thread fencing and RLS. Expanded fields remain nullable until the
explicit operator cutover; production runtime startup refuses that intermediate state.
JSON arrays do not imply separate normalized entities.

```mermaid
erDiagram
    threads ||--o{ runs : "tenant-composite ownership"
    tenants ||--o{ agent_revisions : versions
    agent_revisions ||--o{ runs : pins
    tenant_policy_revisions ||--o{ agent_revisions : authorizes
    tenants ||--o{ memberships : grants
    principals ||--o{ memberships : receives
    tenants ||--o{ tenant_api_keys : scopes
    principals ||--o{ tenant_api_keys : owns
    tenants ||--o{ audit_events : records
    principals ||--o{ audit_events : acts
    tenants ||--o{ tenant_policy_revisions : versions
    tenant_policy_revisions ||--o{ runs : pins
    tenant_policy_revisions ||--o| tenants : active_revision
    threads {
        TEXT thread_id PK
        TEXT tenant_id
        JSONB messages_json
        DOUBLE_PRECISION updated_at
        UUID tenant_uuid FK
        UUID thread_key UK
        TIMESTAMPTZ updated_at_ts
        BIGINT history_version
        TEXT active_run_id FK "nullable"
        TEXT parent_thread_id FK "nullable"
        BIGINT parent_history_version "nullable"
    }
    runs {
        TEXT run_id PK
        TEXT thread_id
        TEXT tenant_id
        TEXT traceparent
        TEXT status
        TEXT primary_model
        TEXT planner_model
        JSONB active_skills_json
        JSONB pending_tool_calls_json "nullable"
        JSONB messages_checkpoint_json
        BOOLEAN last_step_had_error
        TEXT result_text "nullable"
        NUMERIC total_cost_usd "double until cutover"
        INTEGER max_steps
        NUMERIC max_cost_usd "double until cutover"
        INTEGER step_count
        JSONB fingerprints_json
        TEXT webhook_url "nullable"
        DOUBLE_PRECISION created_at
        DOUBLE_PRECISION updated_at
        JSONB skill_allowlist_json "nullable"
        TEXT registry_fingerprint "nullable"
        UUID submitted_by_principal_id "nullable"
        UUID policy_revision_id "nullable"
        UUID tenant_uuid FK
        UUID run_key UK
        UUID thread_key FK
        TIMESTAMPTZ created_at_ts
        TIMESTAMPTZ updated_at_ts
        NUMERIC total_cost_usd_exact
        NUMERIC max_cost_usd_exact
        UUID agent_revision_id FK "nullable"
        BIGINT thread_history_version "nullable for historical rows"
    }
    agent_revisions {
        UUID tenant_id PK,FK
        UUID revision_id PK
        TEXT agent_name UK_part
        BIGINT version UK_part
        TEXT state
        JSONB configuration
        TEXT digest
        UUID policy_revision_id FK
        TEXT registry_fingerprint
        UUID created_by FK
        TIMESTAMPTZ created_at
        TIMESTAMPTZ published_at "nullable"
    }
    tenants {
        UUID tenant_id PK
        TEXT slug UK
        TEXT status
        TEXT region
        BIGINT revocation_epoch
        TIMESTAMPTZ credential_valid_after
        TEXT retention_class
        UUID active_policy_revision_id "nullable"
    }
    principals {
        UUID principal_id PK
        TEXT issuer UK_part
        TEXT subject UK_part
        TEXT kind
        TEXT status
        TIMESTAMPTZ credential_valid_after
    }
    memberships {
        UUID tenant_id PK,FK
        UUID principal_id PK,FK
        TEXT role PK
        TEXT status
    }
    tenant_api_keys {
        UUID key_id PK
        UUID tenant_id FK
        UUID principal_id FK
        TEXT key_prefix UK
        BYTEA key_hash UK
        TEXT name
        TIMESTAMPTZ expires_at
        TIMESTAMPTZ revoked_at
        TIMESTAMPTZ last_used_at
    }
    tenant_policy_revisions {
        UUID policy_revision_id PK
        UUID tenant_id FK
        BIGINT version
        TEXT digest
        JSONB rules
        UUID created_by_principal_id FK
        TIMESTAMPTZ created_at
    }
    audit_events {
        UUID event_id PK
        UUID tenant_id FK
        UUID actor_principal_id FK
        TEXT action
        TEXT target_type
        TEXT target_id
        UUID request_id
        JSONB details
        TIMESTAMPTZ created_at
    }
```

Tenant-composite foreign keys now enforce run/thread aliases, canonical thread keys,
policy/revision pins and branch ancestry; historical `NOT VALID` constraints are validated
at cutover. The active policy pointer is tenant-aware.
Policy revisions and audit events are protected from updates and deletes by database
triggers. Agent bodies are immutable from draft creation; validation and publication
are guarded forward-only transitions. Phase 3 durable workflow tables are described
[below](#phase-3-durable-workflow-tables); artifact tables do not yet exist.

### Current column semantics

| Fields | Constraints / meaning |
|---|---|
| IDs | Stable public text aliases plus immutable internal UUID keys and tenant-composite ownership constraints |
| `tenant_id`, `thread_id` on runs | Legacy selectors/aliases retained; canonical `tenant_uuid` resolves registered UUID/slug or approved mapping |
| Registry statuses | Check constraints on tenant, principal and membership lifecycle; API roles are membership rows |
| Tenant policy | Versioned JSONB rules with a SHA-256 digest; model providers/models, plugin digest+skill grants, exact webhook hosts, maximum budgets and retention class |
| Active policy | Nullable tenant pointer with a composite FK preventing cross-tenant activation; absent policy denies authenticated run admission |
| API credentials | Tenant-scoped key prefix and SHA-256 hash; raw key is never stored |
| `status` on runs | Check constraint (migration 0009): `queued`, `running`, `input_required`, `cancelling`, `completed`, `failed`, `cancelled` |
| Workflow fencing on runs | `state_version`, `lease_epoch`, `lease_owner`, `lease_expires_at`, `next_action`, `recovery_attempts`, `event_sequence`; every transition is a CAS on version + epoch |
| Budgets on runs | `deadline_at`, `budget_reserved_usd` (NUMERIC), `max_tool_invocations`, `tool_invocations_used`; `parent_run_id` reserved for Track A child runs |
| `messages_json`, `messages_checkpoint_json` | Required JSONB, default empty array; full message snapshots |
| `active_skills_json`, `fingerprints_json` | Required JSONB arrays; stored fingerprint history limited to last 20 on checkpoint |
| `pending_tool_calls_json` | Nullable JSONB containing entire paused batch |
| `skill_allowlist_json` | Alembic-managed; NULL is unrestricted only for legacy/development runs; authenticated runs persist the effective policy grant, including an empty array |
| `registry_fingerprint` | Registry snapshot pin on agent revisions and admission; not a content-addressed artifact or signature |
| `submitted_by_principal_id` | Nullable FK added by migration 0003; null on legacy/development-header runs |
| `policy_revision_id` | Nullable FK added by migration 0004; authenticated runs pin the immutable policy revision used at admission |
| `audit_events` | Migration 0005 stores tenant-scoped actor, action, target, request ID and minimized JSON details; rows are append-only |
| `total_cost_usd`, `max_cost_usd` | NUMERIC after cutover, synchronized NUMERIC shadows during expansion; finite nonnegative settled cost and positive budget; reservations live in `usage_ledger` |
| `max_steps`, `step_count` | Defaults 15 and 0; per-attempt detail lives in `run_steps` |
| `last_step_had_error` | Required boolean, default false |
| Timestamps | Legacy Unix seconds retained alongside synchronized TIMESTAMPTZ fields, required after cutover |
| Optional text | `result_text`, `webhook_url`, `registry_fingerprint` |
| Indexes and RLS | Tenant/time indexes, one-active-run partial unique index, forced RLS and transaction-local tenant context; runtime logins cannot own protected tables |

Admission locks the thread, captures `history_version` and claims one active run in the
same transaction as run insertion. Successful completion commits run state, history,
version increment and claim release atomically. Stale completion is rejected without
emitting a successful worker completion; cancellation/reaping also releases the claim.
Branches copy committed history at an explicitly checked version and record ancestry;
they do not reconstruct arbitrary older snapshots.

`legacy_tenant_mappings` and `phase2_quarantine` are operator-only tables, not tenant API
resources. Quarantine preserves original historical JSON for reviewed resolution and may
contain sensitive content. Runtime groups have no grants on them. The schema-owner has
explicit administrator policies; execution/maintenance has deliberately cross-tenant,
narrow table grants, while the API login has tenant-scoped policies only.

### Phase 3 durable workflow tables

Migration `0009_durable_workflow` adds run-child tables keyed by the run's tenant alias
and `run_id`, with `tenant_uuid` copied from the run, forced RLS for the API login and
narrow grants for the execution-service login. `run_events` and `approval_decisions` are
append-only (update/delete triggers).

```mermaid
erDiagram
    runs ||--o{ run_steps : attempts
    runs ||--o{ tool_invocations : invokes
    runs ||--o{ approval_batches : pauses
    approval_batches ||--o| approval_decisions : "decided once"
    approval_batches ||--o{ tool_invocations : covers
    runs ||--o{ run_events : "sequence"
    runs ||--o{ outbox_messages : intents
    runs ||--o{ consumer_inbox : dedup
    runs ||--o{ usage_ledger : "reserve/settle"
    runs ||--o| admission_requests : "Idempotency-Key"
```

| Table | Purpose / key constraints |
|---|---|
| `admission_requests` | Tenant + principal + operation + `Idempotency-Key` → request digest and stored receipt; same key/different digest is a conflict |
| `run_steps` | One row per step attempt with `lease_epoch`, model, reserved vs settled cost, tokens and status (`model_running`, `abandoned`, …) |
| `tool_invocations` | Per call: `arguments_digest`, unique `effect_key`, queue, status (`awaiting_approval` → `approved`/`denied` → `dispatched` → `succeeded`/`failed`/`unknown`/`cancelled`), attempt, epoch, batch link |
| `approval_batches` | Exact pending calls, `batch_digest`, `run_state_version`, `expires_at`, status `pending`/`approved`/`rejected`/`expired`/`cancelled` |
| `approval_decisions` | Immutable decision, attributed actor, reason, request/idempotency IDs |
| `run_events` | Per-run gapless `sequence`, `event_id`, `schema_version`, `type`, payload; SSE and `/events` replay source |
| `outbox_messages` | Task intents (tool intents carry signed envelopes) committed with transitions; unique `dedup_key`; relay claims with `SKIP LOCKED`, bounded attempts (`OUTBOX_MAX_ATTEMPTS`) |
| `consumer_inbox` | `(consumer, tenant_id, message_id)` deduplication of at-least-once deliveries; pruned after `INBOX_RETENTION_SEC` |
| `usage_ledger` | Unique `(tenant_id, source_attempt_key, unit)`; finite NUMERIC quantity/cost; `reserved`/`settled`/`released`/`uncertain` settlement with pricing revision |

The 0009 downgrade refuses while non-terminal runs exist and restores the Phase 2 status
constraint; drain work first.

## 2. Nonrelational state (CURRENT)

| Location / key | Shape / purpose | Lifetime / guarantee |
|---|---|---|
| Redis DB 0 `haas:secrets:{run_id}` | JSON dictionary of caller secret values | Default 3,600-second SETEX; re-supply replaces dictionary and resets TTL; cleared at finalization |
| Redis DB 0 `haas:wake:{run_id}` | Advisory SSE wakeup entries (no event data) | Approximate MAXLEN 50, 86,400-second expiry; loss only delays SSE polling, events live in `run_events` |
| Redis DB 0 `haas:lock:{key}` | Generic NX lock helper | Default 15 seconds; helper exists but is not a general fenced run lease |
| Redis DB 1 Celery transport | Outbox-relayed tasks; tool tasks carry signed execution envelopes | Redis visibility timeout 600 seconds; redelivery deduplicated by `consumer_inbox` and fencing |
| Redis DB 2 Celery result backend | Not used for workflow correctness; outcomes are recorded on `tool_invocations` | Configured result expiry 3,600 seconds |
| Per-process registry | Plugins, schemas, skills, handlers, diagnostics | Startup snapshot; local fingerprint |
| Shared scratchpad volume | Oversized observations named from tool/call ID | No implemented tenant prefix, object authorization, expiry or cleanup policy |

Redis AOF is enabled in Compose. A value called "ephemeral" can still enter Redis
persistence, broker payloads, result metadata, monitoring or backups. Clearing one
secret key does not prove erasure of all copies.

## 3. Proposed tenancy and configuration ERD (TARGET)

These are logical target entities, not a literal migration specification. Phase 1/2
implement the registry, revisions and core ownership described above; later-phase
workflow, artifact and ledger entities remain targets. All tenant-owned relationships
must include `tenant_id`.
Primary IDs are shown for readability; composite key requirements follow the diagrams.

```mermaid
erDiagram
    PRINCIPAL ||--o{ MEMBERSHIP : has
    TENANT ||--o{ MEMBERSHIP : grants
    PRINCIPAL ||--o{ TENANT_API_KEY : authenticates
    TENANT ||--o{ TENANT_API_KEY : scopes
    TENANT ||--o{ POLICY_REVISION : versions
    TENANT ||--o{ AGENT : owns
    AGENT ||--|{ AGENT_REVISION : versions
    POLICY_REVISION ||--o{ AGENT_REVISION : constrains
    AGENT_REVISION ||--o{ CAPABILITY_GRANT : permits
    PLUGIN_REVISION ||--o{ CAPABILITY_GRANT : pins
    TENANT ||--o{ SECRET_BINDING : owns
    EXECUTION_CELL ||--o{ TENANT_PLACEMENT : hosts
    TENANT ||--|{ TENANT_PLACEMENT : assigned
    TENANT {
        uuid tenant_id PK
        string slug UK
        string status
        string region
        bigint revocation_epoch
        timestamptz credential_valid_after
        string retention_class
        timestamptz created_at
    }
    PRINCIPAL {
        uuid principal_id PK
        string issuer
        string subject
        string kind
        string status
        timestamptz credential_valid_after
    }
    MEMBERSHIP {
        uuid tenant_id PK,FK
        uuid principal_id PK,FK
        string role PK
        string status
    }
    TENANT_API_KEY {
        uuid key_id PK
        uuid tenant_id FK
        uuid principal_id FK
        string key_prefix UK
        bytes key_hash UK
        string name
        timestamptz expires_at
        timestamptz revoked_at
        timestamptz last_used_at
    }
    POLICY_REVISION {
        uuid policy_revision_id PK
        uuid tenant_id FK
        bigint version
        string digest
        jsonb rules
        timestamptz created_at
    }
    AGENT {
        uuid agent_id PK
        uuid tenant_id FK
        string name
        string status
    }
    AGENT_REVISION {
        uuid agent_revision_id PK
        uuid tenant_id FK
        uuid agent_id FK
        uuid policy_revision_id FK
        bigint version
        string config_digest
        jsonb instructions_models_limits
    }
    PLUGIN_REVISION {
        uuid plugin_revision_id PK
        string plugin_id
        string version
        string artifact_digest
        string signature_reference
        jsonb static_catalog
    }
    CAPABILITY_GRANT {
        uuid tenant_id PK,FK
        uuid agent_revision_id PK,FK
        uuid plugin_revision_id PK,FK
        string skill_name PK
        jsonb permitted_tools_destinations
    }
    SECRET_BINDING {
        uuid secret_binding_id PK
        uuid tenant_id FK
        string logical_name
        string provider_reference
        string status
    }
    EXECUTION_CELL {
        uuid cell_id PK
        string region
        string isolation_tier
        string status
    }
    TENANT_PLACEMENT {
        uuid tenant_id PK,FK
        bigint placement_version PK
        uuid cell_id FK
        boolean is_active
    }
```

`PRINCIPAL`, `PLUGIN_REVISION` and `EXECUTION_CELL` are global platform entities.
Tenant access to a plugin is conveyed by a grant, not by global catalog visibility.
Principal identity is unique on `(issuer, subject)`; a human may hold memberships in
multiple tenants. Service credentials map to principals with tenant-scoped grants.
Secret binding rows hold references only, never raw values.

The initial registry slice is implemented by migration
[`0002_identity_registry`](../../migrations/versions/0002_identity_registry.py):
`tenants`, `principals`, `memberships`, and `tenant_api_keys`. Tenant suspension and
offboarding advance `revocation_epoch`, set `credential_valid_after`, and revoke active
API keys; reactivation requires explicit reauthorization and does not restore keys.
The API validates active tenant membership for OIDC subjects and tenant-scoped API keys;
only SHA-256 API-key digests are persisted. The one-time
[`haas-bootstrap-tenant`](../../app/bootstrap_tenant.py)
command creates a tenant and its first administrator from an exact configured OIDC
issuer/subject pair and refuses to re-bootstrap an existing slug. Initial action-level
RBAC is implemented in [`authorization.py`](../../app/core/authorization.py). Migration
[`0003_run_submitter`](../../migrations/versions/0003_run_submitter.py) records the
authenticated submitter for new runs; legacy rows remain unattributed and cannot be
approved through an authenticated request. Immutable tenant model/plugin/budget policy
and actor-scoped audit are implemented in migrations 0004/0005. Migration
[`0006_phase2_schema_expand`](../../migrations/versions/0006_phase2_schema_expand.py)
starts the additive Phase 2 expand step: existing text IDs remain as compatibility
aliases while runs and threads gain internal UUID keys, nullable canonical tenant UUIDs,
timestamp/numeric shadows, tenant-composite run/thread ownership constraints, and
not-yet-validated state/cost checks that constrain new writes.
Legacy tenant strings resolve only through an approved mapping or an exact tenant UUID
or slug match; unmapped records remain unassigned rather than defaulting to a tenant.
The operator mapping CLI verifies active tenant-administrator membership and appends
an audit event atomically. Direct owner SQL is not an authorization API or proof of approval.
The `phase2_schema_backfill_status` view reports unresolved tenant and thread ownership
counts, including unresolved legacy state values. Migrations 0007/0008 add immutable
agent revisions, atomic thread claims and forced RLS. Historical validation and required
canonical fields occur in the explicit operator cutover, which production startup requires.
See the [operator runbook](operations-migration.md#phase-2-operator-runbook).

## 4. Proposed execution and delivery ERD (TARGET)

```mermaid
erDiagram
    TENANT ||--o{ THREAD : owns
    THREAD ||--o{ RUN : contains
    AGENT_REVISION ||--o{ RUN : configures
    POLICY_REVISION ||--o{ RUN : snapshots
    EXECUTION_CELL ||--o{ RUN : executes
    PRINCIPAL ||--o{ RUN : submits
    RUN ||--o{ STEP : checkpoints
    STEP ||--o{ TOOL_INVOCATION : fans_out
    STEP ||--o{ APPROVAL_BATCH : gates
    APPROVAL_BATCH o|--o{ TOOL_INVOCATION : binds
    APPROVAL_BATCH ||--o| APPROVAL_DECISION : resolves
    PRINCIPAL ||--o{ APPROVAL_DECISION : decides
    TOOL_INVOCATION ||--o{ INVOCATION_SECRET_GRANT : authorizes
    SECRET_BINDING ||--o{ INVOCATION_SECRET_GRANT : references
    RUN ||--o{ ARTIFACT : produces
    RUN ||--o{ RUN_EVENT : emits
    RUN ||--o{ USAGE_LEDGER : meters
    RUN_EVENT ||--o{ OUTBOX_MESSAGE : triggers
    RUN_EVENT ||--o{ WEBHOOK_DELIVERY : delivers
    WEBHOOK_DELIVERY ||--o{ DELIVERY_ATTEMPT : attempts
    RUN ||--o{ AUDIT_EVENT : correlates
    RUN {
        uuid run_id PK
        uuid tenant_id FK
        uuid thread_id FK
        uuid submitted_by FK
        uuid agent_revision_id FK
        uuid policy_revision_id FK
        uuid cell_id FK
        bigint placement_version
        string status
        bigint state_version
        bigint lease_epoch
        timestamptz lease_expires_at
        timestamptz deadline_at
        numeric budget_reserved_usd
        jsonb checkpoint
        string idempotency_key
        string request_digest
    }
    THREAD {
        uuid thread_id PK
        uuid tenant_id FK
        bigint history_version
        uuid active_run_id
        jsonb committed_messages
    }
    STEP {
        uuid step_id PK
        uuid tenant_id FK
        uuid run_id FK
        integer step_number
        integer attempt
        bigint lease_epoch
        string model_revision
        string status
        jsonb checkpoint
    }
    TOOL_INVOCATION {
        uuid invocation_id PK
        uuid tenant_id FK
        uuid step_id FK
        uuid approval_batch_id FK "nullable"
        uuid plugin_revision_id FK
        string tool_name
        string arguments_digest
        jsonb validated_arguments
        string effect_key
        string status
        integer attempt
        jsonb effect_receipt
    }
    APPROVAL_BATCH {
        uuid approval_batch_id PK
        uuid tenant_id FK
        uuid step_id FK
        bigint run_state_version
        string batch_digest
        string status
        timestamptz expires_at
    }
    APPROVAL_DECISION {
        uuid decision_id PK
        uuid tenant_id FK
        uuid approval_batch_id FK
        uuid decided_by FK
        string decision
        string batch_digest
        string idempotency_key
        timestamptz decided_at
    }
    INVOCATION_SECRET_GRANT {
        uuid tenant_id PK,FK
        uuid invocation_id PK,FK
        uuid secret_binding_id PK,FK
        string capability_digest
        timestamptz expires_at
    }
    ARTIFACT {
        uuid artifact_id PK
        uuid tenant_id FK
        uuid run_id FK
        string object_key
        string content_digest
        bigint size_bytes
        timestamptz expires_at
    }
    RUN_EVENT {
        uuid event_id PK
        uuid tenant_id FK
        uuid run_id FK
        bigint sequence_number
        string schema_version
        string event_type
        jsonb redacted_payload
        timestamptz occurred_at
    }
    OUTBOX_MESSAGE {
        uuid message_id PK
        uuid tenant_id FK
        uuid event_id FK
        string destination
        string deduplication_key
        string state
        timestamptz next_attempt_at
    }
    USAGE_LEDGER {
        uuid usage_id PK
        uuid tenant_id FK
        uuid run_id FK
        string source_attempt_key
        string unit
        numeric quantity
        numeric cost_usd
        string pricing_revision
        string settlement_status
    }
    WEBHOOK_DELIVERY {
        uuid delivery_id PK
        uuid tenant_id FK
        uuid event_id FK
        string endpoint_reference
        string signing_key_reference
        string status
    }
    DELIVERY_ATTEMPT {
        uuid attempt_id PK
        uuid tenant_id FK
        uuid delivery_id FK
        integer attempt_number
        integer response_status
        timestamptz next_attempt_at
    }
    AUDIT_EVENT {
        uuid audit_id PK
        uuid tenant_id FK
        uuid run_id FK "nullable for administration"
        uuid principal_id FK
        string action
        string resource_reference
        string outcome
        string content_digest
        timestamptz occurred_at
    }
```

An approval batch binds the entire proposed tool batch; invocation membership is
immutable after hashing. Multiple batches per step are allowed for future re-proposals,
but only one unresolved batch per active step is permitted. Rejection produces denial
outcomes rather than executing those invocations. An invocation can have multiple
attempts under the same effect identity; if detailed attempt history is needed, normalize
it into a child table rather than overwriting audit evidence.

### Required physical constraints

The target physical migration must implement these; diagrams alone do not enforce them:

1. Use a primary or unique key `(tenant_id, entity_id)` for each tenant-owned entity.
   A global UUID primary key may be retained only with this additional unique pair.
2. All tenant-owned child references are composite. For example,
   `RUN(tenant_id, thread_id)` references `THREAD(tenant_id, thread_id)`, and
   `TOOL_INVOCATION(tenant_id, step_id)` references `STEP(tenant_id, step_id)`.
   Apply the same rule to policy, agent, approval, artifact, event, delivery and secret links.
3. Principal references are checked against an active membership for that tenant at
   authorization time. Preserve past decision attribution when membership is revoked.
4. Unique `(tenant_id, run_id, step_number, attempt)` for step attempts;
   `(tenant_id, run_id, sequence_number)` for events; stable invocation/effect key;
   `(tenant_id, source_attempt_key, unit)` for ledger entries.
5. Unique `(tenant_id, submitted_by, operation, idempotency_key)` in a durable admission
   deduplication record (initial create operation may be materialized on RUN).
   Store request digest and response reference. Add a consumer inbox keyed by
   `(consumer, tenant_id, message_id)` to deduplicate outbox delivery.
6. Unique decision per approval batch; immutable batch digest and invocation membership.
   Different decisions with the same idempotency key conflict, not overwrite.
7. Unique active tenant placement and one active run per thread through partial unique
   indexes/transactional claims. `active_run_id` must match tenant **and thread**.
8. Validated state transitions and nonnegative numeric usage/reservation values.
   Use `NUMERIC`, not binary floating point, for settled monetary values.
9. Tenant-aware indexes starting with `tenant_id` for API list/read paths; dispatch
   indexes on pending outbox state and due time; retention indexes on expiry.
10. RLS on every tenant-owned table, including child, delivery, inbox, outbox and usage
    tables. Policy tests must exercise joins, writes, pooled connection reuse and maintenance.

UUIDs are not authorization. Target timestamps use `TIMESTAMPTZ`; identifiers,
trace IDs and provider request IDs are separate fields.

## 4a. Proposed marketplace and multi-agent ERD (TARGET)

> **Implementation status.** The tenant-owned multi-agent half is implemented by migration
> `0011_multi_agent_schema`: `runs.root_run_id`/`depth`/`delegation_id` (lineage derived from
> the parent by trigger and frozen afterwards; depth ≤ 32), `delegations` (terms immutable;
> only status/settlement/returned budget update), `agent_messages` (`hacp/1`, same-tree only,
> `consumed_at` set once), `workspaces` (one per root run) and `workspace_entry_versions`
> (append-only, `version = current + 1` CAS, path validation), and `agent_cards` (published
> revisions only). All use forced RLS with the `tenant_boundary`/`execution_service`/
> `schema_administrator` policies and are included in the production startup RLS check.
> Differences from the ERD: tree tables key runs by the tenant alias (`tenant_id`, `run_id`)
> like other workflow tables and copy `tenant_uuid` from the root run; workspace entries hold
> either bounded inline content or an artifact reference (no artifacts table exists yet).
> Marketplace tables (`PUBLISHER` … `INSTALLATION`, `CAPABILITY_GRANT`) remain TARGET for Track M.

Extends §3–§4 for the [marketplace](marketplace.md) and [multi-agent](multi-agent.md) designs.
The same composite-key, RLS and immutability constraints apply. `PUBLISHER`, `LISTING`,
`PLUGIN_REVISION` and `ADVISORY` are global; everything else is tenant-owned.

```mermaid
erDiagram
    PUBLISHER ||--o{ LISTING : owns
    LISTING ||--|{ PLUGIN_REVISION : versions
    PLUGIN_REVISION ||--o{ SCAN_REPORT : reviewed_by
    PLUGIN_REVISION ||--o{ ADVISORY : affected_by
    TENANT ||--o{ INSTALLATION : installs
    PLUGIN_REVISION ||--o{ INSTALLATION : pinned_by
    INSTALLATION ||--o{ CAPABILITY_GRANT : backs
    RUN ||--o{ RUN : parent_of
    RUN ||--o{ DELEGATION : delegates
    DELEGATION ||--|| RUN : child
    RUN ||--o{ AGENT_MESSAGE : sends
    RUN ||--o| WORKSPACE : roots
    WORKSPACE ||--o{ WORKSPACE_ENTRY_VERSION : contains
    AGENT_REVISION ||--o| AGENT_CARD : advertises
    PUBLISHER {
        uuid publisher_id PK
        string namespace UK
        string verification_status
        string verified_domain
    }
    LISTING {
        uuid listing_id PK
        uuid publisher_id FK
        string name
        string trust_tier
        string status
    }
    PLUGIN_REVISION {
        uuid plugin_revision_id PK
        uuid listing_id FK
        string version
        string artifact_digest
        string signature_reference
        string provenance_reference
        jsonb permissions
        jsonb static_catalog
        string review_status
    }
    SCAN_REPORT {
        uuid scan_id PK
        uuid plugin_revision_id FK
        string kind
        string outcome
        jsonb findings_summary
    }
    ADVISORY {
        uuid advisory_id PK
        string severity
        string action
        timestamptz published_at
    }
    INSTALLATION {
        uuid installation_id PK
        uuid tenant_id FK
        uuid plugin_revision_id FK
        string permissions_digest
        uuid consented_by FK
        timestamptz consented_at
        string status
    }
    DELEGATION {
        uuid delegation_id PK
        uuid tenant_id FK
        uuid parent_run_id FK
        uuid child_run_id FK
        string caveats_digest
        numeric budget_slice_usd
        integer depth
        timestamptz deadline_at
    }
    AGENT_MESSAGE {
        uuid message_id PK
        uuid tenant_id FK
        uuid root_run_id FK
        uuid from_run_id FK
        uuid to_run_id FK
        string kind
        string body_reference
        timestamptz consumed_at
    }
    WORKSPACE {
        uuid workspace_id PK
        uuid tenant_id FK
        uuid root_run_id FK
    }
    WORKSPACE_ENTRY_VERSION {
        uuid tenant_id PK,FK
        uuid workspace_id PK,FK
        string path PK
        bigint version PK
        uuid written_by_run_id FK
        string content_digest
        uuid artifact_id FK
    }
    AGENT_CARD {
        uuid agent_revision_id PK,FK
        uuid tenant_id FK
        jsonb card
        string exposure
    }
```

Additional constraints: `RUN` gains `parent_run_id`, `root_run_id`, `depth` and `delegation_id`
with composite tenant FKs, and a check that a child's tenant equals its root's.
`WORKSPACE_ENTRY_VERSION` writes require `version = current + 1` (CAS). An installation's
`permissions_digest` must equal the pinned revision's permissions; an expansion creates a new
installation row only after consent.

## 5. Checkpoint vs event vs audit

| Record | Authority / purpose | Mutability |
|---|---|---|
| Run checkpoint | Authoritative current execution state | Version-fenced update |
| Step/invocation | Durable dispatch and effect identity | State transitions with immutable inputs |
| Run event | Ordered user-visible state/observation | Append-only; redacted before persistence |
| Outbox/inbox | Delivery intent and consumer deduplication | Explicit delivery state |
| Usage ledger | Reservation, observation, settlement and adjustment | Append-only charges/adjustments, unique source key |
| Audit event | Who authorized/did what, to which tenant/resource | Append-only with integrity-protected export |
| Artifact | Large result not suitable for queue/message body | Immutable digest, controlled deletion |

The target is not automatically event sourcing: the checkpoint remains authoritative.
Durable events must be sufficient for observation/replay, not assumed sufficient to
reconstruct every provider response or secret.

## 6. Retention and deletion (TARGET policy defaults)

These proposed defaults require customer/legal approval before rollout:

| Data | Proposed baseline | Deletion / preservation rule |
|---|---|---|
| Raw secret value | Not stored in application DB/Redis; transient grant at most invocation lifetime | Revoke grants at completion/cancel; secret service manages authoritative lifecycle |
| Run/thread content and artifacts | 30 days after last completed activity | Tenant-configurable shorter retention; scoped deletion across all projections |
| Hot event projection | 24 hours | Durable replay tier remains available within authorized run retention |
| Delivery attempts | 30 days | Payload content follows run retention, even if delivery metadata retained |
| Security audit and metering metadata | 365 days | Minimize content; separate legal basis and tenant access rules |
| Admission/inbox dedupe | At least maximum task redelivery/replay horizon | Do not expire while old work can still replay |
| Backups | Proposed 35-day rolling encrypted window | Document delayed physical erasure and restore-time tombstone enforcement |

Tenant offboarding suspends new runs, revokes active credentials, fences execution,
exports allowed records, applies deletion/tombstones, removes projections/artifacts,
and records a deletion receipt. Legal holds must be explicit and visible to authorized
operators. Auditability is not a reason to keep raw secrets or unlimited prompts.
