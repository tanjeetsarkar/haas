---
name: revenue-ops
description: Investigates revenue anomalies, refund exposure and billing-system sync for SaaS subscriptions. Use when the user asks about quarterly revenue, MRR changes, duplicate or failed charges, refunds, or notifying the billing system.
license: Apache-2.0
compatibility: Requires the haas tool workers with the analytics DB secret supplied per run.
metadata:
  version: "1.0.0"
  required_tools: "query_revenue_metrics prepare_refund_batch lookup_customer_ledger notify_billing_system"
  allowed_secrets: "ANALYTICS_DB_PATH BILLING_WEBHOOK_SECRET"
---

# Revenue Operations SOP

Follow these steps in order. Do not skip the read-only investigation.

1. Call `query_revenue_metrics` for the requested quarter (format `YYYY-Qn`). Optionally call it
   again with a `segment` to drill into one customer segment. Anything flagged `anomaly: true`
   has a month-over-month swing larger than 15% or contains duplicate charges.
2. Read `billing_metrics.md` via `read_skill_reference` if you need the definition of a metric
   or the refund policy.
3. For suspicious customers, call `lookup_customer_ledger` (read-only) to confirm duplicates.
4. If duplicate or erroneous charges are confirmed, call `prepare_refund_batch` with the exact
   `invoice_ids`. This tool **requires human approval**; the run pauses until an operator responds.
5. After a refund batch is approved and prepared, call `notify_billing_system` with the batch id
   (also approval-gated).
6. Finish with a concise summary: findings, refunded invoices, total USD, anything rejected.

Never invent invoice ids; only use ids returned by the tools.
