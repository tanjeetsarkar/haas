# `haas-chat` — TUI usage guide

An interactive terminal workbench for the running HaaS stack. Chat, browse tenant-scoped
runs, reattach to their event stream, review approvals, and request cooperative cancellation.

The current Chat/Runs/Help shell is the first increment of the
[terminal experience design](design/tui-experience.md). Identity administration,
marketplace operations and multi-agent controls remain future roadmap work.

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
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
uv pip install -e ".[tui]"                 # once; installs Textual + the `haas-chat` / `haas-plugin` commands
haas-chat                                  # or: python -m haas_cli.chat
```

Useful launch options (all optional):

```bash
haas-chat --url http://localhost:8000 --tenant fin-ops
haas-chat --preload github-ops weather     # skills active from the first message
haas-chat --thread thd_ab12cd34ef56        # continue an earlier conversation
haas-chat --planner deepseek/deepseek-chat-v3-0324 --max-steps 10 --budget 0.5
HAAS_SECRET_GITHUB_TOKEN=ghp_xxx haas-chat --preload github-ops
```

If the API is down you still get the UI, with a red banner. Start the stack, then send any message to retry.

## 2. The screen

```
● http://localhost:8000  tenant default-tenant              ← (1) connection dot (green ok / red down) + tenant
   Chat                         Runs                         Help
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
| **Status bar (6)** | `thread` id (copy it to resume later), `idle` or `step N`, **cumulative cost** of this conversation, skills the agent has active, your preloaded skills, number of secrets set (names only; never values) |
| **Tool cards** | `⏺` dispatched/running, `✔` succeeded, `✘` failed (red). The preview is the first ~140 chars of the tool output. A failed tool is normal: the agent reads the error and usually retries |
| **Dim `· step N` lines** | One per LLM step: which model ran and which skills were active |
| **Run footer `✓ done`** | Steps used, USD cost of that run, cached prompt tokens |
| **Runs** | Tenant-scoped cursor-paginated run list with status, thread, steps and cost. Select a run to attach or, for running/paused runs, request cancellation |

## 3. Keyboard

| Key | Action |
|---|---|
| `Enter` | Send the message / run the command. On a *partly typed* command (e.g. `/sta`) it completes it instead; press `Enter` again to run |
| `Ctrl+J` (or `Shift+Enter` where the terminal supports it) | New line inside the message (for pasting logs/CSV, type or paste directly) |
| `Tab` | Complete the `/command` or argument being typed (accepts the highlighted suggestion; never leaves the prompt) |
| `↑` / `↓` | While several suggestions are shown: choose one. Otherwise browse sent messages (when the cursor is on the first/last line). `/secret …` lines are never stored |
| `Esc` | **Detach** from the running task; inside an approval dialog it leaves the run pending |
| `Ctrl+1` / `Ctrl+2` | Switch to Chat / Runs |
| `F6` | Move keyboard focus to the next control |
| `Ctrl+R` | Refresh the current run list |
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

| Command | What it does |
|---|---|
| `/help` | List commands |
| `/status` | API URL + health, tenant, thread, models, limits, preload list, secret **names**, total cost |
| `/skills` | Installed skills with description, tools and the secrets each may read |
| `/tools` | Registered tools and queue; `⚠` marks tools that need your approval |
| `/preload <skill> […]` | Skills activated before the first step of every run. `/preload clear` empties the list; `/preload` shows it |
| `/secret set NAME VALUE` | Set a per-run secret (memory only). `/secret list` (masked), `/secret unset NAME` |
| `/model <name>` · `/planner <name>` | Override the primary / planner (step 1 and error recovery) model. `default` resets |
| `/steps N` | Max LLM steps per run (1–50; server default 15) |
| `/budget USD` | Max cost per run (above 0, up to 10; server default 0.30) |
| `/thread [id]` | Show the current thread id, or switch to another one |
| `/new` | New conversation: new thread, cost counter reset (settings and secrets are kept) |
| `/clear` | Clear the screen only |
| `/quit` | Exit (`/exit`, `/q` also work) |

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
  Reattach from Runs to review it again.
* Read the JSON arguments: they are exactly what will be executed. Several tools in one batch are approved or
  rejected together.

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

## 7. Skills: preload vs automatic

Normally the agent sees a catalogue of skill descriptions and decides to activate one. Smaller/cheaper models sometimes
just chat instead. **`/preload <skill>` (or `--preload`) guarantees the skill is active from step 1** and is the most
reliable way to use a specific skill. Name the tool or the task explicitly in your prompt for best results.

## 8. Recipes for the sample skills

Run `/skills` first to see what the server has installed. Replace the examples with your own data.

| Skill | Setup | Try |
|---|---|---|
| `weather` | `/preload weather` | `Weather in Pune for the next 3 days, in °C` |
| `web-research` | `/preload web-research` (+ `HAAS_SECRET_BRAVE_API_KEY` for search) | `Read https://example.com and summarise it with a link` · `Compare the latest Postgres and MySQL release notes` |
| `github-ops` | `HAAS_SECRET_GITHUB_TOKEN=…`, `/preload github-ops` | `List open bugs in owner/repo` · `Is PR 42 in owner/repo ready to merge? check CI` · `File an issue about X in owner/repo` (→ approval) |
| `incident-triage` | `/preload incident-triage` | Paste logs (`Ctrl+J` for newlines or paste) + `40% of users can't check out, no workaround. Triage.` |
| `data-analyst` | `/preload data-analyst` | Paste a CSV, then `Profile it and show revenue by region` or give a URL: `Load https://…/data.csv as sales` |
| `plugin-creator` | `/preload plugin-creator` | `Draft a skill + tool that looks up customers in our CRM REST API` |
| `revenue-ops` (demo) | `HAAS_SECRET_ANALYTICS_DB_PATH=/tmp/demo.db HAAS_SECRET_BILLING_WEBHOOK_SECRET=x`, `/preload revenue-ops` | `Investigate Q3 revenue anomalies and prepare a refund` (→ two approvals) |

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
* **Tenant identity is trusted from the header** by the server; use `--tenant` only on networks you control.
