# 035 — Bounded external-action initiation

- Status: in-progress — exact-intent contract, separate approver support and
  additive reversible authority migration user-approved; enforcement incomplete
- Type: epic; first slice fully specified below
- Owner agent: core-engineer (kernel), client-engineer (SDK), plugin-engineer (consumers)
- ADR: [ADR-0021](../adr/0021-run-tool-restrictions-and-exact-intents.md)

## Goal

Plugins can submit a tenant-authorized run restricted to exact tools and, when
initiating a reviewed external action, one immutable exact-argument invocation.
Deliver the entire path in bounded slices: public contract, persisted authority,
all dispatch paths, existing approval workflow, SDKs, then extension consumers.
Do not enable the initiator after only implementing schema visibility.

## Superseding operator decisions

The operator supplied a separate local-development approver credential. Read-only
authentication checks confirmed a same-tenant approver role and a different
principal from the submitter; no credential or principal value is recorded here.
The isolated approver proxy and page are implemented, with independent review
pending. Credential availability is no longer the blocker. Exact-intent core
enforcement, migration review, SDK wiring and live approval outcomes remain
unverified; no external action is authorized by these read-only checks.

## Boundary classification

| Change | Owner | Would exist without Atlas? |
|---|---|---|
| Run restrictions, exact-intent workflow, envelopes, grants and approval binding | Core | Yes; an incident-notification plugin also needs them |
| Generic Python/TS create-request fields | Client | Yes; any plugin can submit attenuated runs |
| Inquiry review, form-to-arguments mapping and action selection | Atlas service | No |
| Incident recipient/content mapping | Incident plugin/service | Yes without Atlas, but still extension-owned |
| Separate server-side approver connection and approver-session gate | Consuming extension | Its UI deployment concern; reuse generic core authorization |

No plugin names, recipient rules, domain schemas or provider-specific credentials
are added to core. No extraction is proposed; existing extension ownership remains.

## Evidence and unknowns

### 035e3b worker registered-target recheck / redelivery proof

Bounded files: new `app/core/worker_target.py`, worker restriction checks,
worker restriction tests, new actual-PG `tests/test_intent_worker_execution.py`.
Recompute pinned native handler/module/qualified name, plugin/tool/schema,
execution/queue/context injection or fixed HTTP/MCP URL/protocol descriptor;
recheck exact credential set at pre-grant/final-entry, including after awaited
RPC. Existing SSRF, sandbox, signature, strict arguments and live-policy checks
remain conjunctive. No endpoint ALLOW or public/lease hold removal.
Actual DB held intent → distinct persisted approval → internal release →
original signed envelope → actual `_execute_envelope` → synthetic handler
counter, with mocked RPC authorization only. Duplicate effect returns cached
result without second handler; lost cached result returns unknown and terminal
failure with no model continuation or resend. Redis is synthetic for bounded
worker effect tests; this is not global exactly-once or unbounded dedupe proof.
Focused **80 passed**; combined actual-PG **146 passed**, worker/public holds
**277 passed**; touched ruff/pyright clean. Temporary-key real TLS
handshake remains required; no live keys or shared DB read/applied.

### 035e3a internal release/terminal — independently APPROVED

Atomic internal release reuses the persisted approved signed envelope, rechecks
live eligibility and deduplicates one outbox effect. Rollback preserves held
state. One tool outcome terminates without model continuation; unknown outcomes
are terminal with no automatic resend. Evidence: **198 passed** disposable-PG
regressions; **257 passed** holds/worker selection; touched ruff/pyright clean.
All public/lease/RPC ALLOW holds remain ON. No shared database apply.

### 035e2b live RPC eligibility — independently APPROVED

Connected authenticated internal RPC to transactional read-only validation of
persisted intent/approval digest and signed envelope, current principals,
policy/agent/registry/native or endpoint target, exact arguments and credential
set. Registry identity accepts the actual 16-hex fingerprint and existing
64-hex fixtures without padding or inventing revisions.
Evidence: **253 passed** combined disposable-PG/RPC/phase selection;
**172 passed** public-hold selection; touched-file ruff and pyright clean.
Boundary baseline unchanged: **175 errors / 9 warnings**.
Endpoint remains **deny-only**, including otherwise eligible candidates;
held runs deny. No public admission, lease, dispatch or ALLOW hold removed.
Independent review: APPROVE, minor evidence-entry omission corrected here.


### 035e1 executable deterministic pending-approval integration

Parent reported e0d round-2 independent APPROVE (current eligibility on replay).
Implementation ownership returned explicitly; coherent runtime changes below
completed, then workflow/tasks released back to phase owner for its three
quota/cancellation/finalizer review fixes. No concurrent workflow/tasks writes.

e1 evidence: actual worker branch occurs before model selection and any model
ticket/provider path. Store wrapper calls the new deterministic transaction
module, which revalidates persisted authority/provenance/current eligibility
under tenant→thread/run locks; uses stable UUID5 invocation/call/effect identity;
and reuses existing approval/invocation/events. Always requires approval even
when registered metadata does not. One versioned digested proof contains intent
binding and signed envelope; pending record preserves that proof. Legacy
`batch_digest` implementation remains unchanged. Atomically enters input_required
with next_action none, lease released, zero tool outbox/model ledger/model steps.
No decision/lease/public hold removed; distinct-principal functional approval
remains e2 work, not claimed by pending preparation.

Focused disposable PG/native/endpoint/published-agent/digest/rollback tests:
**13 passed**. Rollback after approval INSERT leaves no batch; retry uses the
same invocation/effect identity. Prepared duplicate attempts lose the fence,
not create another invocation. Stale policy/plugin/schema/target/ceiling,
deadline and cancelled run deny before approval/dispatch writes.
Worker/public/pure regression selection **294 passed**. Runtime/test ruff
passed; helper/store/workflow/tasks pyright **0 errors** before ownership
release, new module rechecked afterward with 0 errors. Boundaries **175/9
unchanged**, diff hygiene passed.

Combined e1+phase ledger/dispatch+held/authority/lineage PG selection:
**186 passed / 1 failed** at authority downgrade test expecting authority refusal
while an earlier phase migration correctly refuses persisted phase data first.
No fresh-DB isolated baseline reproduction; no test/migration/phase behavior
changed to hide that interaction. Initial e1+phase ledger/dispatch selection
**60 passed** before additional native-agent/rollback/digest cases. Previously
recorded worker baseline **74 passed**; not claimed as a clean full-suite gate.
Budget override: runtime/tests ~310 lines, but detailed pre-existing executable
plan plus evidence takes documentation-inclusive slice above 400.
Store wrapper/new transaction module location differs from initially planned
workflow-owned operation to keep phase workflow diff minimal and reusable.
Next independent e1 review; all holds ON. Shared DB untouched.

Next critical slice requires serialized return of **workflow.py + tasks.py**;
phase owns both until its acquisition/quota integration is complete. `llm.py`
is not required by 035e1. No overlapping implementation edits while phase owns
them. Do not create a second approval writer in store as a workaround.

Concrete existing seams: `_step` selects a model and begins a model ticket
before inspecting intent; branch must occur before either operation.
`_create_approval` constructs persisted preview from a fixed subset of fields.
`batch_digest` hashes only id/name/arguments; extra caller metadata alone would
not bind the reviewed intent. `_insert_invocations`, `_append_events`, fenced
thread/run locking and existing approval tables are reusable. Phase may shift
line numbers; these method contracts, not old offsets, identify the edit sites.

Allowed implementation files after ownership return:
* new `app/core/deterministic_intent.py` for persisted-authority validation,
  exact invocation/approval material construction, and versioned intent batch
  digest (use existing envelope signing/argument digest/effect key primitives);
* `app/worker/tasks.py`: branch before model selection/ticket/gateway, invoke
  deterministic store operation, never append model instructions or repair JSON;
* `app/core/workflow.py`: one fenced `prepare_intent_approval` transaction using
  existing pending-approval/invocation/event machinery, and intent-only metadata
  preservation/versioned digest path in `_create_approval`; legacy digest stays
  byte-identical. Do not add a synthetic model attempt or usage ledger entry;
* `app/core/store.py`: transactional current eligibility adapter using the
  already reviewed held-creation validator and stored tenant/submitter/policy/
  registry/plugin/schema/destination/agent/ceiling, not request/message inputs;
* new `tests/test_run_intent_workflow.py` and this evidence.

Operation contract: input is an existing fenced lease/run, not tool name/args
from caller. Only exact intent with singleton ceiling is supported. Lock active
tenant first then existing thread/run order; recheck current eligibility and
deadline/cancellation before any approval write. Reconstruct the frozen
preflight from persisted authority/run and bound schema descriptor, then compare
destination/call identity again. Derive stable call/invocation/effect identity
from stored run + intent binding; same intent retries do not mint new effects.
Construct one signed envelope with authority, exact canonical arguments,
intent binding and unchanged current grant/skill restrictions. Mandatory
`requires_approval=True` regardless of tool metadata.

