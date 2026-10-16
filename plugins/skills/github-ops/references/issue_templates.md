# Issue and review templates

## Bug issue body

```markdown
### Summary
<one sentence: what is broken>

### Steps to reproduce
1. …
2. …

### Expected / actual
- Expected: …
- Actual: …

### Environment
<version, OS, config>

### Evidence
<links to logs, failing CI check URLs, related issues #123>
```

Title format: `[bug] <component>: <symptom>` (≤ 80 chars). Labels: `bug` plus an existing severity label if one exists.

## Feature / task issue body

```markdown
### Goal
<the user outcome>

### Acceptance criteria
- [ ] …
- [ ] …

### Notes
<constraints, links>
```

Title format: `[feat] <capability>` or `[task] <work item>`.

## PR review summary (for a comment or a chat answer)

| Field | Content |
|---|---|
| Scope | files changed / additions / deletions (from `github_get_pull_request`) |
| CI | overall state + failing checks with URLs |
| Reviews | latest state per reviewer (APPROVED / CHANGES_REQUESTED) |
| Mergeability | `mergeable_state` (`clean`, `blocked`, `dirty`, `behind`, `unstable`) |
| Risk | one line, only if evident from the file list (migrations, auth, CI config) |
| Next step | the single most useful action |

`mergeable_state` meanings: `clean` ready; `unstable` non-required checks failing; `blocked` required
reviews/checks missing; `behind` base moved; `dirty` merge conflicts; `draft` not ready for review.
