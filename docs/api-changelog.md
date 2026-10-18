# API changelog

User-visible contract changes, newest first. Policy: [api-deprecation-policy.md](api-deprecation-policy.md).

## Unreleased

### Added

- **OpenAI-compatible facade**:
  - `POST /v1/chat/completions` (streaming and non-streaming), `POST /v1/responses`,
    `GET /v1/responses/{id}` and `GET /v1/models`.
  - Requests go through the same admission, policy, budget and idempotency path as
    `POST /v1/runs`.
  - Errors use the OpenAI error envelope; settled-run failures carry
    `x-should-retry: false`.
  - The `openai-compat.v1` capability flag is set on `GET /v1/me`.
- **Live output deltas**:
  - `GET /v1/runs/{id}/stream?output_deltas=true` multiplexes advisory
    `output_delta` frames (`run-output-delta.v1`).
  - Advertised through the `output-deltas.v1` capability when
    `OUTPUT_DELTA_ENABLED=true`.
- `run_error` events carry a stable machine `code` (see the `run-events.v1` schema).
- Terminal webhooks carry `schema: run-terminal-webhook.v1` and, for failed runs,
  `code`. A cancelled run's `error` is never null.
- Run usage records `completion_tokens` alongside `prompt_tokens`.
- Published contracts: `docs/contracts/*.schema.json` (JSON Schema 2020-12) and
  `docs/asyncapi.yaml` (AsyncAPI 3.1).

### Changed (security tightening, allowlisted)

- `POST /v1/runs/{run_id}/resume`: `rejection_reason` is limited to 2000 characters,
  because the reason is fed back to the model (threat model: prompt-injection
  surface and cost amplification).
