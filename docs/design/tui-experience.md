# Terminal experience design

[Design index](README.md) | [Developer platform](developer-platform.md) | [Roadmap: Track T](../roadmap.md#track-t--terminal-experience) | [Current TUI guide](../tui_usage.md)

**Status: PARTIALLY IMPLEMENTED.** This is the terminal product direction and qualification
contract; Track T in the roadmap owns delivery and status. The Textual workbench now includes
Chat, Runs, authenticated profiles/tenant switching, and role-aware tenant administration
under both `haas chat` and the `haas-chat` alias. Remaining journeys ship with their backend
milestones; do not rewrite the UI framework or wait for a web UI.

## TUI ↔ Harness Service Architecture

The `haas-chat` TUI is a **client application** that connects to the HaaS service over
standard APIs. It does not run agents locally—all agent execution happens in the HaaS service.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              TUI (haas-chat)                                │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────────────┐  │
│  │ Chat    │  │ Runs    │  │Approvals│  │ Admin   │  │ Profile/Identity│  │
│  │ Workspace│ │Workspace│ │Workspace│ │Workspace│  │ Management      │  │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘  └────────┬────────┘  │
└───────┼────────────┼────────────┼────────────┼────────────────┼───────────┘
        │            │            │            │                │
        ▼            ▼            ▼            ▼                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           HaaS Service (REST + SSE)                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ /v1/runs     │  │ /v1/threads  │  │ /v1/agents   │  │ /v1/admin/*    │  │
│  │ /v1/plugins  │  │ /v1/policy   │  │ /v1/approvals│  │ /v1/audit      │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └───────┬────────┘  │
└─────────┼─────────────────┼─────────────────┼──────────────────┼───────────┘
          │                 │                 │                  │
          ▼                 ▼                 ▼                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        HaaS Service Internals                               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────────────┐   │
│  │ API Gateway │  │ Orchestrator│  │ Tool Workers│  │ PostgreSQL +     │   │
│  │ (FastAPI)   │  │ (Celery)    │  │ (Celery)    │  │ Redis (state)    │   │
│  └─────────────┘  └─────────────┘  └─────────────┘  └──────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Communication Channels

| Channel | Protocol | Purpose |
|---------|----------|---------|
| **REST API** | HTTPS/HTTP | Run submission, thread management, agent revisions, plugin catalog, admin operations |
| **SSE Stream** | `text/event-stream` | Real-time run events (`run_queued`, `step_started`, `tool_dispatched`, `tool_result`, `run_completed`, `run_error`, `hitl_pending`, `approval_expired`) |
| **Output Deltas** | SSE (`run.output.delta`) | Advisory live tool output preview (when server advertises `output-deltas.v1`) |
| **Webhooks** | HTTPS POST | Terminal callbacks on run completion (configured in tenant policy) |
| **OpenAI-compatible** | HTTPS | `/v1/chat/completions`, `/v1/responses`, `/v1/models` with `model="agent/<name>"` |

### Authentication Flow

```
1. TUI starts → connects to API base URL (--url / HAAS_URL)
2. User runs /login → provides API key or OIDC bearer token
3. TUI calls GET /v1/me → server verifies credential, returns:
   - tenant_id, principal_id, roles (tenant_admin, submitter, approver, auditor)
   - capabilities (actions, event_schemas, features)
   - environment (dev/staging/prod)
4. TUI caches identity → enables role-gated workspaces (Admin, Approvals)
5. All subsequent requests include Authorization: Bearer <token>
```

### Run Lifecycle in TUI

```
User types prompt
       │
       ▼
┌──────────────────┐
│ POST /v1/runs    │  ← prompt, secrets, preload_skills, model overrides, budgets
│ (async, 202)     │
└────────┬─────────┘
         │ run_id returned
         ▼
┌──────────────────┐
│ GET /v1/runs/    │  ← SSE stream with Last-Event-ID resume
│ {run_id}/stream  │
└────────┬─────────┘
         │
         ▼
    Events flow:
    run_queued → step_started → tool_dispatched → tool_result → ...
         │                              │
         │                    (if approval needed)
         │                              ▼
         │                    hitl_pending → TUI shows approval dialog
         │                              │
         │                    User approves/rejects
         │                              │
         ▼                              ▼
    run_completed ←────────────────── resume with decision
         │
         ▼
    TUI shows final Markdown answer + run footer (steps, cost, cached tokens)
```

## 1. Baseline and ambition

The shipped TUI has multiline composition, command completion, prompt history, Markdown
answers, tool previews, model/step/cost indicators, approval dialogs, SSE reconnect, cursor
run browsing/reattach/cooperative cancellation, verified bearer identity, named non-secret
profiles, tenant switching, and role-gated membership/API-key/policy/scoped-inventory/audit
workflows. The client verifies the tenant with `/v1/me`; unauthenticated development mode
still uses the header only as a local selector. Phase 2 adds command-driven agent
draft/validate/publish/diff/pinning and paginated thread/history/branch journeys,
with a structured JSON draft form and explicit active-run/version conflict recovery.
Versioned identity-scoped capability metadata drives administration visibility; older
servers without metadata retain the role-based compatibility path, and unsupported
advertised versions/event schemas are rejected. These are Chat commands, not new
dedicated visual workspaces. T3 adds the approval inbox workspace (`Ctrl+4`), a run
inspector (budget, steps, invocations, usage) and `/events` over the durable event log.
Marketplace consent, distributed-agent inspection and operator workflows remain roadmap work.

The target is an **agent mission control terminal**: a welcoming chat for first-time users,
a precise execution workbench for developers, and an authorized control surface for
approvers, tenant administrators, publishers, and operators. Progressive disclosure keeps
these roles from sharing one overwhelming dashboard.

Users should always be able to answer:

1. Where am I connected, as whom, for which tenant, and is this production?
2. What is running, what is waiting, and what needs my decision?
3. Which agent revision, capabilities, destinations, and budget apply?
4. What changed, what evidence supports it, and what can I safely do next?
5. Is this view current, reconnecting, incomplete, or showing historical evidence?

## 2. Product surfaces

| Surface | Target experience | Current Status |
|---|---|---|
| Home / activity | Recent and pinned threads, active runs, approval inbox, actionable failures, onboarding; role-scoped counts | **Planned** (T3+) |
| Chat / composer | Multiline drafts, paste preview, skill/agent picker, revision and budget summary, expandable tool results, explicit attachment upload consent | **Implemented** (T0/T1) |
| Runs / threads | Search, filters, cursors, attach/detach, compare runs, explicit cancel, historical transcript; branch only through versioned server APIs | **Implemented** (T1/T2) |
| Run inspector | Ordered timeline, plan checklist, attempts, tool inputs/results, artifact references, cost breakdown, trace/request IDs and failure explanations | **Implemented** (T3) |
| Approvals | Review queue with lineage, exact actions/destinations, permission and argument diffs, expiry, decision history, deliberate approve/reject | **Implemented** (T3) |
| Agents | Draft editor, schema validation, revision comparison, publish review, allowed models/plugins and topology selection | **Implemented** (T2) |
| Team view | Collapsible live run tree, agent lanes, bounded graph inspection, messages, handoffs, joins, child budgets and root-visible approvals | **Planned** (Multi-agent Track) |
| Workspace / artifacts | Scoped file tree, bounded text/JSON/diff previews, provenance, version history, download/export consent, CAS conflict resolution | **Planned** (Phase 4+) |
| Plugins / marketplace | Search public/private catalogs, provenance and trust evidence, permission comparison, digest-pinned install, upgrade consent, advisory/revocation state | **Planned** (Marketplace Track) |
| Connections / developer tools | Guided local mock run, connector readiness, secret-binding names, redacted HTTP/MCP diagnostics, example requests and signed webhook listener | **Planned** (Developer Platform Track) |
| Tenant / operations | Authorized membership and policy workflows, usage/quotas, webhook delivery/replay, audit, retention and operational diagnostics | **Implemented** (T2 Admin) |

The run inspector is a record of user-visible actions, plans, outcomes, and evidence;
it must not present hidden model reasoning as an inspectable execution guarantee.
Cost, latency, cache use, and quotas must say whether values are observed, reserved,
estimated, or settled. A plan checkbox is not proof that an external effect succeeded.

## 3. Interaction and information architecture

Use a consistent application shell: persistent environment/identity banner, navigation rail,
central workspace, optional detail pane, activity/status strip, and contextual help.
At 80x24, use one navigable pane rather than squeezing a dashboard into unreadable columns.
Wider terminals gain side-by-side transcript, timeline, tree, and evidence panes.

* A fuzzy command palette searches navigation and authorized actions. Slash commands remain
  fast paths; important actions also have discoverable menus and keyboard help.
* Tables support filter/search, sorting where supported by the API, paging, stable selection,
  and explicit follow-live versus paused inspection. New events do not steal scroll or focus.
* Preserve drafts and selection across navigation; store non-sensitive preferences locally.
  Persist transcripts or drafts containing customer content only by explicit opt-in.
* Context-scoped shortcuts never approve, publish, revoke, or cancel while a user is typing.
  Keep terminal copy/paste behavior and provide a focus-navigation mode rather than making
  prompt completion trap keyboard users permanently.
* Empty, loading, offline, stale, partial, forbidden, rate-limited, and unsupported views have
  distinct states, useful next actions, and safe correlation IDs.
* Expose connection retry/backoff and stream cursor state. Reattach never silently creates
  a second run. Preserve the original idempotency key after an ambiguous submission.
* Use stable resource IDs for navigation and optional deep links without credentials.
  Browser opening, clipboard writes, file downloads, and local editor launches are explicit.

### Intentional behavior changes

These are future migrations, not claims about current keybindings:

* `Esc` leaves a view/dialog or detaches; it does **not** reject an approval or cancel work.
  Approve and reject are explicit actions; closing review leaves the server batch pending.
  Existing `Esc`-to-reject behavior needs release notes and regression tests when replaced.
* Cancel is separate from detach, shows scope and consequences, and requires confirmation.
  Phase 0 cancellation is cooperative: already executing external calls may finish.
  Later phases distinguish requested, fenced, and supervisor-confirmed termination only
  when the server supplies those facts.
* Reusing a thread with active work is guarded in the UI, then enforced by Phase 2 thread
  fencing. The UI must never advertise its early guard as a concurrency/security boundary.
* Final answers remain readable without inspecting every tool. Detailed output is expandable,
  bounded, searchable, and labelled as untrusted content.

## 4. Current Implementation Status (T0–T3)

### T0 — Foundation (IMPLEMENTED)
- Textual application shell with navigation rail (Chat, Runs, Admin, Help)
- Multiline prompt with history, Tab completion, Ctrl+J newlines
- Markdown rendering for agent answers
- Tool cards with running/succeeded/failed status and preview
- Step lines showing model and active skills
- Run footer with steps, cost, cached tokens
- SSE reconnect with Last-Event-ID resume (up to 5 attempts)
- Connection health indicator (green/red/yellow dot)
- Environment badge (DEV/STAGING/PRODUCTION)
- ASCII mode (`--ascii`) for limited terminals
- Theme cycling (dark/light/auto) with `Ctrl+T`

### T1 — Authenticated Identity & Tenant Context (IMPLEMENTED)
- `/login` with API key or OIDC bearer token (memory only, never persisted)
- `/logout` clears credential
- `/tenant` switch verified by server via `GET /v1/me`
- `/profile` switch with saved endpoint profiles (URL, tenant, environment)
- Profile store: `haas profile add/list/activate/remove`
- Top bar shows: environment, profile name, endpoint, tenant, principal (8 chars), roles
- Role-gated Admin workspace visibility (`tenant_admin`, `auditor`)

### T2 — Agent Revisions & Thread History (IMPLEMENTED)
- `/agent draft` — structured JSON form for immutable agent draft
- `/agent validate <id>` — validates against active tenant policy
- `/agent publish <id>` — publishes immutable revision
- `/agent use <id>` — pins revision for subsequent runs
- `/agent clear` — restores request-level settings
- `/agent diff <id1> <id2>` — compares config, policy permissions, registry fingerprint
- `/agents` — lists revisions with status
- `/threads [search]` — cursor-paginated thread search
- `/threads next` — advances cursor
- `/history [offset]` — refreshes/browses committed history at captured version
- `/branch <version>` — branches from committed snapshot at exact version
- Version conflict detection and recovery guidance

### T3 — Approvals, Inspector & Events (IMPLEMENTED)
- **Approvals workspace** (`Ctrl+4`): tenant-scoped batch list with filter (pending/approved/rejected/expired/all)
- Batch digest binding: decisions sent with reviewed `batch_digest`; 409 on mismatch/expiry
- Approve/Reject/Leave-pending actions with explicit keys (`y`/`n`/`Esc`)
- `/approvals [STATUS]` — Chat command for approval list
- `/approve <run-id>` — review specific pending approval as verified approver
- **Run inspector** (`/inspect [run-id]` or Inspect button): status, state version, event sequence, W3C trace context, budget (settled/uncertain/reserved vs max), remaining cost, steps, tool invocations, deadline, per-step attempts, tool invocations with durable outcome, usage ledger
- **Durable event log** (`/events [run-id] [after] [type=TEXT]`): pages immutable `run_events` with type filtering (e.g., `type=tool`)
- Output deltas: advisory live preview via SSE `run.output.delta` when server advertises `output-deltas.v1`; final `run_completed` output supersedes

## 5. Keyboard Reference (Current)

| Key | Action |
|---|---|
| `Enter` | Send message / run command (completes partial command first) |
| `Ctrl+J` / `Shift+Enter` | New line in prompt |
| `Tab` | Complete command/argument (never leaves prompt) |
| `↑` / `↓` | Choose completion / browse history |
| `Esc` | Detach from running task; leave approval pending |
| `Ctrl+1` / `Ctrl+2` | Chat / Runs workspace |
| `Ctrl+3` | Admin workspace (role-gated) |
| `Ctrl+4` | Approvals workspace |
| `F6` | Next focus |
| `Ctrl+R` | Refresh run list |
| `Ctrl+F` | Follow/pause live output |
| `Ctrl+T` | Cycle theme |
| `F1` | Contextual help |
| `Ctrl+L` | Clear screen |
| `Ctrl+N` | New conversation (`/new`) |
| `Ctrl+Q` | Quit |
| `Ctrl+P` | Command palette |

## 6. Slash Commands Reference (Current)

| Command | Description | API Calls |
|---|---|---|
| `/help` | List commands | — |
| `/login` · `/logout` | Sign in/out (memory only) | `GET /v1/me` |
| `/tenant [id]` · `/profile [name]` | Switch tenant/profile | `GET /v1/me` (verify) |
| `/status` | Connection, thread, models, budgets, secrets, cost | `GET /v1/me`, `GET /v1/plugins` |
| `/admin` | Open Admin workspace | — |
| `/approve <run-id>` | Review pending approval | `GET /v1/approvals/{run_id}`, `POST /v1/runs/{run_id}/resume` |
| `/approvals [STATUS]` | Approval inbox | `GET /v1/approvals` |
| `/inspect [run-id]` | Run inspector | `GET /v1/runs/{run_id}/inspect` |
| `/events [run-id] [after] [type]` | Durable event log | `GET /v1/runs/{run_id}/events` |
| `/live [on\|off]` | Toggle output deltas | SSE `run.output.delta` |
| `/skills` | List skills with secrets | `GET /v1/plugins` |
| `/preload [skill...]` | Set/clear preloaded skills | Sent with `POST /v1/runs` |
| `/secret set/list/unset` | In-memory secrets | Sent with `POST /v1/runs` |
| `/model <id>` · `/planner <id>` | Override models | Sent with `POST /v1/runs` |
| `/steps <N>` · `/budget <USD>` | Override limits | Sent with `POST /v1/runs` |
| `/agent draft/show/validate/publish/use/clear/diff` | Agent revision mgmt | `POST/GET /v1/agents/*` |
| `/agents` | List agent revisions | `GET /v1/agents` |
| `/threads [search]` · `/threads next` | Thread browser | `GET /v1/threads` |
| `/history [offset]` | Thread history | `GET /v1/threads/{id}/history` |
| `/branch <version>` | Branch thread | `POST /v1/threads/{id}/branch` |
| `/new` · `/clear` · `/quit` | New thread, clear, exit | Local |

## 7. Admin Workspace Operations (Current)

| Button | Action | Required Role | API Endpoint |
|---|---|---|---|
| List members | List all tenant memberships | tenant_admin, auditor | `GET /v1/admin/memberships` |
| Grant member | Add principal with role | tenant_admin | `POST /v1/admin/memberships` |
| Revoke member | Remove principal role | tenant_admin | `DELETE /v1/admin/memberships/{principal_id}/{role}` |
| List API keys | List tenant API keys (hashed) | tenant_admin | `GET /v1/admin/api-keys` |
| Create API key | Generate new API key (shown once) | tenant_admin | `POST /v1/admin/api-keys` |
| Revoke API key | Revoke key by prefix | tenant_admin | `DELETE /v1/admin/api-keys/{key_prefix}` |
| View policy | Show active policy revision | tenant_admin, auditor | `GET /v1/admin/policy` |
| Activate policy | Activate policy revision (JSON form) | tenant_admin | `POST /v1/admin/policy/activate` |
| Scoped plugins | List tenant-scoped plugin catalog | tenant_admin | `GET /v1/admin/plugins` |
| Audit events | Page audit events (cursor-paginated) | tenant_admin, auditor | `GET /v1/admin/audit` |

## 8. Security Model (TUI-Specific)

| Threat | Mitigation |
|---|---|
| Stolen API key | Stored in memory only; `/logout` clears; no persistence; short-lived keys recommended |
| Secret disclosure | Never in chat/logs/history; masked in `/secret list`; omitted from `PromptInput.remember()` |
| Approval tampering | Decision bound to `batch_digest` (SHA-256); server rejects digest mismatch; immutable |
| Role escalation | Admin workspace visibility from `/v1/me` capabilities; API enforces RBAC regardless of UI |
| Cross-tenant SSE | SSE requires run ownership; `Last-Event-ID` checked against tenant; RLS on `run_events` |
| MITM to malicious endpoint | TLS verification; profile stores endpoint only; `/login` verifies with server |
| Auto-approve bypass | `--auto-approve` only in `--once` mode; interactive TUI always prompts; server enforces |

## 9. Roadmap (T4+)

| Track | Target | Dependency |
|---|---|---|
| T4 | Home/activity dashboard with role-scoped counts | Backend: recent threads, active runs, approval counts APIs |
| T4 | Team view: live run tree, agent lanes, handoffs | Multi-agent: `runs.root_run_id`, `delegations`, `agent_messages` |
| T4 | Workspace/artifacts: scoped file tree, CAS, provenance | Phase 4: artifact service, workspace entries |
| T4 | Plugins/marketplace: search, consent, digest-pin, advisories | Marketplace: `INSTALLATION`, `CAPABILITY_GRANT`, `ADVISORY` |
| T4 | Connections: mock run, connector readiness, webhook listener | Developer platform: local mock, signed webhook |
| T4 | Operator workflows: fleet ops, cell management, break-glass | Operations: dedicated cells, revocation epochs |

## 10. Acceptance Gates (Track T)

| Gate | Criteria |
|---|---|
| TUI-01 | Journey parity: Chat → Runs → Approvals → Admin flows complete without mouse |
| TUI-02 | Auth/recovery: `/login` → `/tenant` → `/profile` → reconnect preserves state |
| TUI-03 | Safe rendering: untrusted tool output never executes terminal commands; Markdown sanitized |
| TUI-04 | Terminal/accessibility: ASCII mode, theme, focus nav, screen reader, 80x24 usable |
| TUI-05 | Performance: <100ms keystroke-to-render; SSE reconnect <2s; large transcript scroll |
| TUI-06 | Compatibility: works on Linux/macOS/Windows Terminal; SSH; tmux/screen; no raw mode issues |
| TUI-07 | Version skew: TUI gracefully degrades on older servers (capability version check) |

## 4. Delivery waves and backend ownership

Track T supplies individual IDs, dependencies and gates. These waves are consecutive
capability increments, not one large TUI redesign at the end of the roadmap.

| Wave | Ship with | User-visible increment |
|---|---|---|
| T0 bridge | Before Phase 1 implementation | Shell/navigation foundation; existing run listing, reattach, cooperative cancel, problem responses, safe secret form; terminal regression baseline |
| T1 | Identity and tenant authority | Login/logout, profiles and tenant switching, session expiry, role-aware navigation, membership/policy administration and audit |
| T2 | Agent revisions and thread fencing | Agent draft/validate/publish/diff, revision-pinned runs, thread browser/history, branch and conflict recovery |
| T3 | Durable workflows | Authoritative timeline/attempts, replay gaps, recovery evidence, exact-batch approvals, cancellation states and budget ledger |
| T4 | Execution isolation | Artifact explorer, secret bindings without secret readback, sandbox/egress explanations, capability and policy denials |
| T5 | Service operation | Quota/usage views, webhook diagnostics and safe replay, retention/audit, operational health and large-session responsiveness |
| T-M | Marketplace track, incrementally | Catalog first; consent/install/update only after grants and isolation; publisher validation/review reports after publishing APIs |
| T-A | Multi-agent track, incrementally | Run tree first; topology/control and descendant approvals after enforcement; shared workspace, evaluations and A2A/MCP/OpenAI interop after their APIs |
| T-D | Developer platform track | First mock run, local diagnostics, API/SDK examples, connector testing and signed webhook workflow |
| T6 | Cells / before GA | Authorized cell/placement/residency and recovery-evidence views; cross-role journey parity, terminal/accessibility/performance qualification, support diagnostics and compatibility certification |

Token-delta rendering follows `d-token-streaming` when enabled; do not simulate token streaming
from a completed response or make an optional server feature a prerequisite for basic chat.
Every delivered optional feature must still pass the applicable TUI gates.

### API and client prerequisites

The terminal uses the same authorized REST/event/SDK contracts as other clients. No direct
PostgreSQL, Redis, Celery, Flower, registry filesystem, or production shell access.
T0 may extend the current client; migrate shared transport/auth/event handling to the Python
SDK as it becomes available, without a second long-lived implementation.

| Need | Backend owner / requirement |
|---|---|
| Basic run browser and cancel | Phase 0 list/status/cancel/SSE endpoints; disclose finite replay retention |
| Role-aware UI and version compatibility | `d-capabilities`: versioned, identity-scoped capabilities/actions and supported event schemas; server rechecks every action |
| Durable thread and revision browsing | `p2-thread-fencing` and `p2-agent-revisions`: tenant-scoped lists/history, revision metadata, conflict/version responses |
| Reliable inspector and approval inbox | Phase 3 durable events, invocation attempts, scoped approval queries, digest/expiry and decision records |
| Budgets and run trees | Budget reservations/settlements and HACP tree/event APIs; child costs must not be double-counted at the root |
| Artifact and workspace preview | Artifact service and workspace APIs with scoped reads, pagination, content type/size limits, provenance and CAS |
| Marketplace and connector actions | Registry/search/review, installation grants, broker bindings and egress-mediated diagnostics; no client-side import of plugin code |
| Operator workflows | Authorized audit, usage, webhook delivery and retention APIs; replay uses idempotency/destination-effect rules, not an arbitrary retry button |

If one of these query/action contracts is missing, its TUI item includes that API and SDK work
and the corresponding authorization/contract tests. A screen mock is not completion.
Unavailable capabilities are explained or hidden as appropriate; client-side visibility never
replaces server authorization or leaks forbidden inventory.

## 5. Safety, trust and recovery

* Credentials live in an OS credential store where available, or in session memory with an
  explicit warning. Never fall back to plaintext profile files. Secret entry is masked;
  no values in command history, logs, diagnostics, URLs, exports, clipboard, or screenshots.
* Changing profile/tenant cancels local subscriptions, clears tenant caches and pending
  approval context, and revalidates identity. It does not cancel remote runs or submit drafts.
  Production is visibly distinguished; no implicit fallback to `default-tenant`.
* Approval review binds run, tenant, actor, revision and batch digest/expiry from server state.
  An expired, revoked or changed batch cannot reuse an earlier decision. Reconnect re-fetches
  it; no automatic approval retries and no bulk blanket approval across unrelated batches.
* Render tool/plugin/model/remote-agent text as untrusted data. Neutralize terminal control
  sequences, OSC clipboard/title commands, deceptive links and Rich markup injection.
  Bound nesting and payload size. Never interpret rendered content as a local command.
* Distinguish provider/model errors, policy denial, tool failure, unknown effect, stale lease,
  network loss and auth expiry. Unknown external effects require reconciliation, not a
  one-click side-effect retry or a success-shaped fallback.
* Historical run comparison is read-only. Re-run creates a new authorized run with explicit
  revision/input selection; it is not time travel or guaranteed deterministic replay.
* Marketplace badges display verified evidence and residual risk, not a promise of safety.
  Permission expansion requires re-consent; browsing never executes plugin or publisher code.
* Team messages and workspace content carry provenance. Tree actions show affected descendants,
  join/failure policies and budget impact; control appears only after backend enforcement ships.

## 6. Acceptance gates

These are proposed release targets. They are not measurements of the current TUI.
Use Textual Pilot interaction tests, deterministic API/event fixtures, visual snapshots,
contract tests and real-terminal usability checks; screenshots alone do not certify behavior.

| Gate | Required evidence |
|---|---|
| TUI-01 Journey parity | Each shipped user-facing capability has a TUI journey, supported API/SDK operation, role matrix, help/docs and negative/error tests; all core actions work without a mouse |
| TUI-02 Authority | Wrong-tenant/role actions denied server-side; logout, tenant switch, expiry and revocation clear local authority; stale/changed approvals cannot submit |
| TUI-03 Truthful recovery | Disconnect, duplicate/out-of-order/unknown events, replay gap, 429, auth renewal and ambiguous submission tests; no duplicate mutations, false completion or silent data loss |
| TUI-04 Terminal usability | Pilot/snapshot checks at 80x24, 120x40 and 160x50 plus resize mid-dialog; no clipped decisions/focus traps; light/dark/high-contrast, no-color, ASCII and reduced-motion modes; real-terminal keyboard/copy checks |
| TUI-05 Content and secret safety | Canary secrets absent from history/logs/exports/diagnostics; adversarial ANSI/OSC/Rich/link/oversized payload fixtures cannot trigger actions, corrupt shell state or exfiltrate content (SEC-26) |
| TUI-06 Responsiveness | On the declared CI benchmark (2 vCPU, 4 GiB, Python 3.12, local stub), cold launch to usable offline shell <=2 s; p95 input/action feedback <100 ms under 100 events/s for 60 s; client RSS <=250 MiB with 10,000 replayed events |
| TUI-07 Compatibility and onboarding | Supported client/server and event-version matrix; unknown fields/events safely indicated; first mock run plus approval <=5 minutes without provider keys; documented line-oriented/JSON alternatives and manual assistive-technology checks |

Performance requires bounded event buffers, virtualized/paged lists, expandable payloads and
render batching. The full history remains server-side; truncation or evicted client content is
visible and recoverable through authorized paging. Publish benchmark commands, fixtures,
hardware, results and regressions in CI artifacts.

Do not claim screen-reader accessibility from Textual snapshots or terminal color contrast
alone. Test with real assistive workflows, provide equivalent non-interactive commands, and
document remaining limitations. Notifications are opt-in, redacted, and disabled by default
for shared-terminal or presentation use.

## 7. Definition of done

For each Track T item: implement the supported API/client/UI path, fixtures, keyboard journeys,
relevant gates above, current usage docs and migration notes. For each backend feature:
name its TUI counterpart and ship it in the same capability milestone, or record an explicit
non-user-facing exemption. Backend groundwork may merge first; the user-facing capability is
not release-complete while its required TUI journey is missing.

Track availability separately from roadmap completion. Disabled, policy-forbidden,
unsupported-server and not-yet-implemented features must never be represented as usable.
