# Billing Metrics Reference

## Definitions

- **Gross revenue**: sum of `paid` invoice amounts in the period.
- **Refunded**: sum of `refunded` invoice amounts in the period.
- **Net revenue**: gross revenue minus refunded.
- **MoM change**: `(month - previous month) / previous month`, per segment.
- **Anomaly**: MoM swing greater than 15% in either direction, or a customer with more than
  one `paid` invoice for the same month and identical amount (suspected duplicate charge).

## Refund policy

- Only duplicate or erroneous charges may be refunded automatically.
- A refund batch holds at most 50 invoices and must be approved by a human operator.
- Always pass the exact `invoice_id` values returned by `query_revenue_metrics`.
