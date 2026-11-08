---
name: core-boundary-check
description: 'Detect plugin-specific code leaking into HaaS core (plugin ids, tool names, tables, env vars, UI, SDK types in app/, migrations/, SDKs, compose) and plugins importing core internals. Use when: reviewing a diff, before/after extracting a plugin, auditing core purity, measuring boundary debt.'
argument-hint: 'Optional plugin id, e.g. atlas-explorer'
---

# Core boundary check

Enforces AGENTS.md §1: *core never knows a plugin by name; plugins depend on public surfaces only.*

## Run

```bash
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py               # all plugins
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas-explorer
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --json         # for tooling
```

- `ERROR` — a core path references a specific plugin. Exit code 1.
- `WARN` — plugin code imports `app.*` instead of `haas_plugin_sdk` (legacy debt; `--strict` fails on it).

## Interpreting results

1. Compare the **error count** with the slice baseline. A slice must never increase it; extraction slices
   must decrease it for their plugin.
2. For each new error, decide:
   - *Real leak* → move it to the plugin/plugin service, or replace it with a generic extension point.
   - *Generic mention* (an example string, a docstring) → prefer rewording; otherwise add an `allow` entry
     with a reason in [boundary.json](./scripts/boundary.json), or put `boundary: allow` in a trailing
     comment on that line. Allow entries are reviewed like code; the reviewer rejects them for real leaks.
3. If a plugin uses names that differ from its id (e.g. `atlas` for `atlas-explorer`, `searxng` for
   `web-research`), add them to `plugin_tokens` in `boundary.json`.

## Configuration

[boundary.json](./scripts/boundary.json): `core_paths`, `skip_dirs`, `extensions`, `plugin_tokens`,
`generic_ids` (plugin ids that are common words; only their tool names and extra tokens are matched),
and `allow` entries `{path, token, reason}`. Editing it is a governance change (requires user approval).
