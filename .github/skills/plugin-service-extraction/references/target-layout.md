# Plugin service target layout

```
plugins/<id>/                      # Plugin pack: what the harness loads (static manifest, thin tools)
├── plugin.json
├── skills/<id>/SKILL.md           # SOP for the model
├── tools/*.py                     # thin adapters → service API or core extension points (haas_plugin_sdk only)
└── endpoints/*.json               # preferred: HTTP/MCP tools pointing at the service

services/<id>/                     # Plugin service: independently built, tested and deployed
├── README.md                      # purpose, API, config, how to run with and without HaaS
├── pyproject.toml | package.json  # own dependencies, never added to core pyproject
├── docker-compose.yml             # joins the core network as an override file; core compose never lists it
├── app/                           # service code (domain logic, storage access)
├── migrations/                    # service-owned schema (own DB or own schema), tenant-isolated
├── client-ts/ | client-py/        # plugin-specific client built on top of @haas/sdk / haas_sdk
├── web/                           # plugin UI (if any), depends on the public SDK only
└── tests/                         # unit + contract tests against the service API
```

## Run together

```bash
docker compose -f docker-compose.yml -f services/<id>/docker-compose.yml up
```

## Communication rules

| Direction | Mechanism |
|---|---|
| Model → service | Tool in the plugin pack (endpoint JSON or thin HTTP tool) with scoped secrets |
| Service → harness | Public REST/SSE API via `haas_sdk` / `@haas/sdk` with its own API key |
| Harness → service (events) | Core webhooks / event subscriptions (generic), never direct imports |
| Data | Service-owned store; core DB is never shared with a plugin service |

## Choosing pack-only vs pack + service

- **Pack only**: stateless tools calling third-party APIs (e.g. weather, github-ops).
- **Pack + service**: owns data, long-running jobs, its own UI, or its own deployment/scaling needs
  (e.g. atlas-explorer, web-research's SearXNG).
