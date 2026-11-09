# 001 — Extract Atlas and stabilize the public SDKs

- Status: in-progress
- Type: epic
- Owner agent: haas-lead (coordination; one engineer per slice below)
- Epic: none
- ADR: first slice needs none; future core/client slices require an applicable ADR

## Goal
Move Atlas domain storage, behavior, clients and any Atlas UI into an independently
deployable service without losing existing functionality. Stabilize Python and
TypeScript clients against the actual public REST/SSE contracts, not against the
aspirational SDK examples in design docs. Keep main releasable after every slice.

[002](002-atlas-service-skeleton.md) and [005](005-run-created-at-response.md)
are complete. 005's source reviewer approved with no findings, and the required
DB-backed persistence, stored-response, replay and legacy-enrichment selectors
passed against a newly provisioned, disposable local PostgreSQL 16 database after
the user authorized setup and project migrations. No existing data was changed;
the temporary container was stopped and removed after verification.
The TypeScript characterization [004](004-typescript-sdk-contracts.md) is done,
unblocked by 005. Its focused and full Vitest selections passed 10 tests each;
build and supplemental strict test type-check passed; lint retained the
pre-slice missing-ESLint-config failure. Reviewer approved in round 2/2, final
verification was clean, and boundary counts were unchanged. This does not
override per-slice path restrictions and outstanding decisions. On 2026-11-09
the user confirmed the dirty Atlas client change was removed and authorized
updates to `haas_sdk_ts/src/lib/atlasClient.ts` under plan 010.
Slice [006](006-python-fakehaas-scenarios.md) is done. All eight failures
recorded in the baseline for `tests/sdk/test_sdk_testing.py` are resolved. Its
focused suite is 13 passed/1 warning (baseline 4 passed/8 failed/1 warning);
the full SDK suite is 104 passed/5 warnings (baseline 95 passed/8 failed/5
warnings). Ruff passed, Pyright reported 0 diagnostics, and boundary counts
remained unchanged at Atlas 0/0, atlas-explorer 134/1, overall 155/9. The final
verifier was GREEN and final reviewer APPROVE with no findings. The overall
extraction and SDK stabilization remain in progress. The
[007 service invocation audit](007-atlas-service-invocation-audit.md) is done
and final reviewer-approved. Its bounded audit concludes that no existing public
path proves every service identity, credential, egress and approval requirement.
Reported boundary counts remain Atlas 0/0, Atlas Explorer 134/1, overall 155/9.
Service tests were unavailable because `services/atlas-explorer/.venv` is
missing, DB-backed identity tests were not run, and the phase-3 workflow suite
was collected only (18 tests); these are limitations, not passes. The reported
targeted results and documentation diff-check are recorded in 007.

The proposed [008 generic plugin-service invocation epic](008-generic-plugin-service-invocation.md)
turns this gap into bounded core slices and makes its unresolved security
decisions explicit. ADR-17 remains proposed. Neither reviewer approval of 007
nor preparation of 008 authorizes implementation. Do not start a consuming
Atlas service slice until the capability is separately approved, implemented
and verified. In particular, 007 did not prove sandbox egress enforcement:
consumers executed through the sandbox runner must remain disabled until 008's
dedicated sandbox transport/enforcement slice
passes actual transport deny/allow/failure tests. The runner is not thereby
claimed to be a general OS/network security boundary.
Approval-dependent remote effects remain deferred unless exact remote approval
binding is later accepted and proven.
Other entries are slice outlines, not implementation authorization; write a
full plan for each when its dependencies and decisions are resolved.

## Baseline and inspected evidence

Fresh boundary baseline (2026-11-08):

```text
check_boundaries.py --plugin atlas:
  0 errors; 0 warnings
check_boundaries.py --plugin atlas-explorer:
  134 core-boundary errors; 1 warning
overall check_boundaries.py:
  155 errors; 9 warnings
At initial capture: SDK builds, lint, types, pytest and compose not run.
```

Current known reported SDK baseline after 006: Python SDK 104 passed/5 warnings,
Fake focused 13 passed/1 warning and zero failures. TypeScript characterization
10 passed and build passed; lint unavailable because ESLint config is missing.
The earlier 95 passed/8 failed Python and no-test-file TS baselines are historical,
not current failures. These reports do not replace each slice's fresh baseline;
unrelated preexisting core tests remain out of scope.

