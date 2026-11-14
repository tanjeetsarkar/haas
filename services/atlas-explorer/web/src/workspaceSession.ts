import {
  activateExplorationPath, parseExplorationSnapshot, type AtlasExecutionContext, type AtlasOperationPersistence,
} from '@haas/atlas-explorer-client';
import type { AtlasWorkspaceData } from './explorationAdapter';
import { WorkspaceSaveQueue, type WorkspaceReceipt } from './workspaceSaveQueue';
import {
  blocksResearch, WorkspaceStorageApi, type StorageKind, type StoragePayload,
  type WorkspaceDocument, type WorkspacePayloads,
} from './workspaceStorageApi';

/** Recovered records are never executed here. This class writes only Atlas mutations. */
export class WorkspaceSession {
  readonly queue: WorkspaceSaveQueue<StorageKind, StoragePayload, WorkspaceDocument>;
  private readonly callbacks = new Map<string, {
    body: string; promise: Promise<WorkspaceReceipt>; receipt?: WorkspaceReceipt; failed?: boolean;
  }>();
  private blocked: boolean;
  private selection: Record<string, unknown>;

  constructor(readonly document: WorkspaceDocument, storage: WorkspaceStorageApi) {
    this.queue = new WorkspaceSaveQueue(document.workspace_id, document.revision, storage);
    this.blocked = blocksResearch(document);
    this.selection = document.state.selection ?? {};
  }

  canResearch(): boolean {
    return !this.blocked && this.queue.getState().status !== 'conflict'
      && this.queue.getState().status !== 'failed';
  }

  save<K extends StorageKind>(kind: K, payload: WorkspacePayloads[K]): Promise<WorkspaceReceipt> {
    return this.queue.enqueue(kind, payload);
  }

  /** One adapter per explicit submitted query; question commit precedes operation intent. */
  persistence(question: string, observationOnly = false): AtlasOperationPersistence {
    if (!observationOnly && !this.canResearch()) throw new Error('Unresolved workspace submission or save. Research is blocked.');
    const workspaceId = this.document.workspace_id;
    const persist = (key: string, kind: StorageKind, payload: StoragePayload) => {
      const body = JSON.stringify({ kind, payload });
      const previous = this.callbacks.get(key);
      if (previous) {
        if (previous.body !== body) return Promise.reject(new Error('Operation callback body changed.'));
        if (previous.receipt) return Promise.resolve(previous.receipt);
        if (!previous.failed) return previous.promise;
        // Explicit client binding recovery retries the original queue item/ID only.
        return this.queue.retry().then(() => {
          if (!previous.receipt) throw new Error('Original operation receipt remains unavailable.');
          return previous.receipt;
        });
      }
      const entry: {
        body: string; promise: Promise<WorkspaceReceipt>; receipt?: WorkspaceReceipt; failed?: boolean;
      } = {
        body,
        promise: this.queue.enqueue(kind, payload, receipt => { entry.receipt = receipt; })
          .catch(error => { entry.failed = true; throw error; }),
      };
      this.callbacks.set(key, entry);
      return entry.promise;
    };
    const checkWorkspace = (id: string) => {
      if (id !== workspaceId) throw new Error('Operation belongs to a different workspace.');
    };
    return {
      workspaceId,
      prepare: async input => {
        checkWorkspace(input.workspaceId);
        if (observationOnly) throw new Error('Restored observation cannot dispatch research.');
        if (!this.canResearch()) throw new Error('Unresolved submission. No research was dispatched.');
        this.blocked = true;
        await persist(`${input.operationId}:query`, 'query_submitted', { prompt: question });
        return persist(`${input.operationId}:prepare`, input.kind,
          input.payload as WorkspacePayloads['operation_prepared']);
      },
      bind: async input => {
        checkWorkspace(input.workspaceId);
        return persist(`${input.operationId}:bind`, input.kind, input.payload);
      },
      outcome: async input => {
        checkWorkspace(input.workspaceId);
        const receipt = await persist(`${input.operationId}:${input.payload.state}`, input.kind, input.payload);
        if (input.payload.state === 'terminal') this.blocked = false;
        return receipt;
      },
    };
  }

  async saveSelection(patch: Record<string, unknown>): Promise<void> {
    this.selection = { ...this.selection, ...patch };
    await this.save('selection_changed', { selection: JSON.parse(JSON.stringify(this.selection)) });
  }

  observation(): { context: AtlasExecutionContext; lastEventId: string } | null {
    const raw = this.document.state.selection?.execution;
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
    const c = raw as Record<string, unknown>;
    if (['operationId', 'runId', 'threadId', 'requestId', 'explorationId', 'pathId'].some(key =>
      typeof c[key] !== 'string' || !c[key]) || c.workspaceId !== this.document.workspace_id
      || !Number.isSafeInteger(c.baseRevision) || !Number.isSafeInteger(c.historyVersion)
      || (c.parentOccurrenceId !== null && typeof c.parentOccurrenceId !== 'string')) return null;
    const operation = this.document.state.operations?.[c.operationId as string];
    if (!operation || operation.state !== 'bound' || operation.run_id !== c.runId
      || operation.thread_id !== c.threadId) return null;
    const cursor = this.document.state.selection?.last_event_id;
    return { context: c as unknown as AtlasExecutionContext,
      lastEventId: typeof cursor === 'string' && /^\d{1,18}$/u.test(cursor) ? cursor : '0' };
  }

  async commit(data: AtlasWorkspaceData): Promise<void> {
    // Strict parse before storing/restoring canonical graph; no raw provider output.
    const snapshot = parseExplorationSnapshot(data.snapshot);
    await this.save('branch_committed', {
      branch: JSON.parse(JSON.stringify({ snapshot, run_id: data.runId })),
    });
  }

  restoredData(): AtlasWorkspaceData | null {
    const branch = this.document.state.branches?.slice(-1)[0];
    if (!branch) return null;
    let snapshot = parseExplorationSnapshot(branch.snapshot);
    const path = this.document.state.selection?.path_id;
    if (typeof path === 'string') snapshot = activateExplorationPath(snapshot, path);
    if (typeof branch.run_id !== 'string') throw new Error('Saved branch lacks an authoritative run ID.');
    return { snapshot, runId: branch.run_id };
  }
}
