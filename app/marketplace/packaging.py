"""Deterministic plugin archives (gzip'd tar) and safe, read-only archive inspection.

The archive's sha256 is the plugin artifact identity (same hex digest form EXEC's
content-addressed artifacts use). Reading an archive never extracts to disk or runs code.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

from app.marketplace.canonical import sha256_hex

MANIFEST_FILE = "plugin.json"
MAX_FILES = 2_000
_EXCLUDED_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


class ArchiveError(ValueError):
    """The archive is malformed, unsafe, or too large."""


def _included(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in _EXCLUDED_DIRS or part.startswith(".") for part in relative.parts):
        return False
    return path.suffix not in _EXCLUDED_SUFFIXES


def collect_files(root: Path) -> dict[str, bytes]:
    root = root.resolve()
    if not (root / MANIFEST_FILE).is_file():
        raise ArchiveError(f"{MANIFEST_FILE} not found in {root}")
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ArchiveError(f"symlinks are not allowed in plugin archives: {path}")
        if path.is_file() and _included(path, root):
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    if len(files) > MAX_FILES:
        raise ArchiveError(f"plugin has more than {MAX_FILES} files")
    return files


def pack_files(files: dict[str, bytes]) -> bytes:
    """Byte-for-byte reproducible: sorted entries, fixed mode/owner/mtime, gzip mtime 0."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size = len(files[name])
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(files[name]))
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0, filename="") as stream:
        stream.write(raw.getvalue())
    return compressed.getvalue()


def pack_directory(root: Path) -> tuple[bytes, str]:
    data = pack_files(collect_files(root))
    return data, sha256_hex(data)


def read_archive(data: bytes, *, max_unpacked_bytes: int = 64 * 1024 * 1024) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not member.name:
                    raise ArchiveError(f"unsafe path in archive: {member.name!r}")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ArchiveError(f"only regular files are allowed: {member.name!r}")
                total += member.size
                if total > max_unpacked_bytes or len(files) >= MAX_FILES:
                    raise ArchiveError("archive exceeds the unpacked size or file limit")
                handle = archive.extractfile(member)
                if handle is None:
                    raise ArchiveError(f"cannot read archive member {member.name!r}")
                files[path.as_posix()] = handle.read()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ArchiveError(f"artifact is not a valid plugin archive: {exc}") from exc
    if MANIFEST_FILE not in files:
        raise ArchiveError(f"archive has no top-level {MANIFEST_FILE}")
    return files


def archive_manifest(files: dict[str, bytes]) -> dict[str, Any]:
    try:
        manifest = json.loads(files[MANIFEST_FILE].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"{MANIFEST_FILE} in archive is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ArchiveError(f"{MANIFEST_FILE} must contain a JSON object")
    return manifest
