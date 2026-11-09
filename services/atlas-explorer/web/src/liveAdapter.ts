import { AtlasExplorationClient, getExplorationBreadcrumbs, type AtlasExplorationSnapshot } from '@haas/atlas-explorer-client';
import { HaaSClient } from '@haas/sdk';
import { approvalView } from './approverAdapter';
import type {
  AtlasApproval, AtlasResearchStatus, AtlasWorkspaceAdapter, AtlasWorkspaceData,
} from './explorationAdapter';

function workspace(snapshot: AtlasExplorationSnapshot, approvals: AtlasApproval[] = []): AtlasWorkspaceData {
  const request = snapshot.requests[snapshot.requests.length - 1];
  if (!request) throw new Error('Research snapshot has no HaaS execution context.');
  return { snapshot, runId: request.run_id, approvals };
}

/** Calls only exported Atlas client contracts. Dependency injection keeps tests offline. */
export function createLiveAtlasAdapter(client: AtlasExplorationClient): AtlasWorkspaceAdapter {
  return {
    research(input) {
      const controller = new AbortController();
      const approvals = new Map<string, AtlasApproval>();
      let observedRun = false;
      let terminal = false;
      const unsubscribe = client.observe(event => {
        observedRun = true;
        if (event.type === 'status') {
          terminal = ['completed', 'failed', 'cancelled'].includes(event.status);
          const state: AtlasResearchStatus['state'] = event.status === 'input_required' ? 'approval_required'
            : event.status === 'completed' ? 'completed' : event.status === 'cancelled' ? 'cancelled'
              : event.status === 'failed' ? 'failed' : 'researching';
          input.onStatus({
            state, message: event.status === 'input_required'
              ? 'Research paused for server approval. A separate authorized approver must review the exact request.'
              : `HaaS run ${event.status}. Server research step, cost and tool budgets apply.`,
            runId: event.context.runId,
          });
        } else if (event.type === 'approval') {
          approvals.set(event.approval.batch_id, approvalView(event.approval));
          input.onStatus({
            state: 'approval_required', message: `Server approval request: ${event.approval.status}.`,
            runId: event.context.runId, approvals: [...approvals.values()],
          });
        }
      });
      const parent = input.parent;
      const ancestry = parent ? getExplorationBreadcrumbs(parent.data.snapshot) : [];
      const selected = ancestry[ancestry.length - 1];
      const result = client.research(input.query, parent ? {
        snapshot: parent.data.snapshot, target: parent.target,
        parentOccurrenceId: selected?.parent_occurrence_id ?? null,
      } : undefined, controller.signal).then(snapshot => workspace(snapshot, [...approvals.values()]))
        .finally(unsubscribe);
      return {
        result,
        async cancel() {
          controller.abort(new DOMException('User cancelled Atlas research', 'AbortError'));
          // The client's rejection happens only after its remote cancellation
          // finishes. Do not equate AbortController with remote acknowledgement.
          try { await result; }
          catch (e) {
            if (e !== controller.signal.reason && !(e instanceof Error && e.name === 'AbortError')) throw e;
          }
          return {
            acknowledged: observedRun,
            message: !observedRun
              ? 'Cancellation requested before a run ID was received. Late submissions will be cancelled by the client; remote acknowledgement is not yet available.'
              : terminal ? 'The run was already terminal. Stopped waiting; no late result will replace the current view.'
                : 'Remote cancellation acknowledged. No late result will replace the current view.',
          };
        },
      };
    },
    save: data => client.save(data.snapshot, data.runId),
    load: async artifactId => workspace(await client.load(artifactId)),
    recent: runId => client.listSaved(runId),
    decide: async (approval, decision, reason) => approvalView(await client.decideApproval(approval.id, {
      decision, batch_digest: approval.digest, ...(reason ? { reason } : {}),
    })),
  };
}

export function createBrowserAtlasAdapter(): AtlasWorkspaceAdapter {
  // SDK appends /v1 and resolves request URLs with new URL(). The absolute
  // same-origin base is required by that public SDK; no cross-origin backend.
  // Auth and tenant are server concerns. Never read env/localStorage in browser.
  const haas = new HaaSClient({ baseUrl: window.location.origin, maxRetries: 0 });
  return createLiveAtlasAdapter(new AtlasExplorationClient(haas));
}
