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

The web package owns its npm dependencies and binaries. The linked SDK is built
from its own package first; the service lock contains the SDK as a local link
and does not duplicate its transitive runtime dependencies. For standalone client package installation
and peer setup, follow the [client package instructions](../client-ts/README.md).

## Live workspace (default)

The default UI is the new Miller-column research workspace. The old
`src/components/atlas` service-web implementation has been retired after
replacement parity tests; this does not remove core marketplace or public SDK APIs.

- Submit any query to start actual research through `AtlasExplorationClient`.
  Unknown queries never become sample answers. Columns are derived from canonical
  graph entities, facets, relationships and sources—not fixture templates or names.
- Known facets reuse results; unknown facets research only the selected ancestry.
  Parent columns remain visible. New searches invalidate stale callbacks and cancel
  the preceding remote operation. Cancellation success is shown only after the
  client acknowledges remote cancellation; failures remain explicit.
  Cancelling before a run ID arrives is labeled requested/unconfirmed, not
  acknowledged; the client cancels any subsequently completed submission.
- The inspector retains sourced, conflicting and unsupported claims, identity
  references, observed timestamps and source records. Typed `claim.details` render
  price/unit/uncertainty, specialties/credentials, checklists and unsent action drafts
  next to their claim evidence. Compare selected entities in a scrollable table;
  missing values are labeled “Not reported”, never estimated by the UI.
- Connection maps show actual direct graph neighbors, canonical IDs, direction
  and evidence status. No inferred person-to-person edges are created.
- **Save new artifact snapshot** explicitly creates a new immutable HaaS artifact.
  The artifact and run IDs are selectable/copyable. Load by artifact ID or list
  recent saves scoped to the current run. API errors (including storage errors)
  do not fall back to browser storage or claim success.
- Approval review displays actual server batch arguments, destinations and digest,
  including every tool call. Approve/reject use `decideApproval` with the exact
  batch digest. Only server responses change approval state. Separate-authorizer
  and approver-role enforcement remain server responsibilities; 401/403 errors
  are explicit. Dismiss closes the dialog without making a decision.
- Browser back/forward restores in-memory paths or explicit artifact references.
  URLs/history contain only query/path/artifact references, never snapshots or
  credentials. Reopening a query URL fills the form but does not silently start a
  billable run. Paths without an artifact are not durable across reloads.

`src/explorationAdapter.ts` is the typed injected UI boundary; `src/liveAdapter.ts`
implements it using only exported Atlas/public HaaS SDK methods. Offline DOM and
adapter tests mock that boundary or those public methods, never replace real live
results with fixtures. No new runtime or test dependency was required.

## Independent operator approval page

`?atlasApprover=1` opens the standalone approval page; an optional `batch=<id>`
reference fills its batch input. It never starts research or initiates an external
action. The operator must explicitly load the batch and open its actual request
before approving or rejecting it.

The page reads the authenticated identity from public `/v1/me` on **its own
origin**. It does not choose credentials or switch roles. A connection without an
`approver` role cannot load batches through the page; missing authentication is
shown as 401. The role display is not proof of a separate principal: core must
enforce distinct-authorizer, tenant, expiry and digest constraints on every decision.
Self-approval 403 remains visible and never becomes a fabricated approved state.

`src/approverAdapter.ts` calls public `getApproval` and `decideApproval`, sending
only the explicit decision, actual batch digest and optional rejection reason.
All tool destinations/arguments and the server expiry are visible in the shared
review dialog. Dismiss makes no decision. A server “approved” status is **not**
confirmation of external execution.

For local development, launch the independent operator proxy explicitly:

```sh
npm --prefix services/atlas-explorer/web run dev -- --mode approver
```

It listens at `http://127.0.0.1:4318`; the usual submitter window uses port 4317.
The workspace offers an operator-window link only after its same-origin
`GET /__atlas/dev-session` returns `{role:"submitter",developmentOnly:true}`.
That endpoint is a navigation hint, not authentication. Nothing launches
automatically, and links carry only the batch reference and approver-page flag.
The operator proxy restricts routes and verifies independent same-tenant
principals; the page also reads actual `/v1/me`. Normal mode never selects the
approver credential. This page has no key entry or cross-origin authenticated fetch.

Production has **no default approver origin** and does not request development
metadata. Operators may place `<meta name="atlas-approver-origin"
content="https://independent-operator.example">` in the served document. Only an
independent HTTPS origin (or local loopback HTTP origin) without credentials,
paths, queries or fragments is accepted; this is public navigation configuration,
not authority. The operator deployment must supply its own server authentication.
Without that configuration, copy the displayed batch ID into the independently
configured operator window/session. The default submitter connection is never upgraded.
Exact external-action initiation is not implemented by this page or the research
workspace; current research preloads only `web-research`, and drafts remain unsent.
That workflow awaits the bounded deterministic action client/core contract.

## Fixture-only playground

Use `?atlasPreview=1` (or “Open sample playground”) for the approved sample
experience. It contains only synthetic local samples: it never constructs a HaaS
client, searches live sources, fetches example URLs, executes actions, or saves
changes. Unknown queries are reported as unavailable rather than mapped to a
sample. The preview links back to the live workspace.

Its expandable connection view shows explicitly declared, synthetic direct
relationships only, including direction and sample provenance. It does not infer
connections from shared organizations, perform live graph research, or persist
visited paths. “Already explored” refers only to the same entity selected during
this in-memory fixture session.

The web package owns the React DOM test toolchain:

```sh
npm --prefix services/atlas-explorer/web run test
```

## Deployment and non-goals

Build output is under `dist/` and can be deployed independently as static
assets. A deployment must arrange same-origin `/v1` routing as an operator
prerequisite with server-authenticated authorization and tenant scope. The browser
constructs `HaaSClient` using `window.location.origin` (the SDK appends `/v1`);
it never reads environment files, calls `createClientFromEnv`, or stores credentials.
The development proxy is a separate, loopback-only server concern; outside local
development unauthenticated requests must fail closed. Never put credentials in
the bundle or expose the development proxy as a production authentication service.
This UI does not implement
authentication, an Atlas backend, or health/readiness beyond the separate
service health endpoint. No package is published by this slice.
