import type { WorkspaceMutation, WorkspaceReceipt, WorkspaceSaveTransport } from './workspaceSaveQueue';

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export interface WorkspacePayloads {
  draft_checkpoint: { draft: string };
  query_submitted: { prompt: string };
  selection_changed: { selection: Record<string, Json> };
  branch_committed: { branch: Record<string, Json> };
  comparison_changed: { comparison: Record<string, Json> };
  action_draft_changed: { action_draft: Record<string, Json> };
  operation_prepared: { operation_id: string; prompt: string; selected_context: Record<string, Json>[] };
  operation_bound: { operation_id: string; run_id: string; thread_id?: string };
  operation_outcome: { operation_id: string; state: 'submitting' | 'unknown' | 'terminal';
    status: 'submitting' | 'unknown' | 'completed' | 'failed' | 'cancelled'; run_id?: string };
}
export type StorageKind = keyof WorkspacePayloads;
export type StoragePayload = WorkspacePayloads[StorageKind];
export interface WorkspaceCatalogItem {
  workspace_id: string;
  title: string;
  revision: number;
  updated_at: string;
}
export interface WorkspaceDocument extends WorkspaceCatalogItem {
  state: {
    title: string;
    draft: string;
    selection?: Record<string, Json>;
    action_draft?: Record<string, Json>;
    branches?: Record<string, Json>[];
    comparison?: Record<string, Json>;
    operations?: Record<string, {
      state?: 'prepared' | 'submitting' | 'bound' | 'unknown' | 'terminal';
      run_id?: string;
      thread_id?: string | null;
      [key: string]: Json | undefined;
    }>;
  };
}
export interface WorkspaceCatalog {
  items: WorkspaceCatalogItem[];
  next_cursor: string | null;
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function metadata(value: unknown): value is WorkspaceCatalogItem {
  return object(value) && typeof value.workspace_id === 'string' && !!value.workspace_id
    && typeof value.title === 'string' && value.title.length <= 200
    && Number.isSafeInteger(value.revision) && Number(value.revision) >= 1
    && typeof value.updated_at === 'string' && Number.isFinite(Date.parse(value.updated_at));
}
function receipt(value: unknown): WorkspaceReceipt {
  if (!object(value) || typeof value.workspace_id !== 'string' || !value.workspace_id
    || typeof value.mutation_id !== 'string' || !value.mutation_id
    || !Number.isSafeInteger(value.revision) || Number(value.revision) < 1
    || typeof value.saved_at !== 'string' || !Number.isFinite(Date.parse(value.saved_at))) {
    throw new Error('Invalid workspace receipt. Save is not confirmed.');
  }
  return value as unknown as WorkspaceReceipt;
}

/** Unfinished/ambiguous intents block fresh dispatch, including old records without a state. */
export function blocksResearch(document: WorkspaceDocument): boolean {
  return Object.values(document.state.operations ?? {}).some(operation =>
    operation.state !== 'bound' && operation.state !== 'terminal');
}

/** Fixed same-origin 042 protocol only. No credentials, identity headers or retry policy here. */
export class WorkspaceStorageApi implements WorkspaceSaveTransport<StorageKind, StoragePayload, WorkspaceDocument> {
  constructor(private readonly request: typeof fetch = (input, init) => globalThis.fetch(input, init)) {}

  private async json(path: string, method = 'GET', body?: unknown): Promise<unknown> {
    const response = await this.request(`/__atlas/workspaces${path}`, {
      method, credentials: 'same-origin', redirect: 'error',
      headers: body === undefined ? { Accept: 'application/json' }
        : { Accept: 'application/json', 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      // Never echo arbitrary backend bodies/credentials into UI errors.
      throw Object.assign(new Error(`Workspace storage unavailable (HTTP ${response.status}). Changes are not confirmed saved.`),
        { status: response.status });
    }
    if (!response.headers.get('content-type')?.includes('application/json')) {
      throw new Error('Workspace storage returned a non-JSON response.');
    }
    const text = await response.text();
    if (new TextEncoder().encode(text).length > 2 * 1024 * 1024) throw new Error('Workspace response exceeds 2MiB.');
    return JSON.parse(text);
  }

  async catalog(cursor?: string): Promise<WorkspaceCatalog> {
    const query = new URLSearchParams({ limit: '20' });
    if (cursor) query.set('cursor', cursor);
    const value = await this.json(`?${query}`);
    if (!object(value) || !Array.isArray(value.items) || value.items.length > 100
      || !value.items.every(metadata)
      || (value.next_cursor !== null && typeof value.next_cursor !== 'string')) {
      throw new Error('Invalid workspace catalog.');
    }
    return value as unknown as WorkspaceCatalog;
  }

  async create(title: string, draft: string, mutationId: string): Promise<WorkspaceReceipt> {
    const result = receipt(await this.json('', 'POST', {
      schema_version: 1, mutation_id: mutationId, title, draft,
    }));
    if (result.mutation_id !== mutationId || result.revision !== 1) throw new Error('Workspace create receipt mismatch.');
    return result;
  }

  async latest(id: string): Promise<WorkspaceDocument> {
    const value = await this.json(`/${encodeURIComponent(id)}`);
    if (!metadata(value) || value.workspace_id !== id || !object(value)
      || !object(value.state) || typeof value.state.draft !== 'string'
      || value.state.draft.length > 32000 || typeof value.state.title !== 'string'
      || (value.state.operations !== undefined && (!object(value.state.operations)
        || !Object.values(value.state.operations).every(object)))) {
      throw new Error('Invalid workspace state. Nothing was restored.');
    }
    return value as unknown as WorkspaceDocument;
  }

  async mutate(id: string, mutation: WorkspaceMutation<StorageKind, StoragePayload>): Promise<WorkspaceReceipt> {
    return receipt(await this.json(`/${encodeURIComponent(id)}/mutations`, 'POST', mutation));
  }
}
