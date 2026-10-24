/**
 * HaaS Atlas Session Manager
 * 
 * Provides high-level API for HaaS Atlas layered exploration:
 * - Root exploration (Layer 1)
 * - Drill-down layer exploration (Layer K -> K+1) via thread branching
 * - SSE event streaming with Last-Event-ID cursor preservation
 */

import { HaaSClient } from '../client';
import type { 
  RunCreateRequest, 
  RunResponse, 
  RunEvent, 
  ThreadBranchRequest, 
  ThreadBranchResponse,
  Thread,
  RunEventsParams,
  SSEEvent
} from '../types';

export interface DrilldownRequest {
  threadId: string;
  parentBranchVersion: number;
  parentNodeId: string;
  dimensionKey: string;
  prompt: string;
}

export interface AtlasLayerNode {
  node_id: string;
  canonical_key: string;
  title: string;
  subtitle?: string | null;
  component_type: 'entity_card' | 'comparison_row' | 'pricing_tier_card' | 'specialist_profile' | 'checklist_card';
  attributes: Record<string, string | number | boolean | null>;
  provenance: {
    trust_tier: 'verified_official' | 'extracted_document' | 'aggregated_directory' | 'estimated_range' | 'unverified';
    source_urls: string[];
    observed_at: string;
    citation_snippet?: string | null;
  };
}

export interface AtlasLayerEnvelope {
  schema_version: 'atlas-layer.v1';
  layer_depth: number;
  parent_node_id: string | null;
  domain_category: 'healthcare' | 'education' | 'government' | 'commerce' | 'general';
  nodes: AtlasLayerNode[];
  available_drilldowns: Array<{
    dimension_key: string;
    label: string;
    prompt_template: string;
    recommended_skills: string[];
  }>;
  available_actions: Array<{
    action_id: string;
    label: string;
    tool_name: string;
    requires_approval: boolean;
  }>;
}

export interface AtlasRunEvent extends RunEvent {
  type: string;
  data: {
    // Standard run events
    run_completed?: { result: string };
    run_error?: { error: string };
    hitl_pending?: {
      action_digest: string;
      destination: string;
      args: Record<string, unknown>;
    };
    hitl_approved?: { action_digest: string };
    tool_dispatched?: { tool_name: string; invocation_id: string };
    tool_result?: { tool_name: string; invocation_id: string; result: string };
    // Atlas-specific output deltas
    output_delta?: {
      envelope: AtlasLayerEnvelope;
      progress?: number;
    };
  };
}

export type AtlasEventHandler = (event: AtlasRunEvent) => void;

/**
 * AtlasSessionManager handles the complete Atlas exploration flow:
 * 1. Start root exploration on a fresh thread
 * 2. Drill down through layers via thread branching
 * 3. Stream SSE events for progressive UI updates
 */
export class AtlasSessionManager {
  constructor(private readonly client: HaaSClient) {}

  /**
   * Starts Layer 1 (root) exploration on a fresh HaaS Thread using the published
   * `agent/atlas-explorer` revision.
   * 
   * @param query - User's natural language query (e.g., "Cancer hospitals in Mumbai")
   * @param onEvent - Callback for each SSE event received
   * @returns Thread ID, Run ID, and unsubscribe function
   */
  async startRootExploration(
    query: string,
    onEvent: AtlasEventHandler
  ): Promise<{ threadId: string; runId: string; unsubscribe: () => void }> {
    // Create a new thread for this exploration
    const thread = await this.client.createThread({
      metadata: { app: 'haas-atlas', root_query: query },
    });

    // Submit a run with the atlas-explorer skill and supporting skills
    const run = await this.client.createRun({
      thread_id: thread.thread_id,
      prompt: `Layer 1 Root Query: ${query}. Emit atlas-layer.v1 JSON.`,
      preload_skills: ['atlas-explorer', 'web-research', 'data-analyst'],
      max_steps: 6,
      max_cost_usd: 0.15,
      max_tool_invocations: 12,
    });

    // Start streaming events
    const unsubscribe = this.streamRunEvents(run.run_id, onEvent);

    return {
      threadId: thread.thread_id,
      runId: run.run_id,
      unsubscribe,
    };
  }

