"""Contract and discoverability checks for Atlas action-drafting skill."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "plugins" / "atlas-explorer"
SKILL = PACK / "skills" / "atlas-action-drafting" / "SKILL.md"
REFERENCES = SKILL.parent / "references"
SCHEMA = json.loads(
    (REFERENCES / "atlas-action-draft.v1.schema.json").read_text()
)
EXAMPLE = json.loads(
    (REFERENCES / "atlas-action-draft.v1.example.json").read_text()
)


def test_manifest_discovers_skill_without_granting_tools_or_secrets() -> None:
    manifest = json.loads((PACK / "plugin.json").read_text())

    assert manifest["id"] == "atlas-explorer"
    assert manifest["provides"]["skills"] == [
        "atlas-explorer",
        "atlas-action-drafting",
    ]
    assert manifest["provides"]["tools"] == [
        "atlas_cache_lookup",
        "atlas_cache_upsert",
        "atlas_emit_layer",
        "atlas_dispatch_inquiry",
    ]
    text = SKILL.read_text()
    frontmatter = text.split("---", 2)[1]
    assert "name: atlas-action-drafting" in frontmatter
    assert "required_tools:" not in frontmatter
    assert 'allowed_secrets: ""' in frontmatter
    assert "outbound" in frontmatter.lower()


def test_strict_versioned_schema_accepts_example() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    validator = Draft202012Validator(SCHEMA)

    assert list(validator.iter_errors(EXAMPLE)) == []


def test_schema_rejects_untrusted_execution_fields_and_unknown_properties() -> None:
    validator = Draft202012Validator(SCHEMA)
    invalid = json.loads(json.dumps(EXAMPLE))
    invalid["tool_name"] = "invented_tool"
    invalid["capability"]["resolution"] = "trusted"

    assert len(list(validator.iter_errors(invalid))) >= 2


def test_schema_rejects_invented_capability_id_and_version() -> None:
    validator = Draft202012Validator(SCHEMA)
    invalid_id = json.loads(json.dumps(EXAMPLE))
    invalid_id["capability"]["capability_id"] = "invented_capability"
    invalid_version = json.loads(json.dumps(EXAMPLE))
    invalid_version["capability"]["schema_version"] = "invented.v1"

    assert list(validator.iter_errors(invalid_id))
    assert list(validator.iter_errors(invalid_version))


def test_schema_requires_missing_fields_and_preserves_external_hold() -> None:
    validator = Draft202012Validator(SCHEMA)
    invalid = json.loads(json.dumps(EXAMPLE))
    del invalid["missing_fields"]
    invalid["external_execution"]["status"] = "approved"

    assert len(list(validator.iter_errors(invalid))) >= 2


def test_skill_forbids_private_inference_execution_and_tool_generation() -> None:
    text = SKILL.read_text().lower()

    assert "do not infer private medical information" in text
    assert "never execute" in text
    assert "never generate or claim a tool" in text
    assert "external execution remains blocked" in text
