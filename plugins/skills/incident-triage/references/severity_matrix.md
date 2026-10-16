# Severity matrix

`classify_incident_severity` applies these rules in order (first match wins).

| Severity | Triggers | Response | Updates | Postmortem |
|---|---|---|---|---|
| **SEV1** | data loss/corruption or confirmed security breach; **or** core function down for ≥ 50 % of users; **or** major revenue impact | Page immediately; incident commander + comms lead + scribe; bridge open | every 15 min | required |
| **SEV2** | core function down (< 50 % of users); **or** ≥ 25 % of users affected; **or** moderate revenue impact; **or** ≥ 60 min impact on ≥ 10 % of users with no workaround | Page on-call; incident commander; stakeholders notified | every 30 min | required |
| **SEV3** | ≥ 1 % of users affected or minor revenue impact; **or** SEV2 triggers mitigated by a workaround (when the core function is up and revenue impact is not moderate) | Owning team during business hours | every 4 h or on change | optional |
| **SEV4** | negligible impact, cosmetic, internal only | Normal backlog | none | no |

## Input definitions

- **users_affected_pct**: share of *active* users currently experiencing errors, not total registered users.
  Estimate from error rate on the affected path (failed requests ÷ total requests), say if it is a rough guess.
- **core_function_down**: a primary user journey (sign-in, checkout/payment, main API, data ingestion) cannot be completed.
- **data_loss_or_security**: confirmed, not merely suspected. Suspected → treat as SEV1 for triage but flag as unconfirmed.
- **revenue_impact**: none / minor (<1 % of hourly revenue) / moderate (1–10 %) / major (>10 % or contractual SLA breach).
- **workaround_available**: users can complete the task another way without support help.

Re-classify when facts change; upgrades are immediate, downgrades only after mitigation is confirmed.
