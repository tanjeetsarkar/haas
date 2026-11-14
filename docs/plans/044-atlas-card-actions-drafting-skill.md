# 044 — Mandatory card actions and Atlas action-drafting skill

- Status: in-progress — 044a/044b/044d approved; 044c model drafting BLOCKED; external execution disabled
- Type: epic — ordered slices ≤400 changed lines each
- Owner agent: plugin-engineer
- Epic: [038](038-atlas-guided-research-epic.md)
- ADR: none — extension-specific proposals and UI, existing public core authority

## Goal / boundary

Every card offers contextual safe actions and Custom action. Atlas drafts and
stores intent; core alone authorizes tool invocation and approval. Domain
actions, prompts, mappings and skill disappear without Atlas, so remain under
its plugin pack/service. No generated tools or plugin-specific kernel fields.

## Scope / slices

| Slice | Allowed/planned paths | Outcome / dependency |
|---|---|---|
| 044a | `services/atlas-explorer/client-ts/src/` new action contracts/catalog/tests/exports | strict proposal schemas and deterministic per-card safe actions; 039 |
| 044b | `plugins/atlas-explorer/plugin.json`, `skills/<action-drafting>/SKILL.md`, reference/schema examples, adjacent plugin tests | discoverable drafting skill; inspect existing manifest/name/readiness first |
| 044c | Atlas service client action mapping/tests, no core files | explicit user-requested drafting run, strict registered-capability resolution, durable draft lifecycle; 042, 044a/b |
| 044d | Atlas web card Actions/Custom editor/drawer/tests/styles | mandatory affordances, local actions and disabled external state; 043, 044c |

### Integration evidence

- Final 2026-11-10 parent sync: **044c AI drafting remains BLOCKED** on the
  missing server-enforced tool-free contract; external execution and phases
  remain disabled. Independent final plugin checks: **24 tests**, doctor
  **0 errors / 0 warnings**. Subsequent 043d final review **48ba90 APPROVE**,
  **229 UI tests/types/build passed**, supersedes earlier pending drawer review,
  but does not establish model drafting or live research acceptance.
  No paid root/child/research journeys or real-run SSE network checks exercised.
  Architect did not rerun checks; the 044 epic remains in-progress.

- Latest 2026-11-10 parent report: **044d APPROVE 75c412**, **222 UI tests
  passed**, followed by **228 UI tests** at the approved durable cutover.
  These counts precede the current unfinished drawer change; they do not
  establish final browser/drawer/history acceptance.
- **044c BLOCKED, agent 6df13**: no files changed and no model dispatch.
  Public tool-free enforcement is unavailable: `max_tool_invocations` must
  be at least 1, and `activate_skill` can unlock tools. Do not equate omission
  of a tool list or a drafting prompt with server-enforced tool-free execution.
  No workaround or model-dispatch activation is authorized by this update.
  Model-backed drafting remains unavailable; approved local actions/UI and
  saved custom drafts do not imply this capability has shipped. A missing
  generic enforcement capability requires a separately framed core ADR/slice,
  not an Atlas special case. External execution and phases stay disabled.

- Earlier 2026-11-10 parent report: **044a complete**, final reviewer 53d053
  APPROVE, **21 tests passed**. **044b complete**, independently approved,
  **24 plugin tests passed** (doctor previously reported 0 errors / 0 warnings).
  044c model drafting is still pending; 044d UI engineer ecd762 is working.
  The 044 epic remains in-progress. No migration or activation occurred.
  Architect did not rerun these checks.

- 2026-11-10 parent report: **044b complete**, independently APPROVE after the
  null-only capability fix; **24 targeted tests passed**, plugin doctor
  **0 errors / 0 warnings**. Architect did not rerun these checks.
- At that earlier checkpoint, 044 epic and 044a remained in-progress. Completion of the drafting
  skill does not claim completion of action contracts, UI or external execution.
  No runtime migration or activation is recorded or authorized by this status
  update; existing release gates remain unchanged.

Manifest pack path/name and existing skill layout must be confirmed before edits;
if actual Atlas pack location differs, update this plan rather than create a
second pack. No governance skill, dependency, DB migration or external adapter.
Do not combine all rows; preserve a releasable disabled external lane throughout.

## Proposal and intent contracts

- Every card type (entity/category/facet/source/candidate/unavailable) exposes
  Actions and Custom action; include at least Inspect sources or Export sourced
  summary when grounded data exists. Otherwise Inspect missing evidence is a
  truthful no-network local action. Never invent appointment/retailer capability.
- Derive suggestions from card kind and a static trusted capability catalog,
  not a paid model call on render/hover. Bind saved proposals to card ID,
  workspace/branch revision, proposal version and evidence refs.
- Local inspect/export is deterministic, escaped and downloadable only after
  explicit click. Export includes provenance and missing-data labels; never
  issues arbitrary network requests, executes code or implies external success.
