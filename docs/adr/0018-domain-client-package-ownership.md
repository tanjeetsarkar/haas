# ADR-0018: Domain client packages belong to extensions

* Status: accepted
* Roadmap items: Atlas extraction / SDK stabilization, plan 001; bounded plan 010
* Related requirements/threats: AGENTS.md §§1–2, 4, 7; public SDK compatibility; service identity separation

## Context

The public TypeScript kernel client currently exports a domain session facade
and duplicates its types. Plugin services have a designated `client-ts/`
location. Moving ownership must not imply that service routes, authentication,
or domain behavior have been implemented.

## Decision

Plugins can provide independently tested client packages built on public
`@haas/sdk` exports. Atlas Explorer and web-research could both use this
composition pattern. Core remains responsible only for generic run, thread,
approval and event contracts; it neither imports nor re-exports domain clients.
No new core extension point, runtime dependency, transport or authorization
facility is introduced.

The first application is [plan 010](../plans/010-atlas-typescript-client-extraction.md):
move the existing Atlas facade/types to
`services/atlas-explorer/client-ts`, then remove their core exports and source
in a coordinated breaking cutover. The user selected this compatibility policy;
exact package versions and the migration inventory are implementation gates.
Approval of this ADR is not publication authorization.

Preserve all non-domain public SDK exports and behavior. The extracted facade
continues to receive a caller-created public HaaS client and delegates to that
client, not to the health-only Atlas service. Type assertions in the facade
remain assertions, not runtime event validation or evidence of producer support.

## Consequences (including rejected alternatives)

- Ownership becomes service-local without changing tenancy, secret handling,
  approval arguments, budgets, or event behavior during the move.
- Tests/builds must prove independent package resolution against the public SDK;
  source-relative imports into core and core-to-extension dependencies are forbidden.
- The coordinated cutover needs migration instructions, explicit package
  versions, and fresh downstream-consumer evidence before core removal.
- Re-export shims and permanent duplicate declarations were rejected: the user
  selected a breaking policy, and either would retain domain coupling in core.
- A new generic service transport was rejected for this extraction. Service
  identity/authentication and real service invocation remain separately gated
  by ADR-17 / plan 008; this ADR does not accept or implement them.
- No HTTP/event producer contract changes occur. Existing HTTP deprecation
  windows are not shortened; the approved package-export break is documented
  separately and must not silently ship under an unchanged package version.
