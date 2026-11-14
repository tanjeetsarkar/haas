/** Receipt-driven ordering only. The Atlas storage client owns HTTP and payload schemas. */
export interface WorkspaceReceipt {
  workspace_id: string;
  mutation_id: string;
  revision: number;
  saved_at: string;
}

export interface WorkspaceMutation<K extends string, P> {
  schema_version: 1;
  mutation_id: string;
  expected_revision: number;
  kind: K;
  payload: P;
}

export interface WorkspaceSaveTransport<K extends string, P, Latest> {
  mutate(workspaceId: string, mutation: WorkspaceMutation<K, P>): Promise<WorkspaceReceipt>;
  latest(workspaceId: string): Promise<Latest>;
}

export interface WorkspaceSaveState<Latest> {
  status: 'saved' | 'dirty' | 'saving' | 'failed' | 'conflict';
  revision: number;
  pending: number;
  receipt?: WorkspaceReceipt;
  /** Authorized server state, for explicit reload; never automatically merged. */
  latest?: Latest;
  error?: string;
}

interface Pending<K extends string, P> {
  kind: K;
  payload: P;
  wire?: WorkspaceMutation<K, P>;
  receipts: Array<(receipt: WorkspaceReceipt) => void>;
  waiters: Array<{ resolve(receipt: WorkspaceReceipt): void; reject(error: Error): void }>;
}

function immutableCopy<T>(value: T): T {
  const copy: T = JSON.parse(JSON.stringify(value));
  function freeze(item: unknown) {
    if (item && typeof item === 'object') {
      Object.values(item).forEach(freeze);
      Object.freeze(item);
    }
  }
  freeze(copy);
  return copy;
}

/**
 * One instance per workspace. Never dispatches research/actions or retries implicitly.
 * A failed receipt is ambiguous: explicit retry retains the identical mutation body.
 * Construct only from an acknowledged create/get revision, not optimistic local state.
 */
export class WorkspaceSaveQueue<K extends string, P, Latest> {
  private items: Pending<K, P>[] = [];
  private running = false;
  private blocked = false;
  private state: WorkspaceSaveState<Latest>;
  private listeners = new Set<(state: WorkspaceSaveState<Latest>) => void>();

  constructor(
    private readonly workspaceId: string,
    revision: number,
    private readonly transport: WorkspaceSaveTransport<K, P, Latest>,
    private readonly newId: () => string = () => crypto.randomUUID(),
  ) {
    if (!workspaceId || !Number.isSafeInteger(revision) || revision < 1) {
      throw new Error('An acknowledged workspace and revision are required.');
    }
    this.state = { status: 'saved', revision, pending: 0 };
  }

  getState = (): WorkspaceSaveState<Latest> => this.state;

  subscribe(listener: (state: WorkspaceSaveState<Latest>) => void): () => void {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  private publish(update: Partial<WorkspaceSaveState<Latest>>) {
    this.state = Object.freeze({ ...this.state, ...update, pending: this.items.length });
    this.listeners.forEach(listener => listener(this.state));
  }

  enqueue(kind: K, payload: P, onReceipt?: (receipt: WorkspaceReceipt) => void): Promise<WorkspaceReceipt> {
    const copy = immutableCopy(payload);
    const tail = this.items[this.items.length - 1];
    // Only adjacent, unsent drafts may coalesce. Never cross an intent/commit.
    const pending = kind === 'draft_checkpoint' && tail?.kind === kind && !tail.wire
      ? tail : undefined;
    const item = pending ?? { kind, payload: copy, waiters: [], receipts: [] };
    if (pending) pending.payload = copy;
    else this.items.push(item);
    if (onReceipt) item.receipts.push(onReceipt);
    const result = new Promise<WorkspaceReceipt>((resolve, reject) => {
      item.waiters.push({ resolve, reject });
    });
    this.publish(this.blocked ? {} : { status: this.running ? 'saving' : 'dirty' });
    queueMicrotask(() => { void this.pump(); });
    return result;
  }

  /** Resolve only after all edits queued before/during the flush have receipts. */
  async flush(): Promise<void> {
    await this.pump();
    if (!this.items.length) return;
    if (this.blocked) throw new Error('Workspace saves are blocked; explicit recovery is required.');
    await new Promise<void>((resolve, reject) => {
      const unsubscribe = this.subscribe(state => {
        if (state.status === 'failed' || state.status === 'conflict') {
          unsubscribe();
          reject(new Error('Workspace saves are blocked; explicit recovery is required.'));
        } else if (state.pending === 0) {
          unsubscribe();
          resolve();
        }
      });
    });
  }

  /** No automatic retries, especially after conflict. */
  async retry(): Promise<void> {
    if (this.state.status !== 'failed') throw new Error('Only a failed save can be retried.');
    this.blocked = false;
    await this.pump();
    if (this.blocked) throw new Error('Workspace save retry failed.');
  }

  private async pump(): Promise<void> {
    if (this.running || this.blocked || !this.items.length) return;
    this.running = true;
    try {
      while (this.items.length && !this.blocked) {
        const item = this.items[0];
        item.wire ??= immutableCopy({
          schema_version: 1, mutation_id: this.newId(), expected_revision: this.state.revision,
          kind: item.kind, payload: item.payload,
        });
        this.publish({ status: 'saving', error: undefined });
        try {
          const receipt = await this.transport.mutate(this.workspaceId, item.wire);
          if (receipt.workspace_id !== this.workspaceId || receipt.mutation_id !== item.wire.mutation_id
            || receipt.revision !== item.wire.expected_revision + 1
            || !Number.isFinite(Date.parse(receipt.saved_at))) {
            throw new Error('Invalid save receipt.');
          }
          this.items.shift();
          const acknowledged = immutableCopy(receipt);
          item.receipts.splice(0).forEach(notify => notify(acknowledged));
          this.publish({
            status: this.items.length ? 'dirty' : 'saved',
            revision: receipt.revision, receipt: acknowledged,
          });
          item.waiters.splice(0).forEach(w => w.resolve(acknowledged));
        } catch (error) {
          this.blocked = true;
          const conflict = typeof error === 'object' && error !== null
            && 'status' in error && error.status === 409;
          // Do not display arbitrary server errors which may contain private payloads.
          this.publish({
            status: conflict ? 'conflict' : 'failed',
            error: conflict ? 'Revision conflict. Reload or keep a local recovery copy.'
              : 'Save failed. Changes are not confirmed durable.',
          });
          this.items.forEach(p => p.waiters.splice(0).forEach(w => w.reject(new Error(this.state.error))));
          if (conflict) {
            try { this.publish({ latest: await this.transport.latest(this.workspaceId) }); }
            catch { this.publish({ error: 'Conflict; authorized latest workspace could not be loaded.' }); }
          }
        }
      }
    } finally { this.running = false; }
  }
}
