# HaaS Documentation Portal

Welcome to the HaaS (Harness-as-a-Service) documentation portal. This portal provides comprehensive documentation for developers, operators, and platform administrators.

## Quick Navigation

- [Quickstarts](./quickstarts/) - Get started in 5 minutes
- [API Reference](./references/) - Complete API documentation
- [Cookbook](./cookbook/) - Common patterns and recipes
- [Migration Guides](./migration/) - Version upgrade guides
- [Changelog](./changelog/) - Release history

## Overview

HaaS is a multi-tenant platform for running AI agents with durable workflows, approval gates, budget controls, and a plugin marketplace. It provides:

- **Durable Execution**: PostgreSQL-backed workflow state with outbox relay
- **Multi-tenancy**: Row-level security, tenant isolation, quota management
- **Approval Gates**: Human-in-the-loop with digest-bound decisions
- **Budget Controls**: Pre-dispatch reservations with NUMERIC ledger
- **Plugin Marketplace**: Content-addressed OCI artifacts with trust tiers
- **Multi-Agent**: HACP, A2A, MCP, and OpenAI-compatible protocols
- **Developer Experience**: Python/TypeScript SDKs, unified CLI, local dev stack

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        API Gateway                          │
│  (FastAPI, OIDC/JWT, API Keys, Rate Limiting)              │
└──────────────────────────┬──────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│  Orchestrator │  │  Tool Workers │  │ Maintenance   │
│  (Celery)     │  │  (Celery)     │  │  Service      │
└───────┬───────┘  └───────┬───────┘  └───────┬───────┘
        │                  │                  │
        └──────────────────┼──────────────────┘
                           ▼
              ┌───────────────────────┐
              │    PostgreSQL         │
              │  (Authoritative)      │
              │  Runs, Events,        │
              │  Outbox, Budget,      │
              │  Artifacts, etc.      │
              └───────────────────────┘
```

## Getting Started

### Prerequisites
- Docker & Docker Compose
- Python 3.12+ (for SDK/CLI)
- Node.js 18+ (for TypeScript SDK)

### Local Development
```bash
# Clone and start local stack
git clone https://github.com/your-org/haas
cd haas
haas dev up
haas dev seed

# Run a test agent
haas runs create "Hello, world!" --max-steps 1
```

### Production Deployment
```bash
# Using Terraform + Helm
cd deploy/terraform/environments/prod
terraform init
terraform apply

# Or Helm directly
helm install haas ./deploy/helm/haas -f values-prod.yaml
```

## Core Concepts

### Runs
A run is a single agent execution with durable state. Runs progress through states:
`queued` → `running` → `completed` | `failed` | `cancelled` | `input_required`

### Threads
Threads provide persistent conversation history with branching and version control.

### Agents
Agents are versioned configurations (model, skills, tools) that can be drafted, validated, and published.

### Approvals
Human-in-the-loop gates that pause runs until a decision is made. Decisions are digest-bound and immutable.

### Budget
Every run reserves budget before dispatch. Costs are tracked in a NUMERIC ledger with reserved/settled/released states.

## Next Steps

1. [Quickstart: Run your first agent](./quickstarts/first-agent.md)
2. [Quickstart: Multi-agent workflow](./quickstarts/multi-agent.md)
3. [API Reference: Runs](./references/runs.md)
4. [Cookbook: Approval workflows](./cookbook/approvals.md)