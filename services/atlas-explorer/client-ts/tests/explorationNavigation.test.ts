import { describe, expect, it, vi } from 'vitest';
import {
  activateExplorationPath, getExplorationBreadcrumbs, parseExplorationSnapshot,
  previousExplorationPath, reduceExplorationNavigation, selectExplorationTarget,
} from '../src';
import type { AtlasExplorationSnapshot } from '../src';
import { computers, people } from './fixtures/explorations';

const fresh = (): AtlasExplorationSnapshot => structuredClone(computers);
const target = (entity_id: string) => ({ kind: 'entity' as const, entity_id });

describe('read-only exploration navigation', () => {
  it('restores a saved root branch, its suffix, and sibling branches without requests', () => {
    const s = fresh();
    const server = selectExplorationTarget(s, target('server'), null);
    expect(server.active_path_id).toBe('server-path');
    const restored = selectExplorationTarget(server, target('pc'), null);
    expect(restored.active_path_id).toBe('pc-path');
    expect(getExplorationBreadcrumbs(restored).map(o => o.occurrence_id)).toEqual(['pc-row', 'history-row']);
    expect(restored.paths).toBe(s.paths);
    expect(restored.requests).toBe(s.requests);
    expect(s.active_path_id).toBe('pc-path');
  });
  it('creates a sibling under a selected parent and preserves both saved suffixes', () => {
    const s = fresh();
    const next = selectExplorationTarget(s, target('server'), 'pc-row');
    expect(next.paths).toHaveLength(3);
    const trail = getExplorationBreadcrumbs(next);
    expect(trail[0].occurrence_id).toBe('pc-row');
    expect(trail[1].target).toEqual(target('server'));
    expect(trail[1].occurrence_id).not.toBe('server-row');
    expect(next.entities).toBe(s.entities);
    expect(next.requests).toEqual([]);
    expect(next.paths[0]).toEqual(s.paths[0]);
    const restored = selectExplorationTarget(next, { kind: 'facet', entity_id: 'pc', facet_key: 'history' }, 'pc-row');
    expect(restored.active_path_id).toBe('pc-path');
    const siblingAgain = selectExplorationTarget(restored, target('server'), 'pc-row');
    expect(siblingAgain.active_path_id).toBe(next.active_path_id);
    expect(siblingAgain.paths).toHaveLength(3);
  });
  it('navigates root entity -> facet -> source detail and back with a restorable saved suffix', () => {
    let s = activateExplorationPath(people, 'other');
    s = selectExplorationTarget(s, { kind: 'facet', entity_id: 'b', facet_key: 'history' }, 'b-direct');
    const facetRow = getExplorationBreadcrumbs(s).at(-1)!;
    s = selectExplorationTarget(s, { kind: 'source', source_id: 'synthetic' }, facetRow.occurrence_id);
    const saved = s.active_path_id!;
    const back = previousExplorationPath(s);
    expect(getExplorationBreadcrumbs(back)).toHaveLength(2);
    expect(activateExplorationPath(back, saved).active_path_id).toBe(saved);
    expect(activateExplorationPath(back, saved).occurrences).toBe(s.occurrences);
  });
  it('rejects a parent from an inactive sibling and an unknown parent', () => {
    for (const id of ['server-row', 'missing']) {
      expect(() => selectExplorationTarget(fresh(), target('server'), id)).toThrow(/active path/);
    }
    expect(() => activateExplorationPath(fresh(), 'missing')).toThrow(/Unknown Atlas path/);
  });
  it('validates new target references rather than inventing entities or facets', () => {
    expect(() => selectExplorationTarget(fresh(), target('missing'), null)).toThrow(/missing entity/);
    expect(() => selectExplorationTarget(fresh(), {
      kind: 'facet', entity_id: 'pc', facet_key: 'missing',
    }, 'pc-row')).toThrow(/missing facet/);
  });
  it('keeps entity identity separate from occurrence identity on different paths', () => {
    const next = selectExplorationTarget(people, target('b'), 'a-row');
    const rows = next.occurrences.filter(o => o.target.kind === 'entity' && o.target.entity_id === 'b');
    expect(rows).toHaveLength(3);
    expect(new Set(rows.map(o => o.occurrence_id)).size).toBe(3);
    expect(next.entities.filter(e => e.entity_id === 'b')).toHaveLength(1);
    expect(next.relationships).toBe(people.relationships);
  });
  it('supports empty ambiguity, root selection, back and reducer actions', () => {
    const empty: AtlasExplorationSnapshot = {
      ...fresh(), paths: [], occurrences: [], active_path_id: null,
    };
    expect(getExplorationBreadcrumbs(empty)).toEqual([]);
    expect(previousExplorationPath(empty)).toBe(empty);
    const selected = reduceExplorationNavigation(empty, {
      type: 'select', target: target('pc'), parentOccurrenceId: null,
    });
    const back = reduceExplorationNavigation(selected, { type: 'back' });
    expect(getExplorationBreadcrumbs(back)).toEqual([]);
    expect(reduceExplorationNavigation(back, { type: 'activate', pathId: selected.active_path_id! })
      .active_path_id).toBe(selected.active_path_id);
  });
  it('validates occurrence and path cycles at the navigation boundary', () => {
    const s = fresh(); s.paths[0].parent_path_id = 'pc-path';
    expect(() => getExplorationBreadcrumbs(s)).toThrow(/cycle/);
    const other = fresh(); other.occurrences[0].parent_occurrence_id = 'history-row';
    expect(() => activateExplorationPath(other, 'server-path')).toThrow();
  });
  it('rejects inherited selection not present in the immediate parent prefix', () => {
    const s = fresh();
    s.paths.push({ path_id: 'child', parent_path_id: 'pc-path', selected_occurrence_ids: [] });
    s.paths.push({ path_id: 'grandchild', parent_path_id: 'child', selected_occurrence_ids: ['pc-row'] });
    expect(() => parseExplorationSnapshot(s)).toThrow(/parent path prefix/);
  });
  it('generates noncolliding IDs with and without crypto', () => {
    try {
      for (const crypto of [undefined, { randomUUID: vi.fn().mockReturnValueOnce('pc-path').mockReturnValue('test-uuid') }]) {
        vi.stubGlobal('crypto', crypto);
        const one = selectExplorationTarget(fresh(), target('supercomputer'), 'pc-row');
        const two = selectExplorationTarget(one, target('computing'), getExplorationBreadcrumbs(one).at(-1)!.occurrence_id);
        expect(new Set(two.paths.map(p => p.path_id)).size).toBe(two.paths.length);
        expect(new Set(two.occurrences.map(o => o.occurrence_id)).size).toBe(two.occurrences.length);
      }
    } finally { vi.unstubAllGlobals(); }
  });
});
