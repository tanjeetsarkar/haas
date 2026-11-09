import type { RunEvent } from '@haas/sdk';

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
  component_type:
    | 'entity_card'
    | 'comparison_row'
    | 'pricing_tier_card'
    | 'specialist_profile'
    | 'checklist_card';
  attributes: Record<string, string | number | boolean | null>;
  provenance: {
    trust_tier:
      | 'verified_official'
      | 'extracted_document'
      | 'aggregated_directory'
      | 'estimated_range'
      | 'unverified';
    source_urls: string[];
    observed_at: string;
    citation_snippet?: string | null;
  };
}

export interface AtlasLayerEnvelope {
  schema_version: 'atlas-layer.v1';
  layer_depth: number;
  parent_node_id: string | null;
  domain_category:
    | 'healthcare'
    | 'education'
    | 'government'
    | 'commerce'
    | 'general';
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
    output_delta?: {
      envelope: AtlasLayerEnvelope;
      progress?: number;
    };
  };
}

export type AtlasEventHandler = (event: AtlasRunEvent) => void;
