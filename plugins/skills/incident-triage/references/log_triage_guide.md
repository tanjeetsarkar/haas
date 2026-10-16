# Log triage guide

## Reading `analyze_log_text` output

| Field | How to use it |
|---|---|
| `first_error_at` | Start of the incident window; ask what changed (deploy/config/traffic) shortly before it. |
| `spike` | The bucket with ≥ 5 errors and ≥ 3× the median per-bucket errors (needs ≥ 3 buckets); compare to deploy/cron times. No spike + steady errors = chronic issue. |
| `top_signatures` | Same error with ids/numbers normalised (`<n>`, `<uuid>`, `<ip>`). Count × spread over time = blast radius. |
| `top_exceptions` | Exception classes seen in messages and stack frames; the deepest `Caused by` is usually the root. |
| `error_ratio` | ERROR+FATAL ÷ all records; compare against the usual baseline if the user knows it. |

## Signature → likely cause

| Signature contains | Likely cause | First check |
|---|---|---|
| `connection refused`, `ECONNREFUSED` | Dependency down / wrong port / not yet started | Is the target healthy? Recent restart or deploy? |
| `timeout`, `timed out`, `deadline exceeded` | Slow or saturated dependency, network, lock contention | Latency of the dependency; thread/connection pool saturation |
| `too many connections`, `pool exhausted`, `QueuePool limit` | Connection leak or traffic surge | Pool size vs. concurrency; long-running transactions |
| `OutOfMemoryError`, `OOMKilled`, `Cannot allocate memory` | Memory leak, undersized limit | Memory graph, recent traffic/payload change, restart loop |
| `429`, `rate limit`, `quota` | Upstream throttling, retry storm | Retry/backoff config; who is the heavy caller |
| `401`, `403`, `token expired`, `certificate` | Expired credential/cert, rotated secret | Expiry dates, recent rotation |
| `deadlock`, `lock wait timeout` | Conflicting transactions | Query/transaction ordering, long transactions |
| `no space left`, `disk full`, `ENOSPC` | Disk exhausted (logs, tmp, DB) | Disk usage by directory; log rotation |
| `NullPointerException`, `KeyError`, `TypeError` right after a deploy | Regression | Diff of the deploy; roll back candidate |
| `502`, `503`, `upstream`, `no healthy` | Load balancer has no healthy backends | Health checks, readiness, crash loops |
| `DNS`, `NXDOMAIN`, `Name or service not known` | Resolver/service discovery failure | DNS provider status, recent record changes |

## Mitigation order (prefer the cheapest reversible action)

1. Roll back the most recent change if the first error follows it closely.
2. Shed or rate-limit load; disable the failing feature flag.
3. Scale out / restart unhealthy instances (note: restarts can hide evidence; capture logs first).
4. Fail over only with an owner's confirmation.
5. Fix forward last.
