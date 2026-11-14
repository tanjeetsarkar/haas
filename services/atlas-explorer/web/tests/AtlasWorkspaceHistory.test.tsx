import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AtlasWorkspaceHistory } from '../src/components/AtlasWorkspaceHistory';
import { WorkspaceStorageApi } from '../src/workspaceStorageApi';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function setup() {
  const storage = new WorkspaceStorageApi();
  const item = { workspace_id: 'w', title: 'Topic', revision: 2, updated_at: '2026-11-10T00:00:00Z' };
  vi.spyOn(storage, 'catalog').mockResolvedValue({ items: [item], next_cursor: null });
  vi.spyOn(storage, 'latest').mockResolvedValue({ ...item,
    state: { title: 'Topic', draft: 'restored', operations: { op: { state: 'unknown' } } } });
  const beforeNavigate = vi.fn(async () => undefined);
  const onContinue = vi.fn();
  const onNew = vi.fn();
  return { storage, beforeNavigate, onContinue, onNew };
}
describe('workspace history checkpoint', () => {
  it('refreshes real-protocol history through browser fetch without masking invalid catalogs', async () => {
    const item = { workspace_id: 'w', title: 'Topic', revision: 2,
      created_at: '2026-11-10T03:45:02.204068+00:00', updated_at: '2026-11-10T03:45:02.307354+00:00' };
    let body: unknown = { items: [item], next_cursor: null };
    vi.stubGlobal('fetch', function (this: unknown) {
      if (this !== globalThis) throw new TypeError('Illegal invocation');
      return Promise.resolve(new Response(JSON.stringify(body),
        { headers: { 'Content-Type': 'application/json' } }));
    });
    render(<AtlasWorkspaceHistory storage={new WorkspaceStorageApi()}
      beforeNavigate={vi.fn(async () => undefined)} onContinue={vi.fn()} onNew={vi.fn()} />);
    await screen.findByRole('button', { name: 'Continue Topic' });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh history' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh history' })).toBeEnabled());
    expect(screen.queryByRole('alert')).toBeNull();
    body = { items: [{ ...item, revision: 'invalid' }], next_cursor: null };
    fireEvent.click(screen.getByRole('button', { name: 'Refresh history' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Workspace history unavailable');
  });

  it('continues via saved GET only and visibly blocks unresolved submissions', async () => {
    const props = setup();
    render(<AtlasWorkspaceHistory {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Continue Topic' }));
    await waitFor(() => expect(props.onContinue).toHaveBeenCalledOnce());
    expect(props.beforeNavigate).toHaveBeenCalledOnce();
    expect(props.storage.latest).toHaveBeenCalledWith('w');
    expect(screen.getByRole('alert')).toHaveTextContent('nothing was resubmitted');
  });
  it('Start afresh waits for flush, without deleting or submitting anything', async () => {
    const props = setup();
    render(<AtlasWorkspaceHistory {...props} />);
    await screen.findByRole('button', { name: 'Continue Topic' });
    fireEvent.click(screen.getByRole('button', { name: 'Start afresh' }));
    await waitFor(() => expect(props.onNew).toHaveBeenCalledOnce());
    expect(props.beforeNavigate).toHaveBeenCalledOnce();
    expect(props.storage.latest).not.toHaveBeenCalled();
  });
  it('does not replace local state when flushing fails', async () => {
    const props = setup();
    props.beforeNavigate.mockRejectedValue(new Error('save failed'));
    render(<AtlasWorkspaceHistory {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Continue Topic' }));
    await screen.findByRole('alert');
    expect(props.onContinue).not.toHaveBeenCalled();
    expect(props.storage.latest).not.toHaveBeenCalled();
  });
});
