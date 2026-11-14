import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ApprovalBatch, Artifact, HaaSClient, RunResponse } from '@haas/sdk';
import {
  AtlasExplorationClient, parseExplorationSnapshot, selectExplorationTarget,
  type AtlasExecutionEvent, type AtlasResearchEvent, type AtlasResearchResult, type AtlasExplorationSnapshot,
} from '../src';

const source = {
  source_id: 'src', url: 'https://example.test/computers', title: 'Computers',
  publisher: 'Public source', retrieved_at: '2026-11-09T00:00:00Z',
  excerpt: 'Computers process information.', trust: 'official' as const,
};
const entity = {
  entity_id: 'computers', kind: 'concept' as const, title: 'Computers',
  summary: '', identity_status: 'resolved' as const, external_references: [],
  facets: [{ key: 'history', label: 'History', scope: 'History of computers', status: 'research_needed' as const }],
};
const graph = (): AtlasResearchResult => ({
  interpretation: { kind: 'resolved', subject_entity_id: 'computers' },
  entities: [entity], sources: [source], relationships: [],
  claims: [{
    claim_id: 'claim', subject_entity_id: 'computers', text: source.excerpt,
    observed_at: source.retrieved_at, evidence: { status: 'sourced', source_ids: ['src'] },
  }],
  targets: [{ kind: 'entity', entity_id: 'computers' }],
});
const run = (status: RunResponse['status'] = 'completed', result = JSON.stringify(graph())): RunResponse => ({
  run_id: 'run-root', thread_id: 'thread-root', status, result, event_sequence: 4, created_at: 1,
});
const tool = (content = JSON.stringify({ url: source.url, snippet: source.excerpt })) => ({
  role: 'tool', tool_call_id: 'call-search', content,
});
const stub = () => {
  const createRun = vi.fn(async (_request?: unknown) => run());
  return {
  createThread: vi.fn(async () => ({ thread_id: 'thread-root', history_version: 3 })),
  branchThread: vi.fn(async () => ({ thread_id: 'thread-child', history_version: 17 })),
  createRun,
  getRun: vi.fn(async (id?: string) => {
    const result = createRun.mock.results.at(-1)?.value;
    const created = result ? await result : run();
    return { ...created, run_id: id ?? created.run_id };
  }),
  cancelRun: vi.fn(async (_id?: string) => run('cancelled')),
  streamEvents: vi.fn(async function* (
    _id?: string, _options?: unknown,
  ): AsyncGenerator<{ event: string; data: Record<string, unknown>; id?: string }> { }),
  getRunSteps: vi.fn(async (_id?: string) => ({ items: [{
    step_id: 'step-1', run_id: 'run-root', attempt: 1, status: 'completed',
    started_at: 10, completed_at: 20,
    plan: { private_reasoning: 'must not enter telemetry' },
    tool_calls: [{ tool_name: 'web_search', invocation_id: 'call-search',
      arguments: { token: 'private' }, destination: 'https://example.test', required_secrets: [] }],
  }] })),
  getRunUsage: vi.fn(async (_id?: string) => ({
    run_id: 'run-root', model_tokens: { prompt: 10, completion: 20, total: 30 },
    tool_invocations: 1, egress_bytes: 100, artifact_bytes: 0, cost_usd: 0.02,
  })),
  getThread: vi.fn(async (_id?: string, _params?: unknown) => ({
    thread_id: 'thread-root', history_version: 17, items: [tool()], next_offset: null as number | null,
  })),
  getRunEvents: vi.fn(async (_id?: string, _params?: unknown) => ({
    items: [{ sequence: 4, type: 'tool_result', data: {
      status: 'succeeded', is_error: false, tool: 'web_search', tool_call_id: 'call-search',
      preview: 'A truncated preview is NOT the complete output',
    } }], next_after: 4, latest_sequence: 4,
  })),
  getApproval: vi.fn<[], Promise<ApprovalBatch>>(),
  decideApproval: vi.fn(async (_id: string, _decision: unknown) => approval('approved')),
  createArtifact: vi.fn(async (_request?: unknown, _blob?: Blob) => ({
    artifact_id: 'artifact-1', object_key: 'opaque', sha256: 'hash', size_bytes: 10,
  })),
  getArtifact: vi.fn<[], Promise<Artifact>>(),
  downloadArtifact: vi.fn<[], Promise<Blob>>(),
  listArtifacts: vi.fn(async (_params?: unknown) => ({ items: [] as Artifact[], next_cursor: undefined as string | undefined })),
  };
};
type Stub = ReturnType<typeof stub>;
const make = (sdk: Stub, timeoutMs = 1000) => new AtlasExplorationClient(
  sdk as unknown as HaaSClient, { pollIntervalMs: 0, timeoutMs },
);
const approval = (status: ApprovalBatch['status'] = 'pending'): ApprovalBatch => ({
  batch_id: 'batch', run_id: 'run-root', batch_digest: 'digest', status,
  expires_at: '2026-11-09T12:00:00Z', created_at: 1,
  tool_calls: [{ invocation_id: 'invoke', tool_name: 'example_write', destination: 'https://example.test',
    arguments: { message: 'Inspect before deciding' }, required_secrets: [] }],
});
const settle = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
afterEach(() => { vi.useRealTimers(); });

