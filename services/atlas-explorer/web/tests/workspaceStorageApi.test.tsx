import { afterEach, describe, expect, it, vi } from 'vitest';
import { blocksResearch, WorkspaceStorageApi, type WorkspaceDocument } from '../src/workspaceStorageApi';

const saved = { workspace_id: 'w', mutation_id: 'm', revision: 1, saved_at: '2026-11-10T00:00:00Z' };
const document: WorkspaceDocument = { workspace_id: 'w', title: 'Topic', revision: 1,
  updated_at: saved.saved_at, state: { title: 'Topic', draft: 'exact draft' } };
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
}
afterEach(() => vi.unstubAllGlobals());
describe('042 browser workspace protocol', () => {
  it('calls browser fetch with its global receiver and parses the actual catalog metadata', async () => {
    const catalog = { items: [{
      workspace_id: '9945b231-d288-4722-b838-f7c0a4a6d5bc', title: 'Local Atlas readiness check',
      revision: 2, created_at: '2026-11-10T03:45:02.204068+00:00',
      updated_at: '2026-11-10T03:45:02.307354+00:00',
    }], next_cursor: null };
    vi.stubGlobal('fetch', function (this: unknown) {
      if (this !== globalThis) throw new TypeError('Illegal invocation');
      return Promise.resolve(response(catalog));
    });
    expect(await new WorkspaceStorageApi().catalog()).toEqual(catalog);
  });
  it('uses only fixed same-origin routes and preserves the exact create envelope', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(saved));
    const api = new WorkspaceStorageApi(fetcher);
    expect(await api.create('Topic', 'exact draft', 'm')).toEqual(saved);
    expect(fetcher).toHaveBeenCalledWith('/__atlas/workspaces', expect.objectContaining({
      method: 'POST', credentials: 'same-origin', redirect: 'error',
      body: JSON.stringify({ schema_version: 1, mutation_id: 'm', title: 'Topic', draft: 'exact draft' }),
    }));
    expect(fetcher.mock.calls[0][1]?.headers).not.toHaveProperty('Authorization');
  });
  it('loads catalog and draft without any dispatch or mutation requests', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response({ items: [document], next_cursor: null }))
      .mockResolvedValueOnce(response(document));
    const api = new WorkspaceStorageApi(fetcher);
    await api.catalog('cursor/&');
    expect(await api.latest('w')).toEqual(document);
    expect(fetcher.mock.calls.map(c => c[0])).toEqual([
      '/__atlas/workspaces?limit=20&cursor=cursor%2F%26', '/__atlas/workspaces/w',
    ]);
    expect(fetcher.mock.calls.every(c => c[1]?.method === 'GET')).toBe(true);
  });
  it.each([401, 403, 409, 413, 429, 503])('reports HTTP %s without private response text or retry', async status => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ secret: 'private' }, status));
    const api = new WorkspaceStorageApi(fetcher);
    await expect(api.catalog()).rejects.toMatchObject({ status });
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it('rejects HTML fallback, malformed state, and mismatched create receipts', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response('<html>fallback</html>'))
      .mockResolvedValueOnce(response({ ...document, state: {} }))
      .mockResolvedValueOnce(response({ ...saved, mutation_id: 'wrong' }));
    const api = new WorkspaceStorageApi(fetcher);
    await expect(api.catalog()).rejects.toThrow('non-JSON');
    await expect(api.latest('w')).rejects.toThrow('Invalid workspace state');
    await expect(api.create('Topic', '', 'm')).rejects.toThrow('mismatch');
  });
  it.each(['prepared', 'submitting', 'unknown', undefined] as const)('blocks unresolved intent %s without replay', state => {
    expect(blocksResearch({ ...document, state: { ...document.state,
      operations: { op: { state } } } })).toBe(true);
  });
  it('does not block a bound/terminal observation, and never needs a new request to decide', () => {
    expect(blocksResearch({ ...document, state: { ...document.state,
      operations: { op: { state: 'bound', run_id: 'run' }, older: { state: 'terminal' } } } })).toBe(false);
  });
});
