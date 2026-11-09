# Local HaaS and Atlas Explorer runbook

This runbook starts the HaaS API, its local PostgreSQL/Redis dependencies,
web-research's local SearXNG instance, and the Atlas Explorer web UI. It is for
a trusted developer workstation only.

> **Current boundary:** Atlas UI calls the HaaS core API at
> `http://localhost:8000`. Atlas tools and their cache tables still run in the
> core plugin. The separate `services/atlas-explorer` Python service is
> health-only; do not start it expecting Atlas API routes or a database.
> The browser UI currently has no supported sign-in flow. The steps below use
> the core's unauthenticated legacy development mode and must stay bound to
> loopback. Never use this setup on a shared or production network, and never
> put an API key in browser code or a Vite bundle.

## 1. Prerequisites

- Docker Engine with Docker Compose v2.
- Node.js and npm (Node 20.19+ or 22.12+ for the pinned Vite/Rolldown toolchain).
- `curl` and `jq` for health and plugin checks.
- An OpenRouter API key for live model calls. SearXNG search also requires
  outbound internet access from Docker.
- Optional: `openssl` to generate a SearXNG secret.

Run commands from the repository root. Check that ports `8000`, `3000`, `5432`,
`6379`, and `8888` are available. The API, PostgreSQL, Redis and SearXNG host
ports must remain local: set `HAAS_API_BIND_ADDRESS=127.0.0.1` in `.env`;
the development overlay binds PostgreSQL and Redis to loopback and SearXNG's
override binds its port to loopback. The Atlas UI dev server command below is
also explicitly bound to loopback.

## 2. Configure and start HaaS

Create a local environment file and edit it; do not commit it:

```sh
cp .env.example .env
```

In `.env`, replace `OPENROUTER_API_KEY=sk-or-v1-replace-me` with a valid key.
Keep `HAAS_PROFILE=dev`, set the API bind address to loopback, and set a
non-placeholder SearXNG secret. For example, generate a value with:

```sh
openssl rand -hex 32
```

Put the generated value in `SEARXNG_SECRET`. Keep the sample internal search
URL `SEARXNG_URL=http://searxng:8080`; Docker workers use that service name.
Do not set a browser-facing API key. With the default HaaS configuration,
port 8000 is the API port and port 3000 is allowed by the local CORS settings.

Start the core stack plus the development overlay and web-research SearXNG
override:

```sh
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dev.yml \
  -f plugins/web-research/docker-compose.yml \
  up --build -d
```

Wait for the API readiness check and confirm the local plugin listing:

```sh
curl --fail-with-body --show-error http://localhost:8000/readyz
curl --fail-with-body --show-error \
  -H 'X-Tenant-ID: default-tenant' \
  http://localhost:8000/v1/plugins | jq .
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dev.yml \
  -f plugins/web-research/docker-compose.yml \
  ps
```

`/readyz` checks core dependencies (PostgreSQL and Redis); it does not prove
the model provider, workers, SearXNG, or Atlas research is ready. Confirm the
plugin response includes the `atlas-explorer` skill and its tools. If it does
not, check plugin policy/environment and the API/worker logs before proceeding.

The Compose stack owns a local PostgreSQL container and persistent volumes.
Migrations run as part of startup. This local database is where the current
core Atlas cache tables live; the Atlas Python service does not own those
tables. Do not use production data or run destructive volume-cleanup commands.

## 3. Prepare the TypeScript packages

The web package links the local public SDK and Atlas client. Build in dependency
order from the repository root:

```sh
npm --prefix haas_sdk_ts ci --offline --ignore-scripts --no-audit --no-fund
npm --prefix haas_sdk_ts run build

cd haas_sdk_ts
npm pack --pack-destination /tmp
cd ../services/atlas-explorer/client-ts
npm ci --offline --legacy-peer-deps
npm install --offline --no-save /tmp/haas-sdk-0.1.0.tgz
npm run build
cd ../../..

npm --prefix services/atlas-explorer/web ci --offline --ignore-scripts --no-audit --no-fund
npm --prefix services/atlas-explorer/web run bundle
```

These commands provision the local packages and generate a static bundle; they
do not publish packages. If an offline install fails because a dependency is
not cached, stop rather than silently switching package sources. Use the
repository's reviewed package-provisioning process, then repeat the build
sequence. The web package's `npm run build` and `npm run typecheck` currently
report the same five pre-existing AtlasExplorer TypeScript diagnostics recorded
in the [UI extraction plan](plans/012-atlas-ui-service-extraction.md). Do not
weaken strict TypeScript settings to suppress them. The Vite bundle command
does pass and is sufficient for the local UI server below.

