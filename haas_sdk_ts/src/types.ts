/**
 * Type definitions for HaaS API resources.
 * Generated from OpenAPI 3.1 specification.
 */

// --- Core types ---

export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance?: string;
  [key: string]: unknown;
}

export interface CursorPage<T> {
  items: T[];
  next_cursor?: string;
}

// --- Identity ---

export interface MeResponse {
  principal_id: string;
  issuer: string;
  subject: string;
  tenant_id: string;
  tenant_slug: string;
  roles: string[];
  capabilities: string[];
}

export interface CapabilitiesResponse {
  version: string;
  features: Record<string, boolean>;
  event_schemas: Record<string, string>;
}

// --- Runs ---

export interface RunCreateRequest {
  prompt: string;
  thread_id?: string;
  preload_skills?: string[];
  primary_model?: string;
  planner_model?: string;
  max_steps?: number;
  max_cost_usd?: number;
  secrets?: Record<string, string>;
  agent_revision_id?: string;
  expected_history_version?: number;
}

export interface RunResponse {
  run_id: string;
  status: RunStatus;
  thread_id?: string;
  result?: string;
  step_count?: number;
  total_cost_usd?: number;
  event_sequence?: number;
  approval?: ApprovalInfo;
  created_at: number;
}

export type RunStatus = 
  | 'queued' 
  | 'running' 
  | 'cancelling' 
  | 'completed' 
  | 'failed' 
  | 'cancelled' 
  | 'input_required';

export interface ApprovalInfo {
  batch_id: string;
  batch_digest: string;
  expires_at: string;
  tool_calls: ToolCall[];
}

export interface ToolCall {
  invocation_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  destination: string;
  required_secrets: string[];
}

export interface RunListParams {
  limit?: number;
  cursor?: string;
}

export interface RunEventsParams {
  after?: number;
  limit?: number;
}

export interface RunEvent {
  sequence: number;
  type: string;
  data: Record<string, unknown>;
}

export interface RunEventsResponse {
  items: RunEvent[];
  next_after?: number;
  latest_sequence: number;
}

export interface RunStepsResponse {
  items: RunStep[];
}

export interface RunStep {
  step_id: string;
  run_id: string;
  attempt: number;
  status: string;
  plan?: Record<string, unknown>;
  tool_calls?: ToolCall[];
  started_at: number;
  completed_at?: number;
}

export interface RunUsageResponse {
  run_id: string;
  model_tokens: ModelTokens;
  tool_invocations: number;
  egress_bytes: number;
  artifact_bytes: number;
  cost_usd: number;
}

export interface ModelTokens {
  prompt: number;
  completion: number;
  total: number;
}

// --- Approvals ---

export interface ApprovalBatch {
  batch_id: string;
  run_id: string;
  status: 'pending' | 'approved' | 'rejected' | 'expired';
  batch_digest: string;
  tool_calls: ToolCall[];
  expires_at: string;
  decision?: ApprovalDecision;
  created_at: number;
}

export interface ApprovalDecision {
  decision: 'approve' | 'reject';
  batch_digest: string;
  reason?: string;
  secrets?: Record<string, string>;
}

export interface ApprovalListParams {
  status?: 'pending' | 'approved' | 'rejected' | 'expired';
  limit?: number;
}

// --- Threads ---

export interface Thread {
  thread_id: string;
  history_version: number;
  active_run_id?: string;
}

export interface ThreadHistoryParams {
  offset?: number;
  history_version?: number;
}

export interface ThreadBranchRequest {
  expected_history_version: number;
}

export interface ThreadBranchResponse {
  thread_id: string;
  history_version: number;
  active_run_id?: string;
}

// --- Agent Revisions ---

export interface AgentRevision {
  revision_id: string;
  agent_name: string;
  version?: number;
  state?: 'draft' | 'validated' | 'published';
  digest?: string;
  configuration?: AgentConfiguration;
  created_at: number;
}

export interface AgentConfiguration {
  primary_model: string;
  planner_model: string;
  max_steps?: number;
  max_cost_usd?: number;
  skills?: string[];
  preload_skills?: string[];
}

export interface AgentRevisionListParams {
  cursor?: string;
}

export interface AgentRevisionCreateRequest {
  agent_name: string;
  configuration: AgentConfiguration;
}

