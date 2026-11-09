import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ApprovalBatch, Artifact, HaaSClient, RunResponse } from '@haas/sdk';
import {
  AtlasExplorationClient, parseExplorationSnapshot, selectExplorationTarget,
  type AtlasResearchEvent, type AtlasResearchResult, type AtlasExplorationSnapshot,
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
const stub = () => ({
  createThread: vi.fn(async () => ({ thread_id: 'thread-root', history_version: 3 })),
  branchThread: vi.fn(async () => ({ thread_id: 'thread-child', history_version: 17 })),
  createRun: vi.fn(async (_request?: unknown) => run()),
  getRun: vi.fn(async (_id?: string) => run()),
  cancelRun: vi.fn(async (_id?: string) => run('cancelled')),
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
});
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
    const unsubscribe = client.observe(e => events.push(e));
    const snapshot = await client.research('computers');
    expect(parseExplorationSnapshot(snapshot)).toBe(snapshot);
    expect(sdk.createRun).toHaveBeenCalledWith(expect.objectContaining({
      preload_skills: ['web-research'], expected_history_version: 3,
      max_steps: 6, max_cost_usd: 0.15, max_tool_invocations: 12,
    }));
    expect(snapshot.requests[0]).toMatchObject({
      run_id: 'run-root', thread_id: 'thread-root', committed_history_version: 17, status: 'completed',
    });
    expect(snapshot.occurrences[0].target).toEqual({ kind: 'entity', entity_id: 'computers' });
    expect(events.at(-1)).toMatchObject({ type: 'completed', context: { historyVersion: 17 } });
    expect(sdk.createArtifact).not.toHaveBeenCalled();
    unsubscribe();
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
      max_steps: 5, max_cost_usd: 0.10, max_tool_invocations: 8,
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
    sdk.createRun.mockResolvedValueOnce(run('failed', 'max_cost_usd budget exceeded'));
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
      max_steps: 6, max_cost_usd: 0.15, max_tool_invocations: 12,
    }));
    expect(sdk.getRun).not.toHaveBeenCalled();
    expect(sdk.cancelRun).not.toHaveBeenCalled();
  });

  it('aborts the actual run and never publishes stale completion', async () => {
    const sdk = stub();
    sdk.createRun.mockResolvedValueOnce(run('running'));
    const controller = new AbortController();
    const client = make(sdk);
    const listener = vi.fn();
    client.observe(listener);
    const pending = client.research('computers', undefined, controller.signal);
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    await settle();
    controller.abort();
    await rejected;
    expect(sdk.cancelRun).toHaveBeenCalledTimes(1);
    expect(sdk.cancelRun).toHaveBeenCalledWith('run-root');
    expect(listener.mock.calls.some(([event]) => event.type === 'completed')).toBe(false);
  });

  it('cancels a superseded selected scope even if its late run creation finishes afterward', async () => {
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
    expect(sdk.cancelRun).toHaveBeenCalledWith('old-run');
  });

  it('uses a bounded timeout while waiting for human approval and calls cancelRun', async () => {
    vi.useFakeTimers();
    const sdk = stub();
    const waiting = { ...run('input_required'), approval: {
      batch_id: 'batch', batch_digest: 'digest', expires_at: approval().expires_at, tool_calls: approval().tool_calls,
    } };
    sdk.createRun.mockResolvedValue(waiting);
    sdk.getRun.mockResolvedValue(waiting);
    sdk.getApproval.mockResolvedValue(approval());
    const pending = make(sdk, 20).research('computers');
    const rejected = expect(pending).rejects.toMatchObject({ name: 'TimeoutError' });
    await vi.advanceTimersByTimeAsync(21);
    await rejected;
    expect(sdk.cancelRun).toHaveBeenCalledWith('run-root');
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
    await client.research('computers');
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
    expect(sdk.cancelRun).toHaveBeenCalledWith('run-root');
  });

  it('aborts waiting for a stalled SDK poll without publishing late data', async () => {
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
    expect(sdk.cancelRun).toHaveBeenCalledTimes(1);
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
