# Web Research plugin

Searches the public web, reads pages, follows links and produces a cited summary.

## Tools

| Tool | Secret | Notes |
|---|---|---|
| `web_search` | none | Searches via the self-hosted SearXNG instance (`SEARXNG_URL`). |
| `web_fetch_page` | none | Fetches a public page; SSRF-guarded, size-capped, text-only. |
| `web_extract_links` | none | Lists the hyperlinks of a public HTML page. |

No per-run secrets are required. The Brave Search API dependency was removed in 1.1.0.

## Running the SearXNG service

`web_search` calls a self-hosted [SearXNG](https://docs.searxng.org/) instance. The hosting
definition lives in this plugin so the plugin is self-contained.

1. Generate a secret key and put it in your `.env`:

   ```bash
   openssl rand -hex 32
   # .env
   SEARXNG_SECRET=<that value>
   ```

   SearXNG overrides `server.secret_key` from the `SEARXNG_SECRET` env var at load time.
   See `.env.example` for all the `SEARXNG_*` variables.

2. Start the stack with the plugin's compose override merged in:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.dev.yml \
     -f plugins/web-research/docker-compose.yml up -d
   ```

   The `searxng` service joins the default Compose network, so `worker-tools` reaches it at
   `http://searxng:8080` (Docker DNS). The harness SSRF guard is allowlisted for the `searxng`
   hostname via `SSRF_ALLOWED_HOSTS` in `docker-compose.yml`.

3. (Dev only) The dev overlay maps SearXNG to `127.0.0.1:${SEARXNG_PORT:-8888}` so you can
   hit it from a browser or `curl` for debugging:

   ```bash
   curl -s 'http://127.0.0.1:8888/search?format=json&q=postgres+17' | jq '.results | length'
   ```

## SearXNG configuration

`searxng/settings.yml` enables the JSON API (`search.formats: [html, json]`), disables the bot
limiter (`server.limiter: false`) so the worker's automated requests are not throttled, and
binds to `0.0.0.0` so the container is reachable on the Compose network. It is mounted
read-only into the container; edit it here and restart the `searxng` service to apply changes.

## Validation

```bash
haas-plugin doctor
haas-plugin inspect web-research
```

`web_search` is only offered when the `web-research` skill is active; `web_fetch_page` and
`web_extract_links` are always available once the plugin loads.