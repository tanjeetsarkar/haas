import type { RunStatus, RunUsageResponse, RunStepsResponse } from '@haas/sdk';
import type { AtlasExecutionContext } from './explorationClient';

/** Volatile run telemetry. It is not graph data, a durable journal, or a cursor. */
export interface AtlasExecutionProgress {
  context: AtlasExecutionContext;
  eventId?: number;
  event: string;
  occurredAt?: string | number;
  status?: RunStatus | string;
  phase?: string;
  step?: number;
  model?: string;
  activeSkills?: string[];
  toolCallId?: string;
  toolName?: string;
  queue?: string;
  preview?: string;
  previewTruncated?: boolean;
  provisional?: true;
}

export interface AtlasResearchSubmission {
  context: AtlasExecutionContext;
  question: string;
  /** Exact application-generated prompt sent to createRun. */
  prompt: string;
  /** Exact selected-context value embedded in that prompt. */
  selectedContext: unknown;
}

export interface AtlasRunObservationResult {
  context: AtlasExecutionContext;
  status: RunStatus;
  /** Last durable numeric SSE ID successfully projected; previews never advance it. */
  lastEventId: string;
  terminal: boolean;
  disconnected: boolean;
}

export interface AtlasObservedStep {
  stepId: string;
  attempt: number;
  status: string;
  startedAt: number;
  completedAt?: number;
  tools: Array<{ toolName: string; toolCallId?: string }>;
}

export interface AtlasRunTelemetry {
  context: AtlasExecutionContext;
  status: RunStatus;
  eventSequence?: number;
  stepCount?: number;
  costUsd?: number;
  /** Omitted plan/arguments; never inferred chain-of-thought or credentials. */
  steps?: AtlasObservedStep[];
  usage?: Pick<RunUsageResponse, 'model_tokens' | 'tool_invocations' | 'egress_bytes' | 'artifact_bytes' | 'cost_usd'>;
}

export function projectRunTelemetry(
  context: AtlasExecutionContext,
  run: { status: RunStatus; event_sequence?: number; step_count?: number; total_cost_usd?: number },
  steps?: RunStepsResponse,
  usage?: RunUsageResponse,
): AtlasRunTelemetry {
  const telemetry: AtlasRunTelemetry = { context: { ...context }, status: run.status };
  if (Number.isSafeInteger(run.event_sequence)) telemetry.eventSequence = run.event_sequence;
  if (Number.isSafeInteger(run.step_count)) telemetry.stepCount = run.step_count;
  if (typeof run.total_cost_usd === 'number' && Number.isFinite(run.total_cost_usd)) {
    telemetry.costUsd = run.total_cost_usd;
  }
  if (steps && Array.isArray(steps.items)) {
    telemetry.steps = steps.items.slice(0, 100).map(step => ({
      stepId: step.step_id, attempt: step.attempt, status: step.status,
      startedAt: step.started_at, completedAt: step.completed_at,
      tools: (step.tool_calls ?? []).slice(0, 100).map(tool => ({
        toolName: tool.tool_name.slice(0, 160), toolCallId: tool.invocation_id.slice(0, 160),
      })),
    }));
  }
  if (usage) {
    telemetry.usage = {
      model_tokens: usage.model_tokens, tool_invocations: usage.tool_invocations,
      egress_bytes: usage.egress_bytes, artifact_bytes: usage.artifact_bytes, cost_usd: usage.cost_usd,
    };
  }
  return telemetry;
}

export interface AtlasOperationReceipt {
  workspace_id: string;
  mutation_id: string;
  revision: number;
  saved_at: string;
}

/** Mutation kinds and operation states from the approved 042 workspace protocol. */
export type AtlasWorkspaceMutationKind =
  | 'draft_checkpoint' | 'query_submitted' | 'selection_changed' | 'branch_committed'
  | 'comparison_changed' | 'action_draft_changed' | 'operation_prepared'
  | 'operation_bound' | 'operation_outcome';
