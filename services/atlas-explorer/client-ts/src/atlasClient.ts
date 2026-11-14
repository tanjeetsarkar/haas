/**
 * HaaS Atlas Session Manager
 *
 * Provides high-level API for HaaS Atlas layered exploration:
 * root exploration, thread-branch drill-down, and resumable SSE streaming.
 */
import { HaaSClient, type RunResponse } from '@haas/sdk';
import type { AtlasEventHandler, AtlasRunEvent, DrilldownRequest } from './types';

/**
 * AtlasSessionManager handles root exploration, drill-downs via thread
 * branching, and event streaming for progressive UI updates.
 */
export class AtlasSessionManager {
  constructor(private readonly client: HaaSClient) {}

  /**
   * Starts Layer 1 exploration on a fresh HaaS thread.
   * @returns Thread ID, run ID, and unsubscribe function.
   */
  async startRootExploration(
    query: string,
    onEvent: AtlasEventHandler
  ): Promise<{ threadId: string; runId: string; unsubscribe: () => void }> {
    const thread = await this.client.createThread({
      metadata: { app: 'haas-atlas', root_query: query },
    });
    const run = await this.client.createRun({
      thread_id: thread.thread_id,
      prompt: `Layer 1 Root Query: ${query}. Emit atlas-layer.v1 JSON.`,
      preload_skills: ['atlas-explorer', 'web-research', 'data-analyst'],
      max_steps: 1000,
      max_cost_usd: 0.15,
      max_tool_invocations: 12,
    });
    const unsubscribe = this.streamRunEvents(run.run_id, onEvent);
    return { threadId: thread.thread_id, runId: run.run_id, unsubscribe };
  }

  /**
   * Executes a Layer K -> K+1 drill-down at the requested committed version.
   * @returns Branched thread ID, run ID, and unsubscribe function.
   */
  async drillDownLayer(
    req: DrilldownRequest,
    onEvent: AtlasEventHandler
  ): Promise<{ branchedThreadId: string; runId: string; unsubscribe: () => void }> {
    const branchedThread = await this.client.branchThread(req.threadId, {
      expected_history_version: req.parentBranchVersion,
    });
    const run = await this.client.createRun({
      thread_id: branchedThread.thread_id,
      prompt: `[parent_node_id=${req.parentNodeId}] [dimension=${req.dimensionKey}] ${req.prompt}`,
      preload_skills: ['atlas-explorer', 'web-research', 'data-analyst'],
      max_steps: 1000,
      max_cost_usd: 0.10,
      max_tool_invocations: 8,
    });
    const unsubscribe = this.streamRunEvents(run.run_id, onEvent);
    return { branchedThreadId: branchedThread.thread_id, runId: run.run_id, unsubscribe };
  }

  /**
   * Streams events, preserving the last event ID when reconnecting.
   * @returns Unsubscribe function to stop streaming.
   */
  private streamRunEvents(runId: string, onEvent: AtlasEventHandler): () => void {
    let cancelled = false;
    let lastEventId = '0';
    const stream = async () => {
      while (!cancelled) {
        try {
          for await (const event of this.client.streamEvents(runId, { lastEventId, outputDeltas: true })) {
            if (cancelled) break;
            if (event.id) lastEventId = event.id;
            const atlasEvent: AtlasRunEvent = {
              sequence: 0,
              type: event.event,
              data: event.data as AtlasRunEvent['data'],
              id: event.id,
            };
            onEvent(atlasEvent);
            if (['run_completed', 'run_error', 'run_cancelled', 'stream_closed'].includes(event.event)) return;
          }
        } catch (error) {
          if (cancelled) break;
          console.warn(`Atlas stream error for run ${runId}, reconnecting...`, error);
          await new Promise(resolve => setTimeout(resolve, 1000));
        }
      }
    };
    stream();
    return () => { cancelled = true; };
  }

  /** Resumes a paused HITL run after human approval or rejection. */
  async resumeHitlRun(
    runId: string,
    approved: boolean,
    actionDigest: string,
    rejectionReason?: string
  ): Promise<RunResponse> {
    return this.client.resumeRun(runId, approved, {
      rejectionReason,
      batch_digest: actionDigest,
    });
  }

  /** Gets the current run status. */
  async getRunStatus(runId: string): Promise<RunResponse> {
    return this.client.getRun(runId);
  }

  /** Cancels a running exploration. */
  async cancelRun(runId: string): Promise<RunResponse> {
    return this.client.cancelRun(runId);
  }
}

/** Creates an AtlasSessionManager with the given HaaS client. */
export function createAtlasSessionManager(client: HaaSClient): AtlasSessionManager {
  return new AtlasSessionManager(client);
}
