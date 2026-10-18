import base64
import binascii
import json
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    haas_profile: Literal["dev", "prod"] = "dev"
    haas_worker_role: Literal["orchestrator", "tools"] = "tools"
    app_name: str = "Distributed HaaS Platform"
    postgres_dsn: str = "postgresql://haas:haas_dev_password@localhost:5432/haas_db"
    migration_postgres_dsn: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    plugins_dir: Path = Path("./plugins")
    scratchpad_dir: Path = Path("/tmp/haas_scratchpad")
    secret_encryption_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HAAS_SECRET_ENCRYPTION_KEY", "secret_encryption_key"
        ),
    )
    max_request_body_bytes: int = Field(default=1_048_576, gt=0)
    max_prompt_chars: int = Field(default=32_000, gt=0)
    orphan_run_timeout_sec: int = Field(default=900, gt=0)
    scratchpad_ttl_sec: int = Field(default=86_400, gt=0)
    maintenance_interval_sec: int = Field(default=60, gt=0)
    flower_enabled: bool = False
    flower_basic_auth: str = ""
    otel_exporter_otlp_endpoint: str | None = None
    oidc_issuer: str | None = Field(
        default=None,
        validation_alias=AliasChoices("HAAS_OIDC_ISSUER", "oidc_issuer"),
    )
    oidc_audience: str | None = Field(
        default=None,
        validation_alias=AliasChoices("HAAS_OIDC_AUDIENCE", "oidc_audience"),
    )
    oidc_jwks_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("HAAS_OIDC_JWKS_URL", "oidc_jwks_url"),
    )
    bootstrap_tenant_slug: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HAAS_BOOTSTRAP_TENANT_SLUG", "bootstrap_tenant_slug"
        ),
    )
    bootstrap_admin_issuer: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HAAS_BOOTSTRAP_ADMIN_ISSUER", "bootstrap_admin_issuer"
        ),
    )
    bootstrap_admin_subject: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HAAS_BOOTSTRAP_ADMIN_SUBJECT", "bootstrap_admin_subject"
        ),
    )

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

    # Durable workflow (Phase 3). Times are seconds.
    lease_ttl_sec: int = Field(default=300, gt=0)
    approval_ttl_sec: int = Field(default=86_400, gt=0)
    tool_outcome_timeout_sec: int = Field(default=600, gt=0)
    stall_timeout_sec: int = Field(default=120, gt=0)
    default_run_deadline_sec: int = Field(default=3_600, gt=0)
    default_max_tool_invocations: int = Field(default=100, gt=0)
    max_recovery_attempts: int = Field(default=3, ge=0)
    outbox_max_attempts: int = Field(default=8, gt=0)
    idempotency_ttl_sec: int = Field(default=86_400, gt=0)
    inbox_retention_sec: int = Field(default=604_800, gt=0)
    reconcile_interval_sec: int = Field(default=10, gt=0)
    model_call_attempts: int = Field(default=3, gt=0)
    # Reservation charged before a model call when no model pricing is configured.
    default_step_reservation_usd: float = Field(default=0.001, ge=0.0)
    # JSON {"<model>": {"input_per_mtok": 0.5, "output_per_mtok": 1.5}}.
    model_pricing_json: str = ""
    reservation_output_tokens: int = Field(default=2_048, gt=0)
    pricing_revision: str = "static-v1"
    sse_heartbeat_sec: float = Field(default=15.0, gt=0)
    sse_reauth_sec: float = Field(default=60.0, gt=0)
    # Optional live `run.output.delta` previews (Redis, best effort, never authoritative).
    output_delta_enabled: bool = False
    output_delta_flush_chars: int = Field(default=48, ge=1, le=4_096)
    output_delta_flush_ms: int = Field(default=60, ge=0, le=5_000)
    output_delta_max_chars_per_step: int = Field(default=64_000, ge=1, le=1_000_000)
    output_delta_buffer: int = Field(default=2_000, ge=10, le=100_000)
    # OpenAI-compatible facade: how long a blocking call waits for a run to settle.
    openai_facade_wait_sec: float = Field(default=120.0, gt=0, le=900)
    envelope_signing_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HAAS_ENVELOPE_SIGNING_KEY", "envelope_signing_key"
        ),
    )
    envelope_verify_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HAAS_ENVELOPE_VERIFY_KEY", "envelope_verify_key"
        ),
    )

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

    def model_pricing(self) -> dict[str, dict[str, float]]:
        if not self.model_pricing_json.strip():
            return {}
        data = json.loads(self.model_pricing_json)
        if not isinstance(data, dict) or not all(
            isinstance(v, dict)
            and all(
                k in {"input_per_mtok", "output_per_mtok"}
                and isinstance(n, (int, float))
                and n >= 0
                for k, n in v.items()
            )
            for v in data.values()
        ):
            raise ValueError(
                "MODEL_PRICING_JSON must map models to non-negative "
                "input_per_mtok/output_per_mtok prices"
            )
        return data

    @property
    def ssrf_allowlist(self) -> set[str]:
        return {
            h.strip().lower() for h in self.ssrf_allowed_hosts.split(",") if h.strip()
        }


