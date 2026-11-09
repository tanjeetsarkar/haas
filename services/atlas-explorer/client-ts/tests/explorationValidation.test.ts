import { describe, expect, it } from 'vitest';
import { acceptsExplorationUpdate, parseExplorationSnapshot } from '../src';
import type { AtlasExplorationRequest, AtlasExplorationSnapshot } from '../src';
import { computers, people } from './fixtures/explorations';
import cases from './fixtures/validationCases.json';

const clone = (): AtlasExplorationSnapshot => structuredClone(people);

describe('authoritative v2 schema and semantics', () => {
  it.each([...cases.valid, computers, people])('accepts fixture $exploration_id', value => {
    expect(parseExplorationSnapshot(value)).toBe(value);
  });
  it.each(cases.invalidPatches)('rejects shared structural patch %#', patch => {
    expect(() => parseExplorationSnapshot({ ...cases.valid[0], ...patch })).toThrow(/Invalid Atlas exploration/);
  });
  it.each([null, [], {}, 'not JSON', { ...people, revision: NaN }])('rejects unknown %#', value => {
    expect(() => parseExplorationSnapshot(value)).toThrow(/schema/);
  });
  const invalid: Array<[string, (s: AtlasExplorationSnapshot) => void]> = [
    ['duplicate entity', s => { s.entities.push(s.entities[0]); }],
    ['duplicate occurrence', s => { s.occurrences.push(s.occurrences[0]); }],
    ['duplicate path', s => { s.paths.push(s.paths[0]); }],
    ['duplicate claim', s => { s.claims.push(s.claims[0]); }],
    ['duplicate source', s => { s.sources.push(s.sources[0]); }],
    ['duplicate action', s => { s.actions.push(s.actions[0]); }],
    ['duplicate request', s => { s.requests.push(s.requests[0]); }],
    ['duplicate relationship', s => { s.relationships.push(s.relationships[0]); }],
    ['duplicate facet', s => { s.entities[0].facets.push(s.entities[0].facets[0]); }],
    ['subject ref', s => { s.claims[0].subject_entity_id = 'missing'; }],
    ['claim facet', s => { s.claims[0].facet_key = 'missing'; }],
    ['relationship ref', s => { s.relationships[0].to_entity_id = 'missing'; }],
    ['evidence ref', s => { s.claims[0].evidence = { status: 'sourced', source_ids: ['missing'] }; }],
    ['conflicting ref', s => { s.claims[0].evidence = { status: 'conflicting', supporting_source_ids: ['synthetic'], opposing_source_ids: ['missing'] }; }],
    ['confirmed unsupported', s => { s.relationships[0].verification = 'confirmed'; s.relationships[0].evidence = { status: 'unsupported', reason: 'none' }; }],
    ['confirmed conflicting', s => { s.relationships[0].verification = 'confirmed'; s.relationships[0].evidence = s.claims[0].evidence; }],
    ['candidate claim', s => { s.entities[0].identity_status = 'candidate'; }],
    ['candidate relationship', s => { s.entities[2].identity_status = 'candidate'; }],
    ['candidate action', s => { s.entities[1].identity_status = 'candidate'; s.relationships = []; }],
    ['action facet', s => { s.actions[0].target = { kind: 'facet', entity_id: 'a', facet_key: 'missing' }; }],
    ['action source', s => { s.actions[0].target = { kind: 'source', source_id: 'missing' }; }],
    ['occurrence entity', s => { s.occurrences[0].target = { kind: 'entity', entity_id: 'missing' }; }],
    ['occurrence facet', s => { s.occurrences[0].target = { kind: 'facet', entity_id: 'a', facet_key: 'missing' }; }],
    ['occurrence source', s => { s.occurrences[0].target = { kind: 'source', source_id: 'missing' }; }],
    ['active path', s => { s.active_path_id = 'missing'; }],
    ['occurrence path', s => { s.occurrences[0].path_id = 'missing'; }],
    ['missing parent', s => { s.occurrences[0].parent_occurrence_id = 'missing'; }],
    ['occurrence cycle', s => { s.occurrences[3].parent_occurrence_id = 'b-direct'; }],
    ['path cycle', s => { s.paths[0].parent_path_id = 'other'; s.paths[1].parent_path_id = 'career'; }],
    ['missing ancestor', s => { s.paths[0].parent_path_id = 'missing'; }],
    ['selected ref', s => { s.paths[0].selected_occurrence_ids.push('missing'); }],
    ['broken chain', s => { s.paths[0].selected_occurrence_ids.reverse(); }],
    ['cross branch selection', s => { s.paths[1].selected_occurrence_ids = ['a-row']; }],
    ['cross branch parent', s => { s.occurrences[3].parent_occurrence_id = 'a-row'; }],
    ['request exploration', s => { s.requests[0].exploration_id = 'elsewhere'; }],
    ['request path', s => { s.requests[0].path_id = 'missing'; }],
    ['request parent', s => { s.requests[0].parent_occurrence_id = 'b-direct'; }],
    ['ambiguous ref', s => { s.interpretation = { kind: 'ambiguous', candidate_entity_ids: ['missing'] }; }],
    ['resolved candidate', s => { s.claims = []; s.relationships = []; s.entities[0].identity_status = 'candidate'; }],
    ['approval bypass', s => { Object.assign(s.actions[0], { requires_approval: false }); }],
    ['bad observed date', s => { s.claims[0].observed_at = 'yesterday'; }],
    ['bad history number', s => { s.requests[0].committed_history_version = 1.5; }],
    ['bad sequence', s => { s.requests[0].last_sequence = -1; }],
  ];
  it.each(invalid)('rejects %s', (_, mutate) => {
    const s = clone(); mutate(s);
    expect(() => parseExplorationSnapshot(s)).toThrow(/Invalid Atlas exploration/);
  });
  it.each(['javascript:alert(1)', 'file:///etc/passwd', 'https://u:p@example.test', 'https://@example.test',
    'https://example.test\\evil', 'https://exa mple.test', 'https:///missing-host', 'https://example.test:99999'])(
    'rejects unsafe/malformed URL %s without fetching', url => {
      const s = clone(); s.sources[0].url = url;
      expect(() => parseExplorationSnapshot(s)).toThrow();
    });
  it('accepts confirmed sourced relationships and scoped facet claims', () => {
    const s = clone(); s.relationships[0].verification = 'confirmed'; s.claims[0].facet_key = 'career';
    expect(parseExplorationSnapshot(s)).toBe(s);
  });
});