Extend approval data with explicit version/intent binding inside its digested
representation, not an unsigned preview-only field. Persist exactly one pending
invocation and one pending approval batch; atomically enter input_required,
increment state version, clear executable next_action/release lease, emit
existing input_required event. No tool outbox, credential issuance, handler,
model reservation/call or continuation. Repeated already-prepared material must
match persisted batch/invocation/binding or deny; never silently replace it.

Staging rule: public admission, restricted lease acquisition and dispatch stay
held; no test-only production allow switch. Test the store operation with an
explicitly acquired fixture lease and real tables, and test the actual worker
branch with a fenced-store fake plus gateway/begin_step/repair/grant/handler
tripwires. This proves zero-LLM pending preparation, not reachable public action
execution or approval release. Existing d2 decision hold remains until e2.

Acceptance <=400 lines or split before implementation: actual PG pending rows
and digest/envelope agreement; mandatory approval on a normally ungated fake
plugin; one invocation; no model ledger/tool outbox/grants/handler; retry same
effect identity; stale policy/plugin/schema/destination/agent/ceiling, expired/
cancelled run and malformed persisted authority deny before writes; legacy
worker/model and batch digest unchanged. Fast lint/types/targeted tests and
boundary delta. If preparing plus digest integration exceeds cap, first slice
owns digest+transaction preparation and second owns worker branch, both held.

Following 035e2 remains mandatory: both API decision routes retain distinct
authenticated principal and batch binding; store decision rechecks exact intent
and current eligibility before secrets/writes; live RPC validates persisted
approval/invocation/binding at both stages; worker compares full target identity;
terminal after one result, unknown outcomes never redispatched. Only after all
proofs may a separate reviewed slice remove holds/publish request fields.

### 035e0d internal held-admission idempotency

Parent reported e0c independent APPROVE; user authorized next store-owned
slice while phase retains workflow/tasks/llm. Files: held-intent helper, store,
new `tests/test_held_intent_idempotency.py`, this evidence; <=400 lines.
Use existing `admission_requests`, existing replay/conflict exceptions and
transactional admission INSERT; no new table/migration/public activation.
Optional internal idempotency key binds immutable intent context.
Server derives domain-separated request digest from immutable preflight
(including ceiling/policy/schema/plugin/revisions) and effective supplied run
fields, excluding only assigned run ID. Caller may not inject admission digest,
response or authority. Conservative explicit-vs-omitted optional fields can
conflict; no semantic broadening.

Same tenant/principal/run.create/key serializes with transaction advisory lock
before thread/derivation checks, then compares live persisted admission digest.
Matching replay returns original run response; altered pins/args/ceiling/model/
budgets/thread conflict without INSERT or schedule. Existing SQL-NULL legacy
digest/admission behavior untouched. All restricted lease/outbox holds remain.
Baseline latest PG storage selection **100 passed**; pure/API **293 passed**;
helper/store lint/types clean; boundaries 175/9.

e0d final: server digest also includes the resolved trusted destination descriptor.
Actual DB selection **100 → 116 passed** (16 new cases), including concurrent
same-key callers yielding one INSERT/one replay, original persisted response,
intent key binding, changed canonical args/policy/ceiling/model/budget/thread/
destination conflict, foreign tenant denial and caller-digest injection denial.
Pure/API selection **293 passed** unchanged. Touched-file ruff passed;
helper/store pyright **0 errors**; boundaries **175/9 unchanged**; diff hygiene
passed. Runtime/tests/framing under 400 lines. No workflow/tasks/llm/main,
API schema, shared DB or activation edits. Existing legacy storage admission
tests remain passing. No model/tool execution or new effect on replay.

Internal held creation still reads policy/destination before replay and can
fail closed if those are unavailable; replay is not a right to bypass current
eligibility, nor an allow decision. Assigned run ID excluded; chosen thread and
supplied messages retained, so clients must retry the same effective request.
No expiry-extension semantics added to admission records. Public endpoint
remains reject-only and approval/RPC remain deny-only.
Next independent e0d review, then serialized deterministic workflow integration.

#### e0d round-2 current-eligibility replay correction

Review found matching replay returned before transactional current eligibility.
Store now retains the matching response under the advisory lock, validates the
locked active tenant/policy and published-agent fence, checks intent provenance,
then executes the same held-creation validator (active locked membership/
principal, actual policy/config digest and agent skills/models/budgets,
registry/plugin/schema/destination/ceiling pins) before raising replay.
It still skips thread active-run/derivation checks on a valid replay because no
new run is created. No eligibility is inferred merely from stored response.

Actual PG cases revoke membership/principal, suspend tenant, replace current
policy or alter registry/plugin after preliminary policy read; all deny
matching replay without new run/admission/outbox writes. Real published-agent
replay succeeds unchanged, but disabling its granted plugin fails current
agent policy validation; publication/config rows remain immutable, not
artificially modified to invent revocation behavior.
Same PG selection **116 → 124 passed**; pure/API **293 passed** unchanged.
Touched-file ruff passed; store pyright **0 errors**; boundaries
**175/9 unchanged**; diff hygiene passed. Own disposable PG stopped.
No shared apply, phase-path edits or activation. Round-2 review requested.

### 035e0c native target and published-agent held creation

e0b round-2 independent APPROVE reported by parent; all holds retained.
User authorized next nonoverlap slice: `app/core/held_intent.py`,
`app/core/store.py`, new `tests/test_held_native_agent.py`, this evidence;
<=400 lines; no workflow/tasks/llm edits or public admission.
Native destination is the trusted registered call identity: exact plugin/tool,
plugin revision, handler module/qualified name, execution kind, queue, context
injection and schema SHA. Never invent a native URL, inspect caller target
arguments as authorization, or claim identity constrains internal plugin egress.
Existing plugin loading, policy, SSRF, sandbox and secret checks remain required.
Static/catalog-only and nonfunction handler targets deny.

Published-agent integration reads the actual immutable configuration/digest
under transaction lock, validates policy and registry pin, then requires stored
run skills/preloads/models/budgets to equal that config. It cannot replace the
e0b exact skill ceiling with caller authority. Run agent revision is already
immutable for non-NULL authority. No idempotency/public activation in this slice.
Actual PG proof: native held INSERT/zero lease, target mutation denial,
published revision configuration/digest agreement and override denial.
Baseline latest e0b PG selection **88 passed**; pure/API **293 passed**;
root/helper/store lint/types clean; boundaries 175/9.

Implemented native descriptor using actual registered Python function metadata;
no tool execution, invented network destination or endpoint metadata fallback.
Transactional held creation now supports immutable published-agent records:
configuration digest/policy validation and exact stored skill ceiling, preloads,
model/budget equality. Missing/ineligible agent remains fail closed through the
existing policy/registry publication fence and new config check.
Final PG selection **88 → 100 passed**, including native handler/queue/context
identity mutation after initial binding and real published-agent INSERT/override
denials. Pure/API selection **293 passed** unchanged. Touched-file ruff passed;
helper/store pyright **0 errors**; boundaries **175/9 unchanged**; diff hygiene
passed. New tests 132 lines; runtime/test/evidence slice below 400 lines.
Own disposable PG stopped; no shared apply or phase-owned edits.

Native target binding is stored now, but runtime comparison at worker/final
adapter and authenticated live RPC remains required before any release.
No native external egress identity guarantee is claimed; registered plugin
code may choose destinations under the existing egress/SSRF/secret sandbox
restrictions, which this call identity neither replaces nor widens.
Functional deterministic scheduling/approval, public idempotency, exact
approval binding and full allow-path proof remain pending. All holds ON.
Next independent e0c review; workflow integration after phase owner returns
the shared paths, not concurrent edits.

### 035e0b connected internal held-intent creation

User authorized store integration while workflow/tasks/llm stay phase-owned.
Files: new `app/core/held_intent.py`, `app/core/store.py`,
`app/core/plugins.py` metadata-only additions, new
`tests/test_held_intent_creation.py`, this evidence; <=400 lines.
Connect e0 snapshots to internal `create_held_intent_run` then actual create
transaction: locked active policy, authenticated tenant/submitter provenance,
active submitter membership/principal locks, schema/args/plugin/registry and
skill ceiling recheck. Construct run-owned intent, not caller authority.
Existing non-NULL authority immutability and zero-outbox/lease holds remain.

Destination support is deliberately fixed registered HTTP/MCP endpoints:
registry records the URL/protocol already captured by the real adapter.
Bind URL/protocol/queue/schema SHA in canonical destination descriptor alongside
existing intent tenant/run/principal/policy/plugin/args binding. Policy exact
destination host and existing SSRF checks remain conjunctive. Native dynamic
destinations without metadata deny, not guessed; static schemas deny.
Check actual adapter serialization (field names, omit None) equals reviewed
full argument object; reject otherwise without changing legacy adapter code.
No authenticated public request field, no endpoint allow or model scheduling.
Held helper rejects replay admission, parents and mixed phase authority;
future public admission must add reviewed idempotency wiring, not bypass this.
Existing direct authority insertion remains internal storage staging, held.
Baseline latest store/plugin lint/types clean; pure e0/policy **72 passed**.