export interface AgentRevisionOperation {
  operation: 'validate' | 'publish';
}

// --- Artifacts ---

export interface Artifact {
  artifact_id: string;
  run_id?: string;
  name?: string;
  content_type?: string;
  size_bytes?: number;
  sha256?: string;
  object_key?: string;
  source?: 'api' | 'tool';
  created_by?: string;
  metadata?: Record<string, unknown>;
  created_at: number;
  expires_at?: number;
  deleted_at?: number;
}

export interface ArtifactCreateRequest {
  run_id: string;
  name: string;
  content_type: string;
  source: 'api' | 'tool';
  created_by?: string;
  metadata?: Record<string, unknown>;
  expires_at?: string;
}

export interface ArtifactListParams {
  run_id: string;
  limit?: number;
  cursor?: string;
}

// --- Webhooks ---

export interface WebhookEndpoint {
  endpoint_id: string;
  url: string;
  events: string[];
  enabled: boolean;
  description?: string;
  created_at: number;
  updated_at: number;
}

export interface WebhookEndpointCreateRequest {
  url: string;
  events?: string[];
  enabled?: boolean;
  description?: string;
}

export interface WebhookEndpointUpdateRequest {
  url?: string;
  events?: string[];
  enabled?: boolean;
  description?: string;
}

export interface WebhookSigningKey {
  key_id: string;
  status: 'active' | 'retiring' | 'revoked';
  retire_at?: number;
  revoked_at?: number;
  created_at: number;
}

export interface WebhookSigningKeyCreateRequest {
  key_id: string;
  secret_envelope: string;
}

export interface WebhookSigningKeyRotateRequest {
  new_key_id: string;
  new_secret_envelope: string;
  retire_at?: number;
}

export interface WebhookDelivery {
  delivery_id: string;
  run_id?: string;
  endpoint_id?: string;
  event_type: string;
  url: string;
  body: Record<string, unknown>;
  status: 'pending' | 'in_flight' | 'retrying' | 'succeeded' | 'dead' | 'blocked';
  attempts: number;
  max_attempts: number;
  next_attempt_at: number;
  last_status_code?: number;
  last_error?: string;
  last_latency_ms?: number;
  replay_count: number;
  created_at: number;
  updated_at: number;
  completed_at?: number;
}

export interface WebhookDeliveryListParams {
  status?: string;
  limit?: number;
  cursor?: string;
}

export interface WebhookReplayRequest {
  delivery_ids: string[];
}

// --- Quotas ---

export interface TenantQuota {
  max_concurrent_runs?: number;
  max_inflight_runs?: number;
  requests_per_minute?: number;
  principal_requests_per_minute?: number;
  sse_connections?: number;
  weight: number;
  updated_at: number;
}

export interface TenantQuotaRequest {
  max_concurrent_runs?: number;
  max_inflight_runs?: number;
  requests_per_minute?: number;
  principal_requests_per_minute?: number;
  sse_connections?: number;
  weight?: number;
}

// --- Retention ---

export interface RetentionPolicy {
  run_events_ttl_days?: number;
  artifacts_ttl_days?: number;
  messages_ttl_days?: number;
  artifact_quota_bytes?: number;
  updated_at: number;
}

export interface RetentionPolicyRequest {
  run_events_ttl_days?: number;
  artifacts_ttl_days?: number;
  messages_ttl_days?: number;
  artifact_quota_bytes?: number;
}

export interface LegalHold {
  hold_id: string;
  scope: 'tenant' | 'run' | 'thread';
  subject_id?: string;
  reason: string;
  placed_by?: string;
  placed_at: number;
  released_by?: string;
  released_at?: number;
  release_reason?: string;
}

export interface LegalHoldCreateRequest {
  scope: 'tenant' | 'run' | 'thread';
  subject_id?: string;
  reason: string;
}

export interface ErasureRequest {
  request_id: string;
  kind: 'subject' | 'offboarding';
  subject_type?: 'run' | 'thread';
  subject_ids: string[];
  reason?: string;
  status: 'pending' | 'blocked' | 'completed';
  requested_by?: string;
  requested_at: number;
  attempts: number;
  last_error?: string;
  completed_at?: number;
  manifest_sha256?: string;
  receipt_hmac?: string;
  receipt_key_id?: string;
}

