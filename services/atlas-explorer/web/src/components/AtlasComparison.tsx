import type { AtlasExplorationSnapshot } from '@haas/atlas-explorer-client';
import type { AtlasPresentation } from '../explorationAdapter';
import { claimPresentations } from '../graphProjection';

export function AtlasComparison({ snapshot, presentations = [], selectedIds, onToggle }: {
  snapshot: AtlasExplorationSnapshot; presentations?: AtlasPresentation[];
  selectedIds: string[]; onToggle: (id: string) => void;
}) {
  const entities = snapshot.entities.filter(e => selectedIds.includes(e.entity_id));
  presentations = [...presentations, ...claimPresentations(snapshot)];
  const keys = [...new Set(presentations.filter(p => selectedIds.includes(p.entityId)).flatMap(p => p.attributes.map(a => a.key)))];
  return <details className="atlas-live__comparison">
    <summary>Compare entities · {entities.length} selected</summary>
    <fieldset><legend>Select entities to compare</legend>
      {snapshot.entities.map(e => <label key={e.entity_id}><input type="checkbox"
        checked={selectedIds.includes(e.entity_id)} onChange={() => onToggle(e.entity_id)} /> {e.title}</label>)}
    </fieldset>
    {!!entities.length && <div className="atlas-live__table-scroll" tabIndex={0} role="region" aria-label="Comparison table">
      <table><caption>Reported attributes and claims · missing values are not estimates</caption>
        <thead><tr><th scope="col">Attribute</th>{entities.map(e => <th scope="col" key={e.entity_id}>{e.title}<small>{e.entity_id}</small></th>)}</tr></thead>
        <tbody>
          <tr><th scope="row">Identity</th>{entities.map(e => <td key={e.entity_id}>{e.identity_status}</td>)}</tr>
          {keys.map(key => <tr key={key}><th scope="row">{presentations.flatMap(p => p.attributes).find(a => a.key === key)?.label ?? key}</th>
            {entities.map(e => {
              const attributes = presentations.filter(p => p.entityId === e.entity_id).flatMap(p => p.attributes).filter(a => a.key === key);
              return <td key={e.entity_id}>{!attributes.length ? 'Not reported' : attributes.map((a, i) => <div key={i}>
                {a.value === null ? 'Not reported' : `${Array.isArray(a.value) ? a.value.join(', ') : String(a.value)}${a.unit ? ` ${a.unit}` : ''}`}
                {a.uncertainty && <small>Uncertainty: {a.uncertainty}</small>}</div>)}</td>;
            })}</tr>)}
          <tr><th scope="row">Claims / evidence</th>{entities.map(e => <td key={e.entity_id}>
            {snapshot.claims.filter(c => c.subject_entity_id === e.entity_id).map(c => <p key={c.claim_id}>
              {c.text}<small>{c.evidence.status} · {c.observed_at}</small>
            </p>)}
            {!snapshot.claims.some(c => c.subject_entity_id === e.entity_id) && 'Not reported'}
          </td>)}</tr>
        </tbody>
      </table>
    </div>}
  </details>;
}
