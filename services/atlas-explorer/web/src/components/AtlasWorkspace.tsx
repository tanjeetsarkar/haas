import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import {
  activateExplorationPath, getExplorationBreadcrumbs, previousExplorationPath, selectExplorationTarget,
  type AtlasActionDraftV1, type AtlasExecutionEvent, type AtlasOccurrence, type AtlasOperationPersistence, type AtlasTarget,
} from '@haas/atlas-explorer-client';
import {
  describeAtlasError, type AtlasApproval, type AtlasResearchOperation, type AtlasSavedSnapshot,
  type AtlasWorkspaceAdapter, type AtlasWorkspaceData,
} from '../explorationAdapter';
import { facetResearchComplete, projectGraphColumns, targetTitle } from '../graphProjection';
import { scopedDive, scopedDiveKey } from '../scopedDive';
import { AtlasGraphColumn } from './AtlasGraphColumn';
import { AtlasInspector } from './AtlasInspector';
import { AtlasComparison } from './AtlasComparison';
import { AtlasApprovalReview } from './AtlasApprovalReview';
import { AtlasOperatorLink } from './AtlasOperatorLink';
import { AtlasRunDrawer, retainDrawerEvents } from './AtlasRunDrawer';
import './AtlasPreview.css';
import './AtlasWorkspace.css';

export interface AtlasDurableLifecycle {
  workspaceId?: string;
  query: string;
  data: AtlasWorkspaceData | null;
  beforeResearch(query: string): Promise<AtlasOperationPersistence>;
  commit(data: AtlasWorkspaceData): Promise<void>;
  checkpoint(draft: string): Promise<void>;
  actionDrafts?: Record<string, unknown>;
  saveActionDrafts?(drafts: Record<string, AtlasActionDraftV1>): Promise<void>;
  select(data: AtlasWorkspaceData): Promise<void>;
  execution?(event: AtlasExecutionEvent): void;
}

