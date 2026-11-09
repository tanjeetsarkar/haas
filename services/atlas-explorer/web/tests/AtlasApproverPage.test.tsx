import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AtlasApproverPage } from '../src/components/AtlasApproverPage';
import { createApproverAdapter, createBrowserApproverAdapter, type AtlasApproverAdapter } from '../src/approverAdapter';
import { deferred } from './workspaceTestData';
import type { AtlasApproval } from '../src/explorationAdapter';

afterEach(() => { cleanup(); vi.restoreAllMocks(); window.history.replaceState(null, '', '/'); });
const batch: AtlasApproval = {
  id: 'batch-actual', runId: 'run-actual', status: 'pending', digest: 'exact-server-digest',
  destination: 'https://destination.test/inquiry', args: { call: { body: 'Actual inquiry' } },
  calls: [{ invocationId: 'call', toolName: 'atlas_dispatch_inquiry',
    destination: 'https://destination.test/inquiry', args: { body: 'Actual inquiry' } }],
  expiresAt: '2026-11-09T15:00:00Z',
};
function adapter(): AtlasApproverAdapter {
  return {
    identity: vi.fn(async () => ({ principalId: 'operator-2', tenantId: 'tenant', roles: ['approver'] })),
    getApproval: vi.fn(async () => batch),
    decide: vi.fn(async (_approval, decision) => ({ ...batch, status: decision === 'approve' ? 'approved' : 'rejected' })),
  };
}
async function load() {
  await waitFor(() => expect(screen.getByRole('button', { name: 'Load approval batch' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Load approval batch' }));
  await screen.findByRole('region', { name: 'Loaded approval batch' });
}
describe('Independent operator approver page', () => {
  it('uses a batch reference only and requires explicit load/review/decision', async () => {
    window.history.replaceState(null, '', '/?atlasApprover=1&batch=batch-actual');
    const api = adapter();
    render(<AtlasApproverPage adapter={api} />);
    expect(api.getApproval).not.toHaveBeenCalled();
    await load();
    expect(api.getApproval).toHaveBeenCalledWith('batch-actual');
    const loaded = screen.getByRole('region', { name: 'Loaded approval batch' });
    expect(loaded).toHaveTextContent(batch.digest);
    expect(loaded).toHaveTextContent(batch.expiresAt!);
    expect(loaded).toHaveTextContent(batch.destination);
    expect(api.decide).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Review actual request' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('Actual inquiry');
    expect(dialog).toHaveTextContent('atlas_dispatch_inquiry');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve request' }));
    await waitFor(() => expect(api.decide).toHaveBeenCalledWith(batch, 'approve', undefined));
    expect(screen.getByRole('status')).toHaveTextContent('approved. This is not confirmation of external execution');
    expect(window.location.search).not.toContain('Actual');
    expect(window.location.search).not.toContain('digest');
  });

  it('dismisses without rejecting and preserves pending state after separate-authorizer 403', async () => {
    const api = adapter();
    vi.mocked(api.decide).mockRejectedValue(Object.assign(new Error('Separate approver principal required.'), { status: 403 }));
    render(<AtlasApproverPage adapter={api} />);
    fireEvent.change(screen.getByRole('textbox', { name: 'Approval batch id' }), { target: { value: batch.id } });
    await load();
    fireEvent.click(screen.getByRole('button', { name: 'Review actual request' }));
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss review' }));
    expect(api.decide).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Review actual request' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve request' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('HTTP 403: Separate approver principal required.'));
    expect(screen.getByRole('region', { name: 'Loaded approval batch' })).toHaveTextContent('pending');
  });

  it('does not fetch approvals on a submitter-role connection or after missing-config 401', async () => {
    const api = adapter();
    vi.mocked(api.identity).mockResolvedValue({ principalId: 'submitter', tenantId: 'tenant', roles: ['tenant_admin'] });
    const { unmount } = render(<AtlasApproverPage adapter={api} />);
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('does not report the approver role'));
    expect(screen.getByRole('button', { name: 'Load approval batch' })).toBeDisabled();
    expect(api.getApproval).not.toHaveBeenCalled();
    unmount();
    vi.mocked(api.identity).mockRejectedValue(Object.assign(new Error('Approver is not configured'), { status: 401 }));
    render(<AtlasApproverPage adapter={api} />);
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Authentication required'));
    expect(screen.getByRole('status')).toHaveTextContent('No authenticated approver connection');
    expect(api.getApproval).not.toHaveBeenCalled();
  });

  it('keeps load failures explicit and never assumes an expired request can execute', async () => {
    const api = adapter();
    vi.mocked(api.getApproval).mockRejectedValueOnce(new Error('Batch unavailable'))
      .mockResolvedValueOnce({ ...batch, status: 'expired' });
    render(<AtlasApproverPage adapter={api} />);
    fireEvent.change(screen.getByRole('textbox', { name: 'Approval batch id' }), { target: { value: batch.id } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Load approval batch' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Load approval batch' }));
    await screen.findByRole('alert');
    expect(screen.queryByRole('region', { name: 'Loaded approval batch' })).not.toBeInTheDocument();
    await load();
    fireEvent.click(screen.getByRole('button', { name: 'Review actual request' }));
    expect(screen.getByRole('button', { name: 'Approve request' })).toBeDisabled();
    expect(api.decide).not.toHaveBeenCalled();
  });

  it('ignores stale batch loads and never reveals the old batch after another load', async () => {
    const api = adapter(), pending = deferred<AtlasApproval>();
    vi.mocked(api.getApproval).mockReturnValueOnce(pending.promise);
    const { unmount } = render(<AtlasApproverPage adapter={api} />);
    fireEvent.change(screen.getByRole('textbox', { name: 'Approval batch id' }), { target: { value: batch.id } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Load approval batch' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Load approval batch' }));
    unmount();
    await act(async () => pending.resolve(batch));
    expect(screen.queryByRole('region', { name: 'Loaded approval batch' })).not.toBeInTheDocument();
  });
});

