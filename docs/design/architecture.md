# Architecture diagrams

[Design index](README.md) | [SDD](sdd.md) | [DFD](data-flows.md) | [Threat model](threat-model.md)

## 1. System context (LOGICAL)

```mermaid
flowchart LR
    user["Tenant user / operator"]
    client["Application / CLI / Textual TUI"]
    haas["Harness-as-a-Service<br/>authorize, orchestrate, observe"]
    identity["Identity provider<br/>TARGET integration"]
    models["OpenRouter / model providers"]
    systems["Customer and public systems<br/>HTTP, MCP, native connectors"]
    callbacks["Customer callback receiver"]
    platform["Platform operations"]
    user --> client
    client -->|"REST commands + SSE observations"| haas
    identity -.->|"verified principal + membership (TARGET)"| haas
    haas -->|"conversation + permitted tool schemas"| models
    models -->|"assistant response + tool requests + usage"| haas
    haas -->|"authorized tool effects"| systems
    systems -->|"observations"| haas
    haas -->|"terminal callbacks"| callbacks
    platform -->|"deployment, plugin policy, fleet operations"| haas
```

The provider receives prompts, history and included tool outputs, not merely a task
identifier. Provider selection and data residency are part of tenant policy in the target.

## 2. Implemented container architecture (CURRENT)

```mermaid
flowchart TB
    client["REST client / haas-chat"]
    gateway["Authenticating gateway<br/>operator responsibility; not shipped"]
    packs["Read-only plugin packs<br/>native Python + skills + endpoint manifests"]
    subgraph compose["Docker Compose network"]
        api["FastAPI<br/>2 Uvicorn processes"]
        orch["Celery orchestrator<br/>q_orchestrator; concurrency 8<br/>leases + fenced transitions; envelope signing key"]
        maint["Maintenance service<br/>outbox relay + reconciler<br/>(in-process in dev profile)"]
        tools["Celery tools<br/>q_tools_fast + q_tools_sandbox<br/>same service; concurrency 8"]
        pg[("PostgreSQL 16<br/>tenant registry + revisions<br/>RLS threads + runs<br/>steps, invocations, approvals,<br/>run_events, outbox, inbox, usage ledger")]
        subgraph redis["One Redis 7.4 instance"]
            r0[("DB 0<br/>SSE wakeups + secret values")]
            r1[("DB 1<br/>Celery broker")]
            r2[("DB 2<br/>Celery results; not workflow state")]
        end
        scratch[("Shared scratchpad volume")]
        flower["Flower<br/>localhost:5555"]
    end
    provider["OpenRouter"]
    endpoints["External HTTP / MCP systems"]
    callback["Run callback receiver"]
    client -->|"trusted local access or gateway"| gateway
    gateway -->|"tenant header; API :8000"| api
    api -->|"admission / decision / cancel txn + outbox; SSE replay"| pg
    api -->|"secrets; SSE wakeup wait"| r0
    maint -->|"claim outbox SKIP LOCKED; reconcile leases"| pg
    maint -->|"publish relayed tasks"| r1
    r1 -->|"orchestrate + outcome + callback tasks"| orch
    orch -->|"fenced transitions + events + outbox"| pg
    orch -->|"wakeups + scoped secret read"| r0
    orch -->|"model HTTPS; provider key"| provider
    r1 -->|"signed tool envelope + scoped secret values"| tools
    tools -->|"outcome task"| r1
    tools -->|"HTTP / MCP / native effects"| endpoints
    orch -->|"unsigned terminal POST"| callback
    tools -->|"oversized observations"| scratch
    packs -->|"import / discover"| api
    packs -->|"import / discover"| orch
    packs -->|"import / discover"| tools
    flower -->|"monitor"| r1
    flower -->|"inspect results"| r2
    tools -.->|"unused for outcomes"| r2
```

**Boundary qualifications:**

* Gateway is depicted as a required upstream responsibility, not an included service.
  The published API port is not inherently protected by it.
* Tool execution handlers run in tool workers, but Python plugin modules execute at
  import time in API and orchestrator processes too. All installed plugins must be trusted.
