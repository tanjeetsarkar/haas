# 039 — Atlas target-result contract and actionable validation errors

- Status: done — parent-reported independent reviewer 806dca02 APPROVE
- Type: slice
- Owner agent: plugin-engineer
- Epic: [038](038-atlas-guided-research-epic.md)
- ADR: none — extension-only contract consumption, no kernel changes

## Goal

Reduce malformed model-owned target references and replace the repeated AJV
union dump with actionable diagnostics without weakening strict validation.
This is the first bounded reliability slice, not an SSE or action activation.

## Boundary classification

| Change | Core / Plugin / Client | Why |
|---|---|---|
| Atlas output prompt, examples and validation diagnostics | Plugin client | Would not exist without Atlas |
| Regression tests for accepted/rejected target references | Plugin tests | Specific to Atlas graph schema |

## Evidence and limits

`parseResearchResult()` currently validates a strict `oneOf` and throws
`ajv.errorsText()` for every failing alternative. The reported failure contains
errors for targets 0 through 5, but the original JSON is unavailable.
Do not claim a specific malformed shape has been reproduced from that message.
Do not add `source_id` to entity/facet targets to satisfy an unrelated union branch.

Allowed exact references:

```json
{"kind":"entity","entity_id":"hospital-1"}
{"kind":"facet","entity_id":"hospital-1","facet_key":"specialists"}
{"kind":"source","source_id":"source-1"}
```

Labels, descriptions, entity categories and action data belong elsewhere, not
inside these reference objects. The source schema already enforces this;
the change is producer guidance and meaningful error reporting, not permissiveness.

## Scope

**Allowed paths:** the following files under
`services/atlas-explorer/client-ts/` and its README.

**Planned files:**
- `src/explorationResearch.ts` — explicit target instructions and a bounded,
  discriminant-aware diagnostic formatter/error type; retain JSON parse errors.
- `src/researchResultExample.ts` — validated reference examples, retaining the
  complete source-backed synthetic fixture.
- `src/index.ts` — export a diagnostic type only if required by public consumers.
- `tests/explorationPrompt.test.ts` — prompt and strict target-shape regressions.
- `tests/explorationClient.test.ts` — invalid completed output, parent preservation
  and execution-context diagnostics.
- `README.md` — document the target shapes and schema-error behavior.

Read extension instructions before implementation. Keep the slice within
approximately 400 changed lines. If UI-specific error rendering is needed,
plan a separate bounded follow-up rather than silently widening this writer's scope.

**Out of scope:** permissive schema changes, stripping extra properties,
coercing IDs/kinds, automatic repair/model reruns, SSE, storage, phase activation,
external action output, dependency additions, core, migrations and governance.

## Acceptance criteria

- [ ] Keep schema `additionalProperties: false`, exact kinds, required fields and
  semantic reference/provenance validation. No `removeAdditional` or coercion.
- [ ] Prompt spells out all three exact shapes and separates reference kind from
  entity kind such as category/person/organization. It still asks for one level.
- [ ] Valid entity, facet and source references pass; mixed references, unknown
  kinds, extra title/label, missing IDs/facet keys and dangling references fail
  at the appropriate structural or semantic validation layer.
- [ ] Add a synthetic six-invalid-target regression reflecting the reported
  failure pattern, clearly labeled as synthetic, not recovered production data.
- [ ] Diagnostics identify target index, received kind and required/extra fields
  for the selected union branch. Unknown kinds list allowed kinds. Non-target
  errors remain visible; cap repeated messages and report omitted-error count.
- [ ] Error summaries never interpolate arbitrary full model payloads. Preserve
  structured error details for authorized inspection; correlate run/thread/
  request IDs at the client boundary without embedding credentials or prompts.
- [ ] Invalid final output is not projected, saved as a successful graph or
  silently cleaned. Existing successful-tool fallback remains restricted to
  complete successful outputs, not previews; provenance checks stay unchanged.
- [ ] Failed child research preserves its parent and siblings; malformed output
  causes no automatic paid retry or stronger credential use.
- [ ] Existing valid broad-category fixture and source-pair tests still pass;
  inputs are not mutated by validation.
- [ ] Boundary count does not increase; independent reviewer APPROVE.

## Verification rungs

Ladder 3 (targeted tests) and service-local TypeScript builds/types, then 5:

```sh
cd services/atlas-explorer/client-ts
npm test -- tests/explorationPrompt.test.ts tests/explorationClient.test.ts tests/explorationValidation.test.ts
npm run typecheck
npm run build
# From repository root:
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py | tail -1
```

Run these same commands before the first implementation edit and afterward.
Use adjacent existing test helpers. No Python runtime, DB, live/provider or
deployment checks are needed for this slice. No paid reproduction is authorized.

## Baseline

Planning only: boundary scan on 2026-11-10 reported **175 errors / 9 warnings**.
Targeted tests/types/build were not run because no runtime files were changed.
Implementation must capture its actual baseline; do not copy earlier plan
test totals as current evidence.

## Risks and rollback

- Better prompting does not guarantee model compliance. Preserve fail-closed
  validation; do not promise the incident is solved without its payload.
- Diagnostic grouping could hide unrelated failures: test mixed errors and
  retained structured AJV details alongside the concise message.
- Rollback: restore prior prompt/diagnostic presentation only; never weaken
  schema/provenance checks or delete existing saved artifacts.

## Decisions needed

None to implement this bounded slice after plan approval. If available, inspect
the failing run's authorized final JSON before adding an incident-specific
fixture; do not request credentials or private payloads in chat.

## Log

- 2026-11-10 architect: proposed; no implementation, tests or live reproduction claimed.
- 2026-11-10 integration update: parent reports independent reviewer 806dca02
  APPROVE; targeted tests **132 baseline → 139 passed**, types/build passed,
  boundaries **175 errors / 9 warnings → unchanged**, diff check passed.
  Marked done on that reported evidence; architect did not rerun commands.
  No live incident reproduction or recovery of the original failing payload
  is claimed. Other epic slices and runtime release gates remain incomplete.
