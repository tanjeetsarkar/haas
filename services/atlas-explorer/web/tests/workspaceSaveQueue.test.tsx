import { describe, expect, it, vi } from 'vitest';
import {
  WorkspaceSaveQueue, type WorkspaceMutation, type WorkspaceReceipt,
} from '../src/workspaceSaveQueue';

type Kind = 'draft_checkpoint' | 'query_submitted' | 'operation_prepared' | 'branch_committed';
type Mutation = WorkspaceMutation<Kind, { text: string }>;
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}
function receipt(m: Mutation): WorkspaceReceipt {
  return { workspace_id: 'w', mutation_id: m.mutation_id,
    revision: m.expected_revision + 1, saved_at: '2026-11-10T00:00:00Z' };
}
function setup() {
  let id = 0;
  const mutate = vi.fn(async (_w: string, m: Mutation) => receipt(m));
  const latest = vi.fn(async () => ({ revision: 99 }));
  const queue = new WorkspaceSaveQueue<Kind, { text: string }, { revision: number }>(
    'w', 1, { mutate, latest }, () => `m${++id}`,
  );
  return { queue, mutate, latest };
}

describe('workspace receipt queue', () => {
  it('orders receipts and does not mark overlapping edits saved prematurely', async () => {
    const { queue, mutate } = setup();
    const first = deferred<WorkspaceReceipt>();
    mutate.mockImplementationOnce(() => first.promise);
    const a = queue.enqueue('query_submitted', { text: 'a' });
    await Promise.resolve();
    const b = queue.enqueue('operation_prepared', { text: 'b' });
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(queue.getState()).toMatchObject({ status: 'saving', revision: 1, pending: 2 });
    const states: string[] = [];
    queue.subscribe(s => states.push(`${s.status}:${s.pending}`));
    first.resolve(receipt(mutate.mock.calls[0][1]));
    await Promise.all([a, b, queue.flush()]);
    expect(mutate.mock.calls.map(c => c[1].expected_revision)).toEqual([1, 2]);
    expect(states).toContain('dirty:1');
    expect(queue.getState()).toMatchObject({ status: 'saved', revision: 3, pending: 0 });
  });

  it('coalesces only adjacent unsent drafts, never meaningful transitions', async () => {
    const { queue, mutate } = setup();
    const saves = [
      queue.enqueue('draft_checkpoint', { text: 'old' }),
      queue.enqueue('draft_checkpoint', { text: 'new' }),
      queue.enqueue('query_submitted', { text: 'query' }),
      queue.enqueue('operation_prepared', { text: 'intent' }),
      queue.enqueue('branch_committed', { text: 'branch' }),
      queue.enqueue('draft_checkpoint', { text: 'last' }),
    ];
    await Promise.all(saves);
    expect(mutate.mock.calls.map(c => c[1].payload.text)).toEqual(['new', 'query', 'intent', 'branch', 'last']);
    expect((await saves[0]).mutation_id).toBe((await saves[1]).mutation_id);
  });

  it('does not coalesce an already sent draft', async () => {
    const { queue, mutate } = setup();
    const pending = deferred<WorkspaceReceipt>();
    mutate.mockImplementationOnce(() => pending.promise);
    const a = queue.enqueue('draft_checkpoint', { text: 'first' });
    await Promise.resolve();
    const b = queue.enqueue('draft_checkpoint', { text: 'second' });
    pending.resolve(receipt(mutate.mock.calls[0][1]));
    await Promise.all([a, b]);
    expect(mutate).toHaveBeenCalledTimes(2);
  });

  it('retries an ambiguous failure with identical immutable body and ID', async () => {
    const { queue, mutate } = setup();
    mutate.mockRejectedValueOnce(new Error('credential/private response must not be displayed'));
    const payload = { text: 'intent' };
    const save = queue.enqueue('operation_prepared', payload);
    payload.text = 'changed outside';
    await expect(save).rejects.toThrow('not confirmed durable');
    const sent = mutate.mock.calls[0][1];
    expect(sent.payload.text).toBe('intent');
    expect(Object.isFrozen(sent.payload)).toBe(true);
    expect(queue.getState().error).not.toContain('credential');
    await expect(queue.flush()).rejects.toThrow('blocked');
    await queue.retry();
    expect(mutate.mock.calls[1][1]).toBe(sent);
    expect(queue.getState().revision).toBe(2);
  });

  it('blocks on 409, fetches authorized latest and never overwrites it', async () => {
    const { queue, mutate, latest } = setup();
    mutate.mockRejectedValueOnce({ status: 409 });
    const a = queue.enqueue('query_submitted', { text: 'query' });
    const b = queue.enqueue('operation_prepared', { text: 'intent' });
    await Promise.allSettled([a, b]);
    await Promise.resolve();
    expect(latest).toHaveBeenCalledWith('w');
    expect(queue.getState()).toMatchObject({ status: 'conflict', revision: 1, latest: { revision: 99 } });
    await expect(queue.retry()).rejects.toThrow('Only a failed');
    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it.each([413, 429, 503])('retains dirty changes after storage failure %s', async status => {
    const { queue, mutate } = setup();
    mutate.mockRejectedValueOnce({ status });
    await expect(queue.enqueue('draft_checkpoint', { text: 'unsaved' })).rejects.toThrow();
    expect(queue.getState()).toMatchObject({ status: 'failed', revision: 1, pending: 1 });
    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it('does not accept an invalid receipt as saved', async () => {
    const { queue, mutate } = setup();
    mutate.mockImplementationOnce(async (_w, m) => ({ ...receipt(m), mutation_id: 'other' }));
    await expect(queue.enqueue('query_submitted', { text: 'q' })).rejects.toThrow();
    expect(queue.getState()).toMatchObject({ status: 'failed', revision: 1 });
  });

  it('flush waits for the server and an empty flush does no work', async () => {
    const { queue, mutate } = setup();
    await queue.flush();
    expect(mutate).not.toHaveBeenCalled();
    const pending = deferred<WorkspaceReceipt>();
    mutate.mockImplementationOnce(() => pending.promise);
    const save = queue.enqueue('draft_checkpoint', { text: 'draft' });
    const flushed = vi.fn();
    const flush = queue.flush().then(flushed);
    await Promise.resolve();
    expect(flushed).not.toHaveBeenCalled();
    pending.resolve(receipt(mutate.mock.calls[0][1]));
    await Promise.all([save, flush]);
    expect(flushed).toHaveBeenCalledOnce();
  });
});