describe('request cursor correlation (no transport)', () => {
  const setup = () => {
    const s = clone(); s.requests[0].status = 'loading';
    return { s, update: { ...s.requests[0], snapshot_revision: s.revision, last_sequence: 13 } };
  };
  it('accepts a newer correlated sequence', () => {
    const { s, update } = setup(); expect(acceptsExplorationUpdate(s, update)).toBe(true);
  });
  it.each([
    { request_id: 'old' }, { exploration_id: 'other' }, { path_id: 'other' }, { parent_occurrence_id: null },
    { run_id: 'wrong' }, { thread_id: 'wrong' }, { committed_history_version: 6 },
    { snapshot_revision: 0 }, { last_sequence: 12 }, { last_sequence: 11 }, { last_sequence: NaN },
  ])('rejects stale/uncorrelated cursor %#', patch => {
    const { s, update } = setup();
    expect(acceptsExplorationUpdate(s, { ...update, ...patch })).toBe(false);
  });
  it.each(['completed', 'failed', 'cancelled'] as AtlasExplorationRequest['status'][])(
    'rejects late updates after %s', status => {
      const { s, update } = setup(); s.requests[0].status = status;
      expect(acceptsExplorationUpdate(s, update)).toBe(false);
    });
  it('rejects a superseded request even with a larger sequence', () => {
    const { s, update } = setup();
    s.requests.push({ ...s.requests[0], request_id: 'retry', run_id: 'new-run' });
    expect(acceptsExplorationUpdate(s, update)).toBe(false);
  });
});
