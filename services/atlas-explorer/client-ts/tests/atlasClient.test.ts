import { afterEach, describe, expect, it, vi } from 'vitest';
import { AtlasSessionManager, createAtlasSessionManager } from '../src';
import type { HaaSClient } from '@haas/sdk';

const sse = (event: string, id?: string) => ({ event, id, data: { marker: event } });
const generator = (...events: ReturnType<typeof sse>[]) => (async function* () {
  for (const event of events) yield event as never;
})();
const clientStub = (streamEvents = vi.fn(() => generator(sse('run_completed')))) => ({
  createThread: vi.fn(async () => ({ thread_id: 'thread-root' })),
  createRun: vi.fn(async () => ({ run_id: 'run-1' })),
  branchThread: vi.fn(async () => ({ thread_id: 'thread-branch' })),
  streamEvents,
  resumeRun: vi.fn(async () => ({ run_id: 'run-1' })),
  getRun: vi.fn(async () => ({ run_id: 'run-1' })),
  cancelRun: vi.fn(async () => ({ run_id: 'run-1' })),
});
const managerFor = (stub: ReturnType<typeof clientStub>) =>
  new AtlasSessionManager(stub as unknown as HaaSClient);
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('AtlasSessionManager legacy facade behavior', () => {
  it('constructs directly or through the factory and submits the root query unchanged', async () => {
    const stub = clientStub(vi.fn(() => generator(sse('run_completed', '1'))));
    const made = createAtlasSessionManager(stub as unknown as HaaSClient);
    expect(made).toBeInstanceOf(AtlasSessionManager);
    const direct = new AtlasSessionManager(stub as unknown as HaaSClient);
    expect(direct).toBeInstanceOf(AtlasSessionManager);

    const received = vi.fn();
    const result = await made.startRootExploration('Clinics in Mumbai', received);
    expect(result).toMatchObject({ threadId: 'thread-root', runId: 'run-1' });
    expect(typeof result.unsubscribe).toBe('function');
    expect(stub.createThread).toHaveBeenCalledWith({
      metadata: { app: 'haas-atlas', root_query: 'Clinics in Mumbai' },
    });
    expect(stub.createRun).toHaveBeenCalledWith({
      thread_id: 'thread-root',
      prompt: 'Layer 1 Root Query: Clinics in Mumbai. Emit atlas-layer.v1 JSON.',
      preload_skills: ['atlas-explorer', 'web-research', 'data-analyst'],
      max_steps: 6, max_cost_usd: 0.15, max_tool_invocations: 12,
    });
    await settle();
    expect(received).toHaveBeenCalledWith({
      sequence: 0, type: 'run_completed', data: { marker: 'run_completed' }, id: '1',
    });
    expect(stub.streamEvents).toHaveBeenCalledWith('run-1', { lastEventId: '0', outputDeltas: true });
  });

  it('branches at the requested version and preserves drill-down prompt and budgets', async () => {
    const stub = clientStub();
    const result = await createAtlasSessionManager(stub as unknown as HaaSClient).drillDownLayer({
      threadId: 'parent', parentBranchVersion: 17, parentNodeId: 'node-2',
      dimensionKey: 'specialties', prompt: 'Compare specialties',
    }, vi.fn());
    expect(result).toMatchObject({ branchedThreadId: 'thread-branch', runId: 'run-1' });
    expect(stub.branchThread).toHaveBeenCalledWith('parent', { expected_history_version: 17 });
    expect(stub.createRun).toHaveBeenCalledWith({
      thread_id: 'thread-branch',
      prompt: '[parent_node_id=node-2] [dimension=specialties] Compare specialties',
      preload_skills: ['atlas-explorer', 'web-research', 'data-analyst'],
      max_steps: 5, max_cost_usd: 0.10, max_tool_invocations: 8,
    });
  });

  it('reconnects after errors with the last cursor and stops at terminal events', async () => {
    vi.useFakeTimers();
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const broken = (async function* () {
      yield sse('output_delta', 'cursor-8') as never;
      throw new Error('connection lost');
    })();
    const stream = vi.fn()
      .mockReturnValueOnce(broken)
      .mockReturnValue(generator(sse('run_completed', 'cursor-9')));
    const stub = clientStub(stream);
    const received = vi.fn();
    const result = await managerFor(stub).startRootExploration('query', received);
    await settle();
    expect(received).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    await settle();
    expect(stream).toHaveBeenNthCalledWith(1, 'run-1', { lastEventId: '0', outputDeltas: true });
    expect(stream).toHaveBeenNthCalledWith(2, 'run-1', { lastEventId: 'cursor-8', outputDeltas: true });
    expect(received).toHaveBeenCalledTimes(2);
    expect(received.mock.calls[1]?.[0].type).toBe('run_completed');
    await vi.advanceTimersByTimeAsync(5000);
    expect(stream).toHaveBeenCalledTimes(2);
    result.unsubscribe();
  });

  it.each(['run_error', 'run_cancelled', 'stream_closed'])(
    'does not reconnect after terminal event %s',
    async (terminalEvent) => {
      vi.useFakeTimers();
      const stub = clientStub(vi.fn(() => generator(sse(terminalEvent))));
      const result = await managerFor(stub).startRootExploration('query', vi.fn());
      await settle();
      await vi.advanceTimersByTimeAsync(5000);
      expect(stub.streamEvents).toHaveBeenCalledTimes(1);
      result.unsubscribe();
    },
  );

  it('does not retry after unsubscribe during reconnect backoff', async () => {
    vi.useFakeTimers();
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const failing = (async function* (): AsyncGenerator<never, void, unknown> {
      throw new Error('connection lost');
    })();
    const stream = vi.fn(() => failing);
    const stub = clientStub(stream);
    const result = await managerFor(stub).startRootExploration('query', vi.fn());
    await settle();
    expect(vi.getTimerCount()).toBe(1);
    result.unsubscribe();
    await vi.advanceTimersByTimeAsync(1000);
    await settle();
    expect(stream).toHaveBeenCalledTimes(1);
  });

  it('does not deliver queued events or reconnect after unsubscribe', async () => {
    let release!: () => void;
    let markWaiting!: () => void;
    const blocked = new Promise<void>(resolve => { release = resolve; });
    const waiting = new Promise<void>(resolve => { markWaiting = resolve; });
    const stream = vi.fn(() => (async function* () {
      yield sse('output_delta', '1') as never;
      markWaiting();
      await blocked;
      yield sse('run_completed', '2') as never;
    })());
    const stub = clientStub(stream);
    const received = vi.fn();
    const result = await managerFor(stub).startRootExploration('query', received);
    await waiting;
    await settle();
    result.unsubscribe();
    release();
    await settle();
    expect(received).toHaveBeenCalledTimes(1);
    expect(stream).toHaveBeenCalledTimes(1);
  });

  it('forwards approval digest, rejection reason, status, cancellation, and SDK rejections', async () => {
    const stub = clientStub();
    const manager = managerFor(stub);
    const response = { run_id: 'run-1' };
    stub.resumeRun.mockResolvedValue(response);
    stub.getRun.mockResolvedValue(response);
    stub.cancelRun.mockResolvedValue(response);
    await expect(manager.resumeHitlRun('run-1', true, 'sha256:approve')).resolves.toBe(response);
    await expect(manager.resumeHitlRun('run-1', false, 'sha256:reject', 'not approved')).resolves.toBe(response);
    expect(stub.resumeRun).toHaveBeenNthCalledWith(1, 'run-1', true, {
      rejectionReason: undefined, batch_digest: 'sha256:approve',
    });
    expect(stub.resumeRun).toHaveBeenNthCalledWith(2, 'run-1', false, {
      rejectionReason: 'not approved', batch_digest: 'sha256:reject',
    });
    await expect(manager.getRunStatus('run-1')).resolves.toBe(response);
    await expect(manager.cancelRun('run-1')).resolves.toBe(response);
    expect(stub.getRun).toHaveBeenCalledWith('run-1');
    expect(stub.cancelRun).toHaveBeenCalledWith('run-1');

    const failure = new Error('SDK rejected resume');
    stub.resumeRun.mockRejectedValue(failure);
    await expect(manager.resumeHitlRun('run-1', true, 'digest')).rejects.toBe(failure);
  });
});