export interface ErasureRequestCreateRequest {
  kind: 'subject' | 'offboarding';
  subject_type?: 'run' | 'thread';
  subject_ids?: string[];
  reason?: string;
}

export interface RetentionTombstone {
  tombstone_id: string;
  data_class: 'run_events' | 'artifacts' | 'messages' | 'tenant';
  subject_type: 'run' | 'thread' | 'tenant';
  subject_id: string;
  reason: 'retention' | 'erasure_request' | 'offboarding';
  policy: Record<string, unknown>;
  records_erased: number;
  bytes_erased: number;
  manifest_sha256: string;
  request_id?: string;
  erased_by: string;
  erased_at: number;
}

// --- Secrets & Credentials ---

export interface SecretBinding {
  binding_id: string;
  name: string;
  provider: 'inline' | 'env';
  provider_reference?: string;
  version: number;
  allowed_plugins: string[];
  allowed_tools: string[];
  description?: string;
  created_by?: string;
  created_at: number;
  rotated_at?: number;
  deleted_at?: number;
}

export interface SecretBindingCreateRequest {
  name: string;
  provider: 'inline' | 'env';
  provider_reference?: string;
  encrypted_value?: string;
  allowed_plugins?: string[];
  allowed_tools?: string[];
  description?: string;
}

export interface SecretBindingUpdateRequest {
  provider?: 'inline' | 'env';
  provider_reference?: string;
  encrypted_value?: string;
  version?: number;
  allowed_plugins?: string[];
  allowed_tools?: string[];
  description?: string;
}

export interface CredentialGrant {
  grant_id: string;
  run_id: string;
  invocation_id: string;
  secret_name: string;
  source: 'binding' | 'run';
  binding_id?: string;
  binding_version?: number;
  audience: string;
  plugin_digest: string;
  created_at: number;
  expires_at: number;
  redeemed_at?: number;
  denied_reason?: string;
}

export interface CredentialGrantCreateRequest {
  run_id: string;
  invocation_id: string;
  secret_name: string;
  source: 'binding' | 'run';
  binding_id?: string;
  binding_version?: number;
  audience: string;
  plugin_digest?: string;
}

export interface CredentialGrantRedeemResponse {
  secret_value?: string;
  audience: string;
  plugin_digest: string;
}

// --- Egress ---

export interface EgressPolicy {
  allow_hosts: string[];
  allow_cidrs: string[];
  max_bytes_per_run?: number;
  version: number;
  updated_by?: string;
  updated_at: number;
}

export interface EgressPolicyRequest {
  allow_hosts?: string[];
  allow_cidrs?: string[];
  max_bytes_per_run?: number;
}

export interface EgressCheckResponse {
  allowed: boolean;
  host: string;
  host_allowed: boolean;
  cidr_allowed: boolean;
  reason: string;
}

// --- Redaction ---

export interface RedactionRule {
  rule_id: string;
  name: string;
  pattern: string;
  replacement: string;
  applies_to: string[];
  enabled: boolean;
  created_at: number;
  updated_at: number;
}

export interface RedactionRuleCreateRequest {
  name: string;
  pattern: string;
  replacement?: string;
  applies_to?: string[];
  enabled?: boolean;
}

export interface RedactionTestRequest {
  text: string;
  rules?: string[];
}

export interface RedactionTestResponse {
  original: string;
  redacted: string;
  matches: RedactionMatch[];
}

export interface RedactionMatch {
  rule_id: string;
  rule_name: string;
  match: string;
  start: number;
  end: number;
}

// --- Isolation ---

export interface StaticCatalogEntry {
  plugin_id: string;
  name: string;
  description: string;
  version: string;
  category: string;
  enabled: boolean;
  provides_skills?: string[];
  provides_tools?: string[];
  requires_plugins: string[];
  artifact_digest: string;
  artifact_size: number;
  fingerprint_files: string[];
}

export interface PackPluginRequest {
  plugin_id: string;
  pinned_digest?: string;
}

export interface PackPluginResponse {
  plugin_id: string;
  version: string;
  plugin_digest: string;
  artifact_digest: string;
  artifact_size: number;
  config: Record<string, unknown>;
  manifest: Record<string, unknown>;
}

export interface MaterializeRequest {
  plugin_id: string;
  pinned_digest: string;
  run_id: string;
  invocation_id: string;
}

