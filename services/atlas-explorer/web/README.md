# Atlas Explorer web UI

This package builds the Atlas Explorer UI. The UI uses the public HaaS
TypeScript SDK and Atlas client: research and execution data come from the
public HaaS API, while durable workspace drafts and mutation history are served
by the separate Atlas Explorer Python service. The Python service does not
serve the static web bundle. For the complete local setup, ports, credentials
and recovery steps, see the [local runbook](../../../docs/atlas-explorer-local-runbook.md).

Local development uses a loopback-only Vite proxy. It forwards same-origin
requests to the HaaS API and the workspace service, reading the submitter
credential server-side only from the repository-root `.env.haas` file. The
browser bundle does not receive credentials. This development proxy is not a
production authentication or routing configuration; a production deployment
must supply its own server-authenticated routing and authorization.

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
- New selections research only the selected ancestry; completed scoped branches
  reuse cached results. Parent columns remain visible. Navigation invalidates
  stale callbacks and detaches observation, without cancelling remote execution.
  Explicit Cancel uses the public run-cancellation API for a known run. Before a
  run ID arrives cancellation is unconfirmed and requires submission recovery.
- The inspector retains sourced, conflicting and unsupported claims, identity
  references, observed timestamps and source records. Typed `claim.details` render
  price/unit/uncertainty, specialties/credentials, checklists and unsent action drafts
  next to their claim evidence. Compare selected entities in a scrollable table;
  missing values are labeled “Not reported”, never estimated by the UI.
- Connection maps show actual direct graph neighbors, canonical IDs, direction
  and evidence status. No inferred person-to-person edges are created.
- The default durable workspace saves automatically; **Saving**, **Saved**,
  **Save failed** and **Conflict** reflect the server queue, not research completion.
  It does not show **Save new artifact snapshot**. Existing artifact imports,
  readers and export/client APIs remain compatible; artifact and run IDs remain
  selectable/copyable. The default durable workspace has no manual artifact
  snapshot save control. API errors never fall back to browser storage or claim
  success.
- Approval review displays actual server batch arguments, destinations and digest,
  including every tool call. Approve/reject use `decideApproval` with the exact
  batch digest. Only server responses change approval state. Separate-authorizer
  and approver-role enforcement remain server responsibilities; 401/403 errors
  are explicit. Dismiss closes the dialog without making a decision.
- Workspace URLs restore server drafts/committed graphs and recorded execution
  bindings without submitting research. Unresolved intents block fresh dispatch.
  URLs/history contain references only, never snapshots or credentials. Query
  references fill the form without silently starting a billable run.

`src/explorationAdapter.ts` is the typed injected UI boundary; `src/liveAdapter.ts`
implements it using only exported Atlas/public HaaS SDK methods. Offline DOM and
adapter tests mock that boundary or those public methods, never replace real live
results with fixtures. No new runtime or test dependency was required.

### Durable workspace integration checkpoint (043a)

`src/workspaceSaveQueue.ts` provides a receipt-driven serial queue behind an
injected Atlas storage transport. Only adjacent unsent draft checkpoints
coalesce. Queries, operation intents and commits stay ordered; explicit retry
retains the identical mutation ID/body. A conflict blocks writes and fetches
authorized latest state without merging it. Storage failures remain unsaved.

The default workspace uses this queue with aligned 041/042 callbacks. Actual
storage readiness is not assumed: failures remain visible and prevent dispatch.
With local 042 readiness verified, manual artifact save is hidden only in durable
mode; artifact import remains available. No browser-unload write or automatic
retry is introduced.
The storage adapter invokes default browser fetch through `globalThis`, preserving
its required receiver; catalog metadata includes backend timestamps without
requiring graph state. Invalid catalogs still fail visibly, never fall back.

### Public execution drawer checkpoint (043d)

The collapsible run drawer consumes the typed `observeExecution` channel and,
only after the user opens it, reads run status/timestamps, steps, usage and
authorized thread history through existing public `@haas/sdk` methods. Thread
history loads in stable-version pages on demand; older pages require an explicit
button. Only public tool messages render (not system/assistant reasoning);
credential fields are redacted and React renders text rather than HTML.
Each item is UTF-8 bounded at 16KiB with a truncation label, and steps/history/
stream rows share a 200-visible-entry cap. Missing SDK values stay unavailable,
not zero. There is no interval polling, run creation, retry, approval or action.

