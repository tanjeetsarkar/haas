# Quickstart: Run Your First Agent

This guide walks you through running your first agent on HaaS in under 5 minutes.

## Prerequisites

- HaaS CLI installed (`pip install haas-cli` or from source)
- Access to a HaaS instance (local or remote)

## Step 1: Configure Your Profile

```bash
# For local development
haas profile add local \
  --url http://localhost:18000 \
  --tenant default-tenant \
  --environment dev \
  --activate

# For production
haas profile add prod \
  --url https://api.haas.example \
  --tenant my-tenant \
  --environment prod \
  --activate
```

## Step 2: Set Credentials

```bash
# Using API key
export HAAS_API_KEY=hk_your_api_key

# Or using OIDC token
export HAAS_BEARER_TOKEN=eyJhbGciOiJSUzI1NiIs...
```

## Step 3: Run Your First Agent

```bash
# Simple one-shot run
haas runs create "What is the capital of France?" --max-steps 1

# With streaming output
haas runs create "Write a haiku about Kubernetes" --max-steps 3
haas runs stream <run_id>
```

## Step 4: Check Results

```bash
# Get run status
haas runs get <run_id>

# List recent runs
haas runs list --limit 10

# View run events
haas runs events <run_id>
```

## Step 5: Use an Agent Revision

```bash
# List available agent revisions
haas agents list

# Create a run with a specific agent
haas runs create "Analyze this code" --agent-revision-id <revision_id>
```

## Next Steps

- [Multi-agent workflows](./multi-agent.md)
- [Approval gates](./approvals.md)
- [Budget management](./budget.md)
- [Thread history](./threads.md)