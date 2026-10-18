"""Plugin manifest v2 (`manifest_version: 2`) schema, parser, v1 upgrade and permission diff.

v1 manifests (the operator-installed packs read by ``app/core/plugin_manifest.py``) are
accepted and upgraded in memory: they gain a namespace, a semver version and conservative
defaults (no egress, no secrets, every tool ``write`` risk with approval required).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.marketplace.canonical import digest_json, is_sha256
from app.marketplace.semver import coerce_version, parse_version, validate_range

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
PLUGIN_ID_RE = re.compile(r"^@([a-z0-9]+(?:-[a-z0-9]+)*)/([a-z0-9]+(?:-[a-z0-9]+)*)$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
API_VERSIONS = ("haas-plugin/1",)
RISK_CLASSES = ("read", "write", "destructive", "financial", "exfiltration-capable")
RISK_ORDER = {name: index for index, name in enumerate(RISK_CLASSES)}
SANDBOX_PROFILES = ("no-network", "network-restricted", "standard")
SANDBOX_ORDER = {name: index for index, name in enumerate(SANDBOX_PROFILES)}

Risk = Literal["read", "write", "destructive", "financial", "exfiltration-capable"]


class ManifestError(ValueError):
    """The manifest is malformed; ``errors`` lists every problem found."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _unique(values: list[str], what: str) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError(f"{what} must be unique")
    return values


class Provides(_Strict):
    skills: list[str] = Field(default_factory=list, max_length=64)
    tools: list[str] = Field(default_factory=list, max_length=256)

    @field_validator("skills")
    @classmethod
    def _skills(cls, values: list[str]) -> list[str]:
        for value in values:
            if not NAME_RE.fullmatch(value):
                raise ValueError(f"invalid skill name '{value}'")
        return _unique(values, "skills")

    @field_validator("tools")
    @classmethod
    def _tools(cls, values: list[str]) -> list[str]:
        for value in values:
            if not TOOL_NAME_RE.fullmatch(value):
                raise ValueError(f"invalid tool name '{value}'")
        return _unique(values, "tools")


class Requires(_Strict):
    plugins: dict[str, str] = Field(default_factory=dict)

    @field_validator("plugins")
    @classmethod
    def _plugins(cls, values: dict[str, str]) -> dict[str, str]:
        for plugin_id, spec in values.items():
            if not PLUGIN_ID_RE.fullmatch(plugin_id):
                raise ValueError(f"required plugin '{plugin_id}' must be '@publisher/name'")
            validate_range(spec)
        return values


class WorkspaceScope(_Strict):
    read: list[str] = Field(default_factory=list, max_length=32)
    write: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("read", "write")
    @classmethod
    def _paths(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.startswith("/") or ".." in value.split("/"):
                raise ValueError(f"workspace path '{value}' must be absolute without '..'")
        return _unique(values, "workspace paths")


class Permissions(_Strict):
    egress: list[str] = Field(default_factory=list, max_length=64)
    secrets: list[str] = Field(default_factory=list, max_length=32)
    workspace: WorkspaceScope = Field(default_factory=WorkspaceScope)
    sandbox_profile: Literal["no-network", "network-restricted", "standard"] = (
        "no-network"
    )

    @field_validator("egress")
    @classmethod
    def _egress(cls, values: list[str]) -> list[str]:
        normalized = [normalize_origin(value) for value in values]
        return _unique(normalized, "egress destinations")

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, values: list[str]) -> list[str]:
        for value in values:
            if not SECRET_NAME_RE.fullmatch(value):
                raise ValueError(
                    f"secret name '{value}' must be UPPER_SNAKE_CASE (2-64 chars)"
                )
        return _unique(values, "secret names")

    @model_validator(mode="after")
    def _network(self) -> Permissions:
        if self.egress and self.sandbox_profile == "no-network":
            raise ValueError(
                "egress destinations require sandbox_profile 'network-restricted' or 'standard'"
            )
        return self


class ToolSpec(_Strict):
    risk: Risk
    approval: Literal["required", "policy"] = "policy"
    description: str | None = Field(default=None, max_length=2_000)
    input_schema: dict[str, Any] | None = None


class RemoteServer(_Strict):
    protocol: Literal["mcp", "http"]
    url: str
    protocol_version: str | None = Field(default=None, max_length=32)
    # Rug-pull pinning: a changed upstream tool schema blocks use until re-consent.
    tool_schema_digests: dict[str, str] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("remote server url must be an https URL")
        return value

    @field_validator("tool_schema_digests")
    @classmethod
    def _digests(cls, values: dict[str, str]) -> dict[str, str]:
        for tool, digest in values.items():
            if not is_sha256(digest):
                raise ValueError(f"tool schema digest for '{tool}' must be sha256 hex")
        return values


