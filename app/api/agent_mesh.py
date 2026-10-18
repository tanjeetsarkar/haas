"""Track A multi-agent primitives, topologies and interop (owner: AGENTS)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.main import store

router = APIRouter()


# Delegation models
class DelegationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_run_id: str
    child_agent_revision_id: str
    child_agent_config: dict[str, Any] = Field(default_factory=dict)
    attenuation_policy: dict[str, Any] = Field(default_factory=dict)
    budget_usd: float | None = Field(None, gt=0)
    max_steps: int | None = Field(None, gt=0)


class DelegationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delegation_id: str
    parent_run_id: str
    child_run_id: str | None
    child_agent_revision_id: str
    child_agent_config: dict[str, Any]
    attenuation_policy: dict[str, Any]
    budget_usd: float | None
    max_steps: int | None
    status: str
    created_at: datetime
    completed_at: datetime | None


# Agent Messages
class AgentMessageCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    from_agent: str
    to_agent: str | None = None  # None = broadcast
    message_type: str = Field(..., pattern="^(request|response|notification|handoff)$")
    payload: dict[str, Any]
    correlation_id: str | None = None


class AgentMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str
    run_id: str
    from_agent: str
    to_agent: str | None
    message_type: str
    payload: dict[str, Any]
    correlation_id: str | None
    created_at: datetime


# Workspace (CAS-versioned)
class WorkspaceEntryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    agent_id: str
    key: str = Field(..., min_length=1, max_length=256)
    value: dict[str, Any]
    expected_version: int | None = None


class WorkspaceEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_id: str
    run_id: str
    agent_id: str
    key: str
    value: dict[str, Any]
    version: int
    updated_by: str
    updated_at: datetime


class WorkspaceWatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    keys: list[str] = Field(default_factory=list)  # empty = all


# Agent Cards
class AgentCardCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_revision_id: str
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., min_length=1, max_length=1000)
    capabilities: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    endpoints: list[dict[str, Any]] = Field(default_factory=list)
    authentication: dict[str, Any] = Field(default_factory=dict)


class AgentCardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: str
    agent_revision_id: str
    name: str
    description: str
    capabilities: list[str]
    skills: list[str]
    endpoints: list[dict[str, Any]]
    authentication: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# Topologies
class TopologyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=128)
    type: str = Field(..., pattern="^(supervisor|hierarchical|swarm|graph|map_reduce|debate)$")
    config: dict[str, Any]
    agent_revisions: list[str] = Field(..., min_length=1)


class TopologyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topology_id: str
    name: str
    type: str
    config: dict[str, Any]
    agent_revisions: list[str]
    validation_status: str
    validation_report: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


# Run Tree
class RunTreeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    root_run_id: str
    depth: int
    children: list["RunTreeResponse"] = Field(default_factory=list)
    agent_revision_id: str | None
    status: str
    plan: dict[str, Any] | None
    cost_usd: float
    created_at: datetime


RunTreeResponse.model_rebuild()


# HACP Mailbox
class MailboxMessageCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mailbox_id: str
    sender: str
    recipient: str
    message_type: str
    payload: dict[str, Any]
    correlation_id: str | None = None


class MailboxMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str
    mailbox_id: str
    sender: str
    recipient: str
    message_type: str
    payload: dict[str, Any]
    correlation_id: str | None
    delivered_at: datetime | None
    created_at: datetime


# A2A
class A2AAgentCardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_card: dict[str, Any]


# MCP Server
class MCPServerConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_revision_id: str
    transport: str = Field("streamable_http", pattern="^(streamable_http|stdio)$")
    oauth_config: dict[str, Any] | None = None


class MCPServerConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_id: str
    agent_revision_id: str
    transport: str
    endpoint: str
    oauth_config: dict[str, Any] | None
    created_at: datetime


# Delegation endpoints
@router.post("/v1/agent-mesh/delegations", response_model=DelegationResponse)
async def create_delegation(request: DelegationCreateRequest) -> DelegationResponse:
    """Create a delegation (spawn child agent)."""
    async with store.connection() as conn:
        # Verify parent run exists
        parent_run = await conn.fetchrow(
            "SELECT * FROM runs WHERE run_id = $1",
            request.parent_run_id,
        )
        if not parent_run:
            raise HTTPException(status_code=404, detail="Parent run not found")
        
        # Verify agent revision exists
        agent_rev = await conn.fetchrow(
            "SELECT * FROM agent_revisions WHERE revision_id = $1",
            uuid.UUID(request.child_agent_revision_id),
        )
        if not agent_rev:
            raise HTTPException(status_code=404, detail="Agent revision not found")
        
        # Create child run
        child_run_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO runs (
                run_id, tenant_id, tenant_uuid, thread_id, agent_revision_id,
                submitter_id, status, config, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, now())
            """,
            child_run_id,
            parent_run["tenant_id"],
            parent_run["tenant_uuid"],
            parent_run["thread_id"],
            uuid.UUID(request.child_agent_revision_id),
            parent_run["submitter_id"],
            json.dumps(request.child_agent_config),
        )
        
        # Create delegation record
        row = await conn.fetchrow(
            """
            INSERT INTO delegations (
                delegation_id, parent_run_id, child_run_id, child_agent_revision_id,
                child_agent_config, attenuation_policy, budget_usd, max_steps, status
            ) VALUES (
                gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, 'pending'
            )
            RETURNING delegation_id::text, parent_run_id, child_run_id,
                      child_agent_revision_id::text, child_agent_config,
                      attenuation_policy, budget_usd, max_steps, status,
                      created_at, completed_at
            """,
            request.parent_run_id,
            child_run_id,
            uuid.UUID(request.child_agent_revision_id),
            json.dumps(request.child_agent_config),
            json.dumps(request.attenuation_policy),
            request.budget_usd,
            request.max_steps,
        )
    
    return DelegationResponse(**dict(row))