These are pre-implementation counts. The health-only skeleton removes no
coupling, so no decrease is expected or claimed for slice 002. A zero filtered
result is not evidence of no coupling: direct inspection found the following.

| Kind | Evidence | Disposition |
|---|---|---|
| Core tables/migration | `migrations/versions/20261023_01_add_atlas_tables.py`: `atlas_entity_cache`, `atlas_layer_nodes`; forced RLS; UUID tenant IDs; cache FK to tenants, node FKs to threads/runs and parent nodes, cascading deletion | Service-owned schema with independently managed migrations; historical core migration remains unchanged |
| Raw core access | `plugins/atlas-explorer/tools/atlas_tools.py` imports `RunContext` and outbound validator from `app.core.context`; lookup/upsert use `ctx.store.connection()` | Replace with public SDK imports and authenticated service calls in a plugin-only slice |
| Cache behavior | Tenant/key/dimension lookup excludes expired rows; upsert uses domain TTLs and SHA256; missing store returns mock success on writes; exceptions become miss/failed results | Characterize all outcomes before changing them; do not silently promote mock success into durable-write success |
| Layer behavior | Same file validates `atlas-layer.v1`, rejects non-HTTP(S) provenance, hashes normalized envelope and returns it | Service-owned domain validation; inspected handler neither inserts layer nodes nor emits a custom event |
| Inquiry behavior | Same file: `requires_approval=True`, runtime SSRF validation, 15-second HTTP timeout, redirects disabled, caller idempotency key | Preserve approval binding and safe egress; never offer a service route that bypasses approval |
| Core compose | `docker-compose.yml` explicitly mentions Atlas cache DSN/tool DB access | Remove obsolete domain reference after cutover; do not remove generic worker DB access without checking other consumers |
| TS SDK | `haas_sdk_ts/src/lib/atlasClient.ts`, `src/types.ts`, `src/index.ts`: Atlas session manager, types and exports; root/drill-down run submission and SSE wrapper | Move to service client package, retain existing API until approved deprecation/removal |
| Python SDK | `haas_sdk/__init__.py`: `Haas`, `AsyncHaas`, handles, typed models/errors, `Secret`, webhook surface; no Atlas matches in inspected SDK search | Stabilize generic contracts, do not invent a Python Atlas API in core |
| UI | Broad search returned Atlas matches under `web/` | Exact component/route/dependency inventory deferred; no UI relocation/removal authorized yet |
| Tests | `tests/plugins/test_atlas_explorer.py`; `tests/sdk/test_sdk_{client,models,contract,webhooks,preview,testing}.py` | Preserve existing tests; add service-owned tests and compatibility coverage in the owning slices |

At slice 002, `haas_sdk_ts/src/lib/atlasClient.ts` had a user-owned uncommitted
limits change and was protected from inspection or edits. The user confirmed
that change was removed and explicitly authorized updates on 2026-11-09; this
supersedes the old file restriction only for authorized plan 010 work. Do not
change its existing limits unless a separately scoped requirement calls for it.

### Public SDK contracts and stabilization gaps

- Python currently exports `Haas`/`AsyncHaas`, not the design document's proposed
  distributable `haas` package. Existing clients expose resource operations,
  streams, history versions and resume/approval operations. Stabilization must
  preserve sync/async signatures, import paths, return models and exception types.
- TypeScript exports `HaaSClient`/`createClientFromEnv`, not a new `Haas` facade.
  Inspected constructor requires `ClientOptions`; defaults include tenant selector,
  timeout, retries and reconnect limits. It uses fetch-style transport and
  `eventsource-parser`; preserve supported configuration and cancellation APIs.
  Package has version `0.1.0` and a `vitest run` test script.
- TS client names terminal events `run_completed`, `run_error`, `run_cancelled`,
  `stream_closed`, with approval/pause events treated as settled. Design examples
  use dotted names: do not rename based on those examples. Compare with actual
  `docs/contracts/`, AsyncAPI and producer tests first.
- Atlas wrapper synthesizes `sequence: 0`, casts event data, reconnects outside
  the generic client and stops cancellation via a boolean. Characterize cursor,
  dedupe, terminal/pause, abort and error behavior before replacing this wrapper.
