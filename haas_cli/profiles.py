from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENVIRONMENTS = {"unknown", "dev", "staging", "prod"}


@dataclass(frozen=True)
class EnvironmentProfile:
    name: str
    base_url: str
    tenant: str
    environment: Literal["unknown", "dev", "staging", "prod"] = "unknown"

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str)
            for value in (self.name, self.base_url, self.tenant, self.environment)
        ):
            raise ValueError("Profile fields must be strings.")
        if not _PROFILE_NAME.fullmatch(self.name):
            raise ValueError("Profile names must be 1-64 URL-safe characters.")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Profile URLs must be absolute HTTP(S) URLs without credentials."
            )
        if not self.tenant.strip():
            raise ValueError("Profile tenant must not be empty.")
        if self.environment not in _ENVIRONMENTS:
            raise ValueError(
                "Profile environment must be unknown, dev, staging, or prod."
            )


def default_profiles_path() -> Path:
    override = os.environ.get("HAAS_PROFILES_PATH")
    if override:
        return Path(override).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "haas" / "profiles.json"


class ProfileStore:
    """Stores endpoint metadata only; credentials remain in process memory."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_profiles_path()

    def _read(self) -> tuple[str | None, dict[str, EnvironmentProfile]]:
        if not self.path.exists():
            return None, {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read HaaS profiles at {self.path}.") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict):
            raise ValueError(
                f"HaaS profiles file at {self.path} has an invalid format."
            )
        active = raw.get("active")
        if active is not None and not isinstance(active, str):
            raise ValueError(
                f"HaaS profiles file at {self.path} has an invalid active profile."
            )
        profiles: dict[str, EnvironmentProfile] = {}
        for name, values in raw["profiles"].items():
            if not isinstance(name, str) or not isinstance(values, dict):
                raise ValueError(
                    f"HaaS profiles file at {self.path} has an invalid entry."
                )
            try:
                profiles[name] = EnvironmentProfile(
                    name=name,
                    base_url=values["base_url"],
                    tenant=values["tenant"],
                    environment=values.get("environment", "unknown"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"HaaS profile {name!r} is invalid.") from exc
        if active is not None and active not in profiles:
            raise ValueError(f"Active HaaS profile {active!r} does not exist.")
        return active, profiles

    def profiles(self) -> list[EnvironmentProfile]:
        _, profiles = self._read()
        return sorted(profiles.values(), key=lambda profile: profile.name.casefold())

    def get(self, name: str) -> EnvironmentProfile | None:
        _, profiles = self._read()
        return profiles.get(name)

    def active(self) -> EnvironmentProfile | None:
        active, profiles = self._read()
        return profiles.get(active) if active else None

    def save(self, profile: EnvironmentProfile, *, activate: bool = False) -> None:
        active, profiles = self._read()
        profiles[profile.name] = profile
        self._write(profile.name if activate else active, profiles)

    def activate(self, name: str) -> EnvironmentProfile:
        active, profiles = self._read()
        profile = profiles.get(name)
        if profile is None:
            raise ValueError(f"HaaS profile {name!r} does not exist.")
        self._write(name, profiles)
        return profile

    def remove(self, name: str) -> None:
        active, profiles = self._read()
        if name not in profiles:
            raise ValueError(f"HaaS profile {name!r} does not exist.")
        del profiles[name]
        self._write(None if active == name else active, profiles)

    def _write(
        self, active: str | None, profiles: dict[str, EnvironmentProfile]
    ) -> None:
        document = {
            "active": active,
            "profiles": {
                profile.name: {
                    "base_url": profile.base_url,
                    "tenant": profile.tenant,
                    "environment": profile.environment,
                }
                for profile in sorted(
                    profiles.values(), key=lambda item: item.name.casefold()
                )
            },
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
            os.replace(temporary_name, self.path)
            self.path.chmod(0o600)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
