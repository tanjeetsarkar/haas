# `@haas/atlas-explorer-client`

Private TypeScript package for Atlas Explorer domain contracts and the
`AtlasSessionManager` facade. Version `0.1.0`; its transitional peer range is
`@haas/sdk` `^0.1.0 || ^1.0.0`.

The package owns its TypeScript, Vitest, and typecheck dependencies and can be
built or tested independently. It has no workspace dependency and imports the
public `@haas/sdk` package for its client and run types. The facade preserves
the behavior of the former SDK implementation; it does not assert server
routes, event-schema compatibility, authentication, or service transport.

## Local verification against the current SDK

From the repository root, build and pack the current public SDK, install the
tarball as this package's local peer, then run the package gates:

```sh
cd haas_sdk_ts
npm run build
npm pack --pack-destination /tmp
cd ../services/atlas-explorer/client-ts
npm ci --legacy-peer-deps
npm install --no-save /tmp/haas-sdk-0.1.0.tgz
npm test
npm run build
npm run typecheck
```

The package is private. These commands are for local contract verification;
they do not publish or release either package.

The Atlas Explorer service remains a health-only skeleton. The additive live
client below uses authenticated public HaaS APIs directly, not Atlas service routes.
Callers supply an already configured `HaaSClient`; no environment credentials,
tenant claims, localStorage session store or production auth gateway are supplied.

## Live exploration client (029)

```ts
import { AtlasExplorationClient } from '@haas/atlas-explorer-client';
import type {
  AtlasResearchContext, AtlasResearchEvent, AtlasSaveDescriptor,
} from '@haas/atlas-explorer-client';

const atlas = new AtlasExplorationClient(authenticatedHaaSClient, {
  timeoutMs: 300_000, pollIntervalMs: 750,
});
const unsubscribe = atlas.observe((event: AtlasResearchEvent) => {
  // Every event includes requestId, explorationId, pathId, parentOccurrenceId,
  // baseRevision, runId, threadId and historyVersion in event.context.
  // 'status': event.status; 'approval': event.approval (actual ApprovalBatch);
  // 'completed': event.snapshot; 'error': event.error.
});
const controller = new AbortController();
const root = await atlas.research('computers', undefined, controller.signal);
const child = await atlas.research('Published pricing', {
  snapshot: selectedSnapshot,
  target: selectedTarget,
  parentOccurrenceId: selectedTargetsParentOccurrenceId,
}, controller.signal);
const saved: AtlasSaveDescriptor = await atlas.save(child); // explicit only
const reopened = await atlas.load(saved.artifactId); // does not launch research
const recent = await atlas.listSaved(saved.runId); // run ID is mandatory
unsubscribe();
```

### Public signatures

- `constructor(client: HaaSClient, options?: AtlasExplorationClientOptions)`
- `research(query: string, context?: AtlasResearchContext, signal?: AbortSignal): Promise<AtlasExplorationSnapshot>`
- `observe(observer: (event: AtlasResearchEvent) => void): () => void`
- `observeExecution(observer: (event: AtlasExecutionEvent) => void): () => void`
- `resumeObservation(context: AtlasExecutionContext, lastEventId?: string, signal?: AbortSignal): Promise<AtlasRunObservationResult>`
- `recoverOperationBinding(workspaceId: string): Promise<void>`
- `getRunStatus(runId: string): Promise<RunResponse>`
- `cancelRun(runId: string): Promise<RunResponse>`
- `getApproval(batchId: string): Promise<ApprovalBatch>`
- `decideApproval(batchId: string, decision: ApprovalDecision): Promise<ApprovalBatch>`
- `save(snapshot: AtlasExplorationSnapshot, runId?: string): Promise<AtlasSaveDescriptor>`
- `load(artifactId: string): Promise<AtlasExplorationSnapshot>`
- `listSaved(runId: string): Promise<AtlasSaveDescriptor[]>`