Parent reported e0 independent **APPROVE** during implementation. e0b now
connects snapshot → server destination/run binding → locked revalidation →
actual immutable INSERT. Principal/membership locks are held through commit.
Unsupported pinned-agent revisions also deny until their stored skill ceiling
can be derived here; do not trust a caller ceiling to replace that integration.
No new public fields/settings/dependency/migration or SDK extension point.

Final touched-file ruff passed; held helper/store/plugins pyright **0 errors**.
Disposable PG focused selection **67 → 84 passed** (17 new connected-creation
cases): real HTTP/MCP registration, persisted complete authority, no outbox or
lease, revision/schema/plugin/registry/member/preload/ceiling/foreign/phase/
agent denials, policy changing between read and transaction, destination
changing after binding, and adapter-null mismatch. One fixture schema edit
needed Pydantic `model_rebuild` to create an actual revised validator/schema;
no runtime bypass was added. Existing phase storage/lineage tests retained.
Pure/API/phase selection **292 passed**; existing endpoint MCP/manifest SSRF
selection **3 passed**; boundaries **175/9 unchanged**; diff hygiene passed.
Broader `test_core.py` plus that API selection: **318 passed / 1 failed** at
sample-plugin sandbox queue expectation (`q_tools_fast` vs `q_tools_sandbox`).
Outside changed endpoint metadata registration; no fresh HEAD reproduction,
so report separately rather than claim a clean broad baseline or fix it.
Previously reported nine raw-secret/positional worker-test signature failures
remain unrelated and unchanged; never restore raw-secret execution arguments.

Runtime/tests remain below 400 lines with this framing/evidence. Own disposable
PG container stopped; no shared DB apply. Holds/public rejection/deny-only RPC
unchanged. Next: independent e0b review, then workflow integration after phase
ownership returns. Native destination resolution, pinned-agent integration,
public idempotency, exact approval binding, endpoint allow and full release
proof remain explicit follow-ups, not completed by held creation.

#### e0b round-2 ceiling-pin correction

Independent review found preflight pinned only preload skills, allowing a
different stored skill ceiling at held creation. Root correction adds frozen
`skill_ceiling` to the snapshot (sorted unique names, or explicit NULL).
Transactional recheck first requires the stored run ceiling to match this pin,
then recomputes eligibility using the **pinned** ceiling, not caller authority.
NULL remains distinct from `[]`; non-NULL name ordering has no authority meaning.
Snapshot creation detaches mutable inputs; no default broadening is introduced.

Real PG tests replace a narrow pin with NULL, empty, or a wider list and verify
zero run/outbox INSERTs. A NULL pin rejects `[]` replacement and persists only
as NULL. Pure test verifies detached immutable ceiling and stable ordering.
Same PG selection **84 → 88 passed**; pure selection **72 → 73 passed**;
expanded API/policy/phase selection **292 → 293 passed**.
Touched-file ruff passed; root/helper pyright **0 errors**; boundary
**175/9 unchanged**; `git diff --check` passed. Own disposable PG stopped.
No store/workflow/tasks/llm/endpoint behavior expansion or shared apply;
all holds remain ON. Round-2 independent review requested.

### 035e0 strict pinned argument preflight

User authorized a bounded nonoverlap helper while workflow/tasks/llm remain
036-owned. Files: new `app/core/intent_preflight.py`, new
`tests/test_intent_preflight.py`, this evidence; <=400 lines. Internal pure
snapshot only, not a public extension point or authority-minting API.
Uses actual `TenantPolicy.resolve_plugin_access`, registry unlocks, pinned
agent skill ceiling, strict canonical argument validation and loaded plugin
revision. Requires authenticated canonical tenant/submitter IDs and explicit
singleton tools/preloads. Returns immutable argument bytes plus full schema
SHA256, actual registry fingerprint and policy/plugin revisions for subsequent
transactional revalidation. Changed policy digest, unavailable schema or
unsupported static catalog denies. Parent restriction conservatively denies.
No destination is guessed or accepted from user arguments to mint authority;
destination resolution and actual adapter payload equivalence remain required
before constructing the complete run intent. Existing registry fingerprint
format is retained honestly; RPC format integration remains pending.
Baseline restrictions/policy selection **40 passed**; existing helper lint/types
clean. No workflow/tasks/llm/store/mesh/main/endpoint edits or gate removal.

Implemented snapshot preflight with real policy/registry objects and fake
plugin handlers that fail if executed. Tests cover strict full/default/alias/
extra/transform/coercion/size/finite semantics, detached immutable bytes, schema
revision visibility, revoked policy/plugin, skill ownership/readiness, hidden
preloads, exact singleton and inherited ceiling denial. No companion tool is
implicitly added. Status-bearing generic errors retain 401/403/409/422 mapping
for later API integration without exposing argument values.

Final new-module/test ruff passed; module pyright **0 errors**.
Pure baseline plus new tests **40 → 72 passed**. Expanded pure+approval+
authorization+authority/phase rejection+facade selection **292 passed**.
Boundary **175/9 unchanged**; `git diff --check` passed.
Runtime/tests **278 lines**, including this framing/evidence below 400.
No live keys, network/model/handler calls, DB or public contract changes.
Helper is not yet wired into create admission: destination resolution,
transactional pin recheck and deterministic approval workflow integration
remain mandatory; no intent authority or dispatch allow is minted here.
Next independent e0 review, then wire into deterministic path only after
serialized workflow ownership returns and complete bindings are available.

### 035d2 approval API secret-prewrite hold

User authorized this nonoverlapping follow-up while d1 review and 036 runtime
integration proceed. Files: `app/main.py`, new
`tests/test_run_authority_approval_api.py`, this plan; <=400 lines.
No workflow/tasks/llm/store/mesh edits. Shared `_decide` must resolve the owned
persisted run and reject any non-NULL authority before ephemeral secrets,
decision replay/writes or enqueue. Both existing decision routes remain
available for legacy authority; separation of duties and tenant checks remain
conjunctive. Null-tools/deny-all/intent/malformed non-NULL authority deny; no
allow-path switch. Tests exercise both routes, decisions, replay keys, closed
batches, self/foreign principals and zero secret/decision effects.
Baseline main ruff/types clean; authorization + authority reject API + OpenAI
compat selection **131 passed**. All holds remain ON.

Implemented shared owned-run check in `_decide` before request ID generation,
ephemeral secrets or `decide_approval`. Presence of raw non-NULL authority or
decoded authority denies with `restriction.resume_unavailable`; missing run
binding denies. Approval roles/self-separation remain route checks; foreign run
ownership is checked again for both entry paths. Legacy flow still writes
supplied secrets then passes verified actor/request binding to the store.
This only closes restricted staging's prewrite hole, not atomic legacy secret/
decision persistence, nor functional exact-intent approval authorization.

Final main/test ruff passed; main pyright **0 errors**. Same baseline selection
plus 31 new approval API cases: **162 passed**. Including independently approved
phase rejection tests: **220 passed**. Both routes tested for zero secret/
decision effects on restricted approve/reject, raw malformed/null/empty
authority, decoded authority, replay keys, closed batches and self/foreign
principals. Public OpenAPI compared in-memory to current snapshot: unchanged;
no public fields or contract activation. Boundary **175/9 unchanged**;
`git diff --check` passed. One command referenced a nonexistent phase-test
filename; corrected to `tests/test_run_phase_api.py`, with no runtime changes.
No phase-owned edits, DB, keys, deployment or hold removal. Next independent
review before any exact-intent approval allow path.

Parent subsequently reported independent **APPROVE** for d2. Functional intent
approval remains pending; this approval does not remove any hold.

### 035d1 review correction — source admission serialization

Independent review requested source-thread locking through derivation writes.
Corrected only store/mesh/inheritance tests: delegation now has an explicit
transaction spanning checks, child INSERT and delegation INSERT. Parent checks
lock all parent-run/thread ancestry sources; admission locks those sources and
its target together in stable thread-ID order **before** its target row check.
Branch uses the same ancestry lock helper. Locks remain transaction-held through
writes; authority checks use a fresh statement after locks are acquired.
No workflow/tasks/llm edits or phase ownership changes.

Two actual disposable-PG concurrent regressions hold an admission source-thread
lock with an uncommitted authority INSERT, start the real delegation route or
cross-thread child admission, observe lock waiting, commit the writer, and
verify denial with no child/outbox/decision side effects. This specifically
covers parent source != child target, not merely target-thread serialization.
Existing branch concurrency and ancestry cases retained.

Same focused PG selection **65 → 67 passed**. API/authorization/phase rejection
selection **220 passed**; touched-file ruff passed; store/mesh pyright **0
errors**; boundary **175/9 unchanged**; `git diff --check` passed.
Only disposable PG upgraded; own container stopped; no shared apply.
Budget override: required concurrency correction adds ~100 runtime/test lines
to the previously bounded d1 slice; retain real race proofs rather than omit
coverage to meet the documentation-inclusive cap. Round-2 review requested.
All activation holds remain ON.