- Existing ADR-16 (`docs/adr/0016-api-contract-toolchain.md`) partially accepts
  contract tooling; SDK generator choice is open. Do not make generated-code
  replacement, new packages or a new facade prerequisites for this extraction.

## Boundary classification and target

| Change | Owner | Boundary test |
|---|---|---|
| Atlas TTL, schemas, cache, validation, inquiry logic, session orchestration, UI | plugin-engineer | Would not exist without Atlas |
| REST/SSE transport, errors, pagination, auth, replay, capability negotiation, public SDK compatibility | client-engineer / core-engineer in separate slices | Exist without Atlas; plugins can use these public contracts (e.g. web-research and incident-triage) |
| Authenticated plugin-service invocation, only if existing surface cannot supply it | core-engineer | Generic: plugins can call services with trusted tenant scope; Atlas and web-research can consume it |
| Historical Atlas table retirement | core-engineer | Removal of legacy core debt, not a new domain facility; explicit approval required |

Recommended target: `plugins/atlas-explorer/` retains manifest, skills, stable
tool names and thin adapters; `services/atlas-explorer/` owns dependencies, compose,
database, migrations, HTTP API, clients and UI. Service never shares the core DB
or imports `app.*`. Core compose never declares the service.

The completed 007 audit establishes that stock HTTP/MCP payloads do not carry
authenticated tenant/run/plugin/approval scope; public `ToolContext` has no
service-call/SSRF validation facility. Internal trusted envelopes and
tool-audience credential grants do not by themselves authenticate a remote
service request, and the actual egress paths are not proven end to end. Plan 008
proposes the bounded generic core follow-through under ADR-17; user approval and
resolution of its explicit security decisions are required before implementation
or any consuming plugin slice. Do not add core plugin storage merely to preserve
Atlas's relational schema.

## Scope and ordered slices

Every row is one-owner work, ≤400 changed non-generated lines and ≤1 migration.
Split a row again if its measured diff would exceed this limit. No row authorizes
work in another owner's paths. SDK behavior changes and Atlas relocation are separate.