### SSE observation and progress (041)

Research creates one run and observes it through `HaaSClient.streamEvents`
with `outputDeltas: true` and the operation's `AbortSignal`. There is no
periodic run GET; one authenticated `getRun` reconciles stream EOF/errors
before terminal output is validated. The legacy `pollIntervalMs` option remains
accepted but is deprecated and ignored.

Observers may receive `submitted` with the exact question, application prompt
and selected context; `progress` with bounded/redacted telemetry; and
`disconnected` with the last applied durable numeric event ID. ID-less output
deltas are capped volatile previews marked `provisional`; they never become
graph data or replay cursors. Durable duplicate IDs are ignored, and a sequence
gap is surfaced rather than moving the cursor forward. `AtlasExecutionContext`
includes optional workspace/operation IDs and generation for stale-event
gating. `telemetry` includes the public run status/count/cost, bounded step/tool
IDs and public usage totals where the public endpoints return them; plans and
tool arguments are intentionally omitted.

An authoritative `input_required` reconciliation emits the actual approval
batch and rejects as pending; it is not treated as successful research.
Observation abort, supersede and timeout do not cancel the remote run.
`cancelRun(runId)` is the explicit remote cancellation action and is issued at
most once for the active run. Stream failures never cause a replacement
`createRun`. `resumeObservation(context, lastEventId, signal)` resumes the same
run from the last applied durable ID; it returns authoritative status, cursor,
terminal and disconnected flags, and never creates/cancels a run or projects
research output. Call it again with its returned cursor to resume observation.

For 042, optional `operationPersistence` supplies awaited `prepare`, `outcome`
and `bind` hooks. Payloads match the service's strict models:

- `prepare`: `{operation_id, prompt, selected_context: [<selected context>]}`.
  The service computes `intent_digest` and starts the operation as prepared.
- Before `createRun`, `outcome`: `{operation_id, state:"submitting",
  status:"submitting"}`. A dispatch is never attempted without its receipt.
- After an ambiguous createRun failure, `outcome` records
  `{operation_id, state:"unknown", status:"unknown"}`. `run_id` is omitted if
  unknown; it is permitted only when state is `unknown` and a run ID is known.
- After a run ID is returned, `bind`: `{operation_id, run_id, thread_id?}`.
  The server retains that known run ID and rejects a different ID.
- If bind is unacknowledged, the client retains it locally and records an
  `unknown` outcome with the known `run_id`; a concurrent/already-committed
  binding is recovered by retrying the exact original binding mutation.
- After authoritative terminal reconciliation, `outcome` records
  `{operation_id, state:"terminal", status:"completed"|"failed"|"cancelled"}`.

Each hook supplies the mutation envelope (`schema_version`, fresh
`mutation_id`, current `expected_revision`) and must return the server receipt.
The `bind` adapter must retry the *identical queued mutation* (same mutation
ID and body) after an ambiguous acknowledgement; never enqueue a new binding
mutation. A known run stays blocked until binding receives a valid receipt.
`recoverOperationBinding(workspaceId)` explicitly asks the adapter to retry the
retained binding. Its adapter is keyed by operation ID and must reuse the
original mutation ID and canonical body from the queue after an ambiguous
acknowledgement; the client passes the retained payload unchanged. A
failed/missing `unknown` receipt or unresolved binding
blocks further dispatch; a late createRun result is bound if its ID becomes
known. The client never retries a paid submission. Without these hooks,
operation intent is not claimed durable. Exported
`AtlasWorkspaceMutationKind` and operation state types match the strict 042
protocol. Full graph projection continues to use only validated terminal
output or the existing successful-tool full-history fallback.

`AtlasResearchContext = { snapshot: AtlasExplorationSnapshot; target: AtlasTarget;
parentOccurrenceId: string | null }`. The parent is the selected **target's parent**,
not the target itself. Existing selection is reused and any restored suffix is
trimmed locally for research; saved branches remain intact.

