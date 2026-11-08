---
description: "Use when editing HaaS core code: app/, migrations/, haas_plugin_sdk/, compose files, deploy/. Enforces the plugin-agnostic kernel rules."
applyTo: "app/**,migrations/**,haas_plugin_sdk/**,docker-compose*.yml,Dockerfile,deploy/**"
---
# Core kernel rules (see AGENTS.md §1)

- Never reference a specific plugin here: no plugin ids, skill/tool names, tables, env vars or comments
  about a particular plugin. Verify with
  `python3 .github/skills/core-boundary-check/scripts/check_boundaries.py`.
- A plugin's need becomes a generic extension point exposed through `haas_plugin_sdk` (see the
  `core-extension-point` skill), tenant- and plugin-scoped by core.
- Do not hand plugins raw DB connections (`ctx.store`) or core settings; offer a scoped API instead.
- Tables: `tenant_id` + forced RLS; migrations additive and reversible; never edit an applied migration.
- API changes: additive; regenerate `docs/openapi-v1.json` and add to `docs/api-changelog.md`.