| Order | Slice outline | Owner | Depends on / measurable exit |
|---|---|---|---|
| 1 | **002 service skeleton** | plugin-engineer | No prerequisite core capability; health-only standalone service; full plan linked above |
| 3 | [Python generic SDK characterization](003-python-sdk-contracts.md) — **done** | client-engineer | Focused selection 80 passed; full SDK has 8 unchanged baseline failures; Ruff/Pyright pass; boundaries unchanged |
| 4 | [005 persisted run creation time](005-run-created-at-response.md) — **done** | core-engineer | Reviewer approved with no findings; required PostgreSQL selectors passed (2 passed) against the user-authorized disposable, fully migrated local PostgreSQL 16 database; no existing data changed |
| 5 | [TypeScript generic SDK characterization](004-typescript-sdk-contracts.md) — **done** | client-engineer | Unblocked by completed 005; producer-backed creation includes numeric persisted `created_at`; 10 focused/full Vitest tests pass; build and supplemental strict type-check pass; lint same missing-config baseline; protected user-owned Atlas implementation untouched |
| 5a | [Python FakeHaas scenario semantics](006-python-fakehaas-scenarios.md) — **done** | client-engineer | Focused 13 passed/1 warning vs baseline 4 passed/8 failed/1 warning; full SDK 104 passed/5 warnings vs 95 passed/8 failed/5 warnings; Ruff passed; Pyright 0 diagnostics; boundaries unchanged (Atlas 0/0, atlas-explorer 134/1, overall 155/9); final verifier GREEN and reviewer APPROVE with no findings |
| 5b | [007 service invocation trust audit](007-atlas-service-invocation-audit.md) — **done** | architect / verifier evidence | Final reviewer approved; public-path proof gap confirmed; service tests unavailable, DB-backed identity tests not run, phase-3 workflow 18 collected only; no runtime changes |
| 5c | [008 generic plugin-service invocation](008-generic-plugin-service-invocation.md) — **proposed** | core-engineer | User must approve implementation and resolve security gates; each ordered core slice ≤400 changed lines/≤1 migration; sandbox-runner consumers additionally gated on the dedicated sandbox transport/enforcement slice and actual deny/allow/failure tests |
| 6a | Service cache schema | plugin-engineer | 008 capability approved, implemented and verified; consumers executed through the sandbox runner only after the sandbox slice passes; one service migration, forced tenant RLS and A/B read/write rejection tests; no core FKs |
| 6b | Service cache behavior | plugin-engineer | TTL boundary tests with fixed clock; hash fixtures, lookup miss/expiry and upsert conflict/error contracts; no core DB access |
| 6c | Pack cache cutover | plugin-engineer | Trusted per-tenant credentials; stable lookup/upsert names and result JSON; adapter tests; legacy import replaced with public SDK; old tables retained |
| 7a | Layer validation extraction | plugin-engineer | Golden valid/invalid/provenance/digest fixtures match old tool; pack tool name/result retained; no invented layer persistence/event |
| 7b | Inquiry extraction | plugin-engineer | Proven non-bypassable approval binding or retain approved outbound dispatch in thin pack; reject unapproved/mutated/replayed request; SSRF/private host/redirect tests |
| 8a | Generic Python transport fixes | client-engineer | Slice 3 findings; additive only; deterministic safe-retry, auth, errors and sync/async parity tests; split by finding |
| 8b | Generic TS transport fixes | client-engineer | Slice 4 findings; additive only; replay/dedupe/abort/settled-event tests; split by finding; needed fixes precede service clients |
| 9a | Atlas TS service client | plugin-engineer | Generic SDK contract sufficient; new service package depends on public SDK only; old public exports continue to function unchanged |
| 9b | Atlas UI relocation | plugin-engineer | Explicit inventory and feature-compatibility journey fixtures; standalone UI using public clients; core UI remains until approved cutover |
| 10a | Remove obsolete core configuration/compose coupling | core-engineer | Applicable cleanup ADR; deny-pack core boot/tests pass; no unnecessary removal of shared DB facilities |
| 10b | Retire TS Atlas exports/types and core UI separately | client-engineer | **Blocked by breaking-change/feature-removal approval**; deprecation period and successor package proven; per-surface slices |
| 11a | Export/import and reconcile retained data | plugin-engineer | Approved data disposition; operator-controlled export, UUID mapping, counts/digests, restore drill and retention/deletion policy documented |
| 11b, optional | Retire core Atlas tables | core-engineer | **Explicit table-drop approval + export/migration note + reviewed new reversible migration**; ≤1 migration; schema downgrade alone does not restore data |
| 12 | Final docs and release qualification | owners in separate slices | SDK install/build smoke tests, API matrix, service quickstart, release/deprecation notes and evidence links |

Generic SDK characterization may run independently after skeleton; any generic
SDK fix needed by a service client must land before that client. Core capabilities
come before consumers, behavior moves before legacy cleanup, and cleanup never
requires destructive table retirement to make the service usable.
007 is complete. 008 is a proposed epic, not implementation authorization; its
slices and security decisions require separate user approval. Rows 6a onward
remain outlines blocked on a verified safe invocation capability; consumers
executed through the sandbox runner also require verified transport integration.
No runtime
edits, ADR-17 implementation, SDK/API breakage, UI removal, data movement or
table drops are authorized here. Never inspect or touch the user-owned
`haas_sdk_ts/src/lib/atlasClient.ts`.

## Acceptance criteria (epic)

- [ ] All four persisted tool names remain unchanged; golden argument/result
  fixtures compare old and new success and failure cases.
- [ ] Service runtime imports no `app.*`, has no core DB credentials and passes
  tenant A/B read, write and cross-tenant run-reference rejection tests.
- [ ] Core boots from its compose alone with `PLUGINS_DENY=atlas-explorer`;
  service boots from its own compose; no service declaration added to core compose.
- [ ] Both SDKs pass a documented wire-contract matrix: identity/configuration,
  idempotency and retries, typed errors, pagination/history/branch versions,
  approval batch digest, capabilities and SSE replay/dedupe/abort/terminal states.
  Unsupported resources are enumerated rather than falsely claiming parity.
- [ ] Python sync/async share fixtures; TS public-import/type/build tests pass;
  package installation smoke tests run outside the source checkout.
- [ ] Atlas clients/UI live outside core; public legacy exports/entry points
  persist until the separately approved compatibility policy permits retirement.