One client instance supports one active research observation. Starting newer
research aborts the predecessor's observation; it does not remotely cancel its
run. Use `AbortSignal` when navigating away even if not launching another run.
UI must additionally gate promise/event application
on its current workspace generation; the client cannot observe local navigation.
Observers are status/approval notifications, not a token stream or durable log.
Keep parent data visible until the returned validated snapshot is accepted.
Submission failures before a run ID is known reject the promise without an
execution-context event. Snapshot requests persist completed run/thread/history IDs.

Research creates/branches threads with real server history versions and
preloads **only** `web-research`. Root and scoped runs request up to 1,000 steps;
root retains its $0.15 and 12-tool-invocation budgets, while scoped runs retain
$0.10 and 8 invocations. Tenant policy may impose stricter limits; policy
rejections are surfaced unchanged and never bypassed by raising policy or
silently resubmitting a cheaper paid run.
No automatic application-level API retry or paid resubmission;
configure the underlying SDK's retry policy explicitly if needed. Default
deadline is five minutes for local observation. Timeout/abort/supersede never
calls remote cancellation; invoke `cancelRun(runId)` explicitly for that action.
Non-stream SDK calls cannot be interrupted, so late results are not applied as
stale completion; with persistence enabled, their known run ID is bound or the
workspace remains blocked pending recovery.

Approval-required runs remain pending until an authorized human decides through
the public API. Events expose the batch ID, digest, expiry, tool destinations
and exact arguments. `decideApproval` forwards an explicit caller decision; it
does not grant authority, self-approve, bypass server policies or replay a run.
401/403/expiry/rejection/dismissal are not interchangeable. Dismissal alone makes
no decision; observing a pending approval does not wait or cancel remote work.

### Graph/result and capability contracts

The exported `AtlasResearchResult`/`atlasResearchResultSchema` contains only
interpretation, entities, sources, claims, relationships and next-row targets.
The model cannot supply navigation, requests, execution IDs or executable actions.
Final JSON (optionally one fenced block) is validated; full successful public
research-tool messages can be fallback results. Event previews and deltas cannot.
Sources require their exact URL and excerpt in successful research-tool outputs
read from version-pinned public thread history and correlated run events. This
proves observation, not source truth or semantic entailment. No missing data,
quotes, prices or citations are synthesized. Existing identities are preserved;
explicit candidate selection can resolve that candidate before facts attach.

Model-owned `targets` are strict, one-level references; they contain no labels,
descriptions, entity categories or action data. The only accepted shapes are:

```json
{"kind":"entity","entity_id":"hospital-1"}
{"kind":"facet","entity_id":"hospital-1","facet_key":"specialists"}
{"kind":"source","source_id":"source-1"}
```

`kind: "entity"` identifies the reference shape; an entity's own kind (for
example `category`, `person` or `organization`) is separate. The schema rejects
mixed references, missing IDs, extra properties and unknown kinds. Valid-shaped
references are also checked for dangling entity, facet and source references
before a snapshot is projected. Neither validation nor projection strips or
repairs model fields.

Malformed final JSON fails closed: it is not projected, saved as a successful
graph or retried automatically. `AtlasResearchValidationError` has a bounded
message with target index, received kind and required/extra fields (or allowed
kinds); other schema issue summaries are capped with an omitted-count notice.
Its `details` retains the structured AJV errors for authorized inspection, but
the summary never embeds the submitted payload. JSON syntax errors remain the
original parser errors. Once run execution context exists, the client's error
event correlates the failure with request/run/thread IDs without adding prompts
or credentials.

