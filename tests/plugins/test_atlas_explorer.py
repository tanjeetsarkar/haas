"""Unit tests for atlas-explorer plugin tools."""

from __future__ import annotations

import json
import pytest

from app.core.plugins import PluginRegistry
from app.core.context import RunContext


@pytest.fixture
def atlas_registry():
    """Load the atlas-explorer plugin registry."""
    reg = PluginRegistry()
    reg.discover_all(
        __import__("pathlib").Path("plugins"),
        include_disabled=True,
    )
    return reg


@pytest.fixture
def mock_ctx():
    """Create a mock RunContext for testing."""
    return RunContext(
        run_id="test_run_123",
        thread_id="test_thread_456",
        tenant_id="test_tenant",
        traceparent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        active_skills={"atlas-explorer", "web-research", "data-analyst"},
        allowed_secrets=set(),
    )


class TestAtlasCacheLookup:
    """Tests for atlas_cache_lookup tool."""

    async def test_cache_lookup_returns_not_found(self, atlas_registry, mock_ctx):
        """Cache lookup should return found=False when no cached entry exists."""
        tool = atlas_registry.tools["atlas_cache_lookup"]
        result = await tool.handler(mock_ctx, tool.args_schema(
            canonical_key="healthcare:mumbai:tata_memorial",
            dimension_key="root",
        ))
        data = json.loads(result)
        assert data["found"] is False
        assert data["attributes"] is None
        assert data["provenance"] is None


class TestAtlasCacheUpsert:
    """Tests for atlas_cache_upsert tool."""

    async def test_cache_upsert_computes_sha256_and_ttl(self, atlas_registry, mock_ctx):
        """Cache upsert should compute content_sha256 and expires_at based on TTL config."""
        tool = atlas_registry.tools["atlas_cache_upsert"]
        result = await tool.handler(mock_ctx, tool.args_schema(
            canonical_key="healthcare:mumbai:tata_memorial",
            dimension_key="root",
            domain_category="healthcare",
            attributes={"beds": 500, "specialty": "oncology"},
            provenance={
                "trust_tier": "verified_official",
                "source_urls": ["https://tmc.gov.in"],
                "observed_at": "2026-10-24T10:00:00+00:00",
                "citation_snippet": "Tata Memorial Hospital - 500 beds",
            },
            trust_tier="verified_official",
        ))
        data = json.loads(result)
        assert data["ok"] is True
        assert data["cached"] is True
        assert "content_sha256" in data
        assert len(data["content_sha256"]) == 64  # SHA256 hex
        assert "expires_at" in data
        # TTL for root + verified_official should be 30 days
        from datetime import datetime, timezone
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff_days = (expires - now).days
        assert 29 <= diff_days <= 30


