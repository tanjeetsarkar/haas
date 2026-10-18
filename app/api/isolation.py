"""Phase 4 execution isolation: static catalog, plugin artifacts, sandbox (owner: EXEC)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.core.plugin_manifest import PluginManifest, PluginManifestError, plugin_fingerprint_files, read_plugin_manifest
from app.core.plugin_artifacts import (
    ArtifactError,
    ArtifactMismatchError,
    ArtifactStore,
    pack_plugin,
    materialize,
    remove_tree,
)

router = APIRouter()

PLUGINS_ROOT = Path(os.getenv("HAAS_PLUGINS_DIR", "/home/voldemort/work/harness/plugins")).resolve()
ARTIFACTS_ROOT = Path(os.getenv("HAAS_ARTIFACTS_DIR", "/var/lib/haas/plugin-artifacts")).resolve()


class StaticCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str
    name: str
    description: str
    version: str
    category: str
    enabled: bool
    provides_skills: list[str] | None = None
    provides_tools: list[str] | None = None
    requires_plugins: list[str] = Field(default_factory=list)
    artifact_digest: str
    artifact_size: int
    fingerprint_files: list[str]


class PackPluginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str
    pinned_digest: str | None = None


class PackPluginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str
    version: str
    plugin_digest: str
    artifact_digest: str
    artifact_size: int
    config: dict[str, Any]
    manifest: dict[str, Any]


class MaterializeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str
    pinned_digest: str
    run_id: str
    invocation_id: str


class MaterializeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str
    artifact_digest: str
    sandbox_path: str


class SandboxProtocolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str
    artifact_digest: str
    run_id: str
    invocation_id: str
    tool_name: str
    arguments: dict[str, Any]
    secrets: dict[str, str] = Field(default_factory=dict)
    egress_allowed: bool = True
    limits: dict[str, Any] = Field(default_factory=dict)


class SandboxProtocolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    result: Any = None
    error: str | None = None
    stdout: str = ""
    stderr: str = ""
    execution_time_ms: int = 0


def _get_plugin_dir(plugin_id: str) -> Path:
    plugin_dir = PLUGINS_ROOT / plugin_id
    if not plugin_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")
    return plugin_dir


def _read_manifest(plugin_id: str) -> PluginManifest:
    try:
        return read_plugin_manifest(_get_plugin_dir(plugin_id))
    except PluginManifestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/v1/isolation/catalog", response_model=list[StaticCatalogEntry])
async def list_static_catalog(
    enabled_only: bool = Query(True),
    category: str | None = Query(None),
) -> list[StaticCatalogEntry]:
    """List all plugins in the static catalog with their artifact digests."""
    entries: list[StaticCatalogEntry] = []
    
    for plugin_dir in sorted(PLUGINS_ROOT.iterdir()):
        if not plugin_dir.is_dir():
            continue
        try:
            manifest = read_plugin_manifest(plugin_dir)
        except PluginManifestError:
            continue
        
        if enabled_only and not manifest.enabled:
            continue
        if category and manifest.category != category:
            continue
        
        packed = pack_plugin(manifest)
        artifact_digest = packed.digest
        artifact_size = packed.size
        
        fingerprint_files = [
            f.relative_to(manifest.root).as_posix()
            for f in plugin_fingerprint_files(manifest)
        ]
        
        entries.append(StaticCatalogEntry(
            plugin_id=manifest.id,
            name=manifest.name,
            description=manifest.description,
            version=manifest.version,
            category=manifest.category,
            enabled=manifest.enabled,
            provides_skills=manifest.provides_skills,
            provides_tools=manifest.provides_tools,
            requires_plugins=manifest.requires_plugins,
            artifact_digest=artifact_digest,
            artifact_size=artifact_size,
            fingerprint_files=fingerprint_files,
        ))
    
    return entries


@router.get("/v1/isolation/catalog/{plugin_id}", response_model=StaticCatalogEntry)
async def get_static_catalog_entry(plugin_id: str) -> StaticCatalogEntry:
    """Get a single plugin's static catalog entry."""
    manifest = _read_manifest(plugin_id)
    packed = pack_plugin(manifest)
    artifact_digest = packed.digest
    artifact_size = packed.size
    
    fingerprint_files = [
        f.relative_to(manifest.root).as_posix()
        for f in plugin_fingerprint_files(manifest)
    ]
    
    return StaticCatalogEntry(
        plugin_id=manifest.id,
        name=manifest.name,
        description=manifest.description,
        version=manifest.version,
        category=manifest.category,
        enabled=manifest.enabled,
        provides_skills=manifest.provides_skills,
        provides_tools=manifest.provides_tools,
        requires_plugins=manifest.requires_plugins,
        artifact_digest=artifact_digest,
        artifact_size=artifact_size,
        fingerprint_files=fingerprint_files,
    )


