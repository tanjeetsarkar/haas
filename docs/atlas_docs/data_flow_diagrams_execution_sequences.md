# HaaS Atlas — Data-Flow Diagrams (DFD) & Execution Sequences

## 1. Level 0 Context DFD

```
+--------------------+      1. Search / Drill-Down / Approve       +------------------------+
|                    | ------------------------------------------> |                        |
|   Everyday User    |                                             |   HaaS Atlas Platform  |
| (Web / Mobile App) | <------------------------------------------ | (FastAPI + Celery + PG)|
|                    |      2. Progressive SDUI Layers (SSE)       |                        |
+--------------------+                                             +-----------+------------+
                                                                               |
                                            +----------------------------------+----------------------------------+
                                            | 3. Mediated Egress (SSRF-checked)                                   | 4. Model Inference
                                            v                                                                     v
                           +---------------------------------+                                   +---------------------------------+
                           | External Web / MCP Servers      |                                   | OpenRouter / LLM Providers      |
                           | (Brave API, Hospital Websites,  |                                   | (Planner & Primary Models)      |
                           |  Public Tariff Datasets)        |                                   +---------------------------------+
                           +---------------------------------+
```

---

## 2. Level 1 Trust-Boundary Data-Flow Diagram

```
========================================= TRUST BOUNDARY 1: PUBLIC UNTRUSTED INTERNET =========================================
  [Browser Client (haas_sdk_ts)]                                     [Untrusted Web Pages / PDFs / Remote MCP Servers]
        |                                                                                    ^
        | (DF-1) HTTPS + OIDC Bearer / API Key                                               | (DF-7) DNS-verified HTTP/MCP
========|====================================================================================|=================================
        v                                                                                    |
========================================= TRUST BOUNDARY 2: HAAS CONTROL PLANE (DMZ) ==========================================
  [haas-api (FastAPI)]
   - Verifies OIDC JWT / API Key & active tenant membership
   - Loads immutable Tenant Policy Revision
   - Sets PostgreSQL session `SET LOCAL app.current_tenant_id = ...`
        |                                           |
        | (DF-2) SQL over non-owner API role        | (DF-3) Celery Task Enqueue + AES-GCM Ephemeral Secrets
        v                                           v
========================================= TRUST BOUNDARY 3: DATA & BROKER PLANE ===============================================
  [PostgreSQL 16 (Forced RLS)]                [Redis 7 (Loopback / Internal Network)]
   - tenants, policies, memberships            - Queue: orchestrator
   - threads, runs, run_events                 - Queue: tools
   - atlas_entity_cache, atlas_layer_nodes     - Pub/Sub: SSE & output-deltas.v1
        ^                                           |
        | (DF-4) SQL over non-owner Worker role     | (DF-5) Consume Orchestrator / Tool Tasks
========|===========================================|==========================================================================
        |                                           v
========================================= TRUST BOUNDARY 4: EXECUTION PLANE ===================================================
  [worker-orchestrator] <------ (DF-6) Celery Chord (aggregate_step_task) ------> [worker-tools (read_only: true, tmpfs /tmp)]
   - Step 1 Planner / Primary Model                                                - Enforces Active Skill Tool Unlocking
   - HACP Child Run Spawner                                                        - Runs tools in asyncio.to_thread + wait_for
   - Appends gapless run_events                                                    - Executes atlas-explorer, web-research
===============================================================================================================================
```

---

## 3. Execution Sequence 1: Layer 1 Query & Cache-Through Fan-Out