class TestAtlasEmitLayer:
    """Tests for atlas_emit_layer tool."""

    def test_emit_layer_valid_envelope(self, atlas_registry, mock_ctx):
        """Valid atlas-layer.v1 envelope should pass validation and return digest."""
        tool = atlas_registry.tools["atlas_emit_layer"]
        valid_payload = {
            "schema_version": "atlas-layer.v1",
            "layer_depth": 1,
            "parent_node_id": None,
            "domain_category": "healthcare",
            "nodes": [{
                "node_id": "node_001",
                "canonical_key": "healthcare:mumbai:tata_memorial",
                "title": "Tata Memorial Hospital",
                "subtitle": "Premier cancer treatment center",
                "component_type": "entity_card",
                "attributes": {"beds": 500, "established": 1941},
                "provenance": {
                    "trust_tier": "verified_official",
                    "source_urls": ["https://tmc.gov.in"],
                    "observed_at": "2026-10-24T10:00:00+00:00",
                    "citation_snippet": "Official hospital website",
                }
            }],
            "available_drilldowns": [{
                "dimension_key": "facilities",
                "label": "Facilities & Equipment",
                "prompt_template": "List oncology facilities for {entity}",
                "recommended_skills": ["atlas-explorer", "web-research"]
            }],
            "available_actions": []
        }
        result = tool.handler(mock_ctx, tool.args_schema(payload=valid_payload))
        data = json.loads(result)
        assert data["ok"] is True
        assert data["layer_digest"] is not None
        assert data["layer_digest"].startswith("sha256:")
        assert data["envelope"] is not None
        assert data["envelope"]["schema_version"] == "atlas-layer.v1"

    def test_emit_layer_rejects_invalid_schema(self, atlas_registry, mock_ctx):
        """Invalid envelope (missing required fields) should fail validation."""
        tool = atlas_registry.tools["atlas_emit_layer"]
        invalid_payload = {
            "schema_version": "atlas-layer.v1",
            "layer_depth": 1,
            # Missing required fields: parent_node_id, domain_category, nodes, etc.
        }
        result = tool.handler(mock_ctx, tool.args_schema(payload=invalid_payload))
        data = json.loads(result)
        assert data["ok"] is False
        assert data["error"] == "schema_validation_failed"
        assert data["details"] is not None
        assert len(data["details"]) > 0

    def test_emit_layer_rejects_javascript_uri(self, atlas_registry, mock_ctx):
        """Envelope with javascript: URI in provenance should be rejected."""
        tool = atlas_registry.tools["atlas_emit_layer"]
        payload = {
            "schema_version": "atlas-layer.v1",
            "layer_depth": 1,
            "parent_node_id": None,
            "domain_category": "healthcare",
            "nodes": [{
                "node_id": "node_001",
                "canonical_key": "healthcare:mumbai:tata_memorial",
                "title": "Tata Memorial Hospital",
                "component_type": "entity_card",
                "attributes": {},
                "provenance": {
                    "trust_tier": "verified_official",
                    "source_urls": ["javascript:alert(1)"],
                    "observed_at": "2026-10-24T10:00:00+00:00",
                }
            }],
            "available_drilldowns": [],
            "available_actions": []
        }
        result = tool.handler(mock_ctx, tool.args_schema(payload=payload))
        data = json.loads(result)
        assert data["ok"] is False
        assert "Disallowed URI scheme" in data["error"]

    def test_emit_layer_rejects_data_uri(self, atlas_registry, mock_ctx):
        """Envelope with data: URI in provenance should be rejected."""
        tool = atlas_registry.tools["atlas_emit_layer"]
        payload = {
            "schema_version": "atlas-layer.v1",
            "layer_depth": 1,
            "parent_node_id": None,
            "domain_category": "healthcare",
            "nodes": [{
                "node_id": "node_001",
                "canonical_key": "healthcare:mumbai:tata_memorial",
                "title": "Tata Memorial Hospital",
                "component_type": "entity_card",
                "attributes": {},
                "provenance": {
                    "trust_tier": "verified_official",
                    "source_urls": ["data:text/html,<script>alert(1)</script>"],
                    "observed_at": "2026-10-24T10:00:00+00:00",
                }
            }],
            "available_drilldowns": [],
            "available_actions": []
        }
        result = tool.handler(mock_ctx, tool.args_schema(payload=payload))
        data = json.loads(result)
        assert data["ok"] is False
        assert "Disallowed URI scheme" in data["error"]

    def test_emit_layer_accepts_http_https(self, atlas_registry, mock_ctx):
        """Envelope with http/https URLs should be accepted."""
        tool = atlas_registry.tools["atlas_emit_layer"]
        for url in ["https://tmc.gov.in", "http://example.com/page"]:
            payload = {
                "schema_version": "atlas-layer.v1",
                "layer_depth": 1,
                "parent_node_id": None,
                "domain_category": "healthcare",
                "nodes": [{
                    "node_id": "node_001",
                    "canonical_key": "healthcare:mumbai:tata_memorial",
                    "title": "Tata Memorial Hospital",
                    "component_type": "entity_card",
                    "attributes": {},
                    "provenance": {
                        "trust_tier": "verified_official",
                        "source_urls": [url],
                        "observed_at": "2026-10-24T10:00:00+00:00",
                    }
                }],
                "available_drilldowns": [],
                "available_actions": []
            }
            result = tool.handler(mock_ctx, tool.args_schema(payload=payload))
            data = json.loads(result)
            assert data["ok"] is True, f"URL {url} should be accepted"


