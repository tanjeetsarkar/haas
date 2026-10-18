# Cookbook: Approval Workflows

Common patterns for implementing approval gates in HaaS.

## Basic Approval Flow

```python
from haas_sdk import Haas

haas = Haas(api_key="hk_...", tenant="my-tenant")

# Create a run that will require approval
run = haas.runs.create(
    prompt="Deploy to production",
    max_steps=5,
    secrets={"DEPLOY_KEY": "..."}
)

# Poll for approval
while True:
    run = haas.runs.get(run["run_id"])
    if run.get("approval"):
        break
    if run["status"] in ("completed", "failed", "cancelled"):
        break
    time.sleep(2)

# Handle approval
if run.get("approval"):
    approval = run["approval"]
    print(f"Approval required: {approval['batch_id']}")
    print(f"Tool calls: {approval['tool_calls']}")
    
    # Review and decide
    haas.approvals.decide(
        batch_id=approval["batch_id"],
        approve=True,  # or False
        batch_digest=approval["batch_digest"],
        reason="Approved by on-call engineer"
    )
```

## Approval with Custom Secrets

```python
# Approve with additional runtime secrets
haas.approvals.decide(
    batch_id=approval["batch_id"],
    approve=True,
    batch_digest=approval["batch_digest"],
    secrets={
        "PRODUCTION_DB_PASSWORD": "prod-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret"
    }
)
```

## Rejecting an Approval

```python
haas.approvals.decide(
    batch_id=approval["batch_id"],
    approve=False,
    batch_digest=approval["batch_digest"],
    reason="Security review failed: missing encryption"
)
```

## Multi-Agent Approval Cascade

In multi-agent workflows, child agent approvals bubble up to the root:

```python
# Parent run creates child delegations
# Each child may require approval
# All approvals appear in parent's approval list

approvals = haas.approvals.list(status="pending")
for approval in approvals["items"]:
    # Check which agent requested approval
    agent = approval["tool_calls"][0].get("agent", "unknown")
    print(f"Agent {agent} needs approval: {approval['batch_id']}")
    
    # Decide each one
    haas.approvals.decide(...)
```

## Approval Expiry Handling

Approvals have a TTL (default 24 hours). Expired approvals fail the run:

```python
# Check expiry
approval = run["approval"]
expires_at = datetime.fromisoformat(approval["expires_at"])
if datetime.utcnow() > expires_at:
    print("Approval expired - run will fail")
    # Run status becomes 'failed'
```

## Webhook Notifications for Approvals

Configure webhooks to receive approval notifications:

```python
# Create webhook endpoint
endpoint = haas.webhooks.endpoints.create(
    url="https://your-app.com/webhooks/haas",
    events=["approval_required", "approval_decided"]
)

# Verify webhook signature
from haas_sdk.webhooks import verify_webhook

@app.post("/webhooks/haas")
async def handle_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Haas-Signature")
    key_id = request.headers.get("X-Haas-Key-Id")
    
    event = verify_webhook(body, signature, key_id)
    
    if event.type == "approval_required":
        # Notify on-call engineer
        await notify_oncall(event.data)
```

## Batch Approval Decisions

Approve multiple batches at once:

```python
# Get all pending approvals for a run
approvals = haas.approvals.list(status="pending")
run_approvals = [a for a in approvals["items"] if a["run_id"] == run_id]

# Approve all
for approval in run_approvals:
    haas.approvals.decide(
        batch_id=approval["batch_id"],
        approve=True,
        batch_digest=approval["batch_digest"],
        reason="Batch approved by release manager"
    )
```

## Approval Audit Trail

All decisions are recorded in the audit log:

```python
# Query audit events for approvals
audit = haas.audit_events.list(
    limit=100,
    cursor=None
)

for event in audit["items"]:
    if event["action"] == "approval.decide":
        print(f"{event['timestamp']}: {event['actor']} "
              f"{event['decision']} batch {event['batch_id']}")
```

## Testing Approvals Locally

```bash
# Start local stack
haas dev up
haas dev seed

# Create run requiring approval
haas runs create "Deploy to staging" --max-steps 3

# Check for approval
haas approvals list

# Approve via CLI
haas approvals decide <batch_id> --approve --batch-digest <digest>
```