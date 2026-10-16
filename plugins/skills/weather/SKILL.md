---
name: weather
description: "Gets current weather and multi-day forecasts for any named place (Open-Meteo, no API key). Use when the user asks about the weather, temperature, rain, wind, whether to bring an umbrella, or conditions for travel or outdoor plans."
license: Apache-2.0
compatibility: "Needs outbound HTTPS to open-meteo.com. No secrets."
metadata:
  version: "1.0.0"
  required_tools: "get_weather_forecast"
  allowed_secrets: ""
---

# Weather

1. Identify the place. If none was given and none is known from the conversation, ask for it.
   Pass it as the user wrote it, e.g. `Pune, India`; add the country when the name is ambiguous.
2. Call `get_weather_forecast` once. Choose `days` from the question (today/now → 1, weekend → up to 3–5, trip → as many days as needed, max 10) and `units` from the user's locale or request
   (`imperial` only for °F/mph requests or US places).
3. If the place is not found, retry once with a corrected spelling or `City, Country`; then ask the user.
4. Answer concisely: current conditions first (temperature, feels-like, wind), then one line per day
   (summary, min–max, rain chance). Always state units and the location as resolved (name, region, country).
5. Give practical advice only when it follows from the data (umbrella if precipitation probability ≥ 50 %,
   heat/cold extremes, strong wind ≥ 40 km/h).

## Rules

- Report only what the tool returned; do not invent hourly detail or conditions beyond the forecast range.
- Forecasts beyond ~5 days are low confidence; say so.
- Mention the source (Open-Meteo) when asked where the data comes from.
