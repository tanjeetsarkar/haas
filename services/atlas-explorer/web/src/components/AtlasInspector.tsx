import type { ReactNode } from 'react';
import type { AtlasActionDraftV1, AtlasEvidence, AtlasExplorationSnapshot, AtlasSource, AtlasTarget } from '@haas/atlas-explorer-client';
import type { AtlasPresentation } from '../explorationAdapter';
import { evidenceSources, facetResearchComplete, targetClaims, targetRelationships, targetTitle } from '../graphProjection';
import { PreviewConnections } from './PreviewConnections';
import { AtlasCardActions } from './AtlasCardActions';

export function AtlasInspectorFrame({ children, sample = false }: { children: ReactNode; sample?: boolean }) {
  return <aside className="atlas-preview__inspector" aria-label="Evidence and details">
    <div className="atlas-preview__inspector-top"><span>DETAILS & EVIDENCE</span>
      <span className="atlas-preview__synthetic-tag">{sample ? 'SYNTHETIC' : 'RESEARCH SNAPSHOT'}</span>
    </div>{children}
  </aside>;
}

export function sourceTitles(snapshot: AtlasExplorationSnapshot, ids: string[]): string[] {
  return ids.map(id => snapshot.sources.find(s => s.source_id === id)?.title ?? 'Unknown source');
}

export function EvidenceLabel({ status, sample = true }: { status: string; sample?: boolean }) {
  const label = status === 'sourced' ? sample ? 'Sourced · unverified sample' : 'Sourced (not independently verified)'
    : status === 'conflicting' ? sample ? 'Conflicting sample evidence' : 'Conflicting evidence' : 'Unsupported';
  return <span className={`atlas-preview__evidence evidence-${status}`}>{label}</span>;
}

export function RelationshipEvidence({ snapshot, evidence, sample = true }: {
  snapshot: AtlasExplorationSnapshot; evidence: AtlasEvidence; sample?: boolean;
}) {
  return <div className="atlas-preview__relationship-evidence">
    <EvidenceLabel status={evidence.status} sample={sample} />
    {evidence.status === 'unsupported' && <p>{evidence.reason}</p>}
    {evidence.status === 'sourced' && <p>Relationship evidence: {sourceTitles(snapshot, evidence.source_ids).join(', ')}</p>}
    {evidence.status === 'conflicting' && <>
      <p>Supports relationship: {sourceTitles(snapshot, evidence.supporting_source_ids).join(', ')}</p>
      <p>Opposes relationship: {sourceTitles(snapshot, evidence.opposing_source_ids).join(', ')}</p>
    </>}
    <small>Evidence concerns this connection, not unrelated facet claims.</small>
  </div>;
}

function SourceRecord({ source, sample, detail = false }: { source: AtlasSource; sample: boolean; detail?: boolean }) {
  // URLs are data, never executable javascript/data links. Preview never fetches.
  const safe = /^https?:\/\//i.test(source.url);
  return <article className="atlas-preview__source">
    <strong>{source.title}</strong>
    <span>{source.publisher} · {source.trust}{sample ? detail ? ' · never fetched' : ' · sample only' : ''}</span>
    {!sample && safe ? <a href={source.url} target="_blank" rel="noopener noreferrer">{source.url}</a> : <code>{source.url}</code>}
    <p>{source.excerpt}</p>
    <span>{sample ? 'Retrieved in fixture' : 'Retrieved'}: {new Date(source.retrieved_at).toLocaleString()}</span>
  </article>;
}

export function SourceList({ snapshot, entityId, facetKey, relationshipEvidence, sample = true }: {
  snapshot: AtlasExplorationSnapshot; entityId: string; facetKey?: string; relationshipEvidence: AtlasEvidence[]; sample?: boolean;
}) {
  const claims = targetClaims(snapshot, facetKey === undefined
    ? { kind: 'entity', entity_id: entityId } : { kind: 'facet', entity_id: entityId, facet_key: facetKey });
  const ids = new Set([...claims.map(c => c.evidence), ...relationshipEvidence].flatMap(evidenceSources));
  const sources = snapshot.sources.filter(s => ids.has(s.source_id));
  return <section className="atlas-preview__detail-section" aria-label="Sources">
    <h3>Sources <span>{sources.length}</span></h3>
    {!sources.length ? <p className="atlas-preview__muted">No source records for this selection.</p>
      : sources.map(source => <SourceRecord key={source.source_id} source={source} sample={sample} />)}
  </section>;
}

