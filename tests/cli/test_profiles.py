from __future__ import annotations

import json
from argparse import Namespace

import pytest

from haas_cli.chat import settings_from_args
from haas_cli.profiles import EnvironmentProfile, ProfileStore


def test_profile_store_saves_nonsecret_endpoint_metadata(tmp_path):
    path = tmp_path / "config" / "profiles.json"
    store = ProfileStore(path)
    profile = EnvironmentProfile(
        "staging",
        "https://haas-staging.example.test",
        "acme",
        "staging",
    )

    store.save(profile, activate=True)

    assert store.profiles() == [profile]
    assert store.active() == profile
    assert path.stat().st_mode & 0o777 == 0o600
    document = json.loads(path.read_text())
    assert document == {
        "active": "staging",
        "profiles": {
            "staging": {
                "base_url": "https://haas-staging.example.test",
                "environment": "staging",
                "tenant": "acme",
            }
        },
    }
    assert "credential" not in path.read_text()


def test_profile_store_requires_existing_profile_to_activate(tmp_path):
    store = ProfileStore(tmp_path / "profiles.json")

    with pytest.raises(ValueError, match="does not exist"):
        store.activate("missing")


@pytest.mark.parametrize(
    "name, url, tenant, environment",
    [
        ("bad/name", "https://haas.example.test", "acme", "prod"),
        ("prod", "https://user:password@haas.example.test", "acme", "prod"),
        ("prod", "https://haas.example.test/path", "acme", "prod"),
        ("prod", "https://haas.example.test", "", "prod"),
        ("prod", "https://haas.example.test", "acme", "production"),
    ],
)
def test_profile_rejects_invalid_or_ambiguous_configuration(
    name, url, tenant, environment
):
    with pytest.raises(ValueError):
        EnvironmentProfile(name, url, tenant, environment)


def test_profile_store_rejects_malformed_file(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text('{"active": "prod", "profiles": {}}')

    with pytest.raises(ValueError, match="does not exist"):
        ProfileStore(path).active()


def test_chat_settings_load_active_profile_but_not_credentials(tmp_path, monkeypatch):
    path = tmp_path / "profiles.json"
    store = ProfileStore(path)
    store.save(
        EnvironmentProfile("production", "https://haas.example.test", "acme", "prod"),
        activate=True,
    )
    monkeypatch.setenv("HAAS_PROFILES_PATH", str(path))
    monkeypatch.delenv("HAAS_URL", raising=False)
    monkeypatch.delenv("HAAS_TENANT", raising=False)
    monkeypatch.delenv("HAAS_ENVIRONMENT", raising=False)
    monkeypatch.delenv("HAAS_CLIENT_PROFILE", raising=False)
    monkeypatch.setenv("HAAS_API_KEY", "hk_live_memory_only")
    monkeypatch.delenv("HAAS_BEARER_TOKEN", raising=False)

    settings = settings_from_args(Namespace())

    assert settings.profile_name == "production"
    assert settings.base_url == "https://haas.example.test"
    assert settings.tenant == "acme"
    assert settings.environment == "prod"
    assert settings.credential == "hk_live_memory_only"
    assert "hk_live_memory_only" not in repr(settings)
    assert "hk_live_memory_only" not in path.read_text()
