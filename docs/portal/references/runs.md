# API Reference: Runs

Complete reference for the `/v1/runs` endpoints.

## Create Run

```http
POST /v1/runs
Idempotency-Key: <unique-key>
X-Tenant-ID: <tenant-id>
Authorization: Bearer <token>
Content-Type: application/json

{
  "prompt": "string",
  "thread_id": "string|null",
  "preload_skills": ["string"],
  "primary_model": "string|null",
  "planner_model": "string|null",
  "max_steps": "integer|null",
  "max_cost_usd": "number|null",
  "secrets": {"key": "value"},
  "agent_revision_id": "uuid|null",
  "expected_history_version": "integer|null"
}
```

**Response**: `202 Accepted`
```json
{
  "run_id": "uuid",
  "status": "queued",
  "thread_id": "string",
  "created_at": "timestamp"
}
```

## Get Run

```http
GET /v1/runs/{run_id}
X-Tenant-ID: <tenant-id>
Authorization: Bearer <token>
```

**Response**: `200 OK`
```json
{
  "run_id": "uuid",
  "status": "running",
  "thread_id": "string",
  "result": "string|null",
  "step_count": 5,
  "total_cost_usd": 0.023,
  "event_sequence": 42,
  "approval": {
    "batch_id": "uuid",
    "batch_digest": "sha256...",
    "expires_at": "timestamp",
    "tool_calls": [...]
  } | null,
  "created_at": "timestamp"
}
```

## List Runs

```http
GET /v1/runs?limit=20&cursor=<cursor>
X-Tenant-ID: <tenant-id>
Authorization: Bearer <token>
```

**Response**: `200 OK`
```json
{
  "items": [...],
  "next_cursor": "string|null"
}
```

## Cancel Run

```http
POST /v1/runs/{run_id}/cancel
X-Tenant-ID: <tenant-id>
Authorization: Bearer <token>
```

**Response**: `200 OK` with updated run object

## Resume Run (Approval Resolution)

```http
POST /v1/runs/{run_id}/resume
X-Tenant-ID: <tenant-id>
Authorization: Bearer <token>
Content-Type: application/json

{
  "approved": true,
  "rejection_reason": "string|null",
  "secrets": {"key": "value"},
  "batch_id": "uuid",
  "batch_digest": "sha256..."
}
```

**Response**: `200 OK` with updated run object

## Run Events (Durable Replay)

```http
GET /v1/runs/{run_id}/events?after=0&limit=100
X-Tenant-ID: <tenant-id>
Authorization: Bearer <token>
```

**Response**: `200 OK`
```json
{
  "items": [
    {
      "sequence": 1,
      "type": "run_started",
      "data": {"thread_id": "..."}
    }
  ],
  "next_after": 42,
  "latest_sequence": 100
}
```

## Run Stream (SSE)

```http
GET /v1/runs/{run_id}/stream?output_deltas=true
Last-Event-ID: 42
X-Tenant-ID: <tenant-id>
Authorization: Bearer <token>
Accept: text/event-stream
```

**Response**: `text/event-stream`
```
id: 43
event: run_step_started
data: {"step_id": "...", "attempt": 1}

id: 44
event: run.output.delta
data: {"delta": "Hello", "index": 0}

id: 45
event: run_completed
data: {"result": "Hello, world!", "total_cost_usd": 0.01}
```

## Run Steps

```http
GET /v1/runs/{run_id}/steps
X-Tenant-ID: <tenant-id>
Authorization: Bearer <token>
```

**Response**: `200 OK`
```json
{
  "items": [
    {
      "step_id": "uuid",
      "run_id": "uuid",
      "attempt": 1,
      "status": "completed",
      "plan": {...},
      "tool_calls": [...],
      "started_at": "timestamp",
      "completed_at": "timestamp"
    }
  ]
}
```

## Run Usage

```http
GET /v1/runs/{run_id}/usage
X-Tenant-ID: <tenant-id>
Authorization: Bearer <token>
```

**Response**: `200 OK`
```json
{
  "run_id": "uuid",
  "model_tokens": {
    "prompt": 1500,
    "completion": 500,
    "total": 2000
  },
  "tool_invocations": 3,
  "egress_bytes": 1024,
  "artifact_bytes": 2048,
  "cost_usd": 0.023
}
```

## Run Tree (Multi-Agent)

```http
GET /v1/runs/{run_id}/tree
X-Tenant-ID: <tenant-id>
Authorization: Bearer <token>
```

**Response**: `200 OK`
```json
{
  "run_id": "uuid",
  "root_run_id": "uuid",
  "depth": 0,
  "children": [
    {
      "run_id": "uuid",
      "root_run_id": "uuid",
      "depth": 1,
      "children": [],
      "agent_revision_id": "uuid",
      "status": "completed",
      "plan": {...},
      "cost_usd": 0.01,
      "created_at": "timestamp"
    }
  ],
  "agent_revision_id": "uuid",
  "status": "completed",
  "plan": {...},
  "cost_usd": 0.05,
  "created_at": "timestamp"
}
```

## Error Responses

All endpoints may return:

| Status | Code | Description |
|--------|------|-------------|
| 400 | `invalid_request` | Malformed request body |
| 401 | `unauthorized` | Missing or invalid credentials |
| 403 | `forbidden` | Insufficient permissions |
| 404 | `not_found` | Run not found |
| 409 | `idempotency_conflict` | Idempotency key mismatch |
| 422 | `validation_error` | Request validation failed |
| 429 | `rate_limited` | Quota exceeded |
| 500 | `internal_error` | Server error |

## Rate Limits

- `X-RateLimit-Limit`: Requests per minute
- `X-RateLimit-Remaining`: Remaining in window
- `X-RateLimit-Reset`: Unix timestamp of reset
- `Retry-After`: Seconds until retry (on 429)

## Idempotency

Use `Idempotency-Key` header for safe retries:
- Key must be unique per tenant
- Replay returns original response
- Digest mismatch returns `409 Conflict`