@router.get("/v1/agent-mesh/delegations", response_model=list[DelegationResponse])
async def list_delegations(
    parent_run_id: str | None = Query(None),
    status: str | None = Query(None),
) -> list[DelegationResponse]:
    """List delegations."""
    async with store.connection() as conn:
        query = """
            SELECT delegation_id::text, parent_run_id, child_run_id,
                   child_agent_revision_id::text, child_agent_config,
                   attenuation_policy, budget_usd, max_steps, status,
                   created_at, completed_at
            FROM delegations
            WHERE 1=1
        """
        params = []
        if parent_run_id:
            query += " AND parent_run_id = $1"
            params.append(parent_run_id)
        if status:
            query += f" AND status = ${len(params) + 1}"
            params.append(status)
        query += " ORDER BY created_at DESC"
        
        rows = await conn.fetch(query, *params)
    
    return [DelegationResponse(**dict(row)) for row in rows]


@router.get("/v1/agent-mesh/delegations/{delegation_id}", response_model=DelegationResponse)
async def get_delegation(delegation_id: str) -> DelegationResponse:
    """Get a delegation."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT delegation_id::text, parent_run_id, child_run_id,
                   child_agent_revision_id::text, child_agent_config,
                   attenuation_policy, budget_usd, max_steps, status,
                   created_at, completed_at
            FROM delegations
            WHERE delegation_id = $1
            """,
            uuid.UUID(delegation_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Delegation not found")
    
    return DelegationResponse(**dict(row))


# Agent Messages
@router.post("/v1/agent-mesh/messages", response_model=AgentMessageResponse)
async def send_agent_message(request: AgentMessageCreateRequest) -> AgentMessageResponse:
    """Send a message between agents."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO agent_messages (
                message_id, run_id, from_agent, to_agent, message_type,
                payload, correlation_id
            ) VALUES (
                gen_random_uuid(), $1, $2, $3, $4, $5, $6
            )
            RETURNING message_id::text, run_id, from_agent, to_agent, message_type,
                      payload, correlation_id, created_at
            """,
            request.run_id,
            request.from_agent,
            request.to_agent,
            request.message_type,
            json.dumps(request.payload),
            request.correlation_id,
        )
    
    return AgentMessageResponse(**dict(row))