- Custom action first saves user text and explicitly selected context. Running
  the drafting model is an explicit interaction, not card selection or reload.
  Research graph schema remains separate; it cannot encode execution authority.
- Draft output: strict versioned object with `proposal_id`, card/revision
  references, `intent_text`, optional known `capability_id`/schema version,
  structured proposed arguments, evidence refs, missing fields and warnings.
  Unknown capability remains a draft with reason. Reject extra properties,
  executable code, invented tool IDs and arbitrary HTTP destinations.
- The trusted Atlas catalog maps known capabilities to actual registered
  tool/schema versions, credential requirements and destination constraints.
  Model output never supplies trusted mappings, tool allowlists or approval.
  Validate arguments against registered strict schemas and current policy.
- Drafting skill's SOP: use selected public card/ancestry and chosen user inputs;
  mark uncertainty, ask for missing destination/details, never infer private
  patient information or claim availability; propose, do not execute. Give it
  only required drafting/research tools after manifest/readiness inspection,
  no blanket tools/secrets and no external action tool just for drafting.

## State and disabled execution

Persist `proposed → draft → reviewed → awaiting_approval → executing →
succeeded/failed/unknown/cancelled` with explicit event validation. While release
is blocked, permitted path stops at reviewed draft: display **External execution
unavailable — authorization release gates pending**. No real approval request,
external intent submission or simulated success from a draft. Local export/
inspection use a separately labeled local completion, never external receipts.

Future execution binds exact destination, canonical arguments, capability/tool/
schema/credential identity and digest under existing 035 public contracts.
Every edit invalidates review/approval; separate authorized approver must differ
from submitter and verify the same tenant using public identity. UI confirmation,
plan approval and model suggestion are never invocation approval. Unknown effect
never automatically resends. Do not implement this future lane under 044a–d.

## Release blocker (mandatory)

Parent runtime-readiness report on 2026-11-10: both phased research and external
execution are **NO-GO**. Public raw-presence holds remain; 037 is deny-only;
test/review coverage and release TLS proof are incomplete. Source plan 035
records approved sub-slices, not an end-to-end release approval.
SSE/one-level research does not depend on either feature; do not enable phases
as an action/UI workaround.

No external release slice is authorized to remove holds here. Before any
subsequent separately framed consumer activation, require a linked verifier/
reviewer evidence matrix showing:
1. Reviewed public admission/SDKs, applied reviewed **local-only** migration,
   homogeneous local API/worker versions and approved hold removal.
2. Exact-intent binding/immutable authority, strict tools/arguments/targets,
   current policy, separate-principal approval and stale/edit/revocation denials.
3. Live pre-grant/final-entry authorization, real temporary-key TLS handshake,
   denial on TLS/auth/outage/replay failure; endpoint no longer deny-only under
   reviewed controlled configuration, not patched by Atlas.
4. Native/HTTP/MCP/sandbox entry guards, secret scoping, SSRF/destination policy,
   effect dedupe/redelivery, terminal unknown and no automatic resend.
5. Same-tenant success and cross-tenant/wrong-principal denial, cancellation/
   restart/approval races, isolated tests, independent APPROVE and specifically
   authorized non-destructive local integration evidence.

Missing any row = blocker, not implicit user permission to weaken safety.
No provider-effect experiment is necessary for drafting/local actions.

## Acceptance / verification

- [ ] Fixture matrix for every card kind proves Actions + Custom and a truthful
  safe contextual choice; keyboard/action click does not trigger row research.
- [ ] Skill is a plugin-pack skill, discoverable and ready with least privilege;
  strict draft examples parse; invalid capabilities/arguments fail closed.
- [ ] Editing revision invalidates review; save/reload preserves drafts without
  model or external requests. Malicious text is inert; secrets never persisted.
- [ ] Browser/tests assert disabled explanation and **zero external submissions**
  through card clicks, model suggestions, replay and reload.
- [ ] All targeted gates pass vs fresh baseline; independent APPROVE per row.

Rungs 3/5; inspect actual pack ID before selecting plugin doctor:
```sh
cd services/atlas-explorer/client-ts
npm test && npm run typecheck && npm run build
cd ../web
npm test && npm run typecheck && npm run build
cd ../../..
.venv/bin/pytest -q services/atlas-explorer/tests
.venv/bin/haas-plugin --help
# Run list/inspect/doctor with actual CLI syntax and confirmed Atlas pack ID.
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py
git diff --check
```

Baseline: not executed by architect; historical boundaries 175/9, not a fresh
result. Capture current plugin readiness and tests before implementation.
Rollback local action UI/skill activation without deleting saved drafts or
removing existing user-visible features. External holds remain on.

## Decisions needed

None for approved drafting/local actions. External activation is blocked on
evidence, not a product choice to silently make. Any destructive migration,
new core dependency or safety weakening requires a separate hard stop.
