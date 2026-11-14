import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { getExplorationBreadcrumbs, type AtlasOperationPersistence } from '@haas/atlas-explorer-client';
import { AtlasWorkspace, type AtlasDurableLifecycle } from '../src/components/AtlasWorkspace';
import type { AtlasWorkspaceData } from '../src/explorationAdapter';
import { deferred, testAdapter, testData } from './workspaceTestData';

afterEach(() => { cleanup(); window.history.replaceState(null, '', '/'); });
function lifecycle(data: AtlasWorkspaceData | null = null): AtlasDurableLifecycle {
  const ack = async () => ({ workspace_id: 'w', mutation_id: 'm', revision: 1, saved_at: '2026-11-10T00:00:00Z' });
  const p: AtlasOperationPersistence = { workspaceId: 'w', prepare: ack, outcome: ack, bind: ack };
  return { workspaceId: 'w', query: '', data, beforeResearch: vi.fn(async () => p),
    commit: vi.fn(async () => undefined), checkpoint: vi.fn(async () => undefined),
    select: vi.fn(async () => undefined) };
}
function completed(data: AtlasWorkspaceData): AtlasWorkspaceData {
  const snapshot = structuredClone(data.snapshot);
  const selected = getExplorationBreadcrumbs(snapshot);
  snapshot.requests.push({ ...snapshot.requests[0], request_id: `child-${snapshot.requests.length}`,
    path_id: snapshot.active_path_id!, parent_occurrence_id: selected[selected.length - 1].occurrence_id,
    run_id: 'child-run', status: 'completed' });
  snapshot.revision++;
  return { ...data, snapshot, runId: 'child-run' };
}
describe('durable scoped selection wiring', () => {
  it('keeps ordinary cached revisit free but explicitly Research again prepares one new scoped revision', async () => {
    const adapter = testAdapter();
    const durable = lifecycle(testData());
    vi.mocked(adapter.research).mockImplementation(input => ({
      cancel: vi.fn(async () => undefined), result: Promise.resolve(completed(input.parent!.data)),
    }));
    render(<AtlasWorkspace adapter={adapter} durable={durable} />);
    fireEvent.click(screen.getByRole('button', { name: /EntityResearch subject/ }));
    await screen.findByRole('button', { name: 'Research again' });
    expect(adapter.research).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: /EntityResearch subject/ }));
    expect(adapter.research).toHaveBeenCalledOnce();
    const previousRevision = vi.mocked(durable.commit).mock.calls[0][0].snapshot.revision;
    fireEvent.click(screen.getByRole('button', { name: 'Research again' }));
    await waitFor(() => expect(adapter.research).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(durable.commit).toHaveBeenCalledTimes(2));
    expect(durable.beforeResearch).toHaveBeenCalledTimes(2);
    expect(vi.mocked(adapter.research).mock.calls[1][0]).toMatchObject({
      query: 'Research subject', parent: { target: { kind: 'entity', entity_id: 'subject' } },
    });
    expect(vi.mocked(durable.commit).mock.calls[1][0].snapshot.revision).toBeGreaterThan(previousRevision);
  });
  it('creates one root and one child, opens source evidence and revisits cache without spending', async () => {
    const adapter = testAdapter();
    vi.mocked(adapter.research).mockImplementation(input => ({
      cancel: vi.fn(async () => undefined),
      result: Promise.resolve(input.parent ? completed(input.parent.data) : testData()),
    }));
    render(<AtlasWorkspace adapter={adapter} durable={lifecycle()} />);
    fireEvent.change(screen.getByRole('textbox', { name: /what would you like/i }), { target: { value: 'Research subject' } });
    fireEvent.click(screen.getByRole('button', { name: 'Research' }));
    await screen.findByRole('region', { name: 'Starting points column' });
    expect(adapter.research).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: /EntityResearch subject/ }));
    await waitFor(() => expect(adapter.research).toHaveBeenCalledTimes(2));
    await screen.findByText('Research complete.');
    expect(vi.mocked(adapter.research).mock.calls[1][0].query).toBe('Research subject');
    fireEvent.click(screen.getByRole('button', { name: /SourceResearch report/ }));
    expect(screen.getByRole('status')).toHaveTextContent('Source evidence opened');
    expect(adapter.research).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole('button', { name: /EntityResearch subject/ }));
    expect(screen.getByRole('status')).toHaveTextContent('Restored cached branch');
    expect(adapter.research).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole('button', { name: /FacetPublished details/ }));
    await waitFor(() => expect(adapter.research).toHaveBeenCalledTimes(3));
  });
  it('suppresses double-click while one scoped operation is in flight', async () => {
    const adapter = testAdapter();
    const pending = deferred<AtlasWorkspaceData>();
    let child: AtlasWorkspaceData | undefined;
    vi.mocked(adapter.research).mockImplementation(input => {
      child = completed(input.parent!.data);
      return { result: pending.promise, cancel: vi.fn(async () => undefined), detach: vi.fn() };
    });
    render(<AtlasWorkspace adapter={adapter} durable={lifecycle(testData())} />);
    const row = screen.getByRole('button', { name: /EntityResearch subject/ });
    fireEvent.click(row);
    fireEvent.click(row);
    await waitFor(() => expect(adapter.research).toHaveBeenCalledOnce());
    pending.resolve(child!);
    await screen.findByText('Research complete.');
    expect(adapter.research).toHaveBeenCalledOnce();
  });
  it('preserves parent and sibling results on child failure and does not dive unavailable facets', async () => {
    const adapter = testAdapter();
    const data = testData();
    data.snapshot.entities[0].facets[0] = { key: 'known', label: 'Published details', scope: 'Details',
      status: 'unavailable', reason: 'Publication unavailable' };
    vi.mocked(adapter.research).mockReturnValue({ result: Promise.reject(new Error('child failed')),
      cancel: vi.fn(async () => undefined) });
    render(<AtlasWorkspace adapter={adapter} durable={lifecycle(data)} />);
    fireEvent.click(screen.getByRole('button', { name: /EntityResearch subject/ }));
    await screen.findByRole('alert');
    expect(screen.getByRole('region', { name: 'Starting points column' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Research subject column' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /FacetPublished details/ }));
    expect(screen.getByRole('status')).toHaveTextContent('Publication unavailable');
    expect(adapter.research).toHaveBeenCalledOnce();
  });
});
