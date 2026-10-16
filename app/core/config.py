import json
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Distributed HaaS Platform"
    postgres_dsn: str = "postgresql://haas:haas_dev_password@localhost:5432/haas_db"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    plugins_dir: Path = Path("./plugins")
    scratchpad_dir: Path = Path("/tmp/haas_scratchpad")

    # Plugin policy (comma-separated plugin ids). `deny` always wins; a non-empty `allow` is
    # exclusive. Extra roots rank below `plugins_dir` when the same plugin id appears twice.
    plugins_allow: str = ""
    plugins_deny: str = ""
    plugins_extra_dirs: str = ""
    plugins_strict: bool = False  # abort startup if any plugin fails to load

    # Tier-1 catalog budget in characters (0 = unlimited).
    skills_prompt_max_chars: int = 6000
    # JSON object {"<tenant>": ["skill", ...], "*": [...]}; a tenant without an entry (and no
    # "*") is unrestricted. Tenant identity is trusted from X-Tenant-ID.
    tenant_skill_allowlists: str = ""

    # OpenRouter settings (only required on worker-orchestrator)
    openrouter_api_key: str = "sk-or-v1-missing"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    default_primary_model: str = "deepseek/deepseek-chat-v3-0324"
    default_planner_model: str = "deepseek/deepseek-r1"

    # Guardrails & budgets
    default_max_steps: int = 15
    default_max_cost_usd: float = 0.30
    per_tool_timeout_sec: float = 25.0
    max_tool_output_chars: int = 4000
    max_identical_tool_calls: int = 2
    redis_stream_max_len: int = 1000

    # Comma-separated hostnames exempt from the SSRF private-address check
    # (local development / integration tests only).
    ssrf_allowed_hosts: str = ""

    @staticmethod
    def _csv(value: str) -> list[str]:
        return [p.strip() for p in value.split(",") if p.strip()]

    @property
    def plugins_allow_list(self) -> list[str] | None:
        return self._csv(self.plugins_allow) or None

    @property
    def plugins_deny_list(self) -> list[str]:
        return self._csv(self.plugins_deny)

    @property
    def plugins_extra_dir_list(self) -> list[Path]:
        return [Path(p) for p in self._csv(self.plugins_extra_dirs)]

    def tenant_allowlists(self) -> dict[str, list[str]]:
        if not self.tenant_skill_allowlists.strip():
            return {}
        data = json.loads(self.tenant_skill_allowlists)
        if not isinstance(data, dict) or not all(
            isinstance(v, list) and all(isinstance(i, str) for i in v)
            for v in data.values()
        ):
            raise ValueError(
                "TENANT_SKILL_ALLOWLISTS must be a JSON object of tenant -> [skill names]"
            )
        return data

    def tenant_skill_allowlist(self, tenant_id: str) -> list[str] | None:
        lists = self.tenant_allowlists()
        return lists.get(tenant_id, lists.get("*"))

    @property
    def ssrf_allowlist(self) -> set[str]:
        return {
            h.strip().lower() for h in self.ssrf_allowed_hosts.split(",") if h.strip()
        }


settings = Settings()
