import { describe, expect, it } from 'vitest';
import { getExplorationBreadcrumbs, selectExplorationTarget, type AtlasTarget } from '@haas/atlas-explorer-client';
import { scopedDive } from '../src/scopedDive';
import { testData } from './workspaceTestData';

const entity = { kind: 'entity', entity_id: 'subject' } as const;
function selected(target: AtlasTarget = entity) {
  return selectExplorationTarget(testData('exact original question').snapshot, target, null);
}
describe('one scoped dive per selected ancestry', () => {
  it('researches every new entity/category selection using the exact user query', () => {
    const snapshot = selected();
    expect(scopedDive(snapshot, entity)).toEqual({ kind: 'research', question: 'exact original question', identityFirst: false });
    snapshot.entities[0].kind = 'category';
    expect(scopedDive(snapshot, entity).kind).toBe('research');
  });
  it('resolves candidate identities first and blocks their facets', () => {
    const snapshot = selected();
    snapshot.entities[0].identity_status = 'candidate';
    snapshot.interpretation = { kind: 'ambiguous', candidate_entity_ids: ['subject'] };
    snapshot.claims = [];
    snapshot.relationships = [];
    expect(scopedDive(snapshot, entity)).toMatchObject({ kind: 'research', identityFirst: true });
    expect(scopedDive(snapshot, { kind: 'facet', entity_id: 'subject', facet_key: 'known' }))
      .toMatchObject({ kind: 'unavailable', reason: expect.stringContaining('candidate identity') });
  });
  it('opens evidence and unavailable reasons without a dive', () => {
    const snapshot = selected();
    expect(scopedDive(snapshot, { kind: 'source', source_id: 'source' })).toEqual({ kind: 'evidence' });
    snapshot.entities[0].facets[0] = { key: 'known', label: 'Details', scope: 'Details',
      status: 'unavailable', reason: 'Provider does not publish this information' };
    expect(scopedDive(snapshot, { kind: 'facet', entity_id: 'subject', facet_key: 'known' }))
      .toEqual({ kind: 'unavailable', reason: 'Provider does not publish this information' });
  });
  it('uses only completed research for this exact selected occurrence as a cache hit', () => {
    const snapshot = selected();
    const selectedRow = getExplorationBreadcrumbs(snapshot)[0];
    snapshot.requests.push({ ...snapshot.requests[0], request_id: 'child',
      path_id: snapshot.active_path_id!, parent_occurrence_id: selectedRow.occurrence_id });
    expect(scopedDive(snapshot, entity)).toEqual({ kind: 'cached' });
    snapshot.requests[1] = { ...snapshot.requests[0], request_id: 'child' };
    expect(scopedDive(snapshot, entity).kind).toBe('research');
    snapshot.requests[1].parent_occurrence_id = selectedRow.occurrence_id;
    snapshot.requests[1].path_id = snapshot.active_path_id!;
    snapshot.requests[1].status = 'failed';
    expect(scopedDive(snapshot, entity).kind).toBe('research');
  });
});
