import { AtlasExplorationClient, getExplorationBreadcrumbs, type AtlasExplorationSnapshot,
  type AtlasOperationPersistence } from '@haas/atlas-explorer-client';
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
export function createLiveAtlasAdapter(client: AtlasExplorationClient,
  createPersistentClient?: (persistence: AtlasOperationPersistence) => AtlasExplorationClient): AtlasWorkspaceAdapter {
  return {
    async resume(input) {
      if (!createPersistentClient) throw new Error('Observation recovery unavailable. No run was submitted.');
      const observer = createPersistentClient(input.persistence);
      const unsubscribe = observer.observeExecution(input.onExecution);
      try { return await observer.resumeObservation(input.context, input.lastEventId, input.signal); }
      finally { unsubscribe(); }
    },
    research(input) {
      if (input.operationPersistence) {
        if (!createPersistentClient) throw new Error('Durable operation adapter unavailable. Research was not dispatched.');
        return createLiveAtlasAdapter(createPersistentClient(input.operationPersistence))
          .research({ ...input, operationPersistence: undefined });
      }
      const controller = new AbortController();
      const approvals = new Map<string, AtlasApproval>();
      let observedRun = false;
      let observedRunId: string | undefined;
      let cancellation: ReturnType<AtlasExplorationClient['cancelRun']> | undefined;
      let terminal = false;
      const unsubscribeExecution = client.observeExecution(event => input.onExecution?.(event));
      const unsubscribe = client.observe(event => {
        observedRun = true;
        observedRunId = event.context.runId;
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
        .finally(() => { unsubscribe(); unsubscribeExecution(); });
      return {
        result,
        detach() { controller.abort(new DOMException('Detached Atlas observation', 'AbortError')); },
        async cancel() {
          if (observedRunId && !terminal) {
            const response = await (cancellation ??= client.cancelRun(observedRunId));
            controller.abort(new DOMException('User cancelled Atlas research', 'AbortError'));
            return { acknowledged: response.status === 'cancelled',
              message: `Server cancellation response: ${response.status}.` };
          }
          controller.abort(new DOMException('User cancelled Atlas research', 'AbortError'));
          // The client's rejection happens only after its remote cancellation
          // finishes. Do not equate AbortController with remote acknowledgement.
          try { await result; }
          catch (e) {
            if (e !== controller.signal.reason && !(e instanceof Error && e.name === 'AbortError')) throw e;
          }
          return {
            acknowledged: observedRun && terminal,
            message: !observedRun
              ? 'Cancellation requested before a run ID was received. Remote cancellation is unconfirmed; recorded submission intent requires recovery.'
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
  return createLiveAtlasAdapter(new AtlasExplorationClient(haas),
    persistence => new AtlasExplorationClient(haas, { operationPersistence: persistence }));
}
