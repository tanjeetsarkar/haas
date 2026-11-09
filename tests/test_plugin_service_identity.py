import pytest

from app.core.service_invocation import service_request_sha256, validate_service_invocation_claims
from haas_plugin_sdk import ServiceInvocationClaims


def _claims(plugin_id: str) -> ServiceInvocationClaims:
    return {
        "v": 1,
        "iss": "core",
        "aud": "https://service.example:443/v1",
        "tenant_id": "tenant-a",
        "plugin_id": plugin_id,
        "plugin_digest": "sha256:revision",
        "run_id": "run-a",
        "invocation_id": "invoke-a",
        "tool_name": "tool-a",
        "operation": "read",
        "effect_key": "effect-a",
        "policy_revision": "policy-a",
        "catalog_revision": "catalog-a",
        "method": "POST",
        "destination": "https://service.example:443/v1",
        "content_type": "application/json",
        "request_sha256": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
        "iat": 1,
        "exp": 2,
        "jti": "AAECAwQFBgcICQoLDA0ODw",
    }


def test_fake_plugins_share_strict_public_claim_contract() -> None:
    for plugin_id in ("fake-plugin-one", "fake-plugin-two"):
        assert validate_service_invocation_claims(_claims(plugin_id))["plugin_id"] == plugin_id


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("v", True),
        ("iat", True),
        ("exp", "2"),
        ("tenant_id", 7),
        ("jti", "AAAAAAAAAAAAAAAAAAAAAA="),
        ("jti", "A"),
        ("jti", "AAAAAAAAAAAAAAAAAAAAAB"),
        ("request_sha256", "A" * 42),
        ("request_sha256", "A" * 42 + "!"),
        ("destination", "https://user@service.example:443/v1"),
        ("destination", "https://service.example/v1"),
        ("destination", "https://service.example:443/a/../b"),
        ("destination", "https://service.example:443/v1\n"),
        ("destination", "https://127.1:443/v1"),
        ("destination", "https://2130706433:443/v1"),
        ("destination", "https://0x7f000001:443/v1"),
        ("method", "post"),
        ("v", 2),
    ],
)
def test_claim_validation_rejects_bad_types_encodings_and_urls(
    name: str, value: object
) -> None:
    claims = _claims("fake-plugin")
    claims[name] = value  # type: ignore[literal-required]
    with pytest.raises(ValueError):
        validate_service_invocation_claims(claims)


def test_claim_validation_rejects_missing_and_extra_claims() -> None:
    claims = _claims("fake-plugin")
    del claims["tenant_id"]
    with pytest.raises(ValueError):
        validate_service_invocation_claims(claims)
    extra = {**_claims("fake-plugin"), "extra": "value"}
    with pytest.raises(ValueError):
        validate_service_invocation_claims(extra)


def test_binding_bytes_and_digest_are_deterministic_and_sensitive() -> None:
    args = ("POST", "https://service.example:443/v1", "application/json", b'{"a":1}')
    assert service_request_sha256(*args) == "H6hmCo-w1MVsxg37JC5jwaFJLYPml0ZN0WApjQsNWHU"
    for changed in (
        ("GET", *args[1:]),
        (args[0], "https://other.example:443/v1", *args[2:]),
        (args[0], args[1], "text/plain", args[3]),
        (*args[:3], b'{"a":2}'),
    ):
        assert service_request_sha256(*changed) != service_request_sha256(*args)
