# 007 — Audit the Atlas service invocation trust contract

- Status: done — bounded source/test audit complete; final reviewer approved
- Type: slice
- Owner agent: architect (read-only contract audit); verifier supplies baseline/evidence
- Epic: [001](001-atlas-extraction-sdk-epic.md)
- ADR: [ADR-17](../adr/0017-trusted-plugin-service-invocation.md) — proposed, not implementation authorization

## Goal

Determine whether plugins can call an independent service through existing public
contracts with authenticated, non-forgeable tenant/run scope, scoped credentials,
safe egress and approval binding. This is the next smallest valuable extraction
step after completed 006: an evidence-backed prerequisite verdict, not an Atlas
cache schema, service route, adapter or runtime change.

Initial source evidence disproves sufficiency of the stock HTTP/MCP endpoint
wire shape for trusted service scope. Complete the bounded audit of alternate
public paths and actual egress wiring before choosing the consuming slice.
Missing proof is a blocked requirement, never an implicit permission.

## Boundary classification

| Item | Ownership | Boundary test |
|---|---|---|
| Atlas extraction sequencing and stable tool compatibility | Plugin/service planning | Would not exist without Atlas |
| Trusted service invocation contract and enforcement | Generic core design only | Exists without Atlas; plugins can use it, including web-research and incident-triage |
| Source/test audit and contract matrix | Architect/verifier | No runtime ownership transfer or implementation |
| Domain cache, TTL, validation, schema, UI and client relocation | Plugin/service, deferred | Not part of this slice |

## Scope and budget

**Writable paths for this slice:** this plan, `docs/plans/README.md`,
`docs/plans/001-atlas-extraction-sdk-epic.md`, ADR-17 and the ADR index.
Record the final audit matrix in this plan. ADR-17 remains proposed and is not
approved for implementation by this audit.
Maximum 400 non-generated changed lines; zero migrations.

**Read-only audit paths:** the exact paths below and narrowly discovered existing
tests of those paths. Maximum approximately 25 reads/searches per audit pass.
If evidence exceeds that budget, record the missing evidence and stop; do not
expand this into a full security review.

**Excluded:** all code/tests/config edits, HTTP probes carrying real credentials,
new dependencies, service storage/behavior, DB setup/migrations/data movement,
core cleanup, UI removal, SDK/API breaking changes and full repository test repair.
Preserve every dirty file. Do not inspect, hash, diff, format, stage or otherwise
touch `haas_sdk_ts/src/lib/atlasClient.ts`. Do not capture broad content diffs
that include it. The user's continuation of the overall Atlas extraction
authorizes proceeding with this bounded, read-only prerequisite audit only. It
does not authorize runtime changes, ADR-17 implementation, or any other excluded
action. Any generic core capability still requires a separately approved full
implementation plan before work begins.

**Planned document changes:**

- This plan — evidence matrix, negative-case checklist, final prerequisite verdict.
- Epic and plan index — insert 007 before behavior work; conditionally order a
  separately approved generic core slice before any service consumer.

## Inspected evidence (2026-11-08; source, not executed verification)

