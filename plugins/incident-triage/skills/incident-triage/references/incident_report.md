# Incident report layout

```
STATUS: <Investigating | Identified | Mitigating | Monitoring | Resolved> — SEV<n> — as of <UTC time>

IMPACT
- Who/what is affected, since when, how many (%), workaround?

TIMELINE (UTC)
- hh:mm  first error (<signature>)           ← from analyze_log_text
- hh:mm  error spike (<count> errors/bucket)
- hh:mm  <detection / page / mitigation>     ← only facts the user gave

EVIDENCE
- <signature> ×<count> (first … last …)
- Exceptions: <top types>

HYPOTHESES (ranked)
1. <cause> — evidence: … — disproved if: … — next check: …

NEXT ACTIONS
- Mitigate: …
- Diagnose: …
- Communicate: next update in <n> min (from severity result); owner: <role>
```

Keep it to one screen. Put unknowns in a final line: `UNKNOWN: …`.