`AtlasRunDrawerProps` accepts the recorded `runId`, optional saved `threadId`,
events and saved submission. The drawer derives a thread binding from the
matching public event when omitted, and its default API creates a same-origin
public SDK client without reading environment credentials. `detailsApi` is an
optional typed injection point for tests or a parent-owned SDK client.

### Workspace history protocol checkpoint (043b)

The fixed same-origin `/__atlas/workspaces` API adapter and catalog/Continue/
Start afresh component are implemented behind an injected storage boundary.
Navigation awaits the caller's flush; unavailable storage fails visibly.
Prepared/submitting/unknown records block fresh research, including legacy
records without a state. Neither loading nor creating a fresh view dispatches
research. These components are mounted through the durable entry point; a
successful local storage round-trip was verified in the local record dated
2026-11-10; that evidence does not imply production readiness.

`workspaceSession.ts` adapts aligned 041 prepare/outcome/bind callbacks to that
queue. It saves the exact submitted question before prepared intent, awaits
submitting/binding receipts, blocks unresolved work, and recovers a known binding
by retrying its original queue item/ID. Restored canonical graphs are strictly
parsed; restore never executes an operation. Storage failures block execution.

`AtlasDurableWorkspace` is the tested integration entry point: it composes
catalog navigation, meaningful blur checkpoints, server-save receipts, strict
graph reload and the awaited operation adapter. Storage 503 prevents dispatch;
unknown intents block new research. Failed saves retain the exact retry item,
and conflicts offer authorized reload without merging. It is now the default
application entry point; unavailable/unapplied storage fails closed (including
503) before research, rather than silently reverting to an unsaved paid run.
Artifact import/export remains available. Navigation/unmount detaches observation;
explicit user cancellation calls public `cancelRun` when the run ID is known.

The durable entry point uses one scoped dive for a newly selected entity/category
or available facet, retaining the exact original question and selected ancestry.
Candidate facets wait for identity resolution. Sources/unavailable rows do not
research; completed branches revisit from canonical cache. Duplicate in-flight
selection submissions are suppressed. Artifact import/export remains available.

Hard reload/Continue reattaches telemetry only when saved public execution
correlation matches a bound operation/run/thread. Missing correlation is
explicitly unavailable, never guessed. Reconnect uses that same run/cursor and
never creates/cancels a run or projects provisional output as a graph. Start
afresh/unmount abort observation, not remote execution. Completed reload
observations do not synthesize missing graph results.

### Card actions and custom drafts

Every graph card and the selected-card inspector use the Atlas client's shared
deterministic action catalog. Inspect is local and reports source references or
missing evidence; a sourced summary is downloaded only after an explicit click.
Exports carry source provenance and missing-data labels. These actions never
fetch a source URL or submit an external request. **Custom action** is an explicit
user-authored draft editor, not model drafting: saving writes the
`action_draft_changed` workspace mutation and the UI confirms it only after the
server receipt. A restored draft is displayed only when its card and graph
revision still match. No model run, approval, capability resolution, or external
execution is initiated; external execution remains unavailable pending release
gates. The default durable workspace has no manual artifact snapshot control;
artifact import/export remains separate.

Review repairs: returning to a history entry without a workspace flushes then
clears/detaches the current session, just like Start afresh. Cached selections
offer explicit **Research again**; ordinary revisit still submits nothing.
Reloaded drawers render saved operation prompt/context and the receipt-backed
submitted question (when recorded), using the same redaction/text bounds.

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

It listens at `http://127.0.0.1:4318`; the normal submitter window uses
`http://127.0.0.1:3000`.
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
The Atlas backend is the separate workspace service, whose `/healthz` is
process liveness only and whose `/readyz` checks database/schema/runtime-role
readiness. Neither endpoint proves HaaS provider or live research readiness.
No package is published by this slice.
