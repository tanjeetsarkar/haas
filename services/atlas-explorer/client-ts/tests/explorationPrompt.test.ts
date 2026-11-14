import { describe, expect, it } from 'vitest';
import { parseResearchResult, prepareResearch, projectResearch, researchPrompt } from '../src/explorationResearch';
import { researchResultExample } from '../src/researchResultExample';
import { computers } from './fixtures/explorations';

describe('Bounded research prompt strategy', () => {
  it.each([{ child: false, finalStep: 5 }, { child: true, finalStep: 4 }])(
    'reserves final JSON time for child=$child without relaxing evidence checks', ({ child, finalStep }) => {
      const prompt = researchPrompt('computers', computers, child);
      expect(prompt).toContain('Research the current layer only');
      expect(prompt).toContain('at most 1 web_search call');
      expect(prompt).toContain('at most 2 successful\nweb_fetch_page calls total');
      expect(prompt).toContain('Batch independent tool calls only if the tool interface supports batching');
      expect(prompt).toContain('Omit freshness unless needed; its only valid values are pd, pw, pm, py');
      expect(prompt).toContain('Never retry a source that returns HTTP 403');
      expect(prompt).toContain('exact URL and excerpt occur together\nin the same successful search result');
      expect(prompt).toContain(`Finalize the source-backed graph by step ${finalStep}`);
      expect(prompt).toContain('mark missing evidence unsupported with a reason');
      expect(prompt).toContain('leave unresearched facets research_needed');
      expect(prompt).toContain('never fabricate evidence or claim a failed run succeeded');
      expect(prompt).toContain('Return ONE JSON object matching the schema below');
    },
  );

  it('distinguishes broad categories from named-subject facets and enforces facet fields', () => {
    const prompt = researchPrompt('transport', computers, false);
    expect(prompt).toContain('aim for 3-8 sourced useful types');
    expect(prompt).toContain('not a single definition or a repeat of the root concept');
    expect(prompt).toContain('not a fixed domain taxonomy');
    expect(prompt).toContain('Do not invent categories\nto reach the count');
    expect(prompt).toContain('A named subject instead needs identity resolution');
    expect(prompt).toContain('scope is a human-readable research context');
    expect(prompt).toContain('reason is required ONLY for status "unavailable"');
    expect(prompt).toContain('For status "known" or "research_needed", reason MUST be absent');
    expect(prompt).toContain('the example is not an allowed citation or fallback');
    expect(prompt).toContain(JSON.stringify(researchResultExample));
    expect(prompt).toContain('{"kind":"entity","entity_id":"hospital-1"}');
    expect(prompt).toContain('{"kind":"facet","entity_id":"hospital-1","facet_key":"specialists"}');
    expect(prompt).toContain('{"kind":"source","source_id":"source-1"}');
    expect(prompt).toContain('Entity kind (such as category,\nperson or organization) is separate from target kind ("entity").');
    expect(prompt).toContain('Use only a target for the next row; do not generate deeper levels.');
  });

  it('validates and projects the complete broad-category fixture only with observed page evidence', () => {
    const result = parseResearchResult(structuredClone(researchResultExample));
    const base = prepareResearch('transport');
    const request = {
      request_id: 'test-request', exploration_id: base.exploration_id, path_id: base.active_path_id!,
      parent_occurrence_id: null, run_id: 'test-run', thread_id: 'test-thread',
      committed_history_version: 7, last_sequence: 4, status: 'completed' as const,
    };
    expect(() => projectResearch(base, result, request, false, [])).toThrow('Unobserved research source');
    const snapshot = projectResearch(base, result, request, false, [
      JSON.stringify({ url: result.sources[0].url, text: result.sources[0].excerpt }),
    ]);
    expect(snapshot.occurrences.map(o => o.target)).toEqual(result.targets);
    expect(snapshot.entities.filter(e => e.kind === 'category')).toHaveLength(3);
    expect(snapshot.claims.every(c => c.evidence.status === 'sourced')).toBe(true);
    expect(snapshot.paths).toEqual(base.paths);
  });

  it.each(['known', 'research_needed'] as const)('rejects reason on %s facets without deleting it', status => {
    const result = structuredClone(researchResultExample);
    const facet = { key: 'computer', label: 'Computer', scope: 'Public context',
      status, reason: 'Definition available via a public page' };
    result.entities[1].facets = [facet];
    expect(() => parseResearchResult(result)).toThrow('Invalid Atlas research result');
    expect(result.entities[1].facets[0]).toHaveProperty('reason', facet.reason);
  });

  it('reports a synthetic six-invalid-target regression without exposing model values', () => {
    const result = structuredClone(researchResultExample);
    result.targets = [
      { kind: 'entity' } as never,
      { kind: 'facet', entity_id: 'road' } as never,
      { kind: 'source' } as never,
      { kind: 'mystery', entity_id: 'road' } as never,
      { kind: 'entity', entity_id: 'road', title: 'SENSITIVE MODEL TEXT' } as never,
      { kind: 'entity', entity_id: 'road', source_id: 'source-1' } as never,
    ];
    const original = structuredClone(result);
    let failure: unknown;
    try { parseResearchResult(result); } catch (error) { failure = error; }
    expect(failure).toMatchObject({ name: 'AtlasResearchValidationError' });
    const error = failure as Error & { details: unknown[] };
    expect(error.message).toContain('target[0]');
    expect(error.message).toContain('target[5]');
    expect(error.message).toContain('received kind "mystery"');
    expect(error.message).toContain('allowed kinds: entity, facet, source');
    expect(error.message).toContain('required fields: "kind", "entity_id"');
    expect(error.message).toContain('extra fields: "title"');
    expect(error.message).not.toContain('SENSITIVE MODEL TEXT');
    expect(error.details.length).toBeGreaterThan(6);
    expect(result).toEqual(original);
  });

  it('rejects mixed, unknown, extra, and missing reference fields strictly', () => {
    const invalidTargets = [
      { kind: 'entity', entity_id: 'road', source_id: 'synthetic-format-source' },
      { kind: 'unsupported', entity_id: 'road' },
      { kind: 'entity', entity_id: 'road', label: 'Road' },
      { kind: 'entity' },
      { kind: 'facet', entity_id: 'road' },
      { kind: 'source' },
    ];
    for (const target of invalidTargets) {
      const result = structuredClone(researchResultExample);
      result.targets = [target] as never;
      expect(() => parseResearchResult(result)).toThrow('Invalid Atlas research result');
    }
  });

  it.each([
    { kind: 'entity', entity_id: 'missing-entity' },
    { kind: 'facet', entity_id: 'road', facet_key: 'missing-facet' },
    { kind: 'source', source_id: 'missing-source' },
  ] as const)('rejects structurally valid but dangling target $kind references semantically', target => {
    const result = structuredClone(researchResultExample);
    result.targets = [target];
    const base = prepareResearch('transport');
    const request = {
      request_id: 'test-request', exploration_id: base.exploration_id, path_id: base.active_path_id!,
      parent_occurrence_id: null, run_id: 'test-run', thread_id: 'test-thread',
      committed_history_version: 7, last_sequence: 4, status: 'completed' as const,
    };
    const observed = [JSON.stringify({ url: result.sources[0].url, text: result.sources[0].excerpt })];
    expect(() => projectResearch(base, parseResearchResult(result), request, false, observed))
      .toThrow('Invalid Atlas exploration');
  });

  it('caps unrelated validation diagnostics and reports omitted issue count', () => {
    const result = structuredClone(researchResultExample);
    result.entities = Array.from({ length: 12 }, (_, index) => ({ entity_id: `bad-${index}` })) as never;
    let failure: unknown;
    try { parseResearchResult(result); } catch (error) { failure = error; }
    expect(failure).toBeInstanceOf(Error);
    expect((failure as Error).message).toContain('additional validation issue(s) omitted');
    expect((failure as Error).message.length).toBeLessThan(1600);
  });
});