  /**
   * Executes a Layer K -> Layer K+1 drill-down by branching the HaaS Thread
   * at the exact committed version of Layer K so sibling explorations never
   * pollute the context window.
   * 
   * @param req - Drilldown request with parent thread, version, node, and dimension
   * @param onEvent - Callback for each SSE event received
   * @returns Branched thread ID, Run ID, and unsubscribe function
   */
  async drillDownLayer(
    req: DrilldownRequest,
    onEvent: AtlasEventHandler
  ): Promise<{ branchedThreadId: string; runId: string; unsubscribe: () => void }> {
    // 1. Branch the thread at parentBranchVersion using HaaS's native /v1/threads API
    const branchedThread = await this.client.branchThread(req.threadId, {
      expected_history_version: req.parentBranchVersion,
    });

    // 2. Submit a tightly budgeted run on the branched thread
    const run = await this.client.createRun({
      thread_id: branchedThread.thread_id,
      prompt: `[parent_node_id=${req.parentNodeId}] [dimension=${req.dimensionKey}] ${req.prompt}`,
      preload_skills: ['atlas-explorer', 'web-research', 'data-analyst'],
      max_steps: 5,
      max_cost_usd: 0.10,
      max_tool_invocations: 8,
    });

    // 3. Start streaming events
    const unsubscribe = this.streamRunEvents(run.run_id, onEvent);

    return {
      branchedThreadId: branchedThread.thread_id,
      runId: run.run_id,
      unsubscribe,
    };
  }

  /**
   * Streams run events with automatic reconnection and Last-Event-ID cursor preservation.
   * 
   * @param runId - Run ID to stream events for
   * @param onEvent - Callback for each event
   * @returns Unsubscribe function to stop streaming
   */
  private streamRunEvents(runId: string, onEvent: AtlasEventHandler): () => void {
    let cancelled = false;
    let lastEventId = '0';

    const stream = async () => {
      while (!cancelled) {
        try {
          for await (const event of this.client.streamEvents(runId, {
            lastEventId,
            outputDeltas: true,
          })) {
            if (cancelled) break;
            
            // Update cursor for reconnection
            if (event.id) {
              lastEventId = event.id;
            }

            // Transform to AtlasRunEvent for type safety
            const atlasEvent: AtlasRunEvent = {
              sequence: 0,
              type: event.event,
              data: event.data as AtlasRunEvent['data'],
              id: event.id,
            };

            onEvent(atlasEvent);

            // Check for terminal events
            if (['run_completed', 'run_error', 'run_cancelled', 'stream_closed'].includes(event.event)) {
              return;
            }
          }
        } catch (error) {
          if (cancelled) break;
          
          // On connection error, wait and reconnect with Last-Event-ID
          console.warn(`Atlas stream error for run ${runId}, reconnecting...`, error);
          await new Promise(resolve => setTimeout(resolve, 1000));
        }
      }
    };

    // Start streaming in background
    stream();

    // Return unsubscribe function
    return () => {
      cancelled = true;
    };
  }

  /**
   * Resumes a paused HITL run after human approval.
   * Uses the separate approver principal (dual-principal pattern).
   * 
   * @param runId - Run ID to resume
   * @param approved - Whether the action was approved
   * @param actionDigest - SHA256 digest of the approved action (from hitl_pending event)
   * @param rejectionReason - Optional reason if rejected
   */
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

  /**
   * Gets the current run status.
   */
  async getRunStatus(runId: string): Promise<RunResponse> {
    return this.client.getRun(runId);
  }

  /**
   * Cancels a running exploration.
   */
  async cancelRun(runId: string): Promise<RunResponse> {
    return this.client.cancelRun(runId);
  }
}

/**
 * Creates an AtlasSessionManager with the given HaaS client.
 */
export function createAtlasSessionManager(client: HaaSClient): AtlasSessionManager {
  return new AtlasSessionManager(client);
}