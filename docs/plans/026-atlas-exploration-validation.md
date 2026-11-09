# 026 — Atlas exploration runtime validation

- Status: done
- Type: slice (B)
- Owner agent: plugin-engineer
- Epic: [024](024-atlas-exploration-ux-epic.md)
- Depends on: [025](025-atlas-exploration-contract-foundation.md)

## Goal and boundary

Validate unknown exploration snapshots before fixture-preview consumption using
one authoritative structural schema plus TypeScript semantic checks.
This would not exist without Atlas: all changes are plugin-service owned.
No core extension point or ADR is required.

## Scope and files

- `services/atlas-explorer/client-ts/src/explorationSchema.v2.json` —
  authoritative Draft 7 schema interpreted by Ajv and ajv-formats.
- `services/atlas-explorer/client-ts/src/explorationValidation.ts` —
  exported `parseExplorationSnapshot` unknown-to-snapshot validation.
- Client types/exports, package manifests, README, tests and shared JSON
  fixtures under `services/atlas-explorer/client-ts/`; optional claim
  `facet_key` references a facet on its subject.
- `services/atlas-explorer/tests/test_exploration_schema.py` and
  `tests/requirements-contract.txt` within that service — structural parity
  against the same schema and shared valid/invalid fixtures.

Excluded: core/web changes, auth, live research, transport, persistence,
migrations, outbound fetching, source-trust or identity-truth guarantees.

## Acceptance criteria — met

- Authoritative schema rejects invalid structure; TS additionally checks unique
  IDs/facet keys, reference integrity, candidate identity restrictions, evidence,
  dates/URL syntax, selected chains, ancestry/cycles, active paths and request
  correlation. Revisions, history versions and sequences are nonnegative safe
  integers.
- `parseExplorationSnapshot` accepts valid fixtures and throws schema-path or
  semantic-reference errors for invalid input; v1 behavior/exports remain intact.
- Optional `facet_key` scopes a claim to an existing subject facet; absence
  means entity-wide, not applicable to every facet.
- Python checks structural compatibility only; TS tests cover semantic checks.
  Test requirements enable optional date-time/URI format checkers and assertions
  prevent silent skips.
- Client build/typecheck pass; no new web or boundary failures against baseline.

## Baseline and verification evidence

Before B/C: 13 client tests pass; client build/typecheck pass. Isolated service
health tests: 3 pass. Global boundaries: 175 errors / 9 warnings; Atlas: 154 / 1.
Web has five pre-existing AtlasExplorer TypeScript diagnostics.

Combined B/C result: **113 client tests pass** (13 preserved + 100 new);
client build/typecheck pass. Isolated Python: **19 pass** (3 health + 16
shared-schema cases). Ruff/Pyright pass on the new Python test; doctor:
8 plugins, 0 errors / 0 warnings. Boundaries and web diagnostics are unchanged.
Engineer-reported `git diff --check` passes for that implementation.

Exact verification commands confirmed from the client README (repository root
initially; the `cd` applies to the final three commands):

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

Run Python tests from the service directory: repository-root pytest resolves
core's `app` instead and fails the three health tests. Results above are supplied
engineer evidence, not rerun by this documentation update; the 100 added client
tests are a combined B/C total, not a per-slice count.

Independent reviewer: **APPROVE**, read-only review, no bugs reported.
The client README's pending-review wording predates this supplied approval.

## Risks, rollback and decisions

URL checks reject malformed syntax, credentials and non-HTTP(S) schemes; they
do not resolve DNS, fetch, or block private networks. This is **not SSRF
security enforcement**; live transport needs separately enforced outbound policy.
Validation does not establish claim truth, real-world identity or source trust.
The parser returns its input; unchecked mutation can invalidate it.

Rollback removes additive v2 validation/exports and contract tests without
changing v1 or stored data. No runtime cutover or migration occurred.
Decisions needed: none for this completed slice. Later live work remains gated
by [008](008-generic-plugin-service-invocation.md); no alternate adapter authorized.

Budget override: B exceeded ~400 changed lines to keep the authoritative schema,
semantic checks and cross-language regression tests coherent; recorded once for
this slice. C's budget is separate.
