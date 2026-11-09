"""Phase-shaped input is unavailable until every execution path is enforced."""

import pytest

from test_run_restrictions_api import (
    ROUTES,
    admission as admission,
    assert_no_admission_effects,
    client,
)


PHASE_PAYLOADS = (
    None,
    {},
    {"version": 1, "mode": "bounded_acquisition_finalization", "reserve_basis_points": 2000},
    {"version": 88, "mode": "future", "unknown": "private-value"},
    [],
    False,
    "private-value",
)


@pytest.mark.parametrize("route,base", ROUTES)
@pytest.mark.parametrize("phase", PHASE_PAYLOADS)
@pytest.mark.parametrize("idempotency", [None, "prior-request"])
async def test_present_phase_config_rejected_before_any_admission_effect(
    admission, route, base, phase, idempotency,
):
    headers = {"Idempotency-Key": idempotency} if idempotency else {}
    async with client() as http:
        response = await http.post(
            route, json={**base, "phase_config": phase}, headers=headers,
        )
    assert response.status_code == 422, response.text
    assert "private-value" not in response.text
    if route == "/v1/runs":
        assert response.headers["content-type"].startswith("application/problem+json")
    else:
        assert response.json()["error"]["code"] == "phase.unavailable"
        assert response.json()["error"]["param"] == "phase_config"
    assert_no_admission_effects(admission)


@pytest.mark.parametrize("route,base", ROUTES[1:])
@pytest.mark.parametrize("phase", PHASE_PAYLOADS)
async def test_haas_phase_config_rejected_before_facade_mapping_and_chain_lookup(
    admission, route, base, phase,
):
    async with client() as http:
        response = await http.post(
            route, json={**base, "haas": {"phase_config": phase}},
            headers={"Idempotency-Key": "prior-request"},
        )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "phase.unavailable"
    assert response.json()["error"]["param"] == "haas.phase_config"
    assert "private-value" not in response.text
    assert_no_admission_effects(admission)


@pytest.mark.parametrize("route,base", ROUTES[1:])
async def test_phase_config_rejected_before_agent_resolution(admission, route, base):
    async with client() as http:
        response = await http.post(
            route,
            json={**base, "model": "agent:unresolved", "phase_config": {}},
            headers={"Idempotency-Key": "prior-request"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "phase.unavailable"
    assert_no_admission_effects(admission)
