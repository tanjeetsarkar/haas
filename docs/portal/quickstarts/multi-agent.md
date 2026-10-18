# Quickstart: Multi-Agent Workflows

Learn how to run multi-agent workflows with HaaS.

## Prerequisites

- Completed [First Agent](./first-agent.md) quickstart
- Understanding of agent delegation concepts

## Concepts

### Delegation
A parent agent can spawn child agents with attenuated permissions and budgets.

### Topologies
Pre-defined coordination patterns:
- **Supervisor**: One coordinator manages multiple workers
- **Hierarchical**: Tree of agents with parent-child relationships
- **Swarm**: Peer agents collaborating on a task
- **Graph**: Custom DAG of agent interactions
- **Map-Reduce**: Parallel workers with aggregation
- **Debate**: Agents arguing different positions

## Step 1: Create a Topology

```bash
# Create a supervisor topology
haas agents topology create \
  --name "code-review" \
  --type supervisor \
  --agent-revisions "rev_planner,rev_reviewer,rev_fixer" \
  --config '{"max_iterations": 3}'
```

## Step 2: Run with Topology

```bash
# Create a run using the topology
haas runs create "Review this PR for security issues" \
  --agent-revision-id <supervisor_revision_id> \
  --secrets GITHUB_TOKEN=ghp_xxx
```

## Step 3: Monitor the Run Tree

```bash
# View the run tree with all child agents
haas runs get <run_id> --tree

# Or use the observability endpoint
haas runs observability <run_id>
```

## Step 4: Handle Approvals

Child agents may require approvals that bubble up to the root:

```bash
# List pending approvals
haas approvals list

# Approve a batch
haas approvals decide <batch_id> --approve --batch-digest <digest>
```

## Step 5: Inspect Results

```bash
# View messages between agents
haas agent-mesh messages <run_id>

# View workspace state
haas agent-mesh workspace <run_id> <agent_id>
```

## Example: Code Review Workflow

```yaml
# topology-config.yaml
name: "security-code-review"
type: "supervisor"
agent_revisions:
  - "rev_planner"      # Plans the review
  - "rev_static_analyzer"  # Runs static analysis
  - "rev_reviewer"     # Reviews findings
  - "rev_fixer"        # Suggests fixes
config:
  max_iterations: 3
  require_approval: true
  budget_usd: 5.00
```

```bash
# Deploy topology
haas agents topology create --file topology-config.yaml

# Run review
haas runs create "Review PR #123 for security vulnerabilities" \
  --agent-revision-id <topology_supervisor_rev> \
  --secrets GITHUB_TOKEN=ghp_xxx
```

## Next Steps

- [Approval workflows](./approvals.md)
- [Agent workspace](./workspace.md)
- [Budget management](./budget.md)