import type { KeyboardEvent } from 'react';
import type { AtlasExplorationSnapshot, AtlasOccurrence, AtlasTarget } from '@haas/atlas-explorer-client';
import { facetResearchComplete, targetKey, targetTitle } from '../graphProjection';

export function AtlasGraphColumn({ snapshot, title, rows, index, active, selected, explored, emptyMessage,
  sample = false, onSelect, onKeyDown }: {
  snapshot: AtlasExplorationSnapshot;
  title: string;
  rows: AtlasOccurrence[];
  index: number;
  active: boolean;
  selected?: AtlasTarget;
  explored: Set<string>;
  emptyMessage: string;
  sample?: boolean;
  onSelect: (row: AtlasOccurrence, index: number) => void;
  onKeyDown: (event: KeyboardEvent<HTMLElement>, rows: AtlasOccurrence[], index: number) => void;
}) {
  return <section className={`atlas-preview__column${active ? ' is-mobile-active' : ''}`}
    aria-label={`${title} column`} onKeyDown={event => onKeyDown(event, rows, index)}>
    <header className="atlas-preview__column-heading">
      <div><span>STEP {index + 1}</span><h2>{title}</h2></div><span>{rows.length}</span>
    </header>
    <div className="atlas-preview__rows">
      {!rows.length && <p className="atlas-preview__empty">{emptyMessage}</p>}
      {rows.map(row => {
        const target = row.target;
        const entity = target.kind !== 'source' ? snapshot.entities.find(e => e.entity_id === target.entity_id) : undefined;
        const facet = target.kind === 'facet' ? entity?.facets.find(f => f.key === target.facet_key) : undefined;
        const summary = target.kind === 'entity' ? entity?.summary : target.kind === 'source'
          ? snapshot.sources.find(s => s.source_id === target.source_id)?.publisher
          : facet?.status === 'unavailable' ? facet.reason : facet?.scope;
        const status = facet?.status === 'unavailable' ? 'Unavailable'
          : facet?.status === 'research_needed' && !facetResearchComplete(snapshot, target) ? 'Research needed' : facet ? sample ? 'Sample details' : 'Known results' : null;
        const isSelected = selected && targetKey(selected) === targetKey(target);
        return <button className={`atlas-preview__row type-${target.kind}${isSelected ? ' is-selected' : ''}`}
          key={targetKey(target)} type="button" data-atlas-row aria-current={isSelected ? 'step' : undefined}
          onClick={() => onSelect(row, index)}>
          <span className="atlas-preview__row-type">{target.kind === 'entity' ? 'Entity' : target.kind === 'facet' ? 'Facet' : 'Source'}</span>
          <strong>{targetTitle(snapshot, target)}</strong>
          <span className="atlas-preview__row-summary">{summary}</span>
          {target.kind === 'entity' && explored.has(target.entity_id) && <span className="atlas-preview__facet-status">Already explored</span>}
          {status && <span className={`atlas-preview__facet-status status-${status.toLowerCase().replace(/\s/g, '-')}`}>{status}</span>}
          <span className="atlas-preview__row-arrow" aria-hidden="true">›</span>
        </button>;
      })}
    </div>
  </section>;
}
