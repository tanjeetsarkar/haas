# Plugin architecture (plugin packs)

Status: implemented. Supersedes the flat `plugins/skills|tools|manifests` layout of spec v3.0 §5.5.

For the complete service design and production tenancy boundaries, see the
[HaaS design set](design/README.md). Tool handlers execute on tool workers, but native
plugin modules are imported during API and orchestrator discovery too. Both tool
queues currently share one worker service; they are not per-run security sandboxes.
The [architecture](design/architecture.md) and [threat model](design/threat-model.md)
qualify the isolation and secret-scoping descriptions below.

## Why change

The original layout grouped files **by type**. A skill and the tools it unlocks lived in different trees and were
linked only by name strings (`metadata.required_tools`), so there was no unit that could be versioned, enabled,
denied or inspected; tool and skill names collided silently; a skill whose tools were missing was still advertised to
the model; one broken file aborted API and worker startup; and every skill went into one global prompt catalog.

## What we took from OpenClaw (and what we did not)

Compared against OpenClaw's skills and plugin docs (`docs/tools/skills.md`, `docs/plugins/architecture.md`,
`docs/plugins/manifest.md`, `docs/tools/plugin.md`).

| OpenClaw idea | Harness decision |
|---|---|
| Plugin = unit of packaging with a manifest read **without executing code** | Adopted: `plugins/<id>/plugin.json` ([app/core/plugin_manifest.py](../app/core/plugin_manifest.py)) |
| Discovery → enablement/validation → runtime load → registry consumers | Adopted: static phase (manifests, policy, dependency order) then runtime phase per plugin |
| Failed plugins are isolated and kept as diagnostics | Adopted: rollback + `registry.plugins[...].error`; `PLUGINS_STRICT=true` aborts instead |
| `plugins.allow` / `deny` / per-plugin `enabled` | Adopted: `PLUGINS_ALLOW` / `PLUGINS_DENY` / `"enabled"`; deny wins, allow is exclusive |
| Multi-root discovery with precedence, grouped skill folders | Adopted: `PLUGINS_EXTRA_DIRS` (lower precedence), `skills/<group>/<name>/` up to 3 levels |
| Inventory vs readiness vs visibility; `disable-model-invocation`; agent allowlists | Adopted: ready = all tools exist; `metadata.disable_model_invocation`; tenant + per-run allowlists |
| Prompt budget with graceful degradation | Adopted: grouped catalog, descriptions shortened, then names only (`SKILLS_PROMPT_MAX_CHARS`) |
| `plugins list/inspect/doctor` | Adopted: `haas-plugin list/inspect/doctor` |
| Session snapshots / pinned revisions | Adapted: registry fingerprint stored per run, drift flagged on `step_started` |
| Path containment for skill roots | Adopted: manifest paths and `SKILL.md` must resolve inside the pack; symlinked skill dirs not followed |
| In-process, unsandboxed native plugins | **Rejected**: tools keep running only in Celery tool workers (fast/sandbox queues, read-only mounts) |
| Secrets injected into the process env | **Rejected**: per-run scoped secrets via `RunContext.get_secret`, limited to active skills' `allowed_secrets` |
| Model reads `SKILL.md` from disk | **Rejected**: `activate_skill` returns the SOP *and* unlocks tools (least privilege) |
| Env/binary gating (`requires.env/bins`) | **Not applicable**: harness secrets are per-run request data, unknown at boot |
| Capability zoo (providers, channels, speech…), ClawHub, Skill Workshop, per-user libraries | Out of scope; agent-authored skills are unsafe in a multi-tenant server |

## Compatibility invariants

1. `registry.tools` / `registry.skills` and `resolve_*` keep their shape; resolution gained an optional
   `allowed_skills` argument, so dispatch, HITL and secret scoping in `tasks.py` are unchanged.
2. Skill and tool names are **flat and globally unique** (they are persisted in `active_skills_json` and message
   history). The plugin id is provenance, never a name prefix; cross-plugin duplicates fail the later plugin.
3. API and workers run the same deterministic discovery from read-only mounts (no network access).
4. The Tier-1 catalog is deterministic (sorted) for prompt-cache stability.
5. REST changes are additive (`skills` on `POST /v1/runs`; `plugins`/`fingerprint` and per-skill flags on
   `GET /v1/plugins`). The `runs` table gets two nullable columns (`skill_allowlist_json`, `registry_fingerprint`).
6. `SKILL.md` stays Agent-Skills compliant; custom keys live under `metadata`.

## Layout and manifest

```
plugins/<id>/
├── plugin.json
├── skills/[<group>/]<name>/SKILL.md (+ references/)
├── tools/*.py            # register(registry)
└── endpoints/*.json      # HTTP / MCP tools
```

`plugin.json` keys: `id` (= directory), `description`, `name`, `version`, `category`, `enabled`, `skills`, `tools`,
`endpoints` (paths/globs inside the pack), `provides` (`skills`, `tools`) and `requires` (`plugins`). Unknown keys are
errors, so typos are caught. Full table: [plugins/_templates/README.md](../plugins/_templates/README.md).

## Load pipeline

1. **Static**: scan each root (`PLUGINS_DIR`, then `PLUGINS_EXTRA_DIRS`) → read manifests (no code runs) → first
   root wins for duplicate ids → apply policy (deny > allow > `enabled`).
2. **Runtime** (dependencies first): register skills, import tool modules (`haas_plugin_<id>_<stem>`), register
   endpoints, verify `provides`. Any failure rolls the plugin back and records it.
3. **Finalise**: entry-point plugins (`haas.plugins`, id `entrypoint:<name>`), skill readiness, registry fingerprint.

Readiness: a skill is offered and activatable only if every tool in `required_tools` exists. Hidden skills
(`disable_model_invocation`) are usable only through `preload_skills`.

## Visibility and enforcement

For authenticated tenants, the effective skill allowlist is derived from the active immutable
policy revision's plugin ID, exact artifact digest, and explicit skill grants, then narrowed
by the run's `skills`. The selected policy revision is pinned on the run. Development
header-tenancy retains the legacy `TENANT_SKILL_ALLOWLISTS` (`"*"` default) behavior.
The effective list is stored on the run and enforced at: catalog rendering, `preload_skills` validation, `activate_skill`,
`read_skill_reference`, the tools offered to the model, the unlock check on the tool worker, and secret scoping.
The catalog in a thread's first system message is only a hint; enforcement is authoritative.

## Operations

* Fingerprint: `haas-plugin list` and `GET /v1/plugins` show it; runs store the API's value and workers flag a
  mismatch with `registry_drift: true` in `step_started` events (rolling restarts / stale mounts).
* Restart is still the reload mechanism. Authenticated requests verify tenant membership;
  `X-Tenant-ID` is only a tenant selector. The dev header bypass is not a production identity mechanism.
* Migration from the flat layout: `haas-plugin migrate [--dry-run]` (skills sharing tool files are merged into one pack).