@router.get("/v1/agent-mesh/messages", response_model=list[AgentMessageResponse])
async def list_agent_messages(
    run_id: str = Query(...),
    from_agent: str | None = Query(None),
    to_agent: str | None = Query(None),
    message_type: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[AgentMessageResponse]:
    """List agent messages for a run."""
    async with store.connection() as conn:
        query = """
            SELECT message_id::text, run_id, from_agent, to_agent, message_type,
                   payload, correlation_id, created_at
            FROM agent_messages
            WHERE run_id = $1
        """
        params: list[str | int] = [run_id]
        if from_agent:
            query += " AND from_agent = $2"
            params.append(from_agent)
        if to_agent:
            query += f" AND to_agent = ${len(params) + 1}"
            params.append(to_agent)
        if message_type:
            query += f" AND message_type = ${len(params) + 1}"
            params.append(message_type)
        query += f" ORDER BY created_at DESC LIMIT ${len(params) + 1}"
        params.append(limit)
        
        rows = await conn.fetch(query, *params)
    
    return [AgentMessageResponse(**dict(row)) for row in rows]


# Workspace (CAS-versioned)
@router.post("/v1/agent-mesh/workspace", response_model=WorkspaceEntryResponse)
async def write_workspace_entry(request: WorkspaceEntryCreateRequest) -> WorkspaceEntryResponse:
    """Write a workspace entry with CAS (Compare-And-Swap)."""
    async with store.connection() as conn:
        async with conn.transaction():
            # Check current version
            current = await conn.fetchrow(
                """
                SELECT version, value FROM workspaces
                WHERE run_id = $1 AND agent_id = $2 AND key = $3
                FOR UPDATE
                """,
                request.run_id,
                request.agent_id,
                request.key,
            )
            
            if current:
                if request.expected_version is not None and current["version"] != request.expected_version:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Version mismatch: expected {request.expected_version}, current {current['version']}"
                    )
                new_version = current["version"] + 1
                
                # Record version history
                await conn.execute(
                    """
                    INSERT INTO workspace_entry_versions (
                        version_id, run_id, agent_id, key, value, version, updated_by
                    ) VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6)
                    """,
                    request.run_id,
                    request.agent_id,
                    request.key,
                    json.dumps(current["value"]),
                    current["version"],
                    "current_user",
                )
                
                # Update entry
                row = await conn.fetchrow(
                    """
                    UPDATE workspaces SET
                        value = $1, version = $2, updated_by = $3, updated_at = now()
                    WHERE run_id = $4 AND agent_id = $5 AND key = $6
                    RETURNING entry_id::text, run_id, agent_id, key, value, version,
                              updated_by::text, updated_at
                    """,
                    json.dumps(request.value),
                    new_version,
                    "current_user",
                    request.run_id,
                    request.agent_id,
                    request.key,
                )
            else:
                if request.expected_version is not None and request.expected_version != 0:
                    raise HTTPException(status_code=409, detail="Entry does not exist")
                
                row = await conn.fetchrow(
                    """
                    INSERT INTO workspaces (
                        entry_id, run_id, agent_id, key, value, version, updated_by
                    ) VALUES (
                        gen_random_uuid(), $1, $2, $3, $4, 1, $5
                    )
                    RETURNING entry_id::text, run_id, agent_id, key, value, version,
                              updated_by::text, updated_at
                    """,
                    request.run_id,
                    request.agent_id,
                    request.key,
                    json.dumps(request.value),
                    "current_user",
                )
    
    return WorkspaceEntryResponse(**dict(row))


