import { describe, expect, it, vi } from 'vitest';
import { HaaSClient, type ApprovalBatch } from '@haas/sdk';
import { AtlasExplorationClient, selectExplorationTarget, type AtlasResearchEvent } from '@haas/atlas-explorer-client';
import { createBrowserAtlasAdapter, createLiveAtlasAdapter } from '../src/liveAdapter';
import { claimPresentations } from '../src/graphProjection';
import { deferred, testData } from './workspaceTestData';
import { describeAtlasError } from '../src/explorationAdapter';

function clientSetup() {
  const sdk = new HaaSClient({ baseUrl: 'https://same-origin.test', maxRetries: 0 });
  const client = new AtlasExplorationClient(sdk);
  let observer: (event: AtlasResearchEvent) => void = () => undefined;
  const unsubscribe = vi.fn();
  vi.spyOn(client, 'observe').mockImplementation(fn => { observer = fn; return unsubscribe; });
  return { client, sdk, emit: (event: AtlasResearchEvent) => observer(event), unsubscribe };
}
const context = { requestId: 'req', explorationId: 'explore', pathId: 'path', parentOccurrenceId: null,
  baseRevision: 0, runId: 'run', threadId: 'thread', historyVersion: 1 };
const batch: ApprovalBatch = {
  batch_id: 'batch', run_id: 'run', batch_digest: 'server-batch-digest', status: 'pending', created_at: 1,
  expires_at: '2026-11-09T12:00:00Z',
  tool_calls: [
    { invocation_id: 'call', tool_name: 'actual-tool', destination: 'https://destination.test', arguments: { exact: 'arguments' }, required_secrets: [] },
    { invocation_id: 'other', tool_name: 'other-tool', destination: 'https://other.test', arguments: { exact: 'other arguments' }, required_secrets: [] },
  ],
};