export function SourceDetails({ snapshot, sourceId, sample = true }: {
  snapshot: AtlasExplorationSnapshot; sourceId: string; sample?: boolean;
}) {
  const source = snapshot.sources.find(s => s.source_id === sourceId);
  return <section className="atlas-preview__detail-section"><h3>Source record</h3>
    {source ? <SourceRecord source={source} sample={sample} detail /> : <p>Source is not present in this snapshot.</p>}
  </section>;
}

export function AtlasAttributes({ presentation }: { presentation?: AtlasPresentation }) {
  if (!presentation) return null;
  return <>
    {!!presentation.attributes.length && <section aria-label="Attributes" className="atlas-preview__detail-section">
      <h3>Attributes</h3><dl className="atlas-live__attributes">
        {presentation.attributes.map(a => <div key={a.key}><dt>{a.label}</dt>
          <dd>{a.value === null ? 'Not reported' : Array.isArray(a.value) ? a.value.join(', ') : String(a.value)}
            {a.unit && <> {a.unit}</>}{a.uncertainty && <small>Uncertainty: {a.uncertainty}</small>}</dd></div>)}
      </dl>
    </section>}
    {presentation.checklist && <section aria-label="Checklist" className="atlas-preview__detail-section">
      <h3>Checklist</h3><ul>{presentation.checklist.map(item => <li key={item.id}>
        <label><input type="checkbox" checked={item.completed ?? false} readOnly /> {item.text}
          {item.required && <strong> · Required</strong>}</label>
      </li>)}</ul><small>Proposed checklist from the snapshot; no task completion or execution is inferred.</small>
    </section>}
    {presentation.draft && <section aria-label="Action draft" className="atlas-preview__detail-section">
      <h3>{presentation.draft.title}</h3><pre>{presentation.draft.body}</pre>
      <small>Draft only · nothing sent. External execution requires a server-issued approval review.</small>
    </section>}
  </>;
}