Parent reported d1 independent round-2 **APPROVE** after the source/ancestor/
target-lock correction and actual concurrent-insert proofs. This does not
activate restricted derivation or release any execution hold.

### 035d1 bounded inheritance-denial slice (2026-11-09)

Parent reported independent **APPROVE** for 037 worker hooks, the 03501
downgrade-race correction and 036 storage persistence. `store.py` returned to
035 ownership for this slice; preserve approved phase fields/decode/holds.
Phase engineer owns only `main.py` phase raw-request gate plus its new tests;
no 035 main edits. `workflow.py`/`tasks.py` retained through this slice, then
released for serialized 036 integration after handoff.

Files: `app/core/store.py`, `app/core/workflow.py`,
`app/api/agent_mesh.py`, new `tests/test_run_authority_inheritance.py`, this
plan. <=400 lines including tests/evidence; no migration/dependency/public
request-field activation. Plan scoped from actual delegation INSERT, parent
admission, branch storage and approval/resume scheduling seams.

Any non-NULL authority (including null-tools, deny-all, intent or malformed
JSON objects) on a parent/ancestor denies child derivation before writes.
Thread ancestry/history also denies branching or new admission, including
completed restricted runs and preexisting legacy branch/child laundering.
No intent is rebound to a child; the conservative ceiling is no derived work.
Branch source now takes the same update lock as admission, so it cannot pass
its history check while an authority admission holds that source lock.
Tenant-scoped parent lookup occurs before revision lookup/delegation INSERT;
foreign parents are hidden. Existing legacy foreign-parent admission error
shape is retained. Restricted approval/resume workflow denies before replay,
decision/outbox writes: no restricted scheduling is enabled by resumption.
API secret prewrites are not solved here; release proof must cover that seam.

Baseline (before edits): inheritance-area ruff/types clean; multi-agent/
restrictions/storage/dispatch selection **59 passed / 20 DB-only skipped**.
Previously approved worker selection **80 passed**; boundary **175/9**.
Disposable PG qualification includes legacy lineage, all authority variants,
branch/child/continuation/resume zero-write denial, malformed/ancestor cases,
foreign delegation denial and actual in-flight authority INSERT under the
admission thread lock. Existing persisted intent/digest retry/restart tests
remain in the regression selection; no replacement authority is reconstructed.

Final evidence: touched-file ruff passed; pyright on store/workflow/mesh **0
errors**. Disposable PG authority inheritance/storage/DB/dispatch + multi-agent
lineage + phase persistence selection **65 passed** (11 new inheritance cases).
Worker/client/skill/pure authority/public reject-gate selection **188 passed**.
Boundary **175 errors / 9 warnings → unchanged**; `git diff --check` passed.
Broader selection adding phase2 DB tests: **75 passed / 1 failed** at
`test_production_login_rejects_owner_and_service_api_identity`. Isolated rerun
reproduced that production-init failure; AST source comparison confirms the
entire failing `DistributedStore.init` is byte-identical to HEAD. None of the
new derivation/decision methods runs in that test; left untouched, not claimed
as a clean broad gate. Initial inheritance fixtures were corrected for paired
branch history fields and the one-active-run index; legacy foreign-parent
error type was retained after its regression detected the changed shape.

No phase storage code changed; no main/worker-task edits, migration, shared DB
apply or action activation. All holds remain ON. At handoff `workflow.py` and
`tasks.py` are released for parent-serialized 036 integration, with this slice's
workflow denial preserved. `store.py` stays 035-owned until another explicit
handoff. Next independent review concerns this derivation-denial slice only;
deterministic intent, approval/secret-prewrite binding, destination/registry
integration and real endpoint allow proof remain unimplemented.

### Authority downgrade concurrency correction (2026-11-09)

User authorized this isolated correction before any shared migration apply.
03501 now takes `ACCESS EXCLUSIVE` on `runs` **before** its authority-data
check and retains the transaction lock through DDL. Actual concurrent-writer
regression invokes 03501 directly, so 03602's earlier lock cannot mask a bug.
Before the fix, the regression failed: downgrade succeeded after the writer
committed authority. After the fix, it waits, sees the committed authority and
refuses; authority and its immutability trigger survive.

Disposable PostgreSQL16: upgrade to 03602 → downgrade to 20261024_01 →
upgrade to 03602 succeeded on empty authority state. Authority DB/storage/
dispatch plus phase DB selection: **47 passed**. Touched-file ruff passed;
migration pyright **0 errors**; boundaries **175 errors / 9 warnings unchanged**.
No shared DB apply; no 036 migration edits or store ownership changes.
Correction queued for independent review; all 035 holds remain ON.

Bounded source review, 2026-11-09; no code or test execution:

* `app/main.py:214–230`: actual class is `CreateRunRequest`; it contains `skills`,
  `preload_skills`, budgets and secrets, not a tool allowlist or exact intent.
  `RunCreateRequest` is the TypeScript public request type.
* `app/main.py:758–890`: admission uses `run.submit`, request idempotency,
  pinned agent revisions, tenant plugin access and run skill narrowing.
* `app/worker/tasks.py:270–475`: signed invocation body carries skill authority,
  plugin digest and argument digest; `_step` derives model schemas from skills
  and constructs invocation batches from model output.
* `app/worker/tasks.py:632–855`: worker verifies envelopes, uses effect-key Redis
  deduplication, checks plugin digest, redeems credential grants and then calls
  `_async_execute_tool`; that function checks skill unlock and repairs model JSON
  before schema validation. Exact-intent checks must precede grant redemption.
* `app/core/authorization.py`: approver actions are distinct from submitter actions;
  authenticated self-decisions and missing authenticated submitter provenance
  are rejected. Preserve these checks on both decision routes.
* Searches locate `DistributedStore.create_run` in `app/core/store.py:1425`,
  workflow `decode_run`, `_dispatch`, `_create_approval`, `decide_approval`,
  `app/api/agent_mesh.py` delegation, and public `/v1/plugins` catalog.
  Their complete implementations were **not** inspected; no claim of safe
  inheritance, metadata persistence, grant timing or catalog adequacy is made.

Operator reports plans 029–034 implemented/passing, approval review working,
live artifact save verified, browser/live processes still running, and only the
submitter credential known. These are supplied context, not reverified results.
Some existing plan/index status text lags that report; do not rewrite it as evidence.
Do not touch running processes, secret files (including `user.env.haas`), or data.

Exploration stopped at approximately 25 read/search calls including governance
and discovery; remaining unknowns are explicit gates, not permission to guess.

## Contract and invariants

ADR-0021 defines the complete wire/argument contract. Required invariants:

1. `tools=null` is legacy behavior; `tools=[]` denies every tool; exact names only.
   Explicit requests outside policy/unlocks/inherited authority fail admission.
2. `invocation_intent` requires an authenticated submitter, exactly one requested
   tool and policy-permitted preloads that already unlock it. It schedules one
   deterministic invocation, never calls a model and always pauses for approval.
3. Store the reviewed full effective JSON object immutably. Strict schema validation
   may not coerce, insert defaults, drop keys or transform it. Reject changes with
   422 rather than silently changing what the user reviewed.
4. Bind canonical args, target identity, tenant, submitter and revisions to the
   stored intent, approval batch, invocation and signed envelope. Check before
   grants and immediately before adapter/handler dispatch.
5. Restriction survives checkpoints, worker redelivery, approval resumption,
   branching and child runs; activation cannot widen it.
6. Both approval decision routes retain tenant checks, distinct principal checks,
   batch ID/digest, expiry, conflict and idempotency semantics.
7. Tenant policy/revocation, egress/SSRF, secret scope, plugin digest, sandbox and
   effect deduplication are additional conjunctive checks, not replaced by intent.
8. No action initiator is exposed until the complete core+SDK proof is approved.
   Missing approver credential/session boundary means pending/disabled, not bypass.

### API errors and compatibility

Use existing Problem response machinery: malformed/duplicate/oversized fields,
intent without explicit singleton tools/preloads, or changed effective arguments:
422; absent authenticated intent submitter: 401; policy/parent restriction excess:
403 (do not disclose inaccessible inventory); pinned revision/inventory drift or
idempotency reuse with different content: existing 409/availability semantics.
Admission request digest must include both fields, including explicit empty tools.
Keep existing approval decision error semantics, not a parallel decision protocol.
Runtime denials produce a stable generic `restriction.*` error code and no handler
call; do not log argument values, credentials or sensitive previews.

## Ordered slices

Each row is a separately reviewed, releasable slice, ≤400 non-generated changed
lines. The user subsequently authorized one additive, reversible migration for
generic run-owned authority in 035a after inspection found no suitable existing
migrated JSON field. Legacy rows remain unrestricted through nullable authority;
round-trip verification uses disposable PostgreSQL, and shared-database upgrade
remains gated on review. Other rows introduce no migrations. If the measured diff exceeds its
cap, stop and split **before** continuing; never omit a security check to fit.
Future child plans use the next available numeric plan IDs, retaining these labels.