export interface MaterializeResponse {
  plugin_id: string;
  artifact_digest: string;
  sandbox_path: string;
}

export interface SandboxExecuteRequest {
  plugin_id: string;
  artifact_digest: string;
  run_id: string;
  invocation_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  secrets?: Record<string, string>;
  egress_allowed?: boolean;
  limits?: Record<string, unknown>;
}

export interface SandboxExecuteResponse {
  success: boolean;
  result?: unknown;
  error?: string;
  stdout: string;
  stderr: string;
  execution_time_ms: number;
}

// --- Marketplace ---

export interface Publisher {
  publisher_id: string;
  namespace: string;
  display_name: string;
  owner_tenant_id: string;
  verification: 'unverified' | 'verified' | 'official';
  domain?: string;
  status: 'active' | 'suspended';
  status_reason?: string;
  created_at: number;
  updated_at: number;
}

export interface PublisherCreateRequest {
  namespace: string;
  display_name: string;
  domain?: string;
}

export interface PublisherUpdateRequest {
  display_name?: string;
  domain?: string;
  status?: 'active' | 'suspended';
  status_reason?: string;
}

export interface PublisherKey {
  key_id: string;
  publisher_id: string;
  label?: string;
  created_at: number;
  revoked_at?: number;
  revoked_reason?: string;
}

export interface PublisherKeyCreateRequest {
  key_id: string;
  public_key: string;
  label?: string;
}

export interface Listing {
  listing_id: string;
  plugin_id: string;
  publisher_id: string;
  name: string;
  display_name: string;
  summary: string;
  categories: string[];
  keywords: string[];
  visibility: 'public' | 'private';
  owner_tenant_id: string;
  status: 'active' | 'taken_down';
  status_reason?: string;
  install_count: number;
  created_at: number;
  updated_at: number;
}

export interface ListingCreateRequest {
  plugin_id: string;
  name: string;
  display_name: string;
  summary: string;
  categories?: string[];
  keywords?: string[];
  visibility?: 'public' | 'private';
}

export interface ListingUpdateRequest {
  display_name?: string;
  summary?: string;
  categories?: string[];
  keywords?: string[];
  visibility?: 'public' | 'private';
  status?: 'active' | 'taken_down';
  status_reason?: string;
}

export interface Revision {
  revision_id: string;
  listing_id: string;
  version: string;
  major: number;
  minor: number;
  patch: number;
  prerelease?: string;
  digest: string;
  manifest: Record<string, unknown>;
  manifest_raw: Record<string, unknown>;
  manifest_digest: string;
  source_manifest_version: 1 | 2;
  permissions: Record<string, unknown>;
  signature: Record<string, unknown>;
  signing_key_id: string;
  provenance?: Record<string, unknown>;
  tier: 'platform' | 'verified' | 'community' | 'private' | 'unreviewed';
  review_state: 'submitted' | 'approved' | 'rejected';
  review_report: Record<string, unknown>;
  reviewed_by?: string;
  reviewed_at?: number;
  review_notes?: string;
  tlog_seq: number;
  yanked_at?: number;
  yank_reason?: string;
  published_by?: string;
  created_at: number;
}

export interface RevisionCreateRequest {
  listing_id: string;
  version: string;
  major: number;
  minor: number;
  patch: number;
  prerelease?: string;
  digest: string;
  manifest: Record<string, unknown>;
  manifest_raw: Record<string, unknown>;
  manifest_digest: string;
  source_manifest_version: 1 | 2;
  permissions: Record<string, unknown>;
  signature: Record<string, unknown>;
  signing_key_id: string;
  provenance?: Record<string, unknown>;
  tier: 'platform' | 'verified' | 'community' | 'private' | 'unreviewed';
}

export interface RevisionReviewRequest {
  review_state: 'approved' | 'rejected';
  review_report: Record<string, unknown>;
  review_notes?: string;
}

export interface Installation {
  installation_id: string;
  listing_id: string;
  revision_id: string;
  plugin_id: string;
  version: string;
  digest: string;
  consent: Record<string, unknown>;
  consent_digest: string;
  status: 'active' | 'uninstalled' | 'revoked';
  status_reason?: string;
  installed_by?: string;
  installed_at: number;
  updated_at: number;
}

