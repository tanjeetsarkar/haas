import type { AtlasExplorationSnapshot } from './explorationTypes';

// Advisory revision ancestry, never authorization or a tenant identity.
// Weak keys avoid retaining workspaces after callers release them.
const parents = new WeakMap<AtlasExplorationSnapshot, string>();
export function snapshotParent(snapshot: AtlasExplorationSnapshot): string | undefined {
  return parents.get(snapshot);
}
export function rememberSnapshotParent(snapshot: AtlasExplorationSnapshot, artifactId: string): void {
  parents.set(snapshot, artifactId);
}
export function inheritSnapshotParent(
  parent: AtlasExplorationSnapshot | undefined, child: AtlasExplorationSnapshot,
): AtlasExplorationSnapshot {
  const id = parent && parents.get(parent);
  if (id) parents.set(child, id);
  return child;
}
