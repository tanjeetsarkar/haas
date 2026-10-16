"""Weather forecast tool backed by Open-Meteo (https://open-meteo.com), which needs no API key."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from app.core.context import RunContext, ToolRemediationError

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes used by Open-Meteo
WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle", 56: "freezing drizzle", 57: "dense freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}  # fmt: skip

CURRENT_VARS = "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m"
DAILY_VARS = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max"


class ForecastArgs(BaseModel):
    location: str = Field(
        ...,
        min_length=2,
        description="City or place name, optionally qualified: 'Paris', 'Paris, France', 'Springfield, Illinois'.",
    )
    days: int = Field(
        3, ge=1, le=10, description="Number of forecast days including today."
    )
    units: Literal["metric", "imperial"] = Field(
        "metric", description="metric = °C, km/h, mm; imperial = °F, mph, inch."
    )


def _pick_place(results: list[dict], qualifier: str) -> dict | None:
    if not results:
        return None
    if qualifier:
        q = qualifier.lower()
        for r in results:
            haystack = " ".join(
                str(r.get(k, ""))
                for k in ("country", "country_code", "admin1", "admin2")
            ).lower()
            if q in haystack:
                return r
        return None
    return results[0]


def register(registry) -> None:
    @registry.tool(
        name="get_weather_forecast",
        description=(
            "Returns current conditions and a daily forecast (up to 10 days) for a named place using Open-Meteo. "
            "Read-only; no API key needed."
        ),
        args_schema=ForecastArgs,
    )
    async def get_weather_forecast(ctx: RunContext, args: ForecastArgs) -> str:
        assert ctx.http_client is not None
        name, _, qualifier = (p.strip() for p in args.location.partition(","))
        geo = await ctx.http_client.get(
            GEOCODE_URL,
            params={"name": name, "count": 10, "language": "en", "format": "json"},
            timeout=10.0,
            follow_redirects=False,
        )
        geo.raise_for_status()
        place = _pick_place(geo.json().get("results", []), qualifier)
        if place is None:
            raise ToolRemediationError(
                f"Could not find '{args.location}'. Try the plain city name or 'City, Country' "
                "(e.g. 'Pune, India'); check the spelling."
            )

        imperial = args.units == "imperial"
        resp = await ctx.http_client.get(
            FORECAST_URL,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": CURRENT_VARS,
                "daily": DAILY_VARS,
                "forecast_days": args.days,
                "timezone": "auto",
                "temperature_unit": "fahrenheit" if imperial else "celsius",
                "wind_speed_unit": "mph" if imperial else "kmh",
                "precipitation_unit": "inch" if imperial else "mm",
            },
            timeout=15.0,
            follow_redirects=False,
        )
        resp.raise_for_status()
        data = resp.json()
        cur, daily = data["current"], data["daily"]
        u = data.get("current_units", {})
        days = [
            {
                "date": daily["time"][i],
                "summary": WMO.get(
                    daily["weather_code"][i], f"code {daily['weather_code'][i]}"
                ),
                "max": daily["temperature_2m_max"][i],
                "min": daily["temperature_2m_min"][i],
                "precip_sum": daily["precipitation_sum"][i],
                "precip_probability_pct": daily["precipitation_probability_max"][i],
                "wind_max": daily["wind_speed_10m_max"][i],
            }
            for i in range(len(daily["time"]))
        ]
        return json.dumps(
            {
                "location": {
                    "name": place["name"],
                    "region": place.get("admin1"),
                    "country": place.get("country"),
                    "lat": place["latitude"],
                    "lon": place["longitude"],
                    "timezone": data.get("timezone"),
                },
                "units": {
                    "temperature": u.get("temperature_2m", "°C"),
                    "wind": u.get("wind_speed_10m", "km/h"),
                    "precipitation": u.get("precipitation", "mm"),
                },
                "current": {
                    "time": cur["time"],
                    "summary": WMO.get(
                        cur["weather_code"], f"code {cur['weather_code']}"
                    ),
                    "temperature": cur["temperature_2m"],
                    "feels_like": cur["apparent_temperature"],
                    "humidity_pct": cur["relative_humidity_2m"],
                    "precipitation": cur["precipitation"],
                    "wind": cur["wind_speed_10m"],
                },
                "daily": days,
                "source": "open-meteo.com",
            }
        )
