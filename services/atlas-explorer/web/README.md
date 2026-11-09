# Atlas Explorer web UI

This package builds the Atlas Explorer UI as a standalone static application.
The Python service/container remains health-only and does not serve these files.
The UI uses the public HaaS TypeScript SDK and Atlas client; it does not provide
an Atlas backend, authentication, or a production routing configuration.

## Local checks and dependency ownership

Build the public packages first, then build or serve the UI from the repository
root:

```sh
npm --prefix haas_sdk_ts run build
npm --prefix services/atlas-explorer/client-ts run build
npm --prefix services/atlas-explorer/web run build
npm --prefix services/atlas-explorer/web run typecheck
npm --prefix services/atlas-explorer/web run bundle
npm --prefix services/atlas-explorer/web run dev
```

The web package owns its npm dependencies and binaries. Its Atlas contract
fixture is compile-only and is not included in the app project; do not call
`createAtlasManagerContract()`. The linked SDK is built from its own package
first; the service lock contains the SDK as a local link and does not duplicate
its transitive runtime dependencies. For standalone client package installation
and peer setup, follow the [client package instructions](../client-ts/README.md).

## Deployment and non-goals

Build output is under `dist/` and can be deployed independently as static
assets. A deployment must arrange same-origin `/v1` routing as an operator
prerequisite. Never put credentials in the bundle. This UI does not implement
authentication, an Atlas backend, or health/readiness beyond the separate
service health endpoint. No package is published by this slice.
