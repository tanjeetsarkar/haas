---
name: plugin-creator
description: "Designs and drafts new HaaS plugin packs: plugin.json, skills (SKILL.md + references), Python tools and external HTTP/MCP endpoints, following this harness's conventions. Use when the user wants to create, extend, review or debug a plugin, skill, tool or endpoint, or asks how plugins, approval gating, secrets or tool queues work."
license: Apache-2.0
compatibility: "Advisory skill with no tools: it produces file contents in the chat; the user saves them (see `haas-plugin new …` and `haas-plugin doctor`)."
metadata:
  version: "1.1.0"
  required_tools: ""
  allowed_secrets: ""
---

# Plugin Creator

You cannot write files. Produce complete, copy-ready file contents and tell the user where to save them.

## Procedure

1. **Clarify the capability.** Ask only what you cannot infer: the job to be done, example user requests,
   the system/API involved, which actions change state (→ approval), which secrets are needed, read-only vs write.
   **Done when:** you can state the skill's trigger phrases and the tool list in one sentence each.
2. **Choose the shape** using the table in `plugin_anatomy.md` (via `read_skill_reference`):
   instructions only → skill; custom logic or computation → Python tool; existing HTTP/MCP service → endpoint.
   Everything lives in one plugin pack `plugins/<id>/`; add to an existing pack when the domain matches.
3. **Draft the files** in this order, each in its own fenced block with its path as a heading:
   - `plugins/<id>/plugin.json` — id (= directory), description, version, `category`, `provides`, `requires`.
   - `plugins/<id>/skills/<name>/SKILL.md` — follow `skill_authoring.md` (frontmatter rules, body structure, size).
   - `plugins/<id>/tools/<name>.py` — follow `tool_authoring.md` (args schema, errors, SSRF-safe HTTP, secrets, approval).
   - `plugins/<id>/endpoints/<name>.json` — follow `endpoint_authoring.md` for remote tools.
   - Tests: one pytest per tool using `httpx.MockTransport`; cover success, each remediation error, and secret denial.
4. **Self-review** against the checklist at the end of `plugin_anatomy.md`; fix violations before showing the draft.
5. **Hand off** with the exact commands:
   `haas-plugin new plugin <id> …` (scaffold) → paste the drafted contents → `haas-plugin doctor` →
   `docker compose restart haas-api worker-orchestrator worker-tools` → try it with `haas-chat --preload <skill>`.

## Rules

- Names: skill = lowercase-hyphen and identical to its directory; tool = `snake_case`, prefixed by domain (`github_…`).
- Anything that writes, sends, spends or deletes gets `requires_approval=True`. Say so in the SKILL.md steps.
- Secrets are declared in `metadata.allowed_secrets` and read only through `ctx.get_secret()`; never hard-code or print them.
- Never suggest bypassing the SSRF guard, sandbox queue or approval gate.
- Prefer a small, sharp skill over a broad one; keep SKILL.md under ~100 lines and move detail to `references/`.

References: `plugin_anatomy.md`, `skill_authoring.md`, `tool_authoring.md`, `endpoint_authoring.md`.
