import { describe, expect, it } from 'vitest';
import { selectExplorationTarget } from '@haas/atlas-explorer-client';
import { projectGraphColumns, targetClaims, targetRelationships } from '../src/graphProjection';
import { testData } from './workspaceTestData';

describe('Canonical live graph projection', () => {
  it('discloses the same entity facets when reached via a connection with no occurrence template', () => {
    let snapshot = testData().snapshot;
    snapshot = selectExplorationTarget(snapshot, { kind: 'entity', entity_id: 'subject' }, null);
    const parent = snapshot.paths.find(p => p.path_id === snapshot.active_path_id)!.selected_occurrence_ids[0];
    snapshot = selectExplorationTarget(snapshot, { kind: 'entity', entity_id: 'person' }, parent);
    const columns = projectGraphColumns(snapshot);
    expect(columns[2].rows.map(r => r.target)).toContainEqual({ kind: 'facet', entity_id: 'person', facet_key: 'credentials' });
    expect(columns[1].rows.map(r => r.target)).toContainEqual({ kind: 'entity', entity_id: 'person' });
  });

  it('keeps same-name entities distinct and never infers edges from shared labels', () => {
    let snapshot = testData().snapshot;
    snapshot.entities[1].title = snapshot.entities[0].title;
    snapshot = selectExplorationTarget(snapshot, { kind: 'entity', entity_id: 'subject' }, null);
    expect(projectGraphColumns(snapshot)[1].rows.filter(r => r.target.kind === 'entity')
      .map(r => r.target)).toEqual([{ kind: 'entity', entity_id: 'person' }]);
    snapshot.relationships = [];
    expect(projectGraphColumns(snapshot)[1].rows.filter(r => r.target.kind === 'entity')).toEqual([]);
  });

  it('does not treat entity-wide relationship sources as provenance for every facet', () => {
    const snapshot = testData().snapshot;
    expect(targetRelationships(snapshot, { kind: 'entity', entity_id: 'subject' })).toHaveLength(1);
    expect(targetRelationships(snapshot, { kind: 'facet', entity_id: 'subject', facet_key: 'known' })).toEqual([]);
  });

  it('keeps unscoped claims and their sources at the entity rather than inheriting them into facets', () => {
    let snapshot = testData().snapshot;
    snapshot.sources.push({ ...snapshot.sources[0], source_id: 'entity-source', title: 'Entity-only source' });
    snapshot.claims.push({ ...snapshot.claims[0], claim_id: 'entity-claim', facet_key: undefined,
      text: 'Entity-wide finding', evidence: { status: 'sourced', source_ids: ['entity-source'] } });
    const entity = { kind: 'entity' as const, entity_id: 'subject' };
    const facet = { kind: 'facet' as const, entity_id: 'subject', facet_key: 'known' };
    expect(targetClaims(snapshot, entity).map(c => c.claim_id)).toContain('entity-claim');
    expect(targetClaims(snapshot, facet).map(c => c.claim_id)).toEqual(['claim']);
    expect(targetClaims(snapshot, { ...facet, facet_key: 'unknown' })).toEqual([]);
    snapshot = selectExplorationTarget(snapshot, entity, null);
    const parent = snapshot.paths.find(p => p.path_id === snapshot.active_path_id)!.selected_occurrence_ids[0];
    expect(projectGraphColumns(snapshot)[1].rows.map(r => r.target))
      .toContainEqual({ kind: 'source', source_id: 'entity-source' });
    snapshot = selectExplorationTarget(snapshot, facet, parent);
    expect(projectGraphColumns(snapshot)[2].rows.map(r => r.target))
      .toEqual([{ kind: 'source', source_id: 'source' }]);
  });
});