* Tool workers are not given the configured PostgreSQL DSN or OpenRouter key in
  Compose. This credential separation is not network enforcement: services share the
  Compose network, Redis credentials are not separated, and default settings exist.
* Redis DB numbers separate keyspaces, not principals, hosts or fault domains.
* Tool workers hold only the Ed25519 envelope *verify* key: they can check but not mint
  dispatch authority. A compromised Redis can delay or replay messages, but fencing,
  `consumer_inbox` deduplication and envelope expiry stop stale or forged execution.
* `q_tools_sandbox` is routing metadata, not a separate sandbox.
* Native handlers can perform filesystem/network operations according to process
  privileges. URL validation applies only where handlers actually call it.
* All services use the same image. The plugin mount is read-only, but shared scratch
  content and Redis task/result content remain sensitive.

## 3. TUI ↔ Service Interaction Architecture (CURRENT)

```mermaid
flowchart TB
    subgraph tui["haas-chat TUI (Textual)"]
        chat_ws["Chat Workspace<br/>multiline, markdown, tool cards"]
        runs_ws["Runs Workspace<br/>cursor-paginated, attach/cancel"]
        approvals_ws["Approvals Workspace<br/>batch digest, approve/reject/leave"]
        admin_ws["Admin Workspace<br/>memberships, keys, policy, audit"]
        profile_mgr["Profile Manager<br/>endpoint/tenant/env (no secrets)"]
    end
    
    subgraph api["HaaS Service API (FastAPI)"]
        runs_api["/v1/runs<br/>POST submit, GET status, POST resume"]
        threads_api["/v1/threads<br/>history, branch, version"]
        agents_api["/v1/agents<br/>draft, validate, publish, diff, use"]
        plugins_api["/v1/plugins<br/>catalog, skills, tools"]
        admin_api["/v1/admin/*<br/>memberships, keys, policy, audit"]
        approvals_api["/v1/approvals<br/>pending, decide"]
        events_api["/v1/runs/{id}/events<br/>durable replay"]
        openai_api["/v1/chat/completions<br/>/v1/responses, /v1/models"]
    end
    
    subgraph sse["SSE Event Stream"]
        stream["/v1/runs/{id}/stream<br/>Last-Event-ID resume"]
        events["run_queued, step_started,<br/>tool_dispatched, tool_result,<br/>run_completed, run_error,<br/>hitl_pending, approval_expired,<br/>run_recovering, stream_gap"]
        deltas["run.output.delta<br/>(advisory live preview)"]
    end
    
    subgraph core["HaaS Core Services"]
        orchestrator["Celery Orchestrator<br/>leases, fencing, model calls"]
        tool_workers["Celery Tool Workers<br/>q_tools_fast, q_tools_sandbox"]
        pg[("PostgreSQL 16<br/>RLS + composite keys")]
        redis[("Redis 7.4<br/>broker, secrets, SSE wakeups")]
    end
    
    chat_ws -->|"POST /v1/runs<br/>prompt, secrets, skills"| runs_api
    runs_ws -->|"GET /v1/runs, SSE stream"| runs_api
    approvals_ws -->|"GET/POST /v1/approvals"| approvals_api
    admin_ws -->|"GET/POST /v1/admin/*"| admin_api
    profile_mgr -->|"local only"| runs_api
    
    runs_api -->|"202 run_id"| chat_ws
    runs_api -->|"SSE stream"| stream
    stream -->|"events + deltas"| chat_ws
    runs_api -->|"admission txn"| pg
    approvals_api -->|"decision + digest"| pg
    admin_api -->|"RBAC checks"| pg
    
    orchestrator -->|"model calls"| provider["OpenRouter"]
    orchestrator -->|"signed envelopes"| tool_workers
    tool_workers -->|"HTTP/MCP/native"| endpoints["External Systems"]
    orchestrator -->|"events + outbox"| pg
    orchestrator -->|"SSE wakeups"| redis
    tool_workers -->|"outcomes"| orchestrator
```

### TUI Communication Patterns

