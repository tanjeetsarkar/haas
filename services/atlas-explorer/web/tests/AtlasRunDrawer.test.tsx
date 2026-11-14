import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AtlasExecutionEvent } from '@haas/atlas-explorer-client';
import { AtlasRunDrawer, drawerText, retainDrawerEvents } from '../src/components/AtlasRunDrawer';
import type { RunDetailsApi } from '../src/runDetailsApi';

afterEach(cleanup);
const context = { runId: 'run', threadId: 'thread', operationId: 'op', requestId: 'req', explorationId: 'explore', pathId: 'path', parentOccurrenceId: null, baseRevision: 1, historyVersion: 1 };
describe('public execution drawer', () => {
  it('renders exact application prompt/context and public usage, never fills unknowns with zero', () => {
    const events: AtlasExecutionEvent[] = [
      { type: 'submitted', submission: { context, question: 'my question',
        prompt: 'exact application prompt', selectedContext: { subject: 'selected subject' } } },
      { type: 'telemetry', telemetry: { context, status: 'completed', costUsd: 0.02,
        steps: [{ stepId: 'step1', attempt: 1, status: 'completed', startedAt: 1,
          tools: [{ toolName: 'public-tool', toolCallId: 'call1' }] }] } },
    ];
    render(<AtlasRunDrawer events={events} runId="run" />);
    expect(screen.getByText('exact application prompt')).toBeInTheDocument();
    expect(screen.getByText(/"subject":"selected subject"/)).toBeInTheDocument();
    expect(screen.getByText('0.02')).toBeInTheDocument();
    expect(screen.getByText(/public-tool · call1/)).toBeInTheDocument();
    expect(screen.getAllByText('Unavailable').length).toBeGreaterThan(1);
  });

  it('escapes HTML, redacts credentials, bounds text and labels provisional/truncated previews', () => {
    const events: AtlasExecutionEvent[] = [{ type: 'progress', progress: {
      context, event: 'output_preview', provisional: true, previewTruncated: true,
      preview: '<img src=x onerror=alert(1)> Bearer abc123 password=hidden',
    } }];
    const { container } = render(<AtlasRunDrawer events={events} runId="run" />);
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByText(/<img/)).toHaveTextContent('[redacted]');
    expect(screen.getByText(/Provisional/)).toBeInTheDocument();
    expect(screen.getByText(/truncated event preview/)).toBeInTheDocument();
    expect(drawerText('x'.repeat(20000))).toContain('[truncated');
    expect(drawerText('x'.repeat(20000)).length).toBeLessThan(16500);
    expect(new TextEncoder().encode(drawerText('😀'.repeat(10000))).length).toBeLessThan(16500);
  });

  it('isolates selected run and caps the timeline at 200 with explicit unavailable history', () => {
    const events: AtlasExecutionEvent[] = Array.from({ length: 205 }, (_, i) =>
      ({ type: 'progress', progress: { context, event: `event${i}`, eventId: i } }));
    events.push({ type: 'submitted', submission: { context: { ...context, runId: 'other' },
      question: 'other question', prompt: 'other prompt', selectedContext: null } });
    const { container } = render(<AtlasRunDrawer events={events} runId="run" />);
    expect(container.querySelectorAll('li')).toHaveLength(200);
    expect(screen.queryByText('event0')).toBeNull();
    expect(screen.queryByText('other prompt')).toBeNull();
    expect(screen.getByText(/At most 200 step, history and stream entries/)).toBeInTheDocument();
  });

  it('shows disconnect without a submit or retry button', () => {
    render(<AtlasRunDrawer events={[{ type: 'disconnected', context, lastEventId: '9', error: new Error('private response') }]} runId="run" />);
    expect(screen.getByRole('note')).toHaveTextContent('No new run has been submitted');
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.queryByText('private response')).toBeNull();
  });

  it('retains submitted metadata when a long stream evicts old previews', () => {
    let events: AtlasExecutionEvent[] = [{ type: 'submitted', submission: { context, question: 'saved question', prompt: 'saved prompt', selectedContext: null } }];
    for (let i = 0; i < 250; i++) events = retainDrawerEvents(events,
      { type: 'progress', progress: { context, event: 'output_preview', preview: `preview${i}` } });
    expect(events).toHaveLength(201);
    expect(events[0].type).toBe('submitted');
    expect(events.filter(e => e.type === 'progress')).toHaveLength(200);
  });

  it('fetches SDK details only on explicit open and incrementally pages escaped tool history', async () => {
    const api: RunDetailsApi = {
      overview: vi.fn(async () => ({ run: { run_id: 'run', status: 'completed', created_at: 4 },
        steps: { items: [] }, usage: { run_id: 'run', cost_usd: 0.1,
          model_tokens: { prompt: 1, completion: 2, total: 3 }, tool_invocations: 1,
          egress_bytes: 0, artifact_bytes: 0 } })),
      historyPage: vi.fn().mockResolvedValueOnce({ entries: [{
        toolName: 'public-search', toolCallId: 'call', content: '<svg onload=alert(1)> [redacted]',
      }], next: { offset: 50, historyVersion: 3, page: 1 } }).mockResolvedValueOnce({
        entries: [{ toolName: 'public-fetch', content: 'full authorized result' }],
      }),
    };
    const { container } = render(<AtlasRunDrawer events={[]} runId="run" threadId="thread" detailsApi={api} />);
    expect(api.overview).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Run execution · run'));
    await screen.findByText('0.1');
    expect(api.overview).toHaveBeenCalledWith('run');
    expect(api.historyPage).toHaveBeenCalledWith('thread');
    expect(container.querySelector('svg')).toBeNull();
    expect(screen.getByText(/<svg/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Load more public history' }));
    expect(await screen.findByText('full authorized result')).toBeInTheDocument();
    expect(api.historyPage).toHaveBeenLastCalledWith('thread', { offset: 50, historyVersion: 3, page: 1 });
    expect(container.querySelectorAll('li').length).toBeLessThanOrEqual(200);
    const conflicting: RunDetailsApi = { overview: vi.fn(async () => ({})), historyPage: vi.fn(async () => ({ entries: [] })) };
    render(<AtlasRunDrawer events={[{ type: 'submitted', submission: { context, question: '', prompt: '', selectedContext: null } }]}
      runId="run" threadId="other-thread" detailsApi={conflicting} />);
    fireEvent.click(screen.getAllByText('Run execution · run')[1]);
    expect(await screen.findByRole('alert')).toHaveTextContent('conflicts');
    expect(conflicting.historyPage).not.toHaveBeenCalled();
  });
});
