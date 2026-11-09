import type { AtlasExplorationSnapshot, AtlasRelationship } from '@haas/atlas-explorer-client';

type Props = {
  snapshot: AtlasExplorationSnapshot;
  entityId: string;
  relationships: AtlasRelationship[];
  onNavigate: (entityId: string) => void;
  sample?: boolean;
};

/** A local, direct-neighbor projection, not a global graph or inferred connection. */
export function PreviewConnections({ snapshot, entityId, relationships, onNavigate, sample = true }: Props) {
  const entityTitle = (id: string) => snapshot.entities.find(entity => entity.entity_id === id)?.title ?? 'Sample subject';
  const explored = new Set(snapshot.paths.flatMap(path => path.selected_occurrence_ids)
    .map(id => snapshot.occurrences.find(occurrence => occurrence.occurrence_id === id)?.target)
    .filter(target => target?.kind === 'entity')
    .map(target => target.entity_id));
  return (
    <details className="atlas-preview__connection-view">
      <summary>Connection view · {relationships.length} direct connections</summary>
      <p>{sample ? 'Local synthetic neighbors for this selection only.' : 'Direct neighbors from this research snapshot only.'} Lines show explicit relationships,
        not inferred links between people who share an organization. {sample ? 'No live graph research.' : 'Connections retain their stated evidence; no inferred edges are added.'}</p>
      <div className="atlas-preview__connection-center">
        {entityTitle(entityId)}
        {explored.has(entityId) && <small>Already explored</small>}
      </div>
      <ul aria-label="Direct connections">
        {relationships.map(relationship => {
          const incoming = relationship.to_entity_id === entityId;
          const neighborId = incoming ? relationship.from_entity_id : relationship.to_entity_id;
          return (
            <li key={relationship.relationship_id}>
              <div className={`atlas-preview__visual-thread${incoming ? ' is-incoming' : ''}`} aria-hidden="true">
                <span>{incoming ? '←' : '→'}</span>
                <span>{relationship.label}</span>
              </div>
              <span className="atlas-preview__connection-direction">{incoming ? 'Incoming' : 'Outgoing'}</span>
              <p>{entityTitle(relationship.from_entity_id)} <strong>{relationship.label}</strong> {entityTitle(relationship.to_entity_id)}</p>
              <small>{relationship.verification === 'suggested' ? 'Suggested' : sample ? 'Sourced' : 'Confirmed (as reported)'} relationship
                {' · '}{relationship.evidence.status === 'unsupported' ? 'unsupported evidence'
                  : relationship.evidence.status === 'conflicting' ? 'conflicting evidence' : sample ? 'unverified sample sources' : 'source-backed evidence'}</small>
              <button type="button" aria-label={`Explore ${entityTitle(neighborId)}`}
                onClick={() => onNavigate(neighborId)}>
                {entityTitle(neighborId)} <span aria-hidden="true">›</span>
              </button>
              {explored.has(neighborId) && <small className="atlas-preview__already-explored">Already explored</small>}
            </li>
          );
        })}
      </ul>
      {relationships.length === 0 && <p>No direct relationships in this {sample ? 'fixture' : 'selection'} scope.</p>}
    </details>
  );
}
