import { useEffect, useRef, useState } from 'react';
import { describeAtlasError, type AtlasApproval, type AtlasWorkspaceAdapter } from '../explorationAdapter';

export function AtlasApprovalReview({ approval, adapter, onResult, onDismiss }: {
  approval: AtlasApproval; adapter: Pick<AtlasWorkspaceAdapter, 'decide'>;
  onResult: (result: AtlasApproval) => void; onDismiss: () => void;
}) {
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLButtonElement>('button')?.focus();
    return () => previous?.focus();
  }, []);
  async function decide(decision: 'approve' | 'reject') {
    setBusy(true);
    setError('');
    try {
      // Only a server response may change the approval's status.
      onResult(await adapter.decide(approval, decision, decision === 'reject' ? reason.trim() : undefined));
    } catch (e) { setError(describeAtlasError(e, 'approval')); }
    finally { setBusy(false); }
  }
  return <div className="atlas-live__overlay">
    <div role="dialog" aria-modal="true" aria-labelledby="atlas-approval-title" className="atlas-live__approval"
      ref={dialog} onKeyDown={event => {
        if (event.key === 'Escape' && !busy) onDismiss();
        if (event.key !== 'Tab') return;
        const nodes = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), textarea') ?? []);
        const first = nodes[0], last = nodes[nodes.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }}>
      <header><h2 id="atlas-approval-title">Review server approval request</h2>
        <button type="button" disabled={busy} onClick={onDismiss}>Dismiss review</button></header>
      <p>Separate authorizer required. The server enforces role, digest and execution constraints; this UI cannot grant itself approval.</p>
      <dl><dt>Run id</dt><dd>{approval.runId}</dd><dt>Destination</dt><dd><code>{approval.destination}</code></dd>
        <dt>Payload digest</dt><dd><code>{approval.digest}</code></dd><dt>Server status</dt><dd>{approval.status}</dd></dl>
      <h3>Actual arguments</h3><pre>{JSON.stringify(approval.args, null, 2)}</pre>
      {approval.expiresAt && <p>Expires: {approval.expiresAt}</p>}
      {approval.calls?.map(call => <section key={call.invocationId}>
        <h3>{call.toolName}</h3><p>Destination: <code>{call.destination}</code></p>
        <pre>{JSON.stringify(call.args, null, 2)}</pre>
      </section>)}
      <label>Rejection reason<textarea value={reason} onChange={e => setReason(e.target.value)} /></label>
      {error && <p role="alert">{error}</p>}
      <div className="atlas-live__toolbar"><button type="button" disabled={busy || !reason.trim() || approval.status !== 'pending'}
        onClick={() => void decide('reject')}>Reject request</button>
        <button type="button" disabled={busy || approval.status !== 'pending'} onClick={() => void decide('approve')}>Approve request</button></div>
      {busy && <p role="status">Submitting decision to server…</p>}
      <p>Dismiss only closes this review. It does not reject or approve the request.</p>
    </div>
  </div>;
}