| Slice | Owner | Scope / planned files | Acceptance / budget | Depends on |
|---|---|---|---|---|
| 035a | core-engineer | Authority/storage contract and pure matching helper; details below | Strict semantics and persistence gate; ≤350 lines | User confirms ADR contract |
| 035b | core-engineer | `app/main.py`, `app/core/store.py`, `app/core/workflow.py`; create/decode restrictions, request digest, pinned configuration; `tests/test_run_restrictions_api.py` | Null/empty/excess, restart and idempotency tests; ≤350 | a |
| 035c | core-engineer | `app/worker/tasks.py`, `app/core/workflow.py`, actual grant-dispatch module identified in a; `tests/test_run_restrictions_dispatch.py` | Model schemas, forged calls, activation, grants and worker final-entry denial; ≤400 | b |
| 035d | core-engineer | `app/api/agent_mesh.py`, actual branch/child creation paths identified in a; `tests/test_run_restrictions_inheritance.py` | Child cannot widen; branch/retry/restart retain ceiling; fail-closed delegation fallback; ≤300 | c |
| 035e1 | core-engineer | `app/worker/tasks.py`, `app/core/workflow.py`; deterministic intent + existing pending approval creation; `tests/test_run_intent_workflow.py` | Zero model calls; exactly one pending invocation, no dispatch before approval; ≤350 | d |
| 035e2 | core-engineer | `app/core/workflow.py`, `app/worker/tasks.py`, existing grant/adapter seams; `tests/test_run_intent_dispatch.py` | Binding/tamper/race/expiry/redelivery tests; terminal after result, no new effect on unknown outcome; ≤400 | e1 |
| 035f | core-engineer | `app/main.py`, `docs/openapi-v1.json`, `docs/api-changelog.md`, `tests/test_run_intent_api.py` | Publish intent admission only when e2 complete; both approval routes retain SoD; ≤300 excluding generated | e2 |
| 035g1 | client-engineer | `haas_sdk/client.py`, `async_client.py`, `_specs.py`, relevant fake/type/export files; `tests/test_sdk_run_restrictions.py` | Sync/async JSON passthrough incl. `[]`, null, exact args, idempotency and error parsing; ≤350 | f |
| 035g2 | client-engineer | `haas_sdk_ts/src/types.ts`, `client.ts`, neighboring SDK tests | `RunCreateRequest` fields, no widening/default rewriting, approval API reuse; ≤250 | f |
| 035h | plugin-engineer | Atlas service-local server/proxy configuration, review and submission modules; exact paths inventoried before child plan | Public catalog reuse, immutable reviewed inquiry intent, separate approver-session proof and no browser secret; ≤350 | g1/g2 as consumed |
| 035i | plugin-engineer | Incident plugin/service public-client integration or isolated extension fixture if no deployed consumer | Same generic contract, no core domain edits; independent recipient tamper denial proof; ≤250 | f + consumed SDK |
| 035j | verifier/reviewer | No implementation edits; docs evidence by owner | Full acceptance matrix, independent APPROVE, authorized live proof or explicit operational block | h, i |

Until f publishes intent admission, earlier slices must reject non-null intent
requests (explicit 422), not silently ignore them. Earlier exact-intent internals
are exercised directly by tests and cannot be reached by external callers.
Tool restrictions may ship before intent only after c/d are complete; b must
reject non-null restrictions until that point. Activation gating is internal,
default off, and cannot be overridden by client input. Do not send new fields to
old deployments that might ignore them.

## First full slice: 035a — immutable authority substrate

Status: **DONE / APPROVE** — parent reported independent review round-2 APPROVE
on 2026-11-09. This applies only to the substrate/migration, not admission,
dispatch enforcement or action release. Migration remains unapplied to shared DB.

### Goal and owner

Core-engineer establishes exact canonical/matching semantics and identifies the
existing authoritative persistence and dispatch seams without exposing a partial
action API. No new public runtime field is enabled in this slice.

### Allowed paths / planned files

* `app/core/run_restrictions.py` (new): frozen typed restriction/intent models,
  versioned canonical serialization, binding digest, subset/intersection and
  strict exact-argument comparison helpers. No DB, registry globals or domain names.
* `tests/test_run_restrictions.py` (new): pure unit tests with two synthetic plugins.
* `docs/plans/035-bounded-action-initiation.md`: fill the seam inventory and baseline.
  Architect owns follow-up design amendments; engineer reports proposed changes.
* No modifications to other runtime paths in a; report a blocker instead.

### Mandatory seam inventory, before implementation

Read only the located create/decode/dispatch/approval/delegation functions and their
direct dependencies. Record concrete field names and call sites in the child handoff:

1. Does existing **run-owned** persisted JSON/config metadata survive create,
   checkpoint, resume and decode unchanged? Which field and serializers?
   Agent-revision config or message history is not a substitute. If absent:
   **stop**, ask whether to authorize a separately planned migration. No migration
   and no transient-only fallback.
2. Where are credential grants minted, where is the transactional dispatch outbox
   assembled, and where does each native/HTTP/MCP/sandbox adapter receive args?
   Record all callers, including direct dispatch routes; missing a path blocks c.
3. Which APIs actually create children or derived branch runs? Can the parent
   restriction be loaded by authenticated tenant and bound server-side?
   If not, d rejects delegation from restricted parents; no inherited client claim.
4. Does tenant-filtered `/v1/plugins` return the target tool schema and prerequisites
   to an authorized submitter? Reuse if yes; if not, report the exact missing field
   and propose additive catalog metadata, not a new endpoint, before consumer work.
5. Can existing deployment capabilities reliably exclude old workers from new work?
   Require a homogeneous core rollout/drain if not. No new work while old dispatchers
   can drop a restriction. Identify legacy envelope compatibility rules explicitly.

### Implementation contract

#### 035a concrete seam inventory (engineer; 2026-11-09, review round 2)

* **Storage gate and migration:** no existing run-owned configuration field exists
  in the checked-in migrations or `DistributedStore.create_run` INSERT.
  `agent_revisions.configuration`, thread `metadata`, message checkpoints and
  admission replay responses are not run authority. The mesh INSERT's `runs.config`
  reference has no corresponding migrated column. Operator authorized
  `20261109_01_run_authority`: nullable `runs.authority_json JSONB`, object-only,
  retaining existing `runs.tenant_id`/`tenant_uuid` and forced RLS. The trigger
  `runs_immutable_authority` rejects authority changes (including NULL→object)
  and provenance changes on non-NULL authority. Downgrade refuses any non-NULL
  authority rather than silently removing restrictions. No shared DB application.
* **Serialization/lifecycle:** frozen `RunAuthority.model_dump_json()` and
  `model_validate_json()` round-trip; `ExactIntent.arguments_json` holds sorted,
  compact UTF-8 canonical object text. These helpers are **not wired**:
  `create_run` does not yet insert authority; `workflow.decode_run` only decodes
  `active_skills_json`, `messages_checkpoint_json`, `fingerprints_json`,
  `skill_allowlist_json` and `pending_tool_calls_json`. `get_run` and lease
  acquisition call `decode_run`. `commit_step`, `commit_aggregate`, `_finalize`
  and `decide_approval` update checkpoints/status, not authority. The new column
  remains unchanged by those UPDATEs, but create/decode/checkpoint/resume authority
  integration proof belongs to b–e; this is not implemented persistence wiring.
* **Tenant/submitter provenance:** `main.create_run` obtains authenticated identity,
  loads tenant policy/pinned agent revision and passes identity principal,
  policy revision and registry fingerprint to `store.create_run`. Its transaction
  locks the active tenant policy, checks published revision eligibility and locks
  the tenant-owned thread. It stores `submitted_by_principal_id`,
  `policy_revision_id`, `agent_revision_id`, `registry_fingerprint` and the
  thread's tenant ID. Authority must be built/bound server-side in that transaction;
  no client digest or conversation history supplies provenance.
* **Invocation/outbox/grants:** worker `_step` calls `build_invocation`;
  approval `_decision` rebuilds invocations with the original invocation ID.
  `workflow.commit_step` calls `_insert_invocations`, `_create_approval` or
  `_dispatch`; `dispatch_approved` also calls `_dispatch`. `_dispatch` updates
  invocation status and writes the transactional tool outbox. Worker
  `make_outbox_publisher` calls `credentials.prepare_tool_dispatch`, which
  resolves scoped secrets, calls `seal_grant`, records grants and seals egress
  policy. Worker `execute_tool_task` verifies the envelope; `_execute_envelope`
  performs Redis effect reservation/result lookup, plugin-digest checking and
  `redeem_grants` before `_async_execute_tool`. Restriction checks must precede
  grant issuance/redemption and final dispatch; none are wired yet.
* **Adapter entries:** `_async_execute_tool` currently repairs/validates arguments,
  then calls native `tool.handler` or `sandbox.execution.execute_in_sandbox`.
  The latter serializes models and calls `run_in_sandbox_materialized`;
  `sandbox.launcher` repairs/revalidates before invoking its handler.
  HTTP/MCP adapters are the `core.plugins.register_external_manifest` handler
  closure: resolved endpoint URL, model payload serialization and HTTP POST/MCP
  request construction. Exact intent must bypass repair and compare the final
  alias-aware effective object at these boundaries. Endpoint model construction
  currently captures only part of manifest schema; full pinned-schema validation
  and destination identity binding remain unresolved implementation work.
  `/v1/isolation/sandbox/execute` currently materializes and returns a protocol
  stub, not a tool effect; do not treat it as an enforced invocation entry.
