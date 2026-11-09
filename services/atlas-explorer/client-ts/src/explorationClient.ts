import type {
  ApprovalBatch, ApprovalDecision, HaaSClient, RunResponse, RunStatus, Thread,
} from '@haas/sdk';
import type { AtlasExplorationRequest, AtlasExplorationSnapshot } from './explorationTypes';
import { getExplorationBreadcrumbs } from './explorationNavigation';
import {
  atlasId, parseResearchResult, prepareResearch, projectResearch, researchPrompt,
  type AtlasResearchContext,
} from './explorationResearch';
import { AtlasSnapshotStore, type AtlasSaveDescriptor } from './explorationPersistence';

/** Resolve the producer of a row, not a later request researching below it.
 * Local selection creates a new occurrence; its original result row retains
 * the producer path/parent. Suggested facets without result rows inherit the
 * parent's producer. Never pick the newest run merely because it shares a prefix.
 */
function producerFor(
  snapshot: AtlasExplorationSnapshot, occurrenceId: string | null,
): AtlasExplorationRequest | undefined {
  if (occurrenceId === null) return snapshot.requests.find(r =>
    r.status === 'completed' && r.parent_occurrence_id === null);
  const selected = snapshot.occurrences.find(o => o.occurrence_id === occurrenceId);
  if (!selected) return undefined;
  for (const row of snapshot.occurrences) {
    if (row.parent_occurrence_id !== selected.parent_occurrence_id ||
      JSON.stringify(row.target) !== JSON.stringify(selected.target)) continue;
    const producer = snapshot.requests.find(r => r.status === 'completed' &&
      r.path_id === row.path_id && r.parent_occurrence_id === row.parent_occurrence_id);
    if (producer) return producer;
  }
  return producerFor(snapshot, selected.parent_occurrence_id);
}

export interface AtlasExecutionContext {
  requestId: string;
  explorationId: string;
  pathId: string;
  parentOccurrenceId: string | null;
  baseRevision: number;
  runId: string;
  threadId: string;
  historyVersion: number;
}
export type AtlasResearchEvent =
  | { type: 'status'; context: AtlasExecutionContext; status: RunStatus }
  | { type: 'approval'; context: AtlasExecutionContext; approval: ApprovalBatch }
  | { type: 'completed'; context: AtlasExecutionContext; snapshot: AtlasExplorationSnapshot }
  | { type: 'error'; context: AtlasExecutionContext; error: unknown };

export interface AtlasExplorationClientOptions {
  /** Total operation deadline, including approval wait. Default five minutes. */
  timeoutMs?: number;
  /** Poll interval. Default 750ms. No automatic retry of API errors. */
  pollIntervalMs?: number;
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** SDK methods do not accept AbortSignal. Stop waiting without losing late rejection handlers. */
function waitFor<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => { signal.removeEventListener('abort', abort); reject(signal.reason); };
    signal.addEventListener('abort', abort, { once: true });
    promise.then(
      value => { signal.removeEventListener('abort', abort); resolve(value); },
      error => { signal.removeEventListener('abort', abort); reject(error); },
    );
    if (signal.aborted) abort();
  });
}

interface ActiveResearch {
  controller: AbortController;
  runId?: string;
  cancellation?: Promise<RunResponse>;
  terminal?: boolean;
}

/** One active selected-branch operation per instance. A newer research cancels the older one. */
export class AtlasExplorationClient {
  private readonly observers = new Set<(event: AtlasResearchEvent) => void>();
  private active?: ActiveResearch;
  private readonly store: AtlasSnapshotStore;
  private readonly timeoutMs: number;
  private readonly pollMs: number;

  constructor(private readonly client: HaaSClient, options: AtlasExplorationClientOptions = {}) {
    this.timeoutMs = options.timeoutMs ?? 300_000;
    this.pollMs = options.pollIntervalMs ?? 750;
    if (!Number.isFinite(this.timeoutMs) || this.timeoutMs <= 0 ||
      !Number.isFinite(this.pollMs) || this.pollMs < 0) throw new Error('Invalid Atlas polling options');
    this.store = new AtlasSnapshotStore(client);
  }

  observe(observer: (event: AtlasResearchEvent) => void): () => void {
    this.observers.add(observer);
    return () => { this.observers.delete(observer); };
  }

  private emit(event: AtlasResearchEvent): void {
    // View errors must not change run lifecycle or prevent cancellation.
    for (const observer of this.observers) {
      try { observer(event); } catch { /* Observer owns its rendering failures. */ }
    }
  }

