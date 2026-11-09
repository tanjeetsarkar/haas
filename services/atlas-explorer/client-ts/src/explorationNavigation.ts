import type {
  AtlasExplorationSnapshot, AtlasOccurrence, AtlasTarget,
} from './explorationTypes';
import { parseExplorationSnapshot } from './explorationValidation';
import { inheritSnapshotParent } from './explorationLineage';

let fallbackId = 0;
function uniqueId(s: AtlasExplorationSnapshot, kind: string): string {
  const used = new Set([...s.paths.map(p => p.path_id), ...s.occurrences.map(o => o.occurrence_id)]);
  let id: string;
  do {
    const uuid = globalThis.crypto?.randomUUID?.();
    id = `${kind}-${uuid ?? `${Date.now()}-${++fallbackId}`}`;
    if (used.has(id)) id = `${id}-${++fallbackId}`;
  } while (used.has(id));
  return id;
}

function sameTarget(a: AtlasTarget, b: AtlasTarget): boolean {
  if (a.kind === 'source') return b.kind === 'source' && a.source_id === b.source_id;
  if (a.kind === 'entity') return b.kind === 'entity' && a.entity_id === b.entity_id;
  return b.kind === 'facet' && a.entity_id === b.entity_id && a.facet_key === b.facet_key;
}

/** Selected ancestry for the active saved path; entity identity is never a row ID. */
export function getExplorationBreadcrumbs(snapshot: AtlasExplorationSnapshot): AtlasOccurrence[] {
  const s = parseExplorationSnapshot(snapshot);
  const path = s.paths.find(p => p.path_id === s.active_path_id);
  const rows = new Map(s.occurrences.map(o => [o.occurrence_id, o]));
  return path?.selected_occurrence_ids.map(id => rows.get(id)!) ?? [];
}

function changed(s: AtlasExplorationSnapshot, patch: Partial<AtlasExplorationSnapshot>): AtlasExplorationSnapshot {
  return inheritSnapshotParent(s, parseExplorationSnapshot({ ...s, ...patch, revision: s.revision + 1 }));
}

/** Revisit exactly the saved selection, without creating requests or executing actions. */
export function activateExplorationPath(snapshot: AtlasExplorationSnapshot, pathId: string): AtlasExplorationSnapshot {
  const s = parseExplorationSnapshot(snapshot);
  if (!s.paths.some(p => p.path_id === pathId)) throw new Error(`Unknown Atlas path: ${pathId}`);
  return s.active_path_id === pathId ? s : changed(s, { active_path_id: pathId });
}

/** Select a target below a selected parent (null = root).
 * A matching saved prefix restores its longest saved suffix, otherwise a new branch is added.
 * Prefix occurrences are shared only through path ancestry. New rows always have unique IDs.
 */
export function selectExplorationTarget(
  snapshot: AtlasExplorationSnapshot, target: AtlasTarget, parentOccurrenceId: string | null,
): AtlasExplorationSnapshot {
  const s = parseExplorationSnapshot(snapshot);
  const breadcrumbs = getExplorationBreadcrumbs(s);
  const parentIndex = parentOccurrenceId === null ? -1
    : breadcrumbs.findIndex(o => o.occurrence_id === parentOccurrenceId);
  if (parentOccurrenceId !== null && parentIndex < 0) {
    throw new Error(`Atlas parent is not selected on active path: ${parentOccurrenceId}`);
  }
  const prefix = breadcrumbs.slice(0, parentIndex + 1).map(o => o.occurrence_id);
  const rows = new Map(s.occurrences.map(o => [o.occurrence_id, o]));
  const saved = s.paths.filter(p =>
    prefix.every((id, i) => p.selected_occurrence_ids[i] === id) &&
    p.selected_occurrence_ids.length > prefix.length &&
    sameTarget(rows.get(p.selected_occurrence_ids[prefix.length])!.target, target))
    .sort((a, b) => b.selected_occurrence_ids.length - a.selected_occurrence_ids.length)[0];
  if (saved) return activateExplorationPath(s, saved.path_id);
  const pathId = uniqueId(s, 'path');
  const occurrenceId = uniqueId(s, 'occurrence');
  return changed(s, {
    occurrences: [...s.occurrences, {
      occurrence_id: occurrenceId, path_id: pathId, parent_occurrence_id: parentOccurrenceId, target,
    }],
    paths: [...s.paths, {
      path_id: pathId, parent_path_id: parentOccurrenceId === null ? null : s.active_path_id,
      selected_occurrence_ids: [...prefix, occurrenceId],
    }],
    active_path_id: pathId,
  });
}

/** Back changes only local selection. The complete branch remains available for restoration. */
export function previousExplorationPath(snapshot: AtlasExplorationSnapshot): AtlasExplorationSnapshot {
  const s = parseExplorationSnapshot(snapshot);
  const prefix = getExplorationBreadcrumbs(s).slice(0, -1).map(o => o.occurrence_id);
  if (s.active_path_id === null) return s;
  const current = s.paths.find(p => p.path_id === s.active_path_id)!;
  if (!current.selected_occurrence_ids.length) return s;
  const ancestors = new Set<string>();
  let id: string | null = current.path_id;
  while (id !== null) {
    ancestors.add(id);
    id = s.paths.find(p => p.path_id === id)!.parent_path_id;
  }
  const saved = s.paths.find(p => ancestors.has(p.path_id) &&
    p.selected_occurrence_ids.length === prefix.length &&
    prefix.every((o, i) => p.selected_occurrence_ids[i] === o));
  if (saved) return activateExplorationPath(s, saved.path_id);
  const pathId = uniqueId(s, 'path');
  return changed(s, {
    paths: [...s.paths, { path_id: pathId, parent_path_id: current.path_id, selected_occurrence_ids: prefix }],
    active_path_id: pathId,
  });
}

export type AtlasNavigationAction =
  | { type: 'select'; target: AtlasTarget; parentOccurrenceId: string | null }
  | { type: 'activate'; pathId: string }
  | { type: 'back' };

/** Pure, local reducer. It never schedules a run, persists, fetches, or approves an action. */
export function reduceExplorationNavigation(
  snapshot: AtlasExplorationSnapshot, action: AtlasNavigationAction,
): AtlasExplorationSnapshot {
  switch (action.type) {
    case 'select': return selectExplorationTarget(snapshot, action.target, action.parentOccurrenceId);
    case 'activate': return activateExplorationPath(snapshot, action.pathId);
    case 'back': return previousExplorationPath(snapshot);
  }
}
