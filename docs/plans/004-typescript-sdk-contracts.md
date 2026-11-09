# 004 — Characterize the existing TypeScript SDK contracts

- Status: done
- Type: slice
- Owner agent: client-engineer
- Epic: [001](001-atlas-extraction-sdk-epic.md)
- ADR: none — tests only; no API or behavior decision is authorized

The user later confirmed that the dirty change in
`haas_sdk_ts/src/lib/atlasClient.ts` was removed and authorized future edits
under plan 010. This supersedes the file-protection restriction below only for
that subsequent plan; it does not change this completed characterization's
historical scope or authorize unrelated changes.

## Goal

Add focused TypeScript tests that pin current public package exports,
configuration and asynchronous call shapes, plus representative REST, error
and SSE behavior. Expected wire values must come from the current API
producers/contracts and client implementation, not target documentation. This
is a compatibility characterization, not a bug-fix or redesign slice.

**Unblocked by completed [005](005-run-created-at-response.md).** The user
approved this characterization slice and authorized the overall epic. The
producer-backed `created_at` field has passed the required DB verification.
Characterize the resulting producer response; do not change the TypeScript SDK
to accommodate the pre-fix producer. The user-owned Atlas implementation
(`haas_sdk_ts/src/lib/atlasClient.ts`) remains protected: do not inspect, edit,
format, stage or overwrite it.

## Boundary classification

| Change | Owner | Why |
|---|---|---|
| Generic TypeScript SDK contract tests | Client/core | The SDK remains useful if Atlas is deleted; plugins such as web-research and incident-triage can use it |
| API/AsyncAPI producer and SDK inspection | Read-only inputs | Assertions must reflect implemented behavior, not aspirational examples |

No runtime or contract change is planned; no ADR is required.

## Scope and paths

**Only edit:** `haas_sdk_ts/tests/sdk.contract.test.ts` and this plan's
progress/evidence. The test file does not exist yet; package `test` runs
`vitest run`, so Vitest's default discovery can run it without changing package
scripts. Do not edit `package.json`, dependency files, `src/**`, `tsconfig.json`,
generated `dist/**`, API contracts, or any other test file.

**Protected user-owned file:** do not inspect, edit, format, stage, or otherwise
touch `haas_sdk_ts/src/lib/atlasClient.ts`. The verifier alone records the
pre-slice diff/hash before edits and compares it after verification. Do not
include that path among engineer write paths. Package build/test tools may
process package sources as required by their existing scripts; no source edit
or manual inspection of the protected file is authorized.

Read-only sources include `src/index.ts`, `src/client.ts` excluding the
protected file, `src/types.ts`, `package.json`, `app/main.py`,
`docs/openapi-v1.json`, `docs/api-changelog.md`, `docs/asyncapi.yaml`,
`docs/contracts/run-events.v1.schema.json`, and current producer tests.
`src/index.ts` re-exports `HaaSClient` and `createClientFromEnv`, type-only SDK
models/options/errors, and the existing Atlas facade. Preserve these names;
do not inspect or characterize the Atlas implementation.

## Characterization requirements

1. **Public exports and package types:** assert the current runtime package
   exports (`HaaSClient`, `createClientFromEnv`, and the two existing Atlas
   re-exports) remain importable; compile representative type-only imports from
   the root for `ClientOptions`, `StreamOptions`, `SSEEvent`, run request/
   response/event types, and the three error classes. `npm run build` must
   type-check declarations and imports. Do not add a public name.
2. **Configuration:** characterize `ClientOptions`' required `baseUrl` and
   optional API key, tenant, timeout, retry/reconnect settings and default
   headers. With stubbed `fetch`, pin trailing-slash handling, tenant/default
   headers, and query serialization. Characterize
   `createClientFromEnv(explicitEnv)` using `HAAS_API_URL`, `HAAS_API_KEY`,
   `HAAS_TENANT_ID` and the current localhost URL fallback; suppress its
   existing console output in the test. Do not infer new environment options.
