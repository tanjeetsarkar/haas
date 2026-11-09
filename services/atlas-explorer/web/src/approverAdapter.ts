import { HaaSClient, type ApprovalBatch } from '@haas/sdk';
import type { AtlasApproval, AtlasWorkspaceAdapter } from './explorationAdapter';

export interface AtlasApproverIdentity {
  principalId: string;
  tenantId: string;
  roles: string[];
}

export interface AtlasApproverAdapter extends Pick<AtlasWorkspaceAdapter, 'decide'> {
  identity(): Promise<AtlasApproverIdentity>;
  getApproval(batchId: string): Promise<AtlasApproval>;
}

export function approvalView(batch: ApprovalBatch): AtlasApproval {
  return {
    id: batch.batch_id, runId: batch.run_id, digest: batch.batch_digest, status: batch.status,
    destination: batch.tool_calls.map(call => call.destination).join('\n'),
    args: Object.fromEntries(batch.tool_calls.map(call => [call.invocation_id, call.arguments])),
    calls: batch.tool_calls.map(call => ({
      invocationId: call.invocation_id, toolName: call.tool_name, destination: call.destination, args: call.arguments,
    })),
    expiresAt: batch.expires_at,
  };
}

export function createApproverAdapter(client: Pick<HaaSClient, 'getApproval' | 'decideApproval'>,
  readIdentity: () => Promise<AtlasApproverIdentity>): AtlasApproverAdapter {
  return {
    identity: readIdentity,
    getApproval: async batchId => approvalView(await client.getApproval(batchId)),
    decide: async (approval, decision, reason) => approvalView(await client.decideApproval(approval.id, {
      decision, batch_digest: approval.digest, ...(reason ? { reason } : {}),
    })),
  };
}

export function createBrowserApproverAdapter(): AtlasApproverAdapter {
  // This page uses its own operator window's origin. No credential selection,
  // cross-origin requests, browser key input or client-side identity switching.
  const client = new HaaSClient({ baseUrl: window.location.origin, maxRetries: 0 });
  return createApproverAdapter(client, async () => {
    const response = await fetch('/v1/me', { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    const body: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string'
        ? body.detail : 'Approver identity could not be authenticated.';
      throw Object.assign(new Error(detail), { status: response.status, details: detail });
    }
    if (!body || typeof body !== 'object' || !('principal_id' in body) || typeof body.principal_id !== 'string'
      || !('tenant_id' in body) || typeof body.tenant_id !== 'string'
      || !('roles' in body) || !Array.isArray(body.roles) || !body.roles.every(role => typeof role === 'string')) {
      throw new Error('Invalid authenticated identity response. Approval review is unavailable.');
    }
    return { principalId: body.principal_id, tenantId: body.tenant_id, roles: body.roles };
  });
}
