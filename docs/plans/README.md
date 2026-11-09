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