describe('Public Atlas client adapter contract', () => {
  it('preserves safe API problem detail with HTTP context without changing 401 guidance', () => {
    expect(describeAtlasError({ status: 403, problem: { detail: 'Requested max_cost_usd exceeds tenant policy.' } }))
      .toBe('HTTP 403: Requested max_cost_usd exceeds tenant policy.');
    expect(describeAtlasError({ status: 403, details: { detail: 'Tenant policy restriction.' } }))
      .toBe('HTTP 403: Tenant policy restriction.');
    expect(describeAtlasError({ status: 401, problem: { detail: 'Internal response detail' } }))
      .toBe('Authentication required. Connect through the authenticated same-origin HaaS proxy.');
  });

  it('uses the real exported research/observe contract and includes full approval batch contents', async () => {
    const { client, emit, unsubscribe } = clientSetup();
    const snapshot = testData().snapshot;
    const pending = deferred<typeof snapshot>();
    vi.spyOn(client, 'research').mockReturnValue(pending.promise);
    const onStatus = vi.fn();
    const op = createLiveAtlasAdapter(client).research({ query: 'live query', onStatus });
    expect(client.research).toHaveBeenCalledWith('live query', undefined, expect.any(AbortSignal));
    emit({ type: 'status', context, status: 'input_required' });
    expect(onStatus).toHaveBeenCalledWith(expect.objectContaining({ state: 'approval_required', runId: 'run' }));
    emit({ type: 'approval', context, approval: batch });
    expect(onStatus).toHaveBeenLastCalledWith(expect.objectContaining({
      approvals: [expect.objectContaining({
        id: 'batch', digest: 'server-batch-digest', status: 'pending',
        args: { call: { exact: 'arguments' }, other: { exact: 'other arguments' } },
        calls: [
          expect.objectContaining({ toolName: 'actual-tool', destination: 'https://destination.test' }),
          expect.objectContaining({ toolName: 'other-tool', destination: 'https://other.test' }),
        ],
      })],
    }));
    pending.resolve(snapshot);
    expect((await op.result).snapshot).toBe(snapshot);
    expect(unsubscribe).toHaveBeenCalledOnce();
  });

  it('supplies canonical selected parent context and does not invent a private API method', async () => {
    const { client } = clientSetup();
    const data = testData();
    data.snapshot = selectExplorationTarget(data.snapshot, { kind: 'entity', entity_id: 'subject' }, null);
    const entityPath = data.snapshot.paths.find(p => p.path_id === data.snapshot.active_path_id)!;
    const parentId = entityPath.selected_occurrence_ids[0];
    data.snapshot = selectExplorationTarget(data.snapshot, { kind: 'facet', entity_id: 'subject', facet_key: 'unknown' }, parentId);
    vi.spyOn(client, 'research').mockResolvedValue(data.snapshot);
    const target = { kind: 'facet' as const, entity_id: 'subject', facet_key: 'unknown' };
    await createLiveAtlasAdapter(client).research({ query: 'Scoped research', parent: { data, target }, onStatus: vi.fn() }).result;
    expect(client.research).toHaveBeenCalledWith('Scoped research', {
      snapshot: data.snapshot, target, parentOccurrenceId: parentId,
    }, expect.any(AbortSignal));
  });

  it('waits for remote cancellation acknowledgement and reports cancellation failure', async () => {
    const { client, emit } = clientSetup();
    const ack = deferred<void>();
    vi.spyOn(client, 'research').mockImplementation((_q, _c, signal) => new Promise((_res, reject) => {
      signal?.addEventListener('abort', () => {
        void ack.promise.then(() => reject(signal.reason));
      });
    }));
    const op = createLiveAtlasAdapter(client).research({ query: 'subject', onStatus: vi.fn() });
    emit({ type: 'status', context, status: 'running' });
    let done = false;
    const cancelled = op.cancel().then(() => { done = true; });
    await Promise.resolve();
    expect(done).toBe(false);
    ack.resolve();
    await cancelled;
    await expect(op.result).rejects.toHaveProperty('name', 'AbortError');
    vi.mocked(client.research).mockImplementation((_q, _c, signal) => new Promise((_res, reject) => {
      signal?.addEventListener('abort', () => reject(new Error('Remote cancellation failed')));
    }));
    const failed = createLiveAtlasAdapter(client).research({ query: 'subject', onStatus: vi.fn() });
    await expect(failed.cancel()).rejects.toThrow('Remote cancellation failed');
  });

  it('does not claim remote acknowledgement when cancelled before a run ID arrives', async () => {
    const { client } = clientSetup();
    vi.spyOn(client, 'research').mockImplementation((_q, _c, signal) => new Promise((_res, reject) => {
      signal?.addEventListener('abort', () => reject(signal.reason));
    }));
    const op = createLiveAtlasAdapter(client).research({ query: 'subject', onStatus: vi.fn() });
    expect(await op.cancel()).toEqual(expect.objectContaining({ acknowledged: false, message: expect.stringContaining('before a run ID') }));
  });

  it('saves through immutable artifact API and preserves errors without local substitutes', async () => {
    const { client } = clientSetup();
    const data = testData();
    const saved = { artifactId: 'artifact', runId: data.runId, explorationId: data.snapshot.exploration_id, revision: 1,
      parentArtifactId: null, schemaVersion: 'atlas-exploration.v2' as const };
    vi.spyOn(client, 'save').mockResolvedValue(saved);
    vi.spyOn(client, 'load').mockResolvedValue(data.snapshot);
    vi.spyOn(client, 'listSaved').mockResolvedValue([saved]);
    const adapter = createLiveAtlasAdapter(client);
    expect(await adapter.save(data)).toEqual(saved);
    expect(client.save).toHaveBeenCalledWith(data.snapshot, data.runId);
    expect((await adapter.load('artifact')).snapshot).toBe(data.snapshot);
    expect(await adapter.recent(data.runId)).toEqual([saved]);
    expect(client.listSaved).toHaveBeenCalledWith(data.runId);
    vi.mocked(client.save).mockRejectedValue(new Error('HTTP 500'));
    await expect(adapter.save(data)).rejects.toThrow('HTTP 500');
  });

  it('binds approve/reject to exact batch digest and returns only actual server status', async () => {
    const { client } = clientSetup();
    vi.spyOn(client, 'decideApproval').mockResolvedValue({ ...batch, status: 'pending' });
    const adapter = createLiveAtlasAdapter(client);
    const approval = { id: batch.batch_id, runId: batch.run_id, digest: batch.batch_digest, status: batch.status,
      destination: batch.tool_calls[0].destination, args: batch.tool_calls[0].arguments };
    expect((await adapter.decide(approval, 'approve')).status).toBe('pending');
    expect(client.decideApproval).toHaveBeenCalledWith('batch', { decision: 'approve', batch_digest: batch.batch_digest });
    vi.mocked(client.decideApproval).mockResolvedValue({ ...batch, status: 'rejected' });
    await adapter.decide(approval, 'reject', 'Do not execute');
    expect(client.decideApproval).toHaveBeenLastCalledWith('batch', {
      decision: 'reject', batch_digest: batch.batch_digest, reason: 'Do not execute',
    });
  });

  it('projects explicitly structured claim attributes/checklists/drafts without guessing facts', () => {
    const data = testData();
    data.snapshot.claims[0].details = {
      attributes: [
        { key: 'price', label: 'Price', value: 120, unit: 'USD / session', uncertainty: 'Published estimate' },
        { key: 'credentials', label: 'Credentials', value: ['Public qualification'] },
      ],
      checklist: [{ id: 'prepare', text: 'Prepare questions', required: true }],
      draft: { title: 'Inquiry draft', body: 'Ask about availability' },
    };
    const presentation = claimPresentations(data.snapshot)[0];
    expect(presentation.attributes).toHaveLength(2);
    expect(presentation.attributes[0].unit).toBe('USD / session');
    expect(presentation.checklist?.[0].required).toBe(true);
    expect(presentation.draft?.body).toBe('Ask about availability');
    delete data.snapshot.claims[0].details;
    expect(claimPresentations(data.snapshot).filter(p => p.entityId === 'subject')).toEqual([]);
  });

  it('constructs the browser client with same-origin base and no browser credentials or env helper', () => {
    const fetch = vi.spyOn(globalThis, 'fetch');
    const adapter = createBrowserAtlasAdapter();
    expect(adapter.research).toBeTypeOf('function');
    expect(fetch).not.toHaveBeenCalled();
    fetch.mockRestore();
  });
});