* **Approval identity:** both `/v1/runs/{run_id}/resume` and
  `/v1/approvals/{batch_id}/decision` load tenant-owned state and call
  `enforce_approval_separation_of_duties`. The store decision transaction preserves
  batch ID/digest, expiry, conflicts and idempotency. Exact intents still need
  mandatory authenticated approval and binding inside existing digested call data.
* **Inheritance:** `/v1/agent-mesh/delegations` directly INSERTs child runs using
  `child_agent_config`; its parent lookup has no explicit tenant predicate
  (relies on connection/RLS), and no authority inheritance is implemented.
  Restricted-parent delegation must fail closed until authenticated server-side
  inheritance exists. `/v1/threads/{thread_id}/branches` calls
  `store.branch_thread`, which copies tenant-scoped committed messages and records
  `parent_thread_id`/`parent_history_version`, not run authority. Independent run
  admission is fresh authorization; derived-run attenuation must never come from
  copied history. Retry/resume enforcement remains outstanding.
* **Catalog/rollout:** tenant-filtered `GET /v1/plugins` exposes tool name, queue,
  ordinary approval flag, plugin version, skill `required_tools` and filtered
  fingerprint, but not argument JSON Schema, immutable plugin digest, policy
  revision or resolved destination identity. Operator accepted client-owned args
  with core revalidation; optional catalog expansion is deferred, not required.
  Revisions/destination must be resolved and bound by core, not trusted from a
  client or exposed as raw credential-bearing endpoint configuration.
  Envelope v1 verifies signature, expiry and argument digest but has no authority
  version enforcement. No inspected worker capability routing excludes old code.
  Require homogeneous API/orchestrator/tool/sandbox rollout and drain/restart
  before activation; no new work through mixed dispatchers. Legacy NULL authority
  remains compatible. Explicit-field rejection must precede feature publication
  because current `CreateRunRequest` silently ignores unknown fields.

Use existing dependencies/Pydantic v2. Strict intent args accept JSON objects only.
Validate against the supplied pinned tool schema, compare canonical submitted and
validated JSON with defaults included, and reject on any difference. Keep the
legacy model JSON repair path separate. Tools list membership applies to *all*
names; no implicit control tool. Pure helper takes policy/unlock/parent inputs,
not plugin-specific registry lookups. Digests must use version/domain separation
and structured serialization, not ambiguous string concatenation.

### Acceptance criteria

- [ ] Seam inventory names an existing persisted metadata field or returns BLOCKED;
  no unsupported assertion that a run-config field exists.
- [ ] Tests distinguish omitted/null/empty tools, subset/excess and parent narrowing.
- [ ] Tests reject unknown fields, duplicates, >128 names, >64 KiB args, nonfinite
  numbers and schema coercion/default insertion/extra-key dropping.
- [ ] Tests show key-order equivalence but different array order, whitespace inside
  strings, omitted/null and different recipient values do not match.
- [ ] Changing tenant, submitter, policy/plugin revision, target or args changes
  binding; intent model immutable; nested input mutation cannot alter stored bytes.
- [ ] Synthetic plugin A cannot reuse B's intent binding; tenant A cannot reuse B's.
- [ ] No public create/dispatch/role behavior changed; no new dependency/migration.
- [ ] Ruff, Pyright, targeted pytest and boundary delta recorded vs baseline.

### Baseline / verification

#### Engineer evidence: 035a review round 2 (2026-11-09)

* Before this correction, touched-file ruff passed; pyright reported 0 errors;
  targeted pytest reported **90 passed, 1 skipped** (the disposable-DB test).
  Boundary baseline: **175 errors, 9 warnings**.
* After correction, the same commands reported ruff passed, pyright 0 errors,
  **97 passed, 1 skipped** and the same **175 errors, 9 warnings**. Added seven
  rejection cases for empty/noncanonical plugin provenance; binding-isolation
  tests now use valid lowercase 64-hex plugin revisions. `hash_plugin` defines
  that representation with SHA-256 `hexdigest()`.
* Commands: `.venv/bin/ruff check app/core/run_restrictions.py
  migrations/versions/20261109_01_run_authority.py tests/test_run_restrictions.py
  tests/test_run_authority_database.py`; `.venv/bin/pyright` on those two runtime
  paths; `.venv/bin/pytest -q tests/test_run_restrictions.py
  tests/test_run_authority_database.py tests/test_authorization.py
  tests/test_skill_policy.py tests/test_tenant_policy_api.py`; boundary checker
  from the baseline block below. `git diff --check` passed.
* Prior 035a disposable PostgreSQL 16 proof: migration upgrade → downgrade →
  upgrade passed; authority mutation/provenance mutation, tenant isolation,
  retained forced RLS and refusal to downgrade persisted authority passed.
  That run reported **92 passed** including the DB/RLS tests. The container was
  stopped; DB tests were not rerun in round 2 (migration/DB tests unchanged).
  No shared database upgraded; no credentials inspected.
* Budget override: requested review correction brings runtime/migration/tests
  to **401 lines**, plus this required inventory/evidence. No further runtime
  scope added; admission, dispatch and all action activation remain unimplemented.
  Independent review round 2 subsequently returned APPROVE (parent report);
  these are not epic release proofs.

#### Engineer evidence: 035b admission gate only (2026-11-09)

* Scope: `app/main.py`, new `tests/test_run_restrictions_api.py`, and this evidence.
  This is the explicitly authorized safety gate, **not** completion of the
  original 035b persistence/admission slice. No authority field is enabled.
* Raw-request dependency rejects present `tools`/`invocation_intent`, including
  explicit NULL and `[]`, before model parsing can discard those fields and
  before replay lookup, policy/thread/previous-response reads, secret storage,
  run creation or enqueue. Native admission uses existing Problem 422 handling.
  Both facade routes check top-level and `haas` fields and retain OpenAI error
  envelopes. Nonempty OpenAI function-definition lists retain their existing
  unsupported-parameter 400; native exact-name lists/null/empty receive 422.
  No caller switch or environment flag enables authority.
* New tests cover all three submit routes, explicit fields with/without an
  idempotency key, facade nested options, chaining lookup prevention, zero store/
  publisher calls on denial, legacy omitted-field replay through real admission,
  and existing facade function-definition rejection. Values are not echoed.
* Baseline commands: `.venv/bin/ruff check app/main.py` passed;
  `.venv/bin/pyright app/main.py` reported 0 errors; `.venv/bin/pytest -q
  tests/test_tenant_policy_api.py tests/test_skill_policy.py
  tests/test_authorization.py` reported **66 passed**, plus
  `tests/test_openai_compat.py` **6 passed**. New test path: baseline N/A.
* Final commands: `.venv/bin/ruff check app/main.py
  tests/test_run_restrictions_api.py` passed; `.venv/bin/pyright app/main.py`
  reported 0 errors; `.venv/bin/pytest -q tests/test_run_restrictions_api.py
  tests/test_openai_compat.py tests/test_tenant_policy_api.py
  tests/test_skill_policy.py tests/test_authorization.py` reported **149 passed**
  (72 existing + 77 new). Boundary: **175 errors / 9 warnings → unchanged**.
  `git diff --check` passed. In-memory generated `/v1` OpenAPI equals the existing
  snapshot; no field/schema publication or snapshot/changelog edit in this gate.
* One test-fake fix: initial omitted-field Responses rendering needed an async
  `run_usage` fake; adding it produced the final passing result. No production
  scope expansion. Gate runtime diff: 48 added / 3 removed lines; new tests:
  145 lines, comfortably below 400 including this evidence.
* No migration/DB operation, provider call, worker restart or action execution.
  Independent gate review **APPROVE** reported by parent on 2026-11-09.
  Original create/decode/digest persistence,
  enforcement, inheritance, deterministic intent and approval binding remain
  outstanding; leave rejection in place until their complete reviewed rollout.

No commands run by architect; no pass counts available. Engineer records baseline
**before edits**, with command, exit code and counts; do not fix unrelated failures.

#### 035b-storage bounded implementation plan (2026-11-09)

* Files: `app/core/run_restrictions.py`, `app/core/store.py`,
  `app/core/workflow.py`, new `tests/test_run_authority_storage.py`, and this plan.
  Preserve the pre-existing unrelated store audit helper diff. Do not edit
  `main.py` while its admission gate is under independent review.
* Add an internal optional typed authority argument to `create_run`; serialize
  frozen v1 authority into `authority_json` on INSERT. Require pinned policy,
  registry and submitter for non-NULL authority. Match intent provenance to the
  transaction-resolved canonical tenant UUID, run, submitter, policy, registry
  and admission idempotency key before insertion. Never infer authority from
  messages, checkpoints or mutable configuration.
