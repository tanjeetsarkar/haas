---
name: {{name}}
description: "TODO: one or two sentences, third person: what this skill does AND when to use it. Include the trigger words a user would say (e.g. 'Use when the user asks about …')."
license: Apache-2.0
compatibility: "TODO: runtime requirements (network access, secrets the caller must supply per run)."
metadata:
  version: "1.0.0"
  required_tools: "{{required_tools}}"
  allowed_secrets: "{{allowed_secrets}}"
---

# {{title}}

<!--
Authoring rules (delete this block when done):
- Body is loaded into the model context when the skill is activated: keep it short and imperative.
- Put routing/trigger information in `description`, not here. Start the body with execution.
- Numbered steps, each with a checkable outcome. Name tools in `code` exactly as registered.
- Long lookup material (policies, schemas, matrices) goes to references/*.md and is read on demand
  with `read_skill_reference`; mention each file by name below so the agent knows it exists.
- State which actions need human approval and what the agent must never do.
-->

## When to use

- TODO: situations that clearly match this skill.
- Not for: TODO neighbouring tasks that should use another skill or no skill.

## Procedure

1. TODO — read-only discovery first. Call `{{first_tool}}` with …
   **Done when:** you have the facts needed for the next step.
2. TODO — validate / cross-check using `reference.md` (via `read_skill_reference`) if a definition is unclear.
3. TODO — any state-changing tool call (these pause the run for human approval).
4. Summarise: what was found, what was changed, anything rejected or left undone.

## Rules

- Never invent identifiers; only use values returned by tools.
- If a tool returns an error, read the message, fix the arguments, retry once; then report the failure.
- Never print or echo secret values.
