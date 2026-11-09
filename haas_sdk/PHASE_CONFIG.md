# `phase_config` SDK field

Python and TypeScript run-create requests may explicitly include the generic
`phase_config` object:

```json
{
  "version": 1,
  "mode": "bounded_acquisition_finalization",
  "reserve_basis_points": 2000
}
```

Omitting the field preserves legacy behavior; the SDKs do not inject `null` or
an implicit configuration. `PhaseConfig` types accept only this v1 shape.
Sending it is an opt-in request, not proof that the server supports or activates
bounded acquisition/finalization. Server capability/version activation must be
coordinated separately before use. The current server intentionally rejects
requests containing `phase_config` (including `null`) with HTTP 422; SDK wiring
does not bypass that hold or advertise readiness.
