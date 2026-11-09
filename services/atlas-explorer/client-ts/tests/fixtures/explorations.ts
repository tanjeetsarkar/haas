import type { AtlasEntity, AtlasExplorationSnapshot } from '../../src';

const entity = (entity_id: string, title: string, kind: AtlasEntity['kind']): AtlasEntity => ({
  entity_id, title, kind, summary: 'Synthetic contract example, not researched information.',
  identity_status: 'resolved', external_references: [],
  facets: [{ key: 'history', label: 'History', scope: title, status: 'research_needed' }],
});

export const computers = {
  schema_version: 'atlas-exploration.v2', exploration_id: 'computers', revision: 1,
  root_query: 'computers', interpretation: { kind: 'resolved', subject_entity_id: 'computing' },
  entities: [entity('computing', 'Computers', 'concept'),
    entity('pc', 'Personal computers', 'category'), entity('server', 'Servers', 'category'),
    entity('supercomputer', 'Supercomputers', 'category')],
  sources: [], claims: [], relationships: [], actions: [], requests: [],
  occurrences: [
    { occurrence_id: 'pc-row', path_id: 'pc-path', parent_occurrence_id: null,
      target: { kind: 'entity', entity_id: 'pc' } },
    { occurrence_id: 'history-row', path_id: 'pc-path', parent_occurrence_id: 'pc-row',
      target: { kind: 'facet', entity_id: 'pc', facet_key: 'history' } },
    { occurrence_id: 'server-row', path_id: 'server-path', parent_occurrence_id: null,
      target: { kind: 'entity', entity_id: 'server' } },
  ],
  paths: [
    { path_id: 'pc-path', parent_path_id: null, selected_occurrence_ids: ['pc-row', 'history-row'] },
    { path_id: 'server-path', parent_path_id: null, selected_occurrence_ids: ['server-row'] },
  ],
  active_path_id: 'pc-path',
} satisfies AtlasExplorationSnapshot;

export const people = {
  ...computers, exploration_id: 'people', root_query: 'Fictional Person A',
  interpretation: { kind: 'resolved', subject_entity_id: 'a' },
  entities: [
    { ...entity('a', 'Fictional Person A', 'person'), facets: [
      { key: 'career', label: 'Career', scope: 'Public professional history', status: 'known' },
      { key: 'disclosed_finances', label: 'Disclosed financial facts', scope: 'Public disclosures only',
        status: 'unavailable', reason: 'No reliable public information found' },
    ] },
    entity('org', 'Fictional Organization X', 'organization'),
    entity('b', 'Fictional Person B', 'person'),
  ],
  sources: [{
    source_id: 'synthetic', url: 'https://example.test/fictional-history',
    title: 'Synthetic fixture; never fetched', publisher: 'Contract test',
    retrieved_at: '2026-01-01T00:00:00Z', excerpt: 'Fictional role records.', trust: 'unverified',
  }, {
    source_id: 'counterexample', url: 'https://example.test/fictional-correction',
    title: 'Synthetic conflicting record; never fetched', publisher: 'Contract test',
    retrieved_at: '2026-01-01T00:00:00Z', excerpt: 'Fictional differing dates.', trust: 'unverified',
  }],
  claims: [{
    claim_id: 'role-date', subject_entity_id: 'a', text: 'Fictional role dates disagree.',
    observed_at: '2026-01-01T00:00:00Z',
    evidence: { status: 'conflicting', supporting_source_ids: ['synthetic'], opposing_source_ids: ['counterexample'] },
  }],
  relationships: [
    { relationship_id: 'a-org', from_entity_id: 'a', to_entity_id: 'org',
      label: 'held role at', verification: 'suggested', evidence: { status: 'sourced', source_ids: ['synthetic'] } },
    { relationship_id: 'b-org', from_entity_id: 'b', to_entity_id: 'org',
      label: 'held role at', verification: 'suggested', evidence: { status: 'sourced', source_ids: ['synthetic'] } },
  ],
  occurrences: [
    { occurrence_id: 'a-row', path_id: 'career', parent_occurrence_id: null, target: { kind: 'entity', entity_id: 'a' } },
    { occurrence_id: 'org-row', path_id: 'career', parent_occurrence_id: 'a-row', target: { kind: 'entity', entity_id: 'org' } },
    { occurrence_id: 'b-via-org', path_id: 'career', parent_occurrence_id: 'org-row', target: { kind: 'entity', entity_id: 'b' } },
    { occurrence_id: 'b-direct', path_id: 'other', parent_occurrence_id: null, target: { kind: 'entity', entity_id: 'b' } },
  ],
  paths: [
    { path_id: 'career', parent_path_id: null, selected_occurrence_ids: ['a-row', 'org-row', 'b-via-org'] },
    { path_id: 'other', parent_path_id: null, selected_occurrence_ids: ['b-direct'] },
  ],
  active_path_id: 'career',
  requests: [{
    request_id: 'request-career', exploration_id: 'people', path_id: 'career', parent_occurrence_id: 'a-row',
    run_id: 'run-career', thread_id: 'thread-career', committed_history_version: 7,
    last_sequence: 12, status: 'completed',
  }],
  actions: [{
    action_id: 'inquiry', label: 'Draft a fictional inquiry', target: { kind: 'entity', entity_id: 'org' },
    kind: 'external', tool_name: 'atlas_dispatch_inquiry', requires_approval: true,
  }],
} satisfies AtlasExplorationSnapshot;
