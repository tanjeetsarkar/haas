# Plugin authoring guide

A **plugin pack** extends the harness with **skills** (instructions the model loads on demand) and **tools**
(code or remote endpoints the model can call). Everything under `plugins/` is bind-mounted into the
containers (`./plugins:/app/plugins:ro`), so **restart the API and workers to pick up changes**:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml restart haas-api worker-orchestrator worker-tools
```

```
plugins/
├── <id>/                              # one pack per capability/domain; <id> = plugin.json "id"
│   ├── plugin.json                    # static manifest: id, description, version, category, enabled, provides, requires
│   ├── skills/[<group>/]<name>/SKILL.md   # Tier 1: name+description always in context; Tier 2: body on activation
│   │   └── references/*.md            # Tier 3: read on demand via `read_skill_reference`
│   ├── tools/<name>.py               # Python tools: `register(registry)` using @registry.tool
│   └── endpoints/<name>.json         # Remote tools: plain HTTP (HMAC-signed) or MCP Streamable HTTP
└── _templates/                        # these templates (never loaded by the harness)
```

## Fast path: scaffold, edit, doctor

```bash
haas-plugin new plugin github-ops --tools github_list_issues --secrets GITHUB_TOKEN --category devops
haas-plugin new tool github_list_issues --plugin github-ops --skill github-ops   # Python tool (add --approval / --sandbox)
haas-plugin new endpoint billing_lookup --plugin github-ops --protocol mcp_http --secret BILLING_KEY
haas-plugin new skill triage --plugin github-ops                                  # a second skill in the same pack
haas-plugin doctor                                                                # exits 1 on errors
haas-plugin list                                                                  # status of every pack + registry fingerprint
haas-plugin inspect github-ops                                                    # skills, tools, queues, readiness
```

`new` copies the files in this folder and fills in the names; search for `TODO` and replace each one.
`doctor` (alias `validate`) loads everything exactly like the server does, including disabled packs, and also
checks cross-references (see below). Run it before every restart. A flat `plugins/skills|tools|manifests`
layout from older versions is no longer loaded: run `haas-plugin migrate` once.

## `plugin.json`

| Key | Meaning |
|---|---|
| `id` | Required; lowercase `a-z0-9-`, **identical to the directory name**. Provenance only: skill and tool names stay globally unique. |
| `description` | Required; one sentence. |
| `version`, `category` | Shown by `list`/`inspect`; `category` is the default group of the pack's skills in the prompt catalog once it is large. |
| `enabled` | `false` ships the pack switched off (an explicit `PLUGINS_ALLOW` entry turns it on). |
| `skills`, `tools`, `endpoints` | Paths/globs inside the pack. Defaults: `["skills"]`, `["tools/*.py"]`, `["endpoints/*.json"]`. Anything resolving outside the pack is rejected. |
| `provides` | `{"skills": [...], "tools": [...]}`: what the pack must register. A mismatch fails the pack (`doctor` shows why). |
| `requires` | `{"plugins": [...]}`: packs whose tools your skills use; loaded first, blocked if they are missing. |

A pack that fails to load is isolated (its tools and skills are rolled back; the rest keep serving) unless
`PLUGINS_STRICT=true`. Operators control packs with `PLUGINS_ALLOW` / `PLUGINS_DENY` / `PLUGINS_EXTRA_DIRS`.
Authenticated tenant grants pin plugin digests and skills in policy revisions; `TENANT_SKILL_ALLOWLISTS`
is retained only for the development header-tenancy path. Per-run `skills` can narrow, never expand, those grants.

## How the pieces connect

| You write | The harness does |
|---|---|
| `SKILL.md` `description` | Listed in the system prompt catalog (≤1024 chars). The model decides to `activate_skill` from this text alone. |
| `metadata.required_tools` | Tools unlocked only after the skill is activated (space/comma separated). |
| `metadata.category` | Optional group heading in the catalog (defaults to the pack's `category`). |
| `metadata.disable_model_invocation: "true"` | Hides the skill from the catalog and `activate_skill`; it can only be used through `preload_skills` / `haas-chat --preload`. |
| `metadata.allowed_secrets` | The only secret keys tools may read via `ctx.get_secret()` while the skill is active. |
| `requires_approval=True` | The run pauses (`input_required`) until a human approves in the TUI/API. Use for anything that writes, spends, sends or deletes. |
| `sandbox=True` / `"queue": "q_tools_sandbox"` | Runs on the read-only sandbox worker queue (writable `/tmp` only). |

Callers supply secrets **per run** (`secrets` in `POST /v1/runs`; in `haas-chat` use `HAAS_SECRET_<NAME>` or `/secret set`).

## Skill checklist

* `name`: lowercase `a-z0-9-`, ≤64 chars, **identical to the directory name**.
* `description`: what it does **and when to use it**, with the words a user would actually say. Third person.
* Body: imperative numbered steps; each ends with a checkable outcome; read-only investigation first;
  state which steps need approval; list hard "never" rules. Keep it under ~100 lines.
* Move long policy/lookup tables into `references/*.md` and mention them by file name in the body.
* Custom keys go under `metadata` (string → string).

## Tool checklist

* Pydantic `Field(description=...)` on every argument; give formats and examples. This text is prompt.
* One tool = one clear job. Return compact JSON/text (output is truncated at 4000 chars); put big
  results behind filters/limits/pagination arguments.
* Expected failures → `raise ToolRemediationError("what is wrong and how to fix the call")`.
* Outbound HTTP: `async def` + `ctx.http_client`, `validate_safe_outbound_url(url, resolve_dns=True)`,
  `follow_redirects=False` (re-validate manually when following redirects), explicit timeouts.
  Tool timeout is 25 s (`PER_TOOL_TIMEOUT_SEC`).
* Never log or return secret values. Untrusted text (web pages, tickets) is data, not instructions.
* The module is loaded standalone: no sibling imports; files starting with `_` are skipped.
* Add unit tests (see `tests/plugins/` for patterns with `httpx.MockTransport`).
* A skill is only offered when every tool in its `required_tools` exists (it is "ready"); `haas-plugin inspect` shows why not.

## Endpoint checklist (remote tools)

* `protocol`: `http` (POST JSON body) or `mcp_http` (JSON-RPC `tools/call`, MCP `2026-07-28`).
* `endpoint_url` supports `${ENV_VAR:-default}`; must be a public https/http URL (SSRF guard; set
  `SSRF_ALLOWED_HOSTS` for local dev only).
* `secret_key` → request is HMAC-SHA256 signed (`X-HaaS-Signature-256`) and sent as bearer token.
* `parameters` is a JSON-Schema subset: `string|integer|number|boolean|object|array`, `required`, `default`.

## Testing a plugin end to end

```bash
haas-plugin doctor
docker compose -f docker-compose.yml -f docker-compose.dev.yml restart haas-api worker-orchestrator worker-tools
curl -s localhost:8000/v1/plugins | jq '.skills[].name'
HAAS_SECRET_GITHUB_TOKEN=... haas-chat --preload github-ops     # preloading guarantees the skill is active
```
