# HaaS Atlas: Lean Layered Intelligence & Task Engine — Master Specification (v1.0)

## 1. Executive Purpose & Lean Architectural Philosophy

**HaaS Atlas** is a progressive disclosure and task-execution application built natively on top of **HaaS Distributed** (`FastAPI` + `Celery Canvas` + `Redis` + `PostgreSQL 16`). Instead of returning a linear wall of chat text or an overwhelming 2D node graph, HaaS Atlas decomposes any user query (e.g., *"Cancer hospitals in Mumbai"*) into **hierarchical, verifiable Entity Layers** rendered as interactive Miller Columns / Drill-Down Stacks, terminating in **Human-in-the-Loop (HITL) Action Layers**.

### 1.1 Lean Engineering Mandates (Strict Alignment with `tanjeetsarkar/haas`)
1. **Zero New Infrastructure Daemons**: HaaS Atlas does **not** introduce Neo4j, Elasticsearch, Kafka, or a separate BFF microservice. It runs entirely on the existing HaaS stack (`haas-api`, `worker-orchestrator`, `worker-tools`, `PostgreSQL 16`, and `Redis`).
2. **Everything is a HaaS Run, Thread Branch, or Plugin Pack**:
   * **Root Search (Layer 1)** executes as a bounded HaaS Run (`POST /v1/runs`) pinned to a published agent revision (`agent/atlas-explorer`).
   * **Layer Drill-Downs (Layers 2–$N$)** map to **versioned Thread Branches** (`/v1/threads/{thread_id}`) and parallel tool chords (`aggregate_step_task`) or attenuated HACP child runs.
   * **Domain Capabilities** are packaged strictly as HaaS plugin packs (`plugins/atlas-explorer/plugin.json`) adhering to the Agent Skills specification (`SKILL.md` with `metadata` tool/secret declarations).
3. **Server-Driven UI (SDUI) via Existing Event Streams**: The frontend (`haas_sdk_ts`) consumes HaaS's existing `/v1/runs/{run_id}/stream` (`run-events.v1` and `output-deltas.v1`) to progressively render typed JSON UI cards (`atlas-layer.v1.json`) rather than raw Markdown strings.
4. **Provenance-First Consumer Safety**: Every attribute rendered to a user carries a machine-verifiable provenance badge (`verified_source`, `extracted_tariff_pdf`, `crowdsourced_estimate`, or `unverified`) with source URLs and extraction timestamps.

---

## 2. End-to-End Walkthrough: "Cancer Hospitals in Mumbai"