- [ ] Correct-ID filtered boundary errors/warnings end at 0; overall counts never
  increase and decrease when reported coupling is removed. Also maintain a manual
  path inventory: checker-blind references count as debt, not success.
- [ ] Historical migrations are untouched. Table retention is acceptable pending
  decision; no drop is performed without explicit approval and recovery evidence.
- [ ] No new verification failures versus per-slice recorded baseline; all new
  tests pass and reviewer records APPROVE before integration.

## Verification and baseline protocol

Before each implementation slice: record `git status --short`, both
`python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas`
and `--plugin atlas-explorer`, and the overall checker with full output retained.
Capture dirty-file diff separately without modifying/staging it.

Rungs: 1 ruff on touched Python; 2 pyright on affected public/core paths;
3 targeted tests; 5 boundary always; 7 API snapshot when HTTP core contracts change;
10 full pytest before core integration; 11 compose when deployment is touched.
SDK characterization starts with `.venv/bin/pytest tests/sdk -q`,
`npm --prefix haas_sdk_ts run test` and `npm --prefix haas_sdk_ts run build`;
verify package scripts exist before choosing extra commands. Service selections
are specified in slice 002 and future full plans. Infrastructure-unavailable tests
must be marked not run, not passed. Pre-existing unrelated 155/9 debt is out of scope.

## Decisions needed

1. **Data disposition?** A: retain core tables read-only after cutover and export/
   migrate preserved cache/node data; B: discard cache after export but retain
   historical nodes; C: export then explicitly approve both table drops.
   **Recommend A initially.** Approve the mapping, retention window and operator
   export destination before copying data;    a general epic approval is not drop approval. On 2026-11-09 the user
   explicitly approved dropping both legacy Atlas tables after export. Export
   destination, tenant mapping, retention/recovery evidence and safe service
   cutover remain prerequisites; do not drop tables while the core plugin still
   uses them.
2. **SDK compatibility/versioning?** A: additive releases, existing imports and
   signatures retained, deprecation plus separately approved major retirement;
   B: explicitly approved coordinated breaking release. The user selected B for
   Atlas TypeScript exports and chose `@haas/sdk` 1.0.0 plus
   `@haas/atlas-explorer-client` 0.1.0 (final peer range `^1.0.0`), with no
   publication authorization. This does not approve breaking generic Python
   SDK APIs.
3. **Service tenant authentication?** A: existing per-tenant scoped credential
   available through allowed secrets, service maps credential to tenant;
   B: ADR-backed generic signed service invocation if A cannot bind trusted scope.
   The user deferred service authentication; operators/providers and trust
   details are unknown. Keep Atlas business routes, service calls and cutover
   blocked. Never trust body tenant IDs, `X-Tenant-ID` alone or a shared bearer
   secret plus caller-selected tenant.
4. **Atlas service placement/storage?** A: `services/atlas-explorer`, separate
   PostgreSQL database and independent compose; B: different approved service ID
   or managed DB deployment. **Recommend A**, keeping pack/tool IDs stable and
   no foreign keys or shared transactions into the core DB.
5. **Atlas TypeScript source:** user confirmed the dirty change was removed and
   authorized edits. Plan 010 owns that source; the existing limits are not
   changed by the initial type-package slice.

## Unknowns and stop conditions

Exploration stopped at ~25 reads/searches. Exact web routes, service auth/endpoint
capabilities, core tenant-ID conversion, production data volume/use of layer nodes,
TS test locations, package release packaging, event schema fixtures and existing
test/build failures remain unverified. Audit these in their owning slice; do not
read more as a substitute for selecting a bounded next increment.

Inquiry headers currently can override the prepared idempotency header; treat
that as a characterization/security finding, not approved behavior to perpetuate.
Service-side outbound egress must enforce SSRF too; core validation of the service
address does not validate an inquiry destination embedded in the request.

## Risks and rollback

- Cache UUID/text tenant mismatch and removed FKs → explicit tenant mapping,
  service RLS, public run authorization and a service-owned deletion/retention policy.
- Switching data stores → migrate under operator control; avoid unplanned dual writes;
  describe rollback reconciliation for writes accepted after cutover.
- SSE/API drift → fixtures from actual producer contracts; no unchecked new casts.
- Approval bypass → retain existing approved worker path until trusted service
  authorization is proven; halt extraction rather than weakening security.
