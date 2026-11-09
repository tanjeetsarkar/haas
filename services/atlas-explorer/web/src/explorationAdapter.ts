import type { AtlasExplorationSnapshot, AtlasTarget } from '@haas/atlas-explorer-client';

/** UI-owned boundary. No credentials, storage implementation or guessed SDK calls. */
export interface AtlasAttribute {
  key: string;
  label: string;
  value: string | number | boolean | null | string[];
  unit?: string;
  uncertainty?: string;
}

export interface AtlasPresentation {
  entityId: string;
  attributes: AtlasAttribute[];
  checklist?: Array<{ id: string; text: string; required?: boolean; completed?: boolean }>;
  draft?: { title: string; body: string };
}

export interface AtlasApproval {
  id: string;
  runId: string;
  destination: string;
  args: Record<string, unknown>;
  digest: string;
  status: string;
  calls?: Array<{ invocationId: string; toolName: string; destination: string; args: Record<string, unknown> }>;
  expiresAt?: string;
}

export interface AtlasWorkspaceData {
  snapshot: AtlasExplorationSnapshot;
  runId: string;
  presentations?: AtlasPresentation[];
  approvals?: AtlasApproval[];
}

export interface AtlasResearchStatus {
  state: 'starting' | 'researching' | 'partial' | 'approval_required' | 'budget_exceeded' | 'completed' | 'cancelled' | 'failed';
  message: string;
  runId?: string;
  approvals?: AtlasApproval[];
}

export interface AtlasSavedSnapshot {
  artifactId: string;
  runId: string;
  createdAt?: string;
}

export interface AtlasResearchOperation {
  result: Promise<AtlasWorkspaceData>;
  /** Must cancel the remote run, not merely disconnect the browser. */
  cancel(): Promise<void | { acknowledged: boolean; message?: string }>;
}

export interface AtlasWorkspaceAdapter {
  /** Scoped research returns the canonical merged graph with parent navigation retained. */
  research(input: {
    query: string;
    parent?: { data: AtlasWorkspaceData; target: AtlasTarget };
    onStatus: (status: AtlasResearchStatus) => void;
  }): AtlasResearchOperation;
  save(data: AtlasWorkspaceData): Promise<AtlasSavedSnapshot>;
  load(artifactId: string): Promise<AtlasWorkspaceData>;
  recent(runId: string): Promise<AtlasSavedSnapshot[]>;
  decide(approval: AtlasApproval, decision: 'approve' | 'reject', reason?: string): Promise<AtlasApproval>;
}

export function describeAtlasError(error: unknown, context?: 'approval'): string {
  const status = typeof error === 'object' && error !== null && 'status' in error ? error.status : undefined;
  if (status === 401) return 'Authentication required. Connect through the authenticated same-origin HaaS proxy.';
  if (typeof status === 'number') {
    const record = (value: unknown): value is Record<string, unknown> =>
      typeof value === 'object' && value !== null && !Array.isArray(value);
    const text = (value: unknown) => typeof value === 'string' && value.trim() ? value.trim().slice(0, 2000) : undefined;
    const apiError = record(error) ? error : {};
    // Public SDK currently exposes details as a string. Also accept standard
    // problem.detail objects; never stringify arbitrary response/debug payloads.
    const detail = (record(apiError.problem) ? text(apiError.problem.detail) : undefined)
      ?? (record(apiError.details) ? text(apiError.details.detail) : text(apiError.details))
      ?? text(apiError.message);
    if (detail) return `HTTP ${status}: ${detail}${context === 'approval'
      ? ' Approval still requires an approver role and a separate authorizer; self-approval is not permitted.' : ''}`;
    if (status === 403) return context === 'approval'
      ? 'HTTP 403: Authorization denied. Approval requires an approver role and a separate authorizer; self-approval is not permitted.'
      : 'HTTP 403: The server denied this operation. Check the applicable tenant policy and access grants.';
  }
  return error instanceof Error ? error.message : 'The request failed. No success has been assumed.';
}
