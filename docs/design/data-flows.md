# Data-flow diagrams and execution sequences

[Design index](README.md) | [Architecture](architecture.md) | [Data model](data-model.md)

## 1. Notation and boundaries

* Rectangles represent external actors/systems; rounded nodes represent processes;
  cylinders represent stores.
* DFDs describe **data movement**, not scheduling order. Sequence diagrams below
  describe ordering, commits and failure windows.
* Level 0 and level 1 are the **TARGET** system. The current sequence is explicitly
  separate; it must not be read as implementing the target's controls.
* Trust boundaries: TB1 caller-to-edge; TB2 edge-to-control plane; TB3 control-to-execution;
  TB4 execution-to-sandbox; TB5 outbound integrations; TB6 stores/telemetry/operations.

## 2. DFD level 0: context (TARGET)

```mermaid
flowchart LR
    caller["E1 Tenant caller / application"]
    approver["E2 Human approver"]
    idp["E3 Identity provider"]
    model["E4 Model provider"]
    integration["E5 Tool destination"]
    callback["E6 Callback receiver"]
    operator["E7 Platform / tenant administrator"]
    haas(["P0 Harness-as-a-Service"])
    caller -->|"F1 credentials, tenant selection, prompt, agent, limits, secret references"| haas
    haas -->|"F2 run handle, status, result, replayable events, errors"| caller
    approver -->|"F3 credentials, exact decision, batch digest"| haas
    haas -->|"F4 pending-action summary, decision receipt"| approver
    idp -->|"F5 signing keys, identity and membership assertions"| haas
    haas -->|"F6 authorized model input, tool schemas, correlation"| model
    model -->|"F7 model output, tool requests, usage"| haas
    haas -->|"F8 validated invocation, scoped credentials, effect key"| integration
    integration -->|"F9 observation, effect receipt, errors"| haas
    haas -->|"F10 signed terminal event"| callback
    callback -->|"F11 delivery acknowledgement"| haas
    operator -->|"F12 approved policy, plugin releases, lifecycle commands"| haas
    haas -->|"F13 redacted audit, usage and operational reports"| operator
```

## 3. DFD level 1: service decomposition (TARGET)

```mermaid
flowchart TB
    caller["E1 Caller"]
    approver["E2 Approver"]
    idp["E3 Identity provider"]
    model["E4 Model provider"]
    integration["E5 Tool destination"]
    receiver["E6 Callback receiver"]
    operator["E7 Administrator"]
    subgraph boundary["HaaS boundary"]
        p1(["P1 Authenticate and authorize"])
        p2(["P2 Admit and manage runs"])
        p3(["P3 Coordinate and execute agent"])
        p4(["P4 Mediate human decisions"])
        p5(["P5 Serve events and deliver callbacks"])
        p6(["P6 Govern policies and operations"])
        d1[("D1 Tenants, membership, agent and policy revisions")]
        d2[("D2 Threads, runs, steps, invocations, approvals")]
        d3[("D3 Durable events, outbox, delivery and usage ledger")]
        d4[("D4 Managed credentials and keys")]
        d5[("D5 Approved plugin artifacts and run artifacts")]
        d6[("D6 Restricted audit and redacted telemetry")]
    end
    caller -->|"F1 credential / tenant context"| p1
    approver -->|"F3 credential / tenant context"| p1
    operator -->|"F12 credential / admin context"| p1
    idp -->|"F5 keys and assertions"| p1
    d1 -->|"membership and role grants"| p1
    p1 -->|"authorized caller + F1 work request"| p2
    p1 -->|"authorized approver + F3 decision"| p4
    p1 -->|"authorized administrator + F12 commands"| p6
    d1 -->|"agent, policy, quota and placement snapshot"| p2
    p2 -->|"accepted run / checkpoint"| d2
    p2 -->|"dispatch intent + admission event"| d3
    p2 -->|"F2 handle / status / errors"| caller
    d3 -->|"committed dispatch intent"| p3
    d2 -->|"checkpoint + invocation state"| p3
    d1 -->|"revocations and pinned policy"| p3
    d4 -->|"short-lived invocation credential"| p3
    d5 -->|"pinned plugin artifact"| p3
    p3 -->|"run artifacts"| d5
    p3 -->|"F6 model input and schemas"| model
    model -->|"F7 output and usage"| p3
    p3 -->|"F8 validated invocation + effect key"| integration
    integration -->|"F9 observation / receipt / errors"| p3
    p3 -->|"fenced checkpoint / pending batch"| d2
    p3 -->|"events + continuation intents + usage"| d3
    d2 -->|"pending exact batch"| p4
    p4 -->|"F4 summary / decision receipt"| approver
    p4 -->|"decision + batch version"| d2
    p4 -->|"decision event + continuation intent"| d3
    d2 -->|"authorized status / result projection"| p5
    d3 -->|"event replay + terminal delivery intents"| p5
    d4 -->|"callback signing key"| p5
    p5 -->|"F2 events and result"| caller
    p5 -->|"F10 signed terminal event"| receiver
    receiver -->|"F11 acknowledgement"| p5
    p5 -->|"delivery attempts and outcome"| d3
    p6 -->|"approved tenant / agent / policy revisions"| d1
    p6 -->|"approved plugin revisions"| d5
    p6 -->|"rotation / revoke command"| d4
    p2 -->|"admission audit"| d6
    p3 -->|"execution audit"| d6
    p4 -->|"decision audit"| d6
    p5 -->|"delivery audit"| d6
    p6 -->|"administration audit"| d6
    d6 -->|"audit / telemetry"| p6
    d3 -->|"usage / delivery reports"| p6
    p6 -->|"F13 permitted reports"| operator
```

