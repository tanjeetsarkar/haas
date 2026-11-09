import { useEffect, useRef, useState } from 'react';
import type { AtlasApproverAdapter, AtlasApproverIdentity } from '../approverAdapter';
import { describeAtlasError, type AtlasApproval } from '../explorationAdapter';
import { AtlasApprovalReview } from './AtlasApprovalReview';
import './AtlasPreview.css';
import './AtlasWorkspace.css';

export function AtlasApproverPage({ adapter }: { adapter: AtlasApproverAdapter }) {
  const [identity, setIdentity] = useState<AtlasApproverIdentity | null>(null);
  const [batchId, setBatchId] = useState(() => new URLSearchParams(window.location.search).get('batch') ?? '');
  const [approval, setApproval] = useState<AtlasApproval | null>(null);
  const [review, setReview] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('Checking this window’s authenticated operator identity…');
  const generation = useRef(0);
  useEffect(() => {
    let active = true;
    void adapter.identity().then(value => {
      if (!active) return;
      setIdentity(value);
      setStatus(value.roles.includes('approver')
        ? 'Approver role reported by server. Load a batch to review; the server must still verify separate-authorizer constraints.'
        : 'This connection does not report the approver role. Open the independently configured operator approver window.');
    }).catch(e => {
      if (active) {
        setError(describeAtlasError(e, 'approval'));
        setStatus('No authenticated approver connection. No batch has been fetched and no decision is possible.');
      }
    });
    return () => { active = false; ++generation.current; };
  }, [adapter]);
  const canLoad = identity?.roles.includes('approver') === true;
  async function load() {
    if (!canLoad || !batchId.trim()) return;
    const token = ++generation.current;
    setLoading(true);
    setError('');
    setApproval(null);
    setReview(false);
    setStatus('Fetching actual server approval batch…');
    try {
      const result = await adapter.getApproval(batchId.trim());
      if (token !== generation.current) return;
      if (result.id !== batchId.trim()) throw new Error('Approval batch identity mismatch.');
      setApproval(result);
      setStatus(`Server batch status: ${result.status}. Approval is not execution confirmation.`);
      const url = new URL(window.location.href);
      url.search = new URLSearchParams({ atlasApprover: '1', batch: result.id }).toString();
      window.history.replaceState(null, '', url);
    } catch (e) {
      if (token === generation.current) {
        setError(describeAtlasError(e, 'approval'));
        setStatus('Batch could not be loaded. No decision has been made.');
      }
    } finally { if (token === generation.current) setLoading(false); }
  }
  return <main className="atlas-preview atlas-live" aria-label="Atlas operator approver">
    <header className="atlas-preview__header"><div className="atlas-preview__brand">
      <div className="atlas-preview__eyebrow">ATLAS · INDEPENDENT OPERATOR REVIEW</div>
      <h1>Review an approval batch</h1><p>This window uses only its own server-authenticated connection.</p>
    </div></header>
    <div className="atlas-preview__notice" role="note">This page cannot switch roles or credentials.
      An approver role alone does not prove a separate principal: the server enforces that requirement for every decision.
      Default submitter credentials must not become approver credentials.</div>
    {identity && <section className="atlas-live__persistence" aria-label="Authenticated operator identity">
      <h2>Server-reported identity</h2><dl><dt>Principal</dt><dd>{identity.principalId}</dd>
        <dt>Tenant</dt><dd>{identity.tenantId}</dd><dt>Roles</dt><dd>{identity.roles.join(', ')}</dd></dl>
      <p>Separate-authorizer verification remains pending until the server accepts a decision.</p>
    </section>}
    <form className="atlas-preview__search" onSubmit={e => { e.preventDefault(); void load(); }}>
      <label htmlFor="atlas-approval-batch">Approval batch id</label>
      <div className="atlas-preview__search-controls">
        <input id="atlas-approval-batch" value={batchId} onChange={e => setBatchId(e.target.value)} required />
        <button type="submit" disabled={!canLoad || loading || !batchId.trim()}>{loading ? 'Loading batch…' : 'Load approval batch'}</button>
      </div>
    </form>
    <div role="status" className="atlas-preview__status">{status}</div>
    {error && <div role="alert" className="atlas-preview__error">{error}</div>}
    {approval && <section className="atlas-live__persistence" aria-label="Loaded approval batch">
      <h2>Batch {approval.id}</h2><p>Run: {approval.runId}</p><p>Server status: {approval.status}</p>
      <p>Expires: {approval.expiresAt ?? 'Not reported'}</p><p>Destination: <code>{approval.destination}</code></p>
      <p>Digest: <code>{approval.digest}</code></p>
      <button type="button" onClick={() => setReview(true)}>Review actual request</button>
      <p>Loading or dismissing a batch does not approve, reject or execute it.</p>
    </section>}
    {approval && review && <AtlasApprovalReview key={approval.id} approval={approval} adapter={adapter}
      onDismiss={() => setReview(false)} onResult={result => {
        setApproval(result);
        setStatus(`Server batch status: ${result.status}. This is not confirmation of external execution.`);
      }} />}
  </main>;
}