| TUI Workspace | Primary API Calls | SSE Events Consumed |
|---|---|---|
| **Chat** | `POST /v1/runs`, `GET /v1/plugins` | `run_queued`, `step_started`, `tool_dispatched`, `tool_result`, `run_completed`, `run_error`, `hitl_pending`, `run.output.delta` |
| **Runs** | `GET /v1/runs` (cursor), `POST /v1/runs/{id}/cancel` | `run_cancelling`, `run_cancelled` (on attach) |
| **Approvals** | `GET /v1/approvals`, `POST /v1/runs/{id}/resume` | `hitl_approved`, `hitl_rejected`, `approval_expired` |
| **Admin** | `GET/POST /v1/admin/*` (RBAC-gated) | — |
| **Identity** | `GET /v1/me` (verify), `/login`, `/tenant`, `/profile` | — |

## 4. Production logical architecture (TARGET)

```mermaid
flowchart TB
    user["Tenant application / UI / CLI"]
    idp["OIDC IdP / service credential issuer"]
    subgraph edge["Public edge boundary"]
        ingress["TLS ingress + WAF + rate limits"]
        auth["Identity verification<br/>tenant membership + principal context"]
    end
    subgraph control["Shared control plane - trusted platform code only"]
        api["Run / thread / approval API"]
        policy["Tenant policy + agent revision service"]
        pg[("PostgreSQL<br/>RLS + composite tenant keys")]
        relay["Outbox relay + stalled-work reconciler"]
        events["Event replay / SSE gateway"]
        deliveries["Signed callback dispatcher + DLQ"]
        audit["Restricted audit + usage export"]
        registry["Approved immutable plugin artifacts<br/>static metadata catalog"]
        secrets["Managed secret service<br/>tenant key boundaries"]
    end
    subgraph shared["Shared execution cell"]
        broker[("Cell broker / result transport")]
        orch["Trusted orchestrator<br/>leases + version fencing"]
        supervisor["Execution supervisor<br/>no customer plugin imports"]
        sandbox["Per-run isolated sandbox<br/>one tenant + run; bounded lifetime"]
        creds["Invocation credential broker"]
        egress["Policy egress proxy<br/>destination + DNS + TLS controls"]
        artifacts[("Tenant/run-scoped artifact storage")]
    end
    dedicated["Dedicated execution cell<br/>same contracts; isolated workers, keys and network"]
    models["Approved model providers"]
    external["Approved HTTP / MCP / customer systems"]
    receiver["Customer callback receiver"]
    user -->|"HTTPS"| ingress
    ingress --> auth
    idp -->|"JWT validation / key rotation"| auth
    auth -->|"verified context"| api
    api --> policy
    api -->|"state + intent transaction"| pg
    api --> events
    policy --> registry
    relay -->|"claim committed intents"| pg
    relay -->|"tenant + cell route; no raw secrets"| broker
    relay -->|"placement for dedicated tenants"| dedicated
    broker --> orch
    orch -->|"fenced state writes"| pg
    orch -->|"approved model call"| egress
    egress --> models
    orch -->|"invocation grant"| supervisor
    supervisor -->|"pinned artifact + resource policy"| sandbox
    registry -->|"digest-verified artifact"| supervisor
    sandbox -->|"short-lived invocation capability"| creds
    creds --> secrets
    creds --> secrets
    sandbox --> egress
    egress --> external
    sandbox -->|"scoped artifact access"| artifacts
    supervisor -->|"validated outcomes"| broker
    events -->|"replay + live projection"| pg
    events -->|"SSE"| user
    deliveries -->|"committed terminal events"| pg
    deliveries -->|"signed HTTPS callback"| receiver
    pg -->|"redacted export"| audit
```

Arrow directions show requests/writes; replies use the same channel unless separately
drawn. Logical services may initially share a deployment only when identity/privilege
boundaries are preserved. This is not a requirement to create a microservice per box.

### Control-plane responsibilities

* Authenticate callers, resolve tenant membership, enforce RBAC and policy.
* Pin agent/model/plugin/policy revisions, reserve budgets, and persist accepted work.
* Store run, approval, invocation, event, usage and outbox records atomically.
* Route only committed work to the tenant's cell; reconcile incomplete dispatch.
* Serve redacted observations and signed deliveries without granting sandbox access
  to authoritative stores.

