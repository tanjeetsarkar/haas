# Atlas exploration information architecture

Status: A–D implemented and independently approved; full live replacement approved,
implementation in progress under plans 029–031. Live parity/retirement not yet verified.

Atlas will project a source-backed knowledge graph into Miller columns. Columns
show the active traversal; they are not the graph or HaaS execution history.

## Search and navigation

- Broad concepts open useful categories; named subjects open relevant facets.
- Resolve ambiguous identities before attaching claims. Never merge by name alone.
- Specific questions may open the requested facet directly with parent context.
- Distinguish entity/category rows, facets, sources, and actions visually.
- Each column has a meaningful title, parent context, selected row, and status.
- Selecting a row opens its child column, never an external action.
- Retain sibling paths; switching back restores known results without new research.
- Keep parent content during loading/error/retry. Ignore updates for stale requests.
- Use a detail inspector for claims, conflicting evidence, sources, and actions.
- Desktop uses a scrollable column rail; mobile shows one column with back navigation.
- Support keyboard traversal, announced status, visible selection, and focus.

## Identity and connections

Entities have stable tenant-scoped IDs; occurrences identify their positions in
particular paths. Reaching Person B through two branches means one entity and two
occurrences. Exploration, path, request, run, and thread IDs are separate concepts.
Committed thread history versions come from orchestration, not column depth.

Relationships have direction, labels, verification state, and evidence. A person
linked to an organization that links to another person does not establish a
direct person-to-person relationship. Show connection badges and accessible
relationship threads; an expandable map supplements rather than replaces columns.
Give every drawn connection a textual equivalent and an evidence inspection route.

## Trust and actions

Facts and relationships require evidence or an explicit unsupported label.
Conflicting claims retain both sides. A source reference is not proof of truth.
Suggested facets are not researched facts. Person research is limited to appropriate,
attributable public biography, professional history, and disclosed financial facts;
exclude private details, sensitive profiling, and inferred wealth.

Actions are scoped to a target. External actions require explicit intent,
server authorization, and approval; generated descriptors cannot grant permission.

## Contract and implementation boundary

`atlas-exploration.v2` types coexist with unchanged `atlas-layer.v1`. The
[synthetic fixtures](../../services/atlas-explorer/client-ts/tests/fixtures/explorations.ts)
are colocated with tests to reuse the existing TypeScript project without changing
compiler roots. Reserved `example.test` URLs are not fetched or real citations.

Type assertions and fixtures are **not runtime validation**. Completed B/C provide
runtime validation and local navigation guards, not live transport or persistence.
Live results must still pass authoritative runtime/reference validation before
projection, including nonempty IDs, dates, unresolved identity, graph cycles and
referential integrity. URL syntax validation is not SSRF enforcement or proof of
source truth. Source provenance must come from actual successful research tool output.

## Approved live route and execution identity

The user's 2026-11-09 approval supersedes the earlier local-adapter decline:
Atlas uses existing authenticated HaaS **public** thread/run APIs for root research
and selected-branch research, and public artifact APIs for explicit snapshot saves.
See [029](../plans/029-atlas-live-client-snapshots.md). This is not trusted
plugin-service invocation or approval of a standalone production auth gateway.
The independently deployable production Atlas service stays separate.

Model output owns researched entities, sources, claims, relationships and suggested
targets, not execution IDs, permissions or committed history. The client validates
that result and projects it into domain navigation. Branch context includes selected
ancestry, not unrelated siblings. Thread/branch history versions come from actual
public orchestration responses, never column depth or exploration revision.
Correlate run/thread/request/events; ignore stale completions and retain parent
content through errors. Cancel invokes remote run cancellation, not just stream close.
Retry/resume must reflect actual server state and explicit intent, within existing
`max_cost` budgets.

## Persistence and tenant boundary

Save is explicit: serialize a validated snapshot and create a **new immutable core
artifact on each save**. There is no overwrite needing revision-conflict handling,
new DB/session table or local fake persistence. Domain revision remains distinct
from both artifact identity and committed execution history version.
Load verifies public artifact metadata/content and validates before activation;
saved snapshots restore domain state, not expired streams or automatically rerun work.
Recent saves may use existing run-scoped artifact listing; no global session index
is implied. Core owns artifact retention, quota, tenant access and expiry.
Failures must not display a successful save.

Verify isolation through real authenticated public run/thread/artifact paths using
principals with actual tenant membership. Header selectors or invented tenant claims
are not cross-tenant proof. Authentication does not grant approval authorization:
separate-authorizer, role, digest and self-approval restrictions remain authoritative.

## Workspace, parity and cutover

The default replacement is a generic live graph workspace, not a fixture occurrence
renderer. Sample mode is deliberate, labeled and isolated; backend failure never
falls back silently to fixtures. Preserve supported comparison, pricing, specialist,
checklist, draft and approval interactions, with source/uncertainty inspection.
Live relationship threads and the expandable map require directed evidence-backed
edges and textual equivalents; the completed fixture preview is not that proof.

[031](../plans/031-atlas-live-workspace-parity-retirement.md) requires a mapped
capability inventory, automated tests and browser/accessibility verification before
deleting `services/atlas-explorer/web/src/components/atlas/` and proven-unused
private client paths. Public legacy v1 exports remain unless separately proven unused
and approved for breaking removal. No unnecessary compatibility facade copies.
Core marketplace, generic SDKs and DB tables stay unchanged.

## Authentication and deployment distinction

[030](../plans/030-atlas-loopback-proxy.md) owns the loopback-only Vite development
proxy: server reads `.env.haas`, fixed upstream `http://localhost:8000`, explicit
Host/Origin and remote-CSRF guards, no browser secrets, disabled in build/preview.
Never document credential values. Core authorization, including approval restrictions,
must not be bypassed by authentication middleware scope.

A production static build needs actual operator-configured authenticated same-origin
gateway routing. That configuration/deployment is outside these plans; local API
success does not establish production readiness. Without an authenticated backend,
live mode fails closed. Plan 008 remains separately gated for trusted service
invocation and is not a prerequisite for this approved existing-public-API route.
