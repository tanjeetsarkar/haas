# Writing SKILL.md

## Frontmatter

```yaml
---
name: github-ops                     # == directory name; a-z, 0-9, single hyphens; ≤ 64
description: "<what it does>. Use when the user asks about <trigger words>."   # ≤ 1024 chars; third person
license: Apache-2.0                  # optional
compatibility: "needs outbound HTTPS and the GITHUB_TOKEN secret"              # optional
metadata:                            # string → string only
  version: "1.0.0"
  required_tools: "tool_a tool_b"    # space/comma separated; unlocked on activation
  allowed_secrets: "GITHUB_TOKEN"    # secrets tools may read while the skill is active
---
```

Top-level `allowed-tools`, `version` etc. from other ecosystems are ignored by this harness; use `metadata`.

## Description formula

`<Verb phrase: capability>. Use when the user <situations in their words>.`

Good: "Investigates revenue anomalies … Use when the user asks about quarterly revenue, MRR changes, duplicate
or failed charges, refunds."
Bad: "Helps with finance." (never triggers) · "Use this for everything GitHub related and more" (over-triggers).

## Body structure

1. One-line stance (what good looks like).
2. `## Procedure` — numbered steps, imperative, tools in backticks. Each step ends with **Done when:** …
3. `## Rules` — hard constraints: approvals, data handling, things never to do, untrusted-input handling.
4. Pointers to `references/` files by exact filename.

## Style

- Short sentences, no marketing. Tell the model *what to do*, not what the skill "is".
- Order: read-only discovery → analysis → state change (approval) → summary.
- Say how to handle errors (retry once with corrected args, then report) and what to do when rejected.
- Keep under ~100 lines; tables and definitions move to `references/*.md`.
- Do not duplicate tool descriptions; tell the model *when* and *in what order*, not how a tool works.
