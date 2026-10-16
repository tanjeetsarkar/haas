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

    @property
    def ssrf_allowlist(self) -> set[str]:
        return {
            h.strip().lower() for h in self.ssrf_allowed_hosts.split(",") if h.strip()
        }


settings = Settings()