class ManifestV2(_Strict):
    manifest_version: Literal[2]
    id: str
    version: str
    api_version: str = "haas-plugin/1"
    name: str | None = Field(default=None, max_length=128)
    description: str = Field(min_length=10, max_length=4_000)
    license: str | None = Field(default=None, max_length=64)
    homepage: str | None = Field(default=None, max_length=512)
    categories: list[str] = Field(default_factory=list, max_length=8)
    keywords: list[str] = Field(default_factory=list, max_length=16)
    provides: Provides = Field(default_factory=Provides)
    requires: Requires = Field(default_factory=Requires)
    permissions: Permissions = Field(default_factory=Permissions)
    tools: dict[str, ToolSpec] = Field(default_factory=dict)
    remote: RemoteServer | None = None
    sbom: str | None = Field(default=None, max_length=256)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not PLUGIN_ID_RE.fullmatch(value) or len(value) > 130:
            raise ValueError("id must be '@publisher/plugin' (lowercase, digits, hyphens)")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        return str(parse_version(value))

    @field_validator("api_version")
    @classmethod
    def _api(cls, value: str) -> str:
        if value not in API_VERSIONS:
            raise ValueError(f"api_version must be one of {list(API_VERSIONS)}")
        return value

    @field_validator("categories", "keywords")
    @classmethod
    def _tags(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip().lower() for value in values]
        for value in cleaned:
            if not NAME_RE.fullmatch(value) or len(value) > 32:
                raise ValueError(f"invalid category/keyword '{value}'")
        return _unique(cleaned, "categories/keywords")

    @field_validator("tools")
    @classmethod
    def _tool_names(cls, values: dict[str, ToolSpec]) -> dict[str, ToolSpec]:
        for name in values:
            if not TOOL_NAME_RE.fullmatch(name):
                raise ValueError(f"invalid tool name '{name}'")
        return values

    @model_validator(mode="after")
    def _consistency(self) -> ManifestV2:
        declared = set(self.provides.tools)
        described = set(self.tools)
        if declared - described:
            raise ValueError(
                f"tools {sorted(declared - described)} are provided but have no risk class in `tools`"
            )
        if described - declared:
            raise ValueError(
                f"tools {sorted(described - declared)} are described but not listed in provides.tools"
            )
        if self.remote is not None:
            origin = normalize_origin(self.remote.url)
            if origin not in self.permissions.egress:
                raise ValueError("remote server origin must be declared in permissions.egress")
        return self

    @property
    def publisher(self) -> str:
        match = PLUGIN_ID_RE.fullmatch(self.id)
        assert match is not None
        return match[1]

    @property
    def plugin_name(self) -> str:
        match = PLUGIN_ID_RE.fullmatch(self.id)
        assert match is not None
        return match[2]

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


@dataclass(frozen=True, slots=True)
class ParsedManifest:
    manifest: ManifestV2
    source_version: int
    warnings: tuple[str, ...] = ()


def normalize_origin(value: str) -> str:
    """Egress destinations are exact https origins (no wildcards, paths, or credentials)."""
    if not isinstance(value, str):
        raise ValueError("egress destination must be a string")
    parts = urlsplit(value.strip())
    if parts.scheme != "https":
        raise ValueError(f"egress destination '{value}' must use https")
    host = parts.hostname or ""
    if not host or "*" in value or parts.username or parts.password:
        raise ValueError(f"egress destination '{value}' must be an exact https origin")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"egress destination '{value}' must not include a path or query")
    if re.fullmatch(r"[0-9.]+|\[?[0-9a-fA-F:]+\]?", host):
        raise ValueError(f"egress destination '{value}' must use a DNS hostname")
    port = f":{parts.port}" if parts.port and parts.port != 443 else ""
    return f"https://{host.lower()}{port}"


def _format_errors(exc: ValidationError) -> list[str]:
    errors = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "manifest"
        message = str(error["msg"]).removeprefix("Value error, ")
        errors.append(f"{location}: {message}")
    return errors


_V1_KEYS = {
    "id",
    "name",
    "description",
    "version",
    "category",
    "enabled",
    "skills",
    "tools",
    "endpoints",
    "provides",
    "requires",
}


def upgrade_v1(raw: dict[str, Any], *, namespace: str = "local") -> ParsedManifest:
    """Converts an operator pack manifest (v1) into v2 with least-privilege defaults."""
    errors: list[str] = []
    unknown = sorted(set(raw) - _V1_KEYS - {"manifest_version"})
    if unknown:
        errors.append(f"unknown manifest key(s): {unknown}")
    plugin_id = str(raw.get("id", "")).strip()
    if not NAME_RE.fullmatch(plugin_id) or len(plugin_id) > 64:
        errors.append(f"id: invalid v1 plugin id '{plugin_id}'")
    if not NAME_RE.fullmatch(namespace):
        errors.append(f"namespace: invalid publisher namespace '{namespace}'")
    provides = raw.get("provides") or {}
    requires = raw.get("requires") or {}
    if not isinstance(provides, dict) or not isinstance(requires, dict):
        errors.append("provides/requires must be objects")
        provides, requires = {}, {}
    try:
        version = str(coerce_version(str(raw.get("version", "1.0.0"))))
    except ValueError as exc:
        errors.append(f"version: {exc}")
        version = "0.0.0"
    if errors:
        raise ManifestError(errors)
    tools = [str(tool) for tool in provides.get("tools") or []]
    category = str(raw.get("category") or "").strip().lower()
    data: dict[str, Any] = {
        "manifest_version": 2,
        "id": f"@{namespace}/{plugin_id}",
        "version": version,
        "name": raw.get("name") or plugin_id,
        "description": str(raw.get("description", "")).strip(),
        "categories": [category] if category and NAME_RE.fullmatch(category) else [],
        "provides": {
            "skills": [str(skill) for skill in provides.get("skills") or []],
            "tools": tools,
        },
        "requires": {
            "plugins": {
                f"@{namespace}/{dep}": "*" for dep in requires.get("plugins") or []
            }
        },
        "tools": {tool: {"risk": "write", "approval": "required"} for tool in tools},
    }
    try:
        manifest = ManifestV2.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(_format_errors(exc)) from exc
    warnings = (
        "v1 manifest upgraded: tools default to risk 'write' with approval required; "
        "declare permissions explicitly with manifest_version 2",
    )
    return ParsedManifest(manifest, 1, warnings)


