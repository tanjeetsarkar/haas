import { getExplorationBreadcrumbs, type AtlasExplorationSnapshot, type AtlasTarget } from '@haas/atlas-explorer-client';
import { targetKey } from './graphProjection';

export type ScopedDive = { kind: 'evidence' | 'cached' } | { kind: 'unavailable'; reason: string }
  | { kind: 'research'; question: string; identityFirst: boolean };

/** Call after selecting the canonical ancestry. Never treats sibling research as a cache hit. */
export function scopedDive(snapshot: AtlasExplorationSnapshot, target: AtlasTarget): ScopedDive {
  if (target.kind === 'source') return { kind: 'evidence' };
  const entity = snapshot.entities.find(e => e.entity_id === target.entity_id);
  if (!entity) return { kind: 'unavailable', reason: 'Selected identity is unavailable in this graph.' };
  if (target.kind === 'facet') {
    const facet = entity.facets.find(f => f.key === target.facet_key);
    if (!facet) return { kind: 'unavailable', reason: 'Selected facet is unavailable.' };
    if (facet.status === 'unavailable') return { kind: 'unavailable', reason: facet.reason };
    if (entity.identity_status === 'candidate') return {
      kind: 'unavailable', reason: 'Resolve this candidate identity before researching its facets.',
    };
  }
  const ancestry = getExplorationBreadcrumbs(snapshot);
  const selected = ancestry.find(o => targetKey(o.target) === targetKey(target));
  if (selected
    && snapshot.requests.some(r => r.status === 'completed' && r.parent_occurrence_id === selected.occurrence_id)) {
    return { kind: 'cached' };
  }

  // Keep the exact root question; the client builds the scoped prompt from the selected ancestry.
  return { kind: 'research', question: snapshot.root_query, identityFirst: entity.identity_status === 'candidate' };
}

export function scopedDiveKey(snapshot: AtlasExplorationSnapshot): string {
  return `${snapshot.exploration_id}:${getExplorationBreadcrumbs(snapshot).map(o => targetKey(o.target)).join('/')}`;
}