### Execution-plane responsibilities

* Verify execution envelopes and durable state, acquire leases and enforce epochs.
* Keep orchestration separate from native customer/plugin execution.
* Launch bounded per-run sandboxes from approved immutable artifacts.
* Mediate egress and credentials per invocation, collect outcomes and artifacts,
  terminate stale/cancelled work and report uncertain effects.
* Preserve tenant/cell identity through every queue, callback and artifact operation.

## 5. Deployment and network boundaries (TARGET)

```mermaid
flowchart LR
    internet["Internet"]
    subgraph cluster["Kubernetes cluster / region"]
        subgraph edge["Edge namespace"]
            gw["Ingress + identity-aware gateway"]
        end
        subgraph cp["Control namespace"]
            apis["API + event replicas"]
            workers["Outbox / recovery / callbacks"]
            catalog["Static plugin catalog"]
        end
        subgraph cell["Execution cell namespace"]
            coord["Orchestrator + supervisor"]
            cb[("Cell Redis")]
            proxy["Egress + credential mediation"]
        end
        subgraph isolated["Sandbox runtime boundary"]
            runA["Tenant A / run 1"]
            runB["Tenant B / run 2"]
        end
        telemetry["Telemetry collectors<br/>redacted + restricted sinks"]
    end
    db[("Managed PostgreSQL<br/>private endpoint + backup")]
    sm["Managed secrets / KMS"]
    obj[("Artifact object store")]
    destinations["Approved providers / integrations"]
    internet -->|"443 only"| gw
    gw --> apis
    apis --> db
    workers --> db
    workers --> cb
    cb --> coord
    catalog --> coord
    coord -->|"runtime API; scoped workload identity"| runA
    coord -->|"runtime API; scoped workload identity"| runB
    runA --> proxy
    runB --> proxy
    proxy --> sm
    proxy --> destinations
    runA -->|"tenant-scoped grant"| obj
    runB -->|"tenant-scoped grant"| obj
    apis -.-> telemetry
    coord -.-> telemetry
```

Required policies are default-deny ingress/egress, no public database/broker/Flower,
service identities with short-lived credentials, restricted Kubernetes RBAC,
non-root/read-only sandbox root filesystems, no host paths, no privileged mode,
no cloud metadata access, resource limits and enforceable process termination.
Sandbox workloads must not inherit the supervisor's Kubernetes service-account token.

Managed services use TLS, independent identities, encryption and documented backup
and failover policies. Kubernetes scheduling alone does not establish tenant isolation;
enforcement must exist in runtime, network, identity and storage.

## 6. Placement, scaling and alternatives

* Start with one regional control plane and multiple execution cells. A tenant's
  cell/region assignment is durable and versioned; do not reroute in-flight work
  across cells without fencing the old epoch.
* Scale API by request/SSE capacity, orchestrators by queue age and provider capacity,
  and supervisors/sandboxes by reserved tenant concurrency.
* Preserve fair scheduling across tenants before priority within a tenant. Do not
  create unbounded per-tenant queues as the sole fairness mechanism.
* Dedicated cells isolate noisy neighbors and execution blast radius. Residency may
  require a dedicated regional control/data plane too.
* A workflow engine could replace part of Celery coordination if recovery requirements
  outgrow the lease/outbox design. It would not remove application-level authorization,
  effect idempotency, approval binding or sandbox requirements.

## 7. Product extensions (TARGET)

The following reuse the control/execution split above rather than adding new trust boundaries:

* **Marketplace registry** ([design](marketplace.md)) belongs to the control plane but stores
  only static metadata. Artifacts are pulled by digest by the execution supervisor and
  verified before sandbox launch. The review pipeline runs dynamic tests in an isolated
  analysis cell, never in a tenant cell.
* **Multi-agent runs** ([design](multi-agent.md)) are ordinary runs linked as a tree. Each node
  holds its own lease and sandbox and can be placed in any cell of the tenant's placement.
  Wake-ups are outbox messages.
* **Interop surfaces** (A2A, MCP server, OpenAI-compatible facade; [developer platform](developer-platform.md))
  are additional API front-ends behind the same edge identity verification and admission path.