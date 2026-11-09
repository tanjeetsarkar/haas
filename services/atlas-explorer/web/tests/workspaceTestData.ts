import type { AtlasWorkspaceAdapter, AtlasWorkspaceData } from '../src/explorationAdapter';
import { vi } from 'vitest';

export function testData(title = 'Research subject', runId = 'run-1'): AtlasWorkspaceData {
  return {
    runId,
    snapshot: {
      schema_version: 'atlas-exploration.v2', exploration_id: `exploration-${runId}`, revision: 1, root_query: title,
      interpretation: { kind: 'resolved', subject_entity_id: 'subject' },
      entities: [
        { entity_id: 'subject', kind: 'product', title, summary: 'A reported research subject.', identity_status: 'resolved',
          external_references: [{ namespace: 'catalog', value: 'public-123' }],
          facets: [
            { key: 'known', label: 'Published details', scope: 'Reported details', status: 'known' },
            { key: 'unknown', label: 'Open question', scope: 'Research the open question for this subject', status: 'research_needed' },
          ] },
        { entity_id: 'person', kind: 'person', title: 'Specialist', summary: 'Public professional profile',
          identity_status: 'resolved', external_references: [], facets: [
            { key: 'credentials', label: 'Credentials', scope: 'Public qualifications', status: 'known' },
          ] },
      ],
      sources: [{ source_id: 'source', title: 'Research report', url: 'https://research.test/report', publisher: 'Publisher',
        retrieved_at: '2026-11-08T10:00:00Z', excerpt: 'A published excerpt.', trust: 'third_party' }],
      claims: [{ claim_id: 'claim', subject_entity_id: 'subject', facet_key: 'known', text: 'Reported research finding.',
        observed_at: '2026-11-08T10:00:00Z', evidence: { status: 'sourced', source_ids: ['source'] },
        details: { attributes: [
          { key: 'price', label: 'Published price', value: '100–200', unit: 'USD / session', uncertainty: 'Estimated range; verify with provider' },
        ], checklist: [{ id: 'check', text: 'Confirm eligibility', required: true }], draft: { title: 'Inquiry draft', body: 'Please confirm availability.' } } },
      { claim_id: 'credentials', subject_entity_id: 'person', text: 'Public professional information.',
        observed_at: '2026-11-08T10:00:00Z', evidence: { status: 'unsupported', reason: 'Requires independent verification' },
        details: { attributes: [
          { key: 'specialties', label: 'Specialties', value: ['Research', 'Analysis'] },
          { key: 'credentials', label: 'Credentials', value: 'Public qualification' },
        ] } }],
      relationships: [{ relationship_id: 'connection', from_entity_id: 'subject', to_entity_id: 'person',
        label: 'consulted', verification: 'suggested', evidence: { status: 'unsupported', reason: 'Unconfirmed connection' } }],
      occurrences: [{ occurrence_id: 'initial', path_id: 'root', parent_occurrence_id: null, target: { kind: 'entity', entity_id: 'subject' } }],
      paths: [{ path_id: 'root', parent_path_id: null, selected_occurrence_ids: [] }], active_path_id: 'root',
      requests: [{ request_id: 'request', exploration_id: `exploration-${runId}`, path_id: 'root', parent_occurrence_id: null,
        run_id: runId, thread_id: 'thread', committed_history_version: 1, last_sequence: 4, status: 'completed' }],
      actions: [],
    },
  };
}

export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

export function testAdapter(): AtlasWorkspaceAdapter {
  return {
    research: vi.fn(() => ({ result: Promise.resolve(testData()), cancel: vi.fn(async () => undefined) })),
    save: vi.fn(async data => ({ artifactId: 'artifact-1', runId: data.runId })),
    load: vi.fn(async () => testData('Loaded subject', 'saved-run')),
    recent: vi.fn(async () => []),
    decide: vi.fn(async (approval, decision) => ({ ...approval, status: decision === 'approve' ? 'approved' : 'rejected' })),
  };
}
