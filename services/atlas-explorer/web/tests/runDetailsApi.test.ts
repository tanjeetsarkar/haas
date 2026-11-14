import { describe, expect, it, vi } from 'vitest';
import { createRunDetailsApi, safePublicText, type RunDetailsSdk } from '../src/runDetailsApi';

function fakeSdk(): RunDetailsSdk {
  return {
    getRun: vi.fn(async run_id => ({ run_id, status: 'completed', created_at: 4 })),
    getRunSteps: vi.fn(async () => ({ items: [{ step_id: 's', run_id: 'r', attempt: 1, status: 'completed',
      started_at: 1, tool_calls: [{ invocation_id: 'call', tool_name: 'read', arguments: { query: 'hello',
        api_token: 'do-not-show' }, destination: '', required_secrets: [] }] }] })),
    getRunUsage: vi.fn(async run_id => ({ run_id, cost_usd: 0.1, model_tokens: { prompt: 2, completion: 3, total: 5 },
      tool_invocations: 1, egress_bytes: 0, artifact_bytes: 0 })),
    getThread: vi.fn(async (_id, params) => ({ thread_id: 't', history_version: 7,
      items: params?.offset ? [{ role: 'assistant', content: 'private reasoning' },
        { role: 'tool', name: 'search', tool_call_id: 'c2', content: '<b>api_key=abc</b>' }]
        : [{ role: 'system', content: 'private system prompt' },
          { role: 'tool', name: 'search', tool_call_id: 'c1', content: { result: 'public', password: 'dont-show' } }],
      next_offset: params?.offset ? null : 50 })),
  } as unknown as RunDetailsSdk;
}

describe('on-demand public run details', () => {
  it('uses SDK reads, verifies run identity, and omits unavailable metadata', async () => {
    const sdk = fakeSdk();
    vi.mocked(sdk.getRun).mockResolvedValueOnce({ run_id: 'wrong', status: 'completed', created_at: 1 });
    const api = createRunDetailsApi(sdk), result = await api.overview('r');
    expect(sdk.getRun).toHaveBeenCalledWith('r'); expect(sdk.getRunSteps).toHaveBeenCalledWith('r'); expect(sdk.getRunUsage).toHaveBeenCalledWith('r');
    expect(result.run).toBeUndefined();
    expect(safePublicText(result.steps?.items[0].tool_calls?.[0].arguments)).toContain('[redacted]');
  });

  it('pages only public tool messages and carries stable history version through SDK', async () => {
    const sdk = fakeSdk(), api = createRunDetailsApi(sdk), first = await api.historyPage('t');
    expect(first.entries).toHaveLength(1);
    expect(first.entries[0].content).toContain('[redacted]'); expect(first.entries[0].content).not.toContain('dont-show'); expect(first.entries[0].content).not.toContain('private system');
    const second = await api.historyPage('t', first.next);
    expect(second.entries[0].content).not.toContain('private reasoning');
    expect(sdk.getThread).toHaveBeenLastCalledWith('t', { offset: 50, history_version: 7 }); expect(second.next).toBeUndefined();
  });

  it('rejects invalid cursors and labels UTF-8 bounded content', async () => {
    const sdk = fakeSdk();
    vi.mocked(sdk.getThread).mockResolvedValueOnce({ thread_id: 't', history_version: 7, items: [], next_offset: 0 } as never);
    await expect(createRunDetailsApi(sdk).historyPage('t')).rejects.toThrow('cursor');
    const bounded = safePublicText('😀'.repeat(10000));
    expect(new TextEncoder().encode(bounded).length).toBeLessThanOrEqual(16 * 1024); expect(bounded).toContain('[truncated at 16KiB]');
    vi.mocked(sdk.getThread).mockResolvedValueOnce({ thread_id: 'other', history_version: 7, items: [], next_offset: null } as never);
    await expect(createRunDetailsApi(sdk).historyPage('t')).rejects.toThrow('identity mismatch');
  });
});
