---
description: "Use when editing HaaS plugin packs or plugin services: plugins/**, services/**. Enforces extension independence from core internals."
applyTo: "plugins/**,services/**"
---
# Extension rules (see AGENTS.md §1)

- Depend on public surfaces only: `haas_plugin_sdk` for pack tools; the REST API / `haas_sdk` /
  `@haas/sdk` for services. Do not add new `from app...` imports.
- `plugin.json` keys are strict; tool and skill names are flat, globally unique and stable (persisted in
  run history).
- Secrets only via `allowed_secrets` + `ctx.get_secret`; outbound URLs through `validate_safe_outbound_url`.
- Plugin-owned data, migrations, compose services, clients and UI live in `services/<id>/`, never in core.
- Need something core lacks? Stop and describe the generic capability (`NEEDS_DECISION`); do not patch core.
