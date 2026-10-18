# Developer platform design

[Design index](README.md) | [Integrations](integrations-usage.md) | [Roadmap](../roadmap.md)

**Status:** PARTIALLY IMPLEMENTED. The current surface includes `/v1` REST + SSE, the
`haas-chat` / `haas chat` TUI, `haas-plugin`, profile and tenant-admin CLI commands, and an
isolated `haas dev` mock stack. The broader SDK, contract tooling and integration surfaces
remain targets ([integrations](integrations-usage.md), [roadmap](../roadmap.md)).

Phase 2 supplies scoped, cursor-paginated `/v1/agents` and `/v1/threads`,
version-pinned history/branch operations, immutable published revision admission and
shared Python client/TUI methods. `/v1/capabilities`, also embedded in `/v1/me`,
advertises metadata version `1`, authorized actions, feature versions and supported
event schemas. Phase 3 replaced the finite Redis stream with the durable PostgreSQL run
event log: the schema is now `run-events.v1` (integer per-run `sequence` SSE ids,
`Last-Event-ID` replay) and `durable_replay: true`. Phase 3 also advertises
`approval-batches.v1` and `run-inspector.v1`. Clients that only understand
`redis-stream.v1` must refuse to connect rather than misinterpret cursors. This metadata is a UI
compatibility hint, never an authorization grant. Full event schemas, AsyncAPI,
Spectral/oasdiff and distributable Python/TypeScript SDKs remain Track D work.

## 1. Principles

* **Contract first.** OpenAPI 3.1 for REST, JSON Schema for every event and webhook payload,
  AsyncAPI for SSE/webhook channels. SDKs, docs and mocks are generated from the contract.
  CI lints (Spectral) and blocks unannounced breaking changes (oasdiff).
* **Five-minute success.** `haas dev up` starts an isolated local stack with a deterministic
  mock model and billing endpoint; `haas dev seed` creates a local-only tenant and expiring
  `hk_test_` submitter/admin and separate approver keys without a real provider key. CI
  quickstart qualification remains a roadmap gate.
* **Safe by default.** SDKs send idempotency keys, verify webhook signatures in constant time and
  never log credentials.
* **One mental model.** Runs, threads, agents, approvals, artifacts, events and plugins are the
  same resources across REST, SDKs, CLI, A2A, MCP and the OpenAI-compatible facade.

## 2. API conventions

| Convention | Rule |
|---|---|
| Versioning | `/v1` is additive; breaking changes go to a new major path; deprecations use `Deprecation`/`Sunset` headers and a changelog |
| Errors | RFC 9457 `application/problem+json` with stable `code`, `request_id`, safe `detail` |
| Pagination | Opaque cursors (`?cursor=&limit=`), `next_cursor` in body |
| Idempotency | `Idempotency-Key` on every mutating endpoint; same key + different body → 409 |
| Rate limits | `RateLimit-*` headers; 429 with `Retry-After` |
| Events | Dotted, versioned names (`run.completed`), monotonic per-run `sequence`, `event_id` for dedupe |
| Auth | Bearer OIDC tokens or API keys (`hk_live_…`, `hk_test_…`); test keys never reach real providers or effects |

## 3. SDKs

Python (`haas`) and TypeScript (`@haas/sdk`) share a feature set:

```python
from haas import Haas

client = Haas()  # reads HAAS_API_KEY / HAAS_URL
run = client.runs.create(agent="support-triage@3", input="Summarize ticket 812")
for event in run.stream():          # reconnects with Last-Event-ID, dedupes by event_id
    if event.type == "run.waiting_approval":
        event.approval.approve()     # binds the batch digest automatically
    elif event.type == "run.completed":
        print(event.data.output)
```

```ts
import { Haas, verifyWebhook } from "@haas/sdk";

const haas = new Haas();
const run = await haas.runs.create({ agent: "support-triage@3", input: "Summarize ticket 812" });
for await (const event of run.stream()) { /* typed union of events */ }

export async function POST(req: Request) {
  const event = await verifyWebhook(req, process.env.HAAS_WEBHOOK_SECRET!);
}
```

Features: sync and async clients (Python), runtime-agnostic fetch SSE with auth headers (TS,
works in browsers and edge runtimes), automatic retries with jitter, pagination iterators, typed
errors, OpenTelemetry context propagation, agents-as-code builders (see [multi-agent](multi-agent.md)).

## 4. CLI

The current unified `haas` commands are `chat`, `profile`, `admin`, and `dev`;
`haas-chat` and `haas-plugin` remain supported aliases. The target command set adds:
`login` (device flow / API key), `runs`, `threads`, `agents publish`, `approvals`,
`plugins new|test|pack|sign|publish|install`, `logs`, and `webhooks listen` (forwards signed
deliveries to localhost).

### Terminal product and capability parity

The [terminal experience design](tui-experience.md) makes `haas chat` a full Textual
workbench, not only a chat window. [Roadmap Track T](../roadmap.md#track-t--terminal-experience)
ships a T0 bridge before Phase 1, then identity, agent revisions, durable run inspection,
approval/budget workflows, artifacts, marketplace, multi-agent collaboration and operations
with their backend milestones.

CLI, SDK and TUI share auth, transport, event types and safe mutation behavior. The API
advertises versioned, identity-scoped capabilities/actions and event-schema compatibility
(`d-capabilities`); every operation is still authorized server-side. Missing browsing,
diagnostic or action endpoints are part of the corresponding delivery item, not a reason
for the terminal to access stores or run privileged remote shell commands.

User-facing features need a tested TUI journey and a line-oriented/JSON command alternative,
with explicit unsupported/forbidden/stale/error states. TUI-01..07 cover keyboard/accessibility,
secret/content safety, truthful recovery, responsiveness, onboarding and compatibility.

## 5. Integration surfaces

| Surface | Use |
|---|---|
| REST + SSE | Primary programmatic API |
| Signed webhooks | Terminal and approval notifications to backends |
| OpenAI-compatible endpoint | Reuse OpenAI SDKs and frameworks against HaaS agents |
| MCP server | Call HaaS agents from IDEs and other agents |
| A2A | Agent-to-agent interop across organizations |
| React widgets (optional) | Chat, run timeline, approval card |

## 6. Documentation and examples

Docs portal with quickstarts (first run, approvals, webhooks, plugins, multi-agent), generated API
and SDK reference, cookbook, migration guides (from `X-Tenant-ID`, from deepagents/LangGraph) and a
changelog. Example apps: FastAPI backend, Next.js streaming + approvals, Slack bot, GitHub Action,
plus Postman/Bruno collections.