settings = Settings()


def validate_profile_settings(
    config: Settings,
    *,
    role: Literal["api", "orchestrator", "tools"] = "api",
) -> None:
    if config.haas_profile == "dev":
        return

    errors: list[str] = []
    oidc_values = (config.oidc_issuer, config.oidc_audience, config.oidc_jwks_url)
    configured_oidc_values = tuple(
        value for value in oidc_values if value and value.strip()
    )
    if configured_oidc_values and len(configured_oidc_values) != len(oidc_values):
        errors.append(
            "HAAS_OIDC_ISSUER, HAAS_OIDC_AUDIENCE and HAAS_OIDC_JWKS_URL must be configured together"
        )
    if role == "api" and config.haas_profile == "prod" and not configured_oidc_values:
        errors.append(
            "HAAS_OIDC_ISSUER, HAAS_OIDC_AUDIENCE and HAAS_OIDC_JWKS_URL "
            "are required for production API identity."
        )
    if config.haas_profile == "prod" and config.oidc_issuer is not None:
        if not config.oidc_issuer.startswith("https://"):
            errors.append("HAAS_OIDC_ISSUER must use HTTPS in production")
    if config.haas_profile == "prod" and config.oidc_jwks_url is not None:
        if not config.oidc_jwks_url.startswith("https://"):
            errors.append("HAAS_OIDC_JWKS_URL must use HTTPS in production")

    bootstrap_values = (
        config.bootstrap_tenant_slug,
        config.bootstrap_admin_issuer,
        config.bootstrap_admin_subject,
    )
    if any(bootstrap_values) and not all(bootstrap_values):
        errors.append(
            "HAAS_BOOTSTRAP_TENANT_SLUG, HAAS_BOOTSTRAP_ADMIN_ISSUER and "
            "HAAS_BOOTSTRAP_ADMIN_SUBJECT must be configured together"
        )

    encryption_key = config.secret_encryption_key
    if role != "tools" and not encryption_key:
        errors.append("HAAS_SECRET_ENCRYPTION_KEY is required in production")
    elif encryption_key == "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=":
        errors.append("HAAS_SECRET_ENCRYPTION_KEY must not use the development key")
    elif role != "tools" and encryption_key is not None:
        try:
            key = base64.b64decode(encryption_key, validate=True)
        except (ValueError, binascii.Error):
            errors.append("HAAS_SECRET_ENCRYPTION_KEY must be base64-encoded AES-256")
        else:
            if len(key) != 32:
                errors.append(
                    "HAAS_SECRET_ENCRYPTION_KEY must be base64-encoded AES-256"
                )
    if role == "orchestrator" and config.openrouter_api_key in (
        "",
        "sk-or-v1-missing",
        "sk-or-v1-replace-me",
    ):
        errors.append("OPENROUTER_API_KEY must be configured in production")
    from app.core.envelope import DEV_SIGNING_SEED_B64, dev_verify_key_b64

    if role == "orchestrator":
        if not config.envelope_signing_key:
            errors.append("HAAS_ENVELOPE_SIGNING_KEY is required in production")
        elif config.envelope_signing_key == DEV_SIGNING_SEED_B64:
            errors.append("HAAS_ENVELOPE_SIGNING_KEY must not use the development key")
    if role == "tools":
        if not config.envelope_verify_key:
            errors.append("HAAS_ENVELOPE_VERIFY_KEY is required in production")
        elif config.envelope_verify_key == dev_verify_key_b64():
            errors.append("HAAS_ENVELOPE_VERIFY_KEY must not use the development key")
    try:
        config.model_pricing()
    except ValueError as exc:
        errors.append(str(exc))
    if config.flower_enabled and (
        ":" not in config.flower_basic_auth
        or config.flower_basic_auth == "admin:change-me"
    ):
        errors.append(
            "Flower must be disabled or protected with a non-placeholder FLOWER_BASIC_AUTH"
        )

    if errors:
        raise ValueError("Invalid production profile: " + "; ".join(errors))
