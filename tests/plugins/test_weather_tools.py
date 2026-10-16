from __future__ import annotations

import json

import httpx
import pytest

from app.core.context import ToolRemediationError

GEO = {
    "results": [
        {
            "name": "Paris",
            "latitude": 33.66,
            "longitude": -95.55,
            "country": "United States",
            "country_code": "US",
            "admin1": "Texas",
        },
        {
            "name": "Paris",
            "latitude": 48.85,
            "longitude": 2.35,
            "country": "France",
            "country_code": "FR",
            "admin1": "Île-de-France",
        },
    ]
}
FORECAST = {
    "timezone": "Europe/Paris",
    "current_units": {"temperature_2m": "°C", "wind_speed_10m": "km/h", "precipitation": "mm"},
    "current": {"time": "2026-10-16T12:00", "temperature_2m": 14.2, "apparent_temperature": 12.9, "relative_humidity_2m": 70,
                "precipitation": 0.0, "weather_code": 3, "wind_speed_10m": 11.0},
    "daily": {"time": ["2026-10-16", "2026-10-17"], "weather_code": [3, 61], "temperature_2m_max": [16.0, 15.0],
              "temperature_2m_min": [9.0, 8.0], "precipitation_sum": [0.0, 4.2], "precipitation_probability_max": [10, 80],
              "wind_speed_10m_max": [18.0, 25.0]},
}  # fmt: skip


def handler_factory(seen):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(
                200, json=GEO if req.url.params["name"] == "Paris" else {}
            )
        assert req.url.host == "api.open-meteo.com"
        return httpx.Response(200, json=FORECAST)

    return handler


async def test_forecast_resolves_qualified_place_and_maps_codes(make_harness):
    seen: list = []
    h = make_harness("weather_tools.py", handler_factory(seen))
    out = json.loads(
        await h.call("get_weather_forecast", location="Paris, France", days=2)
    )
    assert out["location"]["country"] == "France" and out["location"]["lat"] == 48.85
    assert out["current"]["summary"] == "overcast"
    assert [d["summary"] for d in out["daily"]] == ["overcast", "light rain"]
    forecast_req = seen[-1]
    assert (
        forecast_req.url.params["latitude"] == "48.85"
        and forecast_req.url.params["forecast_days"] == "2"
    )
    assert forecast_req.url.params["temperature_unit"] == "celsius"


async def test_imperial_units_and_default_first_result(make_harness):
    seen: list = []
    h = make_harness("weather_tools.py", handler_factory(seen))
    out = json.loads(
        await h.call("get_weather_forecast", location="Paris", units="imperial")
    )
    assert out["location"]["region"] == "Texas"
    p = seen[-1].url.params
    assert (p["temperature_unit"], p["wind_speed_unit"], p["precipitation_unit"]) == (
        "fahrenheit",
        "mph",
        "inch",
    )


async def test_unknown_place_gives_remediation(make_harness):
    h = make_harness("weather_tools.py", handler_factory([]))
    with pytest.raises(ToolRemediationError, match="Could not find 'Nowhereville'"):
        await h.call("get_weather_forecast", location="Nowhereville")
    with pytest.raises(ToolRemediationError):
        await h.call("get_weather_forecast", location="Paris, Narnia")


async def test_argument_validation(make_harness):
    from pydantic import ValidationError

    h = make_harness("weather_tools.py", handler_factory([]))
    with pytest.raises(ValidationError):
        await h.call("get_weather_forecast", location="Paris", days=11)
    with pytest.raises(ValidationError):
        await h.call("get_weather_forecast", location="Paris", units="kelvin")