def parse_manifest(raw: Any, *, namespace: str | None = None) -> ParsedManifest:
    """Parses a v2 manifest, or upgrades a v1 manifest when ``manifest_version`` is absent/1."""
    if not isinstance(raw, dict):
        raise ManifestError(["manifest must be a JSON object"])
    version = raw.get("manifest_version", 1)
    if version == 1:
        return upgrade_v1(raw, namespace=namespace or "local")
    if version != 2:
        raise ManifestError([f"manifest_version: unsupported value {version!r}"])
    try:
        manifest = ManifestV2.model_validate(raw)
    except ValidationError as exc:
        raise ManifestError(_format_errors(exc)) from exc
    if namespace is not None and manifest.publisher != namespace:
        raise ManifestError(
            [f"id: namespace '@{manifest.publisher}' does not match '@{namespace}'"]
        )
    return ParsedManifest(manifest, 2)


def manifest_digest(raw: dict[str, Any]) -> str:
    return digest_json(raw)


def permission_summary(manifest: ManifestV2) -> dict[str, Any]:
    """What a tenant consents to: egress, secrets, workspace scope, sandbox and tool risks."""
    return {
        "egress": sorted(manifest.permissions.egress),
        "secrets": sorted(manifest.permissions.secrets),
        "workspace": {
            "read": sorted(manifest.permissions.workspace.read),
            "write": sorted(manifest.permissions.workspace.write),
        },
        "sandbox_profile": manifest.permissions.sandbox_profile,
        "tools": {
            name: {"risk": spec.risk, "approval": spec.approval}
            for name, spec in sorted(manifest.tools.items())
        },
        "remote": (
            {
                "protocol": manifest.remote.protocol,
                "url": manifest.remote.url,
                "tool_schema_digests": dict(sorted(manifest.remote.tool_schema_digests.items())),
            }
            if manifest.remote
            else None
        ),
    }


def permission_diff(
    previous: dict[str, Any] | None, current: dict[str, Any]
) -> dict[str, Any]:
    """Diff two ``permission_summary`` dicts; ``expands`` means re-consent is required."""
    before = previous or {
        "egress": [],
        "secrets": [],
        "workspace": {"read": [], "write": []},
        "sandbox_profile": "no-network",
        "tools": {},
        "remote": None,
    }

    def added(key: str) -> list[str]:
        return sorted(set(current[key]) - set(before[key]))

    def removed(key: str) -> list[str]:
        return sorted(set(before[key]) - set(current[key]))

    workspace_added = {
        mode: sorted(set(current["workspace"][mode]) - set(before["workspace"][mode]))
        for mode in ("read", "write")
    }
    risk_escalations = []
    approval_relaxed = []
    for tool, spec in current["tools"].items():
        old = before["tools"].get(tool)
        if old is None:
            continue
        if RISK_ORDER[spec["risk"]] > RISK_ORDER[old["risk"]]:
            risk_escalations.append({"tool": tool, "from": old["risk"], "to": spec["risk"]})
        if old["approval"] == "required" and spec["approval"] != "required":
            approval_relaxed.append(tool)
    new_tools = sorted(set(current["tools"]) - set(before["tools"]))
    sandbox_widened = (
        SANDBOX_ORDER[current["sandbox_profile"]] > SANDBOX_ORDER[before["sandbox_profile"]]
    )
    remote_changed = (before.get("remote") or None) != (current.get("remote") or None)
    expands = bool(
        added("egress")
        or added("secrets")
        or workspace_added["read"]
        or workspace_added["write"]
        or risk_escalations
        or approval_relaxed
        or new_tools
        or sandbox_widened
        or (remote_changed and previous is not None)
    )
    return {
        "expands": expands,
        "added_egress": added("egress"),
        "removed_egress": removed("egress"),
        "added_secrets": added("secrets"),
        "removed_secrets": removed("secrets"),
        "added_workspace": workspace_added,
        "added_tools": new_tools,
        "removed_tools": sorted(set(before["tools"]) - set(current["tools"])),
        "risk_escalations": risk_escalations,
        "approval_relaxed": sorted(approval_relaxed),
        "sandbox_widened": sandbox_widened,
        "remote_changed": remote_changed and previous is not None,
    }