3. **Call shape and REST responses:** use compile-time type assertions (or
   equivalent without adding dependencies) that representative JSON methods
   return Promises and `streamEvents` returns an `AsyncGenerator`; this SDK has
   no synchronous client. Stub fetch and assert request method/path/headers and
   parsed response values for run creation/listing/event paging. After 005,
   run creation includes the required numeric `created_at` from the persisted
   `runs.created_at` value, alongside the existing response fields. The
   `/v1/runs` list producer returns `{items, next_cursor}`; `/events` is
   `{items, next_after, latest_sequence}`. Use response fields/fixtures from
   current producer tests/contracts (including the durable event envelope
   `sequence`, `event_id`, `schema_version`, `type`, `run_id`, `occurred_at`,
   `data`), not invented server fields.
4. **Errors and SSE:** cover the producer's terminal-cancel conflict
   (`POST /v1/runs/{run_id}/cancel`, HTTP 409, detail `Run is already
   terminal.`) and confirm `HaaSAPIError` name, status, message and details
   mapping from `src/client.ts`. Use the current emitted problem response
   format, not a fabricated error envelope.
   Cover stream parsing of an identified durable event (`id`, event name,
   JSON data), cursor forwarding, and the no-id live `output_delta` frame only
   as currently documented in AsyncAPI/schema and implemented in the client.
   Assert behavior rather than adding new retry, abort, event, or error rules.

The minimum set is one contract test group per numbered item above, with at
least one successful producer-backed JSON response, one HTTP-error case and
one durable SSE event assertion. If a candidate expected value or behavior
cannot be tied to current implementation/producer evidence, omit it and record
the uncertainty rather than guessing.

## Implementation sequence

1. Before edits, verifier records `git status --short`, baseline test/build/lint
   results, and the protected-file diff/hash; do not normalize or stage existing
   changes.
2. Add only `haas_sdk_ts/tests/sdk.contract.test.ts`, deriving fixtures and
   assertions from the listed executable sources.
3. Run focused and complete Vitest, package TypeScript build and package lint;
   record exact results and compare the protected-file hash with verifier's
   captured baseline.

## Acceptance criteria

- [x] Diff is limited to the named new test and this plan's evidence; ≤400
  changed non-generated lines. No runtime, package, dependency, generated,
  producer, contract or Atlas file changes.
- [x] Current root runtime exports and representative public type imports
  compile; no new public names are introduced.
- [x] Tests pin current configuration inputs/default fallbacks, observed
  request headers/query encoding, and Promise versus AsyncGenerator shape.
- [x] At least one successful run response and paged events response use
  current producer-backed values; one producer-defined problem response maps
  to the existing SDK error class and properties.
- [x] SSE test covers the current durable event name/id/data and cursor
  forwarding; output deltas are tested only if their exact no-id shape is
  supported by both current source and schema.
- [x] Focused test, full test, build and lint commands pass or introduce no
  failures versus the captured baseline; baseline failures are recorded
  verbatim and not repaired in this slice.
- [x] Protected `atlasClient.ts` diff/hash matches the verifier-captured
  baseline; it is not read or modified by the engineer.
- [x] Any expected assertion that needs runtime/API behavior, public-name,
  package or dependency changes is a stop condition; request a separate plan.

## Baseline and verification protocol

Use the installed package tools; do not install/update dependencies:

```text
cd haas_sdk_ts && npx vitest run tests/sdk.contract.test.ts
npm --prefix haas_sdk_ts run test
npm --prefix haas_sdk_ts run build
npm --prefix haas_sdk_ts run lint
```

The first command is the focused post-edit selection. Before edits, verifier
runs the package test/build/lint baselines and records exact outcomes. The
known pre-004 baseline is: package build passes; Vitest has no discovered test
files; lint cannot run because the package has no ESLint configuration. Python
SDK suite baseline is 95 passed/8 unchanged failures and is context only, not a
TypeScript failure. Reconfirm fresh results before implementation. Treat a
no-tests result or missing lint config as baseline context, not a reason to
change scripts or add dependencies. Build may write ignored `dist` artifacts;
keep generated output out of the slice diff. Verifier captures and compares the
protected dirty-file diff/hash only; engineers must not inspect it. Check
`git diff --check` and confirm only allowed paths changed.

