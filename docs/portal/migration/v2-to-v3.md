# Migration Guide: v2.x to v3.0

This guide covers breaking changes and migration steps for upgrading from HaaS v2.x to v3.0.

## Overview of Changes

| Area | v2.x | v3.0 |
|------|------|------|
| API Version | `/v1` (unstable) | `/v1` (stable, additive) |
| Authentication | API Key only | OIDC JWT + API Keys |
| Tenancy | Header-based | Enforced RLS |
| Runs | Fire-and-forget | Durable with outbox |
| Approvals | Simple pause | Digest-bound, expiring |
| Budget | Best-effort | Pre-dispatch reservations |
| Plugins | Direct imports | OCI artifacts + sandbox |
| Multi-Agent | Not supported | HACP, A2A, MCP, OpenAI |

## Pre-Migration Checklist

- [ ] Backup PostgreSQL database
- [ ] Review custom plugin code for sandbox compatibility
- [ ] Update client SDKs to v3.0
- [ ] Test in staging environment
- [ ] Plan maintenance window

## Database Migration

Run Alembic migrations in order:

```bash
# Upgrade to head (includes all v3.0 migrations)
alembic upgrade head

# Verify migration
alembic current
```

Key migrations:
- `0006_phase2_schema_expand.py` - Tenant UUID keys, composite constraints
- `0007_threads_agents.py` - Thread fencing, agent revisions
- `0008_tenant_rls.py` - Row-level security policies
- `0009_durable_workflow.py` - Outbox, leases, steps, approvals, budget
- `0010_completion_usage.py` - Token streaming, usage ledger
- `0011_multi_agent_schema.py` - Delegations, mailbox, workspace, agent cards
- `0012_execution_isolation.py` - Sandbox sessions, plugin pins
- `0013_credentials_egress.py` - Secret bindings, grants, egress policy
- `0014_artifacts_retention.py` - Artifacts, retention, legal holds, erasure
- `0015_service_operations.py` - Webhooks, quotas, cells, restore epoch
- `0016_marketplace.py` - Publishers, listings, revisions, installations
- `0017_multi_agent_runtime.py` - Topologies, HACP mailbox

## Authentication Changes

### Before (v2.x)
```python
# Only API key
headers = {"Authorization": "Bearer hk_..."}
```

### After (v3.0)
```python
# OIDC JWT (preferred)
headers = {
    "Authorization": "Bearer <oidc-token>",
    "X-Tenant-ID": "tenant-uuid"
}

# Or API key (still supported)
headers = {
    "Authorization": "Bearer hk_...",
    "X-Tenant-ID": "tenant-uuid"
}
```

## Run Creation Changes

### Before (v2.x)
```json
{
  "prompt": "Do something",
  "skills": ["skill1"],
  "model": "gpt-4"
}
```

### After (v3.0)
```json
{
  "prompt": "Do something",
  "preload_skills": ["skill1"],
  "primary_model": "openrouter/model",
  "planner_model": "openrouter/model",
  "max_steps": 15,
  "max_cost_usd": 1.00,
  "secrets": {"KEY": "value"},
  "agent_revision_id": "uuid",
  "expected_history_version": 1
}
```

## Approval Changes

### Before (v2.x)
```json
POST /v1/runs/{id}/resume
{"approved": true}
```

### After (v3.0)
```json
POST /v1/runs/{id}/resume
{
  "approved": true,
  "batch_id": "uuid",
  "batch_digest": "sha256...",
  "secrets": {"KEY": "value"},
  "rejection_reason": "optional"
}
```

Or use the dedicated approval endpoint:
```json
POST /v1/approvals/{batch_id}/decision
{
  "decision": "approve",
  "batch_digest": "sha256...",
  "reason": "Approved by reviewer",
  "secrets": {"KEY": "value"}
}
```

## Budget Changes

### Before (v2.x)
No budget enforcement.

### After (v3.0)
```json
{
  "max_cost_usd": 5.00,
  "max_steps": 20
}
```

Budget is reserved before dispatch. Exhausted budget returns `402 Payment Required`.

## Plugin Migration

### Before (v2.x)
```python
# Direct import
from plugins.my_plugin import my_tool
```

