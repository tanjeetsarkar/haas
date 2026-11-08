# NNN — <Slice title>

- Status: proposed
- Type: slice | epic
- Owner agent: core-engineer | plugin-engineer | client-engineer
- Epic: <link to epic plan, if any>
- ADR: <link, required when core changes>

## Goal
<1–3 sentences: the outcome, and why it is the next smallest valuable step.>

## Boundary classification
| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|

## Scope
**Allowed paths:** <globs the engineer may edit>
**Out of scope:** <explicitly excluded work>

**Planned files:**
- `path` — what changes

## Acceptance criteria
- [ ] <checkable by a command or a specific file read>
- [ ] `check_boundaries.py` errors: <before> → <target>
- [ ] No NEW failures vs baseline on the ladder rungs below

## Verification rungs
<rung numbers from the verification-ladder skill, with exact test selections>

## Baseline
```text
(filled before the first edit)
```

## Risks and rollback
- Risk → mitigation
- Rollback: <revert commit / feature flag / migration downgrade>

## Decisions needed
1. <question> — options: A / B. Recommended: A because …

## Slices (epic only)
| # | Slice | Owner | Depends on | Status |
|---|---|---|---|---|

## Log
- YYYY-MM-DD <agent>: <status change and one-line note>
