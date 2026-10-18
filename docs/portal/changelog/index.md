# HaaS Changelog

All notable changes to HaaS are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.0.0] - 2024-01-15

### Added
- **Multi-tenancy**: Row-level security with enforced tenant isolation
- **Durable Workflows**: PostgreSQL-backed outbox relay, lease fencing, step attempts
- **Approval Gates**: Digest-bound, expiring approvals with batch decisions
- **Budget Controls**: Pre-dispatch reservations with NUMERIC ledger
- **Plugin Marketplace**: Content-addressed OCI artifacts, trust tiers, transparency log
- **Multi-Agent Collaboration**: HACP, A2A, MCP, OpenAI-compatible protocols
- **Execution Isolation**: Static catalog, sandbox protocol, per-run materialization
- **Credential Broker**: Secret bindings, single-use grants, audience binding
- **Egress Proxy**: Destination policy, DNS pinning, byte quotas
- **Redaction**: Configurable regex rules for model/events/logs/callbacks
- **Artifact Service**: Tenant/run-scoped object storage with SHA256 verification
- **Retention**: TTL policies, legal holds, erasure requests, tombstones
- **Signed Webhooks**: HMAC v1, key rotation, delivery ledger, replay
- **Quotas & Fairness**: Rate limits, concurrency reservations, weighted fair queues
- **Platform Operations**: Restore epoch, cell placement, tenant move workflow
- **Developer Platform**: OpenAPI 3.1, AsyncAPI, Python/TypeScript SDKs, unified CLI
- **Local Development**: `haas dev` with mock provider, seed data, smoke/doctor

### Changed
- **API Stability**: `/v1` endpoints now additive-only with deprecation policy
- **Authentication**: OIDC JWT + tenant-scoped API keys (header stripping)
- **Run States**: Added `input_required` for approval pauses
- **Plugin Loading**: No imports in control plane; sandbox-only execution
- **Secret Handling**: No raw secrets in transport; per-invocation grants
- **Event Streaming**: Durable SSE with `Last-Event-ID` resume and gap detection

### Fixed
- Race conditions in run admission and lease management
- Idempotency key replay with digest verification
- Approval expiry handling (expired → run failed)
- Cancellation truthfulness (`cancelling` state)
- Cross-tenant write prevention via RLS

### Security
- SEC-01: OIDC JWT with JWKS rotation
- SEC-02: Tenant registry with lifecycle state machine
- SEC-03: Forced RLS with non-owner runtime logins
- SEC-04: Immutable deny-by-default tenant policies
- SEC-05: Static catalog, no plugin imports in control plane
- SEC-06: Digest-bound approvals with RBAC
- SEC-07: Lease fencing with Ed25519 envelopes
- SEC-08: Egress proxy with destination policy
- SEC-09: Credential broker with redaction
- SEC-10: Signed webhooks with key rotation
- SEC-11: Quotas with weighted fair queuing
- SEC-12: Fenced cancellation with cascade
- SEC-13: Admin API with action-based RBAC
- SEC-14: Retention with legal hold and erasure
- SEC-15: Thread fencing with history version
- SEC-16: Marketplace signing with transparency log
- SEC-17: Review pipeline with static scans
- SEC-18: Digest-pinned installs with consent
- SEC-19: Re-consent on permission expansion
- SEC-20: Delegation attenuation with budget sub-reservations
- SEC-21: Spawn caps and ledger integration
- SEC-22: Workspace CAS with version history
- SEC-23: A2A/MCP with egress mediation
- SEC-24: OpenAI facade with RBAC parity
- SEC-25: HITL cancel cascade
- SEC-26: SDK with webhook verification

## [2.1.0] - 2023-10-01

### Added
- Basic agent execution with skills
- Thread history with branching
- Agent revision lifecycle (draft → validated → published)
- Tenant policy with model/plugin/webhook allowlists
- Audit logging with verified actor

### Changed
- Improved Celery task routing
- Enhanced prompt/body limits

## [2.0.0] - 2023-07-15

### Added
- Multi-tenant architecture
- PostgreSQL authoritative store
- Celery-based execution engine
- Basic approval workflow
- Plugin system with direct imports

### Breaking Changes
- API restructured under `/v1`
- Authentication moved to headers
- Tenant isolation enforced

## [1.0.0] - 2023-04-01

### Added
- Initial release
- Single-tenant agent execution
- Basic skill system
- Docker Compose deployment

---

## Upgrade Notes

### Upgrading to 3.0.0
See [Migration Guide: v2.x to v3.0](../migration/v2-to-v3.md)

### Deprecation Policy
- Features marked deprecated get 6-month notice
- Deprecated endpoints return `Sunset` header
- Removal only in major versions

### Support Matrix
| Version | Status | Supported Until |
|---------|--------|-----------------|
| 3.0.x | Active | 2025-01-15 |
| 2.1.x | Maintenance | 2024-07-15 |
| 2.0.x | EOL | 2024-01-15 |
| 1.x | EOL | 2023-10-01 |