Current source evidence: `src/client.ts` uses async Promise-returning REST
methods and an async-generator SSE method; defaults include 30s request
timeout, 2 retries, 5 stream reconnects and 1s reconnect delay. The API
producer defines run-list `{items,next_cursor}`, run creation status 202 and
the 005 additive `created_at` field, cancel errors 404/409 and paged events;
AsyncAPI/schema define durable event envelopes and live-only output deltas.
Pin details to the executable producer
and client, not those summaries alone.

## Risks, unknowns and rollback

- The TypeScript package exposes many type-only names. This slice pins runtime
  package names and representative core type families; do not silently turn
  every incidental interface into a separately versioned guarantee.
- Existing package Vitest baseline may have no tests. Record its exact
  behavior before adding the focused suite.
- Package build processes all included source files, including the protected
  path, as a tool operation. The engineer must not inspect, edit, or format that
  source; verifier owns before/after diff/hash comparison.
- If API producer data and SDK expectations mismatch, stop and request a
  separately approved behavior/API plan. Do not change assertions to conceal a
  defect or implement a fix here.
- Rollback is limited to removing the new characterization test and reverting
  this plan's progress/evidence; no runtime behavior or persisted data changes.

## Decisions needed

None. The characterization-only, stop-on-mismatch default was approved for
slice 003 and is recommended unchanged for this slice.

## Log

- 2026-11-08 architect: proposed as the next tests-only client slice under epic
  001. No runtime, public API, package or dependency changes are authorized.
- 2026-11-08 client-engineer: blocked before test edits. `HaaSClient.createRun`
  is declared to return `Promise<RunResponse>` (`haas_sdk_ts/src/client.ts`),
  whose `created_at` is required (`haas_sdk_ts/src/types.ts`), but the current
  `POST /v1/runs` producer returns only `run_id`, `thread_id`, `status`,
  `stream_url`, and `events_url` (`app/main.py`). This is a producer/client
  response-contract mismatch; under the stop-on-mismatch policy no fixture or
  assertion was added. The user subsequently selected Option 1, authorizing
  the exact additive producer field; at that time plan 005 was approved and
  this plan remained blocked pending completion of that producer change.
- 2026-11-08 user: approved the overall epic and this TypeScript characterization
  slice after 005's DB-backed acceptance; protected user-owned Atlas
  implementation remains untouched.
- 2026-11-08 integration update: 005 is done; its two required PostgreSQL
  selectors passed against a user-authorized disposable, fully migrated local
  PostgreSQL 16 database. This slice is approved/in-progress and unblocked.
- 2026-11-08 client-engineer implementation (delegation 5/6): added only
  `haas_sdk_ts/tests/sdk.contract.test.ts`; status remains **in-progress** for
  independent verification/review/integration. Before the first edit,
  `test ! -e haas_sdk_ts/tests/sdk.contract.test.ts` confirmed target absence;
  `git diff --name-only -- haas_sdk_ts` showed only the protected Atlas path,
  and `git ls-files --others --exclude-standard -- haas_sdk_ts` was empty.
  Reused the user/verifier's valid pre-edit package baseline, without resets.
- Coverage: 10 tests across root runtime/type exports, required/optional
  configuration, default/explicit headers and timeout, explicit environment
  fallback, URL/query encoding, Promise/AsyncGenerator shapes, run admission
  including numeric `created_at` and URLs, run list and durable event paging,
  terminal-cancel 409 problem mapping, durable SSE plus no-id live output and
  reconnect cursor forwarding. Fixture identifiers/timestamps/text are
  explicitly synthetic schema-conforming examples, not claims about assigned
  server values. Wire shapes derive from `app/main.py`, current OpenAPI,
  AsyncAPI, and the durable/output-delta schemas; the problem envelope derives
  from `app/core/http.py`. Persisted equality is covered by 005's DB verification,
  not simulated by these fetch-stub client tests.