* Compose internal admission digests with explicit tools (including `[]`) and
  intent tool/canonical args, excluding per-run binding so legitimate replay
  remains possible. Preserve the existing legacy digest when authority is NULL.
* Decode present authority with strict versioned models; malformed or unknown
  representations fail, never downgrade to unrestricted. Keep SQL NULL distinct
  from v1 `tools:null` and `tools:[]`.
* Safety staging: non-NULL authority runs are stored inert (`next_action=none`,
  no orchestrator outbox), and lease acquisition refuses them until dispatch
  enforcement is implemented/reviewed. Reconciler must not activate them.
  This is internal storage proof, not supported public admission.
* Tests: pure request-digest/decode semantics; disposable PG legacy/null/empty/
  intent INSERT and restart decode; checkpoint preservation, immutable provenance,
  cross-tenant access denial, replay/conflict, and zero scheduled restricted work.
  Keep the entire sub-slice <=400 changed lines including tests/evidence.
* Baseline: touched-file ruff passed; pyright 0 errors; pure/authorization/policy/
  phase3 selection **97 passed, 18 skipped** without PG; boundary **175 errors /
  9 warnings**. Record disposable-PG baseline before changing runtime.

#### 035b-storage implementation evidence (2026-11-09)

* Internal `create_run(authority=...)` now inserts frozen authority JSON with
  transaction-resolved tenant provenance checks and authority-aware replay digest.
  NULL remains legacy; v1 null-tools and empty-tools remain distinct. Intent
  request replay excludes server run identity while including exact tool/args.
  Admission principal must match the pinned submitter. No `main.py` changes.
* `decode_run` returns a typed `authority` only from run-owned JSON; missing
  explicit version/tools, unknown versions/fields and malformed data fail closed.
  Restarted store/pool reads recover the same frozen intent/canonical args.
* Non-NULL authority INSERTs use `next_action=none` and create no orchestrator
  outbox. Worker lease acquisition refuses authority rows before inbox insertion.
  Real execution-service reconciliation does not requeue those held rows.
  Public rejection gate remains ON; this hold must not be removed independently
  of full dispatch/approval/inheritance enforcement.
* New tests: **20 passed** including disposable-PG create/restart/cancel,
  checkpoint SQL preservation, immutable provenance, cross-tenant RLS, canonical
  tenant UUID (slug in intent rejected), all intent provenance mismatch fields,
  replay/conflict across new run IDs, no restricted outbox and refused wakeups.
  These do **not** prove approval resume/dispatch lifecycle: that remains held.
* Final command: `HAAS_TEST_POSTGRES_DSN=<disposable-only> .venv/bin/pytest -q
  tests/test_run_authority_storage.py tests/test_run_authority_database.py
  tests/test_run_restrictions.py tests/test_run_restrictions_api.py
  tests/test_openai_compat.py tests/test_tenant_policy_api.py
  tests/test_authorization.py tests/test_skill_policy.py`: **201 passed**.
  Ruff on the three touched runtime modules/new test passed; pyright on the
  runtime modules: **0 errors**. `git diff --check` passed; boundary unchanged
  **175 errors / 9 warnings**.
* Identical PG baseline/final selection (`test_run_authority_database.py`,
  phase2 RLS test, `test_phase3_workflow.py`): **11 passed / 9 failed → unchanged**.
  Existing failures use obsolete executor arguments (`_execute_envelope` and
  `scoped_secrets`); no fixes attempted. Gate/API regressions remain passing.
* Fix hypotheses: explicit stored version/tools were needed because `{}` used
  model defaults; reconciler needed its existing service-role test fixture;
  immutability tests needed a genuine agent-revision change, not NULL→NULL.
  No permission weakening, migration modification, shared-DB apply, provider
  call, live action or public activation. Existing audit helper diff preserved.
  Sub-slice remains below 400 lines; independent storage review **APPROVE**
  reported by parent on 2026-11-09.

#### Next: 035c1 bounded pre-dispatch / grant enforcement plan

Status: in progress after independent 035b-storage APPROVE (parent report).
Public admission rejection, restricted INSERT scheduling hold and lease refusal
remain ON throughout c1. This is the first part of c, not its completion.

* Files: `app/core/run_restrictions.py` (generic denial/helper),
  `app/core/workflow.py` (transactional guard), `app/core/credentials.py`
  (actual grant issuance seam), new `tests/test_run_restrictions_dispatch.py`,
  and this plan. No 036 files, migrations, deployment changes or `main.py` edits.
  Target <=350 changed lines including tests; absolute cap 400.
* Pure guard takes persisted `RunAuthority`, exact tool name and explicit
  policy/unlock inputs; ceiling membership is conjunctive, with no control-tool
  exception. Unknown/malformed authority is a denial. Exact intents remain
  `restriction.intent_unavailable` in this stage even when tool/args match:
  no approval/digest enforcement has been implemented yet.
* Transactional `_dispatch` checks the locked run row before any invocation
  status UPDATE/outbox write. Validate every invocation first so a bad later
  item cannot allow earlier grant work. Approval-resume `_dispatch` uses the
  same check; no new parallel dispatch implementation.
* `credentials.prepare_tool_dispatch` first verifies the signed invocation,
  checks message/envelope tenant/run identity, loads authoritative run state
  under existing service-store access, and rejects absent/foreign/held/revoked
  authority before scoped secret resolution, `seal_grant`, grant-row recording,
  or tool publication. Compare the signed authority representation against
  persisted authority; omission/substitution on a restricted run denies.
  SQL NULL remains the explicit legacy path, not fallback after load failure.
  Use canonical tenant UUID resolution, never a caller's tenant alias assertion.
* Current policy must still allow the registered tool and pinned plugin revision;
  active skills and run skill allowlist must also unlock it. Reuse
  `TenantPolicy.from_record` / `resolve_plugin_access` and registry resolution,
  without adding plugin references. Policy drift is fail-closed, not a way to
  widen a pinned restriction. Check availability/status/deadline/cancellation.
* Tests: pure null/empty/exact-ceiling cases, forged signed/persisted authority
  mismatch, omitted envelope authority, wrong tenant/run, revoked policy and
  staged intent denial; fake adapters assert zero secret-resolution/sealed or
  recorded grants and zero published tool calls. Disposable PG tests exercise
  real `_dispatch` rollback/no tool outbox, using existing service-role access.
  Preserve legacy signed-envelope behavior and compare regression baselines;
  do not repair the nine known phase3 obsolete-executor-call failures.
* Before implementation record ruff/pyright on c1 files, affected pure tests,
  credential publisher regression and boundary counts. Existing unsigned
  publisher unit fixture may need conversion to a real signed envelope plus
  authoritative fake run; report that test-only file expansion before editing.

#### 035c1 implementation evidence (2026-11-09)

* Added generic `RestrictionError`, strict shared `decode_authority` and conjunctive
  ceiling/policy/unlock helper. The helper **always denies non-NULL authority**
  at this staging milestone, including matching exact intents. This is not a
  working restricted dispatcher or intent release.
* `_dispatch` checks persisted authority before any invocation UPDATE/tool outbox;
  empty invocation lists cannot bypass the hold. `decode_run` reuses strict
  decoding without changing legacy NULL semantics. `prepare_tool_dispatch`
  verifies signature/expiry/argument digest, loads the authoritative run, binds
  message/envelope run and tenant aliases from the persisted joined row, compares
  signed/persisted authority, and checks current policy revision/access/unlocks
  for restricted rows before denying. No secret resolution/grants/publication
  can occur for non-NULL authority, including held/cancelled/expired rows.
  Granular lifecycle admission checks are still required before allowing work.
* Reported test-only expansion: `tests/test_phase3_units.py` publisher fixture
  now uses a real signed envelope and an explicit legacy authoritative fake run.
  Malformed/unsigned tool outbox messages now fail before secret work; well-formed
  legacy signed dispatch remains passing. No other runtime file expansion.
* Baseline: ruff passed; pyright 0 errors; pure/authority/API/policy selection
  **187 passed**. Final same selection plus new c1 tests: **207 passed / 1 PG
  skipped**. Disposable PG final selection including storage, authority migration,
  phase2 RLS and c1 real service-role checks: **230 passed**. New c1: **21 tests**.
  Commands: `.venv/bin/ruff check app/core/run_restrictions.py
  app/core/workflow.py app/core/credentials.py tests/test_run_restrictions_dispatch.py
  tests/test_phase3_units.py`; `.venv/bin/pyright` on those three runtime modules;
  `.venv/bin/pytest -q tests/test_run_restrictions_dispatch.py
  tests/test_phase3_units.py tests/test_run_restrictions.py
  tests/test_run_restrictions_api.py tests/test_authorization.py
  tests/test_skill_policy.py tests/test_tenant_policy_api.py`.
  PG selection additionally used `test_run_authority_storage.py`,
  `test_run_authority_database.py` and the phase2 RLS test, with only disposable DSN.
