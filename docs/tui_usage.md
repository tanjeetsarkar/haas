# `haas-chat` — TUI usage guide

An interactive terminal workbench for HaaS. Chat, browse tenant-scoped runs, reattach to
their event stream, review approvals, request cooperative cancellation, and—when authorized—
administer tenant access and policy.

Chat, Runs, identity/profile switching, and the role-aware Admin workspace are the current
increments of the [terminal experience design](design/tui-experience.md). Marketplace,
multi-agent and production-operations controls remain future roadmap work.

## TUI ↔ Harness Service Architecture

The TUI is a **client application** that connects to the HaaS service over standard APIs.
It does not run agents locally—all agent execution happens in the HaaS service.

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

* [1. Start it](#1-start-it)
* [2. The screen](#2-the-screen)
* [3. Keyboard](#3-keyboard)
* [4. Slash commands](#4-slash-commands)
* [5. Approvals (human-in-the-loop)](#5-approvals-human-in-the-loop)
* [6. Secrets](#6-secrets)
* [7. Skills: preload vs automatic](#7-skills-preload-vs-automatic)
* [8. Recipes for the sample skills](#8-recipes-for-the-sample-skills)
* [9. One-shot / scripting mode](#9-one-shot--scripting-mode)
* [10. Settings reference](#10-settings-reference)
* [11. Troubleshooting](#11-troubleshooting)
* [12. Behaviour to know about](#12-behaviour-to-know-about)

---

## 1. Start it

```bash
uv pip install -e ".[tui]"                 # once
haas dev up                                # isolated Compose project; deterministic mock provider
haas dev seed                              # prints separate one-time submitter/admin and approver keys
haas chat --url http://127.0.0.1:18000 --tenant local-dev
# In the TUI, use /login and paste the submitter/admin key.
```

`haas dev down` stops this isolated project and preserves its data volumes. `haas-chat` remains
available as an alias; the standard `docker compose` workflow is also supported.

Useful launch options (all optional):

```bash
haas-chat --url http://localhost:8000 --tenant fin-ops
haas-chat --preload github-ops weather     # skills active from the first message
haas-chat --thread thd_ab12cd34ef56        # continue an earlier conversation
haas-chat --planner deepseek/deepseek-chat-v3-0324 --max-steps 10 --budget 0.5
HAAS_SECRET_GITHUB_TOKEN=ghp_xxx haas-chat --preload github-ops
```

If the API is down you still get the UI, with a red banner. Start the stack, then send any message to retry.

### Production Connection

For production HaaS deployments:

```bash
# Using a saved profile (recommended)
haas profile add prod --url https://haas.example.com --tenant my-tenant --environment prod
haas chat --profile prod
# Then /login with your API key or OIDC token

# Or direct connection
haas-chat --url https://haas.example.com --tenant my-tenant
# Then /login
```

**Important**: Production requires HTTPS, verified OIDC/API key authentication, and an active tenant policy revision. The TUI will show `PRODUCTION` in the top bar when connected to a production environment.

## 2. The screen

```
● http://localhost:8000  tenant default-tenant              ← (1) connection dot (green ok / red down) + tenant
   Chat                         Runs       Admin*              Help
   ◆ HaaS Workbench  distributed agent harness
   Connected … — 7 skills, 23 tools.
                                                            ← (2) chat log (scrolls; mouse wheel / PgUp / PgDn)
  ❯ Weather in Tokyo today? one line                           your message
   · step 1 · deepseek/deepseek-chat-v3-0324 · weather         dim: step, model, active skills
   ✔ get_weather_forecast  {"location": {"name": "Tokyo", …    tool card: ⏺ running → ✔ ok / ✘ error
 █ Tokyo today: Partly cloudy, 19°C, no rain.                  green bar: the agent's Markdown answer
   ✓ done · 2 step(s) · $0.0006 · 0 cached tokens              run footer: steps, cost, cached tokens
⠋ step 2 · deepseek-chat-v3-0324…  (esc to detach)          ← (3) activity line (spinner while a task runs)
/status  /skills  /secret                                   ← (4) command hints while you type "/…"
╭───────────────────────────────────────────────────────╮
│ type here…                                            │   ← (5) prompt box (grows up to 10 lines)
╰───────────────────────────────────────────────────────╯
 thread thd_3412… │ idle │ $0.0006 │ skills: weather │ preload: weather │ secrets: 2     ← status bar
 ^1 Chat  ^2 Runs  F6 Next focus  ^q Quit  Esc Detach
```

| Area | What it tells you |
|---|---|
| **Top bar (1)** | Connection status (green=healthy, red=down, yellow=unknown), environment badge (DEV/STAGING/**PRODUCTION**), profile name, API endpoint, tenant, authenticated principal (first 8 chars), roles |
| **Navigation tabs** | Chat (Ctrl+1), Runs (Ctrl+2), Approvals (Ctrl+4), Admin (Ctrl+3, role-gated), Help |
| **Chat log (2)** | User messages (bold), agent Markdown answers (green bar), tool cards, step lines, run footers, command output, notes |
| **Activity line (3)** | Spinner + current phase (submitting, queued, step N, waiting for approval, running tools, cancelling, recovering) |
| **Command hints (4)** | Slash command completions as you type `/` |
| **Prompt box (5)** | Multiline input (Ctrl+J / Shift+Enter for newlines), Tab completion, history (↑/↓) |
| **Status bar (6)** | Thread ID, run state (idle/step N), cumulative session cost, active skills, preloaded skills, secret count |

## 3. Keyboard

| Key | Action |
|---|---|
| `Enter` | Send the message / run the command. On a *partly typed* command (e.g. `/sta`) it completes it instead; press `Enter` again to run |
| `Ctrl+J` (or `Shift+Enter` where the terminal supports it) | New line inside the message (for pasting logs/CSV, type or paste directly) |
| `Tab` | Complete the `/command` or argument being typed (accepts the highlighted suggestion; never leaves the prompt) |
| `↑` / `↓` | While several suggestions are shown: choose one. Otherwise browse sent messages (when the cursor is on the first/last line). `/secret …` lines are never stored |
| `Esc` | **Detach** from the running task; inside an approval dialog it leaves the run pending |
| `Ctrl+1` / `Ctrl+2` | Switch to Chat / Runs |
| `Ctrl+3` | Open Admin/audit when the verified role permits it |
| `Ctrl+4` | Open the approval inbox |
| `F6` | Move keyboard focus to the next control |
| `Ctrl+R` | Refresh the current run list |
| `Ctrl+F` | Follow/pause the live output preview |
| `Ctrl+T` | Cycle the dark/light/auto theme |
| `F1` | Show contextual help |
| `Ctrl+L` | Clear the screen (conversation continues) |
| `Ctrl+N` | Start a new conversation (same as `/new`) |
| `Ctrl+Q` | Quit |
| `Ctrl+P` | Open the command palette for navigation, run actions, help and theme |
| Mouse | Scroll the log; click buttons in dialogs; select text with the mouse to copy (terminal-dependent) |

While a task runs you can still use `/commands`; plain messages are held back with a notice.

## 4. Slash commands

Type `/` to open the suggestion list under the prompt (command name + description). It narrows as you type:

```
 ▸ /status  Connection, thread, models, budgets, secrets (names only)
   /skills  List installed skills
   /secret  Per-run secret, memory only (also: list, unset NAME)
 Tab complete · ↑↓ choose · Enter runs a full command
```

* `Tab` accepts the highlighted entry (commands that take arguments get a trailing space); `↑`/`↓` move the highlight.
* Arguments complete too: `/preload` → installed skill names (already chosen ones are hidden, plus `clear`),
  `/secret` → `set` / `list` / `unset`, and `/secret unset` → the names of secrets you have set.
* Skill names come from the server (cached after the first connect, `/skills` or `/preload`).

| Command | What it does | API Calls |
|---|---|---|
| `/help` | List commands | — |
| `/login` · `/logout` | Sign in with a bearer credential (memory only) / clear it | `GET /v1/me` |
| `/tenant [id]` · `/profile [name]` | Verify and switch tenant / saved environment profile | `GET /v1/me` (verify), profile local |
| `/status` | API URL + health, tenant, thread, models, limits, preload list, secret **names**, total cost | `GET /v1/me`, `GET /v1/plugins` |
| `/admin` | Open the role-authorized Admin/audit workspace | — |
| `/approve <run-id>` | Review a known pending approval as a verified approver | `GET /v1/approvals/{run_id}`, `POST /v1/runs/{run_id}/resume` |
| `/approvals [STATUS]` | Approval inbox: pending (default), approved, rejected, expired, all | `GET /v1/approvals` |
| `/inspect [run-id]` | Run inspector: state, budget, steps, tool invocations and usage | `GET /v1/runs/{run_id}`, `GET /v1/runs/{run_id}/inspect` |
| `/events [run-id] [after] [type=TEXT]` | Page or search (by event type) the durable, replayable run event log | `GET /v1/runs/{run_id}/events` |
| `/live [on\|off]` | Advisory live token preview when the server advertises `output-deltas.v1` | SSE `run.output.delta` |
| `/skills` | List installed skills with descriptions and declared secrets | `GET /v1/plugins` |
| `/preload [skill...]` | Set/clear skills active from the first message | Sent with `POST /v1/runs` |
| `/secret set NAME VALUE` · `/secret list` · `/secret unset NAME` | Per-run secrets in memory only | Sent with `POST /v1/runs` |
| `/model <model-id>` · `/planner <model-id>` | Override primary/planner model for this run | Sent with `POST /v1/runs` |
| `/steps <N>` · `/budget <USD>` | Override step/cost limits for this run | Sent with `POST /v1/runs` |
| `/agent draft` | Create immutable agent draft (JSON form, tenant_admin) | `POST /v1/agents/draft` |
| `/agent show <id>` · `/agent validate <id>` · `/agent publish <id>` | Inspect, validate, publish agent revision | `GET/POST /v1/agents/{id}/validate`, `POST /v1/agents/{id}/publish` |
| `/agent use <id>` · `/agent clear` | Pin/clear agent revision for subsequent runs | Local state |
| `/agent diff <id1> <id2>` | Compare configuration, policy permissions, registry fingerprint | `GET /v1/agents/{id1}/diff/{id2}` |
| `/agents` | List agent revisions with status | `GET /v1/agents` |
| `/threads [search]` · `/threads next` | Search/select thread (cursor-paginated) | `GET /v1/threads` |
| `/history [offset]` | Refresh/browse committed thread history at captured version | `GET /v1/threads/{id}/history` |
| `/branch <version>` | Branch from committed snapshot at exact version | `POST /v1/threads/{id}/branch` |
| `/new` · `/clear` · `/quit` | New thread, clear screen, exit | Local |

## Identity and tenant administration

Use `/login` to authenticate with a tenant-scoped API key or OIDC bearer token; the
credential stays in process memory. `/logout` clears it. `/tenant` verifies a tenant switch
through `/v1/me`; `/profile` changes to a saved endpoint profile and verifies it before the
switch. Profiles contain only endpoint, tenant and environment metadata, never credentials.

The Admin workspace appears only after the server verifies a `tenant_admin` or `auditor`
role. Tenant admins can list/grant/revoke memberships, list/create/revoke API keys, inspect
and activate tenant policy revisions, inspect the tenant-scoped plugin catalog, and page
through audit events. Auditors receive audit-read access but do not see mutation controls.
Every operation is still authorized by the API; a hidden button is only a usability aid.
Human membership grants need a configured OIDC issuer. An API key created in the TUI is
shown once in the result panel and is not saved; copy it securely before leaving the view.

### Admin Workspace Operations

| Button | Action | Required Role | API Endpoint |
|---|---|---|---|
| List members | List all tenant memberships | tenant_admin, auditor | `GET /v1/admin/memberships` |
| Grant member | Add principal with role | tenant_admin | `POST /v1/admin/memberships` |
| Revoke member | Remove principal role | tenant_admin | `DELETE /v1/admin/memberships/{principal_id}/{role}` |
| List API keys | List tenant API keys (hashed) | tenant_admin | `GET /v1/admin/api-keys` |
| Create API key | Generate new API key (shown once) | tenant_admin | `POST /v1/admin/api-keys` |
| Revoke API key | Revoke key by prefix | tenant_admin | `DELETE /v1/admin/api-keys/{key_prefix}` |
| View policy | Show active policy revision | tenant_admin, auditor | `GET /v1/admin/policy` |
| Activate policy | Activate a policy revision (JSON form) | tenant_admin | `POST /v1/admin/policy/activate` |
| Scoped plugins | List tenant-scoped plugin catalog | tenant_admin | `GET /v1/admin/plugins` |
| Audit events | Page audit events (cursor-paginated) | tenant_admin, auditor | `GET /v1/admin/audit` |

## Agent revisions and thread history

These are command-driven Chat workflows; draft creation opens a JSON form rather than
a separate visual agent workspace. A tenant administrator can:

1. Run `/agent draft`, enter a name and structured configuration such as
   `{"primary_model":"your/granted-model","planner_model":"your/granted-model"}`.
   Models, skills and budgets must fit the active tenant policy.
2. Inspect the returned revision with `/agent show ID`, then `/agent validate ID`
   and `/agent publish ID`. Configuration is immutable even in draft: corrections create
   a new version. Validation/publication recheck active policy and registry fingerprint;
   policy/registry changes require a new revision.
3. Use `/agent diff OLD_ID NEW_ID` to review configuration, policy permissions and
   registry changes, then `/agent use ID`. Subsequent prompts send that exact revision,
   not model/budget/preload overrides. `/agent clear` restores request-level settings.

Users with `run.read` can browse revisions and threads; publication needs `tenant.admin`,
and branching/submitting needs `run.submit`. API authorization is authoritative regardless
of command visibility. `/v1/me` capability version `1` drives admin visibility and event
compatibility when present; old servers without metadata retain role-based compatibility.
Unsupported advertised metadata/event versions produce an explicit error, not an
assumption that the transport is compatible.

`/threads search-text` searches thread IDs, `/threads next` advances, and `/thread ID`
selects one. `/history` refreshes committed history and captures its version;
`/history 20` requests a subsequent bounded page at that version. Refresh from `/history`
if a version conflict occurs. `/branch VERSION` copies the **current committed snapshot**
only if that version still matches, records ancestry, and selects the new thread.
It cannot reconstruct arbitrary historical snapshots.

One active/paused run owns a thread at a time. On admission conflict, wait, explicitly
cancel through Runs, or branch; never retry by silently overwriting history. A stale
completion cannot commit over cancellation/newer history. Identity/tenant/new-conversation
changes reset revision/version pins and cursors. SSE replays the durable PostgreSQL event
log (`run-events.v1`), so reconnects resume from the last integer sequence.

The local seed produces a distinct approver identity because the API forbids approving
one's own run. An authenticated submitter cannot approve its own run in the inline dialog;
leave it pending and share the displayed run ID with an approver. Sign in as that approver,
then open the approval inbox with `Ctrl+4` (or `/approvals`), or run `/approve <run-id>`,
to inspect the pending actions and approve, reject, or leave them pending.

### Approval inbox, run inspector and budgets

* **Approvals (`Ctrl+4`)** lists tenant-scoped batches; cycle the filter between pending,
  approved, rejected, expired and all. Each decision sends the reviewed `batch_digest`. If
  the batch changed or expired, the server returns 409 and the detail pane shows that the
  decision was not accepted; press Refresh to reload. `/approvals [STATUS]` prints the same
  list in Chat.
* **Inspector** (Inspect in Runs, the command palette, or `/inspect [run-id]`) shows status,
  state version, event sequence, the W3C trace context for correlating logs and traces,
  and the budget: settled, uncertain and reserved cost against the maximum, remaining
  cost, steps, tool-invocation count and deadline. It also lists per-step attempts, tool
  invocations with their durable outcome and the usage ledger. Uncertain reservations
  count as spent. An `unknown` invocation explains that the effect may have happened and
  is never re-executed by HaaS, and prints its effect key for reconciliation.
* **`/events [run-id] [after] [type=TEXT]`** pages the immutable event log, the same data
  SSE replays; `type=tool` searches up to 500 events per page by event type. Cancellation
  may show `cancelling` while dispatched tools finish.

The equivalent current command-line surfaces are `haas profile`, `haas admin`, and
`haas dev`. For example:

```bash
haas profile add local-dev --url http://127.0.0.1:18000 --tenant local-dev \
  --environment dev --activate
haas profile list
haas admin --url http://127.0.0.1:18000 --tenant local-dev memberships list
```

The `haas admin` commands require `HAAS_API_KEY` or `HAAS_BEARER_TOKEN` in the process
environment. Unlike TUI `/login`, profiles do not persist or retrieve credentials.

## 5. Approvals (human-in-the-loop)

Tools marked `⚠` (for example `github_create_issue`, `github_add_comment`, `prepare_refund_batch`) never run on
their own. The run pauses and a dialog opens:

```
┌─────────────────────────────────────────────────────────┐
│ ⚠  Approval required                                    │
│ The agent wants to run the following action(s). …       │
│ ▸ github_create_issue                                   │
│   { "repo": "me/app", "title": "Crash on login", … }    │
│ [ Rejection reason (optional) ]                         │
│                         [ Approve (y) ] [ Reject (n/esc)]│
└─────────────────────────────────────────────────────────┘
```

* **`y`** or the *Approve* button: the tools run and the agent continues.
* **`n`** or the *Reject* button: nothing runs; the optional reason is passed to the agent, which then
  explains/adapts. Type the reason first (click the field) if you want one.
* **`Esc`** or *Leave pending*: close review without approving or rejecting. The run remains paused on the server.
  Reattach from Runs to review it as an authorized approver, or use `/approve <run-id>` with a separate approver identity.
* Read the JSON arguments: they are exactly what will be executed. Several tools in one batch are approved or
  rejected together.

### Approval Security Model

- **Exact batch digest**: Decisions are bound to the SHA-256 digest of the exact pending tool calls, run state version, and expiry
- **No self-approval**: The API rejects approval from the same principal that submitted the run
- **Attributed decisions**: Every decision records the actor principal, reason, and request ID
- **Single winner**: CAS on approval batch prevents race conditions
- **Expiry**: Batches expire; expired batches cannot be decided

## 6. Secrets

Skills declare which secrets their tools may read (`/skills` shows them). Supply values in either way:

```bash
export HAAS_SECRET_GITHUB_TOKEN=ghp_…      # → secret GITHUB_TOKEN (prefix HAAS_SECRET_ is stripped)
export HAAS_SECRET_BRAVE_API_KEY=…         # → secret BRAVE_API_KEY (enables web_search)
haas-chat
```

or inside the UI: `/secret set GITHUB_TOKEN ghp_…`.

Secrets are kept in the TUI process memory, sent with each run request, never written to disk, masked in `/secret
list`, never shown in the chat, and omitted from the input history. The server only releases a secret to tools of an
**active** skill that lists it. Remember: the value travels in the request body, so use HTTPS/a trusted network if the API is remote.

### Secret Flow

```
TUI: /secret set GITHUB_TOKEN ghp_xxx
       │
       ▼
TUI stores in memory (never disk)
       │
       ▼
User sends prompt
       │
       ▼
POST /v1/runs { ..., "secrets": {"GITHUB_TOKEN": "ghp_xxx"} }
       │
       ▼
API validates: secret declared by active skill?
       │
       ▼
Orchestrator includes secret in tool task envelope (signed)
       │
       ▼
Tool worker verifies envelope, unlocks secret for tool handler only
       │
       ▼
Tool executes with secret → result returned (secret never in output)
```

## 7. Skills: preload vs automatic

Normally the agent sees a catalogue of skill descriptions and decides to activate one. Smaller/cheaper models sometimes
just need a nudge. `/preload skill1,skill2` (or `--preload` at launch) activates those skills for every step of the run,
bypassing the model's skill-selection logic. Preloaded skills still respect the tenant policy allowlist and secret
declarations. Use `/preload clear` to return to automatic selection.

## 8. Recipes for the sample skills

| Skill | Tools | Secrets | Example prompt |
|---|---|---|---|
| `weather` | `get_weather_forecast` | — | "Weather in Tokyo today?" |
| `github-ops` | `github_create_issue`, `github_add_comment`, `github_list_issues` | `GITHUB_TOKEN` | "File a bug for the login crash in my/app" |
| `web-research` | `web_search`, `fetch_url` | `BRAVE_API_KEY` | "Find recent papers on RAG evaluation" |
| `data-analyst` | `sql_query`, `python_exec`, `load_csv` | `ANALYTICS_DB_PATH` | "Load sales.csv and show Q3 trends" |
| `incident-triage` | `create_incident`, `add_timeline_entry`, `generate_postmortem` | — | "Start incident for API 5xx spike" |
| `revenue-ops` | `prepare_refund_batch`, `verify_refund_batch` | `BILLING_WEBHOOK_SECRET` | "Prepare refund batch for order #12345" |

Follow-ups work in the same thread: after loading a CSV just ask the next question.

## 9. One-shot / scripting mode

No TUI, plain text, exit code `0` on success:

```bash
haas-chat --once "Weather in Oslo?" --preload weather
haas-chat --once "Investigate Q3 revenue anomalies" --preload revenue-ops --auto-approve   # approves every gate!
```

Without `--auto-approve` it asks `approve? [y/N]` on the terminal at each gate.

## 10. Settings reference

| Env var | Flag | Default | Meaning |
|---|---|---|---|
| `HAAS_URL` | `--url` | `http://localhost:8000` | API base URL |
| `HAAS_TENANT` | `--tenant` | `default-tenant` | `X-Tenant-ID`; runs/threads are isolated per tenant |
| `HAAS_API_KEY` / `HAAS_BEARER_TOKEN` | `/login` | none | In-memory bearer credential; conflicting values are rejected |
| `HAAS_CLIENT_PROFILE` | – | active saved profile | Select a non-secret endpoint profile |
| – | `--thread` | new | Continue a thread |
| `HAAS_PRIMARY_MODEL` / `HAAS_PLANNER_MODEL` | `--model` / `--planner` | server default | Model overrides (OpenRouter ids) |
| `HAAS_MAX_STEPS` | `--max-steps` | server (15) | Steps per run |
| `HAAS_MAX_COST_USD` | `--budget` | server (0.30) | USD per run |
| `HAAS_PRELOAD_SKILLS` | `--preload` | none | Comma/space separated skills |
| `HAAS_SECRET_<NAME>` | – | none | Run secret `<NAME>` |
| `HAAS_TUI_THEME` | `--theme` | `auto` | `auto`, `dark` or `light`; cycle at runtime with `Ctrl+T` |
| `HAAS_TUI_ASCII` | `--ascii` | false | Use ASCII-friendly status and activity symbols |
| `NO_COLOR` / `HAAS_TUI_NO_COLOR` | `--no-color` | false | Disable terminal colors |

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Red `Cannot reach http://localhost:8000` | Stack not running: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d`; check `curl localhost:8000/readyz`; wrong `--url` |
| `Unknown preload_skills` | Typo or plugin not loaded: `/skills`; after adding plugins `docker compose restart haas-api worker-orchestrator worker-tools` |
| Tool card `✘ … Secret 'X' was not supplied` | `/secret set X …` (or `HAAS_SECRET_X`) and ask again |
| `✘ … Access denied: Secret … not declared` | The skill that owns the tool is not active: `/preload <skill>` |
| Answer contains odd tokens like `<｜tool▁calls▁begin｜>` | Step 1 used `deepseek-r1` which doesn't do tool calls on your provider: `/planner deepseek/deepseek-chat-v3-0324` |
| Agent talks instead of using tools | Use `/preload <skill>` and name the task/tool explicitly |
| `Run failed: … budget` / `max_steps` | `/budget 1` or `/steps 25` and retry |
| `Run failed: … 401` from OpenRouter | `OPENROUTER_API_KEY` in `.env` is invalid; fix and restart the development Compose stack |
| `409 Resume already in progress` | Another operator answered the same approval; nothing to do |
| Broken characters/boxes | Use a UTF-8 terminal with a modern font; `export TERM=xterm-256color` |
| `403 Forbidden` on admin commands | Identity lacks `tenant_admin` or `auditor` role; check `/status` for roles |
| `409 approval_required` on OpenAI-compatible API | Run has pending approval; use TUI or `POST /v1/runs/{id}/resume` to decide |
| SSE reconnects frequently | Check network latency; server may be under load; check `haas dev status` |

## 12. Behaviour to know about

* **Esc detaches, it does not cancel.** The worker continues on the server and may still cost money.
  Use Runs → select a running/paused run → Cancel, then confirm to request cooperative cancellation.
  A tool or external request already in progress may still finish; the UI does not claim a hard kill.
* **Approval Esc leaves the run pending.** Approve and Reject are explicit actions. Closing the dialog
  does not make a decision; reattach from Runs to review the pending batch again.
* **Run history is tenant-scoped and cursor-paginated.** Attached runs replay the event stream from its
  earliest retained entry; retention is finite, so an old run may not replay its complete history.
  The run status/result endpoint is used when the stream no longer contains terminal state.
* **Conversation = thread.** Each message is a new run on the same thread; the server keeps the history. `/new`
  starts a fresh thread. Threads are tenant-scoped.
* **Cost** in the status bar is the sum of the finished runs' `total_cost_usd` in this TUI session.
* **Reconnects:** if the event stream drops, the client resumes from the last event id (up to 5 attempts).
* **Unauthenticated dev tenancy uses the header only as a local selector.** Authenticated
  profiles verify tenant membership with the server; production does not trust header-only identity.
* **Output deltas are advisory.** The final `run_completed` event from the durable log is authoritative.
  Live previews may be truncated; the final answer replaces them.
* **Thread version conflicts:** If `/history` or `/branch` reports a version mismatch, another run
  committed first. Refresh with `/history` and retry.