| Layer | User Interaction | HaaS Execution Pattern | Active Plugin Packs & Tools | SDUI Output Component |
| :--- | :--- | :--- | :--- | :--- |
| **Layer 1: Root Entities** | User searches: *"Cancer hospitals in Mumbai"* | `POST /v1/runs` on new Thread `thd_01`. Planner model decomposes query into root entity type `hospital` + suggested drill-down dimensions. Checks PostgreSQL `atlas_entity_cache` first; misses trigger parallel `web_search`. | `atlas-explorer` (`cache_lookup`, `emit_layer_node`), `web-research` (`web_search`) | `EntityGrid` (Tata Memorial, Kokilaben, Lilavati, HCG, Nanavati) + Next-Layer Dimension Chips |
| **Layer 2: Facilities** | User selects *Tata Memorial* & *Kokilaben* $\rightarrow$ clicks **[Compare Facilities]** | Creates Thread Branch from `thd_01` version $v_1$. Spawns bounded run with parallel Celery chord (`aggregate_step_task`) across `worker-tools` to fetch oncology department pages. | `web-research` (`web_search`, `web_fetch_page`), `atlas-explorer` (`extract_structured_attributes`) | `ComparisonMatrix` (LINAC, CyberKnife, PET-CT, Bone Marrow Transplant, Daycare Chemo) |
| **Layer 3: Facility Charges** | User clicks **[Pricing & Schemes]** on *Radiation Therapy / CyberKnife* | Branches thread at $v_2$. Queries cached tariff datasets via `data-analyst` (`dataset_query`) and scrapes official hospital PDF rate cards / PM-JAY scheme tables. | `data-analyst` (`dataset_query`), `web-research` (`web_fetch_page`), `atlas-explorer` (`verify_provenance`) | `PricingBreakdownCard` (General Ward, Private Ward, Charitable Trust / MJPJAY coverage + Confidence Badge) |
| **Layer 4: Specialists** | User clicks **[Show Oncologists]** for *Tata Memorial Radiation Oncology* | Branches thread at $v_3$. Executes directory lookup and hospital roster extraction. | `web-research` (`web_search`, `web_fetch_page`), optional `mcp_http` healthcare directory endpoint | `PersonRosterList` (Filtered by Radiation & Surgical Oncology) |
| **Layer 5: Credentials** | User clicks *Dr. [Name]* $\rightarrow$ **[Experience & Recognitions]** | Branches thread at $v_4$. Fetches pubmed/fellowship history, years of practice, and medical council registration metadata. | `web-research` (`web_search`, `web_fetch_page`), `atlas-explorer` (`emit_layer_node`) | `CredentialProfileCard` (Degrees, Fellowships, Publications, OPD Timings) |
| **Layer 6: Task Action (HITL)** | User clicks **[Prepare OPD Inquiry / Checklist]** | Invokes approval-gated tool `dispatch_external_inquiry`. Run transitions to `status='input_required'` and emits `hitl_pending` with exact batch digest. | `atlas-explorer` (`draft_patient_checklist`, `dispatch_external_inquiry` `[approval=true]`) | `HITLApprovalDrawer` showing exact payload diff and destination before execution |

---

## 3. Server-Driven UI (SDUI) Contract (`docs/contracts/atlas-layer.v1.json`)

To keep the frontend completely domain-agnostic (capable of rendering hospitals, universities, legal procedures, or laptops without frontend code changes), the HaaS primary model emits structured JSON conforming to `atlas-layer.v1`.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://haas.local/contracts/atlas-layer.v1.json",
  "title": "AtlasLayerEnvelopeV1",
  "type": "object",
  "required": [
    "schema_version",
    "layer_depth",
    "parent_node_id",
    "domain_category",
    "nodes",
    "available_drilldowns",
    "available_actions"
  ],
  "properties": {
    "schema_version": {
      "type": "string",
      "const": "atlas-layer.v1"
    },
    "layer_depth": {
      "type": "integer",
      "minimum": 1,
      "maximum": 10
    },
    "parent_node_id": {
      "type": ["string", "null"]
    },
    "domain_category": {
      "type": "string",
      "enum": ["healthcare", "education", "government", "commerce", "general"]
    },
    "nodes": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["node_id", "canonical_key", "title", "component_type", "attributes", "provenance"],
        "properties": {
          "node_id": { "type": "string" },
          "canonical_key": { "type": "string", "pattern": "^[a-z0-9_:-]{3,128}$" },
          "title": { "type": "string", "maxLength": 160 },
          "subtitle": { "type": ["string", "null"], "maxLength": 240 },
          "component_type": {
            "type": "string",
            "enum": [
              "entity_card",
              "comparison_row",
              "pricing_tier_card",
              "specialist_profile",
              "checklist_card"
            ]
          },
          "attributes": {
            "type": "object",
            "additionalProperties": {
              "type": ["string", "number", "boolean", "null"]
            }
          },
          "provenance": {
            "type": "object",
            "required": ["trust_tier", "source_urls", "observed_at"],
            "properties": {
              "trust_tier": {
                "type": "string",
                "enum": [
                  "verified_official",
                  "extracted_document",
                  "aggregated_directory",
                  "estimated_range",
                  "unverified"
                ]
              },
              "source_urls": {
                "type": "array",
                "items": { "type": "string", "format": "uri" }
              },
              "observed_at": { "type": "string", "format": "date-time" },
              "citation_snippet": { "type": ["string", "null"], "maxLength": 500 }
            }
          }
        }
      }
    },
    "available_drilldowns": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["dimension_key", "label", "prompt_template", "recommended_skills"],
        "properties": {
          "dimension_key": { "type": "string" },
          "label": { "type": "string", "maxLength": 64 },
          "prompt_template": { "type": "string", "maxLength": 500 },
          "recommended_skills": {
            "type": "array",
            "items": { "type": "string" }
          }
        }
      }
    },
    "available_actions": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["action_id", "label", "tool_name", "requires_approval"],
        "properties": {
          "action_id": { "type": "string" },
          "label": { "type": "string", "maxLength": 64 },
          "tool_name": { "type": "string" },
          "requires_approval": { "type": "boolean" }
        }
      }
    }
  }
}
```

---

## 4. Step-by-Step Implementation Specification

### Step 1: Scaffold the `atlas-explorer` Plugin Pack
Using the existing HaaS plugin CLI (`haas-plugin`), scaffold a dedicated plugin pack that works alongside `web-research` and `data-analyst`:

```bash
haas-plugin new plugin atlas-explorer \
  --tools atlas_cache_lookup,atlas_cache_upsert,atlas_emit_layer,atlas_dispatch_inquiry \
  --secrets ATLAS_WEBHOOK_SECRET \
  --category intelligence
