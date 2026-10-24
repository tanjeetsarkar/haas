# HaaS Atlas — Software Design Description (SDD)

## 1. Architectural Alignment with HaaS Distributed

HaaS Atlas is designed as a **zero-daemon-overhead application layer** on top of the verified HaaS Distributed architecture (`docs/design/sdd.md`). Every runtime behavior maps 1-to-1 to an existing HaaS subsystem:

```
+-----------------------------------------------------------------------------------+
|                        HaaS Atlas Web Client (Next.js / React)                    |
|   [Miller-Column Graph Renderer]  [Provenance Inspector]  [HITL Approval Drawer]  |
+-----------------------------------------+-----------------------------------------+
                                          | HTTPS REST + SSE (haas_sdk_ts)
                                          v
+-----------------------------------------------------------------------------------+
|                         HaaS API Gateway (FastAPI / app/)                         |
|  - OIDC JWT / API Key Auth & RBAC (tenant_admin, submitter, approver, auditor)   |
|  - Active Tenant Policy Revision Enforcement (models, plugins, skills, budgets)   |
|  - Routes: /v1/runs, /v1/threads, /v1/agents, /v1/runs/{id}/stream, /resume       |
+-------------------+-------------------------------------------+-------------------+
                    |                                           |
     PostgreSQL 16 (Forced RLS)                        Redis (Broker + Streams)
  - runs, threads, run_events                       - Celery queues (orchestrator, tools)
  - atlas_entity_cache, atlas_layer_nodes           - AES-GCM ephemeral secrets
                    |                               - output-deltas.v1 pub/sub
                    +---------------------+---------------------+
                                          |
            +-----------------------------+-----------------------------+
            v                                                           v
+---------------------------------------+   +---------------------------------------+
|      Celery: worker-orchestrator      |   |         Celery: worker-tools          |
|  - Planner (Step 1) -> Primary Model  |   |  - read_only: true + tmpfs /tmp       |
|  - Dispatches parallel tool chords    |   |  - Enforces active skill tool unlock  |
|  - Executes aggregate_step_task       |   |  - Runs sync tools via to_thread      |
|  - HACP child run orchestration       |   |  - SSRF DNS check at call time        |
|  - Emits gapless run_events sequence  |   |  - Executes: web-research,            |
+---------------------------------------+   |    data-analyst, atlas-explorer       |
                                            +---------------------------------------+
```

---

## 2. Subsystem Design

### 2.1 Query Decomposition & Planner/Primary Model Routing
HaaS natively separates the **Step-1 Planner Model** (`HAAS_PLANNER_MODEL`) from the **Primary Execution Model** with automatic fallback if a provider rejects tool definitions on Step 1.
* **Step 1 (Ontology & Cache Planning)**: When a run starts with the `atlas-explorer` skill active, the Planner inspects the user query and thread branch context, identifies the target entity class and layer depth $L_k$, and emits a batch of `atlas_cache_lookup` tool calls for the candidate entities.
* **Step 2 (Cache-Miss Fan-Out via Celery Chord)**: In `aggregate_step_task`, if `atlas_cache_lookup` returns cache misses or expired TTLs, the Primary model dispatches parallel `web_search` and `web_fetch_page` tool calls across the `worker-tools` queue.
* **Step 3 (Normalization & Emission)**: `aggregate_step_task` collects the parallel tool outputs, invokes `atlas_emit_layer` to validate the `atlas-layer.v1` JSON contract, writes verified attributes to `atlas_cache_upsert`, and completes the run (`run_completed`).

### 2.2 Mapping Visual Layer Trees to HaaS Thread Branches
A core problem in layered exploration is **context window contamination**: if a user explores 5 hospitals at Layer 1, drills into *Tata Memorial Facilities* (Layer 2A), then *CyberKnife Pricing* (Layer 3A), and then clicks back to Layer 1 to inspect *Kokilaben Doctors* (Layer 2B), appending all of those steps into a single linear conversation thread wastes tokens and confuses the model.

HaaS already implements **versioned thread branching** (`/v1/threads` and `/branch VERSION`) with atomic thread claim/release:
1. **Root Layer ($L_1$)**: Executes on root thread $T_0$, producing committed history version $v_1$.
2. **First Drill-Down ($L_1 \rightarrow L_{2A}$)**: Before creating the run for $L_{2A}$, the client calls the HaaS Thread Branch endpoint at version $v_1$, creating branch thread $T_{2A}$ whose context contains **only** $L_1$'s committed summary.
3. **Concurrent Sibling Exploration ($L_1 \rightarrow L_{2B}$)**: Because $T_{2A}$ and $T_{2B}$ are distinct branched threads derived from $T_0@v_1$, HaaS's atomic thread claim lock (`claimed_by_run_id`) allows the user to expand **both** branches concurrently in separate runs without lock contention!

