from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

SECRET_ENV_PREFIX = "HAAS_SECRET_"


def secrets_from_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`HAAS_SECRET_GITHUB_TOKEN=x` becomes the run secret `GITHUB_TOKEN=x`."""
    env = os.environ if environ is None else environ
    return {
        k[len(SECRET_ENV_PREFIX) :]: v
        for k, v in env.items()
        if k.startswith(SECRET_ENV_PREFIX) and len(k) > len(SECRET_ENV_PREFIX) and v
    }


@dataclass
class ChatSettings:
    """Mutable per-conversation settings; slash commands edit these."""

    base_url: str = "http://localhost:8000"
    tenant: str = "default-tenant"
    thread_id: str | None = None
    primary_model: str | None = None
    planner_model: str | None = None
    max_steps: int | None = None
    max_cost_usd: float | None = None
    theme: Literal["auto", "dark", "light"] = "auto"
    ascii: bool = False
    no_color: bool = False
    preload_skills: list[str] = field(default_factory=list)
    secrets: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ChatSettings":
        env = os.environ if environ is None else environ
        theme = env.get("HAAS_TUI_THEME", "auto")
        if theme not in ("auto", "dark", "light"):
            raise ValueError("HAAS_TUI_THEME must be auto, dark, or light")

        def opt(name: str, cast):
            raw = env.get(name)
            return cast(raw) if raw else None

        return cls(
            base_url=env.get("HAAS_URL", "http://localhost:8000").rstrip("/"),
            tenant=env.get("HAAS_TENANT", "default-tenant"),
            primary_model=opt("HAAS_PRIMARY_MODEL", str),
            planner_model=opt("HAAS_PLANNER_MODEL", str),
            max_steps=opt("HAAS_MAX_STEPS", int),
            max_cost_usd=opt("HAAS_MAX_COST_USD", float),
            theme=theme,
            ascii=env.get("HAAS_TUI_ASCII", "").lower() in ("1", "true", "yes"),
            no_color=("NO_COLOR" in env)
            or env.get("HAAS_TUI_NO_COLOR", "").lower() in ("1", "true", "yes"),
            preload_skills=env.get("HAAS_PRELOAD_SKILLS", "").replace(",", " ").split(),
            secrets=secrets_from_env(env),
        )