describe('Atlas authenticated live research adapter', () => {
  it('creates a bounded web-research run and constructs trusted execution/navigation state', async () => {
    const sdk = stub();
    const client = make(sdk);
    const events: AtlasResearchEvent[] = [];
    const executionEvents: AtlasExecutionEvent[] = [];
    const unsubscribe = client.observe(e => events.push(e));
    client.observeExecution(e => executionEvents.push(e));
    const snapshot = await client.research('computers');
    expect(parseExplorationSnapshot(snapshot)).toBe(snapshot);
    expect(sdk.createRun).toHaveBeenCalledWith(expect.objectContaining({
      preload_skills: ['web-research'], expected_history_version: 3,
      max_steps: 1000, max_cost_usd: 0.15, max_tool_invocations: 12,
    }));
    expect(snapshot.requests[0]).toMatchObject({
      run_id: 'run-root', thread_id: 'thread-root', committed_history_version: 17, status: 'completed',
    });
    expect(snapshot.occurrences[0].target).toEqual({ kind: 'entity', entity_id: 'computers' });
    expect(events.at(-1)).toMatchObject({ type: 'completed', context: { historyVersion: 17 } });
    expect(sdk.streamEvents).toHaveBeenCalledTimes(1);
    expect(sdk.streamEvents).toHaveBeenCalledWith('run-root', expect.objectContaining({
      lastEventId: '0', outputDeltas: true, signal: expect.any(AbortSignal),
    }));
    expect(sdk.getRun).toHaveBeenCalledTimes(1);
    expect(executionEvents.find(e => e.type === 'submitted')).toMatchObject({
      submission: { question: 'computers', prompt: expect.stringContaining('Question: "computers"'),
        selectedContext: { ancestry: [] } },
    });
    const telemetry = executionEvents.find(e => e.type === 'telemetry');
    expect(telemetry).toMatchObject({
      telemetry: { status: 'completed', steps: [{ tools: [{ toolName: 'web_search' }] }],
        usage: { model_tokens: { total: 30 }, cost_usd: 0.02 } },
    });
    expect(JSON.stringify(telemetry)).not.toContain('private_reasoning');
    expect(JSON.stringify(telemetry)).not.toContain('private');
    expect(sdk.createArtifact).not.toHaveBeenCalled();
    unsubscribe();
  });

  it('projects bounded telemetry, keeps ID-less deltas provisional, and deduplicates replayed IDs', async () => {
    const sdk = stub();
    sdk.streamEvents.mockImplementationOnce(async function* () {
      yield { event: 'output_delta', data: { delta: 'draft only' } };
      yield { event: 'step_started', id: '1', data: {
        step: 2, model: 'model/example', active_skills: ['web-research'], timestamp: 123,
      } };
      yield { event: 'step_started', id: '1', data: { step: 99 } };
      yield { event: 'tool_result', id: '2', data: {
        tool: 'web_search', tool_call_id: 'call', preview: `token=top-secret ${'x'.repeat(1500)}`,
      } };
    });
    const events: AtlasResearchEvent[] = [];
    const executionEvents: AtlasExecutionEvent[] = [];
    const client = make(sdk);
    client.observeExecution(event => executionEvents.push(event));
    await client.research('computers');
    const progress = executionEvents.filter((event): event is Extract<AtlasExecutionEvent, { type: 'progress' }> =>
      event.type === 'progress').map(event => event.progress);
    expect(progress.map(event => event.event)).toEqual(['output_delta', 'step_started', 'tool_result']);
    expect(progress[0]).toMatchObject({ preview: 'draft only', provisional: true });
    expect(progress[0].eventId).toBeUndefined();
    expect(progress[1]).toMatchObject({ eventId: 1, step: 2, model: 'model/example', activeSkills: ['web-research'] });
    expect(progress[2].preview).not.toContain('top-secret');
    expect(progress[2].previewTruncated).toBe(true);
    expect(sdk.getRun).toHaveBeenCalledTimes(1);
    expect(sdk.createRun).toHaveBeenCalledTimes(1);
  });

  it('makes replay gaps visible without advancing the applied cursor', async () => {
    const sdk = stub();
    sdk.streamEvents.mockImplementationOnce(async function* () {
      yield { event: 'step_started', id: '1', data: { step: 1 } };
      yield { event: 'tool_result', id: '3', data: { tool: 'web_search' } };
    });
    const events: AtlasExecutionEvent[] = [];
    const client = make(sdk);
    client.observeExecution(event => events.push(event));
    await client.research('computers');
    expect(events.find(event => event.type === 'disconnected')).toMatchObject({
      lastEventId: '1',
    });
    expect(sdk.getRun).toHaveBeenCalledTimes(1);
    expect(sdk.createRun).toHaveBeenCalledTimes(1);
  });

  it('resumes observation with the same run and durable cursor only', async () => {
    const sdk = stub();
    const client = make(sdk);
    const context = {
      requestId: 'request', operationId: 'operation', generation: 1,
      explorationId: 'exploration', pathId: 'path', parentOccurrenceId: null,
      baseRevision: 1, runId: 'run-root', threadId: 'thread-root', historyVersion: 3,
    };
    sdk.streamEvents.mockImplementationOnce(async function* (_id?: string, options?: unknown) {
      expect(_id).toBe('run-root');
      expect(options).toMatchObject({ lastEventId: '7', outputDeltas: true });
      yield { event: 'output_delta', data: { delta: 'preview' } };
      yield { event: 'tool_result', id: '8', data: { tool: 'web_search' } };
    });
    const result = await client.resumeObservation(context, '7');
    expect(result).toMatchObject({ status: 'completed', lastEventId: '8', terminal: true, disconnected: false });
    expect(sdk.createRun).not.toHaveBeenCalled();
    expect(sdk.cancelRun).not.toHaveBeenCalled();
    expect(sdk.getRun).toHaveBeenCalledTimes(1);
  });

  it('persists validated terminal outcome when resumed observation reconciles a terminal run', async () => {
    const sdk = stub();
    const outcome = vi.fn(async () => ({
      workspace_id: 'workspace', mutation_id: 'terminal', revision: 4, saved_at: '2026-11-10T00:00:00Z',
    }));
    const client = new AtlasExplorationClient(sdk as unknown as HaaSClient, {
      operationPersistence: {
        workspaceId: 'workspace',
        prepare: vi.fn(), outcome, bind: vi.fn(), submitting: vi.fn(), unknown: vi.fn(),
      } as never,
    });
    const context = {
      requestId: 'request', operationId: 'operation', workspaceId: 'workspace', generation: 1,
      explorationId: 'exploration', pathId: 'path', parentOccurrenceId: null,
      baseRevision: 1, runId: 'run-root', threadId: 'thread-root', historyVersion: 3,
    };
    const result = await client.resumeObservation(context, '0');
    expect(result.terminal).toBe(true);
    expect(outcome).toHaveBeenCalledWith({
      workspaceId: 'workspace', operationId: 'operation', kind: 'operation_outcome',
      payload: { operation_id: 'operation', state: 'terminal', status: 'completed' },
    });
    expect(sdk.createRun).not.toHaveBeenCalled();
    expect(sdk.cancelRun).not.toHaveBeenCalled();
  });

  it('awaits 042 operation prepare and bind receipts around the single run dispatch', async () => {
    const sdk = stub();
    const order: string[] = [];
    const operationPersistence = {
      workspaceId: 'workspace',
      prepare: vi.fn(async input => {
        order.push(`prepare:${input.kind}`);
        return { workspace_id: 'workspace', mutation_id: 'm1', revision: 2, saved_at: '2026-11-10T00:00:00Z' };
      }),
      outcome: vi.fn(async input => {
        order.push(`outcome:${input.payload.state}`);
        return { workspace_id: 'workspace', mutation_id: `m-${input.payload.state}`, revision: 3, saved_at: '2026-11-10T00:00:00Z' };
      }),
      bind: vi.fn(async input => {
        order.push(`bind:${input.kind}:${input.payload.run_id}`);
        return { workspace_id: 'workspace', mutation_id: 'm2', revision: 4, saved_at: '2026-11-10T00:00:00Z' };
      }),
    };
    sdk.createRun.mockImplementationOnce(async () => {
      order.push('dispatch');
      return run();
    });
    const client = new AtlasExplorationClient(sdk as unknown as HaaSClient, { operationPersistence });
    await client.research('exact question');
    expect(order).toEqual([
      'prepare:operation_prepared', 'outcome:submitting', 'dispatch', 'bind:operation_bound:run-root',
      'outcome:terminal',
    ]);
    expect(operationPersistence.prepare).toHaveBeenCalledTimes(1);
    const prepared = operationPersistence.prepare.mock.calls[0][0];
    expect(prepared).toMatchObject({
      workspaceId: 'workspace', kind: 'operation_prepared', payload: {
        operation_id: expect.any(String),
      },
    });
    expect(prepared.payload.prompt).toContain('Question: "exact question"');
    expect(prepared.payload.selected_context).toEqual([{ ancestry: [], entities: [], claims: [], sources: [] }]);
    expect(operationPersistence.bind).toHaveBeenCalledWith(expect.objectContaining({
      kind: 'operation_bound',
      payload: expect.objectContaining({ run_id: 'run-root', thread_id: 'thread-root' }),
    }));
    expect(operationPersistence.outcome).toHaveBeenCalledWith(expect.objectContaining({
      kind: 'operation_outcome',
      payload: expect.objectContaining({ state: 'terminal', status: 'completed' }),
    }));
  });

  it('does not dispatch when operation preparation has no server receipt', async () => {
    const sdk = stub();
    const operationPersistence = {
      workspaceId: 'workspace',
      prepare: vi.fn(async () => { throw new Error('workspace save failed'); }),
      outcome: vi.fn(),
      bind: vi.fn(),
    };
    const client = new AtlasExplorationClient(sdk as unknown as HaaSClient, { operationPersistence });
    await expect(client.research('exact question')).rejects.toThrow('workspace save failed');
    expect(sdk.createRun).not.toHaveBeenCalled();
    expect(operationPersistence.bind).not.toHaveBeenCalled();
  });

  it('persists unknown submission outcome and blocks additional paid submissions', async () => {
    const sdk = stub();
    sdk.createRun.mockRejectedValueOnce(new Error('connection lost after dispatch'));
    const operationPersistence = {
      workspaceId: 'workspace',
      prepare: vi.fn(async () => ({ workspace_id: 'workspace', mutation_id: 'm1', revision: 1, saved_at: '2026-11-10T00:00:00Z' })),
      outcome: vi.fn(async input => ({
        workspace_id: 'workspace', mutation_id: input.payload.state, revision: 2, saved_at: '2026-11-10T00:00:00Z',
      })),
      bind: vi.fn(),
    };
    const client = new AtlasExplorationClient(sdk as unknown as HaaSClient, { operationPersistence });
    await expect(client.research('uncertain')).rejects.toThrow('connection lost');
    expect(operationPersistence.outcome).toHaveBeenCalledWith(expect.objectContaining({
      payload: { operation_id: expect.any(String), state: 'submitting', status: 'submitting' },
    }));
    expect(operationPersistence.outcome).toHaveBeenCalledWith(expect.objectContaining({
      payload: { operation_id: expect.any(String), state: 'unknown', status: 'unknown' },
    }));
    await expect(client.research('must not resubmit')).rejects.toThrow('Workspace operation is unresolved');
    expect(sdk.createRun).toHaveBeenCalledTimes(1);
  });

  it('fails closed when the ambiguous submission outcome cannot be durably acknowledged', async () => {
    const sdk = stub();
    sdk.createRun.mockRejectedValueOnce(new Error('submission response lost'));
    const operationPersistence = {
      workspaceId: 'workspace',
      prepare: vi.fn(async () => ({
        workspace_id: 'workspace', mutation_id: 'm1', revision: 1, saved_at: '2026-11-10T00:00:00Z',
      })),
      outcome: vi.fn()
        .mockResolvedValueOnce({
          workspace_id: 'workspace', mutation_id: 'submitting', revision: 2, saved_at: '2026-11-10T00:00:00Z',
        })
        .mockRejectedValueOnce(new Error('storage unavailable')),
      bind: vi.fn(),
    };
    const client = new AtlasExplorationClient(sdk as unknown as HaaSClient, { operationPersistence });
    await expect(client.research('uncertain')).rejects.toMatchObject({ name: 'AggregateError' });
    await expect(client.research('blocked')).rejects.toThrow('Workspace operation is unresolved');
    expect(sdk.createRun).toHaveBeenCalledTimes(1);
  });

  it('records an unknown submission on observation abort and binds any late known run ID', async () => {
    const sdk = stub();
    let finish!: (value: RunResponse) => void;
    sdk.createRun.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const operationPersistence = {
      workspaceId: 'workspace',
      prepare: vi.fn(async () => ({
        workspace_id: 'workspace', mutation_id: 'm1', revision: 1, saved_at: '2026-11-10T00:00:00Z',
      })),
      outcome: vi.fn(async input => ({
        workspace_id: 'workspace', mutation_id: input.payload.state, revision: 2, saved_at: '2026-11-10T00:00:00Z',
      })),
      bind: vi.fn(async () => ({
        workspace_id: 'workspace', mutation_id: 'bound', revision: 3, saved_at: '2026-11-10T00:00:00Z',
      })),
    };
    const client = new AtlasExplorationClient(sdk as unknown as HaaSClient, { operationPersistence });
    const controller = new AbortController();
    const research = client.research('late run', undefined, controller.signal);
    const rejected = expect(research).rejects.toMatchObject({ name: 'AbortError' });
    await settle();
    controller.abort();
    await rejected;
    finish(run('running'));
    await settle();
    expect(operationPersistence.outcome).toHaveBeenCalledWith(expect.objectContaining({
      payload: { operation_id: expect.any(String), state: 'unknown', status: 'unknown' },
    }));
    expect(operationPersistence.bind).toHaveBeenCalledWith(expect.objectContaining({
      payload: { operation_id: expect.any(String), run_id: 'run-root', thread_id: 'thread-root' },
    }));
    expect(sdk.cancelRun).not.toHaveBeenCalled();
  });

  it('retains a known run and blocks until the same operation_bound mutation receives a receipt', async () => {
    const sdk = stub();
    const boundInput: unknown[] = [];
    let fail = true;
    const operationPersistence = {
      workspaceId: 'workspace',
      prepare: vi.fn(async () => ({ workspace_id: 'workspace', mutation_id: 'm1', revision: 1, saved_at: '2026-11-10T00:00:00Z' })),
      outcome: vi.fn(async input => ({
        workspace_id: 'workspace', mutation_id: input.payload.state, revision: 2, saved_at: '2026-11-10T00:00:00Z',
      })),
      bind: vi.fn(async input => {
        boundInput.push(structuredClone(input));
        if (fail) { fail = false; throw new Error('ack lost'); }
        return { workspace_id: 'workspace', mutation_id: 'bound-mutation', revision: 3, saved_at: '2026-11-10T00:00:00Z' };
      }),
    };
    const client = new AtlasExplorationClient(sdk as unknown as HaaSClient, { operationPersistence });
    await expect(client.research('known run')).rejects.toThrow('Known run is blocked');
    expect(boundInput[0]).toMatchObject({
      kind: 'operation_bound',
      payload: { operation_id: expect.any(String), run_id: 'run-root', thread_id: 'thread-root' },
    });
    await expect(client.research('must wait')).rejects.toThrow('Workspace operation is unresolved');
    await client.recoverOperationBinding('workspace');
    expect(boundInput[1]).toEqual(boundInput[0]);
    expect(operationPersistence.outcome).toHaveBeenCalledWith(expect.objectContaining({
      payload: {
        operation_id: expect.any(String), state: 'unknown', status: 'unknown', run_id: 'run-root',
      },
    }));
    expect(sdk.createRun).toHaveBeenCalledTimes(1);
  });

  it('shows disconnect for a nonterminal run and does not create a replacement run', async () => {
    const sdk = stub();
    sdk.createRun.mockResolvedValue(run('running'));
    sdk.getRun.mockResolvedValue(run('running'));
    sdk.streamEvents.mockImplementationOnce(async function* () {
      yield { event: 'step_started', id: '1', data: { step: 1 } };
      throw new Error('network disconnected');
    });
    const events: AtlasExecutionEvent[] = [];
    const client = make(sdk);
    client.observeExecution(event => events.push(event));
    await expect(client.research('computers')).rejects.toThrow('network disconnected');
    expect(events.find(event => event.type === 'disconnected')).toMatchObject({ lastEventId: '1' });
    expect(sdk.createRun).toHaveBeenCalledTimes(1);
    expect(sdk.getRun).toHaveBeenCalledTimes(1);
  });

  it('does not trust a stale stream terminal event over authoritative run status', async () => {
    const sdk = stub();
    sdk.createRun.mockResolvedValue(run('running'));
    sdk.streamEvents.mockImplementationOnce(async function* () {
      yield { event: 'run_completed', id: '1', data: { output: JSON.stringify(graph()) } };
    });
    const events: AtlasResearchEvent[] = [];
    const executionEvents: AtlasExecutionEvent[] = [];
    const client = make(sdk);
    client.observe(event => events.push(event));
    client.observeExecution(event => executionEvents.push(event));
    await expect(client.research('computers')).rejects.toThrow('Run remains running');
    expect(events.some(event => event.type === 'completed')).toBe(false);
    expect(executionEvents.find(event => event.type === 'disconnected')).toMatchObject({ lastEventId: '1' });
    expect(sdk.getRun).toHaveBeenCalledTimes(1);
  });

  it('surfaces bounded final-target diagnostics with child execution context and keeps the parent intact', async () => {
    const sdk = stub();
    const rootResult = graph();
    rootResult.targets.push({ kind: 'source', source_id: 'src' });
    sdk.createRun.mockResolvedValueOnce(run('completed', JSON.stringify(rootResult)));
    const client = make(sdk);
    const events: AtlasResearchEvent[] = [];
    client.observe(event => events.push(event));
    const parent = await client.research('computers');
    const before = structuredClone(parent);
    const selected = selectExplorationTarget(parent, { kind: 'entity', entity_id: 'computers' }, null);
    const malformed = graph();
    malformed.targets = [
      { kind: 'entity' } as never,
      { kind: 'facet', entity_id: 'computers' } as never,
      { kind: 'source' } as never,
      { kind: 'unknown', entity_id: 'computers' } as never,
      { kind: 'entity', entity_id: 'computers', title: 'DO NOT ECHO MODEL PAYLOAD' } as never,
      { kind: 'entity', entity_id: 'computers', source_id: 'src' } as never,
    ];
    sdk.createRun.mockResolvedValueOnce({
      ...run('completed', JSON.stringify(malformed)), run_id: 'run-child', thread_id: 'thread-child',
    });

    await expect(client.research('History', {
      snapshot: selected, target: { kind: 'entity', entity_id: 'computers' }, parentOccurrenceId: null,
    })).rejects.toMatchObject({ name: 'AtlasResearchValidationError' });

    const failure = events.find(event => event.type === 'error');
    expect(failure).toMatchObject({
      type: 'error',
      context: { runId: 'run-child', threadId: 'thread-child', requestId: expect.any(String) },
    });
    if (failure?.type === 'error') {
      expect((failure.error as Error).message).toContain('target[5]');
      expect((failure.error as Error).message).not.toContain('DO NOT ECHO MODEL PAYLOAD');
    }
    expect(parent).toEqual(before);
    expect(parent.occurrences).toHaveLength(2);
    expect(events.filter(event => event.type === 'completed')).toHaveLength(1);
    expect(sdk.createRun).toHaveBeenCalledTimes(2);
    expect(sdk.createArtifact).not.toHaveBeenCalled();
  });

  it('uses the real committed history version and only selected ancestry for child facets', async () => {
    const sdk = stub();
    const client = make(sdk);
    const root = await client.research('computers');
    const selected = selectExplorationTarget(root, { kind: 'entity', entity_id: 'computers' }, null);
    // Keep an unrelated saved branch and ensure it is not included in the model scope.
    selected.entities.push({ ...entity, entity_id: 'unrelated-secret-branch', title: 'UNRELATED' });
    const childGraph = graph();
    childGraph.targets = [{ kind: 'facet', entity_id: 'computers', facet_key: 'history' }];
    sdk.createRun.mockResolvedValueOnce({ ...run(), run_id: 'run-child', thread_id: 'thread-child', result: JSON.stringify(childGraph) });
    const child = await client.research('History only', {
      snapshot: selected, target: { kind: 'facet', entity_id: 'computers', facet_key: 'history' },
      parentOccurrenceId: selected.paths.find(p => p.path_id === selected.active_path_id)!.selected_occurrence_ids[0],
    });
    expect(sdk.branchThread).toHaveBeenCalledWith('thread-root', { expected_history_version: 17 });
    expect(sdk.createRun).toHaveBeenLastCalledWith(expect.objectContaining({
      thread_id: 'thread-child', expected_history_version: 17,
      max_steps: 1000, max_cost_usd: 0.10, max_tool_invocations: 8,
    }));
    const call = sdk.createRun.mock.calls.at(-1)?.[0] as { prompt: string };
    expect(call.prompt).not.toContain('UNRELATED');
    expect(call.prompt).toContain('"facet_key":"history"');
    expect(child.paths).toEqual(expect.arrayContaining(selected.paths));
    expect(child.occurrences).toEqual(expect.arrayContaining(selected.occurrences));
    expect(child.entities.find(e => e.entity_id === 'computers')).toEqual(entity);
    expect(child.requests.at(-1)?.run_id).toBe('run-child');
  });

  it('resolves ambiguity explicitly and prevents facts attaching to unresolved candidates', async () => {
    const sdk = stub();
    const candidates = graph();
    candidates.entities = [{ ...entity, kind: 'person', identity_status: 'candidate', title: 'Public Alex' }];
    candidates.interpretation = { kind: 'ambiguous', candidate_entity_ids: ['computers'] };
    candidates.claims = [];
    sdk.createRun.mockResolvedValueOnce(run('completed', JSON.stringify(candidates)));
    const client = make(sdk);
    const unresolved = await client.research('Alex');
    expect(unresolved.interpretation.kind).toBe('ambiguous');
    await expect(client.research('career', {
      snapshot: unresolved, target: { kind: 'facet', entity_id: 'computers', facet_key: 'history' },
      parentOccurrenceId: null,
    })).rejects.toThrow('Resolve the candidate');
    const resolved = { ...candidates, entities: [{ ...candidates.entities[0], identity_status: 'resolved' as const }],
      interpretation: { kind: 'resolved' as const, subject_entity_id: 'computers' } };
    sdk.createRun.mockResolvedValueOnce({ ...run(), thread_id: 'thread-child', result: JSON.stringify(resolved) });
    const chosen = await client.research('Resolve this public identity', {
      snapshot: unresolved, target: { kind: 'entity', entity_id: 'computers' }, parentOccurrenceId: null,
    });
    expect(chosen.interpretation.kind).toBe('resolved');
    expect(sdk.createRun.mock.calls[0][0]).toEqual(expect.objectContaining({
      prompt: expect.stringContaining('No private contact/account data, sensitive profiling or inferred wealth'),
    }));
  });

  it.each(['missing url', 'invented excerpt', 'failed tool', 'preview only'])('rejects ungrounded sources: %s', async mode => {
    const sdk = stub();
    if (mode === 'missing url') sdk.getThread.mockResolvedValueOnce({
      thread_id: 'thread-root', history_version: 17, items: [tool('No sources')], next_offset: null,
    });
    if (mode === 'invented excerpt') sdk.getThread.mockResolvedValueOnce({
      thread_id: 'thread-root', history_version: 17, items: [tool(source.url)], next_offset: null,
    });
    if (mode === 'failed tool') sdk.getRunEvents.mockResolvedValueOnce({
      items: [{ sequence: 4, type: 'tool_result', data: {
        status: 'failed', is_error: true, tool: 'web_search', tool_call_id: 'call-search', preview: source.url,
      } }], next_after: 4, latest_sequence: 4,
    });
    if (mode === 'preview only') sdk.getThread.mockResolvedValueOnce({
      thread_id: 'thread-root', history_version: 17, items: [], next_offset: null,
    });
    await expect(make(sdk).research('computers')).rejects.toThrow('Unobserved research source');
  });

  it.each(['separate outputs', 'separate results', 'nested page'])(
    'rejects a URL attributed to another page excerpt: %s', async mode => {
      const sdk = stub();
      const wrongPage = { url: 'https://example.test/other', snippet: source.excerpt };
      const requestedPage = { url: source.url, snippet: 'Different content on this page.' };
      const items = mode === 'separate outputs'
        ? [tool(JSON.stringify(requestedPage)), tool(JSON.stringify(wrongPage))]
        : [tool(JSON.stringify(mode === 'separate results'
          ? { results: [requestedPage, wrongPage] }
          : { ...requestedPage, links: [wrongPage] }))];
      sdk.getThread.mockResolvedValueOnce({
        thread_id: 'thread-root', history_version: 17, items, next_offset: null,
      });
      await expect(make(sdk).research('computers')).rejects.toThrow('Unobserved research source or excerpt');
    },
  );

  it('accepts a source only when its own page result contains the exact excerpt', async () => {
    const sdk = stub();
    sdk.getThread.mockResolvedValueOnce({
      thread_id: 'thread-root', history_version: 17, items: [tool(JSON.stringify({ results: [
        { url: 'https://example.test/other', snippet: 'Other page' },
        { url: source.url, snippet: source.excerpt },
      ] }))], next_offset: null,
    });
    expect((await make(sdk).research('computers')).sources).toEqual([source]);
  });

  it('branches sibling B from the parent producer, not completed sibling A history', async () => {
    const sdk = stub();
    const client = make(sdk);
    const rootGraph = graph();
    rootGraph.entities = [{ ...entity, facets: [
      ...entity.facets,
      { key: 'pricing', label: 'Pricing', scope: 'Published prices', status: 'research_needed' },
    ] }];
    sdk.createRun.mockResolvedValueOnce(run('completed', JSON.stringify(rootGraph)));
    const root = await client.research('computers');
    const parentSelected = selectExplorationTarget(root, { kind: 'entity', entity_id: 'computers' }, null);
    const parentId = parentSelected.paths.find(p => p.path_id === parentSelected.active_path_id)!.selected_occurrence_ids[0];
    sdk.createRun.mockResolvedValueOnce({
      ...run('completed', JSON.stringify(rootGraph)), run_id: 'run-A', thread_id: 'thread-A',
    });
    sdk.branchThread.mockResolvedValueOnce({ thread_id: 'thread-A', history_version: 17 });
    sdk.getThread.mockResolvedValueOnce({
      thread_id: 'thread-A', history_version: 91, items: [tool()], next_offset: null,
    });
    const afterA = await client.research('history A', {
      snapshot: parentSelected, target: { kind: 'entity', entity_id: 'computers' },
      parentOccurrenceId: null,
    });
    expect(afterA.requests.at(-1)).toMatchObject({
      parent_occurrence_id: parentId, thread_id: 'thread-A', committed_history_version: 91,
    });
    const returnedToParent = selectExplorationTarget(afterA, { kind: 'entity', entity_id: 'computers' }, null);
    sdk.createRun.mockResolvedValueOnce({
      ...run('completed', JSON.stringify(rootGraph)), run_id: 'run-B', thread_id: 'thread-B',
    });
    sdk.branchThread.mockResolvedValueOnce({ thread_id: 'thread-B', history_version: 17 });
    const afterB = await client.research('pricing B', {
      snapshot: returnedToParent, target: { kind: 'facet', entity_id: 'computers', facet_key: 'pricing' },
      parentOccurrenceId: parentId,
    });
    expect(sdk.branchThread).toHaveBeenLastCalledWith('thread-root', { expected_history_version: 17 });
    expect(afterB.paths).toEqual(expect.arrayContaining(afterA.paths));
    expect(afterB.paths).toEqual(expect.arrayContaining(root.paths));
    expect(afterB.entities).toEqual(root.entities);
    expect(afterB.claims).toEqual(root.claims);
    expect(afterB.occurrences).toEqual(expect.arrayContaining(afterA.occurrences));
    expect(root.requests).toHaveLength(1);
    expect(afterA.requests).toHaveLength(2);
    expect(afterB.requests).toHaveLength(3);
  });

  it.each(['{"entities":', '{"entities":[]}', '[]', 'prose instead of JSON'])('rejects malformed or partial final output: %s', async result => {
    const sdk = stub();
    sdk.createRun.mockResolvedValueOnce(run('completed', result));
    await expect(make(sdk).research('computers')).rejects.toThrow();
    expect(sdk.createArtifact).not.toHaveBeenCalled();
  });

  it('permits explicit unsupported/empty results without fabricating data', async () => {
    const sdk = stub();
    const result = graph();
    result.sources = [];
    result.claims[0].evidence = { status: 'unsupported', reason: 'No public information found' };
    result.targets = [];
    sdk.createRun.mockResolvedValueOnce(run('completed', JSON.stringify(result)));
    const snapshot = await make(sdk).research('not publicly known');
    expect(snapshot.sources).toEqual([]);
    expect(snapshot.occurrences).toEqual([]);
    expect(snapshot.claims[0].evidence.status).toBe('unsupported');
  });

  it('can recover a complete schema-valid tool output, never a partial preview', async () => {
    const sdk = stub();
    sdk.createRun.mockResolvedValueOnce(run('completed', ''));
    sdk.getThread.mockResolvedValueOnce({
      thread_id: 'thread-root', history_version: 17, items: [tool(JSON.stringify(graph()))], next_offset: null,
    });
    expect((await make(sdk).research('computers')).claims).toHaveLength(1);
  });

  it('surfaces budget failures without retries or fabricated partial success', async () => {
    const sdk = stub();
    sdk.createRun.mockResolvedValueOnce(run('running'));
    sdk.getRun.mockResolvedValueOnce(run('failed', 'max_cost_usd budget exceeded'));
    await expect(make(sdk).research('computers')).rejects.toThrow('budget exceeded');
    expect(sdk.createRun).toHaveBeenCalledTimes(1);
    expect(sdk.cancelRun).not.toHaveBeenCalled();
  });

  it('preserves the original tenant-policy rejection without retrying paid submission', async () => {
    const sdk = stub();
    const rejection = Object.assign(new Error('requested max_cost_usd exceeds tenant policy'), {
      status: 403, details: { detail: 'requested max_cost_usd exceeds tenant policy' },
    });
    sdk.createRun.mockRejectedValueOnce(rejection);
    await expect(make(sdk).research('computers')).rejects.toBe(rejection);
    expect(sdk.createRun).toHaveBeenCalledTimes(1);
    expect(sdk.createRun).toHaveBeenCalledWith(expect.objectContaining({
      max_steps: 1000, max_cost_usd: 0.15, max_tool_invocations: 12,
    }));
    expect(sdk.getRun).not.toHaveBeenCalled();
    expect(sdk.cancelRun).not.toHaveBeenCalled();
  });

  it('aborts stream observation without cancelling remote work or publishing stale completion', async () => {
    const sdk = stub();
    sdk.createRun.mockResolvedValueOnce(run('running'));
    sdk.streamEvents.mockImplementationOnce(async function* (
      _id?: string, _options?: unknown,
    ): AsyncGenerator<{ event: string; data: Record<string, unknown>; id?: string }> {
      const signal = (_options as { signal: AbortSignal }).signal;
      await new Promise<void>((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(signal.reason), { once: true });
      });
    });
    const controller = new AbortController();
    const client = make(sdk);
    const listener = vi.fn();
    client.observe(listener);
    const pending = client.research('computers', undefined, controller.signal);
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    await settle();
    controller.abort();
    await rejected;
    expect(sdk.cancelRun).not.toHaveBeenCalled();
    expect(listener.mock.calls.some(([event]) => event.type === 'completed')).toBe(false);
  });

  it('records explicit Cancel once and aborts only that active run observation', async () => {
    const sdk = stub();
    sdk.createRun.mockResolvedValue(run('running'));
    sdk.streamEvents.mockImplementationOnce(async function* (_id?: string, options?: unknown) {
      const signal = (options as { signal: AbortSignal }).signal;
      await new Promise<void>((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(signal.reason), { once: true });
      });
    });
    const client = make(sdk);
    const research = client.research('computers');
    const rejected = expect(research).rejects.toMatchObject({ name: 'AbortError' });
    await settle();
    await Promise.all([client.cancelRun('run-root'), client.cancelRun('run-root')]);
    await rejected;
    expect(sdk.cancelRun).toHaveBeenCalledTimes(1);
    expect(sdk.cancelRun).toHaveBeenCalledWith('run-root');
  });

  it('persists the authoritative terminal status returned by explicit cancellation', async () => {
    const sdk = stub();
    sdk.createRun.mockResolvedValue(run('running'));
    sdk.streamEvents.mockImplementationOnce(async function* (_id?: string, options?: unknown) {
      const signal = (options as { signal: AbortSignal }).signal;
      await new Promise<void>((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(signal.reason), { once: true });
      });
    });
    sdk.cancelRun.mockResolvedValue(run('cancelled'));
    const outcome = vi.fn(async input => ({
      workspace_id: 'workspace', mutation_id: input.payload.state, revision: 3,
      saved_at: '2026-11-10T00:00:00Z',
    }));
    const client = new AtlasExplorationClient(sdk as unknown as HaaSClient, {
      operationPersistence: {
        workspaceId: 'workspace',
        prepare: vi.fn(async () => ({
          workspace_id: 'workspace', mutation_id: 'prepared', revision: 1, saved_at: '2026-11-10T00:00:00Z',
        })),
        submitting: vi.fn(async () => ({
          workspace_id: 'workspace', mutation_id: 'submitting', revision: 2, saved_at: '2026-11-10T00:00:00Z',
        })),
        outcome, unknown: vi.fn(), bind: vi.fn(async () => ({
          workspace_id: 'workspace', mutation_id: 'bound', revision: 3, saved_at: '2026-11-10T00:00:00Z',
        })),
      } as never,
    });
    const research = client.research('cancel me');
    const rejected = expect(research).rejects.toMatchObject({ name: 'AbortError' });
    await settle();
    await client.cancelRun('run-root');
    await rejected;
    expect(outcome).toHaveBeenCalledWith({
      workspaceId: 'workspace',
      operationId: expect.any(String),
      kind: 'operation_outcome',
      payload: { operation_id: expect.any(String), state: 'terminal', status: 'cancelled' },
    });
  });

  it('does not cancel a superseded remote run when its late creation finishes afterward', async () => {
    const sdk = stub();
    let finish!: (value: RunResponse) => void;
    sdk.createRun.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const client = make(sdk);
    const old = client.research('old');
    const rejected = expect(old).rejects.toMatchObject({ name: 'AbortError' });
    await settle();
    const current = client.research('new');
    finish({ ...run(), run_id: 'old-run' });
    await rejected;
    const snapshot = await current;
    expect(snapshot.root_query).toBe('new');
    expect(sdk.cancelRun).not.toHaveBeenCalled();
  });

  it('times out stream observation without remotely cancelling the run', async () => {
    vi.useFakeTimers();
    const sdk = stub();
    sdk.createRun.mockResolvedValue(run('running'));
    sdk.streamEvents.mockImplementationOnce(async function* (
      _id?: string, options?: unknown,
    ): AsyncGenerator<{ event: string; data: Record<string, unknown>; id?: string }> {
      const signal = (options as { signal: AbortSignal }).signal;
      await new Promise<void>((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(signal.reason), { once: true });
      });
    });
    const pending = make(sdk, 20).research('computers');
    const rejected = expect(pending).rejects.toMatchObject({ name: 'TimeoutError' });
    await vi.advanceTimersByTimeAsync(21);
    await rejected;
    expect(sdk.cancelRun).not.toHaveBeenCalled();
    expect(sdk.decideApproval).not.toHaveBeenCalled();
  });

  it.each(['approve', 'reject'] as const)('exposes digest-bound approval context and allows explicit %s only', async decision => {
    const sdk = stub();
    sdk.createRun.mockResolvedValueOnce({ ...run('input_required'), approval: {
      batch_id: 'batch', batch_digest: 'digest', expires_at: approval().expires_at, tool_calls: approval().tool_calls,
    } });
    sdk.getApproval.mockResolvedValue(approval());
    const client = make(sdk);
    const events: AtlasResearchEvent[] = [];
    let humanDecision: Promise<ApprovalBatch> | undefined;
    client.observe(event => {
      events.push(event);
      if (event.type === 'approval') {
        expect(sdk.decideApproval).not.toHaveBeenCalled();
        humanDecision = client.decideApproval(event.approval.batch_id, {
          decision, batch_digest: event.approval.batch_digest,
        });
      }
    });
    await expect(client.research('computers')).rejects.toThrow('pending approval');
    await humanDecision;
    expect(events.find(e => e.type === 'approval')).toMatchObject({
      approval: { batch_id: 'batch', batch_digest: 'digest',
        tool_calls: [{ destination: 'https://example.test', arguments: { message: 'Inspect before deciding' } }] },
    });
    expect(sdk.decideApproval).toHaveBeenCalledWith('batch', { decision, batch_digest: 'digest' });
  });

  it('rejects approval mismatch and does not bypass a denied public API decision', async () => {
    const sdk = stub();
    sdk.createRun.mockResolvedValueOnce({ ...run('input_required'), approval: {
      batch_id: 'batch', batch_digest: 'wrong', expires_at: approval().expires_at, tool_calls: [],
    } });
    sdk.getRun.mockResolvedValueOnce({ ...run('input_required'), approval: {
      batch_id: 'batch', batch_digest: 'wrong', expires_at: approval().expires_at, tool_calls: [],
    } });
    sdk.getApproval.mockResolvedValue(approval());
    await expect(make(sdk).research('computers')).rejects.toThrow('Approval context mismatch');
    sdk.decideApproval.mockRejectedValueOnce(new Error('403 forbidden'));
    await expect(make(sdk).decideApproval('batch', { decision: 'approve', batch_digest: 'digest' })).rejects.toThrow('403');
  });

  it('does not include a restored deeper suffix when researching an ancestor again', async () => {
    const sdk = stub();
    const client = make(sdk);
    const root = await client.research('computers');
    const selected = selectExplorationTarget(root, { kind: 'entity', entity_id: 'computers' }, null);
    const row = selected.paths.find(p => p.path_id === selected.active_path_id)!.selected_occurrence_ids[0];
    const deeper = selectExplorationTarget(selected, { kind: 'facet', entity_id: 'computers', facet_key: 'history' }, row);
    sdk.createRun.mockResolvedValueOnce({ ...run(), thread_id: 'thread-child' });
    const result = await client.research('Research overview, not history', {
      snapshot: deeper, target: { kind: 'entity', entity_id: 'computers' }, parentOccurrenceId: null,
    });
    const call = sdk.createRun.mock.calls.at(-1)?.[0] as { prompt: string };
    const scope = JSON.parse(call.prompt.split('Selected context: ')[1].split('\nOutput JSON Schema:')[0]);
    expect(scope.ancestry).toEqual([{ kind: 'entity', entity_id: 'computers' }]);
    expect(result.paths).toEqual(expect.arrayContaining(deeper.paths));
    expect(result.requests.at(-1)?.parent_occurrence_id).toBe(row);
  });

  it('preserves typed published-price and checklist details from validated live graph output', async () => {
    const sdk = stub();
    const result = graph();
    result.claims[0].details = {
      attributes: [{ key: 'published_price', label: 'Published price', value: 250, unit: 'USD' }],
      checklist: [{ id: 'verify', text: 'Verify the source before acting', required: true }],
      draft: { title: 'Inquiry', body: 'Not sent' },
    };
    sdk.createRun.mockResolvedValueOnce(run('completed', JSON.stringify(result)));
    const snapshot = await make(sdk).research('published pricing and next steps');
    expect(snapshot.claims[0].details).toEqual(result.claims[0].details);
    expect(snapshot.actions).toEqual([]);
  });

  it('surfaces branch history conflicts without creating a duplicate paid run', async () => {
    const sdk = stub();
    const client = make(sdk);
    const snapshot = await client.research('computers');
    sdk.branchThread.mockRejectedValueOnce(new Error('409 Thread history changed'));
    await expect(client.research('history', {
      snapshot, target: { kind: 'entity', entity_id: 'computers' }, parentOccurrenceId: null,
    })).rejects.toThrow('409');
    expect(sdk.createRun).toHaveBeenCalledTimes(1);
    expect(sdk.branchThread).toHaveBeenCalledTimes(1);
  });

  it('rejects a run status response for a different execution', async () => {
    const sdk = stub();
    sdk.createRun.mockResolvedValueOnce(run('running'));
    sdk.getRun.mockResolvedValueOnce({ ...run(), run_id: 'other-run' });
    await expect(make(sdk).research('computers')).rejects.toThrow('Run execution context mismatch');
    expect(sdk.cancelRun).not.toHaveBeenCalled();
  });

  it('aborts waiting for reconciliation without publishing late data or remote cancellation', async () => {
    const sdk = stub();
    sdk.createRun.mockResolvedValueOnce(run('running'));
    let finish!: (value: RunResponse) => void;
    sdk.getRun.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const client = make(sdk);
    const controller = new AbortController();
    const promise = client.research('computers', undefined, controller.signal);
    const rejected = expect(promise).rejects.toMatchObject({ name: 'AbortError' });
    await new Promise(resolve => setTimeout(resolve, 10));
    controller.abort();
    await rejected;
    finish(run());
    await settle();
    expect(sdk.getThread).not.toHaveBeenCalled();
    expect(sdk.cancelRun).not.toHaveBeenCalled();
  });
});

