import type {
  ApprovalBatch, ApprovalDecision, HaaSClient, RunResponse, RunStatus, RunStepsResponse,
  RunUsageResponse, Thread,
} from '@haas/sdk';
import type { AtlasExplorationRequest, AtlasExplorationSnapshot } from './explorationTypes';
import { getExplorationBreadcrumbs } from './explorationNavigation';
import {
  atlasId, parseResearchResult, prepareResearch, projectResearch, researchPrompt,
  type AtlasResearchContext,
} from './explorationResearch';
import { AtlasSnapshotStore, type AtlasSaveDescriptor } from './explorationPersistence';
import {
  projectProgress, type AtlasExecutionProgress, type AtlasOperationPersistence,
  projectRunTelemetry, type AtlasOperationBind, type AtlasResearchSubmission, type AtlasRunObservationResult,
  type AtlasRunTelemetry,
} from './explorationProgress';

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
  /** Stable operation correlation fields for consumers that gate stale observer events. */
  operationId?: string;
  generation?: number;
  workspaceId?: string;
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

/** Additive execution-only event channel; AtlasResearchEvent stays source compatible. */
export type AtlasExecutionEvent =
  | { type: 'submitted'; submission: AtlasResearchSubmission }
  | { type: 'progress'; progress: AtlasExecutionProgress }
  | { type: 'telemetry'; telemetry: AtlasRunTelemetry }
  | { type: 'disconnected'; context: AtlasExecutionContext; lastEventId: string; error: unknown };

export interface AtlasExplorationClientOptions {
  /** Total operation deadline, including approval wait. Default five minutes. */
  timeoutMs?: number;
  /** @deprecated Run observation uses the SDK event stream; no polling is performed. */
  pollIntervalMs?: number;
  /** Awaited 042 operation_prepared/operation_bound persistence adapter. */
  operationPersistence?: AtlasOperationPersistence;
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isClientApiFailure(value: unknown): boolean {
  return record(value) && typeof value.status === 'number' && value.status >= 400 && value.status < 500;
}

function durableEventId(id: string | undefined, event: string): number | undefined {
  if (event === 'output_delta' || event === 'output_preview' || id === undefined) return undefined;
  if (!/^\d{1,18}$/u.test(id) || !Number.isSafeInteger(Number(id))) {
    throw new Error('Invalid durable Atlas event ID');
  }
  return Number(id);
}

/** Non-stream SDK methods have no AbortSignal; stop waiting and retain late rejection handlers. */
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
  operationId?: string;
  workspaceId?: string;
  cancellation?: Promise<RunResponse>;
  terminal?: boolean;
}

/** One active selected-branch observation per instance; newer work aborts prior observation. */
export class AtlasExplorationClient {
  private readonly observers = new Set<(event: AtlasResearchEvent) => void>();
  private readonly executionObservers = new Set<(event: AtlasExecutionEvent) => void>();
  private active?: ActiveResearch;
  private readonly store: AtlasSnapshotStore;
  private readonly timeoutMs: number;
  private readonly operationPersistence?: AtlasOperationPersistence;
  private generation = 0;
  private readonly blockedWorkspaces = new Set<string>();
  private readonly pendingBindings = new Map<string, AtlasOperationBind>();

  constructor(private readonly client: HaaSClient, options: AtlasExplorationClientOptions = {}) {
    this.timeoutMs = options.timeoutMs ?? 300_000;
    this.operationPersistence = options.operationPersistence;
    if (!Number.isFinite(this.timeoutMs) || this.timeoutMs <= 0) throw new Error('Invalid Atlas research timeout');
    this.store = new AtlasSnapshotStore(client);
  }

  observe(observer: (event: AtlasResearchEvent) => void): () => void {
    this.observers.add(observer);
    return () => { this.observers.delete(observer); };
  }

  observeExecution(observer: (event: AtlasExecutionEvent) => void): () => void {
    this.executionObservers.add(observer);
    return () => { this.executionObservers.delete(observer); };
  }