### Balancing check

Every external level-0 flow survives decomposition:
F1/F2 → P1/P2/P5; F3/F4 → P1/P4; F5 → P1; F6-F9 → P3;
F10/F11 → P5; F12/F13 → P1/P6. D1-D6 are internal stores, not new external actors.
Large artifacts, secret values and audit payloads are not placed in public event streams.

## 4. DFD level 2: P3 agent execution (TARGET)

```mermaid
flowchart LR
    intents[("D3 Dispatch intents")]
    state[("D2 Durable execution state")]
    policy[("D1 Pinned policy + revocation epoch")]
    credential[("D4 Credential service")]
    artifacts[("D5 Plugin and run artifacts")]
    records[("D3 Events / usage / continuation")]
    audit[("D6 Audit")]
    model["E4 Model provider"]
    external["E5 Tool destination"]
    a(["P3.1 Verify envelope, acquire lease, reserve budget"])
    b(["P3.2 Build model context and select approved model"])
    c(["P3.3 Validate response and bind approval batch"])
    d(["P3.4 Supervise isolated tool execution"])
    e(["P3.5 Aggregate, meter and checkpoint"])
    intents -->|"committed work identity"| a
    state -->|"run version and invocation state"| a
    policy -->|"policy / revocation check"| a
    a -->|"fenced run + reservation"| b
    artifacts -->|"approved skill metadata"| b
    b -->|"F6 context + schemas"| model
    model -->|"F7 response + usage"| c
    c -->|"approval-required batch"| state
    c -->|"pending-approval event"| records
    c -->|"permitted invocation + grant"| d
    artifacts -->|"digest-verified code"| d
    credential -->|"invocation-specific credential"| d
    d -->|"F8 policy-filtered effect"| external
    external -->|"F9 receipt / observation"| d
    d -->|"raw output with scoped object key"| artifacts
    d -->|"bounded sanitized outcomes"| e
    c -->|"final text / validation failure / model usage"| e
    e -->|"version-fenced checkpoint / terminal result"| state
    e -->|"events + usage + outbox intent"| records
    a -->|"lease and policy decision"| audit
    d -->|"effect identity and outcome"| audit
    e -->|"transition audit"| audit
```

The state and event writes at P3.5 are one database transaction in the target.
P3.3 does not dispatch tools while approval is pending. Resumed work re-enters P3.1
with a committed decision and an exact batch version.

