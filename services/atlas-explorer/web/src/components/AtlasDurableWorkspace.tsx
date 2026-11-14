import { useEffect, useRef, useState } from 'react';
import type { AtlasActionDraftV1, AtlasExecutionEvent } from '@haas/atlas-explorer-client';
import type { AtlasWorkspaceAdapter } from '../explorationAdapter';
import { WorkspaceSession } from '../workspaceSession';
import { WorkspaceStorageApi, type WorkspaceDocument } from '../workspaceStorageApi';
import type { WorkspaceSaveState } from '../workspaceSaveQueue';
import { AtlasWorkspace } from './AtlasWorkspace';
import { AtlasWorkspaceHistory } from './AtlasWorkspaceHistory';
import { AtlasRunDrawer, retainDrawerEvents } from './AtlasRunDrawer';

/**
 * Explicit integration entry point; runtime activation remains a separate reviewed step.
 * The legacy workspace is not replaced until authenticated storage is verified.
 */
export function AtlasDurableWorkspace({ adapter, storage, approverOrigin }: {
  adapter: AtlasWorkspaceAdapter; storage: WorkspaceStorageApi; approverOrigin?: string;
}) {
  const session = useRef<WorkspaceSession | null>(null);
  const createAttempt = useRef<{ id: string; title: string; draft: string; pending?: Promise<WorkspaceSession> } | null>(null);
  const [selected, setSelected] = useState<WorkspaceDocument | null>(null);
  const [epoch, setEpoch] = useState(0);
  const [error, setError] = useState('');
  const [restoring, setRestoring] = useState(true);
  const [saveState, setSaveState] = useState<WorkspaceSaveState<WorkspaceDocument> | null>(null);
  const subscription = useRef<(() => void) | null>(null);
  const generation = useRef(0);
  const restoreFailed = useRef(false);
  const observation = useRef<AbortController | null>(null);
  const [restoredEvents, setRestoredEvents] = useState<AtlasExecutionEvent[]>([]);
  const [restoredRun, setRestoredRun] = useState('');
  const [observing, setObserving] = useState(false);
  const [savedSubmission, setSavedSubmission] = useState<{
    question?: string; prompt?: string; selectedContext?: unknown;
  }>();

  function clearSession() {
    ++generation.current;
    subscription.current?.();
    subscription.current = null;
    observation.current?.abort();
    session.current = null;
    createAttempt.current = null;
    restoreFailed.current = false;
    setSelected(null);
    setSaveState(null);
    setRestoredRun('');
    setRestoredEvents([]);
    setSavedSubmission(undefined);
    setObserving(false);
    setError('');
    setEpoch(value => value + 1);
  }

  function observe(next: WorkspaceSession) {
    observation.current?.abort();
    const recorded = next.observation();
    if (!recorded || !adapter.resume) {
      if (Object.values(next.document.state.operations ?? {}).some(o => o.state === 'bound')) {
        setError('Recorded run cannot be observed: complete saved correlation or observer is unavailable. No run was resubmitted.');
      }
      return;
    }
    const controller = new AbortController();
    observation.current = controller;
    const token = generation.current;
    setRestoredRun(recorded.context.runId);
    setObserving(true);
    void adapter.resume({
      ...recorded, signal: controller.signal, persistence: next.persistence('', true),
      onExecution(event) {
        if (controller.signal.aborted || token !== generation.current) return;
        setRestoredEvents(events => retainDrawerEvents(events, event));
        if (event.type === 'disconnected') void next.saveSelection({ last_event_id: event.lastEventId })
          .catch(() => setError('Observation cursor was not confirmed saved. Reconnect will replay from the saved cursor.'));
      },
    }).then(result => {
      if (controller.signal.aborted || token !== generation.current) return;
      if (result.disconnected) setError('Observation disconnected. Reconnect uses the same recorded run; no new run is submitted.');
    }).catch(() => {
      if (!controller.signal.aborted && token === generation.current) setError('Observation unavailable. No run was resubmitted.');
    }).finally(() => { if (token === generation.current) setObserving(false); });
  }

  function attach(next: WorkspaceSession) {
    subscription.current?.();
    session.current = next;
    setSaveState(next.queue.getState());
    subscription.current = next.queue.subscribe(setSaveState);
  }
  function restore(document: WorkspaceDocument, recordHistory = true) {
    const next = new WorkspaceSession(document, storage);
    next.restoredData(); // Refuse malformed graph before replacing the current view.
    restoreFailed.current = false;
    ++generation.current;
    attach(next);
    createAttempt.current = null;
    setSelected(document);
    setEpoch(value => value + 1);
    setError('');
    setRestoredEvents([]);
    setRestoredRun('');
    setSavedSubmission(undefined);
    const execution = document.state.selection?.execution;
    if (execution && typeof execution === 'object' && !Array.isArray(execution)
      && typeof execution.operationId === 'string') {
      const operation = document.state.operations?.[execution.operationId];
      if (operation && typeof execution.runId === 'string' && operation.run_id === execution.runId
        && operation.thread_id === execution.threadId) {
        setRestoredRun(execution.runId);
        setSavedSubmission({
          question: typeof document.state.selection?.submitted_question === 'string'
            ? document.state.selection.submitted_question : undefined,
          prompt: typeof operation.prompt === 'string' ? operation.prompt : undefined,
          selectedContext: operation.selected_context,
        });
      }
    }
    observe(next);
    const url = new URL(window.location.href);
    url.search = '';
    url.searchParams.set('workspace', document.workspace_id);
    if (recordHistory) window.history.pushState(null, '', url);
  }
  async function ensure(draft: string): Promise<WorkspaceSession> {
    if (restoreFailed.current) throw new Error('Workspace restore failed. Explicitly Start afresh or Continue a saved workspace before research.');
    if (session.current) return session.current;
    createAttempt.current ??= { id: crypto.randomUUID(), title: draft.trim().slice(0, 200) || 'Untitled workspace', draft };
    const attempt = createAttempt.current;
    if (attempt.pending) return attempt.pending;
    const token = generation.current;
    // Explicit retry of create uses the identical persisted intent/body, never a new workspace ID.
    attempt.pending = storage.create(attempt.title, attempt.draft, attempt.id).then(async receipt => {
      const document = await storage.latest(receipt.workspace_id);
      if (token !== generation.current) throw new Error('Workspace creation superseded. No research was dispatched.');
      const next = new WorkspaceSession(document, storage);
      attach(next);
      // Do not remount on create: keep the current query/operation and local graph.
      const url = new URL(window.location.href);
      url.searchParams.set('workspace', receipt.workspace_id);
      window.history.replaceState(null, '', url);
      return next;
    }).catch(error => {
      attempt.pending = undefined;
      setError('Workspace creation unavailable. Nothing was dispatched or confirmed saved.');
      throw error;
    });
    return attempt.pending;
  }
  useEffect(() => {
    let active = true;
    const id = new URLSearchParams(window.location.search).get('workspace');
    if (id) void storage.latest(id).then(document => { if (active) restore(document, false); })
      .catch(() => {
        if (active) {
          restoreFailed.current = true;
          setError('Saved workspace could not be restored. No run was submitted. Choose Continue or Start afresh explicitly.');
        }
      })
      .finally(() => { if (active) setRestoring(false); });
    else setRestoring(false);
    const pop = () => {
      const id = new URLSearchParams(window.location.search).get('workspace');
      setRestoring(true);
      void flush().then(async () => {
        if (!id) { if (active) clearSession(); return; }
        const document = await storage.latest(id);
        if (active) restore(document, false);
      })
        .catch(() => { if (active) setError('Navigation blocked: unsaved changes or workspace recovery failure.'); })
        .finally(() => { if (active) setRestoring(false); });
    };
    const unload = (event: BeforeUnloadEvent) => {
      if (session.current?.queue.getState().pending || createAttempt.current?.pending && !session.current) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('popstate', pop);
    window.addEventListener('beforeunload', unload);
    return () => {
      active = false; ++generation.current; subscription.current?.();
      observation.current?.abort();
      window.removeEventListener('popstate', pop);
      window.removeEventListener('beforeunload', unload);
    };
  }, [storage]);
  const flush = async () => { await session.current?.queue.flush(); };
  const stateLabel = saveState?.status === 'saved' ? `Saved · server revision ${saveState.revision}`
    : saveState?.status === 'saving' || saveState?.status === 'dirty' ? 'Saving — not yet confirmed'
      : saveState?.status === 'conflict' ? 'Conflict — dispatch blocked'
        : saveState?.status === 'failed' ? 'Save failed — changes remain unsaved' : 'Not saved yet';
  return <>
    <AtlasWorkspaceHistory storage={storage} beforeNavigate={flush} onContinue={document => restore(document)} onNew={() => {
      clearSession();
      window.history.pushState(null, '', window.location.pathname);
    }} />
    <section aria-label="Workspace server save state">
      <p role="status">{stateLabel}</p>
      {error && <p role="alert">{error}</p>}
      {session.current && <button type="button" onClick={() => {
        void flush().then(() => storage.latest(session.current!.document.workspace_id))
          .then(document => restore(document))
          .catch(() => setError('Reload failed or saves remain pending. No new run was submitted.'));
      }}>Reload server workspace</button>}
      {restoredRun && <button type="button" disabled={observing} onClick={() => {
        if (session.current) observe(session.current);
      }}>{observing ? 'Observing recorded run…' : 'Reconnect recorded run'}</button>}
      {session.current && !session.current.canResearch() && <p>
        Unresolved operation or save: fresh research is blocked. No run is automatically resubmitted.</p>}
      {saveState?.status === 'failed' && <button type="button" onClick={() => {
        void session.current?.queue.retry().catch(() => setError('Retry failed. Original save remains pending.'));
      }}>Retry identical save</button>}
      {saveState?.status === 'conflict' && <button type="button" onClick={() => {
        if (saveState.latest) restore(saveState.latest);
      }} disabled={!saveState.latest}>Reload authorized latest</button>}
      {(saveState?.status === 'conflict' || saveState?.status === 'failed') && <p>
        Keep this tab as a local recovery copy. It cannot dispatch research while saves are blocked.</p>}
    </section>
    {restoredRun && <AtlasRunDrawer events={restoredEvents} runId={restoredRun} savedSubmission={savedSubmission} />}
    {!restoring && <AtlasWorkspace key={epoch} adapter={adapter} approverOrigin={approverOrigin} durable={{
      get workspaceId() { return session.current?.document.workspace_id; },
      query: selected?.state.draft ?? '',
      data: selected ? session.current?.restoredData() ?? null : null,
      beforeResearch: async query => {
        const current = await ensure(query);
        await current.queue.flush();
        return current.persistence(query);
      },
      commit: async data => {
        const current = session.current;
        if (!current) throw new Error('Workspace storage is unavailable.');
        await current.commit(data);
      },
      checkpoint: async draft => {
        if (!draft.trim() && !session.current) return;
        const current = await ensure(draft);
        await current.save('draft_checkpoint', { draft });
      },
      actionDrafts: selected?.state.action_draft?.drafts
        && typeof selected.state.action_draft.drafts === 'object'
        && !Array.isArray(selected.state.action_draft.drafts)
        ? selected.state.action_draft.drafts as Record<string, unknown> : {},
      saveActionDrafts: async (drafts: Record<string, AtlasActionDraftV1>) => {
        const current = session.current ?? await ensure(selected?.state.draft ?? '');
        await current.save('action_draft_changed', {
          action_draft: { drafts: JSON.parse(JSON.stringify(drafts)) },
        });
      },
      select: async data => {
        if (session.current) {
          // Persist navigation-created paths before referring to them in selection.
          await session.current.commit(data);
          await session.current.saveSelection({
            path_id: data.snapshot.active_path_id, graph_revision: data.snapshot.revision,
          });
        }
      },
      execution: event => {
        if (event.type === 'submitted') void session.current?.saveSelection({
          execution: event.submission.context, last_event_id: '0', submitted_question: event.submission.question,
        }).catch(() => setError('Run observation correlation was not confirmed saved.'));
        if (event.type === 'disconnected') void session.current?.saveSelection({
          last_event_id: event.lastEventId,
        }).catch(() => setError('Run cursor was not confirmed saved.'));
      },
    }} />}
  </>;
}