  private emit(event: AtlasResearchEvent): void {
    // View errors must not change run lifecycle or prevent cancellation.
    for (const observer of this.observers) {
      try { observer(event); } catch { /* Observer owns its rendering failures. */ }
    }
  }

  private emitExecution(event: AtlasExecutionEvent): void {
    for (const observer of this.executionObservers) {
      try { observer(event); } catch { /* Observer owns its rendering failures. */ }
    }
  }

  private validReceipt(receipt: unknown, workspaceId: string): receipt is {
    workspace_id: string; mutation_id: string; revision: number; saved_at: string;
  } {
    return record(receipt) && receipt.workspace_id === workspaceId &&
      typeof receipt.mutation_id === 'string' && receipt.mutation_id.length > 0 &&
      Number.isSafeInteger(receipt.revision) && (receipt.revision as number) >= 1 &&
      typeof receipt.saved_at === 'string' && Number.isFinite(Date.parse(receipt.saved_at));
  }

  private async persistBinding(input: AtlasOperationBind): Promise<void> {
    const persistence = this.operationPersistence;
    if (!persistence) return;
    this.blockedWorkspaces.add(input.workspaceId);
    this.pendingBindings.set(input.workspaceId, input);
    const receipt = await persistence.bind(input);
    if (!this.validReceipt(receipt, input.workspaceId)) throw new Error('Invalid operation_bound persistence receipt');
    this.pendingBindings.delete(input.workspaceId);
    this.blockedWorkspaces.delete(input.workspaceId);
  }

  private async persistTerminalOutcome(
    workspaceId: string, operationId: string, status: 'completed' | 'failed' | 'cancelled',
  ): Promise<void> {
    const persistence = this.operationPersistence;
    if (!persistence) return;
    this.blockedWorkspaces.add(workspaceId);
    const receipt = await persistence.outcome({
      workspaceId, operationId, kind: 'operation_outcome',
      payload: { operation_id: operationId, state: 'terminal', status },
    });
    if (!this.validReceipt(receipt, workspaceId)) throw new Error('Invalid operation_outcome terminal receipt');
    this.blockedWorkspaces.delete(workspaceId);
  }

  /** Retry a known run's exact pending operation_bound mutation via its queue adapter. */
  async recoverOperationBinding(workspaceId: string): Promise<void> {
    const input = this.pendingBindings.get(workspaceId);
    if (!input || !this.operationPersistence) throw new Error('No known pending operation binding to recover');
    await this.persistBinding(input);
  }