export function AtlasInspector({ snapshot, target, presentations, onNavigate, onResearch, onResearchAgain,
  actionDrafts, editingActionCard, actionDraftText, onEditAction, onActionDraftText, onSaveActionDraft }: {
  snapshot: AtlasExplorationSnapshot;
  target: AtlasTarget | null;
  presentations?: AtlasPresentation[];
  onNavigate: (entityId: string) => void;
  onResearch: (target: AtlasTarget) => void;
  onResearchAgain?: (target: AtlasTarget) => void;
  actionDrafts: Record<string, unknown>;
  editingActionCard: string | null;
  actionDraftText: string;
  onEditAction: (cardId: string, initialText: string) => void;
  onActionDraftText: (value: string) => void;
  onSaveActionDraft?: (cardId: string, draft: AtlasActionDraftV1) => Promise<void>;
}) {
  if (!target) return <AtlasInspectorFrame><div className="atlas-preview__inspector-empty">
    <h2>Select a starting point</h2><p>Select an entity, facet or source to inspect actual research data.</p>
  </div></AtlasInspectorFrame>;
  const entity = target.kind !== 'source' ? snapshot.entities.find(e => e.entity_id === target.entity_id) : undefined;
  const facet = target.kind === 'facet' ? entity?.facets.find(f => f.key === target.facet_key) : undefined;
  const claims = targetClaims(snapshot, target);
  const relationships = targetRelationships(snapshot, target);
  const researched = facetResearchComplete(snapshot, target);
  return <AtlasInspectorFrame>
    <div className="atlas-preview__detail-title"><span>{target.kind}</span><h2>{targetTitle(snapshot, target)}</h2>
      {entity && <><p>{entity.summary}</p><small>Identity: {entity.identity_status} · {entity.entity_id}</small>
        {entity.external_references.map(r => <p key={`${r.namespace}:${r.value}`}>{r.namespace}: {r.value}</p>)}</>}
    </div>
    <AtlasCardActions snapshot={snapshot} target={target} drafts={actionDrafts}
      editingCardId={editingActionCard} draftText={actionDraftText}
      onEdit={onEditAction} onDraftText={onActionDraftText} onSaveDraft={onSaveActionDraft} />
    {onResearchAgain && <button type="button" onClick={event => {
      event.stopPropagation();
      onResearchAgain(target);
    }}>Research again</button>}
    {entity?.identity_status === 'candidate' && target.kind === 'entity' && <div className="atlas-preview__facet-note">
      <strong>Candidate identity · not resolved</strong><p>Do not attribute claims to this person until identity is resolved.</p>
      <button type="button" onClick={() => onResearch(target)}>Resolve this identity</button>
    </div>}
    {facet && <div className="atlas-preview__facet-note"><p>{facet.scope}</p>
      <strong>{facet.status === 'known' || researched ? 'Known results' : facet.status === 'research_needed' ? 'Research needed' : 'Unavailable'}</strong>
      {facet.status === 'unavailable' && <p>{facet.reason}</p>}
      {facet.status === 'research_needed' && !researched && <button type="button" onClick={() => onResearch(target)}>Research this facet</button>}
    </div>}
    {target.kind === 'source' ? <SourceDetails snapshot={snapshot} sourceId={target.source_id} sample={false} /> : <>
      <AtlasAttributes presentation={presentations?.find(p => p.entityId === target.entity_id)} />
      <section className="atlas-preview__detail-section" aria-label="Claims and evidence"><h3>Claims & evidence</h3>
        {!claims.length && <p>No claims are attached here. Missing information is not a negative finding.</p>}
        {claims.map(c => <article className="atlas-preview__claim" key={c.claim_id}>
          <p>{c.text}</p><EvidenceLabel status={c.evidence.status} sample={false} />
          <small>Observed: {new Date(c.observed_at).toLocaleString()}</small>
          {c.details && <AtlasAttributes presentation={{ entityId: c.subject_entity_id,
            attributes: c.details.attributes ?? [], checklist: c.details.checklist, draft: c.details.draft }} />}
          {c.evidence.status === 'unsupported' ? <p>{c.evidence.reason}</p> : c.evidence.status === 'conflicting'
            ? <div className="atlas-preview__conflict">
              <span>Supports: {sourceTitles(snapshot, c.evidence.supporting_source_ids).join(', ')}</span>
              <span>Differs: {sourceTitles(snapshot, c.evidence.opposing_source_ids).join(', ')}</span></div>
            : <p>Evidence: {sourceTitles(snapshot, c.evidence.source_ids).join(', ')}</p>}
        </article>)}
      </section>
      <SourceList snapshot={snapshot} entityId={target.entity_id}
        facetKey={target.kind === 'facet' ? target.facet_key : undefined}
        relationshipEvidence={relationships.map(r => r.evidence)} sample={false} />
      <section className="atlas-preview__detail-section" aria-label="Related connections"><h3>Related connections</h3>
        {target.kind === 'facet' && <p>Connections are entity-wide; select the parent entity to inspect them. They are not evidence for every facet.</p>}
        <PreviewConnections snapshot={snapshot} entityId={target.entity_id} relationships={relationships}
          sample={false} onNavigate={onNavigate} />
        {relationships.map(r => <RelationshipEvidence key={r.relationship_id} snapshot={snapshot} evidence={r.evidence} sample={false} />)}
      </section>
      <section className="atlas-preview__detail-section"><h3>Actions</h3>
        {snapshot.actions.filter(a => a.target.kind !== 'source' && a.target.entity_id === target.entity_id).map(a =>
          <p key={a.action_id}>{a.label} · {a.kind === 'external' ? 'Requires server approval before execution' : 'Local inspection'}</p>)}
        <p>Only actual server-issued pending approvals can be approved. A suggested action is not authorization.</p>
        <button type="button" disabled>External inquiry initiation unavailable</button>
        <p>Drafts remain unsent. Exact deterministic action initiation awaits the supported core/client contract.</p>
      </section>
    </>}
  </AtlasInspectorFrame>;
}
