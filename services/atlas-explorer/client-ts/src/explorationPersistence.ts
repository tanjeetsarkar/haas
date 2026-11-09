import type { Artifact, HaaSClient } from '@haas/sdk';
import type { AtlasExplorationSnapshot } from './explorationTypes';
import { parseExplorationSnapshot } from './explorationValidation';
import { inheritSnapshotParent, rememberSnapshotParent, snapshotParent } from './explorationLineage';

export interface AtlasSaveDescriptor {
  artifactId: string;
  runId: string;
  explorationId: string;
  revision: number;
  parentArtifactId: string | null;
  schemaVersion: 'atlas-exploration.v2';
  sha256?: string;
}

const SAVE_FORMAT = 'atlas-snapshot.v1';
const MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024;
function descriptor(artifact: Artifact): AtlasSaveDescriptor {
  const m = artifact.metadata;
  if (artifact.content_type !== 'application/json' || !artifact.run_id ||
    m?.format !== SAVE_FORMAT || m.schema_version !== 'atlas-exploration.v2' ||
    typeof m.exploration_id !== 'string' || !m.exploration_id.trim() ||
    typeof m.revision !== 'number' || !Number.isSafeInteger(m.revision) || m.revision < 0 ||
    !(m.parent_artifact_id === null || typeof m.parent_artifact_id === 'string')) {
    throw new Error('Artifact is not a versioned Atlas snapshot');
  }
  return {
    artifactId: artifact.artifact_id, runId: artifact.run_id, explorationId: m.exploration_id,
    revision: m.revision, parentArtifactId: m.parent_artifact_id,
    schemaVersion: 'atlas-exploration.v2', sha256: artifact.sha256,
  };
}

/** Immutable artifact writes only. Concurrent saves are sibling revisions, never last-writer-wins. */
export class AtlasSnapshotStore {
  constructor(private readonly client: HaaSClient) {}

  inheritLineage(parent: AtlasExplorationSnapshot | undefined, child: AtlasExplorationSnapshot): void {
    inheritSnapshotParent(parent, child);
  }

  async save(snapshot: AtlasExplorationSnapshot, runId?: string): Promise<AtlasSaveDescriptor> {
    // Capture before the first await: caller mutations/concurrent saves cannot change this write.
    const s = structuredClone(parseExplorationSnapshot(snapshot));
    const selectedRun = runId ?? s.requests.at(-1)?.run_id;
    if (!selectedRun?.trim()) throw new Error('Saving requires a HaaS run ID');
    const parentArtifactId = snapshotParent(snapshot) ?? null;
    const file = new Blob([JSON.stringify(s)], { type: 'application/json' });
    if (file.size > MAX_SNAPSHOT_BYTES) throw new Error('Atlas snapshot exceeds size budget');
    const result = await this.client.createArtifact({
      run_id: selectedRun, name: `atlas-${s.exploration_id}-r${s.revision}.json`,
      content_type: 'application/json', source: 'api',
      metadata: {
        format: SAVE_FORMAT, schema_version: s.schema_version, exploration_id: s.exploration_id,
        revision: s.revision, parent_artifact_id: parentArtifactId,
        execution: s.requests.map(r => ({
          request_id: r.request_id, run_id: r.run_id, thread_id: r.thread_id,
          committed_history_version: r.committed_history_version,
        })),
      },
    }, file);
    rememberSnapshotParent(snapshot, result.artifact_id);
    return {
      artifactId: result.artifact_id, runId: selectedRun, explorationId: s.exploration_id,
      revision: s.revision, parentArtifactId, schemaVersion: s.schema_version, sha256: result.sha256,
    };
  }

  async load(artifactId: string): Promise<AtlasExplorationSnapshot> {
    const artifact = await this.client.getArtifact(artifactId);
    const saved = descriptor(artifact);
    if (artifact.deleted_at || (artifact.size_bytes ?? 0) > MAX_SNAPSHOT_BYTES) throw new Error('Atlas artifact unavailable or too large');
    const blob = await this.client.downloadArtifact(artifactId);
    if (blob.size > MAX_SNAPSHOT_BYTES) throw new Error('Atlas artifact exceeds size budget');
    const snapshot = parseExplorationSnapshot(JSON.parse(await blob.text()));
    if (snapshot.exploration_id !== saved.explorationId || snapshot.revision !== saved.revision) {
      throw new Error('Atlas artifact metadata/content mismatch');
    }
    rememberSnapshotParent(snapshot, artifactId);
    return snapshot;
  }

  async listSaved(runId: string): Promise<AtlasSaveDescriptor[]> {
    if (!runId.trim()) throw new Error('Listing saved Atlas snapshots requires a run ID');
    const result: AtlasSaveDescriptor[] = [];
    let cursor: string | undefined;
    const seen = new Set<string>();
    for (let page = 0; page < 100; page++) {
      const response = await this.client.listArtifacts({ run_id: runId, cursor, limit: 100 });
      for (const artifact of response.items) {
        if (artifact.metadata?.format === SAVE_FORMAT && !artifact.deleted_at) {
          const item = descriptor(artifact);
          if (item.runId !== runId) throw new Error('Artifact run scope mismatch');
          result.push(item);
        }
      }
      if (!response.next_cursor) return result;
      if (seen.has(response.next_cursor)) throw new Error('Invalid artifact pagination');
      cursor = response.next_cursor;
      seen.add(cursor);
    }
    throw new Error('Saved snapshot page budget exceeded');
  }
}
