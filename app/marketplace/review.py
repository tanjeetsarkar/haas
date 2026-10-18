"""Automated review pipeline for submitted plugin revisions.

Checks are static only (``ast`` parsing and regexes); plugin code is never imported or run.
The report decides the initial review state; operators finish manual review via the API.
"""

from __future__ import annotations

import ast
import re
from typing import Any
from urllib.parse import urlsplit

from app.marketplace.manifest import (
    ManifestV2,
    ParsedManifest,
    normalize_origin,
    permission_diff,
    permission_summary,
)

# Imports that let tool code escape the sandbox contract outright.
FORBIDDEN_MODULES = {
    "ctypes": "native memory access",
    "cffi": "native code loading",
    "subprocess": "process spawning",
    "pty": "process spawning",
    "multiprocessing": "process spawning",
}
FORBIDDEN_CALLS = {
    "eval": "dynamic code evaluation",
    "exec": "dynamic code execution",
    "compile": "dynamic code compilation",
    "__import__": "dynamic import",
    "os.system": "shell execution",
    "os.popen": "shell execution",
    "os.fork": "process spawning",
    "os.execv": "process replacement",
    "os.execve": "process replacement",
    "os.spawnv": "process spawning",
}
RISKY_MODULES = {
    "pickle": "unsafe deserialization",
    "marshal": "unsafe deserialization",
    "shelve": "unsafe deserialization",
    "importlib": "dynamic import",
}
NETWORK_MODULES = {
    "socket",
    "ssl",
    "urllib",
    "http",
    "requests",
    "httpx",
    "aiohttp",
    "ftplib",
    "smtplib",
}
SECRET_PATTERNS = {
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "openai_style_key": re.compile(r"\bsk-(?:or-v1-|proj-)?[A-Za-z0-9]{32,}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "haas_canary": re.compile(r"\bhaas_canary_[A-Za-z0-9]+\b"),
}
INJECTION_PATTERNS = (
    re.compile(r"ignore (?:all |any )?(?:previous|prior|above) instructions", re.I),
    re.compile(r"disregard (?:the |your )?(?:system|developer) (?:prompt|message)", re.I),
    re.compile(r"reveal (?:the |your )?(?:system prompt|secrets?|api keys?)", re.I),
    re.compile(r"\byou are now (?:in )?(?:developer|dan|jailbreak)", re.I),
    re.compile(r"exfiltrat", re.I),
)
URL_RE = re.compile(r"https?://[^\s'\"<>)]+")
TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".env"}
SECRET_ACCESSORS = {"getenv", "get_secret", "secret"}


def _check(name: str, status: str, findings: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "findings": findings or [], **extra}


def _status(findings: list[dict[str, Any]]) -> str:
    severities = {finding["severity"] for finding in findings}
    return "fail" if "error" in severities else "warn" if "warning" in severities else "pass"


def lint_manifest(parsed: ParsedManifest, *, tier: str, files: dict[str, bytes] | None) -> dict[str, Any]:
    manifest = parsed.manifest
    findings: list[dict[str, Any]] = []
    if parsed.source_version == 1 and tier not in ("platform", "private"):
        findings.append(
            {
                "severity": "error",
                "message": "manifest v1 is accepted only for platform and private tiers",
            }
        )
    for warning in parsed.warnings:
        findings.append({"severity": "warning", "message": warning})
    if not manifest.license:
        findings.append({"severity": "warning", "message": "no license declared"})
    if not manifest.categories:
        findings.append({"severity": "warning", "message": "no categories declared (hurts discovery)"})
    for tool, spec in manifest.tools.items():
        if spec.risk in ("destructive", "financial", "exfiltration-capable") and spec.approval != "required":
            findings.append(
                {
                    "severity": "warning",
                    "message": f"tool '{tool}' has risk '{spec.risk}'; tenant policy will require approval",
                }
            )
    if files is not None:
        for skill in manifest.provides.skills:
            if not any(
                path.endswith(f"{skill}/SKILL.md") for path in files
            ):
                findings.append(
                    {"severity": "error", "message": f"provided skill '{skill}' has no SKILL.md"}
                )
        if manifest.sbom and manifest.sbom not in files:
            findings.append(
                {"severity": "error", "message": f"declared SBOM '{manifest.sbom}' is missing"}
            )
    if manifest.sbom is None and files is not None and any(path.endswith(".py") for path in files):
        findings.append({"severity": "warning", "message": "code plugin without an SBOM"})
    return _check("manifest_lint", _status(findings), findings)


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def scan_python(path: str, source: str, manifest: ManifestV2) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [{"severity": "error", "file": path, "line": exc.lineno, "message": f"syntax error: {exc.msg}"}]
    declared_egress = set(manifest.permissions.egress)
    declared_secrets = set(manifest.permissions.secrets)
    imported: set[str] = set()
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules = [node.module]
            if node.module == "os":
                modules += [f"os.{alias.name}" for alias in node.names]
        for module in modules:
            root = module.split(".")[0]
            imported.add(root)
            line = getattr(node, "lineno", None)
            if root in FORBIDDEN_MODULES:
                findings.append({"severity": "error", "file": path, "line": line, "message": f"forbidden import '{module}' ({FORBIDDEN_MODULES[root]})"})
            elif module in FORBIDDEN_CALLS:
                findings.append({"severity": "error", "file": path, "line": line, "message": f"forbidden import '{module}' ({FORBIDDEN_CALLS[module]})"})
            elif root in RISKY_MODULES:
                findings.append({"severity": "warning", "file": path, "line": line, "message": f"risky import '{module}' ({RISKY_MODULES[root]})"})
            elif root in NETWORK_MODULES and not declared_egress:
                findings.append({"severity": "error", "file": path, "line": line, "message": f"network import '{module}' but no egress is declared"})
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in FORBIDDEN_CALLS:
                findings.append({"severity": "error", "file": path, "line": node.lineno, "message": f"forbidden call '{name}' ({FORBIDDEN_CALLS[name]})"})
            leaf = name.rsplit(".", 1)[-1] if name else None
            if leaf in SECRET_ACCESSORS and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                secret = node.args[0].value
                if secret not in declared_secrets:
                    findings.append({"severity": "error", "file": path, "line": node.lineno, "message": f"reads undeclared secret '{secret}'"})
        if isinstance(node, ast.Subscript) and _dotted(node.value) in ("os.environ", "environ"):
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value not in declared_secrets:
                findings.append({"severity": "error", "file": path, "line": node.lineno, "message": f"reads undeclared secret '{key.value}'"})
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for url in URL_RE.findall(node.value):
                parts = urlsplit(url)
                if parts.scheme != "https" or not parts.hostname:
                    findings.append({"severity": "warning", "file": path, "line": node.lineno, "message": f"non-https URL literal '{url[:120]}'"})
                    continue
                try:
                    origin = normalize_origin(f"https://{parts.netloc}")
                except ValueError:
                    origin = None
                if origin not in declared_egress:
                    findings.append({"severity": "warning", "file": path, "line": node.lineno, "message": f"URL '{url[:120]}' is not a declared egress destination"})
    return findings


