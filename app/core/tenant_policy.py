from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.core.plugins import PluginRegistry

_HOST_LABEL_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)


class TenantPolicyIntegrityError(ValueError):
    """Stored policy data is malformed or fails its immutable digest check."""


class PluginGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skills: list[str] = Field(default_factory=list)

    @field_validator("skills")
    @classmethod
    def validate_skills(cls, skills: list[str]) -> list[str]:
        if any(not skill.strip() for skill in skills):
            raise ValueError("plugin grant skill names must not be empty")
        if len(skills) != len(set(skills)):
            raise ValueError("plugin grant skill names must be unique")
        return skills


class TenantPolicyRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_providers: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    default_primary_model: str | None = None
    default_planner_model: str | None = None
    plugin_grants: list[PluginGrant] = Field(default_factory=list)
    destination_hosts: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=15, ge=1, le=50)
    max_cost_usd: float = Field(default=0.30, gt=0, le=10)
    retention_class: str = Field(default="standard", min_length=1, max_length=64)
    # Track M: marketplace trust tiers this tenant may install; None = platform/verified/private.
    marketplace_tiers: list[str] | None = None

    @field_validator("marketplace_tiers")
    @classmethod
    def validate_marketplace_tiers(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        from app.marketplace.tiers import normalize_tier

        normalized = [normalize_tier(value) for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("marketplace tiers must be unique")
        return normalized

    def marketplace_tier_allowed(self, tier: str) -> bool:
        from app.marketplace.tiers import DEFAULT_ALLOWED_TIERS

        allowed = self.marketplace_tiers
        return tier in (DEFAULT_ALLOWED_TIERS if allowed is None else allowed)

    @field_validator("model_providers")
    @classmethod
    def validate_model_providers(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().lower() for value in values]
        if any(not value for value in normalized):
            raise ValueError("model provider names must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("model provider names must be unique")
        return normalized

    @field_validator("models")
    @classmethod
    def validate_models(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("model names must not be empty")
        if len(values) != len(set(values)):
            raise ValueError("model names must be unique")
        return values

    @field_validator("plugin_grants")
    @classmethod
    def validate_plugin_grants(cls, values: list[PluginGrant]) -> list[PluginGrant]:
        ids = [grant.plugin_id for grant in values]
        if len(ids) != len(set(ids)):
            raise ValueError("plugin grants must have unique plugin ids")
        return values

    @field_validator("destination_hosts")
    @classmethod
    def validate_destination_hosts(cls, values: list[str]) -> list[str]:
        normalized = [normalize_hostname(host) for host in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("destination hosts must be unique")
        return normalized

    @field_validator("default_primary_model", "default_planner_model")
    @classmethod
    def validate_default_models(cls, model: str | None) -> str | None:
        if model is not None and not model.strip():
            raise ValueError("default model names must not be empty")
        return model

    @model_validator(mode="after")
    def defaults_must_be_granted(self) -> TenantPolicyRules:
        for model in (self.default_primary_model, self.default_planner_model):
            if model is not None and model not in self.models:
                raise ValueError("default models must be present in the model grants")
        return self

    def model_is_allowed(self, model: str) -> bool:
        return "openrouter" in self.model_providers and model in self.models


@dataclass(frozen=True, slots=True)
class TenantPluginAccess:
    skill_names: frozenset[str]
    plugin_ids: frozenset[str]
    tool_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class TenantPolicy:
    policy_revision_id: str
    version: int
    digest: str
    rules: TenantPolicyRules

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> TenantPolicy:
        raw_rules = record.get("rules")
        if isinstance(raw_rules, str):
            try:
                raw_rules = json.loads(raw_rules)
            except json.JSONDecodeError as exc:
                raise TenantPolicyIntegrityError(
                    "Active tenant policy rules are not valid JSON."
                ) from exc
        if not isinstance(raw_rules, dict):
            raise TenantPolicyIntegrityError(
                "Active tenant policy rules must be a JSON object."
            )

        digest = record.get("digest")
        if not isinstance(digest, str) or not hmac.compare_digest(
            digest, policy_digest(raw_rules)
        ):
            raise TenantPolicyIntegrityError(
                "Active tenant policy digest does not match its rules."
            )
        try:
            rules = TenantPolicyRules.model_validate(raw_rules)
            revision_id = str(record["policy_revision_id"])
            version = int(record["version"])
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise TenantPolicyIntegrityError(
                "Active tenant policy revision is malformed."
            ) from exc
        if version < 1:
            raise TenantPolicyIntegrityError(
                "Active tenant policy version must be positive."
            )
        return cls(revision_id, version, digest, rules)

    def resolve_plugin_access(self, registry: PluginRegistry) -> TenantPluginAccess:
        allowed_skills: set[str] = set()
        allowed_plugins: set[str] = set()
        allowed_tools: set[str] = {
            name for name, tool in registry.tools.items() if tool.is_core
        }
        for grant in self.rules.plugin_grants:
            plugin = registry.plugins.get(grant.plugin_id)
            if (
                plugin is None
                or plugin.status != "loaded"
                or not hmac.compare_digest(plugin.digest, grant.digest)
            ):
                continue
            plugin_skills = {
                name
                for name, skill in registry.skills.items()
                if skill.plugin_id == grant.plugin_id
            }
            permitted = plugin_skills.intersection(grant.skills)
            if not permitted:
                continue
            allowed_plugins.add(grant.plugin_id)
            allowed_skills.update(permitted)
            for skill_name in permitted:
                allowed_tools.update(registry.skills[skill_name].required_tools)
        return TenantPluginAccess(
            frozenset(allowed_skills),
            frozenset(allowed_plugins),
            frozenset(allowed_tools),
        )

    def destination_is_allowed(self, url: str) -> bool:
        hostname = urlsplit(url).hostname
        if hostname is None:
            return False
        try:
            normalized = normalize_hostname(hostname)
        except ValueError:
            return False
        return normalized in self.rules.destination_hosts


def normalize_hostname(hostname: str) -> str:
    value = hostname.strip()
    if not value or any(character in value for character in "*/:@"):
        raise ValueError("destination host must be an exact hostname")
    try:
        normalized = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("destination host is not a valid hostname") from exc
    if not _HOST_LABEL_RE.fullmatch(normalized):
        raise ValueError("destination host is not a valid hostname")
    return normalized


def policy_digest(rules: TenantPolicyRules | dict[str, Any]) -> str:
    data = (
        rules.model_dump(mode="json", exclude_none=True)
        if isinstance(rules, TenantPolicyRules)
        else rules
    )
    canonical = json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