  async research(
    query: string, context?: AtlasResearchContext, signal?: AbortSignal,
  ): Promise<AtlasExplorationSnapshot> {
    const base = prepareResearch(query, context);
    signal?.throwIfAborted();
    this.active?.controller.abort(new DOMException('Superseded Atlas research', 'AbortError'));
    const operation: ActiveResearch = { controller: new AbortController() };
    this.active = operation;
    const abort = () => operation.controller.abort(signal?.reason);
    signal?.addEventListener('abort', abort, { once: true });
    const deadline = setTimeout(() => operation.controller.abort(
      new DOMException('Atlas research timed out', 'TimeoutError')), this.timeoutMs);
    let execution: AtlasExecutionContext | undefined;
    const cancel = () => {
      if (operation.runId && !operation.terminal && !operation.cancellation) {
        operation.cancellation = this.client.cancelRun(operation.runId);
        // Attach immediately; the error is surfaced by the catch below.
        void operation.cancellation.catch(() => undefined);
      }
    };
    operation.controller.signal.addEventListener('abort', cancel);
    const check = () => operation.controller.signal.throwIfAborted();
    const wait = <T>(promise: Promise<T>) => waitFor(promise, operation.controller.signal);
    try {
      check();
      const ancestry = context ? getExplorationBreadcrumbs(base) : [];
      const parent = context ? producerFor(base, context.parentOccurrenceId) : undefined;
      if (context && !parent) throw new Error('Selected branch has no completed HaaS execution context');
      const thread = await wait(parent
        ? this.client.branchThread(parent.thread_id, { expected_history_version: parent.committed_history_version })
        : this.client.createThread({ metadata: { app: 'atlas-explorer', exploration_id: base.exploration_id } }));
      check();
      const run = await wait(this.client.createRun({
        thread_id: thread.thread_id, expected_history_version: thread.history_version,
        prompt: researchPrompt(query, base, !!context), preload_skills: ['web-research'],
        max_steps: context ? 5 : 6,
        max_cost_usd: context ? 0.10 : 0.15,
        max_tool_invocations: context ? 8 : 12,
      }).then(created => {
        operation.runId = created.run_id;
        // A non-abortable SDK submission can complete after the UI has stopped waiting.
        if (operation.controller.signal.aborted) cancel();
        return created;
      }));
      execution = {
        requestId: atlasId('request'), explorationId: base.exploration_id,
        pathId: base.active_path_id!, parentOccurrenceId: ancestry.at(-1)?.occurrence_id ?? null,
        baseRevision: base.revision, runId: run.run_id, threadId: thread.thread_id,
        historyVersion: thread.history_version,
      };
      if (operation.controller.signal.aborted) cancel();
      check();
      let current = run;
      let approvalKey = '';
      while (true) {
        check();
        if (current.run_id !== execution.runId ||
          (current.thread_id && current.thread_id !== execution.threadId)) throw new Error('Run execution context mismatch');
        operation.terminal = ['completed', 'failed', 'cancelled'].includes(current.status);
        this.emit({ type: 'status', context: { ...execution }, status: current.status });
        if (current.status === 'completed') break;
        if (current.status === 'failed' || current.status === 'cancelled') {
          throw new Error(`Atlas run ${current.status}: ${current.result ?? 'no result (including budget exhaustion)'}`);
        }
        if (current.status === 'input_required') {
          if (!current.approval) throw new Error('Approval-required run has no approval batch');
          const batch = await wait(this.client.getApproval(current.approval.batch_id));
          check();
          if (batch.batch_id !== current.approval.batch_id || batch.run_id !== execution.runId ||
            batch.batch_digest !== current.approval.batch_digest) {
            throw new Error('Approval context mismatch');
          }
          const key = `${batch.batch_id}:${batch.batch_digest}:${batch.status}`;
          if (approvalKey !== key) {
            approvalKey = key;
            this.emit({ type: 'approval', context: { ...execution }, approval: batch });
          }
        }
        await this.pause(operation.controller.signal);
        current = await wait(this.client.getRun(run.run_id));
      }
      check();
      const history = await this.readHistory(thread.thread_id, check, wait);
      execution.historyVersion = history.version;
      const outputs = await this.researchOutputs(run.run_id, history.items, check, wait);
      check();
      let result;
      try { result = parseResearchResult(current.result); }
      catch (finalError) {
        // Only full successful tool messages can be fallback results, never event previews/deltas.
        for (const output of outputs) {
          try { result = parseResearchResult(output); break; } catch { /* Not a graph result. */ }
        }
        if (!result) throw finalError;
      }
      const request: AtlasExplorationRequest = {
        request_id: execution.requestId, exploration_id: execution.explorationId,
        path_id: execution.pathId, parent_occurrence_id: execution.parentOccurrenceId,
        run_id: execution.runId, thread_id: execution.threadId,
        committed_history_version: history.version, last_sequence: current.event_sequence ?? 0,
        status: 'completed',
      };
      const snapshot = projectResearch(base, result, request, !!context, outputs);
      check();
      this.store.inheritLineage(context?.snapshot, snapshot);
      this.emit({ type: 'completed', context: { ...execution }, snapshot });
      return snapshot;
    } catch (error) {
      // On malformed output, API failure, timeout or abort, do not leave a run consuming budget.
      cancel();
      if (operation.cancellation) {
        try { await waitFor(operation.cancellation, AbortSignal.timeout(10_000)); } catch (cancelError) {
          error = new AggregateError([error, cancelError], 'Atlas research failed; cancellation also failed');
        }
      }
      if (execution) this.emit({ type: 'error', context: { ...execution }, error });
      throw error;
    } finally {
      clearTimeout(deadline);
      signal?.removeEventListener('abort', abort);
      operation.controller.signal.removeEventListener('abort', cancel);
      if (this.active === operation) this.active = undefined;
    }
  }

