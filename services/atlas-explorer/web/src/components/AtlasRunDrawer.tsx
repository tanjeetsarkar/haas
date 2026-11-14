import { useEffect, useMemo, useRef, useState } from 'react';
import type { AtlasExecutionEvent } from '@haas/atlas-explorer-client';
import { createBrowserRunDetailsApi, safePublicText, type RunDetailsApi,
  type RunDetailsOverview, type ThreadHistoryCursor, type PublicToolHistoryEntry } from '../runDetailsApi';
import './AtlasRunDrawer.css';

const MAX_VISIBLE = 200;
const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

export function drawerText(value: unknown): string {
  if (value === undefined || value === null) return 'Unavailable';
  return safePublicText(value);
}

export function executionRunId(event: AtlasExecutionEvent): string {
  return event.type === 'submitted' ? event.submission.context.runId : event.type === 'progress' ? event.progress.context.runId
    : event.type === 'telemetry' ? event.telemetry.context.runId : event.context.runId;
}

export function retainDrawerEvents(events: AtlasExecutionEvent[], event: AtlasExecutionEvent): AtlasExecutionEvent[] {
  const selected = [...events, event].filter(item => executionRunId(item) === executionRunId(event));
  const metadata = (['submitted', 'telemetry', 'disconnected'] as const)
    .flatMap(type => { const last = [...selected].reverse().find(item => item.type === type); return last ? [last] : []; });
  return [...metadata, ...selected.filter(item => item.type === 'progress').slice(-MAX_VISIBLE)];
}

export interface AtlasRunDrawerProps { events: AtlasExecutionEvent[]; runId: string; threadId?: string; savedSubmission?: { question?: string; prompt?: string; selectedContext?: unknown }; detailsApi?: RunDetailsApi }

