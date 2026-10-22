"""Current weather and today's forecast from Open-Meteo (free, documented, no API key)."""

from __future__ import annotations

import datetime as _dt
import urllib.parse
from dataclasses import dataclass

from . import http

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"

# WMO weather code -> (icon kind, label)
WMO = {
    0: ("clear", "Clear sky"), 1: ("partly", "Mainly clear"), 2: ("partly", "Partly cloudy"),
    3: ("cloudy", "Overcast"), 45: ("fog", "Fog"), 48: ("fog", "Rime fog"),
    51: ("drizzle", "Light drizzle"), 53: ("drizzle", "Drizzle"), 55: ("drizzle", "Heavy drizzle"),
    56: ("drizzle", "Freezing drizzle"), 57: ("drizzle", "Freezing drizzle"),
    61: ("rain", "Light rain"), 63: ("rain", "Rain"), 65: ("rain", "Heavy rain"),
    66: ("rain", "Freezing rain"), 67: ("rain", "Freezing rain"),
    71: ("snow", "Light snow"), 73: ("snow", "Snow"), 75: ("snow", "Heavy snow"),
    77: ("snow", "Snow grains"), 80: ("rain", "Light showers"), 81: ("rain", "Showers"),
    82: ("rain", "Violent showers"), 85: ("snow", "Snow showers"), 86: ("snow", "Heavy snow showers"),
    95: ("thunder", "Thunderstorm"), 96: ("thunder", "Thunderstorm, hail"),
    99: ("thunder", "Thunderstorm, heavy hail"),
}


@dataclass(frozen=True)
class Place:
    name: str
    region: str
    country: str
    lat: float
    lon: float

    @property
    def label(self) -> str:
        return ", ".join(p for p in (self.name, self.region, self.country) if p)


@dataclass(frozen=True)
class Weather:
    temp: float
    feels_like: float
    humidity: int          # %
    wind: float            # in wind_unit
    wind_unit: str         # "km/h" or "mph"
    code: int              # WMO weather code
    is_day: bool
    high: float
    low: float
    rain_chance: int | None  # today's maximum precipitation probability, %
    sunrise: str           # "06:09", location time
    sunset: str
    fahrenheit: bool
    utc_offset: int        # seconds, for the location's local time

    @property
    def kind(self) -> str:
        return WMO.get(self.code, ("cloudy", ""))[0]

    @property
    def label(self) -> str:
        return WMO.get(self.code, ("", f"Weather code {self.code}"))[1]

    def local_now(self) -> _dt.datetime:
        return _dt.datetime.now(_dt.timezone(_dt.timedelta(seconds=self.utc_offset)))


def parse_places(data: dict) -> list[Place]:
    return [Place(r["name"], r.get("admin1", ""), r.get("country", ""), r["latitude"], r["longitude"])
            for r in data.get("results", [])]


def parse_forecast(data: dict, fahrenheit: bool = False) -> Weather:
    cur, day = data["current"], data["daily"]
    rain = day.get("precipitation_probability_max", [None])[0]
    return Weather(
        temp=cur["temperature_2m"], feels_like=cur["apparent_temperature"],
        humidity=cur["relative_humidity_2m"], wind=cur["wind_speed_10m"],
        wind_unit=data["current_units"]["wind_speed_10m"].replace("mp/h", "mph"),
        code=cur["weather_code"],
        is_day=bool(cur["is_day"]), high=day["temperature_2m_max"][0],
        low=day["temperature_2m_min"][0], rain_chance=rain,
        sunrise=day["sunrise"][0][-5:], sunset=day["sunset"][0][-5:],
        fahrenheit=fahrenheit, utc_offset=data["utc_offset_seconds"],
    )


async def search(name: str, count: int = 6) -> list[Place]:
    query = urllib.parse.urlencode({"name": name, "count": count, "language": "en"})
    return parse_places(await http.get_json(f"{GEOCODE}?{query}"))


async def current(lat: float, lon: float, fahrenheit: bool = False) -> Weather:
    query = {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
                   "is_day,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "sunrise,sunset",
    }
    if fahrenheit:
        query.update(temperature_unit="fahrenheit", wind_speed_unit="mph")
    data = await http.get_json(f"{FORECAST}?{urllib.parse.urlencode(query)}")
    return parse_forecast(data, fahrenheit)