| Requirement | Exact source evidence | Initial verdict / remaining proof |
|---|---|---|
| Standalone readiness | `services/atlas-explorer/app/main.py`, `docker-compose.yml`, `README.md`, `tests/test_health.py`: health-only; exact health response, absent business/docs routes, isolated service package | No service authentication or business contract exists; health is not trust/readiness evidence |
| Pack definition | `plugins/atlas-explorer/plugin.json` provides `atlas_cache_lookup`, `atlas_cache_upsert`, `atlas_emit_layer`, `atlas_dispatch_inquiry`; tools currently native. `skills/atlas-explorer/SKILL.md` declares no allowed secrets | Endpoint credentials cannot be assumed configured; preserve all four persisted names |
| Current tool boundary | `plugins/atlas-explorer/tools/atlas_tools.py`: legacy `app.core.context` imports, tenant-keyed raw store cache operations, inquiry approval flag and runtime DNS validation/redirect disabling | Neither legacy store access nor importing a core validator is an acceptable extracted public adapter |
| Public context | `haas_plugin_sdk/__init__.py:49–62`: identity fields, trace/effect key, `http_client: Any`, `get_secret`; no service-call helper, outbound validator or approval assertion | Local identity fields are not remotely authenticated claims; no public safe-egress helper is established |
| Stock HTTP/MCP | `app/core/plugins.py:302–412`: registration/runtime URL validation; tool args form body; tracing/effect headers; optional secret authentication and HMAC of args; MCP metadata is protocol/client/trace data; 20s timeout, redirects disabled | No kernel-derived authenticated tenant/run/plugin/approval scope is sent. HMAC of model args does not prove their tenant/run authority |
| Worker identity | `app/worker/tasks.py:622–745`: verifies signed envelope; derives identity from body; redeems invocation grants and passes scoped secrets/context | Trusted inside worker is not automatically trusted at remote service. Inspect envelope minting/argument binding end to end |
| Secret grants | `app/core/credentials.py`: single-use sealed grants, tool audience/plugin digest/invocation binding, run matching and TTL; `prepare_tool_dispatch` | Broker audience is a tool audience, not evidence of a service-verifiable delegation credential; verify tenant/name narrowing and failure behavior |
| SSRF exception | `app/core/context.py:71–124`, `app/core/config.py:141,279–282`: exact lowercased configured host returns before private-address/DNS checks. Core `docker-compose.yml:148–150` sets worker `SSRF_ALLOWED_HOSTS` default `searxng` | Explicit private-host exception exists, not a positive destination allowlist for all traffic. No service default may be hardcoded into core |
| Egress broker | `app/core/egress.py`: `check_destination`, `PinnedTarget`, `egress_request`, policy and meters; `credentials.py:231–289` seals/opens policy | Presence is not enforcement proof. Inspect actual client/transport wiring in native, endpoint and sandbox paths |
| Approval | `app/core/workflow.py`: batch digest, approved dispatch and invocation batch records. `tests/plugins/test_atlas_explorer.py:226–237` checks approval registration | A flag test does not prove remote non-bypassability or forwarding of exact approved arguments |

Source comments in worker/context/SDK describe stronger isolation than is proved
by them: worker execution also initializes a store and creates an HTTP client.
Use executable wiring, not comments or target design, as authority. Existing
`docs/plugin_architecture.md`, `docs/design/architecture.md`,
`docs/design/threat-model.md` and ADR-14 distinguish production identity from
development header tenancy; do not invent trust from `X-Tenant-ID`.

## Bounded audit sequence

1. Verifier records status metadata only (no protected TS content), fresh boundary
   counts and the relevant existing-test results. Retain dirty work without resets,
   staging or broad diff collection. Missing commands/environments remain explicit.
2. Trace run admission/authenticated tenant and encrypted secret storage through
   `app/main.py`, `app/core/store.py:get_scoped_secrets`,
   `app/core/workflow.py` invocation creation/approval/approved dispatch,
   `app/core/envelope.py`, `app/core/credentials.py:prepare_tool_dispatch`
   and `app/worker/tasks.py` publisher/execution. Record source anchors for each
   trust handoff; never record secret values.
3. Compare native and stock endpoint paths with sandbox propagation in
   `app/sandbox/execution.py`, `runner.py`, `launcher.py`. Trace HTTP client
   construction, egress policy activation, broker/transport use and imports.
   Establish whether a plugin using only `haas_plugin_sdk` can perform validated
   outbound HTTP without privileged imports or an unguarded client.
4. Inspect `app/core/context.py`, `config.py`, `egress.py`,
   `credentials.py:open_egress_policy` and worker config in `docker-compose.yml`.
   Separate host exception, tenant allow policy, DNS pinning, redirect handling,
   timeout and byte limits. Record API/discovery versus worker/sandbox config
   requirements; draft example configuration only in docs, using fake host names.
5. Inspect existing tests listed below. Record what they prove and what they do
   not, rather than writing tests. For every negative case below, cite an existing
   assertion or name the missing test and its future owning slice.
6. Finish a verdict with two separate columns: **implemented** and **proven**.
   If an alternate existing public contract satisfies all requirements, cite
   wire bytes/claims and verifier evidence and revise/supersede proposed ADR-17.
   Otherwise retain the gap verdict and propose 008 as generic core contract/
   invocation work first, with full plan and user approval before implementation.
   Split 008 if it cannot meet 400 lines; keep inquiry remote side effects deferred.

## Acceptance criteria

- [x] Final matrix in this file answers the prerequisite yes/no/blocked with
  source anchors, available test results/IDs, and explicit unknowns for each
  row. The verifier handoff omitted exact IDs for several passing selectors;
  that limitation is recorded, not filled by inference. “Yes” requires *every*
  trust/egress requirement proved, not merely a reachable URL.
- [x] Scope provenance is traced from authenticated run tenant to worker to
  service-verifiable identity. Model args, caller headers, tracing, effect keys,
  and a caller-supplied shared secret are explicitly rejected as independent
  proof of tenant/run authorization.