  async research(
    query: string, context?: AtlasResearchContext, signal?: AbortSignal,
  ): Promise<AtlasExplorationSnapshot> {
    const base = prepareResearch(query, context);
    signal?.throwIfAborted();
    this.active?.controller.abort(new DOMException('Superseded Atlas research', 'AbortError'));
    const operation: ActiveResearch = { controller: new AbortController() };
    this.active = operation;
    const generation = ++this.generation;
    const abort = () => operation.controller.abort(signal?.reason);
    signal?.addEventListener('abort', abort, { once: true });
    const deadline = setTimeout(() => operation.controller.abort(
      new DOMException('Atlas research timed out', 'TimeoutError')), this.timeoutMs);
    let execution: AtlasExecutionContext | undefined;
    const check = () => operation.controller.signal.throwIfAborted();
    const wait = <T>(promise: Promise<T>) => waitFor(promise, operation.controller.signal);
    try {
      check();
      if (this.operationPersistence?.workspaceId &&
        this.blockedWorkspaces.has(this.operationPersistence.workspaceId)) {
        throw new Error('Workspace operation is unresolved; recover its durable binding/outcome before dispatch.');
      }
      const ancestry = context ? getExplorationBreadcrumbs(base) : [];
      const parent = context ? producerFor(base, context.parentOccurrenceId) : undefined;
      if (context && !parent) throw new Error('Selected branch has no completed HaaS execution context');
      const prompt = researchPrompt(query, base, !!context);
      const selectedContext = JSON.parse(
        prompt.split('Selected context: ')[1]?.split('\nOutput JSON Schema:')[0] ?? 'null',
      ) as unknown;
      const requestId = atlasId('request');
      const operationId = atlasId('operation');
      operation.operationId = operationId;
      operation.workspaceId = this.operationPersistence?.workspaceId;
      const thread = await wait(parent
        ? this.client.branchThread(parent.thread_id, { expected_history_version: parent.committed_history_version })
        : this.client.createThread({ metadata: { app: 'atlas-explorer', exploration_id: base.exploration_id } }));
      check();
      if (this.operationPersistence) {
        this.blockedWorkspaces.add(this.operationPersistence.workspaceId);
        const receipt = await wait(this.operationPersistence.prepare({
          workspaceId: this.operationPersistence.workspaceId,
          operationId,
          kind: 'operation_prepared',
          payload: {
            operation_id: operationId, prompt, selected_context: [selectedContext as Record<string, unknown>],
          },
        }));
        check();
        if (!this.validReceipt(receipt, this.operationPersistence.workspaceId)) {
          throw new Error('Invalid operation_prepared persistence receipt');
        }
        this.blockedWorkspaces.add(this.operationPersistence.workspaceId);
        const submitting = await wait(this.operationPersistence.outcome({
          workspaceId: this.operationPersistence.workspaceId,
          operationId,
          kind: 'operation_outcome',
          payload: { operation_id: operationId, state: 'submitting', status: 'submitting' },
        }));
        check();
        if (!this.validReceipt(submitting, this.operationPersistence.workspaceId)) {
          throw new Error('Invalid operation_outcome submitting receipt');
        }
      }
      let createRun: Promise<RunResponse> | undefined;
      let run: RunResponse;
      try {
        check();
        const submission = this.client.createRun({
          thread_id: thread.thread_id, expected_history_version: thread.history_version,
          prompt, preload_skills: ['web-research'],
          max_steps: 1000,
          max_cost_usd: context ? 0.10 : 0.15,
          max_tool_invocations: context ? 8 : 12,
        }).then(created => {
          operation.runId = created.run_id;
          return created;
        });
        createRun = submission;
        run = await wait(submission);
      } catch (submitError) {
        if (this.operationPersistence) {
          const workspaceId = this.operationPersistence.workspaceId;
          this.blockedWorkspaces.add(workspaceId);
          const unknownInput = {
            workspaceId, operationId, kind: 'operation_outcome' as const,
            payload: {
              operation_id: operationId, state: 'unknown' as const, status: 'unknown' as const,
              ...(operation.runId ? { run_id: operation.runId } : {}),
            },
          };
          let unknownPersistError: unknown;
          try {
            const receipt = await this.operationPersistence.outcome(unknownInput);
            if (!this.validReceipt(receipt, workspaceId)) throw new Error('Invalid operation_outcome unknown receipt');
          } catch (error) {
            unknownPersistError = error;
          }
          if (operation.controller.signal.aborted && createRun) {
            void createRun.then(created => {
              const lateBind: AtlasOperationBind = {
                workspaceId, operationId, kind: 'operation_bound',
                payload: { operation_id: operationId, run_id: created.run_id, thread_id: thread.thread_id },
              };
              operation.runId = created.run_id;
              return this.persistBinding(lateBind);
            }).catch(() => undefined);
          }
          if (unknownPersistError) {
            throw new AggregateError([submitError, unknownPersistError],
              'Run submission outcome is ambiguous and could not be durably recorded; workspace remains blocked');
          }
        }
        throw submitError;
      }
      execution = {
        requestId, operationId, generation, workspaceId: this.operationPersistence?.workspaceId,
        explorationId: base.exploration_id,
        pathId: base.active_path_id!, parentOccurrenceId: ancestry.at(-1)?.occurrence_id ?? null,
        baseRevision: base.revision, runId: run.run_id, threadId: thread.thread_id,
        historyVersion: thread.history_version,
      };
      if (this.operationPersistence) {
        const binding: AtlasOperationBind = {
          workspaceId: this.operationPersistence.workspaceId,
          operationId,
          kind: 'operation_bound',
          payload: {
            operation_id: operationId, run_id: run.run_id, thread_id: thread.thread_id,
          },
        };
        try {
          await wait(this.persistBinding(binding).then(() => undefined));
          check();
        } catch (bindingError) {
          // A run ID is known. If operation_bound did not commit, record it in
          // the approved unknown outcome; if it did commit, the exact bind
          // retry will recover the original receipt.
          try {
            const receipt = await this.operationPersistence.outcome({
              workspaceId: this.operationPersistence.workspaceId,
              operationId,
              kind: 'operation_outcome',
              payload: {
                operation_id: operationId, state: 'unknown', status: 'unknown', run_id: run.run_id,
              },
            });
            if (!this.validReceipt(receipt, this.operationPersistence.workspaceId)) {
              throw new Error('Invalid known-run operation_outcome receipt');
            }
          } catch (outcomeError) {
            bindingError = new AggregateError([bindingError, outcomeError],
              'Known run binding is unacknowledged; known run ID could not be durably recorded');
          }
          if (operation.controller.signal.aborted) {
            // The original promise retains this binding attempt and input.
            throw bindingError;
          }
          throw new Error('Known run is blocked until its identical operation_bound mutation receives a durable receipt.',
            { cause: bindingError });
        }
      }
      this.emitExecution({ type: 'submitted', submission: {
        context: { ...execution }, question: query, prompt, selectedContext,
      } });
      check();
      if (run.run_id !== execution.runId ||
        (run.thread_id && run.thread_id !== execution.threadId)) throw new Error('Run execution context mismatch');
      if (!['completed', 'failed', 'cancelled'].includes(run.status)) {
        this.emit({ type: 'status', context: { ...execution }, status: run.status });
      }
      let cursor = '0';
      let lastSequence = 0;
      let previewSize = 0;
      let streamFailure: unknown;
      const streamOptions = {
        signal: operation.controller.signal, lastEventId: cursor, outputDeltas: true,
      };
      try {
        for await (const event of this.client.streamEvents(run.run_id, streamOptions)) {
          check();
          const durable = durableEventId(event.id, event.event);
          if (durable !== undefined) {
            if (durable <= lastSequence) continue;
            if (durable !== lastSequence + 1) {
              throw new Error(`Atlas event replay gap: expected ${lastSequence + 1}, received ${durable}`);
            }
          }
          if (event.event === 'stream_gap' || event.event === 'stream_closed') {
            throw new Error(`Atlas event stream ${event.event}`);
          }
          const data = record(event.data) ? event.data : {};
          const progress = projectProgress(execution, event.event, data, durable);
          if (event.event === 'output_delta' || event.event === 'output_preview') {
            const preview = typeof data.delta === 'string' ? data.delta : progress.preview ?? '';
            const allowed = Math.max(0, 4000 - previewSize);
            progress.preview = preview.slice(0, allowed);
            progress.previewTruncated = progress.previewTruncated || preview.length > allowed;
            progress.provisional = true;
            previewSize += progress.preview.length;
          }
          this.emitExecution({ type: 'progress', progress });
          if (durable !== undefined) {
            lastSequence = durable;
            cursor = String(durable);
          }
        }
      } catch (error) {
        if (operation.controller.signal.aborted) throw error;
        if (isClientApiFailure(error)) throw error;
        streamFailure = error;
      }
      check();
      // One authoritative reconciliation after EOF/error; never a timed run GET loop.
      const current = await wait(this.client.getRun(run.run_id));
      check();
      if (current.run_id !== execution.runId ||
        (current.thread_id && current.thread_id !== execution.threadId)) throw new Error('Run execution context mismatch');
      operation.terminal = ['completed', 'failed', 'cancelled'].includes(current.status);
      this.emit({ type: 'status', context: { ...execution }, status: current.status });
      await this.emitRunTelemetry(execution, current, check, wait);
      if (this.operationPersistence && operation.terminal) {
        await wait(this.persistTerminalOutcome(
          this.operationPersistence.workspaceId, execution.operationId!,
          current.status as 'completed' | 'failed' | 'cancelled',
        ));
        check();
      }
      if (current.status === 'input_required') {
        if (!current.approval) throw new Error('Approval-required run has no approval batch');
        const batch = await wait(this.client.getApproval(current.approval.batch_id));
        check();
        if (batch.batch_id !== current.approval.batch_id || batch.run_id !== execution.runId ||
          batch.batch_digest !== current.approval.batch_digest) throw new Error('Approval context mismatch');
        this.emit({ type: 'approval', context: { ...execution }, approval: batch });
        this.emitExecution({ type: 'disconnected', context: { ...execution }, lastEventId: cursor,
          error: new Error('Run is paused for approval; resume observation after an authorized decision') });
        throw new Error(`Atlas run is pending approval; reconnect to run ${run.run_id} after an authorized decision`);
      }
      if (current.status !== 'completed') {
        const error = streamFailure ?? new Error(
          `Run remains ${current.status}; resume observation with the same run and cursor`,
        );
        if (!operation.terminal) {
          this.emitExecution({ type: 'disconnected', context: { ...execution }, lastEventId: cursor, error });
          throw error;
        }
        throw new Error(`Atlas run ${current.status}: ${current.result ?? 'no result (including budget exhaustion)'}`);
      }
      if (streamFailure) {
        this.emitExecution({ type: 'disconnected', context: { ...execution }, lastEventId: cursor, error: streamFailure });
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
      if (this.active === operation) this.active = undefined;
    }
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

  private async emitRunTelemetry(
    context: AtlasExecutionContext,
    run: RunResponse,
    check: () => void,
    wait: <T>(promise: Promise<T>) => Promise<T>,
  ): Promise<void> {
    let steps: RunStepsResponse | undefined;
    let usage: RunUsageResponse | undefined;
    const optional = this.client as HaaSClient & {
      getRunSteps?: (runId: string) => Promise<RunStepsResponse>;
      getRunUsage?: (runId: string) => Promise<RunUsageResponse>;
    };
    const [stepResult, usageResult] = await Promise.all([
      typeof optional.getRunSteps === 'function'
        ? wait(optional.getRunSteps(run.run_id)).then(value => value, () => undefined)
        : Promise.resolve(undefined),
      typeof optional.getRunUsage === 'function'
        ? wait(optional.getRunUsage(run.run_id)).then(value => value, () => undefined)
        : Promise.resolve(undefined),
    ]);
    check();
    steps = stepResult;
    usage = usageResult;
    this.emitExecution({ type: 'telemetry', telemetry: projectRunTelemetry(context, run, steps, usage) });
  }

  /**
   * Resume telemetry only for an already-known run. Never creates/cancels a run,
   * projects a graph, or advances a cursor for previews or unapplied events.
   */
  async resumeObservation(
    context: AtlasExecutionContext, lastEventId = '0', signal?: AbortSignal,
  ): Promise<AtlasRunObservationResult> {
    if (!/^\d{1,18}$/u.test(lastEventId) || !Number.isSafeInteger(Number(lastEventId))) {
      throw new Error('Invalid Atlas durable event cursor');
    }
    signal?.throwIfAborted();
    let cursor = String(Number(lastEventId));
    let sequence = Number(lastEventId);
    let failure: unknown;
    let previewSize = 0;
    const streamOptions = { signal, lastEventId: cursor, outputDeltas: true };
    try {
      for await (const event of this.client.streamEvents(context.runId, streamOptions)) {
        signal?.throwIfAborted();
        const id = durableEventId(event.id, event.event);
        if (id !== undefined) {
          if (id <= sequence) continue;
          if (id !== sequence + 1) throw new Error(`Atlas event replay gap: expected ${sequence + 1}, received ${id}`);
        }
        if (event.event === 'stream_gap' || event.event === 'stream_closed') {
          throw new Error(`Atlas event stream ${event.event}`);
        }
        const data = record(event.data) ? event.data : {};
        const progress = projectProgress(context, event.event, data, id);
        if (event.event === 'output_delta' || event.event === 'output_preview') {
          const value = typeof data.delta === 'string' ? data.delta : progress.preview ?? '';
          const allowed = Math.max(0, 4000 - previewSize);
          progress.preview = value.slice(0, allowed);
          progress.previewTruncated = progress.previewTruncated || value.length > allowed;
          progress.provisional = true;
          previewSize += progress.preview.length;
        }
        this.emitExecution({ type: 'progress', progress });
        if (id !== undefined) {
          sequence = id;
          cursor = String(id);
        }
      }
    } catch (error) {
      if (signal?.aborted) throw error;
      if (isClientApiFailure(error)) throw error;
      failure = error;
    }
    signal?.throwIfAborted();
    const current = signal
      ? await waitFor(this.client.getRun(context.runId), signal)
      : await this.client.getRun(context.runId);
    signal?.throwIfAborted();
    if (current.run_id !== context.runId ||
      (current.thread_id && current.thread_id !== context.threadId)) throw new Error('Run execution context mismatch');
    const terminal = ['completed', 'failed', 'cancelled'].includes(current.status);
    this.emit({ type: 'status', context: { ...context }, status: current.status });
    if (terminal && this.operationPersistence) {
      if (!context.operationId || context.workspaceId !== this.operationPersistence.workspaceId) {
        throw new Error('Cannot persist terminal status without matching workspace and operation IDs');
      }
      if (current.status === 'completed' || current.status === 'failed' || current.status === 'cancelled') {
        const status = current.status;
        const persist = () => this.persistTerminalOutcome(context.workspaceId!, context.operationId!, status);
        if (signal) await waitFor(persist(), signal);
        else await persist();
      }
    }
    const optional = this.client as HaaSClient & {
      getRunSteps?: (runId: string) => Promise<RunStepsResponse>;
      getRunUsage?: (runId: string) => Promise<RunUsageResponse>;
    };
    const [steps, usage] = await Promise.all([
      typeof optional.getRunSteps === 'function'
        ? (signal ? await waitFor(optional.getRunSteps(context.runId), signal).catch(() => undefined)
          : await optional.getRunSteps(context.runId).catch(() => undefined))
        : undefined,
      typeof optional.getRunUsage === 'function'
        ? (signal ? await waitFor(optional.getRunUsage(context.runId), signal).catch(() => undefined)
          : await optional.getRunUsage(context.runId).catch(() => undefined))
        : undefined,
    ]);
    signal?.throwIfAborted();
    this.emitExecution({ type: 'telemetry', telemetry: projectRunTelemetry(context, current, steps, usage) });
    if (current.status === 'input_required') {
      if (!current.approval) throw new Error('Approval-required run has no approval batch');
      const approval = signal
        ? await waitFor(this.client.getApproval(current.approval.batch_id), signal)
        : await this.client.getApproval(current.approval.batch_id);
      signal?.throwIfAborted();
      if (approval.run_id !== context.runId || approval.batch_digest !== current.approval.batch_digest) {
        throw new Error('Approval context mismatch');
      }
      this.emit({ type: 'approval', context: { ...context }, approval });
    }
    const disconnected = !!failure || !terminal;
    if (disconnected) {
      this.emitExecution({ type: 'disconnected', context: { ...context }, lastEventId: cursor,
        error: failure ?? new Error(`Run remains ${current.status}; resume observation with the same run and cursor`) });
    }
    return { context: { ...context }, status: current.status, lastEventId: cursor, terminal, disconnected };
  }

  getRunStatus(runId: string): Promise<RunResponse> { return this.client.getRun(runId); }
  async cancelRun(runId: string): Promise<RunResponse> {
    if (this.active?.runId === runId) {
      const operation = this.active;
      if (operation.cancellation) return operation.cancellation;
      operation.cancellation = this.client.cancelRun(runId).then(async response => {
        if (this.operationPersistence && operation.workspaceId && operation.operationId &&
          ['completed', 'failed', 'cancelled'].includes(response.status)) {
          await this.persistTerminalOutcome(
            operation.workspaceId, operation.operationId,
            response.status as 'completed' | 'failed' | 'cancelled',
          );
        }
        return response;
      });
      operation.controller.abort(new DOMException('Atlas research cancelled', 'AbortError'));
      return operation.cancellation;
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
