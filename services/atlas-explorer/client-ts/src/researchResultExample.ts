import type { AtlasResearchResult } from './explorationResearch';

/** Synthetic format example only. Never a source of facts or fallback research data. */
const referenceExamples = [
  { kind: 'entity', entity_id: 'road' },
  { kind: 'facet', entity_id: 'road', facet_key: 'overview' },
  { kind: 'source', source_id: 'synthetic-format-source' },
] satisfies AtlasResearchResult['targets'];

export const researchResultExample: AtlasResearchResult = {
  interpretation: { kind: 'resolved', subject_entity_id: 'transport' },
  entities: [
    {
      entity_id: 'transport', kind: 'concept', title: 'Transport', summary: '',
      identity_status: 'resolved', external_references: [], facets: [],
    },
    ...[
      ['road', 'Road transport'], ['rail', 'Rail transport'], ['water', 'Water transport'],
    ].map(([entity_id, title]) => ({
      entity_id, kind: 'category' as const, title, summary: '',
      identity_status: 'resolved' as const, external_references: [],
      facets: [
        { key: 'overview', label: 'Overview', scope: `Public overview of ${title}`,
          status: 'known' as const },
        { key: 'history', label: 'History', scope: `Development of ${title}`,
          status: 'research_needed' as const },
        { key: 'pricing', label: 'Published pricing', scope: `Published fares for ${title}`,
          status: 'unavailable' as const, reason: 'No pricing observed in the example source' },
      ],
    })),
  ],
  sources: [{
    source_id: 'synthetic-format-source', url: 'https://example.test/format-only',
    title: 'Synthetic format example, not researched', publisher: 'Example only',
    retrieved_at: '2026-01-01T00:00:00Z',
    excerpt: 'Example taxonomy: road transport, rail transport and water transport.',
    trust: 'unverified',
  }],
  claims: ['road', 'rail', 'water'].map(id => ({
    claim_id: `type-${id}`, subject_entity_id: id,
    text: `${id} transport is an example entry point in this synthetic taxonomy.`,
    observed_at: '2026-01-01T00:00:00Z',
    evidence: { status: 'sourced', source_ids: ['synthetic-format-source'] },
  })),
  relationships: [],
  targets: referenceExamples,
};
