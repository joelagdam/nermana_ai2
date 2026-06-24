from __future__ import annotations

from nermana.config import AppConfig
from nermana.http_client import get_json
from nermana.tooling import Tool, ToolRegistry


def register_weather_tools(registry: ToolRegistry, config: AppConfig) -> None:
    def available() -> tuple[bool, str]:
        if not config.weather.enabled:
            return False, "weather disabled"
        return True, "Open-Meteo"

    def current_weather(payload: dict) -> dict:
        lat = payload.get("latitude", config.weather.latitude)
        lon = payload.get("longitude", config.weather.longitude)
        location = str(payload.get("location", config.weather.location_name or "")).strip()
        if (lat is None or lon is None) and location:
            geo = get_json(
                "https://geocoding-api.open-meteo.com/v1/search",
                {"name": location, "count": 1, "language": "en", "format": "json"},
                timeout=config.weather.timeout_seconds,
            )
            if not geo.ok:
                return {"ok": False, "error": f"geocoding unavailable: {geo.error}"}
            results = geo.data.get("results") or []
            if not results:
                return {"ok": False, "error": f"location not found: {location}"}
            lat = results[0]["latitude"]
            lon = results[0]["longitude"]
            location = ", ".join(part for part in [results[0].get("name"), results[0].get("country")] if part)
        if lat is None or lon is None:
            return {"ok": False, "error": "set a weather location or pass latitude/longitude"}
        response = get_json(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,cloud_cover,wind_speed_10m,wind_direction_10m,wind_gusts_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
                "forecast_days": 3,
                "temperature_unit": config.weather.temperature_unit,
                "wind_speed_unit": config.weather.wind_speed_unit,
                "timezone": config.weather.timezone,
            },
            timeout=config.weather.timeout_seconds,
        )
        if not response.ok:
            return {"ok": False, "error": f"weather unavailable: {response.error}"}
        return {"ok": True, "location": location or f"{lat},{lon}", "latitude": lat, "longitude": lon, "weather": response.data}

    registry.register(
        Tool(
            name="current_weather",
            description="Get current weather and short forecast when online.",
            provider="open-meteo",
            input_schema={
                "type": "object",
                "properties": {
                    "location": {"type": "string"},
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                },
            },
            online_required=True,
            risk="read",
            timeout_seconds=config.weather.timeout_seconds,
            handler=current_weather,
            availability=available,
        )
    )