```

#### 4.1.1 Static Manifest (`plugins/atlas-explorer/plugin.json`)
Matches HaaS's static manifest validator (`haas-plugin doctor`) with zero runtime code execution in the control plane:

```json
{
  "id": "atlas-explorer",
  "version": "1.0.0",
  "name": "HaaS Atlas Layered Intelligence Pack",
  "description": "Structured entity graph caching, provenance verification, and SDUI layer emission.",
  "category": "intelligence",
  "skills": [
    "skills/atlas-explorer/SKILL.md"
  ],
  "tools": [
    "tools/atlas_tools.py"
  ],
  "endpoints": [],
  "policy": {
    "requires_network": true,
    "default_timeout_seconds": 25,
    "isolation_tier": "standard"
  }
}
```

#### 4.1.2 Skill Definition (`plugins/atlas-explorer/skills/atlas-explorer/SKILL.md`)
Strictly adheres to the Agent Skills specification enforced in HaaS (`name` matches directory; custom keys placed under `metadata` as space-separated lists, plus `allowed-tools`):

```markdown
---
name: atlas-explorer
description: Decomposes user queries into progressive entity layers, checks the shared entity cache, verifies source provenance, and emits atlas-layer.v1 JSON payloads.
license: Apache-2.0
compatibility: haas>=3.0
allowed-tools: atlas_cache_lookup atlas_cache_upsert atlas_emit_layer atlas_dispatch_inquiry web_search web_fetch_page dataset_query
metadata:
  version: "1.0.0"
  required_tools: "atlas_cache_lookup atlas_cache_upsert atlas_emit_layer atlas_dispatch_inquiry"
  allowed_secrets: "ATLAS_WEBHOOK_SECRET"
---

# Instructions for `atlas-explorer`

You are the **HaaS Atlas Layered Intelligence Engine**. For every user request:
1. **Cache Lookup First**: Before invoking `web_search` or `web_fetch_page`, call `atlas_cache_lookup` with the normalized `canonical_key` and requested `dimension_key` (e.g., `root`, `facilities`, `pricing`, `specialists`, `credentials`).
2. **Scoped Parallel Discovery**: On a cache miss, invoke `web_search` and `web_fetch_page` (or `dataset_query` for tabular tariffs) only for the current layer depth $L_k$. Never fetch Layer $L_{k+1}$ unless explicitly instructed.
3. **Provenance Enforcement**:
   - Never fabricate prices, medical credentials, or government fees.
   - If a hospital or service does not publish exact tariffs, set `trust_tier` to `"estimated_range"` or `"unverified"` and include the exact source URL and snippet.
