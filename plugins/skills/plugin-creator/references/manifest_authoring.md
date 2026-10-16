# Writing an external-tool manifest

File: `plugins/manifests/<name>.json` — one object or a list. Use it when a service already speaks HTTP/MCP.

```json
{
  "name": "lookup_customer_ledger",
  "description": "Read-only lookup of a customer's billing ledger via the billing MCP server.",
  "protocol": "mcp_http",
  "endpoint_url": "${BILLING_MCP_URL:-https://mcp.billing.example.com/mcp}",
  "secret_key": "BILLING_WEBHOOK_SECRET",
  "requires_approval": false,
  "queue": "q_tools_fast",
  "parameters": {
    "type": "object",
    "properties": {
      "customer": {"type": "string", "description": "Customer slug, e.g. acme."},
      "month": {"type": "string", "description": "Optional month filter, YYYY-MM."}
    },
    "required": ["customer"]
  }
}
```

| Field | Notes |
|---|---|
| `protocol` | `http`: POST the JSON arguments as the body. `mcp_http`: JSON-RPC `tools/call` (MCP `2026-07-28`, JSON or SSE replies). |
| `endpoint_url` | Public http(s) URL; `${VAR:-default}` expands from the environment at load time. Private/internal hosts are blocked (dev only: `SSRF_ALLOWED_HOSTS`). |
| `secret_key` | Name of a run secret; requests get `Authorization` bearer plus `X-HaaS-Signature-256` (HMAC-SHA256 of the sorted JSON payload). Must be in a skill's `allowed_secrets`. |
| `requires_approval` | `true` for state-changing endpoints. |
| `queue` | `q_tools_fast` or `q_tools_sandbox`. |
| `parameters` | JSON-Schema subset: property `type` ∈ string/integer/number/boolean/object/array, `description`, `default`, `required[]`. Descriptions are prompt text. |

Remote errors (`isError`, JSON-RPC `error`, HTTP ≥ 400) are surfaced to the model; make the remote service return
actionable messages. Timeout is 20 s per call. Unlock the tool by listing its `name` in a skill's `required_tools`.
