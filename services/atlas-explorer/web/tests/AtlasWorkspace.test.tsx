import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AtlasWorkspace } from '../src/components/AtlasWorkspace';
import type { AtlasApproval, AtlasWorkspaceData } from '../src/explorationAdapter';
import { deferred, testAdapter, testData } from './workspaceTestData';
import { StrictMode } from 'react';

afterEach(() => { cleanup(); window.history.replaceState(null, '', '/'); });
function submit(query = 'an arbitrary unknown subject') {
  fireEvent.change(screen.getByRole('textbox', { name: /what would you like/i }), { target: { value: query } });
  fireEvent.click(screen.getByRole('button', { name: 'Research' }));
}
async function loaded() {
  await screen.findByRole('region', { name: 'Starting points column' });
}
function open(name = /EntityResearch subject/) {
  fireEvent.click(screen.getByRole('button', { name }));
}

describe('Live Atlas workspace', () => {
  it('natively submits only the research form despite an invalid empty artifact input under StrictMode', async () => {
    const adapter = testAdapter();
    render(<StrictMode><AtlasWorkspace adapter={adapter} /></StrictMode>);
    const query = screen.getByRole('textbox', { name: /what would you like/i }) as HTMLInputElement;
    const artifact = screen.getByRole('textbox', { name: 'Artifact id' }) as HTMLInputElement;
    const researchForm = screen.getByRole('form', { name: 'Live research' }) as HTMLFormElement;
    const artifactForm = artifact.closest('form')!;
    const research = screen.getByRole('button', { name: 'Research' }) as HTMLButtonElement;
    fireEvent.change(query, { target: { value: 'computers' } });
    expect(query.form).toBe(researchForm);
    expect(research.form).toBe(researchForm);
    expect(artifact.form).toBe(artifactForm);
    expect(artifact.checkValidity()).toBe(false);
    expect(artifactForm.checkValidity()).toBe(false);
    expect(researchForm.checkValidity()).toBe(true);
    act(() => research.click());
    await waitFor(() => expect(adapter.research).toHaveBeenCalledWith(expect.objectContaining({ query: 'computers' })));
    await loaded();
    expect(adapter.load).not.toHaveBeenCalled();
    expect(screen.getByRole('status')).toHaveTextContent('Research complete');
  });

  it('surfaces a 403 budget policy detail and retries scoped research with parent columns retained', async () => {
    const adapter = testAdapter();
    const detail = 'Requested max_cost_usd exceeds tenant policy.';
    vi.mocked(adapter.research).mockImplementation(input => {
      if (!input.parent) return { result: Promise.resolve(testData()), cancel: vi.fn(async () => undefined) };
      return { result: Promise.reject(Object.assign(new Error(detail), { name: 'HaaSAPIError', status: 403, details: detail })),
        cancel: vi.fn(async () => undefined) };
    });
    render(<AtlasWorkspace adapter={adapter} />);
    submit(); await loaded(); open(); open(/FacetOpen question/);
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(`HTTP 403: ${detail}`));
    expect(screen.getByRole('alert')).not.toHaveTextContent('role grants');
    expect(screen.getByRole('region', { name: 'Starting points column' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Research subject column' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Research failed');
    vi.mocked(adapter.research).mockImplementation(input => ({
      result: Promise.resolve(input.parent!.data), cancel: vi.fn(async () => undefined),
    }));
    fireEvent.click(screen.getByRole('button', { name: 'Research this facet' }));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Research complete'));
    expect(adapter.research).toHaveBeenCalledTimes(3);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Research subject column' })).toBeInTheDocument();
  });

  it('does not display entity-wide claims or source evidence in an unrelated facet inspector', async () => {
    const adapter = testAdapter(), data = testData();
    data.snapshot.sources.push({ ...data.snapshot.sources[0], source_id: 'entity-source', title: 'Entity-only evidence' });
    data.snapshot.claims.push({ ...data.snapshot.claims[0], claim_id: 'entity-claim', facet_key: undefined,
      text: 'Entity-wide finding only', details: undefined,
      evidence: { status: 'sourced', source_ids: ['entity-source'] } });
    vi.mocked(adapter.research).mockReturnValue({ result: Promise.resolve(data), cancel: vi.fn(async () => undefined) });
    render(<AtlasWorkspace adapter={adapter} />);
    submit(); await loaded(); open();
    const inspector = screen.getByRole('complementary', { name: 'Evidence and details' });
    expect(inspector).toHaveTextContent('Entity-wide finding only');
    expect(within(inspector).getByRole('region', { name: 'Sources' })).toHaveTextContent('Entity-only evidence');
    open(/FacetPublished details/);
    expect(inspector).not.toHaveTextContent('Entity-wide finding only');
    expect(within(inspector).getByRole('region', { name: 'Sources' })).not.toHaveTextContent('Entity-only evidence');
    expect(inspector).toHaveTextContent('Reported research finding.');
    expect(within(inspector).getByRole('region', { name: 'Sources' })).toHaveTextContent('Research report');
  });

  it('starts real adapter research for arbitrary input and reports loading without sample substitution', async () => {
    const adapter = testAdapter();
    const pending = deferred<AtlasWorkspaceData>();
    vi.mocked(adapter.research).mockReturnValue({ result: pending.promise, cancel: vi.fn(async () => undefined) });
    render(<AtlasWorkspace adapter={adapter} />);
    submit();
    await waitFor(() => expect(adapter.research).toHaveBeenCalledWith(expect.objectContaining({ query: 'an arbitrary unknown subject' })));
    expect(screen.getByRole('status')).toHaveTextContent('Starting live research');
    expect(screen.queryByText('Personal computers')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Starting points column' })).not.toBeInTheDocument();
    await act(async () => pending.resolve(testData()));
    await loaded();
    expect(screen.getByRole('textbox', { name: 'Current run id' })).toHaveValue('run-1');
  });

  it('shows empty results honestly and surfaces a failed research/auth request', async () => {
    const adapter = testAdapter();
    const empty = testData();
    empty.snapshot.claims = [];
    empty.snapshot.occurrences = [];
    vi.mocked(adapter.research).mockReturnValueOnce({ result: Promise.resolve(empty), cancel: vi.fn(async () => undefined) });
    render(<AtlasWorkspace adapter={adapter} />);
    submit();
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('no result rows'));
    vi.mocked(adapter.research).mockImplementationOnce(() => ({
      result: Promise.reject(Object.assign(new Error('Unauthorized'), { status: 401 })), cancel: vi.fn(async () => undefined),
    }));
    submit('retry query');
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Authentication required'));
    expect(screen.getByRole('status')).toHaveTextContent('Research failed');
  });

  it('cancels remotely and ignores results that resolve after acknowledgement', async () => {
    const adapter = testAdapter();
    const pending = deferred<AtlasWorkspaceData>(), ack = deferred<void>();
    const cancel = vi.fn(() => ack.promise);
    vi.mocked(adapter.research).mockReturnValue({ result: pending.promise, cancel });
    render(<AtlasWorkspace adapter={adapter} />);
    submit();
    await waitFor(() => expect(adapter.research).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'Cancel research' }));
    expect(cancel).toHaveBeenCalledOnce();
    expect(screen.getByRole('status')).toHaveTextContent('Requesting remote cancellation');
    await act(async () => ack.resolve());
    expect(screen.getByRole('status')).toHaveTextContent('Remote cancellation acknowledged');
    await act(async () => pending.resolve(testData('Late answer')));
    expect(screen.queryByRole('button', { name: /EntityLate answer/ })).not.toBeInTheDocument();
  });

  it('reports remote cancellation failure without claiming it stopped', async () => {
    const adapter = testAdapter();
    const pending = deferred<AtlasWorkspaceData>();
    vi.mocked(adapter.research).mockReturnValue({ result: pending.promise, cancel: vi.fn(async () => { throw new Error('Cancel unavailable'); }) });
    render(<AtlasWorkspace adapter={adapter} />);
    submit();
    await waitFor(() => expect(adapter.research).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'Cancel research' }));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('run may still be active'));
    expect(screen.getByRole('alert')).toHaveTextContent('Cancel unavailable');
    expect(screen.getByRole('button', { name: 'Cancel research' })).toBeEnabled();
  });

  it('prevents a stale search from overwriting a newer one', async () => {
    const adapter = testAdapter();
    const first = deferred<AtlasWorkspaceData>(), second = deferred<AtlasWorkspaceData>();
    const cancel = vi.fn(async () => undefined);
    vi.mocked(adapter.research).mockReturnValueOnce({ result: first.promise, cancel })
      .mockReturnValueOnce({ result: second.promise, cancel: vi.fn(async () => undefined) });
    render(<AtlasWorkspace adapter={adapter} />);
    submit('first');
    await waitFor(() => expect(adapter.research).toHaveBeenCalledTimes(1));
    submit('second');
    await waitFor(() => expect(adapter.research).toHaveBeenCalledTimes(2));
    expect(cancel).toHaveBeenCalledOnce();
    await act(async () => second.resolve(testData('New answer', 'run-new')));
    await act(async () => first.resolve(testData('Old answer', 'run-old')));
    expect(screen.getByRole('button', { name: /EntityNew answer/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /EntityOld answer/ })).not.toBeInTheDocument();
  });

  it('reuses known facets and researches unknown facets with parent context retained', async () => {
    const adapter = testAdapter();
    const pending = deferred<AtlasWorkspaceData>();
    vi.mocked(adapter.research).mockReturnValueOnce({ result: Promise.resolve(testData()), cancel: vi.fn(async () => undefined) })
      .mockReturnValueOnce({ result: pending.promise, cancel: vi.fn(async () => undefined) });
    render(<AtlasWorkspace adapter={adapter} />);
    submit(); await loaded(); open();
    open(/FacetPublished details/);
    expect(adapter.research).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Reported research finding.')).toBeInTheDocument();
    open(/FacetOpen question/);
    await waitFor(() => expect(adapter.research).toHaveBeenCalledTimes(2));
    expect(adapter.research).toHaveBeenLastCalledWith(expect.objectContaining({
      query: 'Research the open question for this subject',
      parent: expect.objectContaining({ target: { kind: 'facet', entity_id: 'subject', facet_key: 'unknown' } }),
    }));
    expect(screen.getByRole('region', { name: 'Research subject column' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Starting points column' })).toBeInTheDocument();
  });

  it('keeps canonical parents after scoped results arrive and reuses a completed facet without re-research', async () => {
    const adapter = testAdapter();
    vi.mocked(adapter.research).mockImplementation(input => {
      if (!input.parent) return { result: Promise.resolve(testData()), cancel: vi.fn(async () => undefined) };
      const data = structuredClone(input.parent.data);
      const path = data.snapshot.paths.find(p => p.path_id === data.snapshot.active_path_id)!;
      const occurrenceId = path.selected_occurrence_ids[path.selected_occurrence_ids.length - 1];
      data.snapshot.requests.push({
        ...data.snapshot.requests[0], request_id: 'facet-request', run_id: 'facet-run', path_id: path.path_id,
        parent_occurrence_id: occurrenceId,
      });
      data.runId = 'facet-run';
      data.snapshot.claims.push({ claim_id: 'facet-claim', subject_entity_id: 'subject', facet_key: 'unknown',
        text: 'New scoped finding', observed_at: '2026-11-08T10:00:00Z', evidence: { status: 'unsupported', reason: 'Requires confirmation' } });
      return { result: Promise.resolve(data), cancel: vi.fn(async () => undefined) };
    });
    render(<AtlasWorkspace adapter={adapter} />);
    submit(); await loaded(); open(); open(/FacetOpen question/);
    await screen.findByText('New scoped finding');
    expect(screen.getByRole('region', { name: 'Research subject column' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Starting points column' })).toBeInTheDocument();
    open(/FacetPublished details/);
    open(/FacetOpen question/);
    expect(adapter.research).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: /FacetOpen question/ })).toHaveTextContent('Known results');
  });

  it('does not let late facet results displace a newly selected known sibling', async () => {
    const adapter = testAdapter(), pending = deferred<AtlasWorkspaceData>(), cancel = vi.fn(async () => undefined);
    vi.mocked(adapter.research).mockReturnValueOnce({ result: Promise.resolve(testData()), cancel: vi.fn(async () => undefined) })
      .mockReturnValueOnce({ result: pending.promise, cancel });
    render(<AtlasWorkspace adapter={adapter} />);
    submit(); await loaded(); open(); open(/FacetOpen question/);
    await waitFor(() => expect(adapter.research).toHaveBeenCalledTimes(2));
    open(/FacetPublished details/);
    await waitFor(() => expect(cancel).toHaveBeenCalledOnce());
    await act(async () => pending.resolve(testData('Late facet replacement')));
    expect(screen.getByRole('region', { name: 'Published details column' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /EntityLate facet replacement/ })).not.toBeInTheDocument();
  });

  it('shows explicit budget status and does not fabricate partial results', async () => {
    const adapter = testAdapter(), pending = deferred<AtlasWorkspaceData>();
    vi.mocked(adapter.research).mockImplementation(input => {
      input.onStatus({ state: 'budget_exceeded', message: 'Research budget exhausted. No completed result.', runId: 'budget-run' });
      return { result: pending.promise, cancel: vi.fn(async () => undefined) };
    });
    render(<AtlasWorkspace adapter={adapter} />);
    submit();
    await waitFor(() => expect(screen.getByRole('status')).toHaveAttribute('data-state', 'budget_exceeded'));
    expect(screen.getByRole('status')).toHaveTextContent('Research budget exhausted');
    expect(screen.queryByRole('region', { name: 'Starting points column' })).not.toBeInTheDocument();
  });

  it('saves explicitly and exposes copyable server artifact/run identifiers then loads by id', async () => {
    const adapter = testAdapter();
    render(<AtlasWorkspace adapter={adapter} />);
    submit(); await loaded();
    expect(adapter.save).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save new artifact snapshot' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Saved artifact id' })).toHaveValue('artifact-1'));
    expect(screen.getByRole('textbox', { name: 'Saved run id' })).toHaveValue('run-1');
    expect(adapter.save).toHaveBeenCalledWith(expect.objectContaining({ runId: 'run-1' }));
    expect(adapter.recent).toHaveBeenCalledWith('run-1');
    fireEvent.change(screen.getByRole('textbox', { name: 'Artifact id' }), { target: { value: 'artifact-other' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load artifact' }));
    await waitFor(() => expect(adapter.load).toHaveBeenCalledWith('artifact-other'));
    expect(await screen.findByRole('button', { name: /EntityLoaded subject/ })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Saved artifact id' })).toHaveValue('artifact-other');
  });

  it('does not claim save/load durability after a failed server request', async () => {
    const adapter = testAdapter();
    vi.mocked(adapter.save).mockRejectedValue(new Error('Storage unavailable'));
    vi.mocked(adapter.load).mockRejectedValue(new Error('Artifact not found'));
    render(<AtlasWorkspace adapter={adapter} />);
    submit(); await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'Save new artifact snapshot' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Storage unavailable'));
    expect(screen.queryByRole('textbox', { name: 'Saved artifact id' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole('textbox', { name: 'Artifact id' }), { target: { value: 'missing' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load artifact' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Artifact not found'));
  });

  it('loads recent saves only from the current run and restores query refs without automatically spending', async () => {
    const adapter = testAdapter();
    vi.mocked(adapter.recent).mockResolvedValue([{ artifactId: 'recent-artifact', runId: 'run-1' }]);
    window.history.replaceState(null, '', '/?q=restored+question');
    render(<AtlasWorkspace adapter={adapter} />);
    expect(screen.getByRole('textbox', { name: /what would you like/i })).toHaveValue('restored question');
    expect(adapter.research).not.toHaveBeenCalled();
    submit(); await loaded();
    const recent = await screen.findByRole('navigation', { name: 'Recent saves for this run' });
    expect(adapter.recent).toHaveBeenCalledWith('run-1');
    fireEvent.click(within(recent).getByRole('button', { name: 'recent-artifact' }));
    await waitFor(() => expect(adapter.load).toHaveBeenCalledWith('recent-artifact'));
  });

  it('supports price/unit/uncertainty, checklist and unsent action drafts alongside comparisons and credentials', async () => {
    render(<AtlasWorkspace adapter={testAdapter()} />);
    submit(); await loaded(); open();
    const inspector = screen.getByRole('complementary', { name: 'Evidence and details' });
    expect(inspector).toHaveTextContent('100–200 USD / session');
    expect(inspector).toHaveTextContent('Uncertainty: Estimated range');
    expect(within(inspector).getByRole('checkbox', { name: /Confirm eligibility/ })).not.toBeChecked();
    expect(within(inspector).getByRole('region', { name: 'Action draft' })).toHaveTextContent('nothing sent');
    fireEvent.click(screen.getByText('Compare entities · 0 selected'));
    const choices = screen.getByRole('group', { name: 'Select entities to compare' });
    fireEvent.click(within(choices).getByRole('checkbox', { name: 'Research subject' }));
    fireEvent.click(within(choices).getByRole('checkbox', { name: 'Specialist' }));
    const table = screen.getByRole('table');
    expect(table).toHaveTextContent('Published price');
    expect(table).toHaveTextContent('USD / session');
    expect(table).toHaveTextContent('Specialties');
    expect(table).toHaveTextContent('Public qualification');
    expect(table).toHaveTextContent('Not reported');
  });

  it('navigates actual connections by canonical identity and discloses facets independent of occurrence templates', async () => {
    render(<AtlasWorkspace adapter={testAdapter()} />);
    submit(); await loaded(); open();
    fireEvent.click(screen.getByText('Connection view · 1 direct connections'));
    expect(screen.getByRole('list', { name: 'Direct connections' })).toHaveTextContent('Suggested relationship');
    fireEvent.click(screen.getByRole('button', { name: 'Explore Specialist' }));
    expect(screen.getByRole('region', { name: 'Specialist column' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /FacetCredentials/ })).toBeInTheDocument();
    expect(screen.getByRole('complementary')).toHaveTextContent('Identity: resolved · person');
    expect(screen.getByRole('complementary')).toHaveTextContent('Public qualification');
  });

  it('preserves active row focus and allows keyboard parent navigation with mobile ancestry', async () => {
    render(<AtlasWorkspace adapter={testAdapter()} />);
    submit(); await loaded();
    const entity = screen.getByRole('button', { name: /EntityResearch subject/ });
    entity.focus(); fireEvent.keyDown(entity, { key: 'ArrowRight' });
    expect(entity).toHaveFocus();
    const facet = screen.getByRole('button', { name: /FacetPublished details/ });
    facet.focus(); fireEvent.keyDown(facet, { key: 'ArrowRight' });
    expect(facet).toHaveFocus();
    fireEvent.keyDown(facet, { key: 'ArrowLeft' });
    expect(entity).toHaveFocus();
    expect(screen.getByRole('region', { name: 'Starting points column' })).toHaveClass('is-mobile-active');
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Visited paths' })).getByRole('button', { name: 'Research subject' }));
    expect(screen.getByRole('region', { name: 'Research subject column' })).toHaveClass('is-mobile-active');
    fireEvent.click(screen.getByRole('button', { name: '← Back' }));
    expect(screen.getByRole('region', { name: 'Starting points column' })).toHaveClass('is-mobile-active');
  });

  it('restores browser path references without serializing payloads or restarting research', async () => {
    const adapter = testAdapter();
    render(<AtlasWorkspace adapter={adapter} />);
    submit(); await loaded();
    const rootState = window.history.state;
    open();
    expect(window.location.search).toContain('path=');
    expect(window.location.search).not.toContain('Reported');
    expect(JSON.stringify(window.history.state)).not.toContain('snapshot');
    act(() => window.dispatchEvent(new PopStateEvent('popstate', { state: rootState })));
    expect(screen.queryByRole('region', { name: 'Research subject column' })).not.toBeInTheDocument();
    expect(adapter.research).toHaveBeenCalledTimes(1);
  });

  it('shows actual pending approvals during research; dismiss is not reject and authorization errors never fabricate approval', async () => {
    const adapter = testAdapter(), pending = deferred<AtlasWorkspaceData>();
    const approval: AtlasApproval = { id: 'batch-1', runId: 'run-approval', destination: 'https://destination.test',
      digest: 'real-server-digest', args: { recipient: 'actual destination', body: 'actual payload' }, status: 'pending' };
    vi.mocked(adapter.research).mockImplementation(input => {
      input.onStatus({ state: 'approval_required', message: 'Approval required by server.', runId: 'run-approval', approvals: [approval] });
      return { result: pending.promise, cancel: vi.fn(async () => undefined) };
    });
    vi.mocked(adapter.decide).mockRejectedValue(Object.assign(new Error('Forbidden'), { status: 403 }));
    render(<AtlasWorkspace adapter={adapter} />);
    submit();
    const button = await screen.findByRole('button', { name: 'Review request batch-1' });
    fireEvent.click(button);
    let dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('real-server-digest');
    expect(dialog).toHaveTextContent('actual payload');
    expect(within(dialog).getByRole('button', { name: 'Dismiss review' })).toHaveFocus();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Dismiss review' }));
    expect(adapter.decide).not.toHaveBeenCalled();
    fireEvent.click(button);
    dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve request' }));
    await waitFor(() => expect(within(dialog).getByRole('alert')).toHaveTextContent('approver role and a separate authorizer'));
    expect(dialog).toHaveTextContent('pending');
    expect(adapter.decide).toHaveBeenCalledWith(approval, 'approve', undefined);
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Rejection reason' }), { target: { value: 'Not authorized' } });
    vi.mocked(adapter.decide).mockResolvedValue({ ...approval, status: 'rejected' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Reject request' }));
    await waitFor(() => expect(dialog).toHaveTextContent('rejected'));
    expect(adapter.decide).toHaveBeenLastCalledWith(approval, 'reject', 'Not authorized');
  });
});