4. **Structured Layer Emission**: Call `atlas_emit_layer` to validate your output against `atlas-layer.v1` and persist fresh entity attributes via `atlas_cache_upsert`.
5. **External Actions Require Approval**: Only call `atlas_dispatch_inquiry` when the user explicitly requests an external task action.
```

#### 4.1.3 Tool Implementation (`plugins/atlas-explorer/tools/atlas_tools.py`)
In HaaS, tool modules are registered in `sys.modules` before execution so Pydantic `Literal` and `from __future__ import annotations` resolve cleanly, and sync functions run via `asyncio.to_thread` under `asyncio.wait_for`.

```python
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, HttpUrl, ValidationError

TrustTier = Literal[
    "verified_official",
    "extracted_document",
    "aggregated_directory",
    "estimated_range",
    "unverified",
]

ComponentType = Literal[
    "entity_card",
    "comparison_row",
    "pricing_tier_card",
    "specialist_profile",
    "checklist_card",
]


class ProvenanceModel(BaseModel):
    trust_tier: TrustTier
    source_urls: list[HttpUrl] = Field(min_length=1, max_length=10)
    observed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    citation_snippet: str | None = Field(default=None, max_length=500)


class LayerNodeModel(BaseModel):
    node_id: str = Field(min_length=3, max_length=64)
    canonical_key: str = Field(pattern=r"^[a-z0-9_:-]{3,128}$")
    title: str = Field(min_length=1, max_length=160)
    subtitle: str | None = Field(default=None, max_length=240)
    component_type: ComponentType
    attributes: dict[str, str | int | float | bool | None]
    provenance: ProvenanceModel


class DrilldownChipModel(BaseModel):
    dimension_key: str = Field(pattern=r"^[a-z0-9_]{2,48}$")
    label: str = Field(min_length=2, max_length=64)
    prompt_template: str = Field(min_length=5, max_length=500)
    recommended_skills: list[str] = Field(default_factory=lambda: ["atlas-explorer", "web-research"])


class ActionChipModel(BaseModel):
    action_id: str = Field(pattern=r"^[a-z0-9_]{2,48}$")
    label: str = Field(min_length=2, max_length=64)
    tool_name: str
    requires_approval: bool = True


class AtlasLayerEnvelope(BaseModel):
    schema_version: Literal["atlas-layer.v1"] = "atlas-layer.v1"
    layer_depth: int = Field(ge=1, le=10)
    parent_node_id: str | None = None
    domain_category: Literal["healthcare", "education", "government", "commerce", "general"]
    nodes: list[LayerNodeModel] = Field(min_length=1, max_length=25)
    available_drilldowns: list[DrilldownChipModel] = Field(max_length=8)
    available_actions: list[ActionChipModel] = Field(max_length=6)


def canonicalize_entity_key(domain: str, location: str, slug: str) -> str:
    """Deterministic key generator for PostgreSQL atlas_entity_cache."""
    clean = f"{domain.strip().lower()}:{location.strip().lower()}:{slug.strip().lower()}"
    return "".join(c if c.isalnum() or c in "_:-" else "_" for c in clean)[:128]