export interface InstallationCreateRequest {
  listing_id: string;
  revision_id: string;
  consent: Record<string, unknown>;
  consent_digest: string;
}

export interface InstallationUpdateRequest {
  status?: 'active' | 'uninstalled' | 'revoked';
  status_reason?: string;
}

export interface Notification {
  notification_id: string;
  kind: 'yanked' | 'taken_down' | 'publisher_suspended' | 'key_revoked';
  plugin_id: string;
  version?: string;
  details: Record<string, unknown>;
  created_at: number;
  acknowledged_at?: number;
}

export interface AbuseReport {
  report_id: string;
  listing_id: string;
  version?: string;
  reporter_tenant_id?: string;
  reporter_principal_id?: string;
  reason: 'malware' | 'data_exfiltration' | 'impersonation' | 'prompt_injection' | 'policy' | 'other';
  details: string;
  status: 'open' | 'actioned' | 'dismissed';
  resolution?: string;
  resolved_by?: string;
  resolved_at?: number;
  created_at: number;
}

export interface AbuseReportCreateRequest {
  listing_id: string;
  version?: string;
  reason: 'malware' | 'data_exfiltration' | 'impersonation' | 'prompt_injection' | 'policy' | 'other';
  details: string;
}

export interface SearchRequest {
  query?: string;
  categories?: string[];
  keywords?: string[];
  tiers?: ('platform' | 'verified' | 'community' | 'private' | 'unreviewed')[];
  visibility?: 'public' | 'private';
  limit?: number;
  offset?: number;
}

export interface SearchResult {
  listing_id: string;
  plugin_id: string;
  publisher_id: string;
  name: string;
  display_name: string;
  summary: string;
  categories: string[];
  keywords: string[];
  visibility: string;
  install_count: number;
  latest_revision?: Revision;
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
  limit: number;
  offset: number;
}

export interface TransparencyLogEntry {
  seq: number;
  entry: string;
  leaf_hash: string;
  chain_hash: string;
  created_at: number;
}

export interface TransparencyLogResponse {
  entries: TransparencyLogEntry[];
  next_after: number;
}

// --- Agent Mesh ---

export interface Delegation {
  delegation_id: string;
  parent_run_id: string;
  child_run_id?: string;
  child_agent_revision_id: string;
  child_agent_config: Record<string, unknown>;
  attenuation_policy: Record<string, unknown>;
  budget_usd?: number;
  max_steps?: number;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  created_at: number;
  completed_at?: number;
}

export interface DelegationCreateRequest {
  parent_run_id: string;
  child_agent_revision_id: string;
  child_agent_config?: Record<string, unknown>;
  attenuation_policy?: Record<string, unknown>;
  budget_usd?: number;
  max_steps?: number;
}

export interface AgentMessage {
  message_id: string;
  run_id: string;
  from_agent: string;
  to_agent?: string;
  message_type: 'request' | 'response' | 'notification' | 'handoff';
  payload: Record<string, unknown>;
  correlation_id?: string;
  created_at: number;
}

export interface AgentMessageCreateRequest {
  run_id: string;
  from_agent: string;
  to_agent?: string;
  message_type: 'request' | 'response' | 'notification' | 'handoff';
  payload: Record<string, unknown>;
  correlation_id?: string;
}

export interface WorkspaceEntry {
  entry_id: string;
  run_id: string;
  agent_id: string;
  key: string;
  value: Record<string, unknown>;
  version: number;
  updated_by: string;
  updated_at: number;
}

export interface WorkspaceEntryCreateRequest {
  run_id: string;
  agent_id: string;
  key: string;
  value: Record<string, unknown>;
  expected_version?: number;
}

export interface AgentCard {
  card_id: string;
  agent_revision_id: string;
  name: string;
  description: string;
  capabilities: string[];
  skills: string[];
  endpoints: Record<string, unknown>[];
  authentication: Record<string, unknown>;
  created_at: number;
  updated_at: number;
}

export interface AgentCardCreateRequest {
  agent_revision_id: string;
  name: string;
  description: string;
  capabilities?: string[];
  skills?: string[];
  endpoints?: Record<string, unknown>[];
  authentication?: Record<string, unknown>;
}