- [x] Credential analysis covers tenant A/B, plugin X/Y, wrong service audience,
  wrong run/invocation, missing/expired/redeemed grants, undeclared names,
  redaction and no credential logging. Tool credentials are distinguished from
  kernel-issued service delegation.
- [x] Egress analysis covers non-HTTP(S), literal private/IPv6/mapped IPs, private
  DNS and mixed DNS answers, rebinding/check-connect race, userinfo, wrong host/
  port, redirects, missing policy and explicit private-service exception.
  It states whether each guard actually runs on HTTP, MCP and native SDK calls.
- [x] Approval analysis covers unapproved direct service requests, mutated args/
  destination, expired or wrong-run approval, replay and duplicate effect keys.
  Scope authentication and idempotency are not equated with approval.
- [x] Service `/healthz` remains unauthenticated liveness only; no business route,
  DB schema/cache contract or runtime feature is designed/implemented here.
- [x] Gap result links proposed ADR-17 and conditional next core slice; no
  consuming plugin slice starts until capability and trust tests are accepted.
- [x] Boundary target is unchanged: overall 155/9, Atlas-filtered 0/0,
  atlas-explorer 134/1; fresh differing baseline is reconciled, not repaired.
- [x] Only the allowed documentation paths were edited; no runtime, test or
  config edits, DB setup, real-credential HTTP probes, or protected-file reads
  were performed.
- [x] Post-edit `git diff --check -- docs/plans docs/adr` passed (reported by
  user in the follow-up).
- [x] Final reviewer approved 007. This approves the bounded audit and its
  evidence record only; it does not accept ADR-17 or authorize implementation.

## Verification rungs and baseline

Verifier runs existing selectors only; collect IDs before using broader files:

```text
git status --short
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py --plugin atlas-explorer
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py
.venv/bin/pytest tests/test_core.py -q -k 'ssrf or secret_scoping or mcp or manifest_blocks_internal'
.venv/bin/pytest tests/plugins/test_atlas_explorer.py -q
.venv/bin/pytest tests/test_phase3_units.py -q
.venv/bin/pytest tests/test_phase3_workflow.py --collect-only -q
git diff --check -- docs/plans docs/adr
```

Inspect existing credential/egress/envelope/identity tests discovered by references
to the exact audited symbols. Record their precise IDs, prerequisites and targeted
commands before execution. Approval DB-backed tests may be blocked by environment;
no DB provisioning authorized here. Run service health tests from its directory
using the existing isolated environment/README command, never root `app` imports.
No Ruff/Pyright or SDK rerun is required for documents alone; mark not applicable.
No full repository pytest or TS content inspection.

Verifier-reported results for this bounded audit (not rerun by architect):

```text
Overall boundaries: 155 errors / 9 warnings.
Atlas filter: 0 / 0; atlas-explorer: 134 / 1.
Targeted core selector: 16 passed; Atlas plugin: 18 passed.
Phase-3 units: 13 passed; identity/approval: 5 passed.
Phase-3 workflow: 18 collected; DB-backed identity tests not run.
Service tests unavailable: services/atlas-explorer/.venv missing.
No direct test IDs found for prepare_tool_dispatch or actual egress broker wiring.
Post-edit `git diff --check -- docs/plans docs/adr`: passed (user-reported).
Exact IDs/counts for handoff-listed credential/context, SSRF, envelope and
identity selectors were not preserved in the available verifier report.
Service tests were environment-blocked by the missing isolated venv; DB-backed
identity tests were not run. These are missing proof, not passing tests.
Unrelated preexisting core failures: not characterized or repaired in this slice.
```

The verifier-reported 007 baseline is overall 155 errors / 9 warnings, Atlas
0/0, and Atlas Explorer 134/1. Slice 006's FIFO scenario semantics work is done.
This plan records the completed bounded audit and explicitly distinguishes
source implementation from evidence actually proven by tests. The final reviewer
approved 007; its status is done. Reported limits remain material: service tests
were unavailable because `services/atlas-explorer/.venv` is missing, DB-backed
identity tests were not run, and the phase-3 workflow suite was collected but
not executed. The audit verdict remains **NO**; these limits neither reverse that
verdict nor count as passing evidence. ADR-17 remains proposed, and a separate
user decision/approval is required before any core implementation.

## Final bounded audit evidence (2026-11-08)

**Verdict: NO.** The existing public plugin paths do not prove every required
trust, credential, egress and approval property. A few components are
implemented, but component presence or propagation is not end-to-end proof.
The gap is sufficient to block starting a consuming Atlas service slice. This
is a bounded source/test audit, not a penetration test or a claim that the
identified components are exploitable.