### After (v3.0)
```python
# Plugin packed as OCI artifact
# Materialized in sandbox at runtime
# Tool invoked via sandbox protocol
```

Steps:
1. Add `plugin.json` manifest to plugin directory
2. Run `haas plugin pack` to create artifact
3. Publish to marketplace or private registry
4. Install via `haas marketplace install`
5. Reference in agent configuration

## Multi-Agent Migration

### New Capabilities

```python
# Create delegation (spawn child agent)
delegation = haas.agent_mesh.delegations.create(
    parent_run_id=run_id,
    child_agent_revision_id=child_rev_id,
    budget_usd=1.00,
    max_steps=5
)

# Agent messaging
haas.agent_mesh.messages.send(
    run_id=run_id,
    from_agent="parent",
    to_agent="child",
    message_type="request",
    payload={"task": "analyze"}
)

# CAS-versioned workspace
haas.agent_mesh.workspace.write(
    run_id=run_id,
    agent_id="child",
    key="findings",
    value={"issues": [...]},
    expected_version=0
)
```

## SDK Migration

### Python SDK

```python
# v2.x
from haas import Client
client = Client(api_key="...")

# v3.0
from haas_sdk import Haas
haas = Haas(api_key="hk_...", tenant="my-tenant")

# Async support
from haas_sdk import AsyncHaas
async with AsyncHaas(api_key="hk_...") as haas:
    run = await haas.runs.create("prompt")
```

### TypeScript SDK

```typescript
// v3.0 only
import { HaaSClient } from '@haas/sdk';

const haas = new HaaSClient({
  baseUrl: 'https://api.haas.example',
  apiKey: 'hk_...',
  tenant: 'my-tenant'
});

const run = await haas.runs.create({ prompt: 'Hello' });
```

## CLI Migration

### New Commands

```bash
# Runs
haas runs create "prompt" --max-steps 5
haas runs stream <run_id>
haas runs tree <run_id>

# Agents
haas agents list
haas agents draft "name" --primary-model "..." --planner-model "..."
haas agents validate <rev_id>
haas agents publish <rev_id>

# Approvals
haas approvals list
haas approvals decide <batch_id> --approve --batch-digest <digest>

# Webhooks
haas webhooks endpoints create <url>
haas webhooks keys rotate <key_id> --new-key-id <id> --new-secret <secret>

# Marketplace
haas marketplace search --query "security"
haas marketplace install <listing_id> --revision <rev_id>

# Agent Mesh
haas agent-mesh delegations create --parent-run <id> --child-rev <id>
haas agent-mesh workspace write --run <id> --agent <id> --key <key> --value '{}'
```

## Configuration Changes

### Environment Variables

| v2.x | v3.0 | Notes |
|------|------|-------|
| `HAAS_API_KEY` | `HAAS_API_KEY` | Still supported |
| `HAAS_TENANT` | `HAAS_TENANT_ID` | Renamed |
| `HAAS_PROFILE` | `HAAS_PROFILE` | `dev`/`prod` |
| - | `HAAS_OIDC_ISSUER` | New |
| - | `HAAS_OIDC_AUDIENCE` | New |
| - | `HAAS_OIDC_JWKS_URL` | New |

### Profile Configuration

```bash
# v3.0 profiles include environment
haas profile add prod \
  --url https://api.haas.example \
  --tenant my-tenant \
  --environment prod
```

## Testing Migration

```bash
# Run migration tests
pytest tests/test_phase2_database.py -v
pytest tests/test_phase3_workflow.py -v
pytest tests/test_multi_agent_schema.py -v

# Run E2E tests
haas dev up
haas dev seed
pytest tests/e2e/ -v
```

## Rollback Procedure

If issues arise:

```bash
# 1. Stop services
docker compose down

# 2. Restore database from backup
pg_restore -d haas backup.dump

# 3. Downgrade migrations (if needed)
alembic downgrade 0005

# 4. Deploy v2.x images
docker compose -f docker-compose.yml -f docker-compose.v2.yml up -d
```

## Support

- Migration issues: #haas-migration Slack channel
- Documentation: https://docs.haas.example/migration
- Support: support@haas.example