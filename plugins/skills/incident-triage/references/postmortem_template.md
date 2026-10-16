# Blameless postmortem template

**Title:** <short, factual>  **Severity:** SEV<n>  **Date:** <UTC>  **Author / reviewers:** <names>

## Summary
Two or three sentences: what happened, impact, duration, how it was resolved.

## Impact
Users/regions/functions affected, percentage and duration, revenue or SLA effect, data impact.

## Timeline (UTC)
| Time | Event | Source |
|---|---|---|

## Root cause and contributing factors
- Trigger (what changed):
- Root cause (why it failed):
- Contributing factors (why it wasn't caught / why it was worse):

## Detection and response
How it was detected, time to detect, time to mitigate, what worked, what slowed the response.

## What went well / what went poorly / where we got lucky

## Action items
| Action | Type (prevent / detect / mitigate) | Owner | Due | Ticket |
|---|---|---|---|---|

Principles: blameless language (systems and decisions, not people); every action item has one owner and a date;
prefer actions that remove the failure class over actions that add a reminder.