class TestAtlasDispatchInquiry:
    """Tests for atlas_dispatch_inquiry tool."""

    def test_dispatch_inquiry_requires_approval(self, atlas_registry):
        """atlas_dispatch_inquiry should have requires_approval=True."""
        tool = atlas_registry.tools["atlas_dispatch_inquiry"]
        assert tool.requires_approval is True

    def test_dispatch_inquiry_not_sandbox(self, atlas_registry):
        """atlas_dispatch_inquiry should not be sandboxed (runs on q_tools_fast)."""
        tool = atlas_registry.tools["atlas_dispatch_inquiry"]
        assert tool.queue == "q_tools_fast"


class TestCanonicalizeEntityKey:
    """Tests for canonicalize_entity_key function."""

    def test_canonicalize_basic(self):
        import sys
        sys.path.insert(0, "/home/voldemort/work/harness/plugins/atlas-explorer/tools")
        from atlas_tools import canonicalize_entity_key
        key = canonicalize_entity_key("healthcare", "mumbai", "tata memorial hospital")
        assert key == "healthcare:mumbai:tata_memorial_hospital"

    def test_canonicalize_special_chars(self):
        import sys
        sys.path.insert(0, "/home/voldemort/work/harness/plugins/atlas-explorer/tools")
        from atlas_tools import canonicalize_entity_key
        key = canonicalize_entity_key("healthcare", "new york", "memorial sloan-kettering")
        # Hyphens are preserved in canonical keys (allowed chars: alnum, _, :, -)
        assert key == "healthcare:new_york:memorial_sloan-kettering"

    def test_canonicalize_truncation(self):
        import sys
        sys.path.insert(0, "/home/voldemort/work/harness/plugins/atlas-explorer/tools")
        from atlas_tools import canonicalize_entity_key
        long_slug = "a" * 200
        key = canonicalize_entity_key("healthcare", "mumbai", long_slug)
        assert len(key) <= 128


class TestComputeTTL:
    """Tests for compute_ttl function."""

    def test_ttl_root_verified(self):
        import sys
        sys.path.insert(0, "/home/voldemort/work/harness/plugins/atlas-explorer/tools")
        from atlas_tools import compute_ttl
        ttl = compute_ttl("root", "verified_official")
        assert ttl == 30 * 86400

    def test_ttl_pricing_estimated(self):
        import sys
        sys.path.insert(0, "/home/voldemort/work/harness/plugins/atlas-explorer/tools")
        from atlas_tools import compute_ttl
        ttl = compute_ttl("pricing", "estimated_range")
        assert ttl == 86400  # 24 hours

    def test_ttl_credentials_unverified(self):
        import sys
        sys.path.insert(0, "/home/voldemort/work/harness/plugins/atlas-explorer/tools")
        from atlas_tools import compute_ttl
        ttl = compute_ttl("credentials", "unverified")
        assert ttl == 12 * 3600  # 12 hours

    def test_ttl_unknown_dimension_defaults(self):
        import sys
        sys.path.insert(0, "/home/voldemort/work/harness/plugins/atlas-explorer/tools")
        from atlas_tools import compute_ttl
        ttl = compute_ttl("unknown_dimension", "verified_official")
        assert ttl == 86400  # default 24 hours


class TestComputeContentSHA256:
    """Tests for compute_content_sha256 function."""

    def test_deterministic_hash(self):
        import sys
        sys.path.insert(0, "/home/voldemort/work/harness/plugins/atlas-explorer/tools")
        from atlas_tools import compute_content_sha256, ProvenanceModel
        prov = ProvenanceModel(
            trust_tier="verified_official",
            source_urls=["https://example.com"],
            observed_at="2026-10-24T10:00:00+00:00",
        )
        attrs = {"beds": 500, "name": "Test Hospital"}
        hash1 = compute_content_sha256(attrs, prov)
        hash2 = compute_content_sha256(attrs, prov)
        assert hash1 == hash2
        assert len(hash1) == 64

    def test_different_attributes_different_hash(self):
        import sys
        sys.path.insert(0, "/home/voldemort/work/harness/plugins/atlas-explorer/tools")
        from atlas_tools import compute_content_sha256, ProvenanceModel
        prov = ProvenanceModel(
            trust_tier="verified_official",
            source_urls=["https://example.com"],
            observed_at="2026-10-24T10:00:00+00:00",
        )
        hash1 = compute_content_sha256({"beds": 500}, prov)
        hash2 = compute_content_sha256({"beds": 600}, prov)
        assert hash1 != hash2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])