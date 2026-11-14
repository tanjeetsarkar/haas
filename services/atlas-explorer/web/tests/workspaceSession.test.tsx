import { describe, expect, it, vi } from 'vitest';
import { WorkspaceSession } from '../src/workspaceSession';
import { WorkspaceStorageApi, type WorkspaceDocument } from '../src/workspaceStorageApi';
import { deferred, testData } from './workspaceTestData';
import type { WorkspaceReceipt } from '../src/workspaceSaveQueue';

const document: WorkspaceDocument = { workspace_id: 'w', title: 'Topic', revision: 1,
  updated_at: '2026-11-10T00:00:00Z', state: { title: 'Topic', draft: 'question' } };
const prepare = { workspaceId: 'w', operationId: 'op', kind: 'operation_prepared' as const,
  payload: { operation_id: 'op', prompt: 'exact prompt', selected_context: [{ selected: 'subject' }] } };
const submitting = { workspaceId: 'w', operationId: 'op', kind: 'operation_outcome' as const,
  payload: { operation_id: 'op', state: 'submitting' as const, status: 'submitting' as const } };
const bound = { workspaceId: 'w', operationId: 'op', kind: 'operation_bound' as const,
  payload: { operation_id: 'op', run_id: 'run', thread_id: 'thread' } };
function setup(doc = document) {
  const storage = new WorkspaceStorageApi();
  const mutate = vi.spyOn(storage, 'mutate').mockImplementation(async (id, m) => ({
    workspace_id: id, mutation_id: m.mutation_id, revision: m.expected_revision + 1,
    saved_at: document.updated_at,
  }));
  return { storage, mutate, session: new WorkspaceSession(doc, storage) };
}
describe('durable operation adapter', () => {
  it('receipts the submitted question and prepared intent before awaited submitting/bind', async () => {
    const { session, mutate } = setup();
    const adapter = session.persistence('exact user question');
    await adapter.prepare(prepare);
    expect(session.canResearch()).toBe(false);
    expect(() => session.persistence('another query')).toThrow('blocked');
    await adapter.outcome(submitting);
    await adapter.bind(bound);
    expect(mutate.mock.calls.map(c => c[1].kind)).toEqual([
      'query_submitted', 'operation_prepared', 'operation_outcome', 'operation_bound',
    ]);
    expect(mutate.mock.calls[0][1].payload).toEqual({ prompt: 'exact user question' });
    expect(mutate.mock.calls[1][1].payload).toEqual(prepare.payload);
    expect(mutate.mock.calls.map(c => c[1].expected_revision)).toEqual([1, 2, 3, 4]);
    await adapter.outcome({ ...submitting, payload: { operation_id: 'op', state: 'terminal', status: 'completed' } });
    expect(session.canResearch()).toBe(true);
  });
  it('waits for durable prepare and fails closed on storage failure', async () => {
    const { session, mutate } = setup();
    const pending = deferred<WorkspaceReceipt>();
    mutate.mockImplementationOnce(() => pending.promise);
    const adapter = session.persistence('question');
    const prepared = adapter.prepare(prepare);
    await Promise.resolve();
    expect(mutate).toHaveBeenCalledOnce();
    pending.reject({ status: 503 });
    await expect(prepared).rejects.toThrow();
    expect(session.canResearch()).toBe(false);
    expect(mutate).toHaveBeenCalledOnce();
  });
  it('recovers a known binding with the identical body/ID and no second mutation', async () => {
    const { session, mutate } = setup();
    const adapter = session.persistence('question');
    await adapter.prepare(prepare);
    await adapter.outcome(submitting);
    mutate.mockRejectedValueOnce(new Error('lost receipt'));
    await expect(adapter.bind(bound)).rejects.toThrow();
    const original = mutate.mock.calls[3][1];
    expect(session.canResearch()).toBe(false);
    await adapter.bind(bound);
    expect(mutate.mock.calls[4][1]).toBe(original);
    await adapter.bind(bound);
    expect(mutate).toHaveBeenCalledTimes(5);
    await expect(adapter.bind({ ...bound, payload: { ...bound.payload, run_id: 'other' } })).rejects.toThrow('body changed');
  });
  it('records ambiguity without permitting another dispatch', async () => {
    const { session, mutate } = setup();
    const adapter = session.persistence('question');
    await adapter.prepare(prepare);
    await adapter.outcome(submitting);
    await adapter.outcome({ ...submitting, payload: { operation_id: 'op', state: 'unknown', status: 'unknown' } });
    expect(session.canResearch()).toBe(false);
    expect(mutate.mock.calls[3][1].payload).toEqual({ operation_id: 'op', state: 'unknown', status: 'unknown' });
  });
  it('rejects mismatched workspace callbacks before storage', async () => {
    const { session, mutate } = setup();
    const adapter = session.persistence('question');
    await expect(adapter.prepare({ ...prepare, workspaceId: 'other' })).rejects.toThrow('different workspace');
    await expect(adapter.bind({ ...bound, workspaceId: 'other' })).rejects.toThrow('different workspace');
    await expect(adapter.outcome({ ...submitting, workspaceId: 'other' })).rejects.toThrow('different workspace');
    expect(mutate).not.toHaveBeenCalled();
  });
  it('restores a strictly validated saved graph without calling storage or execution', async () => {
    const data = testData();
    const { session, mutate } = setup({ ...document, state: { ...document.state,
      branches: [JSON.parse(JSON.stringify({ snapshot: data.snapshot, run_id: data.runId }))],
      operations: { op: { state: 'unknown' } },
    } });
    expect(session.restoredData()).toEqual(data);
    expect(session.canResearch()).toBe(false);
    expect(mutate).not.toHaveBeenCalled();
  });
  it('refuses malformed restored graph data and commits only strict snapshots', async () => {
    const { session, mutate } = setup({ ...document, state: { ...document.state,
      branches: [{ snapshot: { malformed: true }, run_id: 'run' }],
    } });
    expect(() => session.restoredData()).toThrow();
    await session.commit(testData());
    expect(mutate.mock.calls[0][1].kind).toBe('branch_committed');
  });
});