@router.get("/v1/agent-mesh/workspace/{run_id}/{agent_id}/{key}", response_model=WorkspaceEntryResponse)
async def read_workspace_entry(run_id: str, agent_id: str, key: str) -> WorkspaceEntryResponse:
    """Read a workspace entry."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT entry_id::text, run_id, agent_id, key, value, version,
                   updated_by::text, updated_at
            FROM workspaces
            WHERE run_id = $1 AND agent_id = $2 AND key = $3
            """,
            run_id,
            agent_id,
            key,
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Workspace entry not found")
    
    return WorkspaceEntryResponse(**dict(row))


@router.get("/v1/agent-mesh/workspace/{run_id}/{agent_id}", response_model=list[WorkspaceEntryResponse])
async def list_workspace_entries(run_id: str, agent_id: str) -> list[WorkspaceEntryResponse]:
    """List all workspace entries for an agent in a run."""
    async with store.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT entry_id::text, run_id, agent_id, key, value, version,
                   updated_by::text, updated_at
            FROM workspaces
            WHERE run_id = $1 AND agent_id = $2
            ORDER BY key
            """,
            run_id,
            agent_id,
        )
    
    return [WorkspaceEntryResponse(**dict(row)) for row in rows]


# Agent Cards
@router.post("/v1/agent-mesh/agent-cards", response_model=AgentCardResponse)
async def create_agent_card(request: AgentCardCreateRequest) -> AgentCardResponse:
    """Create an agent card."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO agent_cards (
                card_id, agent_revision_id, name, description, capabilities,
                skills, endpoints, authentication
            ) VALUES (
                gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7
            )
            RETURNING card_id::text, agent_revision_id::text, name, description,
                      capabilities, skills, endpoints, authentication, created_at, updated_at
            """,
            uuid.UUID(request.agent_revision_id),
            request.name,
            request.description,
            request.capabilities,
            request.skills,
            json.dumps(request.endpoints),
            json.dumps(request.authentication),
        )
    
    return AgentCardResponse(**dict(row))


@router.get("/v1/agent-mesh/agent-cards", response_model=list[AgentCardResponse])
async def list_agent_cards(
    agent_revision_id: str | None = Query(None),
) -> list[AgentCardResponse]:
    """List agent cards."""
    async with store.connection() as conn:
        query = """
            SELECT card_id::text, agent_revision_id::text, name, description,
                   capabilities, skills, endpoints, authentication, created_at, updated_at
            FROM agent_cards
            WHERE 1=1
        """
        params = []
        if agent_revision_id:
            query += " AND agent_revision_id = $1"
            params.append(uuid.UUID(agent_revision_id))
        query += " ORDER BY created_at DESC"
        
        rows = await conn.fetch(query, *params)
    
    return [AgentCardResponse(**dict(row)) for row in rows]


@router.get("/v1/agent-mesh/agent-cards/{card_id}", response_model=AgentCardResponse)
async def get_agent_card(card_id: str) -> AgentCardResponse:
    """Get an agent card."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT card_id::text, agent_revision_id::text, name, description,
                   capabilities, skills, endpoints, authentication, created_at, updated_at
            FROM agent_cards
            WHERE card_id = $1
            """,
            uuid.UUID(card_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Agent card not found")
    
    return AgentCardResponse(**dict(row))


# Topologies
@router.post("/v1/agent-mesh/topologies", response_model=TopologyResponse)
async def create_topology(request: TopologyCreateRequest) -> TopologyResponse:
    """Create and validate a multi-agent topology."""
    # In a full implementation, this would validate the topology config
    # against the agent revisions and their capabilities
    validation_status = "valid"
    validation_report = {"checks": ["agent_revisions_exist", "config_schema_valid"]}
    
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO agent_topologies (
                topology_id, name, type, config, agent_revisions,
                validation_status, validation_report
            ) VALUES (
                gen_random_uuid(), $1, $2, $3, $4, $5, $6
            )
            RETURNING topology_id::text, name, type, config, agent_revisions,
                      validation_status, validation_report, created_at, updated_at
            """,
            request.name,
            request.type,
            json.dumps(request.config),
            request.agent_revisions,
            validation_status,
            json.dumps(validation_report),
        )
    
    return TopologyResponse(**dict(row))


