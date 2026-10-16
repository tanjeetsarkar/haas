# Plugin anatomy

## Which shape?

| Need | Build | Why |
|---|---|---|
| Teach the model a procedure using existing tools | Skill only | Cheapest; `SKILL.md` body is the SOP |
| New logic, computation, parsing, calling an API directly | Python tool + skill | Full control, typed args, unit-testable |
| Service already exposes HTTP or MCP | Endpoint JSON + skill | No code; HMAC signing and SSRF guard built in |
| Needs long reference material | Skill + `references/*.md` | Loaded on demand, not in every prompt |

## Plugin pack layout

Everything belongs to one plugin pack; `plugin.json` is read without running any plugin code.

```
plugins/<id>/
├── plugin.json                 # id (= dir name), description, version, category, enabled, provides, requires
├── skills/[<group>/]<name>/SKILL.md   (+ references/)   # groups are organisation only
├── tools/*.py                  # register(registry)
└── endpoints/*.json            # remote HTTP / MCP tools
```

- `provides.skills` / `provides.tools` declare what the pack registers; a mismatch fails the plugin.
- `requires.plugins` lists packs whose tools your skills use. Skill and tool names stay globally unique.
- `category` (also settable per skill as `metadata.category`) groups the skill catalog once it is large.
- `metadata.disable_model_invocation: "true"` hides a skill from the model; it is then only usable via preload.
- A skill is **ready** only if all its `required_tools` exist; unready skills are not offered to the model.
- Operators can allow/deny whole plugins (`PLUGINS_ALLOW` / `PLUGINS_DENY`) and restrict skills per tenant or run.

## Loading tiers (why descriptions matter)

1. **Tier 1** — every skill's `name` + `description` sit in the system prompt catalog. The model decides to
   `activate_skill` from this text only.
2. **Tier 2** — activating returns the `SKILL.md` body and unlocks `required_tools`.
3. **Tier 3** — `read_skill_reference` loads a file from `references/` when the body points to it.

## Execution model

- A tool call runs in a Celery worker on `q_tools_fast`, or `q_tools_sandbox` (`sandbox=True`): read-only root
  filesystem, writable `/tmp` and the shared scratchpad volume, non-root, no LLM key or database DSN.
- `requires_approval=True` pauses the run (`input_required`); a human approves or rejects in the TUI/API.
- Tool timeout 25 s, output truncated at 4000 chars, identical repeated calls trip a circuit breaker.
- Secrets arrive per run and only reach tools of **active** skills that list them in `allowed_secrets`.

## Review checklist

- [ ] `name` equals the directory name; lowercase `a-z0-9-`.
- [ ] `description` says what it does **and when to use it**, with user-vocabulary trigger words.
- [ ] Every tool in `required_tools` exists; every secret a tool reads is in `allowed_secrets`.
- [ ] State-changing tools are approval-gated; the SOP says when to call them and what to do if rejected.
- [ ] Steps are ordered, imperative, each with a checkable outcome; read-only investigation comes first.
- [ ] Untrusted content (web pages, tickets, logs) is explicitly treated as data.
- [ ] Tool args have `Field(description=…)` with formats/examples; limits and caps on sizes and counts.
- [ ] Expected failures raise `ToolRemediationError` with an actionable message.
- [ ] Outbound HTTP validates URLs (`validate_safe_outbound_url(..., resolve_dns=True)`), no auto-redirects, timeouts set.
- [ ] `plugin.json` has `provides` (and `requires` for tools from other packs); no `TODO` left.
- [ ] Tests added, `haas-plugin doctor` passes.