| Requirement | Implemented (source anchors) | Proven / test evidence | Audit result and missing proof |
|---|---|---|---|
| Authenticated run admission and tenant provenance | `app/main.py:338` obtains authenticated identity; `app/main.py:1032-1052` checks owned run and passes request secrets on resume. `app/core/store.py` contains identity/membership resolution and scoped-secret paths (the bounded search did not capture the `get_scoped_secrets` definition line). | Identity selector reported 5 passed; DB-backed identity tests were not run. | Tenant selection is enforced at the API boundary, but no service wire assertion binds the authenticated tenant to a remote service-verifiable claim. `X-Tenant-ID`/caller data is not independent proof. Exact assertion IDs were not present in the verifier handoff. |
| Approval and dispatch identity | `app/core/workflow.py:140` computes the batch digest; approval/dispatch persistence is in `app/core/workflow.py` (including approval query/decision paths around `1539-1557`). `app/core/envelope.py:88-114` signs/verifies canonical envelope bodies and argument digests. | Phase-3 workflow collected 18; DB-backed identity/approval tests were not run. `tests/test_phase3_workflow.py::test_approval_digest_binding_idempotency_and_resume` was found, but collection is not execution evidence. | Existing approval and signed dispatch are core-local; no evidence that an external service receives or verifies exact approved arguments/destination. Direct prepare-to-dispatch path test IDs were not found. Replay/mutation at a service boundary remains unproven. |
| Credential grant scope and service audience | `app/core/credentials.py:141-181` validates sealed grant records; `:291-336` prepares tool dispatch grants. The code binds tool audience, plugin digest, invocation and TTL. | Credential/context selectors were reported passing, without exact IDs/counts in the handoff. Phase-3 units: 13 passed. | Grants are tool/invocation scoped, not demonstrated as service-verifiable delegations. No proven service-audience binding, tenant A/B plus plugin X/Y/name narrowing matrix, or remote wrong-run/invocation/replay rejection. Missing/expired/redeemed and undeclared-name negative-case coverage cannot be attributed from the available test IDs. |
| Worker trust handoff | `app/worker/tasks.py:622-745` verifies the envelope and reconstructs invocation context; `:826-848` binds scoped context and active egress policy before handler execution. `app/core/envelope.py:92-114` contains signing and verification. | Envelope selector reported passed; phase-3 units: 13 passed. Exact envelope test IDs were not included in the verifier handoff. | This establishes a worker-side signed boundary, not a remote service identity. No proof that services receive worker envelope keys (they must not) or a separate audience-bound assertion. |
| Public SDK and stock HTTP/MCP endpoints | `haas_plugin_sdk/__init__.py:49-62` exposes identity fields, an untyped HTTP client and `get_secret`, not a safe service-call helper. `app/core/plugins.py:302-412` validates endpoint registration and constructs HTTP/MCP calls; `:402-403` uses a 20s timeout and disables redirects. | Atlas plugin selector: 18 passed; `tests/plugins/test_atlas_explorer.py::TestAtlasExplorerTools::test_dispatch_inquiry_requires_approval` checks registration metadata. | Endpoint arguments, trace/effect metadata and optional configured secret/HMAC do not prove kernel-derived tenant/run/plugin/approval authority. Metadata/registration checks do not prove runtime scope or broker use. |
| Actual egress enforcement by path | `app/core/egress.py:215-263` checks host/IP and returns a pinned target; `:346-437` implements mediated requests, redirect revalidation and byte metering. `app/worker/tasks.py:836-839` binds policy/meter contextvars. `app/sandbox/execution.py:63-110` passes an egress policy into sandbox execution. Native tool context also has `httpx.AsyncClient` (`app/core/context.py:36`). | Core selector (`ssrf or secret_scoping or mcp or manifest_blocks_internal`): 16 passed; SSRF/context selectors were reported passing, but individual IDs were not supplied. | Actual guarded use by arbitrary native plugin clients is not proven: policy binding does not force callers to use `egress_request`; a direct `httpx` client remains exposed. Endpoint construction is a separate HTTP client path (`app/core/plugins.py:402-403`) and no broker enforcement was found there. Sandbox policy propagation is implemented, but actual runner/transport enforcement is unproven by the available source/test evidence. Do not infer enforcement from egress module comments. |
| Destination and request limits | `app/core/context.py:71-124` validates outbound URLs; configured host exceptions are in `app/core/config.py:141,279-282`. Egress policy checks and pinned-target construction are at `app/core/egress.py:215-263`; request redirects/timeout/byte accounting are at `:346-437`. | Core selector: 16 passed; exact tests for mapped IPv6, mixed DNS answers, rebinding/connect race, userinfo, wrong port and byte-cap enforcement were not identified. | Guards exist in selected code, but no proof they run on every HTTP, MCP, native SDK or sandbox route. Private-host exceptions are not a general allowlist. Redirect and timeout controls in the endpoint client do not establish DNS pinning, response-byte caps or policy binding. |
| Service readiness and plugin contract | `services/atlas-explorer/app/main.py`, its compose/README and `tests/test_health.py` describe a health-only service. `plugins/atlas-explorer/plugin.json` retains four existing tool names; the skill declares no allowed secrets. | Atlas Explorer suite: 18 passed; service tests unavailable because `services/atlas-explorer/.venv` is missing. | `/healthz` is liveness only. No authenticated business route or service trust contract exists. No service behavior/schema is designed here. |
| Redaction, credential leakage and duplicate effects | Tool grants and effect identity are represented in `app/core/credentials.py` and `app/core/envelope.py`; worker context is assembled at `app/worker/tasks.py:826-848`. | No direct test IDs/results for `prepare_tool_dispatch` or actual egress broker wiring were found. | No service-facing proof of redaction/no credential logging, destination idempotency, exact effect/approval binding or safe duplicate handling was established. |

