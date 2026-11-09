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
});