export interface Topology {
  topology_id: string;
  name: string;
  type: 'supervisor' | 'hierarchical' | 'swarm' | 'graph' | 'map_reduce' | 'debate';
  config: Record<string, unknown>;
  agent_revisions: string[];
  validation_status: 'pending' | 'valid' | 'invalid';
  validation_report?: Record<string, unknown>;
  created_at: number;
  updated_at: number;
}

export interface TopologyCreateRequest {
  name: string;
  type: 'supervisor' | 'hierarchical' | 'swarm' | 'graph' | 'map_reduce' | 'debate';
  config: Record<string, unknown>;
  agent_revisions: string[];
}

export interface RunTreeNode {
  run_id: string;
  root_run_id: string;
  depth: number;
  children: RunTreeNode[];
  agent_revision_id?: string;
  status: string;
  plan?: Record<string, unknown>;
  cost_usd: number;
  created_at: number;
}

export interface MailboxMessage {
  message_id: string;
  mailbox_id: string;
  sender: string;
  recipient: string;
  message_type: string;
  payload: Record<string, unknown>;
  correlation_id?: string;
  delivered_at?: number;
  created_at: number;
}

export interface MailboxMessageCreateRequest {
  mailbox_id: string;
  sender: string;
  recipient: string;
  message_type: string;
  payload: Record<string, unknown>;
  correlation_id?: string;
}

export interface A2AAgentCard {
  agent_card: Record<string, unknown>;
}

export interface MCPServerConfig {
  server_id: string;
  agent_revision_id: string;
  transport: 'streamable_http' | 'stdio';
  endpoint: string;
  oauth_config?: Record<string, unknown>;
  created_at: number;
}

export interface MCPServerConfigCreateRequest {
  agent_revision_id: string;
  transport?: 'streamable_http' | 'stdio';
  oauth_config?: Record<string, unknown>;
}

export interface RunObservability {
  runs: Array<{
    run_id: string;
    agent_revision_id?: string;
    status: string;
    cost_usd: number;
    created_at: number;
    parent_run_id?: string;
  }>;
  messages: AgentMessage[];
  workspace: WorkspaceEntry[];
}

// --- Operations ---

export interface Cell {
  cell_id: string;
  region: string;
  base_url: string;
  status: 'active' | 'draining' | 'drained';
  dedicated: boolean;
  created_at: number;
  updated_at: number;
}

export interface CellCreateRequest {
  cell_id: string;
  region: string;
  base_url: string;
  dedicated?: boolean;
}

export interface TenantPlacement {
  tenant_id: string;
  slug: string;
  cell_id: string;
  placement_epoch: number;
  state: 'active' | 'moving';
  target_cell_id?: string;
  active_runs: number;
}

export interface TenantMoveRequest {
  target_cell_id: string;
  expected_epoch: number;
  force?: boolean;
}

export interface TenantMoveResponse {
  tenant_id: string;
  cell_id: string;
  placement_epoch: number;
  state: string;
  target_cell_id?: string;
  moved_at?: number;
}

export interface TenantMoveFinishRequest {
  expected_epoch: number;
  commit?: boolean;
}

export interface PlatformMetrics {
  metrics: Array<{
    metric: string;
    labels: Record<string, unknown>;
    value: number;
  }>;
}

export interface RestoreEpochRequest {
  reason: string;
  actor: string;
}

export interface RestoreEpochResponse {
  new_epoch: number;
  fenced_runs: number;
  reset_deliveries: number;
}

export interface FenceRunsResponse {
  fenced_runs: number;
}

// --- Error types ---

export class HaaSAPIError extends Error {
  constructor(
    message: string,
    public status: number,
    public details?: unknown
  ) {
    super(message);
    this.name = 'HaaSAPIError';
  }
}

export class HaaSConnectionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'HaaSConnectionError';
  }
}

export class HaaSStreamClosedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'HaaSStreamClosedError';
  }
}

// --- Client options ---

export interface ClientOptions {
  baseUrl: string;
  apiKey?: string;
  tenant?: string;
  timeout?: number;
  maxRetries?: number;
  streamMaxReconnects?: number;
  streamReconnectDelay?: number;
  defaultHeaders?: Record<string, string>;
}

export interface StreamOptions {
  lastEventId?: string;
  outputDeltas?: boolean;
}

export interface SSEEvent {
  event: string;
  data: Record<string, unknown>;
  id?: string;
}