`AtlasClaim.details?: AtlasClaimDetails` provides optional typed attributes
(`key`, `label`, `value`, optional `unit`/`uncertainty`), proposed checklist steps
(`id`, `text`, `required`) and an informational draft (`title`, `body`). Values
are strings, finite numbers, booleans, null or string arrays. Attribute keys and
step IDs are unique per claim. The enclosing claim's evidence covers all details.
Comparisons, prices/currency, specialist credentials and availability can use
attributes; unavailable values stay null with uncertainty, never inferred.
Checklist completion, approval status and executed actions are not model fields.
Plain `claim.text` remains human-readable; structured JSON-in-text is unnecessary.

### Immutable save, lineage and limits

Every successful save creates a **new artifact**, with `application/json` bytes
containing the validated snapshot and versioned metadata (`atlas-snapshot.v1`).
The returned descriptor contains `artifactId`, `runId`, `explorationId`, `revision`,
`parentArtifactId`, `schemaVersion` and optional `sha256`. The run defaults to the
last snapshot request, or the explicit `runId` argument. No tenant ID is accepted.
Core authentication, ownership, retention and quotas remain authoritative.

Concurrent saves capture bytes and parent lineage before awaiting upload. They
are immutable siblings, not overwrites or revision conflicts. Each descriptor
records its own parent; no single global “latest” artifact is claimed. Load and
successful save associate an advisory parent artifact with that in-memory object.
Navigation reducers and research preserve it. Arbitrary external object copies,
JSON round-trips and reloads do not; reopen the chosen artifact via `load` when
lineage matters. Advisory lineage is never an authorization claim.

Load checks metadata, size (10 MiB cap), schema, semantic graph references and
metadata/content agreement before returning. Lists are run-scoped and paginated;
there is no global tenant exploration index. Transport, quota, denied, expired,
deleted and malformed-artifact failures reject visibly. No autosave or browser
durable-storage substitute exists.

**Integration gate:** the initial actual-route serialization probe rejected the
SDK JSON multipart `request` Blob (422 with isolated dependencies; parent reports
500 with full tenant-context cleanup). A separate generic core fix is in progress.
Mocked SDK tests are not proof of deployed upload compatibility, real authenticated
round-trip, cross-tenant denial or separate-approver enforcement. Until those
checks pass, do not claim durable save availability or retire legacy functionality.

## Additive exploration v2 APIs (slices 026/B and 027/C)

All v1 exports and behavior remain supported. From the package root:

```ts
import {
  parseExplorationSnapshot, selectExplorationTarget, activateExplorationPath,
  getExplorationBreadcrumbs, previousExplorationPath, reduceExplorationNavigation,
  acceptsExplorationUpdate,
} from '@haas/atlas-explorer-client';
import type { AtlasExplorationSnapshot, AtlasTarget, AtlasNavigationAction } from '@haas/atlas-explorer-client';

const snapshot = parseExplorationSnapshot(untrustedValue); // unknown -> typed, or throws
const selected = selectExplorationTarget(snapshot, target, parentOccurrenceId);
const restored = activateExplorationPath(selected, savedPathId);
```

`parseExplorationSnapshot(value: unknown): AtlasExplorationSnapshot` throws
`Invalid Atlas exploration...` errors with schema paths or semantic reference
details. The authoritative structural contract is `src/explorationSchema.v2.json`
(Draft 7), interpreted by Ajv + ajv-formats, followed by TS semantic checks:
unique IDs/facet keys, reference integrity, candidate identity restrictions,
evidence rules, safe HTTP(S) URL syntax, selected chains, path ancestry/cycles,
active path, and request-to-exploration/path/parent correlation. Revisions,
history versions and sequences are independent nonnegative safe integers.
It returns the original input; do not mutate a validated value unchecked.

Optional `AtlasClaim.facet_key` scopes a claim to an existing facet on its subject.
Omission means entity-wide, **not** applicable to every facet. Validation checks
references, not the truth of claims, equivalence of real identities, or source trust.
URL checks reject malformed syntax, credentials and non-HTTP(S) schemes; they
do not resolve DNS, fetch, block private networks, or constitute complete SSRF
enforcement. Live transport must enforce outbound policy separately.

