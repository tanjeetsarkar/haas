/** Additive v2 contract. Parse untrusted values with parseExplorationSnapshot. */
export type AtlasEvidence =
  | { status: 'sourced'; source_ids: [string, ...string[]] }
  | { status: 'conflicting'; supporting_source_ids: [string, ...string[]]; opposing_source_ids: [string, ...string[]] }
  | { status: 'unsupported'; reason: string };

export interface AtlasSource {
  source_id: string;
  url: string;
  title: string;
  publisher: string;
  retrieved_at: string;
  excerpt: string;
  trust: 'official' | 'third_party' | 'unverified';
}

export type AtlasFacet = {
  key: string;
  label: string;
  scope: string;
} & (
  | { status: 'known' | 'research_needed' }
  | { status: 'unavailable'; reason: string }
);

export interface AtlasEntity {
  entity_id: string;
  kind: 'concept' | 'category' | 'person' | 'organization' | 'place' | 'product' | 'event' | 'other';
  title: string;
  summary: string;
  identity_status: 'resolved' | 'candidate';
  external_references: Array<{ namespace: string; value: string }>;
  facets: AtlasFacet[];
}

/** Source-backed presentation data, not execution instructions or approval state.
 * The enclosing claim's evidence applies to every attribute, proposed step and draft.
 */
export interface AtlasClaimDetails {
  attributes?: Array<{
    key: string;
    label: string;
    value: string | number | boolean | null | string[];
    unit?: string;
    uncertainty?: string;
  }>;
  checklist?: Array<{ id: string; text: string; required: boolean }>;
  draft?: { title: string; body: string };
}

export interface AtlasClaim {
  claim_id: string;
  subject_entity_id: string;
  /** Omitted means an entity-wide claim, not a claim about every facet. */
  facet_key?: string;
  text: string;
  observed_at: string;
  evidence: AtlasEvidence;
  details?: AtlasClaimDetails;
}

export interface AtlasRelationship {
  relationship_id: string;
  from_entity_id: string;
  to_entity_id: string;
  label: string;
  verification: 'confirmed' | 'suggested';
  evidence: AtlasEvidence;
}

export type AtlasTarget =
  | { kind: 'entity'; entity_id: string }
  | { kind: 'facet'; entity_id: string; facet_key: string }
  | { kind: 'source'; source_id: string };

export interface AtlasOccurrence {
  occurrence_id: string;
  path_id: string;
  parent_occurrence_id: string | null;
  target: AtlasTarget;
}

export interface AtlasExplorationPath {
  path_id: string;
  parent_path_id: string | null;
  selected_occurrence_ids: string[];
}

export interface AtlasExplorationRequest {
  request_id: string;
  exploration_id: string;
  path_id: string;
  parent_occurrence_id: string | null;
  run_id: string;
  thread_id: string;
  committed_history_version: number;
  last_sequence: number;
  status: 'loading' | 'partial' | 'completed' | 'failed' | 'cancelled';
}

export type AtlasExplorationAction = {
  action_id: string;
  label: string;
  target: AtlasTarget;
} & (
  | { kind: 'local'; operation: 'inspect_sources' | 'open_connections'; requires_approval: false }
  | { kind: 'external'; tool_name: string; requires_approval: true }
);

export interface AtlasExplorationSnapshot {
  schema_version: 'atlas-exploration.v2';
  exploration_id: string;
  revision: number;
  root_query: string;
  interpretation:
    | { kind: 'ambiguous'; candidate_entity_ids: [string, ...string[]] }
    | { kind: 'resolved'; subject_entity_id: string };
  entities: AtlasEntity[];
  sources: AtlasSource[];
  claims: AtlasClaim[];
  relationships: AtlasRelationship[];
  occurrences: AtlasOccurrence[];
  paths: AtlasExplorationPath[];
  active_path_id: string | null;
  requests: AtlasExplorationRequest[];
  actions: AtlasExplorationAction[];
}
