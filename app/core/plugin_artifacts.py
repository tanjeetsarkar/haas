"""Content-addressed plugin artifacts in an OCI-image-layout-like store.

A plugin pack is turned into three blobs: a deterministic tar ``layer`` (exactly the files
covered by ``hash_plugin``, mtime 0, uid/gid 0, fixed modes), a ``config`` JSON (plugin id,
version, source digest, file list) and an OCI image manifest (``artifactType``
``application/vnd.haas.plugin.v1``). The artifact digest is the sha256 of the manifest
bytes, so it transitively pins every byte of code that will run.

Store layout (``PLUGIN_ARTIFACTS_DIR``)::

    oci-layout                    {"imageLayoutVersion": "1.0.0"}
    index.json                    OCI image index; one entry per stored plugin manifest
    blobs/sha256/<hex>            manifest, config and layer blobs

Packing is deterministic, so a worker without access to the store can repack its local
plugin source and must refuse execution when the digest differs from the run's pin.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.plugin_manifest import (
    PluginManifest,
    hash_plugin,
    plugin_fingerprint_files,
)

MEDIA_INDEX = "application/vnd.oci.image.index.v1+json"
MEDIA_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_CONFIG = "application/vnd.haas.plugin.config.v1+json"
MEDIA_LAYER = "application/vnd.haas.plugin.layer.v1.tar"
ARTIFACT_TYPE = "application/vnd.haas.plugin.v1"
CONFIG_SCHEMA = "haas.plugin.config/1"
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
DIGEST_PREFIX = "sha256:"


class ArtifactError(RuntimeError):
    """The artifact is missing, corrupted or malformed."""


class ArtifactMismatchError(ArtifactError):
    """The plugin code available here is not the artifact the run pinned."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def blob_digest(data: bytes) -> str:
    return DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def _hex(digest: str) -> str:
    if not digest.startswith(DIGEST_PREFIX):
        raise ArtifactError(f"unsupported digest '{digest}'")
    hexpart = digest[len(DIGEST_PREFIX) :]
    if len(hexpart) != 64 or any(c not in "0123456789abcdef" for c in hexpart):
        raise ArtifactError(f"malformed digest '{digest}'")
    return hexpart


@dataclass(frozen=True)
class PackedPlugin:
    plugin_id: str
    version: str
    plugin_digest: str
    manifest_bytes: bytes
    config_bytes: bytes
    layer_bytes: bytes

    @property
    def digest(self) -> str:
        return blob_digest(self.manifest_bytes)

    @property
    def size(self) -> int:
        return (
            len(self.manifest_bytes) + len(self.config_bytes) + len(self.layer_bytes)
        )

    def blobs(self) -> dict[str, bytes]:
        return {
            blob_digest(b): b
            for b in (self.manifest_bytes, self.config_bytes, self.layer_bytes)
        }


def build_layer(manifest: PluginManifest) -> tuple[bytes, list[str]]:
    buf = io.BytesIO()
    names: list[str] = []
    total = 0
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for path in plugin_fingerprint_files(manifest):
            if path.is_symlink():
                raise ArtifactError(f"{path}: symlinks are not packed")
            rel = path.relative_to(manifest.root).as_posix()
            data = path.read_bytes()
            total += len(data)
            if total > MAX_ARTIFACT_BYTES:
                raise ArtifactError(
                    f"plugin '{manifest.id}' exceeds {MAX_ARTIFACT_BYTES} bytes"
                )
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
            names.append(rel)
    return buf.getvalue(), names


def pack_plugin(manifest: PluginManifest) -> PackedPlugin:
    plugin_digest = hash_plugin(manifest)
    layer, files = build_layer(manifest)
    config = _canonical(
        {
            "schema": CONFIG_SCHEMA,
            "plugin_id": manifest.id,
            "version": manifest.version,
            "plugin_digest": plugin_digest,
            "files": files,
        }
    )
    oci_manifest = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": MEDIA_MANIFEST,
            "artifactType": ARTIFACT_TYPE,
            "config": {
                "mediaType": MEDIA_CONFIG,
                "digest": blob_digest(config),
                "size": len(config),
            },
            "layers": [
                {
                    "mediaType": MEDIA_LAYER,
                    "digest": blob_digest(layer),
                    "size": len(layer),
                }
            ],
            "annotations": {
                "org.opencontainers.image.title": manifest.id,
                "org.opencontainers.image.version": manifest.version,
                "dev.haas.plugin.digest": plugin_digest,
            },
        }
    )
    return PackedPlugin(
        manifest.id, manifest.version, plugin_digest, oci_manifest, config, layer
    )


def artifact_digest(manifest: PluginManifest) -> str:
    return pack_plugin(manifest).digest


def _safe_member(member: tarfile.TarInfo) -> PurePosixPath:
    rel = PurePosixPath(member.name)
    if (
        not member.isreg()
        or rel.is_absolute()
        or ".." in rel.parts
        or not rel.parts
        or member.name != rel.as_posix()
    ):
        raise ArtifactError(f"unsafe layer entry '{member.name}'")
    return rel


