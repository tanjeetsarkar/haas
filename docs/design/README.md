# Harness-as-a-Service design

**Design baseline:** repository application version 3.0.0, inspected 2026-10-17.  
**Status:** current-state documentation plus proposed production design; not a production-readiness certification.

This design describes a service that accepts tenant-authorized work, executes a bounded
agent loop, invokes permitted capabilities, pauses for human decisions, and returns
results through REST, server-sent events (SSE), and callbacks.

The agreed production direction is **cloud-neutral Kubernetes**, managed PostgreSQL
and Redis, a managed secret service, a shared control plane, tenant-scoped data, and
**per-run isolated execution**. Higher-assurance tenants use dedicated execution cells.
These are design decisions, not capabilities already supplied by Docker Compose.

## Read the design

| Document | Contents |
|---|---|
| [Software design description (SDD)](sdd.md) | Requirements, responsibilities, tenant authorization, agent lifecycle, contracts, design decisions |
| [Architecture diagrams](architecture.md) | System context, implemented containers, production control/execution planes, deployment and isolation |
| [Data-flow diagrams (DFD)](data-flows.md) | Level 0, balanced level 1, execution decomposition, current sequence, target approval/recovery |
| [Entity-relationship diagrams (ERD)](data-model.md) | Exact current schema, Redis structures, proposed tenant-aware relational model, constraints and retention |
| [Integrations and usage](integrations-usage.md) | Actual REST/SSE contract, working request examples, native/HTTP/MCP plugins, target event and credential contracts |
| [Threat model](threat-model.md) | Assets, actors, trust boundaries, STRIDE scenarios, controls, residual risk, security acceptance tests |
| [Operations and migration](operations-migration.md) | Proposed SLOs, capacity, failure recovery, observability, rollout stages and verification gates |
| [Plugin marketplace](marketplace.md) | TARGET open marketplace: publishers, manifest v2 permissions, signing, review, trust tiers, installs |
| [Multi-agent collaboration (HACP)](multi-agent.md) | TARGET distributed sub-agents, delegation attenuation, workspace, topologies, A2A/MCP interop |
| [Developer platform](developer-platform.md) | TARGET API conventions, Python/TypeScript SDKs, unified CLI, docs and examples |
| [Terminal experience](tui-experience.md) | TARGET keyboard-first mission control; phased TUI parity, run/agent/marketplace/team workflows and UX qualification |
| [Roadmap](../roadmap.md) | Living, dependency-ordered production-readiness checklist mapped to FR/TM/SEC gates |

Diagrams are embedded as Mermaid source so GitHub and Mermaid-enabled Markdown
viewers can render them without maintaining a second, drifting diagram source.
Each diagram is explicitly marked **CURRENT**, **TARGET**, or **LOGICAL**.
DFD arrows name the data being transferred; architecture arrows name protocols or responsibilities.

## Status vocabulary

* **CURRENT:** verified from executable source or deployment configuration.
* **TARGET / REQUIRED:** proposed production behavior; implementation and acceptance
  tests are still required.
* **LOGICAL:** conceptual relationship, not necessarily a physical table or enforced constraint.
* **GAP:** a difference between current code and the production target.

Do not interpret a target diagram, target API field, SLO, or threat-control requirement
as an implemented feature. There is no authentication service, tenant membership
database, RLS policy, outbox dispatcher, Kubernetes deployment, or per-run sandbox
in the current repository.

## Evidence and precedence

Current behavior in this set is grounded in:

| Source | Evidence |
|---|---|
| [API](../../app/main.py) | Request validation, ownership, create/resume/status/SSE/plugin endpoints |
| [Store](../../app/core/store.py) | PostgreSQL DDL, checkpoints, approval claim, secret TTL, event retention |
| [Worker tasks](../../app/worker/tasks.py) | Agent loop, model routing, fan-out/join, failure paths, callback delivery |
| [Celery configuration](../../app/worker/celery_app.py) | Queues, late acknowledgement, retry/redelivery settings, plugin startup |
| [Plugin registry](../../app/core/plugins.py) and [manifest model](../../app/core/plugin_manifest.py) | Loading, policy, readiness, adapters, capability enforcement |
| [Run context](../../app/core/context.py) and [settings](../../app/core/config.py) | Secret checks, URL guard, budgets and configuration |
| [Compose](../../docker-compose.yml) and [image](../../Dockerfile) | Actual process, volume, network and credential boundaries |
| [Client](../../haas_cli/client.py), [TUI guide](../tui_usage.md) | Client reconnect, approvals and usage |
| [Tests](../../tests/) and [E2E driver](../../tests/e2e/run_e2e.py) | Existing verification scenarios, not proof of target features |

For current behavior, source takes precedence over historical proposals. The
[original distributed specification](../dockerized_celery_haas_spec.md) and earlier
[v1](../extensible_ai_agent_harness_plugin_framework_proposal_v1.md) /
[v2](../extensible_ai_agent_harness_plugin_framework_proposal_v2.md) proposals remain
historical context, not alternative production guarantees.

The [implemented plugin design](../plugin_architecture.md) remains the detailed
plugin-pack reference. Its queue isolation and secret-scoping descriptions must be
read with the actual process/import and Redis boundaries documented here.

## Key conclusions

1. The application is a distributed **single-agent run harness** with a planner/primary
   model routing policy. Parallel tools are not independent subagents.
2. Tenant IDs are currently caller-supplied headers. Ownership checks prevent accidental
   cross-tenant access only when an upstream gateway has securely established that identity.
3. Both tool queues currently share one worker service. The sandbox queue name does not
   provide a per-run sandbox or a tenant security boundary.
4. Plugins are trusted executable deployment artifacts: native modules are imported in
   the API and orchestrator as well as tool workers.
5. Durable checkpoints exist, but database updates and queue/event publication are not
   atomic. Redelivery is possible; external effects are not exactly-once.
6. Production tenancy requires authenticated identity, enforced data partitioning,
   immutable policies, credential brokering, execution isolation, and recovery machinery,
   not merely an additional `tenant_id` column.
