import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AtlasDurableWorkspace } from '../src/components/AtlasDurableWorkspace';
import { WorkspaceStorageApi, type WorkspaceDocument } from '../src/workspaceStorageApi';
import { deferred, testAdapter, testData } from './workspaceTestData';
import type { WorkspaceReceipt } from '../src/workspaceSaveQueue';
import type { AtlasExecutionContext } from '@haas/atlas-explorer-client';

afterEach(() => { cleanup(); window.history.replaceState(null, '', '/'); });
const document: WorkspaceDocument = { workspace_id: 'w', title: 'Topic', revision: 1,
  updated_at: '2026-11-10T00:00:00Z', state: { title: 'Topic', draft: 'saved question' } };
function setup(doc = document) {
  const storage = new WorkspaceStorageApi();
  vi.spyOn(storage, 'catalog').mockResolvedValue({ items: [doc], next_cursor: null });
  vi.spyOn(storage, 'create').mockResolvedValue({ workspace_id: 'w', mutation_id: 'create',
    revision: 1, saved_at: doc.updated_at });
  vi.spyOn(storage, 'latest').mockResolvedValue(doc);
  const mutate = vi.spyOn(storage, 'mutate').mockImplementation(async (id, m) => ({
    workspace_id: id, mutation_id: m.mutation_id, revision: m.expected_revision + 1,
    saved_at: doc.updated_at,
  }));
  return { storage, mutate, adapter: testAdapter() };
}
function submit() {
  fireEvent.change(screen.getByRole('textbox', { name: /what would you like/i }), { target: { value: 'my exact question' } });
  fireEvent.click(screen.getByRole('button', { name: 'Research' }));
}
describe('durable workspace integration entry point', () => {
  it('hides manual save while retaining artifact import without research or artifact writes', async () => {
    const { storage, adapter } = setup();
    window.history.replaceState(null, '', '/?workspace=w');
    vi.mocked(adapter.load).mockResolvedValue(testData());
    render(<AtlasDurableWorkspace storage={storage} adapter={adapter} />);
    const artifact = await screen.findByRole('textbox', { name: 'Artifact id' });
    expect(screen.queryByRole('button', { name: 'Save new artifact snapshot' })).toBeNull();
    fireEvent.change(artifact, { target: { value: 'legacy-artifact' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load artifact' }));
    await screen.findByRole('textbox', { name: 'Saved artifact id' });
    expect(adapter.load).toHaveBeenCalledWith('legacy-artifact');
    expect(adapter.save).not.toHaveBeenCalled();
    expect(adapter.research).not.toHaveBeenCalled();
    expect(screen.queryByText(/not saved until|until.*explicit save/i)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save new artifact snapshot' })).toBeNull();
  });

  it.each(['receipt', 'failure', 'conflict'] as const)(
    'shows pending saves until the server returns %s, including edits after research', async outcome => {
      const { storage, mutate, adapter } = setup();
      render(<AtlasDurableWorkspace storage={storage} adapter={adapter} />);
      submit();
      await screen.findByText('Research complete.');
      const saveStatus = within(screen.getByRole('region', { name: 'Workspace server save state' }));
      await waitFor(() => expect(saveStatus.getByRole('status')).toHaveTextContent(/^Saved · server revision/));
      const pending = deferred<WorkspaceReceipt>();
      mutate.mockImplementationOnce(() => pending.promise);
      const query = screen.getByRole('textbox', { name: /what would you like/i });
      fireEvent.change(query, { target: { value: 'later draft' } });
      fireEvent.blur(query);
      await waitFor(() => expect(saveStatus.getByRole('status')).toHaveTextContent('Saving — not yet confirmed'));
      expect(screen.queryByText(/Workspace saved by the server|not saved until/i)).toBeNull();
      await act(async () => {
        const mutation = mutate.mock.calls.at(-1)![1];
        if (outcome === 'receipt') pending.resolve({
          workspace_id: 'w', mutation_id: mutation.mutation_id,
          revision: mutation.expected_revision + 1, saved_at: document.updated_at,
        });
        else pending.reject({ status: outcome === 'conflict' ? 409 : 503 });
      });
      await waitFor(() => expect(saveStatus.getByRole('status')).toHaveTextContent(
        outcome === 'receipt' ? /^Saved · server revision/
          : outcome === 'conflict' ? 'Conflict — dispatch blocked' : 'Save failed — changes remain unsaved',
      ));
      expect(adapter.save).not.toHaveBeenCalled();
      expect(adapter.research).toHaveBeenCalledOnce();
    },
  );

  it('flushes before history returns to Start afresh, detaches and never saves into the old workspace', async () => {
    const { storage, mutate, adapter } = setup();
    window.history.replaceState(null, '', '/?workspace=w');
    render(<AtlasDurableWorkspace storage={storage} adapter={adapter} />);
    const query = await screen.findByRole('textbox', { name: /what would you like/i });
    const pending = deferred<WorkspaceReceipt>();
    mutate.mockImplementationOnce(() => pending.promise);
    fireEvent.change(query, { target: { value: 'old pending edit' } });
    fireEvent.blur(query);
    await waitFor(() => expect(mutate).toHaveBeenCalledOnce());
    window.history.pushState(null, '', '/');
    fireEvent.popState(window);
    expect(screen.queryByText('Not saved yet')).toBeNull();
    await act(async () => pending.resolve({
      workspace_id: 'w', mutation_id: mutate.mock.calls[0][1].mutation_id,
      revision: 2, saved_at: document.updated_at,
    }));
    await screen.findByText('Not saved yet');
    const fresh = screen.getByRole('textbox', { name: /what would you like/i });
    expect(fresh).toHaveValue('');
    vi.mocked(storage.create).mockResolvedValue({ workspace_id: 'new', mutation_id: 'new-create',
      revision: 1, saved_at: document.updated_at });
    vi.mocked(storage.latest).mockResolvedValue({ ...document, workspace_id: 'new' });
    fireEvent.change(fresh, { target: { value: 'new workspace draft' } });
    fireEvent.blur(fresh);
    await waitFor(() => expect(mutate).toHaveBeenCalledTimes(2));
    expect(storage.create).toHaveBeenCalledOnce();
    expect(mutate.mock.calls.map(call => call[0])).toEqual(['w', 'new']);
    expect(adapter.research).not.toHaveBeenCalled();
  });

  it('saves an explicit custom card draft before showing saved and never dispatches it', async () => {
    const { storage, mutate, adapter } = setup();
    window.history.replaceState(null, '', '/?workspace=w');
    render(<AtlasDurableWorkspace storage={storage} adapter={adapter} />);
    fireEvent.change(await screen.findByRole('textbox', { name: /what would you like/i }),
      { target: { value: 'public topic' } });
    fireEvent.click(screen.getByRole('button', { name: 'Research' }));
    const actions = await screen.findByRole('region', { name: 'Research subject actions' });
    await waitFor(() => expect(screen.getByText('Research complete.')).toBeInTheDocument());
    const beforeAction = vi.mocked(adapter.research).mock.calls.length;
    fireEvent.click(within(actions).getByRole('button', { name: 'Custom action' }));
    fireEvent.change(within(actions).getByRole('textbox', { name: 'Describe the action to draft' }),
      { target: { value: 'Prepare a question about the published details' } });
    fireEvent.click(within(actions).getByRole('button', { name: 'Save custom draft' }));
    await waitFor(() => expect(within(actions).getByRole('status'))
      .toHaveTextContent('Draft saved locally to this workspace'));
    const mutation = mutate.mock.calls.at(-1)?.[1];
    expect(mutation).toMatchObject({
      kind: 'action_draft_changed',
      payload: { action_draft: { drafts: { 'entity:subject': { intent_text: 'Prepare a question about the published details' } } } },
    });
    expect(vi.mocked(adapter.research)).toHaveBeenCalledTimes(beforeAction);
    expect(storage.create).not.toHaveBeenCalled();
  });

  it('restores a matching custom draft without starting research or writing storage', async () => {
    const graph = testData().snapshot;
    const saved = {
      schema_version: 'atlas-action-draft.v1', proposal_id: 'local-saved',
      card_reference: { card_id: 'entity:subject', workspace_revision: '1', branch_revision: 'root' },
      intent_text: 'Restored custom request',
      capability: { capability_id: null, schema_version: null, resolution: 'unverified', reason: 'No catalog.' },
      proposed_arguments: [], evidence_refs: ['source'], missing_fields: [],
      warnings: ['Locally saved custom draft; not model-generated.'],
      external_execution: { status: 'unavailable',
        reason: 'External execution unavailable — authorization release gates pending' },
    };
    const restored = { ...document, state: { ...document.state,
      branches: [{ snapshot: graph, run_id: 'run-1' }],
      action_draft: { drafts: { 'entity:subject': saved } },
    } };
    const { storage, mutate, adapter } = setup(restored);
    window.history.replaceState(null, '', '/?workspace=w');
    render(<AtlasDurableWorkspace storage={storage} adapter={adapter} />);
    const actions = await screen.findByRole('region', { name: 'Research subject actions' });
    fireEvent.click(within(actions).getByRole('button', { name: 'Custom action' }));
    expect(within(actions).getByRole('textbox', { name: 'Describe the action to draft' }))
      .toHaveValue('Restored custom request');
    expect(adapter.research).not.toHaveBeenCalled();
    expect(mutate).not.toHaveBeenCalled();
    expect(storage.create).not.toHaveBeenCalled();
  });
  it('restores saved operation prompt/context and submitted question without transient events', async () => {
    const context: AtlasExecutionContext = { workspaceId: 'w', operationId: 'op', runId: 'run',
      threadId: 'thread', requestId: 'req', explorationId: 'explore', pathId: 'root',
      parentOccurrenceId: null, baseRevision: 1, historyVersion: 1 };
    const { storage, adapter } = setup({ ...document, state: { ...document.state,
      selection: { execution: JSON.parse(JSON.stringify(context)), submitted_question: 'exact saved question' },
      operations: { op: { state: 'terminal', run_id: 'run', thread_id: 'thread',
        prompt: '<img src=x> Bearer saved-secret', selected_context: [{ subject: 'saved subject' }] } },
    } });
    window.history.replaceState(null, '', '/?workspace=w');
    render(<AtlasDurableWorkspace storage={storage} adapter={adapter} />);
    const summary = await screen.findByText('Run execution · run');
    const drawer = summary.closest('details')!;
    expect(within(drawer).getByText('exact saved question')).toBeInTheDocument();
    expect(within(drawer).getByText('<img src=x> [redacted]')).toBeInTheDocument();
    expect(within(drawer).getByText('[{"subject":"saved subject"}]')).toBeInTheDocument();
    expect(drawer.querySelector('img')).toBeNull();
    expect(drawer.textContent).not.toContain('saved-secret');
    expect(adapter.research).not.toHaveBeenCalled();
  });
  it('does not silently create a replacement workspace after hard-reload recovery fails', async () => {
    const { storage, adapter } = setup();
    vi.mocked(storage.latest).mockRejectedValue({ status: 503 });
    window.history.replaceState(null, '', '/?workspace=w');
    render(<AtlasDurableWorkspace storage={storage} adapter={adapter} />);
    await screen.findByText(/Saved workspace could not be restored/);
    submit();
    await screen.findByText(/Explicitly Start afresh/);
    expect(storage.create).not.toHaveBeenCalled();
    expect(adapter.research).not.toHaveBeenCalled();
  });
  it('reload reattaches observation to the saved run/cursor only and aborts observation on departure', async () => {
    const context: AtlasExecutionContext = { workspaceId: 'w', operationId: 'op', runId: 'run',
      threadId: 'thread', requestId: 'req', explorationId: 'explore', pathId: 'root',
      parentOccurrenceId: null, baseRevision: 1, historyVersion: 1 };
    const { storage, adapter } = setup({ ...document, state: { ...document.state,
      selection: { execution: JSON.parse(JSON.stringify(context)), last_event_id: '9' },
      operations: { op: { state: 'bound', run_id: 'run', thread_id: 'thread' } },
    } });
    const resume = vi.fn(async (input: Parameters<NonNullable<typeof adapter.resume>>[0]) => ({
      context: input.context, status: 'running' as const, lastEventId: '9', terminal: false, disconnected: true,
    }));
    adapter.resume = resume;
    window.history.replaceState(null, '', '/?workspace=w');
    const view = render(<AtlasDurableWorkspace storage={storage} adapter={adapter} />);
    await waitFor(() => expect(resume).toHaveBeenCalledOnce());
    expect(resume.mock.calls[0][0]).toMatchObject({ context, lastEventId: '9' });
    expect(adapter.research).not.toHaveBeenCalled();
    expect(storage.create).not.toHaveBeenCalled();
    await screen.findByRole('button', { name: 'Reconnect recorded run' });
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect recorded run' }));
    await waitFor(() => expect(resume).toHaveBeenCalledTimes(2));
    expect(resume.mock.calls[1][0].context.runId).toBe('run');
    expect(adapter.research).not.toHaveBeenCalled();
    window.history.pushState(null, '', '/');
    fireEvent.popState(window);
    await screen.findByText('Not saved yet');
    expect(resume.mock.calls[1][0].signal.aborted).toBe(true);
    expect(screen.queryByText('Run execution · run')).toBeNull();
    view.unmount();
    expect(resume.mock.calls[1][0].signal.aborted).toBe(true);
  });
  it('does not guess missing restored execution correlation', async () => {
    const { storage, adapter } = setup({ ...document, state: { ...document.state,
      operations: { op: { state: 'bound', run_id: 'run', thread_id: 'thread' } },
    } });
    adapter.resume = vi.fn();
    window.history.replaceState(null, '', '/?workspace=w');
    render(<AtlasDurableWorkspace storage={storage} adapter={adapter} />);
    await screen.findByText(/complete saved correlation/);
    expect(adapter.resume).not.toHaveBeenCalled();
    expect(adapter.research).not.toHaveBeenCalled();
  });
  it('refuses dispatch when storage is 503 and does not claim successful autosave', async () => {
    const { storage, adapter } = setup();
    vi.mocked(storage.create).mockRejectedValue({ status: 503 });
    render(<AtlasDurableWorkspace storage={storage} adapter={adapter} />);
    submit();
    await screen.findByText(/Workspace creation unavailable/);
    expect(adapter.research).not.toHaveBeenCalled();
    expect(screen.getByText('Not saved yet')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save new artifact snapshot' })).toBeNull();
  });
  it('passes durable callbacks and commits only strict completed graph after server receipts', async () => {
    const { storage, mutate, adapter } = setup();
    vi.mocked(adapter.research).mockImplementation(input => ({ cancel: vi.fn(async () => undefined), result: (async () => {
      const p = input.operationPersistence!;
      await p.prepare({ workspaceId: 'w', operationId: 'op', kind: 'operation_prepared',
        payload: { operation_id: 'op', prompt: 'application prompt', selected_context: [] } });
      await p.outcome({ workspaceId: 'w', operationId: 'op', kind: 'operation_outcome',
        payload: { operation_id: 'op', state: 'submitting', status: 'submitting' } });
      await p.bind({ workspaceId: 'w', operationId: 'op', kind: 'operation_bound',
        payload: { operation_id: 'op', run_id: 'run', thread_id: 'thread' } });
      await p.outcome({ workspaceId: 'w', operationId: 'op', kind: 'operation_outcome',
        payload: { operation_id: 'op', state: 'terminal', status: 'completed' } });
      return testData();
    })() }));
    render(<AtlasDurableWorkspace storage={storage} adapter={adapter} />);
    submit();
    await screen.findByText('Research complete.');
    expect(mutate.mock.calls.slice(0, 4).map(c => c[1].kind)).toEqual([
      'query_submitted', 'operation_prepared', 'operation_outcome', 'operation_bound',
    ]);
    expect(mutate.mock.calls[0][1].payload).toEqual({ prompt: 'my exact question' });
    expect(mutate.mock.calls.some(c => c[1].kind === 'branch_committed')).toBe(true);
    expect(adapter.research).toHaveBeenCalledOnce();
    expect(window.location.search).toContain('workspace=w');
  });
  it('hard reload restores graph/draft and blocks ambiguous operations with zero research requests', async () => {
    const data = testData();
    window.history.replaceState(null, '', '/?workspace=w');
    const { storage, adapter } = setup({ ...document, state: { ...document.state,
      branches: [JSON.parse(JSON.stringify({ snapshot: data.snapshot, run_id: data.runId }))],
      operations: { op: { state: 'unknown' } },
    } });
    render(<AtlasDurableWorkspace storage={storage} adapter={adapter} />);
    await screen.findByRole('region', { name: 'Starting points column' });
    expect(screen.getByRole('textbox', { name: /what would you like/i })).toHaveValue('saved question');
    expect(adapter.research).not.toHaveBeenCalled();
    submit();
    await waitFor(() => expect(screen.getByText(/Unresolved workspace submission/)).toBeInTheDocument());
    expect(adapter.research).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Start afresh' }));
    await screen.findByText('Not saved yet');
    expect(adapter.research).not.toHaveBeenCalled();
    expect(storage.create).not.toHaveBeenCalled();
  });
});
