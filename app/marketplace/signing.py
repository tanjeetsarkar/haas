"""Ed25519 publisher signatures and SLSA/in-toto style provenance statements.

A publisher signs a canonical JSON statement binding the artifact sha256 digest, the
canonical manifest digest and the provenance digest. The registry verifies it at
publish, again at install, and the bundle can be re-verified offline (``haas plugin verify``).
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.marketplace.canonical import canonical_json, digest_json, is_sha256, sha256_hex

SIGNATURE_TYPE = "haas.plugin.signature/v1"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PROVENANCE_PREDICATE = "https://haas.dev/plugin-provenance/v1"
ALGORITHM = "ed25519"


class SignatureError(ValueError):
    """Signature, key or provenance material is malformed or does not verify."""


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(value: str, *, what: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, AttributeError) as exc:
        raise SignatureError(f"{what} is not valid base64") from exc


def public_key_bytes(value: str) -> bytes:
    raw = b64decode(value, what="public key")
    if len(raw) != 32:
        raise SignatureError("Ed25519 public key must be 32 raw bytes (base64)")
    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise SignatureError("invalid Ed25519 public key") from exc
    return raw


def key_id_for(public_key: bytes) -> str:
    return sha256_hex(public_key)[:32]


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_key_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def load_private_key_pem(data: bytes) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SignatureError("signing key must be an Ed25519 private key")
    return key


def raw_public_key(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def signing_payload(
    artifact_digest: str, manifest_digest: str, provenance_digest: str | None
) -> bytes:
    if not is_sha256(artifact_digest) or not is_sha256(manifest_digest):
        raise SignatureError("artifact and manifest digests must be sha256 hex")
    if provenance_digest is not None and not is_sha256(provenance_digest):
        raise SignatureError("provenance digest must be sha256 hex")
    return canonical_json(
        {
            "_type": SIGNATURE_TYPE,
            "artifact_digest": artifact_digest,
            "manifest_digest": manifest_digest,
            "provenance_digest": provenance_digest,
        }
    )


def sign(
    key: Ed25519PrivateKey,
    *,
    artifact_digest: str,
    manifest: dict[str, Any],
    provenance: dict[str, Any] | None,
) -> dict[str, str]:
    payload = signing_payload(
        artifact_digest,
        digest_json(manifest),
        digest_json(provenance) if provenance is not None else None,
    )
    return {
        "alg": ALGORITHM,
        "key_id": key_id_for(raw_public_key(key)),
        "sig": b64encode(key.sign(payload)),
    }


def verify(
    public_key: bytes,
    signature: dict[str, Any],
    *,
    artifact_digest: str,
    manifest: dict[str, Any],
    provenance: dict[str, Any] | None,
) -> None:
    if not isinstance(signature, dict) or signature.get("alg") != ALGORITHM:
        raise SignatureError("signature must be an ed25519 signature object")
    if signature.get("key_id") != key_id_for(public_key):
        raise SignatureError("signature key_id does not match the publisher key")
    raw = b64decode(str(signature.get("sig", "")), what="signature")
    payload = signing_payload(
        artifact_digest,
        digest_json(manifest),
        digest_json(provenance) if provenance is not None else None,
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(raw, payload)
    except (InvalidSignature, ValueError) as exc:
        raise SignatureError("signature does not verify") from exc


def build_provenance(
    *,
    plugin_id: str,
    version: str,
    artifact_digest: str,
    builder_id: str,
    started_on: str,
    source_uri: str | None = None,
    source_commit: str | None = None,
) -> dict[str, Any]:
    materials = []
    if source_uri:
        material: dict[str, Any] = {"uri": source_uri}
        if source_commit:
            material["digest"] = {"gitCommit": source_commit}
        materials.append(material)
    return {
        "_type": STATEMENT_TYPE,
        "subject": [
            {"name": f"{plugin_id}@{version}", "digest": {"sha256": artifact_digest}}
        ],
        "predicateType": PROVENANCE_PREDICATE,
        "predicate": {
            "builder": {"id": builder_id},
            "buildType": "haas-plugin-pack/1",
            "materials": materials,
            "metadata": {"buildStartedOn": started_on},
        },
    }


def validate_provenance(
    provenance: Any, *, plugin_id: str, version: str, artifact_digest: str
) -> None:
    if not isinstance(provenance, dict):
        raise SignatureError("provenance must be a JSON object")
    if provenance.get("_type") != STATEMENT_TYPE:
        raise SignatureError(f"provenance _type must be {STATEMENT_TYPE}")
    if provenance.get("predicateType") != PROVENANCE_PREDICATE:
        raise SignatureError(f"provenance predicateType must be {PROVENANCE_PREDICATE}")
    subjects = provenance.get("subject")
    if not isinstance(subjects, list) or not any(
        isinstance(item, dict)
        and isinstance(item.get("digest"), dict)
        and item["digest"].get("sha256") == artifact_digest
        and item.get("name") == f"{plugin_id}@{version}"
        for item in subjects
    ):
        raise SignatureError("provenance subject does not match the artifact digest")
    predicate = provenance.get("predicate")
    if not isinstance(predicate, dict) or not isinstance(
        (predicate.get("builder") or {}).get("id"), str
    ):
        raise SignatureError("provenance predicate.builder.id is required")
