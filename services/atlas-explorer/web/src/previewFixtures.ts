import { parseExplorationSnapshot, type AtlasExplorationSnapshot } from '@haas/atlas-explorer-client';

const observed = '2026-01-15T12:00:00Z';

function occurrence(occurrence_id: string, target: AtlasExplorationSnapshot['occurrences'][number]['target'], parent_occurrence_id: string | null = null) {
  return { occurrence_id, path_id: 'sample-path', parent_occurrence_id, target };
}

const computers: AtlasExplorationSnapshot = {
  schema_version: 'atlas-exploration.v2',
  exploration_id: 'fixture-computers',
  revision: 1,
  root_query: 'computers',
  interpretation: { kind: 'resolved', subject_entity_id: 'computers' },
  entities: [
    { entity_id: 'computers', kind: 'concept', title: 'Computers', summary: 'A broad family of programmable machines.', identity_status: 'resolved', external_references: [], facets: [] },
    { entity_id: 'personal-computers', kind: 'category', title: 'Personal computers', summary: 'Computers designed primarily for individual use.', identity_status: 'resolved', external_references: [], facets: [
      { key: 'architecture', label: 'Architecture', scope: 'Core components and design', status: 'known' },
      { key: 'uses', label: 'Common uses', scope: 'Tasks and everyday contexts', status: 'known' },
      { key: 'security', label: 'Security', scope: 'Risks and safeguards', status: 'research_needed' },
      { key: 'pricing', label: 'Pricing', scope: 'Current market pricing', status: 'unavailable', reason: 'No current pricing research is included in this local sample.' },
    ] },
    { entity_id: 'servers', kind: 'category', title: 'Servers', summary: 'Systems that provide services or resources to other computers.', identity_status: 'resolved', external_references: [], facets: [
      { key: 'roles', label: 'Roles', scope: 'Typical server workloads', status: 'known' },
      { key: 'energy', label: 'Energy use', scope: 'Power and efficiency claims', status: 'known' },
    ] },
    { entity_id: 'supercomputers', kind: 'category', title: 'Supercomputers', summary: 'High-performance systems used for demanding computation.', identity_status: 'resolved', external_references: [], facets: [
      { key: 'applications', label: 'Applications', scope: 'Representative computational work', status: 'known' },
    ] },
  ],
  sources: [
    { source_id: 'pc-source', url: 'https://example.test/computers/personal', title: 'Personal computers — sample reference', publisher: 'Atlas fictional fixture', retrieved_at: observed, excerpt: 'A synthetic example of a reference excerpt. This is not a real source.', trust: 'unverified' },
    { source_id: 'server-source-a', url: 'https://example.test/computers/server-a', title: 'Server energy note A — sample', publisher: 'Atlas fictional fixture', retrieved_at: observed, excerpt: 'Synthetic note suggesting server energy varies by workload.', trust: 'unverified' },
    { source_id: 'server-source-b', url: 'https://example.test/computers/server-b', title: 'Server energy note B — sample', publisher: 'Atlas fictional fixture', retrieved_at: observed, excerpt: 'A conflicting synthetic note uses a different measurement boundary.', trust: 'unverified' },
    { source_id: 'super-source', url: 'https://example.test/computers/supercomputers', title: 'Supercomputing — sample reference', publisher: 'Atlas fictional fixture', retrieved_at: observed, excerpt: 'A synthetic example mentioning large-scale scientific computation.', trust: 'unverified' },
  ],
  claims: [
    { claim_id: 'pc-claim', subject_entity_id: 'personal-computers', facet_key: 'architecture', text: 'Personal computers commonly combine a processor, memory, storage, and user-facing input/output.', observed_at: observed, evidence: { status: 'sourced', source_ids: ['pc-source'] } },
    { claim_id: 'server-claim-a', subject_entity_id: 'servers', facet_key: 'energy', text: 'A server energy estimate depends on workload and measurement scope.', observed_at: observed, evidence: { status: 'conflicting', supporting_source_ids: ['server-source-a'], opposing_source_ids: ['server-source-b'] } },
    { claim_id: 'super-claim', subject_entity_id: 'supercomputers', facet_key: 'applications', text: 'Large-scale scientific simulation is a common example of high-performance computing.', observed_at: observed, evidence: { status: 'sourced', source_ids: ['super-source'] } },
  ],
  relationships: [],
  occurrences: [
    occurrence('root-personal', { kind: 'entity', entity_id: 'personal-computers' }),
    occurrence('root-servers', { kind: 'entity', entity_id: 'servers' }),
    occurrence('root-supercomputers', { kind: 'entity', entity_id: 'supercomputers' }),
    occurrence('pc-architecture', { kind: 'facet', entity_id: 'personal-computers', facet_key: 'architecture' }, 'root-personal'),
    occurrence('pc-uses', { kind: 'facet', entity_id: 'personal-computers', facet_key: 'uses' }, 'root-personal'),
    occurrence('pc-security', { kind: 'facet', entity_id: 'personal-computers', facet_key: 'security' }, 'root-personal'),
    occurrence('pc-pricing', { kind: 'facet', entity_id: 'personal-computers', facet_key: 'pricing' }, 'root-personal'),
    occurrence('pc-source-occurrence', { kind: 'source', source_id: 'pc-source' }, 'pc-architecture'),
    occurrence('server-roles', { kind: 'facet', entity_id: 'servers', facet_key: 'roles' }, 'root-servers'),
    occurrence('server-energy', { kind: 'facet', entity_id: 'servers', facet_key: 'energy' }, 'root-servers'),
    occurrence('server-source-occurrence', { kind: 'source', source_id: 'server-source-a' }, 'server-energy'),
    occurrence('super-applications', { kind: 'facet', entity_id: 'supercomputers', facet_key: 'applications' }, 'root-supercomputers'),
    occurrence('super-source-occurrence', { kind: 'source', source_id: 'super-source' }, 'super-applications'),
  ],
  paths: [{ path_id: 'sample-path', parent_path_id: null, selected_occurrence_ids: [] }],
  active_path_id: 'sample-path',
  requests: [],
  actions: [{ action_id: 'external-research', label: 'Request live research', target: { kind: 'entity', entity_id: 'computers' }, kind: 'external', tool_name: 'atlas.research', requires_approval: true }],
};