- Rollback each code slice by reverting only its owned change. Keep old deployment
  and tables through cutover; never reset user changes, edit historical migrations
  or use compose teardown with `-v`. No commits/push requested.

## Log
- 2026-11-08 user: approved slice 002 with recommended defaults; epic remains in progress.
- 2026-11-08 architect: recorded fresh pre-implementation boundary baseline; no runtime changes.
- 2026-11-08 user: selected Option 1 for the 004 blocker, authorizing the additive persisted `created_at` producer field; this authorized the implementation scope, not completion of DB-backed acceptance.
- 2026-11-08 user: authorized the overall epic and the isolated disposable PostgreSQL 16 container plus project migrations needed for local qualification; no existing data was changed.
- 2026-11-08 integration update: 005 is done. Both required PostgreSQL selectors passed (2 passed), covering persisted timestamp equality, stored admission response equality, same-key replay and legacy enrichment; temporary container stopped/removed. Ruff passed, Pyright retained only the same pre-existing `store.py:1104` error, boundaries unchanged at 155/9, and reviewer approved with no findings. Slice 004 is approved/in-progress and unblocked; protected user-owned Atlas implementation remains off limits.
- 2026-11-08 integration update: slice 004 is done. Focused and full Vitest pass 10 tests each; package build and supplemental strict test type-check pass; lint exits 2 for the same missing ESLint config as baseline; `git diff --check` passes. Boundary counts remain overall 155/9, Atlas-filtered 0/0, atlas-explorer-filtered 134/1. Protected dirty `haas_sdk_ts/src/lib/atlasClient.ts` hash matches `cb14fd38219926cb569c149fe00ec5f0ded0473520689ee80dbf6b20f68d4f81` and was never touched. Only one test file and plan evidence changed (311 lines). Verifier's first pass found the SSE fixture used the whole envelope; review round 1 requested serializing `event.data`; correction was test-only. Final verification/review clean; reviewer **APPROVE**, round 2/2. One-time budget override: prior blocked initial attempt plus mandatory verifier/reviewer/integration gates; total slice task delegations 11/6. Candidate follow-ups, in existing order, are TypeScript transport fixes and Python SDK findings, then Atlas trust audit/extraction; each needs its own full plan. This is not authorization for runtime work.
- 2026-11-08 lead/architect continuation: proposed slice 006 after inspecting the recorded failing tests, FakeHaas implementation, run submission client, pytest fixture, and SDK testing spec. Source indicates prompt-selection and fault-test setup mismatches, but neither has been freshly reproduced. Plan is proposed only; no SDK implementation was edited.
- 2026-11-08 user: selected FIFO consumption of pre-scripted scenarios for slice 006 and approved it to proceed in-progress. Fresh baseline capture remains required; no fresh baseline is claimed.
- 2026-11-08 integration: slice 006 is done. Focused SDK test 13 passed/1 warning vs baseline 4 passed/8 failed/1 warning; full SDK 104 passed/5 warnings vs 95 passed/8 failed/5 warnings; Ruff passed, Pyright 0 diagnostics, boundaries Atlas 0/0, atlas-explorer 134/1, overall 155/9 unchanged, and diff-check passed. Protected TypeScript Atlas diff SHA `cb14fd38219926cb569c149fe00ec5f0ded0473520689ee80dbf6b20f68d4f81` was unchanged and not inspected. Review round 1 APPROVE found only that fail-next consumption was unproven; test added `assert not fake.failures`; final verifier GREEN and final reviewer APPROVE with no findings. Final slice change 57 insertions/14 deletions in the three allowed implementation/test/spec files plus the existing untracked plan, within budget. Budget override: 7 specialist delegations exceeded 6 by one extra independent verification after the reviewer-requested assertion, preserving required review gate integrity. All baseline failures in `test_sdk_testing.py` are resolved; extraction and SDK stabilization remain in progress. Recommend a separate Atlas service trust/behavior plan next; this is not authorization to implement it.
- 2026-11-08 user: clarified that continuation of the overall Atlas extraction authorizes bounded read-only prerequisite audit 007 only. 006 FIFO slice is done and latest reported overall boundary baseline is 155/9. Generic core implementation remains unauthorized absent a separately approved full plan; no runtime edits or audit completion are claimed.
