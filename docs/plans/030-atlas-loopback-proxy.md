# 030 — Atlas loopback-only authenticated development proxy

- Status: done — independent review approved (78 tests); authenticated live `/v1/me` returned 200
- Type: slice
- Owner agent: plugin-engineer
- Workstream: proxyagent
- Epic: [024](024-atlas-exploration-ux-epic.md)
- ADR: none — Atlas service-local development tooling only

## Goal and boundary

Allow the new Atlas workspace to use existing authenticated public HaaS APIs in
local development without browser secrets. The user approves a server-side Vite
development proxy reading `.env.haas` and targeting fixed `http://localhost:8000`.
This tooling would not exist without the Atlas web app; it is extension-owned,
not a generic core gateway or production authentication design.

## Scope and files

**Allowed:** `services/atlas-explorer/web/vite.config.ts`, a focused server-only
proxy helper under `services/atlas-explorer/web/dev/`, proxy tests under
`services/atlas-explorer/web/tests/`, and service-web development instructions.
If a test config include must change, coordinate with UI owner; one writer per file.

**Planned files:**
- `web/vite.config.ts` — enable hardened authenticated proxy only in dev serve mode.
- `web/dev/loopbackProxy.ts` — optional server-only guard/credential loading helper.
- `web/tests/loopbackProxy.test.ts` — request/host/origin/mode tests.
- `web/README.md` — optional local setup and production distinction, no credential values.

Do not edit UI components/client-ts (029/031 owners), `.env.haas`, core auth,
SDKs, runtime dependencies, compose/deployment or production gateway configuration.
No browser-visible secret env prefix, generated credential file or compatibility proxy.

## Execution and budget

Single slice ≤400 changed lines including tests. Keep helper and focused tests
small; if the coherent guard matrix exceeds this, document a budget override and
split configuration/guards from verification checkpoints before integration.
030 can run parallel to 029; agree same-origin `/v1` routing with 031.

## Acceptance criteria

- [ ] Dev server binds loopback only; supported loopback Host values/ports and
  origins are explicit, not wildcard or suffix matches. Default backend target is
  fixed `http://localhost:8000`, never request/query/header/env-controlled.
- [ ] Credentials load from `.env.haas` on the server only, with existing variable
  names confirmed locally but values never printed/documented. Missing/invalid
  configuration fails closed; no unauthenticated fallback that implies live success.
- [ ] Proxy middleware runs only for Vite **development serve**. Neither build nor
  preview installs authenticated forwarding or reads/injects credentials.
- [ ] Reject untrusted Host/Origin before credential injection/forwarding, including
  remote hosts, DNS-rebinding-style names, null/foreign origins and cross-site writes.
  Define safe no-Origin handling explicitly for loopback CLI/read traffic; missing
  Origin is not a blanket exemption for browser mutation endpoints.
- [ ] Reject browser requests marked cross-site by fetch metadata; test forged Host,
  unexpected port and state-changing no-Origin cases. Test real loopback requests
  and rejected requests, not only helper booleans.
- [ ] Only intended public `/v1` paths forward; malformed/absolute/traversal targets
  cannot select another upstream. Client authorization/tenant headers cannot override
  server configuration or manufacture membership; core enforces tenant selection.
- [ ] Stream forwarding, run cancellation and artifact upload/download work without
  secrets entering browser storage, browser request headers, HTML, errors or logs.
  Do not follow attacker-controlled redirects with server credentials.
- [ ] Proxy forwards core approval denials unchanged. It never grants approver role,
  changes principal to self-approve, bypasses separate-authorizer rules or adds an
  authentication middleware exception for approval routes.
- [ ] Production build artifact inspection finds no credential values/material;
  perform inspection without echoing secrets. Preview cannot forward authenticated
  requests. Remote access attempts fail before any backend call.
- [ ] Document that production static output needs an actual operator-configured
  authenticated same-origin gateway; live mode without one fails closed.
- [ ] No new failures or boundary increases; independent verification/review pending.

## Baseline and verification

Supplied baseline (not rerun here): web 10 tests pass, bundle passes, five legacy
TS diagnostics; client 113/Python 19 pass; boundaries global 175/9, Atlas 154/1.
Before changes capture actual working-tree baseline because implementations are in flight.

From `services/atlas-explorer/web`: `npm test`, `npm run typecheck`,
`npm run bundle`; repeat focused proxy tests and full suite. Validate dev, build
and preview modes explicitly; run loopback HTTP guard probes with no secret output.
Inspect built JS/maps and browser storage/network using redacted assertions.
Run `python .github/skills/core-boundary-check/scripts/check_boundaries.py`
from root; global ≤175 errors/9 warnings and Atlas ≤154/1.
The five legacy diagnostics remain baseline until 031's authorized retirement;
do not fix unrelated components here.

## Risks, unknowns and rollback

- Vite middleware ordering/Host validation and no-Origin behavior require runtime
  evidence; config inspection alone does not establish resistance to remote CSRF.
- Actual env variable names and availability are implementation prerequisites;
  never guess values, broaden target selection or expose them to the UI.
- Production gateway/operator setup is outside this slice; a successful local
  request proves neither production authentication nor trusted service invocation.
- Rollback: disable/remove the authenticated dev proxy and leave live mode unavailable;
  never restore insecure secret forwarding or silently fall back to sample data.

## Decisions needed

None. Production deployment requires later operator configuration, not approval to
build a new gateway here.

## Log

- 2026-11-09 architect: initial in-flight handoff recorded remote/CSRF,
  credential-isolation and build/preview evidence as open gates; subsequent
  independent review and 78-test approval plus authenticated `/v1/me` live
  verification supersede that interim status for this local proxy slice.
- 2026-11-09 architect: parent reports 78 proxy tests and independent `APPROVE`,
  plus an actual authenticated `/v1/me` request through the proxy returning 200.
  This closes the local proxy integration gate only; it is not evidence of a
  production gateway or deployment, which remains outside this plan.