  private pause(signal: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
      const abort = () => { clearTimeout(timer); reject(signal.reason); };
      const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, this.pollMs);
      signal.addEventListener('abort', abort, { once: true });
      if (signal.aborted) { signal.removeEventListener('abort', abort); abort(); }
    });
  }

  /** SDK's Thread type omits public history fields; narrow the public JSON at runtime. */
  private async readHistory(
    threadId: string, check: () => void, wait: <T>(promise: Promise<T>) => Promise<T>,
  ): Promise<{ version: number; items: unknown[] }> {
    let offset = 0;
    let version: number | undefined;
    const items: unknown[] = [];
    for (let page = 0; page < 100; page++) {
      const response: Thread & { items?: unknown; next_offset?: unknown } =
        await wait(this.client.getThread(threadId, { offset, history_version: version }));
      check();
      if (!Number.isSafeInteger(response.history_version) || response.history_version < 0 ||
        (version !== undefined && version !== response.history_version) || !Array.isArray(response.items)) {
        throw new Error('Invalid or changed public HaaS thread history');
      }
      version = response.history_version;
      items.push(...response.items);
      if (response.next_offset === null) return { version, items };
      if (typeof response.next_offset !== 'number' || !Number.isSafeInteger(response.next_offset) ||
        response.next_offset <= offset) throw new Error('Invalid thread history pagination');
      offset = response.next_offset;
    }
    throw new Error('Thread history page budget exceeded');
  }

  private async researchOutputs(
    runId: string, items: unknown[], check: () => void, wait: <T>(promise: Promise<T>) => Promise<T>,
  ): Promise<string[]> {
    const successful = new Set<string>();
    let after = 0;
    for (let page = 0; page < 100; page++) {
      const events = await wait(this.client.getRunEvents(runId, { after, limit: 200 }));
      check();
      for (const event of events.items) {
        const d = event.data;
        if (event.type === 'tool_result' && d.status === 'succeeded' && d.is_error === false &&
          typeof d.tool_call_id === 'string' && typeof d.tool === 'string' &&
          ['web_search', 'web_fetch_page', 'web_extract_links'].includes(d.tool)) {
          successful.add(d.tool_call_id);
        }
      }
      if (events.next_after == null || !events.items.length || events.next_after >= events.latest_sequence) {
        return items.flatMap(item => record(item) && item.role === 'tool' &&
          typeof item.tool_call_id === 'string' && successful.has(item.tool_call_id) &&
          typeof item.content === 'string' ? [item.content] : []);
      }
      if (!Number.isSafeInteger(events.next_after) || events.next_after <= after) throw new Error('Invalid event pagination');
      after = events.next_after;
    }
    throw new Error('Run event page budget exceeded');
  }

  getRunStatus(runId: string): Promise<RunResponse> { return this.client.getRun(runId); }
  async cancelRun(runId: string): Promise<RunResponse> {
    if (this.active?.runId === runId) {
      const operation = this.active;
      operation.controller.abort(new DOMException('Atlas research cancelled', 'AbortError'));
      return operation.cancellation ?? this.client.getRun(runId);
    }
    return this.client.cancelRun(runId);
  }
  getApproval(batchId: string): Promise<ApprovalBatch> { return this.client.getApproval(batchId); }
  decideApproval(batchId: string, decision: ApprovalDecision): Promise<ApprovalBatch> {
    return this.client.decideApproval(batchId, decision);
  }
  save(snapshot: AtlasExplorationSnapshot, runId?: string): Promise<AtlasSaveDescriptor> {
    return this.store.save(snapshot, runId);
  }
  load(artifactId: string): Promise<AtlasExplorationSnapshot> { return this.store.load(artifactId); }
  listSaved(runId: string): Promise<AtlasSaveDescriptor[]> { return this.store.listSaved(runId); }
}
