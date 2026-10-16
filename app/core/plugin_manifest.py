"""Static plugin manifests (`plugins/<id>/plugin.json`).

Everything here is metadata-only: reading a manifest never imports plugin code, so the
loader, `haas-plugin inspect/doctor` and policy checks can reason about a plugin cheaply
and safely before (or without) executing any of its tools.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_FILE = "plugin.json"
SKILL_MAX_DEPTH = 3
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

DEFAULT_SKILLS = ["skills"]
DEFAULT_TOOLS = ["tools/*.py"]
DEFAULT_ENDPOINTS = ["endpoints/*.json"]

_ALLOWED_KEYS = {
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
LEGACY_DIRS = ("skills", "tools", "manifests")


class PluginManifestError(ValueError):
    """The manifest is missing, malformed, or points outside the plugin directory."""


@dataclass
class PluginManifest:
    id: str
    name: str
    description: str
    version: str
    category: str
    enabled: bool
    root: Path
    skill_roots: list[Path] = field(default_factory=list)
    tool_files: list[Path] = field(default_factory=list)
    endpoint_files: list[Path] = field(default_factory=list)
    provides_skills: list[str] | None = None
    provides_tools: list[str] | None = None
    requires_plugins: list[str] = field(default_factory=list)


def _str_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PluginManifestError(f"`{what}` must be a list of strings")
    return value


def _contained(root: Path, rel: str, what: str) -> None:
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PluginManifestError(
            f"`{what}` entry '{rel}' must be a relative path inside the plugin"
        )


def _expand(root: Path, patterns: list[str], what: str, *, dirs: bool) -> list[Path]:
    found: dict[Path, None] = {}
    real_root = root.resolve()
    for pat in patterns:
        _contained(root, pat, what)
        for match in sorted(root.glob(pat)):
            resolved = match.resolve()
            if not resolved.is_relative_to(real_root):
                raise PluginManifestError(
                    f"`{what}` entry '{pat}' resolves outside the plugin directory"
                )
            if dirs and match.is_dir():
                found[resolved] = None
            elif not dirs and match.is_file() and not match.name.startswith("_"):
                found[resolved] = None
    return list(found)


def read_plugin_manifest(plugin_dir: Path) -> PluginManifest:
    path = plugin_dir / MANIFEST_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PluginManifestError(f"{MANIFEST_FILE} not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginManifestError(f"{MANIFEST_FILE} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PluginManifestError(f"{MANIFEST_FILE} must contain a JSON object")

    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        raise PluginManifestError(f"unknown manifest key(s): {unknown}")

    plugin_id = str(raw.get("id", "")).strip()
    if not plugin_id or len(plugin_id) > 64 or not NAME_RE.match(plugin_id):
        raise PluginManifestError(
            f"invalid plugin id '{plugin_id}' (lowercase a-z, 0-9, single hyphens, <=64 chars)"
        )
    if plugin_id != plugin_dir.name:
        raise PluginManifestError(
            f"plugin id '{plugin_id}' must match directory '{plugin_dir.name}'"
        )
    description = str(raw.get("description", "")).strip()
    if not description:
        raise PluginManifestError("`description` is required")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PluginManifestError("`enabled` must be a boolean")

    provides = raw.get("provides") or {}
    requires = raw.get("requires") or {}
    if not isinstance(provides, dict) or not isinstance(requires, dict):
        raise PluginManifestError("`provides` and `requires` must be objects")
    bad = (set(provides) - {"skills", "tools"}) | (set(requires) - {"plugins"})
    if bad:
        raise PluginManifestError(f"unknown key(s) in provides/requires: {sorted(bad)}")

    skills_pats = _str_list(raw.get("skills"), "skills") or DEFAULT_SKILLS
    tools_pats = (
        _str_list(raw.get("tools"), "tools") if "tools" in raw else list(DEFAULT_TOOLS)
    )
    endpoint_pats = (
        _str_list(raw.get("endpoints"), "endpoints")
        if "endpoints" in raw
        else list(DEFAULT_ENDPOINTS)
    )

    tool_files = _expand(plugin_dir, tools_pats, "tools", dirs=False)
    for f in tool_files:
        if f.suffix != ".py":
            raise PluginManifestError(f"tool file '{f.name}' must be a .py file")
    endpoint_files = _expand(plugin_dir, endpoint_pats, "endpoints", dirs=False)
    for f in endpoint_files:
        if f.suffix != ".json":
            raise PluginManifestError(f"endpoint file '{f.name}' must be a .json file")

    return PluginManifest(
        id=plugin_id,
        name=str(raw.get("name") or plugin_id),
        description=description,
        version=str(raw.get("version", "1.0.0")),
        category=str(raw.get("category", "")).strip(),
        enabled=enabled,
        root=plugin_dir.resolve(),
        skill_roots=_expand(plugin_dir, skills_pats, "skills", dirs=True),
        tool_files=tool_files,
        endpoint_files=endpoint_files,
        provides_skills=(
            _str_list(provides["skills"], "provides.skills")
            if "skills" in provides
            else None
        ),
        provides_tools=(
            _str_list(provides["tools"], "provides.tools")
            if "tools" in provides
            else None
        ),
        requires_plugins=_str_list(requires.get("plugins"), "requires.plugins"),
    )


def find_skill_dirs(skills_root: Path, max_depth: int = SKILL_MAX_DEPTH) -> list[Path]:
    """Finds `SKILL.md` directories, allowing grouped layouts (`skills/<group>/<name>/`).

    The folder path is organisation only; traversal stops below a directory that holds a
    SKILL.md. Symlinked directories are not followed so a skill cannot escape its plugin.
    """
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        if (directory / "SKILL.md").is_file():
            found.append(directory)
            return
        if depth >= max_depth:
            return
        for child in sorted(directory.iterdir()):
            if (
                child.is_dir()
                and not child.is_symlink()
                and not child.name.startswith(("_", "."))
            ):
                walk(child, depth + 1)

    if skills_root.is_dir():
        walk(skills_root, 0)
    return found


def plugin_fingerprint_files(manifest: PluginManifest) -> list[Path]:
    files: dict[Path, None] = {manifest.root / MANIFEST_FILE: None}
    for f in (*manifest.tool_files, *manifest.endpoint_files):
        files[f] = None
    for sroot in manifest.skill_roots:
        for skill_dir in find_skill_dirs(sroot):
            for f in sorted(skill_dir.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts:
                    files[f] = None
    return sorted(files)


def hash_plugin(manifest: PluginManifest) -> str:
    h = hashlib.sha256()
    h.update(f"{manifest.id}\0{manifest.version}\0".encode())
    for f in plugin_fingerprint_files(manifest):
        h.update(f.relative_to(manifest.root).as_posix().encode() + b"\0")
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()
