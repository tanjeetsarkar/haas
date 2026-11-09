import {
  getExplorationBreadcrumbs,
  type AtlasEvidence,
  type AtlasExplorationSnapshot,
  type AtlasOccurrence,
  type AtlasTarget,
} from '@haas/atlas-explorer-client';
import type { AtlasPresentation } from './explorationAdapter';

export function claimPresentations(snapshot: AtlasExplorationSnapshot): AtlasPresentation[] {
  return snapshot.claims.flatMap(claim => claim.details ? [{
    entityId: claim.subject_entity_id, attributes: claim.details.attributes ?? [],
    checklist: claim.details.checklist, draft: claim.details.draft,
  }] : []);
}

export function targetKey(target: AtlasTarget): string {
  if (target.kind === 'source') return `source:${target.source_id}`;
  if (target.kind === 'entity') return `entity:${target.entity_id}`;
  return `facet:${target.entity_id}:${target.facet_key}`;
}

export function targetTitle(snapshot: AtlasExplorationSnapshot, target: AtlasTarget): string {
  if (target.kind === 'source') return snapshot.sources.find(s => s.source_id === target.source_id)?.title ?? 'Source';
  const entity = snapshot.entities.find(e => e.entity_id === target.entity_id);
  return target.kind === 'entity' ? entity?.title ?? 'Entity'
    : entity?.facets.find(f => f.key === target.facet_key)?.label ?? 'Facet';
}

export function evidenceSources(evidence: AtlasEvidence): string[] {
  if (evidence.status === 'sourced') return evidence.source_ids;
  if (evidence.status === 'conflicting') return [...evidence.supporting_source_ids, ...evidence.opposing_source_ids];
  return [];
}

export function targetClaims(snapshot: AtlasExplorationSnapshot, target: AtlasTarget) {
  return target.kind === 'source' ? [] : snapshot.claims.filter(c =>
    c.subject_entity_id === target.entity_id &&
    (target.kind === 'entity' || c.facet_key === target.facet_key));
}

export function targetRelationships(snapshot: AtlasExplorationSnapshot, target: AtlasTarget) {
  // The graph contract has no relationship facet key. Do not silently attribute
  // all entity relationships to every facet.
  return target.kind !== 'entity' ? [] : snapshot.relationships.filter(r =>
    r.from_entity_id === target.entity_id || r.to_entity_id === target.entity_id);
}

export function facetResearchComplete(snapshot: AtlasExplorationSnapshot, target: AtlasTarget): boolean {
  const ids = new Set(snapshot.occurrences.filter(o => targetKey(o.target) === targetKey(target)).map(o => o.occurrence_id));
  return snapshot.requests.some(r => r.status === 'completed' && r.parent_occurrence_id !== null && ids.has(r.parent_occurrence_id));
}

/** Project actual canonical graph data, never fixture occurrence templates. */
export function projectGraphColumns(snapshot: AtlasExplorationSnapshot) {
  const selected = getExplorationBreadcrumbs(snapshot);
  const rootPath = snapshot.paths.find(p => p.selected_occurrence_ids.length === 0);
  const parents: Array<AtlasOccurrence | null> = [null, ...selected];
  return parents.map(parent => {
    const target = parent?.target;
    let targets: AtlasTarget[] = [];
    if (!target) {
      // Initial occurrences define the server's taxonomy/facet result set. When
      // absent, expose the interpretation, not a made-up taxonomy.
      targets = snapshot.occurrences.filter(o => o.parent_occurrence_id === null).map(o => o.target);
      if (!targets.length) {
        const ids = snapshot.interpretation.kind === 'ambiguous'
          ? snapshot.interpretation.candidate_entity_ids : [snapshot.interpretation.subject_entity_id];
        targets = ids.filter(id => snapshot.entities.some(e => e.entity_id === id))
          .map(entity_id => ({ kind: 'entity', entity_id }));
      }
    } else if (target.kind !== 'source') {
      const entity = snapshot.entities.find(e => e.entity_id === target.entity_id);
      if (target.kind === 'entity') {
        targets.push(...(entity?.facets ?? []).map(f => ({
          kind: 'facet' as const, entity_id: target.entity_id, facet_key: f.key,
        })));
        targets.push(...targetRelationships(snapshot, target).map(r => ({
          kind: 'entity' as const,
          entity_id: r.from_entity_id === target.entity_id ? r.to_entity_id : r.from_entity_id,
        })));
      }
      const sourceIds = new Set([
        ...targetClaims(snapshot, target).flatMap(c => evidenceSources(c.evidence)),
        ...targetRelationships(snapshot, target).flatMap(r => evidenceSources(r.evidence)),
      ]);
      targets.push(...[...sourceIds].map(source_id => ({ kind: 'source' as const, source_id })));
      // Honor explicit result children, but match canonical identity so revisiting
      // the same entity through a different branch still discloses known data.
      const parentIds = new Set(snapshot.occurrences.filter(o => targetKey(o.target) === targetKey(target))
        .map(o => o.occurrence_id));
      targets.push(...snapshot.occurrences.filter(o =>
        o.parent_occurrence_id !== null && parentIds.has(o.parent_occurrence_id)).map(o => o.target));
    }
    const unique = [...new Map(targets.map(t => [targetKey(t), t])).values()];
    return {
      title: target ? targetTitle(snapshot, target) : 'Starting points',
      rows: unique.map(t => ({
        occurrence_id: `${parent?.occurrence_id ?? 'root'}:${targetKey(t)}`,
        path_id: parent?.path_id ?? rootPath?.path_id ?? '',
        parent_occurrence_id: parent?.occurrence_id ?? null,
        target: t,
      })),
    };
  });
}
