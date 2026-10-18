# API compatibility and deprecation policy

Applies to every public HaaS contract: the HTTP API ([openapi-v1.json](openapi-v1.json)),
the event and webhook payloads ([contracts/](contracts/), [asyncapi.yaml](asyncapi.yaml)),
the OpenAI-compatible facade, the CLI's `--json` output and the SDKs. Decision record:
[ADR-0016](adr/0016-api-contract-toolchain.md).

## What can change within `v1`

Allowed without notice (additive):

- New endpoints, optional request fields, query parameters and headers.
- New response fields, new event types, new `run_error.code` values, and new enum
  values in responses. **Clients must ignore unknown fields and event types**, and
  treat an unknown error code as `run.failed`.
- New capability flags on `GET /v1/me` (`capabilities.features`).

Breaking (never silently within `v1`):

- Removing or renaming fields, endpoints, event types or enum values.
- Changing types or semantics, tightening validation, making optional input required.
- Changing authentication, authorization or idempotency behaviour of an operation.

A security fix may tighten validation within `v1` when the threat model requires it.
It must be listed in `api-breaking-allowlist.txt` with its justification and in the
[changelog](api-changelog.md).

## How breaking changes are caught

| Gate | Tool | Fails CI on |
|---|---|---|
| OpenAPI snapshot | `scripts/update_openapi_snapshot.py` + `git diff --exit-code` | any undocumented HTTP contract drift |
| Breaking changes | `oasdiff breaking --fail-on ERR` (pinned `v1.33.0`) against the base commit | breaking changes not in [api-breaking-allowlist.txt](api-breaking-allowlist.txt) |
| Style | Spectral with [.spectral.yaml](../.spectral.yaml) | unversioned paths, camelCase segments, non-snake operationIds |
| Event contracts | AsyncAPI CLI validate + `tests/test_contracts.py` | invalid AsyncAPI/JSON Schema, schema/code drift (error codes, terminal types) |
| Producer conformance | autouse fixture in `tests/conftest.py` | any event emitted by any test that violates `run-events.v1` or has undeclared fields |

## Deprecation lifecycle

1. **Announce.** Mark the operation `deprecated=True`, which is visible in OpenAPI and in
   generated SDKs. Attach `deprecated_route(RouteDeprecation(...))` from
   `app/core/deprecation.py`. Add a changelog entry naming the successor.
2. **Signal.** Every response carries:
   - `Deprecation: @<unix-seconds>` (RFC 9745);
   - `Sunset: <HTTP-date>` (RFC 8594), once a date is set;
   - `Link: <policy>; rel="deprecation"`, plus `rel="successor-version"` when a successor exists.
3. **Window.** The sunset is at least **180 days** after the deprecation date; the
   helper refuses shorter windows. Calls to deprecated operations appear in the per-route
   OpenTelemetry request spans, so tenants still calling them can be contacted.
4. **Remove** only in a new major version (`/v2`, `run-events.v2`). The previous major
   stays served for the full window.

Event schemas follow the same rule. `run-events.v1` and `run-output-delta.v1` evolve
additively. A breaking payload change ships as `.v2` and is advertised through
`capabilities.event_schemas` for at least one deprecation window, so clients can
negotiate.