describe('Explicit immutable Atlas snapshot artifacts', () => {
  const savedArtifact = (snapshot: AtlasExplorationSnapshot, id = 'artifact-1'): Artifact => ({
    artifact_id: id, run_id: 'run-root', content_type: 'application/json', created_at: 1,
    metadata: { format: 'atlas-snapshot.v1', schema_version: snapshot.schema_version,
      exploration_id: snapshot.exploration_id, revision: snapshot.revision, parent_artifact_id: null },
  });

  it('saves only on request, loads validated JSON and returns run IDs for reopening/listing', async () => {
    const sdk = stub();
    const client = make(sdk);
    const snapshot = await client.research('computers');
    expect(sdk.createArtifact).not.toHaveBeenCalled();
    const saved = await client.save(snapshot);
    expect(saved).toMatchObject({ artifactId: 'artifact-1', runId: 'run-root', parentArtifactId: null });
    const [request, file] = sdk.createArtifact.mock.calls[0];
    expect(request).toMatchObject({ content_type: 'application/json', run_id: 'run-root', source: 'api' });
    expect(JSON.parse(await file!.text())).toEqual(snapshot);
    sdk.getArtifact.mockResolvedValue(savedArtifact(snapshot));
    sdk.downloadArtifact.mockResolvedValue(new Blob([JSON.stringify(snapshot)]));
    const loaded = await client.load(saved.artifactId);
    expect(loaded).toEqual(snapshot);
    sdk.listArtifacts.mockResolvedValueOnce({ items: [savedArtifact(snapshot)], next_cursor: undefined });
    expect(await client.listSaved(saved.runId)).toEqual([expect.objectContaining({ artifactId: saved.artifactId })]);
    expect(sdk.listArtifacts).toHaveBeenCalledWith({ run_id: 'run-root', cursor: undefined, limit: 100 });
  });

  it('writes sibling immutable artifacts for concurrent saves, then records loaded parent lineage', async () => {
    const sdk = stub();
    const client = make(sdk);
    const snapshot = await client.research('computers');
    sdk.createArtifact.mockResolvedValueOnce({ artifact_id: 'a', object_key: 'x', sha256: 'x', size_bytes: 1 })
      .mockResolvedValueOnce({ artifact_id: 'b', object_key: 'x', sha256: 'x', size_bytes: 1 });
    const saved = await Promise.all([client.save(snapshot), client.save(snapshot)]);
    expect(saved.map(s => s.parentArtifactId)).toEqual([null, null]);
    expect(saved.map(s => s.artifactId)).toEqual(['a', 'b']);
    sdk.getArtifact.mockResolvedValue(savedArtifact(snapshot, 'a'));
    sdk.downloadArtifact.mockResolvedValue(new Blob([JSON.stringify(snapshot)]));
    const loaded = await client.load('a');
    expect((await client.save(loaded, 'explicit-run')).parentArtifactId).toBe('a');
    expect(sdk.createArtifact.mock.calls.at(-1)?.[0]).toMatchObject({ run_id: 'explicit-run' });
  });

  it('rejects invalid snapshots, missing run scopes, malformed content and cross-metadata mismatch', async () => {
    const sdk = stub();
    const client = make(sdk);
    const snapshot = await client.research('computers');
    await expect(client.save({ ...snapshot, requests: [] })).rejects.toThrow('run ID');
    await expect(client.listSaved('')).rejects.toThrow('run ID');
    await expect(client.save({ ...snapshot, entities: [] })).rejects.toThrow();
    sdk.getArtifact.mockResolvedValue(savedArtifact(snapshot));
    sdk.downloadArtifact.mockResolvedValueOnce(new Blob(['not JSON']));
    await expect(client.load('artifact-1')).rejects.toThrow();
    sdk.downloadArtifact.mockResolvedValueOnce(new Blob([JSON.stringify({ ...snapshot, revision: 99 })]));
    await expect(client.load('artifact-1')).rejects.toThrow('metadata/content mismatch');
    sdk.getArtifact.mockRejectedValueOnce(new Error('404 not owned'));
    await expect(client.load('other-tenant-artifact')).rejects.toThrow('404 not owned');
  });

  it('preserves loaded artifact lineage through navigation and subsequent branch research', async () => {
    const sdk = stub();
    const client = make(sdk);
    const snapshot = await client.research('computers');
    sdk.getArtifact.mockResolvedValue(savedArtifact(snapshot, 'original'));
    sdk.downloadArtifact.mockResolvedValue(new Blob([JSON.stringify(snapshot)]));
    const loaded = await client.load('original');
    const navigated = selectExplorationTarget(loaded, { kind: 'entity', entity_id: 'computers' }, null);
    expect((await client.save(navigated)).parentArtifactId).toBe('original');
    sdk.createRun.mockResolvedValueOnce({ ...run(), thread_id: 'thread-child' });
    const child = await client.research('history', {
      snapshot: navigated, target: { kind: 'facet', entity_id: 'computers', facet_key: 'history' },
      parentOccurrenceId: navigated.paths.find(p => p.path_id === navigated.active_path_id)!.selected_occurrence_ids[0],
    });
    expect((await client.save(child)).parentArtifactId).toBe('artifact-1');
  });

  it('does not acknowledge or advance lineage when an upload is denied or fails', async () => {
    const sdk = stub();
    const client = make(sdk);
    const snapshot = await client.research('computers');
    sdk.createArtifact.mockRejectedValueOnce(new Error('422 invalid multipart request'));
    await expect(client.save(snapshot)).rejects.toThrow('422');
    expect((await client.save(snapshot)).parentArtifactId).toBeNull();
    sdk.createArtifact.mockRejectedValueOnce(new Error('413 artifact quota exceeded'));
    await expect(client.save(snapshot)).rejects.toThrow('413');
    expect((await client.save(snapshot)).parentArtifactId).toBe('artifact-1');
  });
});