def static_scan(manifest: ManifestV2, files: dict[str, bytes] | None) -> dict[str, Any]:
    if files is None:
        return _check(
            "static_scan",
            "skipped",
            [{"severity": "warning", "message": "artifact bytes not supplied; only metadata was reviewed"}],
        )
    findings: list[dict[str, Any]] = []
    for path, content in sorted(files.items()):
        if path.endswith(".py"):
            try:
                findings += scan_python(path, content.decode("utf-8"), manifest)
            except UnicodeDecodeError:
                findings.append({"severity": "error", "file": path, "message": "python file is not UTF-8"})
        elif path.endswith((".so", ".dylib", ".dll", ".exe", ".pyd")):
            findings.append({"severity": "error", "file": path, "message": "native binaries are not allowed"})
    return _check("static_scan", _status(findings), findings)


def secret_scan(raw_manifest: dict[str, Any], files: dict[str, bytes] | None) -> dict[str, Any]:
    import json

    texts = {"plugin.json(submitted)": json.dumps(raw_manifest)}
    for path, content in (files or {}).items():
        if any(path.endswith(suffix) for suffix in TEXT_SUFFIXES) or "." not in path.rsplit("/", 1)[-1]:
            texts[path] = content.decode("utf-8", errors="replace")
    findings = []
    for path, text in sorted(texts.items()):
        for kind, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append({"severity": "error", "file": path, "message": f"embedded credential ({kind})"})
    return _check("secret_scan", _status(findings), findings)


def injection_lint(manifest: ManifestV2, files: dict[str, bytes] | None) -> dict[str, Any]:
    texts = {"manifest.description": manifest.description}
    for tool, spec in manifest.tools.items():
        if spec.description:
            texts[f"tools.{tool}.description"] = spec.description
    for path, content in (files or {}).items():
        if path.endswith(".md"):
            texts[path] = content.decode("utf-8", errors="replace")
    findings = []
    for where, text in sorted(texts.items()):
        for pattern in INJECTION_PATTERNS:
            match = pattern.search(text)
            if match:
                findings.append({"severity": "warning", "file": where, "message": f"possible prompt injection: '{match.group(0)}'"})
    return _check("injection_lint", _status(findings), findings)


def run_review(
    parsed: ParsedManifest,
    raw_manifest: dict[str, Any],
    *,
    tier: str,
    files: dict[str, bytes] | None,
    previous_permissions: dict[str, Any] | None,
    signature_verified: bool,
    provenance_verified: bool | None,
) -> dict[str, Any]:
    manifest = parsed.manifest
    current = permission_summary(manifest)
    diff = permission_diff(previous_permissions, current)
    checks = [
        lint_manifest(parsed, tier=tier, files=files),
        _check("signature", "pass" if signature_verified else "fail"),
        _check(
            "provenance",
            "pass" if provenance_verified else "warn" if provenance_verified is None else "fail",
            [] if provenance_verified else [{"severity": "warning", "message": "no provenance statement supplied"}] if provenance_verified is None else [],
        ),
        _check(
            "permission_diff",
            "warn" if diff["expands"] and previous_permissions is not None else "pass",
            diff=diff,
        ),
        static_scan(manifest, files),
        secret_scan(raw_manifest, files),
        injection_lint(manifest, files),
    ]
    return {
        "passed": not any(check["status"] == "fail" for check in checks),
        "coverage": "full" if files is not None else "metadata-only",
        "expands_permissions": diff["expands"] and previous_permissions is not None,
        "permissions": current,
        "checks": checks,
    }


def decide(tier: str, report: dict[str, Any], *, community_auto_approve: bool) -> tuple[str, str]:
    """Returns ``(review_state, effective_tier)`` after automated review."""
    if not report["passed"]:
        return "rejected", tier
    if tier in ("platform", "private"):
        return "approved", tier
    if report["coverage"] != "full":
        return "approved", "unreviewed"
    if tier == "verified":
        return "submitted", tier
    return ("approved" if community_auto_approve else "submitted"), tier
