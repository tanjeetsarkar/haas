---
name: core-extension-point
description: 'Design and implement a generic HaaS core capability that any plugin can use (plugin-scoped storage, events, config, UI slots, webhooks, SDK surface) instead of special-casing one plugin. Use when: a plugin needs something core lacks, replacing raw ctx.store or app.* access from plugins, adding to haas_plugin_sdk, writing the ADR for a core change.'
---

# Core extension point

Rule (AGENTS.md §1): a plugin's need becomes a **generic** core facility, never a special case.

## Procedure

1. **State the capability generically.** "Plugins can persist tenant-scoped key/value records with TTL",
   not "Atlas can cache entities". Name ≥ 2 plugins (existing or plausible) that would use it.
2. **Check what exists** in `app/core/` and `haas_plugin_sdk/` and the ADR index; extend before adding.
3. **ADR** (architect): `docs/adr/NNNN-<slug>.md` with context, decision, rejected alternatives, security
   implications (tenancy, quotas, secrets), and migration path for current users. Add it to the index.
4. **Contract first** (core-engineer):
   - Public surface in `haas_plugin_sdk` (Protocol/dataclass/Pydantic model). Plugins import only this.
   - Implementation in `app/core/`, injected into `RunContext`/tool context; plugins never receive raw DB
     connections or core settings objects.
   - Namespacing by **plugin id + tenant id**, enforced by core (forced RLS for tables), never trusted from
     plugin input.
   - Quotas/limits and observability (metrics, structured logs with `run_id`, `tenant_id`, `plugin_id`).
   - REST/SSE exposure only if clients need it: additive, OpenAPI snapshot + changelog.
5. **Tests**: unit tests with a fake plugin in `tests/`; isolation test proving tenant A cannot read
   tenant B and plugin X cannot read plugin Y; migration round trip if storage is added.
6. **Docs**: update [docs/plugin_architecture.md](../../../docs/plugin_architecture.md) and the plugin
   templates README when the plugin author experience changes.

## Patterns to prefer

| Need | Pattern |
|---|---|
| Persistence | Plugin-scoped storage API (KV/document with TTL) owned by core, or the plugin's own service DB |
| Plugin-owned relational schema | Lives in the plugin service with its own migrations, never in core `migrations/` |
| Reacting to runs | Core emits versioned events (outbox → webhooks/SSE); plugins subscribe |
| Calling a plugin service | `endpoints/*.json` HTTP/MCP tool with scoped secrets and SSRF allowlist via config |
| Plugin config/env | Declared in `plugin.json`/service config, resolved per run; no plugin env vars in core settings |
| Plugin UI | Separate app in the plugin service, or a generic renderer/slot registry in the core web app |

## Anti-patterns

`if plugin_id == "..."` in core; plugin tables in core migrations; plugin env vars in core compose or
`app/core/config.py`; plugin types in `haas_sdk`/`@haas/sdk`; giving plugins `ctx.store`/settings objects.
