---
name: slice-planning
description: 'Write a HaaS slice or epic plan in docs/plans/NNN-slug.md with goal, ownership, boundary classification, file list, acceptance criteria, baseline, risks and rollback. Use when: planning the next increment, splitting a large goal into slices, preparing a handoff to an engineer.'
argument-hint: 'Goal to plan'
---

# Slice planning

A plan is the contract between the architect, the engineer, the verifier and the reviewer. Engineers must
be able to execute it without reading the planner's conversation.

## Procedure

1. Pick the next number: highest `docs/plans/NNN-*.md` + 1, zero-padded to 3 digits.
2. Classify every change with the boundary test: *would it exist if the plugin were deleted?*
   Yes → core (generic). No → plugin / plugin service.
3. If the goal mixes core and plugin work or exceeds the size budget (~400 lines, ≤ 1 migration), write an
   **epic** plan listing ordered slices, then a full plan only for the first slice. Order: generic core
   extension point → plugin consumes it → remove the old coupling from core → docs.
4. Fill the [plan template](./assets/plan-template.md) and add a row to `docs/plans/README.md`. Acceptance
   criteria must be checkable by a command or by reading a specific file — never "works correctly".
5. Every slice must leave `main` releasable: old and new paths may coexist briefly (strangler pattern), but
   the system works after each slice.
6. List decisions the user must make with options and a recommendation; do not silently choose for
   anything in AGENTS.md §7 hard stops.

## Status values

`proposed` → `approved` (user agreed to decisions) → `in-progress` → `done` | `blocked` | `superseded`.
The lead resumes any `in-progress`/`blocked` plan before starting new work.
