/**
 * HaaS TypeScript SDK - Main Client
 * 
 * Provides both synchronous-style (Promise-based) and streaming APIs
 * for interacting with the HaaS /v1 REST API and SSE event streams.
 */

import { createParser, ParsedEvent } from 'eventsource-parser';
import {
  ClientOptions,
  StreamOptions,
  SSEEvent,
  HaaSAPIError,
  HaaSConnectionError,
  HaaSStreamClosedError,
  // Types
  MeResponse,
  CapabilitiesResponse,
  RunCreateRequest,
  RunResponse,
  RunListParams,
  RunEventsParams,
  RunEventsResponse,
  RunStepsResponse,
  RunUsageResponse,
  ApprovalBatch,
  ApprovalDecision,
  ApprovalListParams,
  Thread,
  ThreadHistoryParams,
  ThreadBranchRequest,
  ThreadBranchResponse,
  AgentRevision,
  AgentRevisionListParams,
  AgentRevisionCreateRequest,
  AgentRevisionOperation,
  Artifact,
  ArtifactCreateRequest,
  ArtifactListParams,
  WebhookEndpoint,
  WebhookEndpointCreateRequest,
  WebhookEndpointUpdateRequest,
  WebhookSigningKey,
  WebhookSigningKeyCreateRequest,
  WebhookSigningKeyRotateRequest,
  WebhookDelivery,
  WebhookDeliveryListParams,
  WebhookReplayRequest,
  TenantQuota,
  TenantQuotaRequest,
  RetentionPolicy,
  RetentionPolicyRequest,
  LegalHold,
  LegalHoldCreateRequest,
  ErasureRequest,
  ErasureRequestCreateRequest,
  RetentionTombstone,
  SecretBinding,
  SecretBindingCreateRequest,
  SecretBindingUpdateRequest,
  CredentialGrant,
  CredentialGrantCreateRequest,
  CredentialGrantRedeemResponse,
  EgressPolicy,
  EgressPolicyRequest,
  EgressCheckResponse,
  RedactionRule,
  RedactionRuleCreateRequest,
  RedactionTestRequest,
  RedactionTestResponse,
  StaticCatalogEntry,
  PackPluginRequest,
  PackPluginResponse,
  MaterializeRequest,
  MaterializeResponse,
  SandboxExecuteRequest,
  SandboxExecuteResponse,
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
  TransparencyLogResponse,
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
} from './types';

/**
 * Default configuration values
 */
const DEFAULT_TIMEOUT = 30000; // 30 seconds
const DEFAULT_MAX_RETRIES = 2;
const DEFAULT_STREAM_MAX_RECONNECTS = 5;
const DEFAULT_STREAM_RECONNECT_DELAY = 1000; // 1 second

/**
 * Terminal event types that end a run stream
 */
const TERMINAL_EVENTS = new Set([
  'run_completed',
  'run_error',
  'run_cancelled',
  'stream_closed',
]);

const SETTLED_EVENTS = new Set([
  ...TERMINAL_EVENTS,
  'approval_required',
  'run_paused',
  'input_required',
]);

/**
 * Main HaaS Client Class
 */
export class HaaSClient {
  private baseUrl: string;
  private apiKey?: string;
  private tenant: string;
  private timeout: number;
  private maxRetries: number;
  private streamMaxReconnects: number;
  private streamReconnectDelay: number;
  private defaultHeaders: Record<string, string>;
  private authExpired: boolean = false;