def extract_layer(layer: bytes, dest: Path, *, read_only: bool = True) -> list[Path]:
    """Extracts a plugin layer into ``dest`` (which must not exist yet)."""
    dest.mkdir(parents=True)
    written: list[Path] = []
    with tarfile.open(fileobj=io.BytesIO(layer), mode="r:") as tar:
        for member in tar.getmembers():
            rel = _safe_member(member)
            target = dest.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                raise ArtifactError(f"unreadable layer entry '{member.name}'")
            target.write_bytes(src.read())
            written.append(target)
    if read_only:
        for f in written:
            f.chmod(0o444)
        for d in sorted({p.parent for p in written} | {dest}, reverse=True):
            d.chmod(0o555)
    return written


def make_writable(path: Path) -> None:
    """Undo ``extract_layer``'s read-only modes so the tree can be removed."""
    if not path.exists():
        return
    for root, dirs, _files in os.walk(path):
        os.chmod(root, stat.S_IRWXU)
        for d in dirs:
            os.chmod(os.path.join(root, d), stat.S_IRWXU)


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def blobs_dir(self) -> Path:
        return self.root / "blobs" / "sha256"

    def _blob_path(self, digest: str) -> Path:
        return self.blobs_dir / _hex(digest)

    def _ensure_layout(self) -> None:
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        layout = self.root / "oci-layout"
        if not layout.exists():
            self._atomic_write(layout, _canonical({"imageLayoutVersion": "1.0.0"}))

    def _atomic_write(self, path: Path, data: bytes) -> None:
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def has(self, digest: str) -> bool:
        return self._blob_path(digest).is_file()

    def put(self, packed: PackedPlugin) -> str:
        self._ensure_layout()
        for digest, data in packed.blobs().items():
            if not self.has(digest):
                self._atomic_write(self._blob_path(digest), data)
        entry = {
            "mediaType": MEDIA_MANIFEST,
            "artifactType": ARTIFACT_TYPE,
            "digest": packed.digest,
            "size": len(packed.manifest_bytes),
            "annotations": {
                "org.opencontainers.image.ref.name": f"{packed.plugin_id}:{packed.version}",
                "dev.haas.plugin.id": packed.plugin_id,
                "dev.haas.plugin.digest": packed.plugin_digest,
            },
        }
        with open(self.root / ".lock", "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            index = self.index()
            if not any(m.get("digest") == packed.digest for m in index["manifests"]):
                index["manifests"].append(entry)
                index["manifests"].sort(key=lambda m: m["digest"])
                self._atomic_write(self.root / "index.json", _canonical(index))
        return packed.digest

    def index(self) -> dict[str, Any]:
        path = self.root / "index.json"
        if not path.is_file():
            return {"schemaVersion": 2, "mediaType": MEDIA_INDEX, "manifests": []}
        return json.loads(path.read_bytes())

    def read_blob(self, digest: str) -> bytes:
        path = self._blob_path(digest)
        if not path.is_file():
            raise ArtifactError(f"blob {digest} is not in the artifact store")
        data = path.read_bytes()
        if blob_digest(data) != digest:
            raise ArtifactError(f"blob {digest} is corrupted (digest mismatch)")
        return data

    def load(self, digest: str) -> tuple[dict[str, Any], dict[str, Any], bytes]:
        """Returns (manifest, config, layer) after verifying every blob's digest and size."""
        manifest = json.loads(self.read_blob(digest))
        if manifest.get("artifactType") != ARTIFACT_TYPE:
            raise ArtifactError(f"{digest} is not a HaaS plugin artifact")
        cfg_desc = manifest["config"]
        layers = manifest.get("layers") or []
        if len(layers) != 1:
            raise ArtifactError(f"{digest} must have exactly one layer")
        config_bytes = self.read_blob(cfg_desc["digest"])
        layer = self.read_blob(layers[0]["digest"])
        if len(config_bytes) != cfg_desc["size"] or len(layer) != layers[0]["size"]:
            raise ArtifactError(f"{digest} blob size does not match its descriptor")
        config = json.loads(config_bytes)
        if config.get("schema") != CONFIG_SCHEMA:
            raise ArtifactError(f"{digest} has an unsupported config schema")
        return manifest, config, layer

    def verify(self, digest: str) -> dict[str, Any]:
        return self.load(digest)[1]


def materialize(
    manifest: PluginManifest,
    dest: Path,
    *,
    pinned: str = "",
    store: ArtifactStore | None = None,
) -> str:
    """Extracts the plugin code to run into ``dest``; returns the executed artifact digest.

    With a ``pinned`` digest the stored artifact is used when present (after verifying all
    blob digests); otherwise the local source is packed and must reproduce the pin exactly.
    Any mismatch raises ``ArtifactMismatchError``; nothing is extracted in that case.
    """
    if pinned:
        _hex(pinned)
        if store is not None and store.has(pinned):
            _manifest, config, layer = store.load(pinned)
            if config.get("plugin_id") != manifest.id:
                raise ArtifactMismatchError(
                    f"pinned artifact {pinned} belongs to plugin "
                    f"'{config.get('plugin_id')}', not '{manifest.id}'"
                )
            extract_layer(layer, dest)
            return pinned
        packed = pack_plugin(manifest)
        if packed.digest != pinned:
            raise ArtifactMismatchError(
                f"plugin '{manifest.id}' artifact {packed.digest} does not match the "
                f"pinned {pinned}"
            )
        if store is not None:
            store.put(packed)
        extract_layer(packed.layer_bytes, dest)
        return packed.digest
    packed = pack_plugin(manifest)
    extract_layer(packed.layer_bytes, dest)
    return packed.digest


def remove_tree(path: Path) -> None:
    make_writable(path)
    shutil.rmtree(path, ignore_errors=True)
