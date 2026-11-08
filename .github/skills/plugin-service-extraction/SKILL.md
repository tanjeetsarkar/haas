---
name: plugin-service-extraction
description: 'Strangler-pattern procedure to move a plugin (e.g. atlas-explorer) and everything it leaked into core (tables, migrations, compose env, SDK types, web components, config) into an independent plugin service under services/<id> with its own storage, compose, client and UI. Use when: extracting atlas or any plugin out of core, creating a standalone plugin service, decoupling plugin data from core DB.'
argument-hint: 'Plugin id to extract, e.g. atlas-explorer'
---

# Plugin service extraction

Goal: the harness runs unchanged with the plugin removed, and the plugin runs as its own service.

## Procedure

1. **Inventory** — run the boundary check for the plugin and group every finding by kind:
   ```bash
   python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin <id>
   ```
   Kinds: core migrations/tables · raw DB access (`ctx.store`) · core config/env · compose services/env ·
   SDK types/clients · web components · docs. Record counts in the epic plan as the baseline.
2. **Decide target shape** (architect, with the user) using
   [references/target-layout.md](./references/target-layout.md): what stays a plugin pack (skills, thin tools,
   endpoints) and what becomes the service (storage, domain logic, UI, client library).
3. **Identify missing core capabilities** — anything the plugin still needs from core must become a
   generic extension point first ([core-extension-point skill](../core-extension-point/SKILL.md)).
4. **Slice order** (each slice keeps the system working):
   1. Core extension points needed (if any).
   2. Create `services/<id>/` skeleton: README, own dependency manifest, health endpoint, own compose file,
      own migrations, contract tests. No behaviour moved yet.
   3. Move domain logic + storage into the service; switch the plugin pack's tools to call the service
      (endpoint tool or HTTP via `validate_safe_outbound_url`) or the generic core facility.
   4. Move plugin-specific client/SDK code and UI into `services/<id>/` (e.g. `services/<id>/web`,
      `services/<id>/client-ts`) depending on the public `@haas/sdk`/`haas_sdk` only.
   5. Remove the coupling from core: compose env/comments, config fields, SDK exports/types, web components.
   6. Core migration cleanup: a **new** reversible migration that drops the plugin's tables from core —
      **hard stop: requires explicit user approval** and a data-migration/export note. Never edit or delete an
      already-applied migration file.
   7. Docs: plugin README, service README, update architecture docs; remove plugin docs from core docs.
5. **Exit criteria**: `check_boundaries.py --plugin <id>` reports 0 errors; core test suite passes with
   `PLUGINS_DENY=<id>`; the service's own tests pass; compose without the service file still boots.

## Gotchas

- Tool names are persisted in run history: keep tool names stable across the move.
- Tenant isolation must be re-established in the service (its own RLS or tenant-keyed storage) — treat it as
  a security requirement with tests, not a detail.
- The tool worker is network- and privilege-restricted; calls to the service need an explicit SSRF allowlist
  entry via configuration, not a hardcoded host.