  constructor(options: ClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, '');
    this.apiKey = options.apiKey;
    this.tenant = options.tenant || 'default-tenant';
    this.timeout = options.timeout || DEFAULT_TIMEOUT;
    this.maxRetries = options.maxRetries ?? DEFAULT_MAX_RETRIES;
    this.streamMaxReconnects = options.streamMaxReconnects ?? DEFAULT_STREAM_MAX_RECONNECTS;
    this.streamReconnectDelay = options.streamReconnectDelay ?? DEFAULT_STREAM_RECONNECT_DELAY;
    this.defaultHeaders = options.defaultHeaders || {};
  }

  /**
   * Build request headers
   */
  private getHeaders(extraHeaders: Record<string, string> = {}): Record<string, string> {
    const headers: Record<string, string> = {
      'X-Tenant-ID': this.tenant,
      'Content-Type': 'application/json',
      ...this.defaultHeaders,
      ...extraHeaders,
    };

    if (this.apiKey) {
      headers['Authorization'] = `Bearer ${this.apiKey}`;
    }

    return headers;
  }

  /**
   * Build idempotency headers
   */
  private getIdempotencyHeaders(idempotencyKey?: string): Record<string, string> {
    if (!idempotencyKey) return {};
    return { 'Idempotency-Key': idempotencyKey };
  }

  /**
   * Make an HTTP request with retry logic
   * Only retries idempotent requests (GET, DELETE, or requests with Idempotency-Key)
   */
  private async request<T>(
    method: string,
    path: string,
    options: {
      params?: Record<string, string | number | boolean | undefined> | URLSearchParams | RunListParams | RunEventsParams | ApprovalListParams | ThreadHistoryParams | AgentRevisionListParams | ArtifactListParams | WebhookDeliveryListParams;
      body?: unknown;
      headers?: Record<string, string>;
      idempotent?: boolean;
      idempotencyKey?: string;
    } = {}
  ): Promise<T> {
    const { params, body, headers = {}, idempotent = false, idempotencyKey } = options;
    
    const url = new URL(`${this.baseUrl}${path}`);
    if (params) {
      if (params instanceof URLSearchParams) {
        params.forEach((value, key) => {
          if (value !== undefined && value !== null) {
            url.searchParams.append(key, String(value));
          }
        });
      } else {
        Object.entries(params).forEach(([key, value]) => {
          if (value !== undefined && value !== null) {
            url.searchParams.append(key, String(value));
          }
        });
      }
    }

    const requestHeaders = this.getHeaders({
      ...headers,
      ...this.getIdempotencyHeaders(idempotencyKey),
    });

    // Determine if this request is safe to retry
    const isRetryable = idempotent || method === 'GET' || method === 'DELETE' || !!idempotencyKey;

    let lastError: Error | null = null;
    
    for (let attempt = 0; attempt <= this.maxRetries; attempt++) {
      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), this.timeout);

        const response = await fetch(url.toString(), {
          method,
          headers: requestHeaders,
          body: body ? JSON.stringify(body) : undefined,
          signal: controller.signal,
        });

        clearTimeout(timeoutId);

        if (response.status === 401) {
          this.authExpired = true;
          this.apiKey = undefined;
        }

        if (response.status >= 400) {
          let detail: string;
          try {
            const errorBody = await response.json() as { detail?: string; message?: string };
            detail = errorBody.detail || errorBody.message || JSON.stringify(errorBody);
          } catch {
            detail = await response.text();
          }
          
          if (response.status === 429) {
            const retryAfter = response.headers.get('Retry-After');
            detail = `Rate limited: ${detail}${retryAfter ? ` Retry after ${retryAfter}s.` : ''}`;
          }
          
          throw new HaaSAPIError(detail, response.status, detail);
        }

        if (response.status === 204 || response.headers.get('content-length') === '0') {
          return {} as T;
        }

        return await response.json() as T;
      } catch (error) {
        lastError = error as Error;
        
        // Don't retry on client errors (4xx except 429)
        if (error instanceof HaaSAPIError && error.status >= 400 && error.status < 500 && error.status !== 429) {
          throw error;
        }
        
        // Don't retry on auth errors
        if (error instanceof HaaSAPIError && error.status === 401) {
          throw error;
        }

        // Only retry if the request is idempotent
        if (!isRetryable) {
          throw error;
        }

        // Wait before retry - honor Retry-After header
        if (attempt < this.maxRetries) {
          let delay = Math.min(1000 * Math.pow(2, attempt), 10000);
          if (lastError instanceof HaaSAPIError) {
            const retryAfter = lastError.status === 429 ?               parseInt(lastError.message.match(/Retry after (\d+)s/)?.[1] || '0') : 0;
            if (retryAfter > 0) {
              delay = Math.min(retryAfter * 1000, 60000);
            }
          }
          await new Promise(resolve => setTimeout(resolve, delay));
        }
      }
    }

    throw new HaaSConnectionError(`Request failed after ${this.maxRetries + 1} attempts: ${lastError?.message}`);
  }

  /**
   * Make a streaming request with automatic reconnection
   */
  async *streamEvents(runId: string, options: StreamOptions = {}): AsyncGenerator<SSEEvent, void, unknown> {
    const { lastEventId = '0', outputDeltas = false } = options;
    let cursor = /^\d{1,18}$/.test(lastEventId) ? lastEventId : '0';
    let failures = 0;

    while (true) {
      // Create a queue to bridge the parser callback and the async generator
      const eventQueue: SSEEvent[] = [];
      let parseError: Error | null = null;

      const parser = createParser((event: { type: string; data?: string | number; id?: string; event?: string; error?: Error }) => {
        // Handle different event types from eventsource-parser
        if (event.type === 'error') {
          parseError = event.error || new Error('SSE parse error');
          return;
        }
        // event can be { type: 'event', event: string, data: string, id: string | undefined }
        // or { type: 'reconnect-interval', data: number }
        if (event.type === 'reconnect-interval') {
          return;
        }
        if (event.type !== 'event') {
          return;
        }
        const sseEvent: SSEEvent = {
          event: event.event || 'message',
          data: event.data ? JSON.parse(String(event.data)) : {},
          id: event.id,
        };
        eventQueue.push(sseEvent);
        // Update cursor for reconnection
        if (sseEvent.id) {
          cursor = sseEvent.id;
        }
      });

      try {
        const url = new URL(`${this.baseUrl}/v1/runs/${runId}/stream`);
        if (outputDeltas) {
          url.searchParams.set('output_deltas', 'true');
        }

        const response = await fetch(url.toString(), {
          method: 'GET',
          headers: {
            ...this.getHeaders(),
            'Accept': 'text/event-stream',
            'Last-Event-ID': cursor,
          },
        });

        if (response.status >= 400) {
          let detail: string;
          try {
            const errorBody = await response.json() as { detail?: string; message?: string };
            detail = errorBody.detail || errorBody.message || JSON.stringify(errorBody);
          } catch {
            detail = await response.text();
          }
          throw new HaaSAPIError(detail, response.status, detail);
        }

        if (!response.body) {
          throw new HaaSConnectionError('No response body for stream');
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();

        let done = false;
        while (!done) {
          // Yield any queued events
          while (eventQueue.length > 0) {
            yield eventQueue.shift()!;
          }

          const { done: readerDone, value } = await reader.read();
          done = readerDone;
          if (value) {
            const chunk = decoder.decode(value, { stream: true });
            parser.feed(chunk);
          }

          if (parseError) {
            throw parseError;
          }
        }

        reader.releaseLock();

        // Yield any remaining events
        while (eventQueue.length > 0) {
          yield eventQueue.shift()!;
        }

        // Stream ended cleanly
        const run = await this.getRun(runId);
        if (!['queued', 'running', 'cancelling'].includes(run.status)) {
          return;
        }

        failures++;
        if (failures > this.streamMaxReconnects) {
          throw new HaaSConnectionError(`Lost connection to event stream after ${this.streamMaxReconnects} reconnects`);
        }

        await new Promise(resolve => setTimeout(resolve, this.streamReconnectDelay));
      } catch (error) {
        if (error instanceof HaaSAPIError || error instanceof HaaSStreamClosedError) {
          throw error;
        }

        failures++;
        if (failures > this.streamMaxReconnects) {
          throw new HaaSConnectionError(`Lost connection to event stream: ${error instanceof Error ? error.message : String(error)}`);
        }

        await new Promise(resolve => setTimeout(resolve, this.streamReconnectDelay));
      }
    }
  }

  // =========================================================================
  // Identity & Capabilities
  // =========================================================================

  async me(): Promise<MeResponse> {
    return this.request('GET', '/v1/me', { idempotent: true });
  }

  async capabilities(): Promise<CapabilitiesResponse> {
    return this.request('GET', '/v1/capabilities', { idempotent: true });
  }

  async health(path: string = '/readyz'): Promise<Record<string, unknown>> {
    return this.request('GET', path, { idempotent: true });
  }

  // =========================================================================
  // Runs
  // =========================================================================

  async createRun(request: RunCreateRequest, idempotencyKey?: string): Promise<RunResponse> {
    return this.request('POST', '/v1/runs', {
      body: request,
      idempotent: true,
      idempotencyKey,
    });
  }

  async getRun(runId: string): Promise<RunResponse> {
    return this.request('GET', `/v1/runs/${encodeURIComponent(runId)}`, { idempotent: true });
  }

  async listRuns(params: RunListParams = {}): Promise<{ items: RunResponse[]; next_cursor?: string }> {
    return this.request('GET', '/v1/runs', { params, idempotent: true });
  }

  async cancelRun(runId: string): Promise<RunResponse> {
    return this.request('POST', `/v1/runs/${encodeURIComponent(runId)}/cancel`);
  }

  async resumeRun(
    runId: string,
    approved: boolean,
    options: {
      rejectionReason?: string;
      secrets?: Record<string, string>;
      batchId?: string;
      batch_digest?: string;
    } = {}
  ): Promise<RunResponse> {
    return this.request('POST', `/v1/runs/${encodeURIComponent(runId)}/resume`, {
      body: {
        approved,
        ...options,
      },
    });
  }

  async getRunEvents(runId: string, params: RunEventsParams = {}): Promise<RunEventsResponse> {
    return this.request('GET', `/v1/runs/${encodeURIComponent(runId)}/events`, { params, idempotent: true });
  }

  async getRunSteps(runId: string): Promise<RunStepsResponse> {
    return this.request('GET', `/v1/runs/${encodeURIComponent(runId)}/steps`, { idempotent: true });
  }

  async getRunUsage(runId: string): Promise<RunUsageResponse> {
    return this.request('GET', `/v1/runs/${encodeURIComponent(runId)}/usage`, { idempotent: true });
  }

  // =========================================================================
  // Approvals
  // =========================================================================

  async listApprovals(params: ApprovalListParams = {}): Promise<{ items: ApprovalBatch[] }> {
    return this.request('GET', '/v1/approvals', { params, idempotent: true });
  }

  async getApproval(batchId: string): Promise<ApprovalBatch> {
    return this.request('GET', `/v1/approvals/${encodeURIComponent(batchId)}`, { idempotent: true });
  }

  async decideApproval(batchId: string, decision: ApprovalDecision): Promise<ApprovalBatch> {
    return this.request('POST', `/v1/approvals/${encodeURIComponent(batchId)}/decision`, {
      body: decision,
    });
  }

  // =========================================================================
  // Threads
  // =========================================================================

  async createThread(request: { metadata?: Record<string, unknown> }): Promise<Thread> {
    return this.request('POST', '/v1/threads', { body: request });
  }

  async listThreads(params: { cursor?: string; search?: string } = {}): Promise<{ items: Thread[]; next_cursor?: string }> {
    return this.request('GET', '/v1/threads', { params, idempotent: true });
  }

  async getThread(threadId: string, params: ThreadHistoryParams = {}): Promise<Thread> {
    return this.request('GET', `/v1/threads/${encodeURIComponent(threadId)}`, { params, idempotent: true });
  }

  async branchThread(threadId: string, request: ThreadBranchRequest): Promise<ThreadBranchResponse> {
    return this.request('POST', `/v1/threads/${encodeURIComponent(threadId)}/branches`, {
      body: request,
    });
  }

  // =========================================================================
  // Agent Revisions
  // =========================================================================

  async listAgentRevisions(params: AgentRevisionListParams = {}): Promise<{ items: AgentRevision[]; next_cursor?: string }> {
    return this.request('GET', '/v1/agents/revisions', { params, idempotent: true });
  }

  async getAgentRevision(revisionId: string): Promise<AgentRevision> {
    return this.request('GET', `/v1/agents/revisions/${encodeURIComponent(revisionId)}`, { idempotent: true });
  }

  async draftAgent(request: AgentRevisionCreateRequest): Promise<AgentRevision> {
    return this.request('POST', '/v1/agents/revisions', { body: request });
  }

  async transitionAgent(revisionId: string, operation: 'validate' | 'publish'): Promise<AgentRevision> {
    return this.request('POST', `/v1/agents/revisions/${encodeURIComponent(revisionId)}/${operation}`);
  }

  // =========================================================================
  // Artifacts
  // =========================================================================

  async createArtifact(request: ArtifactCreateRequest, file: Blob): Promise<{ artifact_id: string; object_key: string; size_bytes: number; sha256: string }> {
    const formData = new FormData();
    formData.append('file', file);
    
    // Add metadata as JSON
    const metadata = { ...request };
    delete (metadata as any).file;
    formData.append('request', new Blob([JSON.stringify(metadata)], { type: 'application/json' }));

    return this.request('POST', '/v1/artifacts', {
      body: formData,
      headers: {
        // Don't set Content-Type - let browser set it with boundary
      },
    });
  }

  async getArtifact(artifactId: string): Promise<Artifact> {
    return this.request('GET', `/v1/artifacts/${encodeURIComponent(artifactId)}`, { idempotent: true });
  }

  async listArtifacts(params: ArtifactListParams): Promise<{ items: Artifact[]; next_cursor?: string }> {
    return this.request('GET', '/v1/artifacts', { params, idempotent: true });
  }

  async downloadArtifact(artifactId: string): Promise<Blob> {
    const response = await fetch(`${this.baseUrl}/v1/artifacts/${encodeURIComponent(artifactId)}/download`, {
      headers: this.getHeaders(),
    });
    
    if (response.status >= 400) {
      throw new HaaSAPIError('Failed to download artifact', response.status);
    }
    
    return response.blob();
  }

  async deleteArtifact(artifactId: string): Promise<void> {
    await this.request('DELETE', `/v1/artifacts/${encodeURIComponent(artifactId)}`);
  }

  // =========================================================================
  // Webhooks
  // =========================================================================

  async createWebhookEndpoint(request: WebhookEndpointCreateRequest): Promise<WebhookEndpoint> {
    return this.request('POST', '/v1/webhooks/endpoints', { body: request });
  }

  async listWebhookEndpoints(enabledOnly = true): Promise<WebhookEndpoint[]> {
    return this.request('GET', '/v1/webhooks/endpoints', { 
      params: { enabled_only: enabledOnly },
      idempotent: true 
    });
  }

  async getWebhookEndpoint(endpointId: string): Promise<WebhookEndpoint> {
    return this.request('GET', `/v1/webhooks/endpoints/${encodeURIComponent(endpointId)}`, { idempotent: true });
  }

  async updateWebhookEndpoint(endpointId: string, request: WebhookEndpointUpdateRequest): Promise<WebhookEndpoint> {
    return this.request('PATCH', `/v1/webhooks/endpoints/${encodeURIComponent(endpointId)}`, { body: request });
  }

  async deleteWebhookEndpoint(endpointId: string): Promise<void> {
    await this.request('DELETE', `/v1/webhooks/endpoints/${encodeURIComponent(endpointId)}`);
  }

  async createWebhookSigningKey(request: WebhookSigningKeyCreateRequest): Promise<WebhookSigningKey> {
    return this.request('POST', '/v1/webhooks/signing-keys', { body: request });
  }

  async listWebhookSigningKeys(activeOnly = true): Promise<WebhookSigningKey[]> {
    return this.request('GET', '/v1/webhooks/signing-keys', { 
      params: { active_only: activeOnly },
      idempotent: true 
    });
  }

  async rotateWebhookSigningKey(keyId: string, request: WebhookSigningKeyRotateRequest): Promise<WebhookSigningKey> {
    return this.request('POST', `/v1/webhooks/signing-keys/${encodeURIComponent(keyId)}/rotate`, { body: request });
  }

  async revokeWebhookSigningKey(keyId: string): Promise<void> {
    await this.request('POST', `/v1/webhooks/signing-keys/${encodeURIComponent(keyId)}/revoke`);
  }

  async listWebhookDeliveries(params: WebhookDeliveryListParams = {}): Promise<{ items: WebhookDelivery[]; next_cursor?: string }> {
    return this.request('GET', '/v1/webhooks/deliveries', { params, idempotent: true });
  }

  async getWebhookDelivery(deliveryId: string): Promise<WebhookDelivery> {
    return this.request('GET', `/v1/webhooks/deliveries/${encodeURIComponent(deliveryId)}`, { idempotent: true });
  }

  async replayWebhookDeliveries(request: WebhookReplayRequest): Promise<{ replayed: number }> {
    return this.request('POST', '/v1/webhooks/deliveries/replay', { body: request });
  }

  // =========================================================================
  // Quotas
  // =========================================================================

  async getQuota(): Promise<TenantQuota> {
    return this.request('GET', '/v1/quotas', { idempotent: true });
  }

  async setQuota(request: TenantQuotaRequest): Promise<TenantQuota> {
    return this.request('PUT', '/v1/quotas', { body: request });
  }

  // =========================================================================
  // Retention
  // =========================================================================

  async getRetentionPolicy(): Promise<RetentionPolicy> {
    return this.request('GET', '/v1/retention/policy', { idempotent: true });
  }

  async setRetentionPolicy(request: RetentionPolicyRequest): Promise<RetentionPolicy> {
    return this.request('PUT', '/v1/retention/policy', { body: request });
  }

  async createLegalHold(request: LegalHoldCreateRequest): Promise<LegalHold> {
    return this.request('POST', '/v1/retention/legal-holds', { body: request });
  }

  async listLegalHolds(scope?: string, activeOnly = true): Promise<LegalHold[]> {
    return this.request('GET', '/v1/retention/legal-holds', { 
      params: { scope, active_only: activeOnly },
      idempotent: true 
    });
  }

  async releaseLegalHold(holdId: string, releaseReason?: string): Promise<LegalHold> {
    return this.request('POST', `/v1/retention/legal-holds/${encodeURIComponent(holdId)}/release`, {
      body: { release_reason: releaseReason },
    });
  }

  async createErasureRequest(request: ErasureRequestCreateRequest): Promise<ErasureRequest> {
    return this.request('POST', '/v1/retention/erasure-requests', { body: request });
  }

  async listErasureRequests(status?: string): Promise<ErasureRequest[]> {
    return this.request('GET', '/v1/retention/erasure-requests', { 
      params: { status },
      idempotent: true 
    });
  }

  async listRetentionTombstones(dataClass?: string, subjectType?: string, subjectId?: string): Promise<RetentionTombstone[]> {
    return this.request('GET', '/v1/retention/tombstones', { 
      params: { data_class: dataClass, subject_type: subjectType, subject_id: subjectId },
      idempotent: true 
    });
  }

  // =========================================================================
  // Secrets & Credentials
  // =========================================================================

  async createSecretBinding(request: SecretBindingCreateRequest): Promise<SecretBinding> {
    return this.request('POST', '/v1/secrets/bindings', { body: request });
  }

  async listSecretBindings(includeDeleted = false): Promise<SecretBinding[]> {
    return this.request('GET', '/v1/secrets/bindings', { 
      params: { include_deleted: includeDeleted },
      idempotent: true 
    });
  }

  async getSecretBinding(bindingId: string): Promise<SecretBinding> {
    return this.request('GET', `/v1/secrets/bindings/${encodeURIComponent(bindingId)}`, { idempotent: true });
  }

  async updateSecretBinding(bindingId: string, request: SecretBindingUpdateRequest): Promise<SecretBinding> {
    return this.request('PATCH', `/v1/secrets/bindings/${encodeURIComponent(bindingId)}`, { body: request });
  }

  async deleteSecretBinding(bindingId: string): Promise<void> {
    await this.request('DELETE', `/v1/secrets/bindings/${encodeURIComponent(bindingId)}`);
  }

  async createCredentialGrant(request: CredentialGrantCreateRequest): Promise<CredentialGrant> {
    return this.request('POST', '/v1/credentials/grants', { body: request });
  }

  async getCredentialGrant(grantId: string): Promise<CredentialGrant> {
    return this.request('GET', `/v1/credentials/grants/${encodeURIComponent(grantId)}`, { idempotent: true });
  }

  async redeemCredentialGrant(grantId: string): Promise<CredentialGrantRedeemResponse> {
    return this.request('POST', `/v1/credentials/grants/${encodeURIComponent(grantId)}/redeem`);
  }

  // =========================================================================
  // Egress
  // =========================================================================

  async getEgressPolicy(): Promise<EgressPolicy> {
    return this.request('GET', '/v1/egress/policy', { idempotent: true });
  }

  async setEgressPolicy(request: EgressPolicyRequest): Promise<EgressPolicy> {
    return this.request('PUT', '/v1/egress/policy', { body: request });
  }

  async checkEgress(url: string): Promise<EgressCheckResponse> {
    return this.request('POST', '/v1/egress/check', { 
      params: { url },
      idempotent: true 
    });
  }

  // =========================================================================
  // Redaction
  // =========================================================================

  async createRedactionRule(request: RedactionRuleCreateRequest): Promise<RedactionRule> {
    return this.request('POST', '/v1/redaction/rules', { body: request });
  }

  async listRedactionRules(): Promise<RedactionRule[]> {
    return this.request('GET', '/v1/redaction/rules', { idempotent: true });
  }

  async testRedaction(request: RedactionTestRequest): Promise<RedactionTestResponse> {
    return this.request('POST', '/v1/redaction/test', { body: request });
  }

  // =========================================================================
  // Isolation
  // =========================================================================

  async listStaticCatalog(enabledOnly = true, category?: string): Promise<StaticCatalogEntry[]> {
    return this.request('GET', '/v1/isolation/catalog', { 
      params: { enabled_only: enabledOnly, category },
      idempotent: true 
    });
  }

  async getStaticCatalogEntry(pluginId: string): Promise<StaticCatalogEntry> {
    return this.request('GET', `/v1/isolation/catalog/${encodeURIComponent(pluginId)}`, { idempotent: true });
  }

  async packPlugin(request: PackPluginRequest): Promise<PackPluginResponse> {
    return this.request('POST', '/v1/isolation/pack', { body: request });
  }

  async materializePlugin(request: MaterializeRequest): Promise<MaterializeResponse> {
    return this.request('POST', '/v1/isolation/materialize', { body: request });
  }

  async executeInSandbox(request: SandboxExecuteRequest): Promise<SandboxExecuteResponse> {
    return this.request('POST', '/v1/isolation/sandbox/execute', { body: request });
  }

  async cleanupSandbox(runId: string, invocationId: string): Promise<{ status: string }> {
    return this.request('DELETE', `/v1/isolation/sandbox/${encodeURIComponent(runId)}/${encodeURIComponent(invocationId)}`);
  }

  // =========================================================================
  // Marketplace
  // =========================================================================

  async createPublisher(request: PublisherCreateRequest): Promise<Publisher> {
    return this.request('POST', '/v1/marketplace/publishers', { body: request });
  }

  async listPublishers(status?: string, verification?: string): Promise<Publisher[]> {
    return this.request('GET', '/v1/marketplace/publishers', { 
      params: { status, verification },
      idempotent: true 
    });
  }

  async getPublisher(publisherId: string): Promise<Publisher> {
    return this.request('GET', `/v1/marketplace/publishers/${encodeURIComponent(publisherId)}`, { idempotent: true });
  }

  async updatePublisher(publisherId: string, request: PublisherUpdateRequest): Promise<Publisher> {
    return this.request('PATCH', `/v1/marketplace/publishers/${encodeURIComponent(publisherId)}`, { body: request });
  }

  async createPublisherKey(publisherId: string, request: PublisherKeyCreateRequest): Promise<PublisherKey> {
    return this.request('POST', `/v1/marketplace/publishers/${encodeURIComponent(publisherId)}/keys`, { body: request });
  }

  async listPublisherKeys(publisherId: string): Promise<PublisherKey[]> {
    return this.request('GET', `/v1/marketplace/publishers/${encodeURIComponent(publisherId)}/keys`, { idempotent: true });
  }

  async revokePublisherKey(publisherId: string, keyId: string, reason?: string): Promise<void> {
    await this.request('POST', `/v1/marketplace/publishers/${encodeURIComponent(publisherId)}/keys/${encodeURIComponent(keyId)}/revoke`, {
      body: { reason },
    });
  }

  async createListing(request: ListingCreateRequest): Promise<Listing> {
    return this.request('POST', '/v1/marketplace/listings', { body: request });
  }

  async listListings(publisherId?: string, status?: string, visibility?: string): Promise<Listing[]> {
    return this.request('GET', '/v1/marketplace/listings', { 
      params: { publisher_id: publisherId, status, visibility },
      idempotent: true 
    });
  }

  async getListing(listingId: string): Promise<Listing> {
    return this.request('GET', `/v1/marketplace/listings/${encodeURIComponent(listingId)}`, { idempotent: true });
  }

  async updateListing(listingId: string, request: ListingUpdateRequest): Promise<Listing> {
    return this.request('PATCH', `/v1/marketplace/listings/${encodeURIComponent(listingId)}`, { body: request });
  }

  async createRevision(request: RevisionCreateRequest): Promise<Revision> {
    return this.request('POST', '/v1/marketplace/revisions', { body: request });
  }

  async listRevisions(listingId?: string, reviewState?: string, tier?: string): Promise<Revision[]> {
    return this.request('GET', '/v1/marketplace/revisions', { 
      params: { listing_id: listingId, review_state: reviewState, tier },
      idempotent: true 
    });
  }

  async getRevision(revisionId: string): Promise<Revision> {
    return this.request('GET', `/v1/marketplace/revisions/${encodeURIComponent(revisionId)}`, { idempotent: true });
  }

  async reviewRevision(revisionId: string, request: RevisionReviewRequest): Promise<Revision> {
    return this.request('POST', `/v1/marketplace/revisions/${encodeURIComponent(revisionId)}/review`, { body: request });
  }

  async yankRevision(revisionId: string, reason: string): Promise<void> {
    await this.request('POST', `/v1/marketplace/revisions/${encodeURIComponent(revisionId)}/yank`, {
      body: { reason },
    });
  }

  async createInstallation(request: InstallationCreateRequest): Promise<Installation> {
    return this.request('POST', '/v1/marketplace/installations', { body: request });
  }

  async listInstallations(status?: string): Promise<Installation[]> {
    return this.request('GET', '/v1/marketplace/installations', { 
      params: { status },
      idempotent: true 
    });
  }

  async getInstallation(installationId: string): Promise<Installation> {
    return this.request('GET', `/v1/marketplace/installations/${encodeURIComponent(installationId)}`, { idempotent: true });
  }

  async updateInstallation(installationId: string, request: InstallationUpdateRequest): Promise<Installation> {
    return this.request('PATCH', `/v1/marketplace/installations/${encodeURIComponent(installationId)}`, { body: request });
  }

  async listNotifications(unacknowledgedOnly = true): Promise<Notification[]> {
    return this.request('GET', '/v1/marketplace/notifications', { 
      params: { unacknowledged_only: unacknowledgedOnly },
      idempotent: true 
    });
  }

  async acknowledgeNotification(notificationId: string): Promise<void> {
    await this.request('POST', `/v1/marketplace/notifications/${encodeURIComponent(notificationId)}/acknowledge`);
  }

  async createAbuseReport(request: AbuseReportCreateRequest): Promise<AbuseReport> {
    return this.request('POST', '/v1/marketplace/abuse-reports', { body: request });
  }

  async listAbuseReports(status?: string): Promise<AbuseReport[]> {
    return this.request('GET', '/v1/marketplace/abuse-reports', { 
      params: { status },
      idempotent: true 
    });
  }

  async searchMarketplace(request: SearchRequest): Promise<SearchResponse> {
    return this.request('POST', '/v1/marketplace/search', { body: request });
  }

  async getTransparencyLog(afterSeq = 0, limit = 100): Promise<TransparencyLogResponse> {
    return this.request('GET', '/v1/marketplace/transparency-log', { 
      params: { after_seq: afterSeq, limit },
      idempotent: true 
    });
  }

  // =========================================================================
  // Agent Mesh
  // =========================================================================

  async createDelegation(request: DelegationCreateRequest): Promise<Delegation> {
    return this.request('POST', '/v1/agent-mesh/delegations', { body: request });
  }

  async listDelegations(parentRunId?: string, status?: string): Promise<Delegation[]> {
    return this.request('GET', '/v1/agent-mesh/delegations', { 
      params: { parent_run_id: parentRunId, status },
      idempotent: true 
    });
  }

  async getDelegation(delegationId: string): Promise<Delegation> {
    return this.request('GET', `/v1/agent-mesh/delegations/${encodeURIComponent(delegationId)}`, { idempotent: true });
  }

  async sendAgentMessage(request: AgentMessageCreateRequest): Promise<AgentMessage> {
    return this.request('POST', '/v1/agent-mesh/messages', { body: request });
  }

  async listAgentMessages(runId: string, fromAgent?: string, toAgent?: string, messageType?: string, limit = 100): Promise<AgentMessage[]> {
    return this.request('GET', '/v1/agent-mesh/messages', { 
      params: { run_id: runId, from_agent: fromAgent, to_agent: toAgent, message_type: messageType, limit },
      idempotent: true 
    });
  }

  async writeWorkspaceEntry(request: WorkspaceEntryCreateRequest): Promise<WorkspaceEntry> {
    return this.request('POST', '/v1/agent-mesh/workspace', { body: request });
  }

  async readWorkspaceEntry(runId: string, agentId: string, key: string): Promise<WorkspaceEntry> {
    return this.request('GET', `/v1/agent-mesh/workspace/${encodeURIComponent(runId)}/${encodeURIComponent(agentId)}/${encodeURIComponent(key)}`, { idempotent: true });
  }

  async listWorkspaceEntries(runId: string, agentId: string): Promise<WorkspaceEntry[]> {
    return this.request('GET', `/v1/agent-mesh/workspace/${encodeURIComponent(runId)}/${encodeURIComponent(agentId)}`, { idempotent: true });
  }

  async createAgentCard(request: AgentCardCreateRequest): Promise<AgentCard> {
    return this.request('POST', '/v1/agent-mesh/agent-cards', { body: request });
  }

  async listAgentCards(agentRevisionId?: string): Promise<AgentCard[]> {
    return this.request('GET', '/v1/agent-mesh/agent-cards', { 
      params: { agent_revision_id: agentRevisionId },
      idempotent: true 
    });
  }

  async getAgentCard(cardId: string): Promise<AgentCard> {
    return this.request('GET', `/v1/agent-mesh/agent-cards/${encodeURIComponent(cardId)}`, { idempotent: true });
  }

  async createTopology(request: TopologyCreateRequest): Promise<Topology> {
    return this.request('POST', '/v1/agent-mesh/topologies', { body: request });
  }

  async listTopologies(): Promise<Topology[]> {
    return this.request('GET', '/v1/agent-mesh/topologies', { idempotent: true });
  }

  async getRunTree(runId: string): Promise<RunTreeNode> {
    return this.request('GET', `/v1/runs/${encodeURIComponent(runId)}/tree`, { idempotent: true });
  }

  async sendMailboxMessage(request: MailboxMessageCreateRequest): Promise<MailboxMessage> {
    return this.request('POST', '/v1/agent-mesh/mailbox/send', { body: request });
  }

  async getMailboxMessages(mailboxId: string, recipient?: string, limit = 100): Promise<MailboxMessage[]> {
    return this.request('GET', `/v1/agent-mesh/mailbox/${encodeURIComponent(mailboxId)}`, { 
      params: { recipient, limit },
      idempotent: true 
    });
  }

  async getA2AAgentCard(agentRevisionId: string): Promise<A2AAgentCard> {
    return this.request('GET', `/v1/agent-mesh/a2a/agent-cards/${encodeURIComponent(agentRevisionId)}`, { idempotent: true });
  }

  async createMCPServer(request: MCPServerConfigCreateRequest): Promise<MCPServerConfig> {
    return this.request('POST', '/v1/agent-mesh/mcp/servers', { body: request });
  }

  async listMCPServers(): Promise<MCPServerConfig[]> {
    return this.request('GET', '/v1/agent-mesh/mcp/servers', { idempotent: true });
  }

  async getRunObservability(runId: string): Promise<RunObservability> {
    return this.request('GET', `/v1/agent-mesh/observability/${encodeURIComponent(runId)}`, { idempotent: true });
  }

  // =========================================================================
  // Operations
  // =========================================================================

  async createCell(request: CellCreateRequest): Promise<Cell> {
    return this.request('POST', '/v1/ops/cells', { body: request });
  }

  async listCells(): Promise<Cell[]> {
    return this.request('GET', '/v1/ops/cells', { idempotent: true });
  }

  async getCell(cellId: string): Promise<Cell> {
    return this.request('GET', `/v1/ops/cells/${encodeURIComponent(cellId)}`, { idempotent: true });
  }

  async updateCell(cellId: string, status: 'active' | 'draining' | 'drained'): Promise<Cell> {
    return this.request('PATCH', `/v1/ops/cells/${encodeURIComponent(cellId)}`, { 
      params: { status },
    });
  }

  async listPlacements(cellId?: string, isDefault = false): Promise<TenantPlacement[]> {
    return this.request('GET', '/v1/ops/placements', { 
      params: { cell_id: cellId, is_default: isDefault },
      idempotent: true 
    });
  }

  async beginTenantMove(tenantId: string, request: TenantMoveRequest): Promise<TenantMoveResponse> {
    return this.request('POST', `/v1/ops/placements/${encodeURIComponent(tenantId)}/move`, { body: request });
  }

  async finishTenantMove(tenantId: string, request: TenantMoveFinishRequest): Promise<TenantMoveResponse> {
    return this.request('POST', `/v1/ops/placements/${encodeURIComponent(tenantId)}/move/finish`, { body: request });
  }

  async getPlatformMetrics(): Promise<PlatformMetrics> {
    return this.request('GET', '/v1/ops/metrics', { idempotent: true });
  }

  async bumpRestoreEpoch(request: RestoreEpochRequest): Promise<RestoreEpochResponse> {
    return this.request('POST', '/v1/ops/restore-epoch', { body: request });
  }

  async fenceRuns(tenantId?: string): Promise<FenceRunsResponse> {
    return this.request('POST', '/v1/ops/fence-runs', { 
      params: { tenant_id: tenantId },
    });
  }

  // =========================================================================
  // Utility
  // =========================================================================

  setApiKey(apiKey: string | undefined): void {
    this.apiKey = apiKey;
    this.authExpired = false;
  }

  setTenant(tenant: string): void {
    this.tenant = tenant;
  }

  setBaseUrl(baseUrl: string): void {
    this.baseUrl = baseUrl.replace(/\/$/, '');
  }

  isAuthExpired(): boolean {
    return this.authExpired;
  }
}

/**
 * Create a client from environment variables
 * @param env - Optional environment variables object (defaults to process.env in Node.js)
 */
export function createClientFromEnv(env?: Record<string, string | undefined>): HaaSClient {
  // Use provided env, or fall back to process.env in Node.js environments
  // Use globalThis.process to avoid TypeScript errors when @types/node is not installed
  const envVars = env || (typeof globalThis !== 'undefined' && 'process' in globalThis && (globalThis as any).process?.env ? (globalThis as any).process.env : {});
  
  console.log({
    baseUrl: envVars.HAAS_API_URL || 'http://localhost:8000',
    apiKey: envVars.HAAS_API_KEY,
    tenant: envVars.HAAS_TENANT_ID,
  })

  return new HaaSClient({
    baseUrl: envVars.HAAS_API_URL || 'http://localhost:8000',
    apiKey: envVars.HAAS_API_KEY,
    tenant: envVars.HAAS_TENANT_ID,
  });
}

export default HaaSClient;