const people: AtlasExplorationSnapshot = {
  schema_version: 'atlas-exploration.v2',
  exploration_id: 'fixture-people',
  revision: 1,
  root_query: 'Avery Rowan',
  interpretation: { kind: 'resolved', subject_entity_id: 'avery-rowan' },
  entities: [
    { entity_id: 'avery-rowan', kind: 'person', title: 'Avery Rowan', summary: 'A fictional designer in this local preview.', identity_status: 'resolved', external_references: [], facets: [
      { key: 'career', label: 'Career', scope: 'Fictional professional milestones', status: 'known' },
      { key: 'organizations', label: 'Organizations', scope: 'Fictional affiliations', status: 'known' },
      { key: 'collaborators', label: 'Collaborators', scope: 'Fictional professional connections', status: 'known' },
      { key: 'biography', label: 'Biography', scope: 'Personal biography', status: 'unavailable', reason: 'This sample avoids personal-life claims.' },
    ] },
    { entity_id: 'northstar-lab', kind: 'organization', title: 'Northstar Lab', summary: 'A fictional research studio.', identity_status: 'resolved', external_references: [], facets: [
      { key: 'work', label: 'Work', scope: 'Fictional projects', status: 'known' },
    ] },
    { entity_id: 'morgan-vale', kind: 'person', title: 'Morgan Vale', summary: 'A fictional engineer connected to Avery Rowan and Northstar Lab.', identity_status: 'resolved', external_references: [], facets: [
      { key: 'career', label: 'Career', scope: 'Fictional professional milestones', status: 'known' },
    ] },
  ],
  sources: [
    { source_id: 'fiction-profile', url: 'https://example.test/people/avery-rowan', title: 'Avery Rowan — fictional profile', publisher: 'Atlas fictional fixture', retrieved_at: observed, excerpt: 'Every person and detail in this sample is invented.', trust: 'unverified' },
    { source_id: 'fiction-project', url: 'https://example.test/people/northstar', title: 'Northstar Lab — fictional project note', publisher: 'Atlas fictional fixture', retrieved_at: observed, excerpt: 'A synthetic note describing a fictional collaboration.', trust: 'unverified' },
  ],
  claims: [
    { claim_id: 'avery-career', subject_entity_id: 'avery-rowan', facet_key: 'career', text: 'Avery Rowan joined the fictional Northstar Lab in 2022.', observed_at: observed, evidence: { status: 'sourced', source_ids: ['fiction-profile'] } },
    { claim_id: 'morgan-role', subject_entity_id: 'morgan-vale', facet_key: 'career', text: 'Morgan Vale worked on a fictional open-tools project.', observed_at: observed, evidence: { status: 'unsupported', reason: 'Synthetic fixture detail; no independent source is asserted.' } },
  ],
  relationships: [
    { relationship_id: 'avery-at-northstar', from_entity_id: 'avery-rowan', to_entity_id: 'northstar-lab', label: 'joined', verification: 'confirmed', evidence: { status: 'sourced', source_ids: ['fiction-profile'] } },
    { relationship_id: 'morgan-at-northstar', from_entity_id: 'morgan-vale', to_entity_id: 'northstar-lab', label: 'collaborated with', verification: 'suggested', evidence: { status: 'sourced', source_ids: ['fiction-project'] } },
    { relationship_id: 'avery-morgan', from_entity_id: 'avery-rowan', to_entity_id: 'morgan-vale', label: 'worked alongside', verification: 'suggested', evidence: { status: 'sourced', source_ids: ['fiction-project'] } },
  ],
  occurrences: [
    occurrence('root-avery', { kind: 'entity', entity_id: 'avery-rowan' }),
    occurrence('avery-career-occ', { kind: 'facet', entity_id: 'avery-rowan', facet_key: 'career' }, 'root-avery'),
    occurrence('avery-orgs-occ', { kind: 'facet', entity_id: 'avery-rowan', facet_key: 'organizations' }, 'root-avery'),
    occurrence('avery-collabs-occ', { kind: 'facet', entity_id: 'avery-rowan', facet_key: 'collaborators' }, 'root-avery'),
    occurrence('avery-bio-occ', { kind: 'facet', entity_id: 'avery-rowan', facet_key: 'biography' }, 'root-avery'),
    occurrence('northstar-occ', { kind: 'entity', entity_id: 'northstar-lab' }, 'avery-orgs-occ'),
    occurrence('morgan-from-org', { kind: 'entity', entity_id: 'morgan-vale' }, 'northstar-occ'),
    occurrence('morgan-from-avery', { kind: 'entity', entity_id: 'morgan-vale' }, 'avery-collabs-occ'),
    occurrence('northstar-work', { kind: 'facet', entity_id: 'northstar-lab', facet_key: 'work' }, 'northstar-occ'),
    occurrence('northstar-source', { kind: 'source', source_id: 'fiction-project' }, 'northstar-work'),
    occurrence('avery-source', { kind: 'source', source_id: 'fiction-profile' }, 'avery-career-occ'),
    occurrence('morgan-career', { kind: 'facet', entity_id: 'morgan-vale', facet_key: 'career' }, 'morgan-from-org'),
  ],
  paths: [{ path_id: 'sample-path', parent_path_id: null, selected_occurrence_ids: [] }],
  active_path_id: 'sample-path',
  requests: [],
  actions: [{ action_id: 'external-person-action', label: 'Contact this person', target: { kind: 'entity', entity_id: 'avery-rowan' }, kind: 'external', tool_name: 'atlas.contact', requires_approval: true }],
};

export const SAMPLE_SNAPSHOTS = {
  computers: parseExplorationSnapshot(computers),
  people: parseExplorationSnapshot(people),
} satisfies Record<string, AtlasExplorationSnapshot>;

export type SampleKey = keyof typeof SAMPLE_SNAPSHOTS;

/** Fixture presentation metadata: relationships have no facet_key in the contract.
 * Explicit editorial scope avoids implying every connection supports every facet.
 */
export const FIXTURE_FACET_RELATIONSHIPS: Record<string, Record<string, string[]>> = {
  'avery-rowan': {
    career: ['avery-at-northstar'],
    organizations: ['avery-at-northstar'],
    collaborators: ['avery-morgan'],
  },
  'northstar-lab': { work: ['morgan-at-northstar'] },
  'morgan-vale': { career: ['morgan-at-northstar', 'avery-morgan'] },
};