### Navigation semantics for the fixture preview

- `selectExplorationTarget(snapshot, target: AtlasTarget, parentOccurrenceId: string | null)`
  requires the parent to be selected on the active path; null selects at root.
  An exact saved occurrence prefix + matching target restores the longest saved
  suffix (ties use stored path order). Otherwise it adds a path and unique row.
- `activateExplorationPath(snapshot, pathId: string)` restores that exact branch.
  Both return `AtlasExplorationSnapshot`. No requests, actions or runs are created.
- `getExplorationBreadcrumbs(snapshot)` returns the active selected occurrence chain.
  `previousExplorationPath(snapshot)` selects its prefix, retaining the full saved
  branch. `reduceExplorationNavigation(snapshot, action: AtlasNavigationAction)`
  accepts `select`, `activate`, and `back` actions.
- Branches share ancestor occurrence prefixes; `parent_path_id` records derivation.
  A branch's selected chain is complete, not only its new suffix. Inherited rows
  must be selected at the same position in the immediate parent path.
  New occurrences have unique IDs even when targeting an already-known entity.
  IDs use `crypto.randomUUID` when available, with collision checks/fallback IDs.
- Navigation is immutable/local, increments snapshot revision only on change,
  and preserves all sibling paths. It does not merge entities or relationships.
  UI owns synthetic fixtures and derives alternatives from facets/relationships;
  no new column-alternatives field or public fictional/live-data fixture export.
- `acceptsExplorationUpdate(snapshot, incomingRequest & { snapshot_revision })`
  is a pre-application cursor guard, not a stream implementation. Request array
  order is issuance order; only the newest request for a path/parent may update
  while loading/partial, with exact exploration/request/run/thread/history/revision
  and a strictly newer safe sequence. Terminal, superseded and stale updates fail.
  It does not apply or persist updates.

### Verification and status

Baseline: client build/typecheck pass; 13 tests pass. Isolated service health
tests: 3 pass. Root-level pytest instead imports core's `app` and fails those
three tests; run from the service directory. Web's five existing TS errors,
global boundaries 175 errors/9 warnings and Atlas boundaries 154/1 are unchanged.

After B+C: client build/typecheck pass; 113 tests pass (13 preserved + 100 new).
Python: 19 pass (3 health + 16 shared-schema cases). Ruff and Pyright on the
new Python test pass. Doctor: 8 plugins, 0 errors/0 warnings. `git diff --check`
passes. Runtime-only `npm audit --omit=dev`: 0 vulnerabilities; full audit reports
5 existing dev-tool advisories (2 moderate, 1 high, 2 critical), not changed here.

```sh
npm --prefix services/atlas-explorer/client-ts run build
npm --prefix services/atlas-explorer/client-ts run typecheck
npm --prefix services/atlas-explorer/client-ts test
.venv/bin/python -m pip install -r services/atlas-explorer/tests/requirements-contract.txt
cd services/atlas-explorer
../../.venv/bin/python -m pytest tests -q
../../.venv/bin/ruff check tests/test_exploration_schema.py
../../.venv/bin/pyright tests/test_exploration_schema.py
```

Python uses the same JSON Schema and shared valid/invalid JSON fixtures. It
checks structural compatibility only; TS tests cover the semantic validator.
Initially the environment lacked jsonschema's optional date-time/URI checkers;
the test-only requirements above enable them and assertions prevent silent skips.

025: independent reviewer APPROVE reported by parent; ready for plan/index done.
026/B and 027/C: implemented and locally verified, pending independent review.
Plan/index edits require the documentation owner (outside this engineer's allowed
paths); no session plan edits or commits made. Budget override (B, once):
schema, semantic checks and cross-language regression tests exceed ~400 lines.
C remains within its soft budget. Backend remains health-only; plan 008's
operational blockers remain unresolved. No auth, persistence, live transport,
web code, or core changes are included.
