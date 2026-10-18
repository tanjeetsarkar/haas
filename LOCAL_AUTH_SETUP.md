# Local HaaS Authorization Setup Guide

This guide walks you through setting up authentication and authorization for a local HaaS development instance running in Docker.

## Quick Start (TL;DR)

```bash
# 1. Start the stack
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# 2. Wait for services to be healthy (check with `docker compose ps`)

# 3. Seed development data (creates tenant + API keys)
docker compose exec haas-api python -m app.seed_dev

# 4. Use the printed API key
export HAAS_API_KEY="hk_test_..."  # Copy from seed_dev output
export HAAS_TENANT="local-dev"

# 5. Test CLI
haas chat
```

---

## Detailed Step-by-Step

### 1. Prerequisites

- Docker & Docker Compose v2+
- `.env` file (copy from `.env.example`):
  ```bash
  cp .env.example .env
  ```
- Required in `.env` for local dev:
  ```bash
  HAAS_PROFILE=dev
  OPENROUTER_API_KEY=sk-or-v1-...  # Get from openrouter.ai
  ```

### 2. Start the Local Stack

```bash
# Start all services (API, workers, Postgres, Redis)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# Verify all services are healthy
docker compose ps
# All should show "healthy" or "running"
```

### 3. Initialize Development Data

The `seed_dev` script creates:
- A tenant named `local-dev`
- A bootstrap admin principal (`urn:haas:local-dev` / `local-admin`)
- **Two API keys** (printed to stdout):
  - `admin_credential` — `tenant_admin` + `submitter` roles
  - `approver_credential` — `approver` role

```bash
# Run inside the API container
docker compose exec haas-api python -m app.seed_dev

# Output example:
# Seeded local tenant local-dev.
# One-time tenant-admin/submitter API key: hk_test_abc123...
# One-time separate approver API key: hk_test_xyz789...
```

> **Important**: These keys are only shown **once**. Save them immediately!

### 4. Configure Your Client

#### Option A: Environment Variables (Recommended)

```bash
# For admin/submitter access
export HAAS_API_KEY="hk_test_abc123..."  # From seed_dev output
export HAAS_TENANT="local-dev"
export HAAS_URL="http://localhost:8000"

# For approver access (separate terminal)
export HAAS_API_KEY="hk_test_xyz789..."
export HAAS_TENANT="local-dev"
```

#### Option B: CLI Config File

The CLI stores config in `~/.config/haas/` but env vars take precedence.

### 5. Verify Access

```bash
# Test basic connectivity
haas chat --help

# Test API directly
curl -H "Authorization: Bearer $HAAS_API_KEY" \
     -H "X-Tenant-ID: local-dev" \
     http://localhost:8000/v1/me

# Should return your identity with roles
```

---

## Understanding the Auth System

### Credential Types

| Type | Prefix | Use Case |
|------|--------|----------|
| **API Key** | `hk_test_` (dev) / `hk_live_` (prod) | Long-lived, programmatic access |
| **OIDC Bearer** | (JWT) | Human login via identity provider |

### Local Dev Defaults

| Setting | Value |
|---------|-------|
| Tenant slug | `local-dev` |
| Admin issuer | `urn:haas:local-dev` |
| Admin subject | `local-admin` |
| API key prefix | `hk_test_` |

### Roles & Permissions

| Role | Key Permissions |
|------|-----------------|
| `tenant_admin` | Full tenant management, memberships, API keys, policies, audit |
| `submitter` | Submit runs, read own runs, artifacts, plugins |
| `approver` | Read pending approvals, approve/reject |
| `auditor` | Read audit logs, usage metrics |

---

## Common Workflows

### Create Additional API Keys

```bash
# As tenant_admin, create a new key for a service account
curl -X POST http://localhost:8000/v1/admin/api-keys \
  -H "Authorization: Bearer $HAAS_API_KEY" \
  -H "X-Tenant-ID: local-dev" \
  -H "Content-Type: application/json" \
  -d '{
    "principal_id": "<principal-uuid>",
    "name": "ci-cd-pipeline",
    "expires_in_days": 90
  }'
```

### Grant Roles to Other Users

```bash
# Grant submitter role to a colleague (by their OIDC subject)
curl -X POST http://localhost:8000/v1/admin/memberships \
  -H "Authorization: Bearer $HAAS_API_KEY" \
  -H "X-Tenant-ID: local-dev" \
  -H "Content-Type: application/json" \
  -d '{
    "subject": "their-oidc-subject",
    "kind": "human",
    "role": "submitter"
  }'
```

### Use with haas-sdk (Python)

```python
from haas_sdk import HaasClient

client = HaasClient(
    base_url="http://localhost:8000",
    api_key="hk_test_...",
    tenant="local-dev"
)

# Submit a run
run = await client.runs.submit(
    agent="my-agent",
    input={"prompt": "Hello!"}
)
```

---

## Troubleshooting

### "Invalid or expired API key"
- Key was revoked or expired → Run `seed_dev --rotate-key` to regenerate
- Wrong tenant → Check `HAAS_TENANT` matches `local-dev`
- Wrong profile → Ensure `HAAS_PROFILE=dev` (test keys rejected in prod)

### "OIDC bearer authentication is not configured"
- You're sending a JWT but OIDC isn't set up
- Use API key (`hk_test_...`) instead for local dev

### "API key is not authorized for this tenant"
- Tenant mismatch: `X-Tenant-ID` header must match key's tenant
- Key belongs to different tenant

### "No active membership for the selected tenant"
- Principal's membership was revoked
- Re-run `seed_dev` or grant membership via admin API

### Connection refused / 502
- Services not ready: `docker compose ps` → wait for healthy
- Wrong port: API defaults to `localhost:8000`

---

## Resetting Local State

```bash
# Full reset (loses all data)
docker compose down -v
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
docker compose exec haas-api python -m app.seed_dev

# Rotate just the API key (keeps tenant/data)
docker compose exec haas-api python -m app.seed_dev --rotate-key
```

---

## Production Notes

For production, you **must** configure:
- `HAAS_OIDC_ISSUER`, `HAAS_OIDC_AUDIENCE`, `HAAS_OIDC_JWKS_URL`
- `HAAS_BOOTSTRAP_TENANT_SLUG`, `HAAS_BOOTSTRAP_ADMIN_ISSUER`, `HAAS_BOOTSTRAP_ADMIN_SUBJECT`
- Unique `HAAS_SECRET_ENCRYPTION_KEY` (32-byte base64)
- `HAAS_ENVELOPE_SIGNING_KEY` / `HAAS_ENVELOPE_VERIFY_KEY` (Ed25519)
- Production Postgres DSNs with restricted logins

See `.env.example` and [operations guide](docs/operations/backup-dr.md).