# Slice plans

Plans written by the `architect` agent using the
[slice-planning skill](../../.github/skills/slice-planning/SKILL.md). One file per slice or epic:
`NNN-short-slug.md`. The `haas-lead` agent resumes any plan with status `in-progress` or `blocked`
before starting new work. Workflow and handoff rules: [AGENTS.md](../../AGENTS.md).

| # | Plan | Status |
|---|---|---|
| 001 | [Atlas extraction and SDK stabilization — epic](001-atlas-extraction-sdk-epic.md) | in-progress |
| 002 | [Standalone Atlas service skeleton — first slice](002-atlas-service-skeleton.md) | done |
| 003 | [Python SDK contract characterization](003-python-sdk-contracts.md) | done |
| 004 | [TypeScript SDK contract characterization](004-typescript-sdk-contracts.md) | done |
| 005 | [Persisted run creation time in submission response](005-run-created-at-response.md) | done |
| 006 | [Restore Python FakeHaas scripted scenario semantics](006-python-fakehaas-scenarios.md) | done |
| 007 | [Atlas service invocation trust audit](007-atlas-service-invocation-audit.md) | done — final reviewer approved; public-path proof gap confirmed; service tests unavailable, DB-backed identity tests not run, phase-3 workflow collected only |
| 008 | [Generic plugin-service invocation](008-generic-plugin-service-invocation.md) | proposed — 008a2 blocked; child 008a2a proposed with only internal injected-key Ed25519 primitive scope user-approved; operational gates unverified; no broader implementation/consumer authorized |
| 009 | [Finite Python SDK Retry-After handling](009-python-sdk-finite-retry-after.md) | done — 117 SDK tests passed; Ruff/Pyright passed; focused reviewer approved; boundary scan unavailable under protected-file restriction |
| 010 | [Atlas TypeScript client/type extraction — bounded epic](010-atlas-typescript-client-extraction.md) | in-progress — 010a/010b done; G2 inventory complete; 010c waits for 012k UI relocation and coordinated cutover checks |
| 011 | [Marketplace Atlas client import migration](011-marketplace-atlas-client-imports.md) | blocked — reviewer found 20 new Atlas boundary errors; user selected service-local UI; unaccepted working changes preserved as temporary bridge pending approved 012 migration |
| 012 | [Atlas UI service extraction — bounded epic and first full slice](012-atlas-ui-service-extraction.md) | in-progress — 012a–k done; 012l blocked by plan 013; core web retirement deferred to plan 013 |
| 013 | [Retire the entire core marketplace web package — bounded epic](013-retire-core-marketplace-web.md) | proposed |
| 014 | [Stage the Atlas EntityCard component](014-atlas-ui-entity-card-copy.md) | done |
| 015 | [Stage Atlas comparison and specialist components](015-atlas-ui-comparison-specialist-copy.md) | done |
| 016 | [Stage Atlas specialist and pricing components](016-atlas-ui-specialist-pricing-copy.md) | done |
| 017 | [Stage Atlas checklist component](017-atlas-ui-checklist-copy.md) | done |
| 018 | [Stage Atlas approval drawer and app styles](018-atlas-ui-approval-drawer-app-styles-copy.md) | done |
| 019 | [Stage Atlas drawer styles and component exports](019-atlas-ui-drawer-styles-exports-copy.md) | done |
| 020 | [Stage Atlas MillerColumn component](020-atlas-ui-miller-column-copy.md) | done |
| 021 | [Stage Atlas explorer stylesheets](021-atlas-ui-stylesheets-copy.md) | done |
| 022 | [Stage Atlas explorer component](022-atlas-ui-explorer-component-copy.md) | done |
| 023 | [Activate the Atlas service UI](023-atlas-ui-service-ui-activation.md) | done |
| 024 | [Atlas knowledge exploration UX — epic](024-atlas-exploration-ux-epic.md) | in-progress — A–D done; live workspace default and conditional service-UI retirement reported; live research/authorization proof pending |
| 025 | [Atlas exploration contract foundation](025-atlas-exploration-contract-foundation.md) | done — independent reviewer APPROVE; 13 client tests pass |
| 026 | [Atlas exploration runtime validation](026-atlas-exploration-validation.md) | done — independent reviewer APPROVE; combined B/C 113 client / 19 isolated Python tests pass |
| 027 | [Atlas exploration navigation state](027-atlas-exploration-navigation.md) | done — independent reviewer APPROVE; local immutable navigation and update guard only |
| 028 | [Atlas fixture-only Miller-column preview](028-atlas-miller-column-preview.md) | done — independent reviewer APPROVE after review fix round 1; opt-in fixture preview only |
| 029 | [Atlas live public-API client and immutable snapshots](029-atlas-live-client-snapshots.md) | in-progress — source-pair/branch-history fix independently approved; current live research graph and remaining security proof pending |
| 030 | [Atlas loopback-only authenticated development proxy](030-atlas-loopback-proxy.md) | done — independent APPROVE, 78 tests reported, authenticated live `/v1/me` returned 200; production gateway remains out of scope |
| 031 | [Atlas live workspace, capability parity and legacy retirement](031-atlas-live-workspace-parity-retirement.md) | in-progress — default live UI and conditional legacy service-UI retirement reported; latest web 120 tests/build/typecheck pass reported; live proof pending |
| 032 | [Generic artifact multipart validation and tenant context cleanup](032-artifact-upload-validation-fix.md) | done — 112 targeted tests reported passing, independent review approved; browser-native artifact round trip recorded under 034 |
| 033 | [Resolve tenant UUIDs for generic audit writes](033-audit-tenant-uuid-mapping.md) | done — independent review approved; Docker-backed authenticated `POST /v1/threads` 201, configured identity 200 and mismatch 403 reported |
| 034 | [Generic artifact CRUD against the canonical schema](034-artifact-canonical-schema-roundtrip.md) | in-progress — 126 tests incl. 14 DB tests and browser save/list/download byte match reported; empty-cursor fix and independent review pending |
| 035 | [Bounded external-action initiation — epic and first slice](035-bounded-action-initiation.md) | proposed — generic restriction/separate approver direction user-approved; detailed contract proposed; live approval credential/session boundary unavailable |
| 036 | [Bounded acquisition and tool-free finalization — epic and first slices](036-bounded-acquisition-finalization.md) | approved for 036a/036b implementation; one reversible migration authorized for disposable verification only; no shared DB apply or execution claimed |
| 037 | [Live final-entry authorization RPC — 035c2](037-live-final-entry-authorization-rpc.md) | approved design; implementation gated on confirming existing worker service-auth identity; no new auth mechanism or env setting inferred |