export function AtlasRunDrawer({ events, runId, threadId: suppliedThreadId, savedSubmission, detailsApi }: AtlasRunDrawerProps) {
  const current = events.filter(event => executionRunId(event) === runId);
  const submitted = current.find(event => event.type === 'submitted');
  const telemetry = [...current].reverse().find(event => event.type === 'telemetry');
  const progress = current.filter(event => event.type === 'progress').map(event => event.progress), last = progress[progress.length - 1];
  const context = submitted?.submission.context ?? telemetry?.telemetry.context ?? last?.context;
  const eventThreadIds = new Set(current.map(event => event.type === 'submitted' ? event.submission.context.threadId : event.type === 'progress' ? event.progress.context.threadId : event.type === 'telemetry' ? event.telemetry.context.threadId : event.context.threadId).filter(Boolean));
  const threadConflict = eventThreadIds.size > 1 || (!!suppliedThreadId && [...eventThreadIds].some(id => id !== suppliedThreadId));
  const threadId = threadConflict ? undefined : suppliedThreadId ?? context?.threadId;
  const metrics = telemetry?.telemetry;
  const model = [...progress].reverse().find(item => item.model)?.model;
  const skills = [...progress].reverse().find(item => item.activeSkills)?.activeSkills;
  const api = useMemo(() => detailsApi ?? createBrowserRunDetailsApi(), [detailsApi]);
  const [open, setOpen] = useState(false), [retry, setRetry] = useState(0);
  const [overview, setOverview] = useState<RunDetailsOverview>();
  const [history, setHistory] = useState<PublicToolHistoryEntry[]>([]);
  const [cursor, setCursor] = useState<ThreadHistoryCursor>();
  const [loading, setLoading] = useState(false), [historyLoading, setHistoryLoading] = useState(false);
  const [overviewError, setOverviewError] = useState(false), [historyError, setHistoryError] = useState(false);
  const loadedKey = useRef('');
  const key = `${runId}\u0000${threadId ?? ''}`;
  useEffect(() => {
    if (!open || loadedKey.current === `${key}:${retry}`) return;
    loadedKey.current = `${key}:${retry}`;
    let active = true;
    setLoading(true); setOverviewError(false); setHistoryError(false); setOverview(undefined); setHistory([]); setCursor(undefined);
    void api.overview(runId).then(value => {
      if (active) { setOverview(value); setOverviewError(!value.run && !value.steps && !value.usage); }
    }, () => { if (active) setOverviewError(true); }).finally(() => { if (active) setLoading(false); });
    if (threadId) {
      setHistoryLoading(true);
      void api.historyPage(threadId).then(page => {
        if (active) { setHistory(page.entries); setCursor(page.next); }
      }, () => { if (active) setHistoryError(true); }).finally(() => { if (active) setHistoryLoading(false); });
    } else setHistoryError(true);
    return () => { active = false; };
  }, [api, key, open, retry, runId, threadId]);
  const loadMore = async () => {
    if (!threadId || !cursor || historyLoading) return;
    setHistoryLoading(true); setHistoryError(false);
    try {
      const page = await api.historyPage(threadId, cursor);
      setHistory(previous => [...previous, ...page.entries].slice(-MAX_VISIBLE));
      setCursor(page.next);
    } catch { setHistoryError(true); }
    finally { setHistoryLoading(false); }
  };
  const steps = overview?.steps?.items ?? [];
  const visibleSteps = steps.slice(0, MAX_VISIBLE);
  const liveSteps = metrics?.steps ?? [];
  const visibleLiveSteps = liveSteps.slice(0, Math.max(0, MAX_VISIBLE - visibleSteps.length));
  const visibleHistory = history.slice(-Math.max(0, MAX_VISIBLE - visibleSteps.length - visibleLiveSteps.length));
  const visibleProgress = progress.slice(-Math.max(0, MAX_VISIBLE - visibleSteps.length - visibleLiveSteps.length - visibleHistory.length));
  const disconnected = current.some(event => event.type === 'disconnected');
  const status = overview?.run?.status ?? metrics?.status ?? last?.status;
  const createdAt = overview?.run?.created_at;
  return <details className="atlas-run-drawer" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Run execution · {runId || 'No run selected'}</summary>
    <p>Public execution data only. Previews are provisional, not validated results.
      Details load only when opened; this view does not poll or submit a run.</p>
    <dl>
      <dt>Run</dt><dd>{runId || 'Unavailable'}</dd>
      <dt>Thread</dt><dd>{threadId ?? 'Unavailable'}</dd>
      <dt>Operation</dt><dd>{context?.operationId ?? 'Unavailable'}</dd>
      <dt>Status</dt><dd>{status ?? 'Unavailable'}</dd>
      <dt>Model</dt><dd>{drawerText(model)}</dd>
      <dt>Active skills</dt><dd>{skills ? drawerText(skills.join(', ')) : 'Unavailable'}</dd>
      <dt>Cost USD</dt><dd>{overview?.usage?.cost_usd ?? metrics?.usage?.cost_usd ?? metrics?.costUsd ?? 'Unavailable'}</dd>
      <dt>Tokens</dt><dd>{drawerText(overview?.usage?.model_tokens ?? metrics?.usage?.model_tokens)}</dd>
      <dt>Created</dt><dd>{typeof createdAt !== 'number' || !Number.isFinite(createdAt) ? 'Unavailable' : drawerText(new Date(createdAt * 1000).toISOString())}</dd>
      <dt>Elapsed time</dt><dd>{typeof createdAt !== 'number' || !Number.isFinite(createdAt) ? 'Unavailable' :
        status && TERMINAL.has(status) ? 'Unavailable — public terminal timestamp not provided' :
          `Live estimate since ${new Date(createdAt * 1000).toISOString()} (no background refresh)`}</dd>
    </dl>
    {threadConflict && <p role="alert">Saved thread binding conflicts with this run's public event. History was not requested.</p>}
    {disconnected && <p role="note">Observation disconnected. No new run has been submitted.
      Reconnect is unavailable until a recorded run/cursor is restored.</p>}
    <h2>Submitted question</h2><pre>{drawerText(submitted?.submission.question ?? savedSubmission?.question)}</pre>
    <h2>Application prompt</h2><pre>{drawerText(submitted?.submission.prompt ?? savedSubmission?.prompt)}</pre>
    <h2>Selected public context</h2><pre>{drawerText(submitted?.submission.selectedContext ?? savedSubmission?.selectedContext)}</pre>
    {loading && <p role="status">Loading public run details…</p>}
    {overviewError && <p role="status">Public run details unavailable.
      <button type="button" onClick={() => setRetry(value => value + 1)}>Retry details</button></p>}
    <h2>Public steps and tool arguments</h2>
    {steps.length ? <ol>{visibleSteps.map((step, index) =>
      <li key={`${step.step_id}:${step.attempt}:${index}`}>
        {drawerText(step.step_id)} · {drawerText(step.status)} · attempt {drawerText(step.attempt)}
        {(step.tool_calls ?? []).map((call, callIndex) => <section key={`${call.invocation_id}:${callIndex}`}>
          <strong>{drawerText(call.tool_name)} · {drawerText(call.invocation_id)}</strong>
          <pre>{drawerText(call.arguments)}</pre>
        </section>)}
      </li>)}</ol> : visibleLiveSteps.length ? <ol>{visibleLiveSteps.map((step, index) =>
      <li key={`${step.stepId}:${step.attempt}:${index}`}>{drawerText(step.stepId)} · {drawerText(step.status)}
        {step.tools.map((tool, toolIndex) => <span key={`${tool.toolCallId}:${toolIndex}`}>
          {drawerText(tool.toolName)} · {drawerText(tool.toolCallId)}
        </span>)}
      </li>)}</ol> : <p>{overview ? 'Unavailable' : 'Not loaded'}</p>}
    <h2>Authorized public tool history</h2>
    {historyError && <p role="status">Public thread history unavailable.
      {threadId && <button type="button" onClick={() => {
        if (cursor) void loadMore();
        else { loadedKey.current = ''; setRetry(value => value + 1); }
      }}>Retry history</button>}</p>}
    {historyLoading && <p role="status">Loading public history…</p>}
    {visibleHistory.length > 0 && <ol>{visibleHistory.map((entry, index) => <li key={`${entry.toolCallId ?? entry.toolName}:${index}`}>
      <strong>{drawerText(entry.toolName)}{entry.toolCallId ? ` · ${drawerText(entry.toolCallId)}` : ''}</strong>
      <pre>{drawerText(entry.content)}</pre>
    </li>)}</ol>}
    {cursor && <button type="button" disabled={historyLoading} onClick={() => void loadMore()}>
      {historyLoading ? 'Loading history…' : 'Load more public history'}
    </button>}
    {!historyLoading && !historyError && !visibleHistory.length && <p>{threadId ? 'No public tool history entries' : 'Unavailable — no recorded thread binding'}</p>}
    <h2>Public stream timeline</h2>
    <p>At most 200 step, history and stream entries are visible together.</p>
    {visibleProgress.length > 0 ? <ol>{visibleProgress.map((item, index) =>
      <li key={`${item.eventId ?? 'preview'}:${index}`}>
        <strong>{drawerText(item.event)}</strong> · {drawerText(item.status)} · {drawerText(item.occurredAt)}
        {item.phase && <span> · {drawerText(item.phase)}</span>}
        {item.toolName && <span> · Tool: {drawerText(item.toolName)} ({drawerText(item.toolCallId)})</span>}
        {item.provisional && <span> · Provisional</span>}
        {item.preview && <pre>{drawerText(item.preview)}{item.previewTruncated ? '\n[truncated event preview]' : ''}</pre>}
      </li>)}</ol> : <p>Unavailable</p>}
  </details>;
}