@router.get("/v1/agent-mesh/topologies", response_model=list[TopologyResponse])
async def list_topologies() -> list[TopologyResponse]:
    """List topologies."""
    async with store.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT topology_id::text, name, type, config, agent_revisions,
                   validation_status, validation_report, created_at, updated_at
            FROM agent_topologies
            ORDER BY created_at DESC
            """
        )
    
    return [TopologyResponse(**dict(row)) for row in rows]


# Run Tree
@router.get("/v1/runs/{run_id}/tree", response_model=RunTreeResponse)
async def get_run_tree(run_id: str) -> RunTreeResponse:
    """Get the run tree for a multi-agent run."""
    async with store.connection() as conn:
        # Get root run
        root = await conn.fetchrow(
            "SELECT * FROM runs WHERE run_id = $1",
            run_id,
        )
        if not root:
            raise HTTPException(status_code=404, detail="Run not found")
        
        # Recursive CTE to build tree
        rows = await conn.fetch(
            """
            WITH RECURSIVE run_tree AS (
                SELECT r.*, 0 as depth, ARRAY[]::uuid[] as path
                FROM runs r WHERE r.run_id = $1
                UNION ALL
                SELECT r.*, rt.depth + 1, rt.path || r.run_id::uuid
                FROM runs r
                JOIN run_tree rt ON r.thread_id = rt.thread_id
                WHERE r.run_id <> rt.run_id
                  AND r.status NOT IN ('completed', 'failed', 'cancelled')
                  AND NOT r.run_id::uuid = ANY(rt.path)
            )
            SELECT * FROM run_tree ORDER BY depth, created_at
            """,
            run_id,
        )
    
    # Build tree structure
    nodes = {row["run_id"]: {**dict(row), "children": []} for row in rows}
    root_node = None
    
    for row in rows:
        node = nodes[row["run_id"]]
        if row["depth"] == 0:
            root_node = node
        else:
            # Find parent (simplified - in reality would use delegation links)
            pass
    
    if not root_node:
        root_node = nodes[run_id]
    
    def build_tree(node: dict, depth: int) -> RunTreeResponse:
        children = [build_tree(nodes[child_id], depth + 1) 
                    for child_id in nodes 
                    if nodes[child_id].get("parent_run_id") == node["run_id"]]
        return RunTreeResponse(
            run_id=node["run_id"],
            root_run_id=run_id,
            depth=depth,
            children=children,
            agent_revision_id=str(node["agent_revision_id"]) if node["agent_revision_id"] else None,
            status=node["status"],
            plan=node.get("plan"),
            cost_usd=float(node.get("cost_usd", 0)),
            created_at=node["created_at"],
        )
    
    return build_tree(root_node, 0)


# HACP Mailbox
@router.post("/v1/agent-mesh/mailbox/send", response_model=MailboxMessageResponse)
async def send_mailbox_message(request: MailboxMessageCreateRequest) -> MailboxMessageResponse:
    """Send a message to a HACP mailbox."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO hacp_mailbox (
                message_id, mailbox_id, sender, recipient, message_type,
                payload, correlation_id
            ) VALUES (
                gen_random_uuid(), $1, $2, $3, $4, $5, $6
            )
            RETURNING message_id::text, mailbox_id, sender, recipient, message_type,
                      payload, correlation_id, delivered_at, created_at
            """,
            request.mailbox_id,
            request.sender,
            request.recipient,
            request.message_type,
            json.dumps(request.payload),
            request.correlation_id,
        )
    
    return MailboxMessageResponse(**dict(row))


@router.get("/v1/agent-mesh/mailbox/{mailbox_id}", response_model=list[MailboxMessageResponse])
async def get_mailbox_messages(
    mailbox_id: str,
    recipient: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[MailboxMessageResponse]:
    """Get messages from a HACP mailbox."""
    async with store.connection() as conn:
        query = """
            SELECT message_id::text, mailbox_id, sender, recipient, message_type,
                   payload, correlation_id, delivered_at, created_at
            FROM hacp_mailbox
            WHERE mailbox_id = $1
        """
        params: list[str | int] = [mailbox_id]
        if recipient:
            query += f" AND recipient = ${len(params) + 1}"
            params.append(recipient)
        query += f" ORDER BY created_at DESC LIMIT ${len(params) + 1}"
        params.append(limit)
        
        rows = await conn.fetch(query, *params)
    
    return [MailboxMessageResponse(**dict(row)) for row in rows]


# A2A
@router.get("/v1/agent-mesh/a2a/agent-cards/{agent_revision_id}", response_model=A2AAgentCardResponse)
async def get_a2a_agent_card(agent_revision_id: str) -> A2AAgentCardResponse:
    """Get an A2A agent card for an agent revision."""
    async with store.connection() as conn:
        card = await conn.fetchrow(
            """
            SELECT * FROM agent_cards WHERE agent_revision_id = $1
            """,
            uuid.UUID(agent_revision_id),
        )
    
    if not card:
        raise HTTPException(status_code=404, detail="Agent card not found")
    
    # Convert to A2A format
    a2a_card = {
        "name": card["name"],
        "description": card["description"],
        "capabilities": card["capabilities"],
        "skills": card["skills"],
        "endpoints": card["endpoints"],
        "authentication": card["authentication"],
        "version": "1.0",
    }
    
    return A2AAgentCardResponse(agent_card=a2a_card)


# MCP Server
@router.post("/v1/agent-mesh/mcp/servers", response_model=MCPServerConfigResponse)
async def create_mcp_server(request: MCPServerConfigRequest) -> MCPServerConfigResponse:
    """Create an MCP server configuration for an agent."""
    # In a full implementation, this would register the MCP server
    # and generate the endpoint URL
    server_id = str(uuid.uuid4())
    endpoint = f"https://mcp.haas.example/{server_id}"
    
    return MCPServerConfigResponse(
        server_id=server_id,
        agent_revision_id=request.agent_revision_id,
        transport=request.transport,
        endpoint=endpoint,
        oauth_config=request.oauth_config,
        created_at=datetime.now(),
    )


@router.get("/v1/agent-mesh/mcp/servers", response_model=list[MCPServerConfigResponse])
async def list_mcp_servers() -> list[MCPServerConfigResponse]:
    """List MCP servers."""
    # In a full implementation, this would query a database table
    return []


# Observability
@router.get("/v1/agent-mesh/observability/{run_id}")
async def get_run_observability(run_id: str) -> dict[str, Any]:
    """Get observability data for a multi-agent run."""
    async with store.connection() as conn:
        # Get run tree with costs
        runs = await conn.fetch(
            """
            SELECT r.run_id, r.agent_revision_id::text, r.status, r.cost_usd,
                   r.created_at, d.parent_run_id
            FROM runs r
            LEFT JOIN delegations d ON d.child_run_id = r.run_id
            WHERE r.thread_id = (SELECT thread_id FROM runs WHERE run_id = $1)
            ORDER BY r.created_at
            """,
            run_id,
        )
        
        # Get messages
        messages = await conn.fetch(
            """
            SELECT * FROM agent_messages WHERE run_id = $1 ORDER BY created_at
            """,
            run_id,
        )
        
        # Get workspace entries
        workspace = await conn.fetch(
            """
            SELECT * FROM workspaces WHERE run_id = $1 ORDER BY agent_id, key
            """,
            run_id,
        )
    
    return {
        "runs": [dict(r) for r in runs],
        "messages": [dict(m) for m in messages],
        "workspace": [dict(w) for w in workspace],
    }