```
User (Browser)          haas-api            PostgreSQL (RLS)          Redis          worker-orchestrator       worker-tools
      |                    |                       |                    |                     |                     |
      |-- POST /v1/threads |                       |                    |                     |                     |
      |------------------->| INSERT threads (v=0)  |                    |                     |                     |
      |                    |---------------------->|                    |                     |                     |
      |<-- thd_01 ---------|                       |                    |                     |                     |
      |                    |                       |                    |                     |                     |
      |-- POST /v1/runs -->|                       |                    |                     |                     |
      | (prompt: "Cancer   | Verify Policy Rev     |                    |                     |                     |
      |  hospitals Mumbai",| Claim thd_01          |                    |                     |                     |
      |  skills: [atlas,   | INSERT runs (queued)  |                    |                     |                     |
      |   web-research])   |---------------------->|                    |                     |                     |
      |                    |-- Enqueue Orchestrator ------------------->|                     |                     |
      |<-- run_01 ---------|                       |                    |---- Deliver ------->|                     |
      |                    |                       |                    |                     |                     |
      |-- GET /v1/runs/run_01/stream ------------->|                    |                     |                     |
      |                    |                       |                    |                     |-- Step 1 (Planner) -|
      |                    |                       |                    |                     | Call OpenRouter     |
      |                    |                       |                    |                     |                     |
      |                    |                       |                    |                     |-- Dispatch Tool --->|
      |                    |                       |                    |                     | atlas_cache_lookup  |
      |                    |                       |<------------------- SELECT atlas_entity_cache -----------------|
      |                    |                       |---- (2 Hits, 3 Misses) --------------------------------------->|
      |                    |                       |                    |                     |<-- Chord Complete --|
      |                    |                       |                    |                     | (aggregate_step_task)
      |                    |                       |                    |                     |                     |
      |                    |                       |                    |                     |-- Step 2 (Primary) -|
      |                    |                       |                    |                     | Parallel Chord:     |
      |                    |                       |                    |                     | 3x web_search       |
      |                    |                       |                    |                     |-------------------->|
      |<=================== SSE: run.output.delta (Progressive Entity Previews) ====================================|
      |                    |                       |                    |                     |<-- Chord Complete --|
      |                    |                       |                    |                     | (aggregate_step_task)
      |                    |                       |                    |                     |                     |
      |                    |                       |                    |                     |-- Step 3:           |
      |                    |                       |                    |                     | atlas_emit_layer +  |
      |                    |                       |                    |                     | atlas_cache_upsert  |
      |                    |                       |<------------------- UPSERT cache + INSERT atlas_layer_nodes ---|
      |                    |                       |<-- INSERT run_events (run_completed) ----|                     |
      |<=================== SSE: run_completed (Validated atlas-layer.v1 Envelope) ===========|                     |
```

---

## 4. Execution Sequence 2: Layer $K \rightarrow K+1$ Branching & Layer 6 HITL Action

When the user drills down to Layer 6 and clicks **[Send Hospital Inquiry]**, HaaS pauses at the approval gate (`status='input_required'`) and enforces the **no-self-approval rule** on resume:

```
User (Browser)            haas-api (Submitter)      haas-api (Approver)       PostgreSQL (RLS)       worker-orchestrator
      |                            |                         |                       |                        |
      |-- POST /v1/threads/thd_01/branch (version=4) ------->|                       |                        |
      |<-- thd_branch_05 ----------|                         |                       |                        |
      |                            |                         |                       |                        |
      |-- POST /v1/runs ---------->|                         |                       |                        |
      | (thread: thd_branch_05,    | INSERT run_06           |                       |                        |
      |  principal: svc_submitter) |------------------------------------------------>|                        |
      |                            |                         |                       |---- Start Run -------->|
      |                            |                         |                       |                        |
      |                            |                         |                       |   Tool `atlas_dispatch_inquiry`
      |                            |                         |                       |   has `approval=true`.
      |                            |                         |                       |   Compute batch `action_digest`.
      |                            |                         |                       |<-- UPDATE runs --------|
      |                            |                         |                       |   SET status='input_required'
      |                            |                         |                       |   INSERT event `hitl_pending`
      |<=================== SSE: hitl_pending (action_digest, destination, args) ====|                        |
      |                            |                         |                       |                        |
  [User Reviews Immutable Consent Drawer]                    |                       |                        |
  [Clicks "Approve & Send" with Human OIDC Token]            |                       |                        |
      |                            |                         |                       |                        |
      |-- POST /v1/runs/run_06/resume ---------------------->|                       |                        |
      |  { "approved": true,       |                         | Verify:               |                        |
      |    "digest": "sha256:..." }|                         | 1. Tenant matches     |                        |
      |                            |                         | 2. Role == approver   |                        |
      |                            |                         | 3. approver_id !=     |                        |
      |                            |                         |    submitter_id       |                        |
      |                            |                         | 4. Atomic SQL:        |                        |
      |                            |                         | UPDATE runs           |                        |
      |                            |                         | WHERE run_id='run_06' |                        |
      |                            |                         | AND status=           |                        |
      |                            |                         |  'input_required'     |                        |
      |                            |                         |---------------------->|                        |
      |                            |                         |                       |---- Resume Task ------>|
      |<=================== SSE: hitl_approved -> tool_dispatched -> tool_result -> run_completed ============|
```