def atlas_emit_layer(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Validates and normalizes a layer response against the atlas-layer.v1 contract.
    Rejects any node lacking valid HTTPS/HTTP provenance URLs.
    """
    try:
        envelope = AtlasLayerEnvelope.model_validate(payload)
    except ValidationError as exc:
        return {
            "ok": False,
            "error": "schema_validation_failed",
            "details": exc.errors(),
        }

    # Verify every URL uses http/https scheme to prevent javascript:/data: XSS in SDUI
    for node in envelope.nodes:
        for url in node.provenance.source_urls:
            parsed = urlparse(str(url))
            if parsed.scheme not in ("http", "https"):
                return {
                    "ok": False,
                    "error": f"Disallowed URI scheme in provenance for {node.node_id}",
                }

    serialized = envelope.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(serialized, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "ok": True,
        "layer_digest": f"sha256:{digest}",
        "envelope": serialized,
    }
```

---

### Step 2: Frontend Integration via `haas_sdk_ts`

The consumer web application uses `haas_sdk_ts` to submit scoped runs, branch threads when the user clicks a drill-down chip, and stream SSE events with `Last-Event-ID` cursor preservation.

```typescript
// src/lib/atlasClient.ts
import { HaasClient, RunEvent } from "@haas/sdk-ts";

export interface DrilldownRequest {
  threadId: string;
  parentBranchVersion: number;
  parentNodeId: string;
  dimensionKey: string;
  prompt: string;
}

export class AtlasSessionManager {
  constructor(private readonly client: HaasClient) {}

  /**
   * Starts Layer 1 exploration on a fresh HaaS Thread using the published
   * `agent/atlas-explorer` revision.
   */
  async startRootExploration(query: string, onEvent: (ev: RunEvent) => void) {
    const thread = await this.client.threads.create({
      metadata: { app: "haas-atlas", root_query: query },
    });

    const run = await this.client.runs.create({
      thread_id: thread.thread_id,
      prompt: `Layer 1 Root Query: ${query}. Emit atlas-layer.v1 JSON.`,
      skills: ["atlas-explorer", "web-research", "data-analyst"],
      budget: {
        max_steps: 6,
        max_cost_usd: 0.15,
        max_tool_calls: 12,
      },
    });

    const unsubscribe = this.client.runs.stream(run.run_id, {
      onEvent,
    });

    return { threadId: thread.thread_id, runId: run.run_id, unsubscribe };
  }

  /**
   * Executes a Layer K -> Layer K+1 drill-down by branching the HaaS Thread
   * at the exact committed version of Layer K so sibling explorations never
   * pollute the context window.
   */
  async drillDownLayer(req: DrilldownRequest, onEvent: (ev: RunEvent) => void) {
    // 1. Branch the thread at parentBranchVersion using HaaS's native /v1/threads API
    const branchedThread = await this.client.threads.branch(req.threadId, {
      version: req.parentBranchVersion,
    });

    // 2. Submit a tightly budgeted run on the branched thread
    const run = await this.client.runs.create({
      thread_id: branchedThread.thread_id,
      prompt: `[parent_node_id=${req.parentNodeId}] [dimension=${req.dimensionKey}] ${req.prompt}`,
      skills: ["atlas-explorer", "web-research", "data-analyst"],
      budget: {
        max_steps: 5,
        max_cost_usd: 0.10,
        max_tool_calls: 8,
      },
    });

    const unsubscribe = this.client.runs.stream(run.run_id, {
      onEvent,
    });

    return {
      branchedThreadId: branchedThread.thread_id,
      runId: run.run_id,
      unsubscribe,
    };
  }
}
```

---

## 5. Phased Lean Delivery Plan

### Phase 1: Core Plugin & SDUI Contract (Weeks 1–2)
* Create `plugins/atlas-explorer/` pack (`plugin.json`, `SKILL.md`, `tools/atlas_tools.py`).
* Validate with `haas-plugin doctor` and unit tests under `tests/plugins/test_atlas_explorer.py`.
* Publish immutable agent revision `agent/atlas-explorer` via `haas chat` (`/agent draft` $\rightarrow$ `/agent validate` $\rightarrow$ `/agent publish`) and grant `atlas-explorer`, `web-research`, and `data-analyst` in the tenant policy revision.

### Phase 2: Shared Entity Cache & Provenance Store (Week 3)
* Add Alembic migration creating `atlas_entity_cache` and `atlas_layer_nodes` in PostgreSQL 16 with `FORCE ROW LEVEL SECURITY` and tenant-composite foreign keys.
* Wire `atlas_cache_lookup` and `atlas_cache_upsert` to prevent redundant scraping across users within the same tenant.

### Phase 3: Miller-Column Web Workbench & Dual-Principal HITL Proxy (Weeks 4–5)
* Build the React Miller-Column explorer using `haas_sdk_ts`.
* Implement the HITL Approval Drawer connected to `/v1/runs/{run_id}/stream` (`hitl_pending`) and `/v1/runs/{run_id}/resume` using HaaS's separated `submitter` and `approver` principals.