import Ajv from 'ajv';
import addFormats from 'ajv-formats';
import schema from './explorationSchema.v2.json';
import type {
  AtlasEvidence, AtlasExplorationRequest, AtlasExplorationSnapshot, AtlasTarget,
} from './explorationTypes';

const ajv = new Ajv({ allErrors: true });
addFormats(ajv, { mode: 'full' });
const validate = ajv.compile<AtlasExplorationSnapshot>(schema);

function requireValid(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(`Invalid Atlas exploration: ${message}`);
}

/** Structural and semantic validation, without fetching URLs or executing actions.
 * Returns the input after validation; callers retaining it must not mutate it unchecked.
 */
export function parseExplorationSnapshot(value: unknown): AtlasExplorationSnapshot {
  if (!validate(value)) {
    throw new Error(`Invalid Atlas exploration schema: ${ajv.errorsText(validate.errors, { separator: '; ' })}`);
  }
  const s = value;
  const index = <T>(rows: T[], key: (row: T) => string, label: string) => {
    const result = new Map<string, T>();
    for (const row of rows) {
      const id = key(row);
      requireValid(!result.has(id), `duplicate ${label} ${id}`);
      result.set(id, row);
    }
    return result;
  };
  const entities = index(s.entities, e => e.entity_id, 'entity');
  const sources = index(s.sources, e => e.source_id, 'source');
  const occurrences = index(s.occurrences, e => e.occurrence_id, 'occurrence');
  const paths = index(s.paths, e => e.path_id, 'path');
  index(s.claims, e => e.claim_id, 'claim');
  index(s.relationships, e => e.relationship_id, 'relationship');
  index(s.requests, e => e.request_id, 'request');
  index(s.actions, e => e.action_id, 'action');
  const entityRef = (id: string, resolved = false) => {
    const e = entities.get(id);
    requireValid(e, `missing entity ${id}`);
    requireValid(!resolved || e.identity_status === 'resolved', `candidate entity ${id} cannot have attached facts/actions`);
    return e;
  };
  const facetRef = (id: string, key: string) =>
    requireValid(entityRef(id).facets.some(f => f.key === key), `missing facet ${id}/${key}`);
  const targetRef = (target: AtlasTarget, action = false) => {
    if (target.kind === 'source') requireValid(sources.has(target.source_id), `missing source ${target.source_id}`);
    else {
      entityRef(target.entity_id, action);
      if (target.kind === 'facet') facetRef(target.entity_id, target.facet_key);
    }
  };
  const evidenceRef = (e: AtlasEvidence) => {
    const ids = e.status === 'sourced' ? e.source_ids : e.status === 'conflicting'
      ? [...e.supporting_source_ids, ...e.opposing_source_ids] : [];
    for (const id of ids) requireValid(sources.has(id), `missing evidence source ${id}`);
  };
  for (const e of s.entities) index(e.facets, f => f.key, `facet on ${e.entity_id}`);
  for (const source of s.sources) {
    // WHATWG URL alone normalizes malformed inputs; reject whitespace/backslashes first.
    requireValid(!/[\s\\\u0000-\u001f\u007f]/u.test(source.url), `unsafe source URL ${source.source_id}`);
    let url: URL;
    try { url = new URL(source.url); } catch { throw new Error(`Invalid Atlas exploration: source URL ${source.source_id}`); }
    const authority = source.url.split('/')[2];
    requireValid(authority && ['http:', 'https:'].includes(url.protocol) && url.hostname &&
      !url.username && !url.password && !authority.includes('@'), `unsafe source URL ${source.source_id}`);
  }
  for (const c of s.claims) {
    entityRef(c.subject_entity_id, true);
    if (c.facet_key !== undefined) facetRef(c.subject_entity_id, c.facet_key);
    evidenceRef(c.evidence);
    if (c.details?.attributes) index(c.details.attributes, a => a.key, `attribute on ${c.claim_id}`);
    if (c.details?.checklist) index(c.details.checklist, step => step.id, `checklist step on ${c.claim_id}`);
  }
  for (const r of s.relationships) {
    entityRef(r.from_entity_id, true); entityRef(r.to_entity_id, true);
    evidenceRef(r.evidence);
    requireValid(r.verification !== 'confirmed' || r.evidence.status === 'sourced',
      `confirmed relationship ${r.relationship_id} requires sourced evidence`);
  }
  for (const a of s.actions) targetRef(a.target, true);
  if (s.interpretation.kind === 'resolved') entityRef(s.interpretation.subject_entity_id, true);
  else for (const id of s.interpretation.candidate_entity_ids) {
    requireValid(entityRef(id).identity_status === 'candidate', `ambiguous identity ${id} must be candidate`);
  }
  const lineage = (id: string) => {
    const seen = new Set<string>();
    let current: string | null = id;
    while (current !== null) {
      requireValid(!seen.has(current), `path cycle at ${current}`);
      seen.add(current);
      const path = paths.get(current);
      requireValid(path, `missing path ${current}`);
      current = path.parent_path_id;
    }
    return seen;
  };
  for (const p of s.paths) {
    const ancestors = lineage(p.path_id);
    let previous: string | null = null;
    for (const [position, id] of p.selected_occurrence_ids.entries()) {
      const o = occurrences.get(id);
      requireValid(o, `missing selected occurrence ${id}`);
      requireValid(ancestors.has(o.path_id), `occurrence ${id} outside path lineage ${p.path_id}`);
      if (o.path_id !== p.path_id) {
        const parentPath = paths.get(p.parent_path_id!);
        requireValid(parentPath?.selected_occurrence_ids[position] === id,
          `inherited occurrence ${id} is not in parent path prefix`);
      }
      requireValid(o.parent_occurrence_id === previous, `broken selected chain at ${id}`);
      previous = id;
    }
  }
  for (const o of s.occurrences) {
    targetRef(o.target);
    const ancestors = lineage(o.path_id);
    const seen = new Set([o.occurrence_id]);
    let parent = o.parent_occurrence_id;
    while (parent !== null) {
      requireValid(!seen.has(parent), `occurrence cycle at ${parent}`);
      seen.add(parent);
      const row = occurrences.get(parent);
      requireValid(row, `missing parent occurrence ${parent}`);
      requireValid(ancestors.has(row.path_id), `parent occurrence ${parent} outside lineage`);
      parent = row.parent_occurrence_id;
    }
  }
  requireValid(s.active_path_id === null || paths.has(s.active_path_id), `missing active path ${s.active_path_id}`);
  for (const r of s.requests) {
    requireValid(r.exploration_id === s.exploration_id, `request ${r.request_id} exploration mismatch`);
    const path = paths.get(r.path_id);
    requireValid(path, `request ${r.request_id} missing path`);
    requireValid(r.parent_occurrence_id === null || path.selected_occurrence_ids.includes(r.parent_occurrence_id),
      `request ${r.request_id} parent not selected on path`);
  }
  return s;
}

/** Caller must correlate an incoming update before applying it. No transport is installed.
 * Array order denotes issuance order; only the newest request for a path/parent can update it.
 * Snapshot revision and committed HaaS history version are independent counters.
 */
export function acceptsExplorationUpdate(
  snapshot: AtlasExplorationSnapshot,
  incoming: AtlasExplorationRequest & { snapshot_revision: number },
): boolean {
  const s = parseExplorationSnapshot(snapshot);
  const current = [...s.requests].reverse().find(r =>
    r.path_id === incoming.path_id && r.parent_occurrence_id === incoming.parent_occurrence_id);
  return !!current && (current.status === 'loading' || current.status === 'partial') &&
    incoming.exploration_id === s.exploration_id && incoming.snapshot_revision === s.revision &&
    incoming.request_id === current.request_id && incoming.run_id === current.run_id &&
    incoming.thread_id === current.thread_id &&
    incoming.committed_history_version === current.committed_history_version &&
    Number.isSafeInteger(incoming.last_sequence) && incoming.last_sequence > current.last_sequence;
}