## 4. Start the Atlas UI

Start the Vite development server in a separate terminal:

```sh
npm --prefix services/atlas-explorer/web run dev -- --host 127.0.0.1
```

Open [http://localhost:3000](http://localhost:3000). Use `localhost` (not a
different hostname or port) so the configured local CORS origin matches.
The current UI SDK client defaults directly to `http://localhost:8000`; the
Vite proxy is not an authentication layer and does not change that API URL.
Keep HaaS listening on port 8000 for this browser workflow.

## 5. Explore with Atlas

1. In the Atlas page, enter a focused query, for example:
   `Find public hospitals in Mumbai and list their official services`.
2. Select **Explore** (or press Enter) and wait for the first layer.
3. Review each result's source links, citation snippet, and trust tier. Prefer
   official sources; treat estimated or unverified values as uncertain.
4. Select a drill-down chip to request a narrower layer such as facilities,
   pricing, specialists, or credentials. Atlas creates a branched HaaS thread
   for each drill-down.
5. If an approval prompt appears, read the exact destination and action. Only
   approve a real external action when you explicitly intended it and can
   verify its target. Otherwise reject or dismiss it.

Atlas is a research aid, not medical, legal, pricing, or booking advice.
Verify important details directly with the cited source. Do not submit private
or sensitive personal information in a query.

### Optional API/plugin check

To confirm core discovers the plugin independently of the browser UI:

```sh
curl --fail-with-body --show-error \
  -H 'X-Tenant-ID: default-tenant' \
  http://localhost:8000/v1/plugins | jq .
```

This local API uses development-mode legacy identity. The tenant header is a
local selector, not authentication. Do not expose this API or use this pattern
for multiple users. For authenticated runs, use the supported CLI/TUI login
flow with a tenant-scoped API key or OIDC token; the browser Atlas UI does not
yet accept credentials safely.

## 6. Logs and shutdown

If a run fails, inspect the relevant service logs:

```sh
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dev.yml \
  -f plugins/web-research/docker-compose.yml \
  logs --tail=200 haas-api worker-orchestrator worker-tools searxng
```

Stop Vite with Ctrl+C in its terminal. Stop the Compose services while
preserving local database/Redis volumes:

```sh
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dev.yml \
  -f plugins/web-research/docker-compose.yml \
  down
```

Do not add `-v` unless you intentionally want to delete the local volumes and
all data they contain.

## 7. Troubleshooting

| Symptom | Check |
|---|---|
| `/readyz` fails | Run `docker compose ... ps` and inspect `haas-api`, `postgres`, and `redis` logs. Wait for migrations and dependencies to finish. |
| UI shows a network or CORS error | Confirm API is on port 8000, Vite is on `localhost:3000`, and `.env` keeps the development CORS origin. Restart Compose after `.env` changes. |
| UI returns 401/403 | The browser app has no credential entry. This local recipe requires `HAAS_PROFILE=dev`; never put an API key into the bundle. Use CLI/TUI for authenticated environments. |
| `/v1/plugins` omits Atlas | Check `PLUGINS_ALLOW`, `PLUGINS_DENY`, `TENANT_SKILL_ALLOWLISTS`, and API/worker startup logs. |
| Model call fails | Replace the OpenRouter placeholder with a valid key and inspect `worker-orchestrator` logs. |
| Search returns no useful results | Check `searxng` health/logs, Docker outbound connectivity, and `SEARXNG_URL=http://searxng:8080`. |
| `npm run build` fails with the five AtlasExplorer diagnostics | This is the recorded baseline. Use `npm run bundle` for local serving; do not suppress diagnostics as part of this runbook. |

## 8. Separate deterministic HaaS smoke stack

The isolated `haas dev` stack is useful for a deterministic core smoke test:

```sh
haas dev up
haas dev status
haas dev smoke
haas dev down
```

It uses a mock model and a different API port (`18000`). It is not the Atlas UI
recipe above: the current browser UI targets `localhost:8000`, and the mock
stack is not evidence that live Atlas search/model behavior works.

## References

- [HaaS root setup and development profile](../README.md#deterministic-local-development)
- [Atlas service and web ownership/limitations](../services/atlas-explorer/README.md)
- [Atlas UI package instructions](../services/atlas-explorer/web/README.md)
- [Atlas extraction progress and gates](plans/012-atlas-ui-service-extraction.md)
- [TUI usage guide](tui_usage.md)
