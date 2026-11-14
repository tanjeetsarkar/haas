import { useEffect, useRef, useState } from 'react';
import {
  blocksResearch, type WorkspaceCatalog, type WorkspaceDocument, WorkspaceStorageApi,
} from '../workspaceStorageApi';

/** Catalog/load only. Parent must detach observation and flush before replacing its selected document. */
export function AtlasWorkspaceHistory({ storage, beforeNavigate, onContinue, onNew }: {
  storage: WorkspaceStorageApi;
  beforeNavigate(): Promise<void>;
  onContinue(document: WorkspaceDocument): void;
  onNew(): void;
}) {
  const [catalog, setCatalog] = useState<WorkspaceCatalog>({ items: [], next_cursor: null });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  async function refresh(cursor?: string) {
    const token = ++generation.current;
    setBusy(true);
    setError('');
    try {
      const result = await storage.catalog(cursor);
      if (token === generation.current) setCatalog(current => cursor
        ? { ...result, items: [...current.items, ...result.items] } : result);
    } catch { if (token === generation.current) setError('Workspace history unavailable. No server save or recovery is assumed.'); }
    finally { if (token === generation.current) setBusy(false); }
  }
  useEffect(() => {
    void refresh();
    return () => { ++generation.current; };
  }, [storage]);
  async function load(id: string) {
    const token = ++generation.current;
    setBusy(true);
    setError('');
    try {
      await beforeNavigate();
      const document = await storage.latest(id);
      if (token !== generation.current) return;
      onContinue(document);
      if (blocksResearch(document)) setError('Unfinished submission requires recovery. Fresh research is blocked; nothing was resubmitted.');
    } catch { if (token === generation.current) setError('Continue failed or unsaved changes remain. Current workspace was not replaced.'); }
    finally { if (token === generation.current) setBusy(false); }
  }
  return <section aria-label="Workspace history">
    <h2>Workspaces</h2>
    <button type="button" disabled={busy} onClick={() => {
      setBusy(true);
      void beforeNavigate().then(onNew).catch(() => setError('Unsaved changes remain. Wait for save or keep a recovery copy.'))
        .finally(() => setBusy(false));
    }}>Start afresh</button>
    <button type="button" disabled={busy} onClick={() => void refresh()}>Refresh history</button>
    <p>Continue only restores saved state. Start afresh never deletes the previous workspace.</p>
    {error && <p role="alert">{error}</p>}
    <ul>{catalog.items.map(item => <li key={item.workspace_id}>
      {item.title} · revision {item.revision} · {item.updated_at}
      <button type="button" disabled={busy} onClick={() => void load(item.workspace_id)}>Continue {item.title}</button>
    </li>)}</ul>
    {catalog.next_cursor && <button type="button" disabled={busy}
      onClick={() => void refresh(catalog.next_cursor!)}>More workspaces</button>}
  </section>;
}
