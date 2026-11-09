import { describe, expect, it } from 'vitest';
import type { AtlasEvidence, AtlasExplorationAction, AtlasExplorationSnapshot } from '../src';
import { computers, people } from './fixtures/explorations';

const ambiguous = {
  ...people,
  interpretation: { kind: 'ambiguous', candidate_entity_ids: ['a', 'b'] },
  entities: people.entities.map(entity => ({ ...entity, identity_status: 'candidate' })),
  claims: [], relationships: [], occurrences: [], paths: [], active_path_id: null,
  requests: [], actions: [],
} satisfies AtlasExplorationSnapshot;

if (false) {
  // @ts-expect-error The new contract cannot masquerade as a v1 layer.
  const wrongVersion: AtlasExplorationSnapshot = { ...computers, schema_version: 'atlas-layer.v1' };
  // @ts-expect-error Sourced claims require at least one source reference.
  const noSource: AtlasEvidence = { status: 'sourced', source_ids: [] };
  // @ts-expect-error External actions cannot bypass approval.
  const unsafeAction: AtlasExplorationAction = { ...people.actions[0], requires_approval: false };
  // @ts-expect-error Conflicting claims must reference both sides.
  const oneSided: AtlasEvidence = { status: 'conflicting', supporting_source_ids: ['synthetic'] };
  void [wrongVersion, noSource, unsafeAction, oneSided];
}

describe('proposed Atlas exploration contracts (not runtime validation)', () => {
  it('represents categories, adaptive facets, and preserved sibling selections', () => {
    expect(computers.entities.filter(entity => entity.kind === 'category')).toHaveLength(3);
    expect(computers.occurrences[1].target).toEqual({ kind: 'facet', entity_id: 'pc', facet_key: 'history' });
    expect(computers.paths.map(path => path.selected_occurrence_ids)).toEqual([
      ['pc-row', 'history-row'], ['server-row'],
    ]);
  });

  it('reuses entity identity without inventing a direct person relationship', () => {
    const occurrences = people.occurrences.filter(row => row.target.entity_id === 'b');
    expect(occurrences.map(row => row.occurrence_id)).toEqual(['b-via-org', 'b-direct']);
    expect(people.entities.filter(entity => entity.entity_id === 'b')).toHaveLength(1);
    expect(people.relationships.map(edge => [edge.from_entity_id, edge.to_entity_id]))
      .toEqual([['a', 'org'], ['b', 'org']]);
    expect(people.sources.every(source => source.trust === 'unverified')).toBe(true);
  });

  it('represents ambiguity, unavailable facets, conflicting evidence, and execution identity', () => {
    expect(ambiguous.interpretation.candidate_entity_ids).toEqual(['a', 'b']);
    expect(ambiguous.claims).toEqual([]);
    expect(people.entities[0].facets[1].status).toBe('unavailable');
    expect(people.claims[0].evidence.status).toBe('conflicting');
    expect(people.requests[0]).toMatchObject({
      exploration_id: 'people', path_id: 'career', request_id: 'request-career',
      run_id: 'run-career', thread_id: 'thread-career', committed_history_version: 7,
    });
    expect(people.actions[0].requires_approval).toBe(true);
    expect({ status: 'unsupported', reason: 'No reliable source' } satisfies AtlasEvidence)
      .toHaveProperty('reason', 'No reliable source');
  });
});
