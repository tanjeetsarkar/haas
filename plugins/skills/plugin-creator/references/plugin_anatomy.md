# Plugin anatomy

## Which shape?

| Need | Build | Why |
|---|---|---|
| Teach the model a procedure using existing tools | Skill only | Cheapest; `SKILL.md` body is the SOP |
| New logic, computation, parsing, calling an API directly | Python tool + skill | Full control, typed args, unit-testable |
| Service already exposes HTTP or MCP | Manifest + skill | No code; HMAC signing and SSRF guard built in |
| Needs long reference material | Skill + `references/*.md` | Loaded on demand, not in every prompt |

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
- [ ] No `TODO` left, tests added, `haas-plugin validate` passes.