describe('Public approver adapter', () => {
  it('fetches and decides through public SDK methods with exact batch digest and no secrets', async () => {
    const serverBatch = { batch_id: batch.id, run_id: batch.runId, batch_digest: batch.digest, status: 'pending' as const,
      expires_at: batch.expiresAt!, created_at: 1, tool_calls: [{ invocation_id: 'call', tool_name: 'atlas_dispatch_inquiry',
        arguments: { exact: 'payload' }, destination: batch.destination, required_secrets: [] }] };
    const sdk = { getApproval: vi.fn(async () => serverBatch), decideApproval: vi.fn(async () => serverBatch) };
    const api = createApproverAdapter(sdk, async () => ({ principalId: 'operator', tenantId: 'tenant', roles: ['approver'] }));
    const actual = await api.getApproval(batch.id);
    expect(sdk.getApproval).toHaveBeenCalledWith(batch.id);
    await api.decide(actual, 'reject', 'Not authorized');
    expect(sdk.decideApproval).toHaveBeenCalledWith(batch.id, {
      decision: 'reject', batch_digest: batch.digest, reason: 'Not authorized',
    });
  });

  it('reads identity only from same-origin public /v1/me without credential headers', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      principal_id: 'operator', tenant_id: 'tenant', roles: ['approver'],
    }), { status: 200 }));
    const api = createBrowserApproverAdapter();
    expect(await api.identity()).toEqual({ principalId: 'operator', tenantId: 'tenant', roles: ['approver'] });
    expect(fetch).toHaveBeenCalledWith('/v1/me', { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    fetch.mockResolvedValue(new Response(JSON.stringify({ detail: 'Missing operator configuration' }), { status: 401 }));
    await expect(api.identity()).rejects.toHaveProperty('status', 401);
  });
});
