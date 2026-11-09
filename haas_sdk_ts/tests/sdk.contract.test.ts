import { afterEach, describe, expect, expectTypeOf, it, vi } from 'vitest';
import * as sdk from '../src/index';
import type {
  ClientOptions, StreamOptions, SSEEvent, RunCreateRequest, RunResponse,
  RunEvent, RunEventsResponse, HaaSAPIError, HaaSConnectionError,
  HaaSStreamClosedError,
} from '../src/index';
import { HaaSAPIError as APIError } from '../src/types';

// Synthetic identifiers/times, not captured production values. Field names and
// shapes come from app/main.py's producers and the current OpenAPI/event schemas.
const runId = 'run_0123456789abcd';
const threadId = 'thd_0123456789ab';
const createdAt = Date.parse('2026-11-08T00:00:00Z') / 1000;
const admitted = {
  run_id: runId, thread_id: threadId, status: 'queued' as const,
  stream_url: `/v1/runs/${runId}/stream`,
  events_url: `/v1/runs/${runId}/events`,
  created_at: createdAt,
} satisfies RunResponse & { stream_url: string; events_url: string };
const queued = {
  sequence: 1, event_id: '00000000-0000-4000-8000-000000000001',
  schema_version: 1, type: 'run_queued', run_id: runId,
  occurred_at: new Date(createdAt * 1000).toISOString(),
  data: { run_id: runId, thread_id: threadId },
} satisfies RunEvent & {
  event_id: string; schema_version: number; run_id: string; occurred_at: string;
};
const completed = {
  ...queued, sequence: 2,
  event_id: '00000000-0000-4000-8000-000000000002',
  type: 'run_completed', data: { output: 'done' },
};
const json = (body: unknown, status = 200) => new Response(
  JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } },
);
const mockFetch = () => {
  const fetch = vi.fn<Parameters<typeof globalThis.fetch>, ReturnType<typeof globalThis.fetch>>();
  vi.stubGlobal('fetch', fetch);
  return fetch;
};
const client = (options: Partial<ClientOptions> = {}) => new sdk.HaaSClient({
  baseUrl: 'https://harness.example/', ...options,
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('root exports and public types', () => {
  it('keeps existing runtime exports importable without exercising the facade', () => {
    for (const name of [
      'HaaSClient', 'createClientFromEnv',
      'AtlasSessionManager', 'createAtlasSessionManager',
    ] as const) {
      expect(sdk[name]).toBeTypeOf('function');
    }
  });

  it('accepts root type-only options, models and error imports', () => {
    expectTypeOf<ClientOptions>().toMatchTypeOf<{ baseUrl: string }>();
    expectTypeOf<{ baseUrl: string }>().toMatchTypeOf<ClientOptions>();
    // The required baseUrl cannot be omitted.
    // @ts-expect-error baseUrl is required by the public options contract
    const missingBaseUrl: ClientOptions = {};
    void missingBaseUrl;
    expectTypeOf<RunCreateRequest>().toMatchTypeOf<{ prompt: string }>();
    expectTypeOf<RunResponse>().toMatchTypeOf<{ created_at: number }>();
    expectTypeOf<RunEvent>().toMatchTypeOf<{ sequence: number }>();
    expectTypeOf<HaaSAPIError>().toMatchTypeOf<Error & { status: number }>();
    expectTypeOf<HaaSConnectionError>().toMatchTypeOf<Error>();
    expectTypeOf<HaaSStreamClosedError>().toMatchTypeOf<Error>();
  });
});

describe('configuration and request serialization', () => {
  it('accepts all options and merges headers with tenant and API key', async () => {
    const fetch = mockFetch().mockResolvedValue(json({ items: [], next_cursor: null }));
    const options: ClientOptions = {
      baseUrl: 'https://harness.example/', apiKey: 'test-only-key', tenant: 'tenant-a',
      timeout: 1234, maxRetries: 0, streamMaxReconnects: 0, streamReconnectDelay: 1,
      defaultHeaders: { 'X-Contract': 'yes', 'Content-Type': 'application/custom' },
    };
    const timeout = vi.spyOn(globalThis, 'setTimeout');
    await new sdk.HaaSClient(options).listRuns({ limit: 2, cursor: 'a+/= b' });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith(
      'https://harness.example/v1/runs?limit=2&cursor=a%2B%2F%3D+b',
      expect.objectContaining({
        method: 'GET', body: undefined,
        headers: {
          'X-Tenant-ID': 'tenant-a', 'Content-Type': 'application/custom',
          'X-Contract': 'yes', Authorization: 'Bearer test-only-key',
        },
        signal: expect.any(AbortSignal),
      }),
    );
    expect(timeout).toHaveBeenCalledWith(expect.any(Function), 1234);
  });

  it('uses default tenant, JSON headers and timeout, omitting undefined queries', async () => {
    const fetch = mockFetch().mockResolvedValue(json({ items: [], next_cursor: null }));
    const timeout = vi.spyOn(globalThis, 'setTimeout');
    await client().listRuns({ cursor: undefined });
    expect(fetch).toHaveBeenCalledWith('https://harness.example/v1/runs',
      expect.objectContaining({
        headers: { 'X-Tenant-ID': 'default-tenant', 'Content-Type': 'application/json' },
      }));
    expect(timeout).toHaveBeenCalledWith(expect.any(Function), 30000);
  });

  it('reads only the supported explicit environment and uses the localhost fallback', async () => {
    vi.spyOn(console, 'log').mockImplementation(() => {});
    const fetch = mockFetch().mockImplementation(async () => json({}));
    await sdk.createClientFromEnv({
      HAAS_API_URL: 'https://env.example/', HAAS_API_KEY: 'env-test-key',
      HAAS_TENANT_ID: 'tenant-env',
    }).health();
    expect(fetch).toHaveBeenNthCalledWith(1, 'https://env.example/readyz',
      expect.objectContaining({ headers: {
        'X-Tenant-ID': 'tenant-env', 'Content-Type': 'application/json',
        Authorization: 'Bearer env-test-key',
      } }));
    await sdk.createClientFromEnv({}).health();
    expect(fetch).toHaveBeenNthCalledWith(2, 'http://localhost:8000/readyz',
      expect.objectContaining({ headers: {
        'X-Tenant-ID': 'default-tenant', 'Content-Type': 'application/json',
      } }));
  });
});

describe('asynchronous calls and producer-backed JSON responses', () => {
  it('returns Promises for JSON and an AsyncGenerator for SSE', async () => {
    const fetch = mockFetch().mockResolvedValue(json(admitted, 202));
    const api = client();
    expectTypeOf<ReturnType<typeof api.createRun>>().toEqualTypeOf<Promise<RunResponse>>();
    expectTypeOf<ReturnType<typeof api.listRuns>>().toEqualTypeOf<
      Promise<{ items: RunResponse[]; next_cursor?: string }>
    >();
    expectTypeOf<ReturnType<typeof api.getRunEvents>>()
      .toEqualTypeOf<Promise<RunEventsResponse>>();
    expectTypeOf<ReturnType<typeof api.streamEvents>>()
      .toEqualTypeOf<AsyncGenerator<SSEEvent, void, unknown>>();
    const result = api.createRun({ prompt: 'qa' });
    expect(result).toBeInstanceOf(Promise);
    await result;
    const options: StreamOptions = { lastEventId: '0', outputDeltas: false };
    const stream = api.streamEvents(runId, options);
    expect(stream[Symbol.asyncIterator]()).toBe(stream);
    expect(stream.next).toBeTypeOf('function');
    expect(fetch).toHaveBeenCalledTimes(1); // Generator is lazy, not a Promise.
    await stream.return();
  });

  it('preserves the admitted response including persisted numeric created_at and URLs', async () => {
    // app/main.py create_run; CreateRunResponse in docs/openapi-v1.json.
    const fetch = mockFetch().mockResolvedValue(json(admitted, 202));
    const request: RunCreateRequest = { prompt: 'qa', thread_id: threadId };
    const response = await client().createRun(request, 'contract-create');
    expect(response).toEqual(admitted);
    expect(response.created_at).toBe(createdAt);
    expect(response.created_at).toBeTypeOf('number');
    expect(fetch).toHaveBeenCalledWith('https://harness.example/v1/runs',
      expect.objectContaining({
        method: 'POST', body: JSON.stringify(request),
        headers: {
          'X-Tenant-ID': 'default-tenant', 'Content-Type': 'application/json',
          'Idempotency-Key': 'contract-create',
        },
      }));
  });

  it('preserves list null cursor and the complete durable paged event envelope', async () => {
    // app/main.py list_runs/list_run_events; run-events.v1.schema.json.
    const page = { items: [queued], next_after: 1, latest_sequence: 1 };
    const fetch = mockFetch()
      .mockResolvedValueOnce(json({ items: [admitted], next_cursor: null }))
      .mockResolvedValueOnce(json(page));
    const api = client();
    expect(await api.listRuns({ limit: 1 }))
      .toEqual({ items: [admitted], next_cursor: null });
    expect(await api.getRunEvents(runId, { after: 0, limit: 1 })).toEqual(page);
    expect(fetch).toHaveBeenNthCalledWith(1, 'https://harness.example/v1/runs?limit=1',
      expect.objectContaining({ method: 'GET' }));
    expect(fetch).toHaveBeenNthCalledWith(2,
      `https://harness.example/v1/runs/${runId}/events?after=0&limit=1`,
      expect.objectContaining({ method: 'GET' }));
  });
});

describe('terminal cancel problem mapping', () => {
  it('maps the producer 409 problem to the existing error without retrying', async () => {
    // app/main.py cancel_run + app/core/http.py problem_response.
    const detail = 'Run is already terminal.';
    const problem = {
      type: 'about:blank', title: 'Conflict', status: 409, detail,
      instance: `/v1/runs/${runId}/cancel`,
    };
    const fetch = mockFetch().mockResolvedValue(new Response(JSON.stringify(problem), {
      status: 409, headers: { 'Content-Type': 'application/problem+json' },
    }));
    const error = await client().cancelRun(runId).catch((error: unknown) => error);
    expect(error).toBeInstanceOf(APIError); // Root exports errors as types only.
    expect(error).toMatchObject({
      name: 'HaaSAPIError', status: 409, message: detail, details: detail,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith(
      `https://harness.example/v1/runs/${runId}/cancel`,
      expect.objectContaining({ method: 'POST' }),
    );
  });
});

describe('durable SSE and live-only output', () => {
  it('parses id/name/data and reconnects with the durable cursor, not a live delta', async () => {
    // app/main.py stream_run + AsyncAPI and both run event/delta schemas.
    const delta = {
      schema: 'run-output-delta.v1', step: 1, attempt: 1, call: 1,
      index: 0, text: 'preview', final: false, truncated: false,
    };
    const frame = (event: typeof queued | typeof completed) =>
      `id: ${event.sequence}\nevent: ${event.type}\ndata: ${JSON.stringify(event.data)}\n\n`;
    const sse = (body: string) => new Response(body, {
      headers: { 'Content-Type': 'text/event-stream' },
    });
    const fetch = mockFetch()
      .mockResolvedValueOnce(sse(
        `: keep-alive\n\n${frame(queued)}event: output_delta\ndata: ${JSON.stringify(delta)}\n\n`,
      ))
      .mockResolvedValueOnce(json({ ...admitted, status: 'running' }))
      .mockResolvedValueOnce(sse(frame(completed)))
      .mockResolvedValueOnce(json({ ...admitted, status: 'completed' }));
    const events: SSEEvent[] = [];
    for await (const event of client({ streamReconnectDelay: 1 }).streamEvents(runId, {
      lastEventId: '0', outputDeltas: true,
    })) events.push(event);
    expect(events).toEqual([
      { id: '1', event: 'run_queued', data: queued.data },
      { id: undefined, event: 'output_delta', data: delta },
      { id: '2', event: 'run_completed', data: completed.data },
    ]);
    for (const [call, cursor] of [[1, '0'], [3, '1']] as const) {
      expect(fetch).toHaveBeenNthCalledWith(call,
        `https://harness.example/v1/runs/${runId}/stream?output_deltas=true`,
        expect.objectContaining({ method: 'GET', headers: {
          'X-Tenant-ID': 'default-tenant', 'Content-Type': 'application/json',
          Accept: 'text/event-stream', 'Last-Event-ID': cursor,
        } }));
    }
    expect(fetch).toHaveBeenCalledTimes(4);
  });
});