export function AtlasWorkspace({ adapter, approverOrigin, durable }: {
  adapter: AtlasWorkspaceAdapter; approverOrigin?: string; durable?: AtlasDurableLifecycle;
}) {
  const [query, setQuery] = useState(durable?.query ?? '');
  const [data, setData] = useState<AtlasWorkspaceData | null>(durable?.data ?? null);
  const [status, setStatus] = useState('Ready. Enter any subject to start live research.');
  const [state, setState] = useState('idle');
  const [error, setError] = useState('');
  const [runId, setRunId] = useState(durable?.data?.runId ?? '');
  const [executionEvents, setExecutionEvents] = useState<AtlasExecutionEvent[]>([]);
  const [artifactInput, setArtifactInput] = useState('');
  const [saved, setSaved] = useState<AtlasSavedSnapshot | null>(null);
  const [recent, setRecent] = useState<AtlasSavedSnapshot[]>([]);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);
  const [mobileColumn, setMobileColumn] = useState(0);
  const [comparison, setComparison] = useState<string[]>([]);
  const [review, setReview] = useState<AtlasApproval | null>(null);
  const [approvals, setApprovals] = useState<AtlasApproval[]>([]);
  const [actionDrafts, setActionDrafts] = useState<Record<string, unknown>>(durable?.actionDrafts ?? {});
  const actionDraftsRef = useRef(actionDrafts);
  actionDraftsRef.current = actionDrafts;
  const [editingActionCard, setEditingActionCard] = useState<string | null>(null);
  const [actionDraftText, setActionDraftText] = useState('');
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const operation = useRef<AtlasResearchOperation | null>(null);
  const pendingSelections = useRef(new Set<string>());
  const dataRef = useRef(data);
  dataRef.current = data;
  const rail = useRef<HTMLDivElement>(null);
  const parentFocus = useRef<number | null>(null);
  const historyData = useRef(new Map<string, AtlasWorkspaceData>());
  const snapshot = data?.snapshot;
  const selected = snapshot ? getExplorationBreadcrumbs(snapshot) : [];
  const target = selected[selected.length - 1]?.target ?? null;
  const columns = snapshot ? projectGraphColumns(snapshot) : [];
  const activeColumn = Math.min(mobileColumn, Math.max(0, columns.length - 1));
  const activePath = snapshot?.paths.find(p => p.path_id === snapshot.active_path_id);
  const rootPath = snapshot?.paths.find(p => p.selected_occurrence_ids.length === 0);
  const explored = new Set(snapshot?.paths.flatMap(p => p.selected_occurrence_ids)
    .map(id => snapshot.occurrences.find(o => o.occurrence_id === id)?.target)
    .filter(t => t?.kind === 'entity').map(t => t.entity_id) ?? []);

  function remember(next: AtlasWorkspaceData, artifactId?: string) {
    // History contains references only. Snapshot data stays in memory or in an
    // explicitly saved artifact, never in URL/history/localStorage.
    const key = `${next.snapshot.exploration_id}:r${next.snapshot.revision}:${next.snapshot.active_path_id ?? 'root'}`;
    historyData.current.set(key, next);
    const url = new URL(window.location.href);
    url.search = '';
    if (durable?.workspaceId) {
      url.searchParams.set('workspace', durable.workspaceId);
      void durable.select(next).catch(e => setError(describeAtlasError(e)));
    }
    if (artifactId) url.searchParams.set('artifact', artifactId);
    else url.searchParams.set('q', next.snapshot.root_query);
    if (next.snapshot.active_path_id) url.searchParams.set('path', next.snapshot.active_path_id);
    window.history.pushState({ atlasRef: key, artifact: artifactId }, '', url);
  }

  async function stopPrevious() {
    const previous = operation.current;
    if (previous?.detach) previous.detach();
    else if (previous) await previous.cancel();
    if (operation.current === previous) operation.current = null;
  }

  async function saveActionDraft(cardId: string, draft: AtlasActionDraftV1) {
    const next = { ...actionDraftsRef.current, [cardId]: draft };
    if (!durable?.saveActionDrafts) return;
    await durable.saveActionDrafts(next as Record<string, AtlasActionDraftV1>);
    actionDraftsRef.current = next;
    setActionDrafts(next);
  }

  async function research(value: string, parent?: { data: AtlasWorkspaceData; target: AtlasTarget }) {
    const scopeKey = parent ? scopedDiveKey(parent.data.snapshot)
      : `root:${value}`;
    if (pendingSelections.current.has(scopeKey)) return;
    pendingSelections.current.add(scopeKey);
    const token = ++generation.current;
    let started: AtlasResearchOperation | null = null;
    setBusy(true);
    setError('');
    setState('starting');
    setStatus(parent ? 'Researching this facet in its parent context…' : 'Starting live research…');
    if (!parent) {
      setData(null);
      setSaved(null);
      setRecent([]);
      setRunId('');
      setExecutionEvents([]);
      setComparison([]);
      setReview(null);
      setApprovals([]);
      setMobileColumn(0);
    }
    try {
      const persistence = await durable?.beforeResearch(value);
      if (token !== generation.current) return;
      await stopPrevious();
      if (token !== generation.current) return;
      const current = adapter.research({
        query: value, parent, ...(persistence ? { operationPersistence: persistence } : {}),
        onExecution(event) {
          if (token !== generation.current) return;
          durable?.execution?.(event);
          setExecutionEvents(events => retainDrawerEvents(events, event));
        },
        onStatus(update) {
          if (token !== generation.current) return;
          setState(update.state);
          setStatus(update.message);
          if (update.runId) setRunId(update.runId);
          if (update.approvals) {
            setApprovals(update.approvals);
            setReview(current => current ? update.approvals?.find(a => a.id === current.id) ?? current : null);
          }
        },
      });
      operation.current = current;
      started = current;
      const result = await current.result;
      if (token !== generation.current) return;
      const next = result;
      setData(next);
      await durable?.commit(next);
      if (token !== generation.current) return;
      setApprovals(next.approvals ?? []);
      setRunId(next.runId);
      setSaved(null);
      setState('completed');
      setStatus(next.snapshot.occurrences.some(o => o.parent_occurrence_id === null)
        ? durable ? 'Research complete.'
          : 'Research complete. Results are not saved until you explicitly save a snapshot.'
        : 'Research completed with no result rows. Only the returned interpretation is shown; no sample content has been substituted.');
      remember(next);
    } catch (e) {
      if (token !== generation.current) return;
      setError(describeAtlasError(e));
      setState(!started && operation.current ? 'cancel_failed' : 'failed');
      setStatus(!started && operation.current ? 'Remote cancellation failed; the prior run may still be active. Retry cancellation.'
        : 'Research failed. No successful result is assumed.');
    } finally {
      pendingSelections.current.delete(scopeKey);
      if (token === generation.current) {
        if (operation.current === started) operation.current = null;
        setBusy(false);
      }
    }
  }

  function detachObservation() {
    ++generation.current;
    const previous = operation.current;
    operation.current = null;
    previous?.detach?.();
    setBusy(false);
  }

  async function cancel() {
    const token = ++generation.current; // Invalidate callbacks before waiting for remote ACK.
    setState('cancelling');
    setStatus('Requesting remote cancellation…');
    const current = operation.current;
    try {
      const cancellation = current ? await current.cancel() : undefined;
      if (token !== generation.current) return;
      operation.current = null;
      setState(cancellation?.acknowledged === false ? 'cancellation_requested' : 'cancelled');
      setStatus(cancellation?.message ?? 'Remote cancellation acknowledged. No late result will replace the current view.');
    } catch (e) {
      if (token !== generation.current) return;
      setError(describeAtlasError(e));
      setStatus('Remote cancellation failed; the run may still be active. Retry cancellation.');
      setState('cancel_failed');
    } finally { if (token === generation.current) setBusy(false); }
  }

  async function load(artifactId: string, recordHistory = true) {
    if (!artifactId.trim()) return;
    const token = ++generation.current;
    setLoading(true);
    setBusy(false);
    setError('');
    setStatus('Loading saved artifact…');
    try {
      await stopPrevious();
      if (token !== generation.current) return;
      const next = await adapter.load(artifactId.trim());
      if (token !== generation.current) return;
      await durable?.commit(next);
      if (token !== generation.current) return;
      setData(next);
      setApprovals(next.approvals ?? []);
      setRunId(next.runId);
      setQuery(next.snapshot.root_query);
      setSaved({ artifactId: artifactId.trim(), runId: next.runId });
      setMobileColumn(0);
      setComparison([]);
      setReview(null);
      setState('loaded');
      setStatus('Loaded a saved artifact snapshot. This does not start research or overwrite the artifact.');
      if (recordHistory) remember(next, artifactId.trim());
    } catch (e) {
      if (token === generation.current) { setError(describeAtlasError(e)); setStatus('Artifact could not be loaded.'); }
    } finally { if (token === generation.current) setLoading(false); }
  }

  async function save() {
    if (!data) return;
    const captured = data;
    const token = generation.current;
    setSaving(true);
    setError('');
    try {
      const result = await adapter.save(captured);
      if (token !== generation.current) return;
      setSaved(result);
      setStatus('New immutable artifact snapshot saved. Further exploration will not change this artifact.');
      if (dataRef.current === captured) remember(captured, result.artifactId);
      // Saving succeeded even if listing recent artifacts subsequently fails.
      try {
        const rows = await adapter.recent(result.runId);
        if (token === generation.current) setRecent(rows);
      }
      catch (e) { if (token === generation.current) setError(`Snapshot saved; recent list unavailable. ${describeAtlasError(e)}`); }
    } catch (e) { if (token === generation.current) setError(describeAtlasError(e)); }
    finally { setSaving(false); }
  }

  useEffect(() => {
    let active = true;
    if (data?.runId) {
      setRecent([]);
      void adapter.recent(data.runId).then(rows => { if (active) setRecent(rows); })
        .catch(e => { if (active) setError(`Recent saves unavailable. ${describeAtlasError(e)}`); });
    }
    return () => { active = false; };
  }, [adapter, data?.runId]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const artifact = params.get('artifact');
    const initialQuery = params.get('q');
    // Query references are restored to the form, never silently billed again.
    if (artifact) { setArtifactInput(artifact); void load(artifact, false); }
    else if (initialQuery) setQuery(initialQuery);
    function pop(event: PopStateEvent) {
      const cached = historyData.current.get(event.state?.atlasRef);
      if (cached) {
        ++generation.current;
        const previous = operation.current;
        operation.current = null;
        if (previous?.detach) previous.detach();
        else if (previous) void previous.cancel().catch(e => setError(describeAtlasError(e)));
        setBusy(false);
        setData(cached);
        setApprovals(cached.approvals ?? []);
        setReview(null);
        setQuery(cached.snapshot.root_query);
        setRunId(cached.runId);
        setMobileColumn(getExplorationBreadcrumbs(cached.snapshot).length);
        setSaved(event.state?.artifact ? { artifactId: event.state.artifact, runId: cached.runId } : null);
        setStatus('Restored a visited path from memory. No research or save was started.');
      } else {
        const params = new URLSearchParams(window.location.search);
        if (params.get('artifact')) void load(params.get('artifact')!, false);
        else {
          ++generation.current;
          void stopPrevious().catch(e => setError(describeAtlasError(e)));
          setBusy(false);
          setData(null);
          setApprovals([]);
          setReview(null);
          setSaved(null);
          setRunId('');
          setQuery(params.get('q') ?? '');
          setStatus('Query restored. Submit Research to start a new run.');
        }
      }
    }
    window.addEventListener('popstate', pop);
    return () => {
      window.removeEventListener('popstate', pop);
      ++generation.current;
      const current = operation.current;
      operation.current = null;
      // Remote cancellation on departure, not just stream disconnection.
      if (current?.detach) current.detach();
      else if (current) void current.cancel().catch(() => undefined);
    };
    // Adapter identity is stable for the lifetime of a workspace.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [adapter]);

  useEffect(() => {
    if (rail.current && window.innerWidth > 600) rail.current.scrollLeft = rail.current.scrollWidth;
  }, [columns.length, snapshot?.active_path_id]);
  useEffect(() => {
    if (parentFocus.current === null) return;
    const index = parentFocus.current;
    parentFocus.current = null;
    rail.current?.querySelectorAll<HTMLElement>('.atlas-preview__column')[index]
      ?.querySelector<HTMLButtonElement>('[aria-current="step"]')?.focus();
  });

  function activate(pathId: string, index?: number) {
    if (!data) return;
    if (busy) { if (durable) detachObservation(); else void cancel(); }
    const next = { ...data, snapshot: activateExplorationPath(data.snapshot, pathId) };
    setData(next);
    setMobileColumn(index ?? getExplorationBreadcrumbs(next.snapshot).length);
    remember(next);
  }
  function open(row: AtlasOccurrence, index: number) {
    if (!data) return;
    try {
      const next = { ...data, snapshot: selectExplorationTarget(data.snapshot, row.target, row.parent_occurrence_id) };
      if (durable && pendingSelections.current.has(scopedDiveKey(next.snapshot))) return;
      setData(next);
      setMobileColumn(index + 1);
      remember(next);
      const t = row.target;
      if (durable) {
        const dive = scopedDive(next.snapshot, t);
        if (dive.kind === 'research') void research(dive.question, { data: next, target: t });
        else {
          if (busy) detachObservation();
          setStatus(dive.kind === 'unavailable' ? dive.reason
            : dive.kind === 'cached' ? 'Restored cached branch. No new research run.'
              : 'Source evidence opened. No research run.');
        }
        return;
      }
      const facet = t.kind === 'facet' ? data.snapshot.entities.find(e => e.entity_id === t.entity_id)
        ?.facets.find(f => f.key === t.facet_key) : undefined;
      if (facet?.status === 'research_needed' && !facetResearchComplete(data.snapshot, t))
        void research(facet.scope, { data: next, target: t });
      else if (busy) void cancel();
    } catch (e) { setError(describeAtlasError(e)); }
  }
  function keys(event: KeyboardEvent<HTMLElement>, rows: AtlasOccurrence[], index: number) {
    const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[data-atlas-row]'));
    const focused = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      buttons[(focused + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length]?.focus();
    } else if (event.key === 'ArrowRight' && rows[focused]) {
      event.preventDefault(); open(rows[focused], index);
    } else if (event.key === 'ArrowLeft' && selected[index - 1]) {
      event.preventDefault(); parentFocus.current = index - 1; activate(selected[index - 1].path_id, index - 1);
    }
  }

  const reviewGeneration = generation.current;
  return <main className="atlas-preview atlas-live" aria-label="Atlas research workspace">
    <header className="atlas-preview__header"><div className="atlas-preview__brand">
      <div className="atlas-preview__eyebrow">ATLAS · LIVE RESEARCH</div>
      <h1>Explore a subject, one useful step at a time</h1>
      <p>Research real sources. Follow facets. {durable ? 'Server receipts confirm workspace saves.' : 'Save only what you choose.'}</p>
    </div><a className="atlas-preview__exit" href="?atlasPreview=1">Open sample playground</a></header>
    <div className="atlas-preview__notice" role="note">Live research uses your authenticated HaaS connection. Results may be incomplete or conflicting.
      {durable ? 'Workspace saves automatically; check the server save status for confirmation. Existing artifact snapshots can still be loaded.'
        : 'Nothing here is a saved snapshot until the server confirms an explicit save.'}</div>
    <form className="atlas-preview__search" aria-label="Live research" onSubmit={e => {
      e.preventDefault(); if (query.trim()) { setLoading(false); void research(query.trim()); }
    }}>
      <label htmlFor="atlas-live-query">What would you like to explore?</label>
      <div className="atlas-preview__search-controls">
        <input id="atlas-live-query" value={query} onChange={e => setQuery(e.target.value)}
          onBlur={() => { if (durable) void durable.checkpoint(query).catch(e => setError(describeAtlasError(e))); }}
          placeholder="Any subject, person, place or comparison" required />
        <button className="atlas-preview__search-button" type="submit" disabled={!query.trim()}>Research</button>
        {(busy || state === 'cancel_failed') && <button type="button" onClick={() => void cancel()} disabled={state === 'cancelling'}>Cancel research</button>}
      </div>
    </form>
    <section className="atlas-live__persistence" aria-label="Artifact snapshots">
      <div className="atlas-live__toolbar">{!durable && <button type="button" onClick={() => void save()} disabled={!data || busy || saving || loading}>
        {saving ? 'Saving snapshot…' : 'Save new artifact snapshot'}</button>}
        <form onSubmit={e => { e.preventDefault(); void load(artifactInput); }}>
          <label htmlFor="atlas-artifact-id">Artifact id</label>
          <input id="atlas-artifact-id" value={artifactInput} onChange={e => setArtifactInput(e.target.value)} placeholder="Load a saved artifact" required />
          <button type="submit" disabled={loading}>{loading ? 'Loading artifact…' : 'Load artifact'}</button>
        </form></div>
      <div className="atlas-live__references">
        {runId && <label>Run id<input aria-label="Current run id" readOnly value={runId} /></label>}
        {saved && <><label>Saved artifact id<input aria-label="Saved artifact id" readOnly value={saved.artifactId} /></label>
          <label>Saved run id<input aria-label="Saved run id" readOnly value={saved.runId} /></label>
          <button type="button" onClick={() => {
            void navigator.clipboard?.writeText(saved.artifactId).then(() => setStatus('Artifact id copied.'))
              .catch(() => setStatus('Select and copy the artifact id from the field.'));
          }}>Copy artifact id</button></>}
      </div>
      {!!recent.length && <nav aria-label="Recent saves for this run">{recent.map(row =>
        <button type="button" key={row.artifactId} onClick={() => void load(row.artifactId)}>
          {row.artifactId}{row.createdAt ? ` · ${row.createdAt}` : ''}</button>)}</nav>}
    </section>
    <div role="status" className="atlas-preview__status" data-state={state}>{status}</div>
    {error && <div role="alert" className="atlas-preview__error">{error}</div>}
    <AtlasRunDrawer events={executionEvents} runId={runId} />
    {data && snapshot && <>
      <nav className="atlas-preview__breadcrumbs" aria-label="Selected ancestry"><span>Path</span>
        <button type="button" onClick={() => rootPath && activate(rootPath.path_id, 0)}>Start</button>
        {selected.map((o, i) => <span className="atlas-preview__crumb" key={o.occurrence_id}> /
          <button type="button" onClick={() => activate(o.path_id, i + 1)}>{targetTitle(snapshot, o.target)}</button></span>)}
      </nav>
      <div className="atlas-preview__workspace" aria-busy={busy}>
        <section className="atlas-preview__navigation" aria-label="Exploration columns">
          <div className="atlas-preview__rail" ref={rail}>{columns.map((column, i) =>
            <AtlasGraphColumn key={i} snapshot={snapshot} title={column.title} rows={column.rows}
              index={i} active={i === activeColumn} selected={selected[i]?.target} explored={explored}
              actionDrafts={actionDrafts} editingActionCard={editingActionCard} actionDraftText={actionDraftText}
              onEditAction={(cardId, initialText) => { setEditingActionCard(cardId); setActionDraftText(initialText); }}
              onActionDraftText={setActionDraftText} onSaveActionDraft={durable?.saveActionDrafts ? saveActionDraft : undefined}
              onSelect={open} onKeyDown={keys}
              emptyMessage={busy ? 'Researching this selection…' : 'No further results in this snapshot. Inspect details or research an unknown facet.'} />)}</div>
          <div className="atlas-preview__mobile-controls">
            <button type="button" disabled={!selected.length} onClick={() => {
              if (busy) { if (durable) detachObservation(); else void cancel(); }
              const next = { ...data, snapshot: previousExplorationPath(snapshot) };
              setData(next);
              setMobileColumn(getExplorationBreadcrumbs(next.snapshot).length);
              remember(next);
            }}>← Back</button><span>Column {activeColumn + 1} of {columns.length}</span>
          </div>
          <nav className="atlas-preview__visited" aria-label="Visited paths"><span>Visited paths</span>
            {snapshot.paths.filter(p => p.selected_occurrence_ids.length).map(p => {
              const label = p.selected_occurrence_ids.map(id => snapshot.occurrences.find(o => o.occurrence_id === id))
                .filter(o => o !== undefined).map(o => targetTitle(snapshot, o.target)).join(' › ');
              return <button type="button" key={p.path_id} aria-current={p.path_id === snapshot.active_path_id ? 'page' : undefined}
                onClick={() => activate(p.path_id)}>{label}</button>;
            })}
          </nav>
        </section>
        <AtlasInspector snapshot={snapshot} target={target} presentations={data.presentations}
          actionDrafts={actionDrafts} editingActionCard={editingActionCard} actionDraftText={actionDraftText}
          onEditAction={(cardId, initialText) => { setEditingActionCard(cardId); setActionDraftText(initialText); }}
          onActionDraftText={setActionDraftText}
          onSaveActionDraft={durable?.saveActionDrafts ? saveActionDraft : undefined}
          onResearchAgain={durable && target && scopedDive(snapshot, target).kind === 'cached'
            ? t => void research(snapshot.root_query, { data, target: t }) : undefined}
          onNavigate={entityId => open({ occurrence_id: '', path_id: activePath?.path_id ?? '',
            parent_occurrence_id: selected[selected.length - 1]?.occurrence_id ?? null, target: { kind: 'entity', entity_id: entityId } }, selected.length)}
          onResearch={t => void research(durable ? snapshot.root_query : targetTitle(snapshot, t), { data, target: t })} />
      </div>
      <AtlasComparison snapshot={snapshot} presentations={data.presentations} selectedIds={comparison}
        onToggle={id => setComparison(ids => ids.includes(id) ? ids.filter(i => i !== id) : [...ids, id])} />
    </>}
    {!!approvals.length && <section className="atlas-live__approvals" aria-label="Server approval requests">
      <h2>Server approval requests</h2>{approvals.map(a => <div key={a.id}><span>{a.destination} · {a.status}</span>
        <button type="button" onClick={() => setReview(a)}>Review request {a.id}</button>
        <AtlasOperatorLink batchId={a.id} configuredOrigin={approverOrigin} /></div>)}
    </section>}
    {!data && !busy && <section className="atlas-live__welcome"><h2>Start with a question, not a template</h2>
      <p>Explore taxonomy, compare options, inspect people’s specialties or build a checklist from actual research.
        Unknown queries never fall back to sample answers.</p></section>}
    {review && <AtlasApprovalReview key={review.id} approval={review} adapter={adapter} onDismiss={() => setReview(null)}
      onResult={result => {
        if (generation.current !== reviewGeneration) return;
        setApprovals(current => current.map(a => a.id === result.id ? result : a));
        setData(current => current ? { ...current, approvals: current.approvals?.map(a => a.id === result.id ? result : a) } : null);
        setReview(result);
        setStatus(`Server approval status: ${result.status}. Execution is managed by the server.`);
      }} />}
    <footer className="atlas-preview__footer"><span>HaaS public APIs · explicit immutable saves · no browser credentials</span></footer>
  </main>;
}