## 5. Create, execute and observe sequence (CURRENT)

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant A as FastAPI
    participant P as PostgreSQL
    participant R as Redis streams / secrets
    participant Q as Celery broker / results
    participant O as Orchestrator
    participant M as OpenRouter
    participant T as Tool worker
    participant X as External destination
    C->>A: POST /v1/runs + trusted X-Tenant-ID
    A->>P: Load/create thread and check tenant ownership
    A->>R: SETEX run secrets (if supplied)
    A->>P: INSERT run status=running + checkpoint
    A->>R: XADD run_queued
    A->>Q: Enqueue llm_step_task
    A-->>C: 202 run_id, thread_id, stream_url
    Note over A,Q: Separate writes: a crash can leave undispatched durable state
    C->>A: GET stream + Last-Event-ID
    A->>P: Check run ownership
    A->>R: XREAD BLOCK from cursor
    Q->>O: Deliver model task (redelivery possible)
    O->>P: Load checkpoint
    O->>R: Publish step_started
    O->>M: Messages + unlocked tool schemas
    M-->>O: Assistant message + usage
    alt Any requested tool needs approval
        O->>P: Save pending whole batch with input_required status
        O->>R: Publish input_required
        R-->>A: Stream entry
        A-->>C: SSE input_required and close if still paused
    else Ungated tool batch
        O->>P: Save model checkpoint and observed cost
        O->>R: Read union-scoped run secrets
        O->>Q: Chord of tool tasks + aggregation callback
        par Tool invocation 1
            Q->>T: Context + arguments + scoped secret values
            T->>X: Validated native / HTTP / MCP invocation
            X-->>T: Observation / effect response
            T->>Q: Tool outcome
        and Other tool invocations
            Q->>T: Sibling calls with pre-batch active skills
            T->>Q: Sibling outcomes
        end
        Q->>O: aggregate_step_task(outcomes)
        O->>R: Publish tool_result events
        O->>P: Merge messages / skills and checkpoint
        O->>Q: Enqueue next model step
    else No tool calls
        O->>P: Finalize run and update thread if completed
        O->>R: Delete secret key and publish run_completed
        O->>Q: Optional terminal callback task
        R-->>A: Terminal event
        A-->>C: SSE run_completed
    end
```

Event order within aggregation is tool-result publication followed by its checkpoint.
Finalization and event publication are also separate operations. SSE is not an atomic
commit log of PostgreSQL and cannot repair missing publication.

## 6. Approval and safe continuation sequence (TARGET)

```mermaid
sequenceDiagram
    actor H as Authorized approver
    participant A as Approval API
    participant D as PostgreSQL
    participant R as Outbox relay
    participant W as Orchestrator
    participant S as Sandbox supervisor
    participant X as Destination
    W->>D: Transaction: pending batch + digest + expiry + waiting_approval event
    H->>A: Read pending batch under tenant + approver role
    A-->>H: Tool / destination / arguments / risk / batch digest
    H->>A: Decision + approval ID + digest + idempotency key
    A->>D: Lock and verify tenant, role, expiry, run version, exact digest
    alt Valid and first decision
        A->>D: Transaction: immutable decision + queued state + outbox + audit
        A-->>H: 202 decision receipt
    else Stale / expired / conflicting decision
        A-->>H: 409 conflict without dispatch
    end
    R->>D: Claim committed continuation intent
    R->>W: Signed tenant/run/batch/version envelope
    W->>D: Recheck revocation + decision and acquire fenced lease
    W->>S: Authorized invocation with stable effect key
    S->>X: Execute using destination idempotency key
    X-->>S: Effect receipt
    S-->>W: Outcome + receipt + invocation identity
    W->>D: Transaction: invocation result + checkpoint + event + usage
    Note over R,D: Relay retries are expected with inbox and invocation deduplication
```

Rejection commits denial outcomes and a model continuation, not tool dispatch.
Approval expiry or run deadline produces an explicit failure; cancellation invalidates
the pending batch. A crash after remote success but before recording its receipt
requires a destination lookup by effect key or an `unknown` invocation for reconciliation.

## 7. Data classification along the flows

| Flow / store | Classification | Target handling |
|---|---|---|
| Prompt, thread, tool arguments and outputs | Tenant confidential; may contain regulated data | Tenant-aware access, provider policy, encryption, retention and redaction |
| Credential references | Sensitive metadata | Opaque references, authorization on dereference; no authority from knowing an ID |
| Secret values / signing keys | Restricted secrets | Secret service only; transient scoped use, never prompts, queues, logs or SSE |
| Approval summaries and decisions | Tenant confidential + security evidence | Exact digest, principal, timestamps and restricted audit |
| Events and callback payloads | Tenant confidential | Field allowlist, signed delivery, authenticated replay |
| Usage / identifiers / trace context | Operationally sensitive | Bounded cardinality, tenant authorization, no content in metrics labels |
| Plugin artifacts | Executable trusted or sandboxed code | Review, provenance, immutable digest and isolated import/execution |
| Raw artifacts / spilled output | Tenant confidential | Tenant/run-scoped grants, byte quota, expiry and deletion verification |
