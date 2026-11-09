# Atlas Explorer service skeleton

This independently buildable service is a health-only starting point for the
future Atlas extraction. It has no Atlas business behavior, data, database,
authentication, or dependency on the HaaS repository's core Python package.

## Run and test locally

From this directory:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
curl --fail http://127.0.0.1:8080/healthz
.venv/bin/python -m pytest -q
```

The health endpoint returns HTTP 200 and exactly `{"status":"ok"}`. It indicates
only that this process can answer HTTP requests (liveness); it does not check
core, a database, credentials, or domain readiness. No core environment or
service configuration is required.

## Container and network

Compose builds only this directory. Create a named network before starting:

```sh
docker network create atlas-explorer-local
docker compose up --build -d --wait
curl --fail http://localhost:8081/healthz
docker compose down
```

Set `ATLAS_EXPLORER_PORT` to change the host port (default `8081`). The service
always listens on container port `8080`. `HAAS_CORE_NETWORK` selects the name
of the external network (default `atlas-explorer-local`); to join an existing
core network, use its actual name, for example:

```sh
HAAS_CORE_NETWORK=haas_default ATLAS_EXPLORER_PORT=8081 docker compose up --build -d
```

The network must already exist. Compose does not start core or create/remove
external networks. The container runs as an unprivileged user and has an HTTP
healthcheck against `/healthz`.

## Explicit non-goals

No behavior or data has been migrated; old tables are retained; drops require
explicit approval. This skeleton adds no database configuration, credentials,
database connection, migrations, business routes, service authentication, or
Atlas data. The health endpoint must not be used as evidence of Atlas readiness.
