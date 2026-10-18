"""Static and rendered invariants for the deploy/helm/haas chart.

Rendering uses the `alpine/helm` container; those tests skip when Docker (or the image)
is unavailable. Static checks of values files always run.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "haas"
HELM_IMAGE = "alpine/helm:3.16.2"
WORKLOADS = {"api", "orchestrator", "tool-worker", "maintenance"}
FORBIDDEN_TOOL_ENV = {
    "POSTGRES_DSN",
    "OPENROUTER_API_KEY",
    "HAAS_ENVELOPE_SIGNING_KEY",
    "HAAS_SECRET_ENCRYPTION_KEY",
}


def _values(name: str = "values.yaml") -> dict[str, Any]:
    return yaml.safe_load((CHART / name).read_text())


def test_chart_files_present() -> None:
    for rel in (
        "Chart.yaml",
        "values.yaml",
        "values-prod.yaml",
        "values-staging.yaml",
        "templates/networkpolicies.yaml",
        "templates/migrations-job.yaml",
        "templates/keda.yaml",
        "templates/pdb.yaml",
    ):
        assert (CHART / rel).is_file(), rel
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert chart["apiVersion"] == "v2"
    assert chart["name"] == "haas"


def test_default_values_are_restricted() -> None:
    values = _values()
    pod = values["podSecurityContext"]
    assert pod["runAsNonRoot"] is True
    assert pod["seccompProfile"]["type"] == "RuntimeDefault"
    container = values["containerSecurityContext"]
    assert container["readOnlyRootFilesystem"] is True
    assert container["allowPrivilegeEscalation"] is False
    assert container["capabilities"]["drop"] == ["ALL"]
    assert values["serviceAccount"]["automountToken"] is False
    assert values["networkPolicy"]["enabled"] is True
    assert "169.254.0.0/16" in values["networkPolicy"]["publicHttps"]["except"]


def test_tool_worker_values_least_privilege() -> None:
    values = _values()
    assert set(values["toolWorker"]["secretEnv"]).isdisjoint(FORBIDDEN_TOOL_ENV)
    assert values["toolWorker"]["env"]["HAAS_WORKER_ROLE"] == "tools"
    # The schema-owner DSN is only for the migration hook.
    migration_secret = values["migrations"]["secretEnv"]["POSTGRES_DSN"]["name"]
    for component in ("api", "orchestrator", "toolWorker", "maintenance"):
        refs = {ref["name"] for ref in values[component]["secretEnv"].values()}
        assert migration_secret not in refs, component


def test_terraform_secret_names_match_chart() -> None:
    main_tf = (ROOT / "deploy" / "terraform" / "modules" / "haas-platform" / "main.tf").read_text()
    values = _values()
    for component in ("redis", "api", "orchestrator", "toolWorker", "maintenance", "migrations"):
        for ref in values[component]["secretEnv"].values():
            assert f'"{ref["name"]}"' in main_tf, ref
            assert f'"{ref["key"]}"' in main_tf, ref


@cache
def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "image", "inspect", HELM_IMAGE],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if probe.returncode != 0:
            pulled = subprocess.run(
                ["docker", "pull", "-q", HELM_IMAGE], capture_output=True, timeout=300, check=False
            )
            return pulled.returncode == 0
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _helm(*args: str) -> str:
    if not _docker_ready():
        pytest.skip("docker / alpine/helm image unavailable")
    import os

    cmd = [
        "docker", "run", "--rm",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{ROOT}:/w:ro", "-w", "/w",
        HELM_IMAGE, *args,
    ]  # fmt: skip
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


@cache
def _render(*values_files: str) -> tuple[dict[str, Any], ...]:
    args = ["template", "haas", "deploy/helm/haas", "-n", "haas"]
    for name in values_files:
        args += ["-f", f"deploy/helm/haas/{name}"]
    docs = [doc for doc in yaml.safe_load_all(_helm(*args)) if doc]
    return tuple(docs)


def _by_kind(docs: tuple[dict[str, Any], ...], kind: str) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc["kind"] == kind]


def _component(doc: dict[str, Any]) -> str:
    return doc["metadata"]["labels"]["app.kubernetes.io/component"]


def _pod_specs(docs: tuple[dict[str, Any], ...]) -> dict[str, dict[str, Any]]:
    specs = {}
    for doc in docs:
        if doc["kind"] in {"Deployment", "Job"}:
            specs[_component(doc)] = doc["spec"]["template"]["spec"]
    return specs


def _env_names(container: dict[str, Any]) -> set[str]:
    return {item["name"] for item in container.get("env", [])}


@pytest.mark.parametrize("values_files", [(), ("values-prod.yaml",), ("values-staging.yaml",)])
def test_lint(values_files: tuple[str, ...]) -> None:
    args = ["lint", "deploy/helm/haas", "--strict"]
    for name in values_files:
        args += ["-f", f"deploy/helm/haas/{name}"]
    assert "0 chart(s) failed" in _helm(*args)


@pytest.mark.parametrize("values_files", [(), ("values-prod.yaml",)])
def test_rendered_pods_meet_restricted_profile(values_files: tuple[str, ...]) -> None:
    docs = _render(*values_files)
    specs = _pod_specs(docs)
    assert set(specs) == WORKLOADS | {"migrations"}
    assert not _by_kind(docs, "Secret"), "chart must never render credentials"
    for component, spec in specs.items():
        assert spec["automountServiceAccountToken"] is False, component
        assert spec["securityContext"]["runAsNonRoot"] is True, component
        assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault", component
        for key in ("hostNetwork", "hostPID", "hostIPC"):
            assert not spec.get(key), (component, key)
        for volume in spec.get("volumes", []):
            assert set(volume) - {"name"} <= {"emptyDir", "persistentVolumeClaim"}, volume
        for container in spec["containers"]:
            sc = container["securityContext"]
            assert sc["runAsNonRoot"] is True
            assert sc["readOnlyRootFilesystem"] is True
            assert sc["allowPrivilegeEscalation"] is False
            assert sc["capabilities"]["drop"] == ["ALL"]
            assert "add" not in sc["capabilities"]
            assert sc["seccompProfile"]["type"] == "RuntimeDefault"
            assert container["resources"]["requests"] and container["resources"]["limits"]


def test_probes_and_role_separation() -> None:
    specs = _pod_specs(_render("values-prod.yaml"))
    api = specs["api"]["containers"][0]
    assert api["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert api["livenessProbe"]["httpGet"]["path"] == "/livez"
    for worker in ("orchestrator", "tool-worker"):
        assert "inspect ping" in " ".join(specs[worker]["containers"][0]["livenessProbe"]["exec"]["command"])

    tool = specs["tool-worker"]
    assert _env_names(tool["containers"][0]).isdisjoint(FORBIDDEN_TOOL_ENV)
    assert tool["runtimeClassName"] == "gvisor"
    orchestrator_env = _env_names(specs["orchestrator"]["containers"][0])
    assert {"HAAS_ENVELOPE_SIGNING_KEY", "OPENROUTER_API_KEY", "POSTGRES_DSN"} <= orchestrator_env
    assert "OPENROUTER_API_KEY" not in _env_names(api)

    migration_secret = {
        e["valueFrom"]["secretKeyRef"]["name"]
        for e in specs["migrations"]["containers"][0]["env"]
        if e["name"] == "POSTGRES_DSN"
    }
    for component in WORKLOADS:
        refs = {
            e["valueFrom"]["secretKeyRef"]["name"]
            for e in specs[component]["containers"][0]["env"]
            if "valueFrom" in e
        }
        assert refs.isdisjoint(migration_secret), component


def test_migration_job_is_pre_upgrade_hook() -> None:
    docs = _render()
    (job,) = _by_kind(docs, "Job")
    hooks = job["metadata"]["annotations"]["helm.sh/hook"]
    assert "pre-install" in hooks and "pre-upgrade" in hooks
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_network_policies_default_deny_and_explicit_allows() -> None:
    docs = _render("values-prod.yaml")
    policies = {p["metadata"]["name"]: p for p in _by_kind(docs, "NetworkPolicy")}
    deny = policies["haas-default-deny"]["spec"]
    assert deny["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in deny and "egress" not in deny
    assert "app.kubernetes.io/component" not in deny["podSelector"]["matchLabels"]

    for component in WORKLOADS | {"migrations"}:
        assert f"haas-{component}" in policies, component
    for name, policy in policies.items():
        if name == "haas-api":
            continue
        assert "ingress" not in policy["spec"], f"{name} must not accept ingress"
    assert policies["haas-api"]["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8000}]

    for policy in policies.values():
        for rule in policy["spec"].get("egress", []):
            for peer in rule["to"]:
                block = peer.get("ipBlock")
                if block and block["cidr"] == "0.0.0.0/0":
                    assert "169.254.0.0/16" in block["except"]
                    assert "10.0.0.0/8" in block["except"]

    tool_targets = [peer for rule in policies["haas-tool-worker"]["spec"]["egress"] for peer in rule["to"]]
    assert {"ipBlock": {"cidr": "10.20.0.0/16"}} not in tool_targets, "tool workers must not reach PostgreSQL"


def test_disruption_budgets_and_autoscaling() -> None:
    docs = _render("values-prod.yaml")
    assert {_component(p) for p in _by_kind(docs, "PodDisruptionBudget")} == {
        "api",
        "orchestrator",
        "tool-worker",
    }
    assert {_component(h) for h in _by_kind(docs, "HorizontalPodAutoscaler")} == {"api"}
    scaled = {_component(s): s for s in _by_kind(docs, "ScaledObject")}
    assert set(scaled) == {"orchestrator", "tool-worker"}
    lists = {t["metadata"]["listName"] for t in scaled["tool-worker"]["spec"]["triggers"]}
    assert lists == {"q_tools_fast", "q_tools_sandbox"}
    assert all(t["type"] == "redis" for t in scaled["orchestrator"]["spec"]["triggers"])
    for deployment in _by_kind(docs, "Deployment"):
        if _component(deployment) in scaled:
            assert "replicas" not in deployment["spec"], "KEDA owns replica count"

    default_docs = _render()
    assert not _by_kind(default_docs, "ScaledObject")
    assert {_component(h) for h in _by_kind(default_docs, "HorizontalPodAutoscaler")} == {
        "api",
        "orchestrator",
        "tool-worker",
    }
