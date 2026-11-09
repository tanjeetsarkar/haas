# API changelog

User-visible contract changes, newest first. Policy: [api-deprecation-policy.md](api-deprecation-policy.md).

## Unreleased

### Fixed

- Held bounded-phase finalizer failures persist their exact claimed-step marker
  atomically with settlement. Terminal events and webhooks retain the structured
  `phase.finalizer_tool_calls`, `phase.finalizer_failed`, or
  `phase.finalizer_outcome_unknown` code. Public phase admission remains disabled.

- Artifact CRUD uses the current tenant/retention schema, resolves slug and UUID
  tenant selectors consistently, validates run visibility before storing content,
  and preserves canonical audit attribution and quota accounting. Metadata
  retrieval/listing decodes stored JSON correctly; pagination accepts its emitted
  timestamp cursors and returns 422 for malformed cursors.
- `POST /v1/artifacts` accepts the public SDKs' JSON `request` multipart file
  part and JSON form strings alongside the `file` upload. File parts require
  strict UTF-8; form strings use Starlette's multipart charset decoding (UTF-8
  by default, with Latin-1 fallback). Metadata is limited to 64 KiB (UTF-8
  encoded size after decoding for form strings) and still validates against
  the existing artifact fields (unknown fields are rejected). Invalid metadata
  returns 422.
- Request tenant context is restored on success, error and cancellation without
  crossing threadpool contexts; authorization and database RLS remain unchanged.

### Added

- Successful `POST /v1/runs` responses include numeric epoch-seconds `created_at`
  from the persisted run; idempotent replays retain the same creation time.
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

- `POST /v1/runs`, `POST /v1/chat/completions`, and `POST /v1/responses`
  reject a present `phase_config` field, including `null`, before run admission;
  the facade also rejects it in `haas` options. Requests omitting it retain
  their existing behavior. This temporary guard does not activate phase mode
  or establish the final public opt-in field name.
- `POST /v1/runs/{run_id}/resume`: `rejection_reason` is limited to 2000 characters,
  because the reason is fed back to the model (threat model: prompt-injection
  surface and cost amplification).