@router.post("/v1/isolation/pack", response_model=PackPluginResponse)
async def pack_plugin_endpoint(request: PackPluginRequest) -> PackPluginResponse:
    """Pack a plugin into an OCI artifact (deterministic)."""
    manifest = _read_manifest(request.plugin_id)
    packed = pack_plugin(manifest)
    
    if request.pinned_digest and packed.digest != request.pinned_digest:
        raise HTTPException(
            status_code=409,
            detail=f"Plugin artifact digest {packed.digest} does not match pinned {request.pinned_digest}"
        )
    
    artifact_store = ArtifactStore(ARTIFACTS_ROOT)
    artifact_store.put(packed)
    
    return PackPluginResponse(
        plugin_id=packed.plugin_id,
        version=packed.version,
        plugin_digest=packed.plugin_digest,
        artifact_digest=packed.digest,
        artifact_size=packed.size,
        config=json.loads(packed.config_bytes),
        manifest=json.loads(packed.manifest_bytes),
    )


@router.post("/v1/isolation/materialize", response_model=MaterializeResponse)
async def materialize_plugin(request: MaterializeRequest) -> MaterializeResponse:
    """Materialize a pinned plugin artifact into a per-run sandbox directory."""
    manifest = _read_manifest(request.plugin_id)
    artifact_store = ArtifactStore(ARTIFACTS_ROOT)
    
    # Create per-run sandbox directory
    sandbox_root = Path(tempfile.gettempdir()) / "haas-sandboxes" / request.run_id / request.invocation_id
    sandbox_root.mkdir(parents=True, exist_ok=True)
    plugin_dest = sandbox_root / request.plugin_id
    
    try:
        executed_digest = materialize(
            manifest,
            plugin_dest,
            pinned=request.pinned_digest,
            store=artifact_store,
        )
    except ArtifactMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ArtifactError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    
    return MaterializeResponse(
        plugin_id=request.plugin_id,
        artifact_digest=executed_digest,
        sandbox_path=str(plugin_dest),
    )


@router.post("/v1/isolation/sandbox/execute", response_model=SandboxProtocolResponse)
async def execute_in_sandbox(request: SandboxProtocolRequest) -> SandboxProtocolResponse:
    """Execute a tool in the sandbox (stub - actual sandbox runtime in Phase 4)."""
    # This is a stub for the sandbox protocol. The actual sandbox runtime
    # will be implemented with proper isolation (gVisor, Firecracker, etc.)
    import time
    start = time.perf_counter()
    
    try:
        # Materialize the plugin if needed
        manifest = _read_manifest(request.plugin_id)
        artifact_store = ArtifactStore(ARTIFACTS_ROOT)
        
        sandbox_root = Path(tempfile.gettempdir()) / "haas-sandboxes" / request.run_id / request.invocation_id
        plugin_dest = sandbox_root / request.plugin_id
        
        if not plugin_dest.exists():
            materialize(manifest, plugin_dest, pinned=request.artifact_digest, store=artifact_store)
        
        # For now, return a stub response indicating the sandbox protocol is ready
        # The actual tool execution will be implemented with the sandbox runtime
        return SandboxProtocolResponse(
            success=True,
            result={"status": "sandbox_protocol_ready", "plugin_path": str(plugin_dest)},
            execution_time_ms=int((time.perf_counter() - start) * 1000),
        )
    except Exception as exc:
        return SandboxProtocolResponse(
            success=False,
            error=str(exc),
            execution_time_ms=int((time.perf_counter() - start) * 1000),
        )


@router.delete("/v1/isolation/sandbox/{run_id}/{invocation_id}")
async def cleanup_sandbox(run_id: str, invocation_id: str) -> dict[str, str]:
    """Clean up a sandbox directory after execution."""
    sandbox_root = Path(tempfile.gettempdir()) / "haas-sandboxes" / run_id / invocation_id
    if sandbox_root.exists():
        remove_tree(sandbox_root)
    return {"status": "cleaned"}
