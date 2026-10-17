from __future__ import annotations

from types import SimpleNamespace

from haas_cli import cli


def test_dev_up_uses_isolated_loopback_stack_and_mock_provider(monkeypatch, capsys):
    captured = {}

    def fake_run(command, *, cwd, env, check):
        captured.update(command=command, cwd=cwd, env=env, check=check)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setenv("HAAS_DEV_API_PORT", "18123")
    monkeypatch.setenv("HAAS_PROFILE", "prod")
    monkeypatch.setenv("OPENROUTER_API_KEY", "real-secret")

    assert cli.main(["dev", "up"]) == 0

    command = captured["command"]
    assert command[:3] == ["docker", "compose", "-p"]
    assert command[-3:] == ["up", "--build", "-d"]
    assert "tests/e2e/docker-compose.e2e.yml" in command
    assert captured["env"]["HAAS_PROFILE"] == "dev"
    assert captured["env"]["HAAS_API_BIND_ADDRESS"] == "127.0.0.1"
    assert captured["env"]["HAAS_API_PORT"] == "18123"
    assert captured["env"]["OPENROUTER_API_KEY"] == "sk-or-v1-local-dev-mock"
    assert captured["env"]["OPENROUTER_API_KEY"] != "real-secret"
    assert "http://127.0.0.1:18123" in capsys.readouterr().out


def test_dev_seed_passes_rotation_to_container(monkeypatch):
    captured = {}

    def fake_run(command, *, cwd, env, check):
        captured.update(command=command, cwd=cwd, env=env, check=check)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.main(["dev", "seed", "--rotate-key"]) == 0

    assert captured["command"][-5:] == [
        "haas-api",
        "python",
        "-m",
        "app.seed_dev",
        "--rotate-key",
    ]