### Verifier evidence and limitations

Reported baseline/results: boundary checks Atlas **0/0**, Atlas Explorer
**134/1**, overall **155/9**; targeted core selector **16 passed**; Atlas
plugin **18 passed**; phase-3 units **13 passed**; identity/approval **5 passed**;
phase-3 workflow **18 collected**. Credential/context, SSRF, envelope and
identity selectors listed in the verifier handoff were reported passing, but
the handoff did not preserve their exact selector strings, individual test IDs
or per-selector counts. They therefore do not close any negative-case gap above.
`tests/test_phase3_workflow.py::test_approval_digest_binding_idempotency_and_resume`
was identified by source search but the workflow suite was collection-only.
Service tests were unavailable (missing `services/atlas-explorer/.venv`); DB-backed
identity tests were not run. No direct test IDs were found for
`prepare_tool_dispatch` or actual egress broker wiring. Documentation
diff-check was reported clean. Ruff/Pyright and unrelated SDK suites were not
applicable to this documentation-only audit. No DB provisioning, real-credential
HTTP probe, or protected TypeScript file inspection was done.

The verifier-reported boundary baseline is unchanged: Atlas 0/0, Atlas Explorer
134/1, overall 155/9. Boundary counts are not evidence of trust-path correctness.

## Unknowns, risks and rollback

- No alternate public path was demonstrated to satisfy the complete contract.
  Exact egress transports, signature coverage/key provisioning, policy defaults,
  run admission secret narrowing and service-side approval/replay checks remain
  unproven.
- Global host exceptions may bypass private-IP protections; do not broaden them,
  infer host/port restriction, or claim DNS validation alone prevents rebinding.
- Secret-based body signing authenticates possession, not trusted run identity.
  Never give a service core envelope signing keys or raw core DB access.
- ADR-17 is proposed; signer format/key management and proof-bearing approval
  delivery need review, not invented implementation.
- Rollback: withdraw/supersede these document changes only, preserving prior dirty
  content. There is no runtime/data rollback because no runtime/data change occurs.

## Decisions needed

1. Whether to approve the separately proposed generic core capability in
   [008](008-generic-plugin-service-invocation.md) for implementation after its
   listed security decisions are resolved: A) approve the plan and resolve its
   gates; B) investigate an independently authenticated existing public API
   path first; C) defer service extraction. **Recommend A**, retaining every
   security gate in 008. Reviewer approval of 007 and creation of 008 are not
   implementation authorization.

## Log

- 2026-11-08 user: continued the overall Atlas extraction, authorizing only this
  bounded read-only prerequisite audit. No runtime edits or ADR-17 implementation
  are authorized; any generic core implementation needs a separately approved
  full plan. Latest reported boundary baseline is 155/9; 006 FIFO slice is done.
- 2026-11-08 architect: completed the bounded source/test audit within the
  approximately 25-read/search budget. No public path proves every requirement;
  ADR-17 remains proposed. A separate generic core plan is conditional on user
  decision/approval; no consuming service slice or runtime implementation is
  authorized by this result.
- 2026-11-08 user: reported final reviewer approval of 007 and requested
  documentation-only follow-through. Status set to done. Audit baseline and
  unavailable/unrun test limitations retained; no runtime changes or
  implementation authorization implied.
