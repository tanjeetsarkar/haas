/**
 * HaaS TypeScript SDK - Main Entry Point
 * 
 * @packageDocumentation
 */

export { HaaSClient, createClientFromEnv } from './client';
export type { EventSourceParser } from 'eventsource-parser';

// Re-export all types
export type {
  // Core
  ProblemDetails,
  CursorPage,
  
  // Identity
  MeResponse,
  CapabilitiesResponse,
  
  // Runs
  RunCreateRequest,
  RunResponse,
  RunStatus,
  ApprovalInfo,
  ToolCall,
  RunListParams,
  RunEventsParams,
  RunEventsResponse,
  RunEvent,
  RunStepsResponse,
  RunStep,
  RunUsageResponse,
  ModelTokens,
  
  // Approvals
  ApprovalBatch,
  ApprovalDecision,
  ApprovalListParams,
  
  // Threads
  Thread,
  ThreadHistoryParams,
  ThreadBranchRequest,
  ThreadBranchResponse,
  
  // Agent Revisions
  AgentRevision,
  AgentConfiguration,
  AgentRevisionListParams,
  AgentRevisionCreateRequest,
  AgentRevisionOperation,
  
  // Artifacts
  Artifact,
  ArtifactCreateRequest,
  ArtifactListParams,
  
  // Webhooks
  WebhookEndpoint,
  WebhookEndpointCreateRequest,
  WebhookEndpointUpdateRequest,
  WebhookSigningKey,
  WebhookSigningKeyCreateRequest,
  WebhookSigningKeyRotateRequest,
  WebhookDelivery,
  WebhookDeliveryListParams,
  WebhookReplayRequest,
  
  // Quotas
  TenantQuota,
  TenantQuotaRequest,
  
  // Retention
  RetentionPolicy,
  RetentionPolicyRequest,
  LegalHold,
  LegalHoldCreateRequest,
  ErasureRequest,
  ErasureRequestCreateRequest,
  RetentionTombstone,
  
  // Secrets & Credentials
  SecretBinding,
  SecretBindingCreateRequest,
  SecretBindingUpdateRequest,
  CredentialGrant,
  CredentialGrantCreateRequest,
  CredentialGrantRedeemResponse,
  
  // Egress
  EgressPolicy,
  EgressPolicyRequest,
  EgressCheckResponse,
  
  // Redaction
  RedactionRule,
  RedactionRuleCreateRequest,
  RedactionTestRequest,
  RedactionTestResponse,
  RedactionMatch,
  
  // Isolation
  StaticCatalogEntry,
  PackPluginRequest,
  PackPluginResponse,
  MaterializeRequest,
  MaterializeResponse,
  SandboxExecuteRequest,
  SandboxExecuteResponse,
  
  // Marketplace
  Publisher,
  PublisherCreateRequest,
  PublisherUpdateRequest,
  PublisherKey,
  PublisherKeyCreateRequest,
  Listing,
  ListingCreateRequest,
  ListingUpdateRequest,
  Revision,
  RevisionCreateRequest,
  RevisionReviewRequest,
  Installation,
  InstallationCreateRequest,
  InstallationUpdateRequest,
  Notification,
  AbuseReport,
  AbuseReportCreateRequest,
  SearchRequest,
  SearchResponse,
  SearchResult,
  TransparencyLogEntry,
  TransparencyLogResponse,
  
  // Agent Mesh
  Delegation,
  DelegationCreateRequest,
  AgentMessage,
  AgentMessageCreateRequest,
  WorkspaceEntry,
  WorkspaceEntryCreateRequest,
  AgentCard,
  AgentCardCreateRequest,
  Topology,
  TopologyCreateRequest,
  RunTreeNode,
  MailboxMessage,
  MailboxMessageCreateRequest,
  A2AAgentCard,
  MCPServerConfig,
  MCPServerConfigCreateRequest,
  RunObservability,
  
  // Operations
  Cell,
  CellCreateRequest,
  TenantPlacement,
  TenantMoveRequest,
  TenantMoveResponse,
  TenantMoveFinishRequest,
  PlatformMetrics,
  RestoreEpochRequest,
  RestoreEpochResponse,
  FenceRunsResponse,
  
  // Errors
  HaaSAPIError,
  HaaSConnectionError,
  HaaSStreamClosedError,
  
  // Client
  ClientOptions,
  StreamOptions,
  SSEEvent,
  
  // Atlas
  DrilldownRequest,
  AtlasLayerNode,
  AtlasLayerEnvelope,
  AtlasRunEvent,
  AtlasEventHandler,
} from './types';

// Re-export Atlas client
export { AtlasSessionManager, createAtlasSessionManager } from './lib/atlasClient';