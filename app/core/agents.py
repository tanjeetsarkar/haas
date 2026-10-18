from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.plugins import PluginRegistry
from app.core.tenant_policy import TenantPolicy, policy_digest


class AgentConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    primary_model: str = Field(min_length=1, max_length=256)
    planner_model: str = Field(min_length=1, max_length=256)
    skills: list[str] = Field(default_factory=list, max_length=100)
    preload_skills: list[str] = Field(default_factory=list, max_length=100)
    max_steps: int = Field(default=15, ge=1, le=50)
    max_cost_usd: float = Field(default=0.30, gt=0, le=10, allow_inf_nan=False)

    def validate_policy(self, policy: TenantPolicy, registry: PluginRegistry) -> None:
        if not all(
            policy.rules.model_is_allowed(model)
            for model in (self.primary_model, self.planner_model)
        ):
            raise ValueError("Agent models exceed the active tenant policy.")
        if (
            self.max_steps > policy.rules.max_steps
            or self.max_cost_usd > policy.rules.max_cost_usd
        ):
            raise ValueError("Agent budgets exceed the active tenant policy.")
        access = policy.resolve_plugin_access(registry)
        if (
            len(self.skills) != len(set(self.skills))
            or not set(self.skills).issubset(access.skill_names)
            or not set(self.preload_skills).issubset(self.skills)
        ):
            raise ValueError(
                "Agent skills are unavailable or exceed the active grants."
            )

    def digest(self) -> str:
        return policy_digest(self.model_dump(mode="json"))