- No behavior assertion failed or required changing runtime/API behavior.
  Root exports error classes as types only; runtime error identity is checked
  against the existing `src/types.ts` class. Producer additive URL/envelope
  fields are represented by local intersections with existing SDK types, not
  new public types. No protected source was manually accessed or edited.
  Verifier must compare the protected diff to its captured hash
  `cb14fd38219926cb569c149fe00ec5f0ded0473520689ee80dbf6b20f68d4f81`.
- Exact gate outcomes (2026-11-08):
  - `cd haas_sdk_ts && npx --no-install vitest run tests/sdk.contract.test.ts`:
    exit 0, `Test Files 1 passed (1)`, `Tests 10 passed (10)`; final run test
    time 40ms, duration 1.18s. Baseline exit 1, no test file.
  - `npm --prefix haas_sdk_ts run test`: exit 0, Vitest v1.6.1,
    `Test Files 1 passed (1)`, `Tests 10 passed (10)`, test time 85ms,
    duration 1.50s. Baseline exit 1, no test files.
  - `npm --prefix haas_sdk_ts run build`: exit 0, output
    `> @haas/sdk@0.1.0 build` / `> tsc`; unchanged passing baseline.
  - Supplemental test type-check (package tsconfig includes only `src/**/*`):
    `cd haas_sdk_ts && npx --no-install tsc --noEmit --target ES2022 --module ESNext --moduleResolution bundler --lib ES2022,DOM,DOM.Iterable --strict --esModuleInterop --skipLibCheck tests/sdk.contract.test.ts`:
    final exit 0, no diagnostics. Initial exit 2: `tests/sdk.contract.test.ts(38,23): error TS2344`
    (function type did not satisfy `any[]`); one test-only correction used
    installed Vitest 1's argument/return generics. No assertion changed.
  - `npm --prefix haas_sdk_ts run lint`: exit 2, ESLint 8.57.1:
    `ESLint couldn't find a configuration file.` Searched package `src` and
    ancestors, then home. Same missing-config failure as baseline; not repaired.
  - `git diff --check`: exit 0, no output; unchanged passing baseline.
  - `python .github/skills/core-boundary-check/scripts/check_boundaries.py`:
    exit 1 before and after, exact summary
    `155 core-boundary error(s), 9 plugin->core-internal warning(s)`;
    delta 0 errors / 0 warnings. Existing debt remains out of scope.
- Verification correction: the verifier's first pass correctly identified that
  the SSE fixture supplied the whole event envelope rather than serialized
  `event.data`. Review round 1 requested serializing `event.data`; the resulting
  correction was test-only. Final verification and review were clean.
- Final evidence: focused and full Vitest each pass all 10 tests; package build
  and supplemental strict test type-check pass. Lint exits 2 because the ESLint
  configuration is missing, exactly as at the pre-slice baseline. `git diff
  --check` passes. Boundary counts are unchanged: overall 155 errors/9 warnings,
  Atlas-filtered 0/0, and atlas-explorer-filtered 134/1 (known debt).
- Protected-file check: the dirty
  `haas_sdk_ts/src/lib/atlasClient.ts` diff hash matches
  `cb14fd38219926cb569c149fe00ec5f0ded0473520689ee80dbf6b20f68d4f81`; it was
  never touched or read. The slice added only the one test file and plan
  evidence: 254 test lines plus 57 plan lines, 311 changed lines total.
- Review: round 2/2 verdict **APPROVE**; final verification/review clean.
- Budget override (one time): prior blocked initial attempt plus mandatory
  verifier, reviewer and integration gates required the over-budget workflow;
  total slice task delegations are now 11/6. No further override is implied.
- 2026-11-08 integration: slice complete; no runtime/code changes beyond the
  single characterization test file. Future candidates are TypeScript
  transport fixes and Python SDK findings, then the Atlas trust audit/extraction.
  Each requires its own separate full plan; this completion authorizes no
  runtime work.
