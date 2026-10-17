# Data model and entity-relationship diagrams

[Design index](README.md) | [SDD](sdd.md) | [Data flows](data-flows.md)

## 1. Physical relational model (CURRENT)

Authoritative DDL is [SCHEMA_SQL](../../app/core/store.py). There are exactly two
application tables. JSON arrays do not imply separate normalized entities.

```mermaid
erDiagram
    threads ||..o{ runs : "logical thread_id link; no FK"
    threads {
        TEXT thread_id PK
        TEXT tenant_id
        JSONB messages_json
        DOUBLE_PRECISION updated_at
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
        DOUBLE_PRECISION total_cost_usd
        INTEGER max_steps
        DOUBLE_PRECISION max_cost_usd
        INTEGER step_count
        JSONB fingerprints_json
        TEXT webhook_url "nullable"
        DOUBLE_PRECISION created_at
        DOUBLE_PRECISION updated_at
        JSONB skill_allowlist_json "nullable"
        TEXT registry_fingerprint "nullable"
    }
```

The dashed relationship is logical only: the current database does **not** enforce a
foreign key between runs and threads or tenant equality across those records.
There is no tenants, users, approvals, events, invocations, artifacts or usage-ledger table.

### Current column semantics

| Fields | Constraints / meaning |
|---|---|
| IDs | Text primary keys; globally unique thread/run IDs, not composite tenant keys |
| `tenant_id`, `thread_id` on runs | Required text; ownership checked by application paths |
| `status` | Required free text; no database state enum/check constraint |
| `messages_json`, `messages_checkpoint_json` | Required JSONB, default empty array; full message snapshots |
| `active_skills_json`, `fingerprints_json` | Required JSONB arrays; stored fingerprint history limited to last 20 on checkpoint |
| `pending_tool_calls_json` | Nullable JSONB containing entire paused batch |
| `skill_allowlist_json` | Added by startup `ALTER`; NULL means unrestricted, empty array means no domain skills |
| `registry_fingerprint` | Added by startup `ALTER`; drift diagnostic, not a pinned artifact or signature |
| `total_cost_usd`, `max_cost_usd` | Double precision, defaults 0 and 0.30; not an exact monetary ledger |
| `max_steps`, `step_count` | Defaults 15 and 0; persisted step count can lag terminal reported step |
| `last_step_had_error` | Required boolean, default false |
| Timestamps | Double precision Unix seconds, set by application |
| Optional text | `result_text`, `webhook_url`, `registry_fingerprint` |
| Index | `runs_thread_idx(thread_id)`; no explicit tenant/status index or RLS |

Startup serializes schema creation with a PostgreSQL advisory transaction lock.
It is not a versioned migration framework. Finalization updates the run and, for
completion, thread history using separate statements without an explicit surrounding
transaction. Concurrent runs can overwrite whole thread histories.

## 2. Nonrelational state (CURRENT)

| Location / key | Shape / purpose | Lifetime / guarantee |
|---|---|---|
| Redis DB 0 `haas:secrets:{run_id}` | JSON dictionary of caller secret values | Default 3,600-second SETEX; re-supply replaces dictionary and resets TTL; cleared at finalization |
| Redis DB 0 `haas:stream:{run_id}` | Stream entries: `event_type`, `payload_json` | Approximate MAXLEN, default 1,000; expiry reset to 86,400 seconds on every publish |
| Redis DB 0 `haas:lock:{key}` | Generic NX lock helper | Default 15 seconds; helper exists but is not a general fenced run lease |
| Redis DB 1 Celery transport | Serialized JSON tasks / chord signatures | Redis visibility timeout 600 seconds; at-least-once/redelivery exposure |
| Redis DB 2 Celery result backend | Tool results, chord bookkeeping | Configured result expiry 3,600 seconds |
| Per-process registry | Plugins, schemas, skills, handlers, diagnostics | Startup snapshot; local fingerprint |
| Shared scratchpad volume | Oversized observations named from tool/call ID | No implemented tenant prefix, object authorization, expiry or cleanup policy |

Redis AOF is enabled in Compose. A value called "ephemeral" can still enter Redis
persistence, broker payloads, result metadata, monitoring or backups. Clearing one
secret key does not prove erasure of all copies.

## 3. Proposed tenancy and configuration ERD (TARGET)

These are logical entities for migration design, **not existing tables or executable
migrations**. All tenant-owned relationships below must include `tenant_id`.
Primary IDs are shown for readability; composite key requirements follow the diagrams.

```mermaid
erDiagram
    PRINCIPAL ||--o{ MEMBERSHIP : has
    TENANT ||--o{ MEMBERSHIP : grants
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
        string status
        string region
        bigint revocation_epoch
        string retention_class
        timestamptz created_at
    }
    PRINCIPAL {
        uuid principal_id PK
        string issuer
        string subject
        string kind
        string status
    }
    MEMBERSHIP {
        uuid tenant_id PK,FK
        uuid principal_id PK,FK
        string role PK
        string status
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