* Migration command pinned to **20261109_01**, not `head`, to avoid applying
  concurrent 036 work even on the disposable DB. No migration edits or shared
  database changes. Ruff/pyright final clean; boundary **175 errors / 9 warnings
  → unchanged**; `git diff --check` passed. No fix attempts needed.
  C1 remains below 400 changed lines including tests/evidence. User reports
  independent review APPROVE (2026-11-09); public gate, scheduling hold and
  lease refusal all remain ON.

#### Existing credential redemption / revocation evidence for c2 design

* Worker path is `_execute_envelope` → `credentials.redeem_grants` →
  `redeem_grant`: Redis `GETDEL` consumes the grant before validation/decryption.
  `seal_grant` writes Redis ciphertext with TTL; redemption checks audience,
  loaded plugin digest, invocation ID and expiry, then AES-GCM integrity.
  It does not query current tenant policy, submitter/approver, run status,
  authority or approval binding. The `run_id` passed to `redeem_grants` is used
  for redaction registration, not a current-policy check.
* Invalid individual grants are logged/skipped; worker can continue with an
  empty/partial secret map. Grant possession or redemption success is therefore
  not action authorization. `_execute_envelope` separately checks a Redis run
  cancellation flag and effect-key deduplication, not live policy revocation.
* `_note` queues redemption records best-effort. Despite the credentials module
  docstring, inspected `AccessStore.maintenance_access` only cleans expired SQL
  grant rows and retires webhook keys; no queue-drain/live authorization proof
  was found there. Do not rely on that comment as an implemented revocation seam.
* `/v1/credentials/grants/{grant_id}/redeem` in `app/api/access.py` is a distinct
  secret-admin SQL route: locks grant row, checks redeemed/expiry and looks up a
  binding. Worker redemption does not call it; it has no current run-authority/
  approval/policy-revision enforcement to reuse as-is.
* No inspected grant path invalidates all outstanding Redis grants synchronously
  on policy revision/revocation. DB/network privileges remain unchanged.
  The user selected the internal authorization RPC seam recorded below and in
  plan 037. No live revocation lookup exists in grant redemption itself.

#### Remaining concrete c/e sequencing and final-worker authorization gate

* **035c2:** use the internal authenticated core authorization RPC before grant
  redemption and again at final handler entry; keep worker schema offering,
  `build_invocation` attenuation, signed authority transport, strict argument
  digest checks, and fail-closed denials. Test fabricated/control calls and
  dynamic activation cannot widen. Full contract and first bounded child are
  in [plan 037](037-live-final-entry-authorization-rpc.md). Do not remove
  scheduling/public holds.
* **035d:** authenticated server-derived child/branch restriction or deny the
  unsupported derived path. Never derive authority from copied messages.
* **035e1/e2:** deterministic no-model intent scheduling, forced existing HITL
  approval, digest-bound immutable arguments and target, distinct authenticated
  approver on both decision routes, exact final adapter payload, stable effect
  key/redelivery/unknown-outcome handling, terminal after one result.
  Scheduling/lease holds can only be replaced inside the reviewed internal
  enforcement path; public rejection remains until f release proof.
* **Infrastructure fact and user-selected seam for c2/e2:**
  `execute_tool_task` documents no PostgreSQL access and
  `deploy/helm/haas/templates/networkpolicies.yaml` gives `tool-worker` Redis
  (plus configured outbound hosts), not PostgreSQL. Local Compose supplies a
  DSN and `_async_execute_tool` currently opens a service store, but that is
  not proof of authorized production DB access. Signed dispatch-time policy
  cannot prove immediate current revocation by itself.
  **User-selected seam (2026-11-09):** an authenticated internal core
  authorization RPC, called before grant redemption and again at final handler
  entry. The user approved required service authentication and narrowly scoped
  worker-to-core network reachability; tool-worker PostgreSQL access remains
  prohibited. Use the existing authenticated worker service identity if one
  exists. Bounded source search did not locate one; if unavailable, stop and ask
  for the specific auth mechanism rather than adding an env var, dependency,
  key or credential-distribution path by inference. The RPC contract, point-in-
  time consistency limit, ordered c2 slices and tests are in
  [037](037-live-final-entry-authorization-rpc.md).
  Authorization checks bind tenant/run/invocation/tool/canonical-argument
  digest/persisted intent binding/pinned policy revision/registry fingerprint/
  plugin digest/envelope expiry. Unavailable, stale, expired or mismatched
  decisions fail closed before grant redemption or final handler entry. This
  is point-in-time authorization, not a global transaction with an external
  effect. Revocation after final-entry allow remains a documented TOCTOU
  window; no "no effects after revocation completes" guarantee is made.
  Homogeneous rollout excludes old workers but does not solve that window.
  Public admission, restricted scheduling and lease holds stay ON until the
  full 035 release proof, independent review and compatible-worker rollout.

```text
.venv/bin/ruff check app/core/run_restrictions.py tests/test_run_restrictions.py
.venv/bin/pyright app/core/run_restrictions.py
.venv/bin/pytest -q tests/test_run_restrictions.py
.venv/bin/pytest -q tests/test_authorization.py tests/test_skill_policy.py tests/test_tenant_policy_api.py
.venv/bin/python .github/skills/core-boundary-check/scripts/check_boundaries.py
```

New paths do not exist at baseline: record N/A for those commands before creation,
then run them after implementation. Rungs: touched-file lint/types and pure tests;
affected authorization/policy regressions; boundary gate. Later slices add real
DB/outbox/grant integration tests, API contracts and SDK tests, not mock-only proof.
For f run `python -m scripts.update_openapi_snapshot` and review generated diff.

## Epic acceptance / release proof

Use fakes for providers until an operator explicitly authorizes a harmless live
action. Each test must assert **zero handler calls and zero redeemed grants** on
pre-dispatch denial, not merely a hidden schema or HTTP error.

* Legacy API/SDK payload unchanged; `tools=[]` persists and blocks every tool.
* Requested target outside tenant policy, cross-tenant run/approval access,
  stale plugin revision, policy revocation and unavailable secrets remain denied.
* Model fabricates another tool, unlocks a skill, branches, delegates or retries:
  ceiling retained or operation denied; no authority derived from conversation.
* Reviewed recipient/body changed in request replay, invocation DB payload,
  approval batch, signed envelope or final adapter payload: no effect.
* Intent path never calls a model/JSON repair; canonical bytes observed at the
  native and endpoint adapter boundary equal the approved object.
* Two competing approvers, expiry, stale batch digest, reject, cancel, restart,
  outbox redelivery and effect-result loss retain existing conflict/failure rules.
  Unknown effect outcome is surfaced, not resubmitted with a new effect key.
* Submitter-only principal denied approval; same principal with both roles still
  denied; separate authorized approver succeeds; wrong tenant denied on both routes.
* Unconfigured approver is unavailable; malformed credential is rejected; no
  fallback. Submitter browser cannot invoke an approver proxy using server-held
  credentials. Approved browser session receives no credential bytes.
* Both consumers use public API/SDK only. No plugin-specific core additions.
  Boundary errors must not increase; independent reviewer returns APPROVE.

## Risks and rollback

* Storage seam or worker rollout support may be absent → explicit stop gates, not
  a guessed no-migration implementation.
* Broad skills may expose other tools → tool ceiling enforced separately everywhere.
* Schema validators can transform args → admission rejects transformations and
  final worker compares the actual validated dispatch object.
* External endpoint authentication/trust may remain incomplete (ADR-17) → prove
  existing endpoint credential/egress support separately; no inference from intent.
* Raw reviewed args may be sensitive → existing approval access controls/retention,
  no argument logging; credentials use existing scoped secret machinery.
* Rollback: disable new admission; resolve/cancel pending work under operator
  authority; drain new workers before reverting. Never replay restricted work
  through an old dispatcher. Extension rollback hides initiators and leaves
  existing research/artifact/approval observations intact.

## Decisions needed

1. Confirm exact-intent contract: A) singleton deterministic invocation, always
   human-approved, full explicit schema-valid args; B) model-mediated matching.
   **Recommend A**; smallest complete prevention of model substitution.
2. Where is the existing separate approver credential, and which independently
   authenticated approver session/operator-only server may use it?
   A) operator supplies approved secret location and access boundary;
   B) leave review/decision disabled and runs pending/expiring.
   **Recommend A only when available, otherwise B.** Do not paste the key, read
   `user.env.haas`, mint credentials, reseed memberships or weaken self-approval.
3. If 035a finds no suitable run metadata field: A) authorize a separately planned
   additive migration; B) stop without enabling actions. **Recommend B until A is
   explicitly approved.** No migration is currently authorized.

## Core-engineer objective

Start **035a only**, after decision 1: inventory the authoritative seams, implement
the pure immutable restriction/intent substrate and its tests within 350 lines,
and hand back exact persistence/dispatch locations and baseline evidence.
Do not expose fields, dispatch an external action, touch extension UI, provision
an approver or claim the epic complete. Subsequent slices require their own bounded
child plans based on this contract and the recorded seam inventory.

## Log

- 2026-11-09 architect: proposed ADR/epic and first executable slice; docs only.
  Generic restriction/separate approval direction supplied as user-approved;
  detailed choices, storage seam and live credential/session evidence remain open.