export type AtlasOperationState = 'prepared' | 'submitting' | 'bound' | 'unknown' | 'terminal';

export interface AtlasOperationPrepare {
  workspaceId: string;
  operationId: string;
  kind: 'operation_prepared';
  payload: {
    operation_id: string;
    prompt: string;
    selected_context: Array<Record<string, unknown>>;
  };
}

export interface AtlasOperationOutcome {
  workspaceId: string;
  operationId: string;
  kind: 'operation_outcome';
  payload: {
    operation_id: string;
    state: 'submitting' | 'unknown' | 'terminal';
    status: 'submitting' | 'unknown' | 'completed' | 'failed' | 'cancelled';
    /** Accepted by the 042 contract only for state=unknown. */
    run_id?: string;
  };
}

export interface AtlasOperationBind {
  workspaceId: string;
  operationId: string;
  kind: 'operation_bound';
  payload: {
    operation_id: string;
    run_id: string;
    thread_id?: string;
  };
}

/**
 * Awaited adapter to the 042 mutation queue. It supplies the envelope and
 * returns a server receipt. bind() must retain/retry its exact queued mutation
 * (same mutation ID/body) until a receipt is known; it must not enqueue a new
 * mutation body after an ambiguous response.
 */
export interface AtlasOperationPersistence {
  workspaceId: string;
  prepare(input: AtlasOperationPrepare): Promise<AtlasOperationReceipt>;
  outcome(input: AtlasOperationOutcome): Promise<AtlasOperationReceipt>;
  bind(input: AtlasOperationBind): Promise<AtlasOperationReceipt>;
}

/** Keep observer previews bounded and avoid exposing common credential forms. */
export function boundedPreview(value: unknown, limit = 1200): { text: string; truncated: boolean } {
  if (typeof value !== 'string') return { text: '', truncated: false };
  const redacted = value
    .replace(/\bBearer\s+[A-Za-z0-9._~+/-]+=*/giu, 'Bearer [redacted]')
    .replace(/\b(api[_-]?key|token|secret|password)\b(\s*[:=]\s*)["']?[^,\s"']+/giu, '$1$2[redacted]');
  return { text: redacted.slice(0, limit), truncated: redacted.length > limit };
}

/** Only fields used by public run/event telemetry are projected from untrusted event data. */
export function projectProgress(
  context: AtlasExecutionContext, event: string, data: Record<string, unknown>, eventId?: number,
): AtlasExecutionProgress {
  const result: AtlasExecutionProgress = { context: { ...context }, event, eventId };
  for (const key of ['status', 'phase', 'model', 'queue', 'tool_name', 'tool_call_id'] as const) {
    const value = data[key];
    if (typeof value === 'string') {
      if (key === 'tool_name') result.toolName = value.slice(0, 160);
      else if (key === 'tool_call_id') result.toolCallId = value.slice(0, 160);
      else if (key === 'model') result.model = value.slice(0, 160);
      else if (key === 'queue') result.queue = value.slice(0, 100);
      else if (key === 'phase') result.phase = value.slice(0, 120);
      else result.status = value;
    }
  }
  const step = data.step;
  if (typeof step === 'number' && Number.isSafeInteger(step)) result.step = step;
  const skills = data.active_skills;
  if (Array.isArray(skills)) {
    result.activeSkills = skills.filter((skill): skill is string => typeof skill === 'string')
      .slice(0, 32).map(skill => skill.slice(0, 120));
  }
  const timestamp = data.timestamp ?? data.occurred_at ?? data.created_at;
  if (typeof timestamp === 'string' || typeof timestamp === 'number') result.occurredAt = timestamp;
  const previewValue = data.preview ?? data.output ?? data.content ?? data.message;
  if (typeof previewValue === 'string') {
    const preview = boundedPreview(previewValue);
    result.preview = preview.text;
    if (preview.truncated) result.previewTruncated = true;
  }
  return result;
}