### 2.3 Multi-Entity Comparison via HACP Child Runs & Shared Workspace
When a user selects multiple entities in Layer 1 (e.g., comparing 4 hospitals simultaneously across 6 facility types), a single run with a tight step limit could exhaust its quota.
* Using HaaS's **Multi-Agent Collaboration Protocol (HACP)**, the parent orchestrator run spawns **durable child runs**—one per selected hospital—with **attenuated capabilities** (read-only `web-research` + `atlas-explorer` skills) and **budget slices**:
  $$\text{Budget}_{\text{child}} = \left\lfloor \frac{\text{Budget}_{\text{parent}} - \text{Budget}_{\text{reserve}}}{N_{\text{entities}}} \right\rfloor$$
* Each child run writes its extracted `LayerNodeModel` into HaaS's **versioned scoped workspace** using Compare-And-Swap (CAS) conflict resolution.
* The parent run joins the child runs and emits the unified `ComparisonMatrix` payload.

### 2.4 Solving the HaaS "No-Self-Approval" Rule for Consumer HITL Tasks
**Critical HaaS Codebase Constraint**: In `tanjeetsarkar/haas`, Human-in-the-Loop (HITL) approval gates strictly enforce the **no-self-approval rule**:
> *"Approvers are separated from submitters... The separate approver preserves the no-self-approval rule; use the admin/submitter key to create a run and the approver key to inspect/approve its pending action... Resumed via `POST /v1/runs/{run_id}/resume` with atomic `UPDATE ... WHERE status='input_required'`."*

If a consumer web app authenticates every browser session directly as a single HaaS principal, that principal **cannot** both submit the run (`POST /v1/runs`) and approve the paused HITL gate (`POST /v1/runs/{run_id}/resume`)!

**Lean Architectural Solution (Dual-Principal Session Delegation)**:
1. **Service Submitter Principal (`role=submitter`)**: The Atlas Web Gateway submits exploration and task runs using a service-scoped `submitter` API key (or user OIDC token mapped to `submitter`), tagging the run metadata with `end_user_sub` (the authenticated human user's OIDC subject).
2. **Human Approver Principal (`role=approver`)**: When a run hits an approval-gated tool (e.g., `atlas_dispatch_inquiry`) and transitions to `status='input_required'`, the browser renders the **Immutable Consent & Approval Drawer** showing the exact batch digest (`action_digest`), target URL, and payload diff from the `hitl_pending` event.
3. **Step-Up Human Consent**: When the human user clicks **"Confirm & Execute"** in the UI, the request is authenticated via the user's OIDC bearer token mapped to the `approver` role (distinct principal ID from the automated `submitter` service account), satisfying HaaS's database-level `submitter_principal_id != approver_principal_id` invariant while preserving cryptographic attribution in the append-only `audit_events` log.

---

## 3. Component Failure & Recovery Semantics

| Failure Mode | HaaS Native Mechanism | Atlas Client Behavior |
| :--- | :--- | :--- |
| **Planner Model Rejects Tool Calls on Step 1** | Orchestrator catches HTTP `400`/`404` and automatically falls back to the Primary execution model. | Transparent to user; `step_started` continues without interruption. |
| **Tool Worker Timeout or Unhandled Exception** | Sync tools run under `asyncio.to_thread` + `wait_for`. Transient errors retry; fatal tool errors route back to the Planner/Primary model as structured `isError` tool results. | Model degrades gracefully by emitting `trust_tier: "unverified"` on the affected attribute rather than crashing the whole layer. |
| **Browser Network Drop During Layer Streaming** | `/v1/runs/{run_id}/stream` supports `Last-Event-ID` backed by PostgreSQL gapless per-run integer sequence (`run_events`). | `haas_sdk_ts` reconnects automatically with `Last-Event-ID`, replaying missed events and draining buffered messages cleanly. |
| **Abandoned / Stale Run Holding Thread Claim** | HaaS stale-run reaper periodically releases orphaned thread claims (`claimed_by_run_id`). | User can re-branch from the last committed thread version immediately. |
| **Approval Gate Expiry** | HaaS emits `approval_expired` when a paused run exceeds its approval TTL. | UI disables the "Approve" button on the Layer 6 card and offers a "Retry Action" button. |