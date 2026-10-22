"""Open-Meteo parsing, weather cards and the live weather loop."""

import asyncio
import datetime as dt
import io

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from ledpanel import weather
from ledpanel.render import WEATHER_PAGES, weather_card, weather_icon

# Trimmed real response for Bangkok (timezone=auto, forecast_days=1)
FORECAST = {
    "utc_offset_seconds": 25200,
    "current_units": {"wind_speed_10m": "km/h"},
    "current": {"temperature_2m": 28.6, "apparent_temperature": 33.1, "relative_humidity_2m": 79,
                "weather_code": 0, "is_day": 0, "wind_speed_10m": 6.8},
    "daily": {"temperature_2m_max": [33.4], "temperature_2m_min": [25.9],
              "precipitation_probability_max": [40],
              "sunrise": ["2026-10-22T06:09"], "sunset": ["2026-10-22T17:56"]},
}
PLACES = {"results": [{"name": "Bangkok", "admin1": "Bangkok", "country": "Thailand",
                       "latitude": 13.75, "longitude": 100.5}, {"name": "Paris", "latitude": 48.85,
                                                                "longitude": 2.35, "country": "France"}]}


def test_parse_forecast():
    w = weather.parse_forecast(FORECAST)
    assert (w.temp, w.feels_like, w.humidity, w.wind, w.wind_unit) == (28.6, 33.1, 79, 6.8, "km/h")
    assert (w.high, w.low, w.rain_chance, w.sunrise, w.sunset) == (33.4, 25.9, 40, "06:09", "17:56")
    assert (w.kind, w.label, w.is_day) == ("clear", "Clear sky", False)
    assert w.local_now().utcoffset() == dt.timedelta(hours=7)


def test_mph_unit_is_normalised_and_unknown_codes_still_render():
    data = {**FORECAST, "current_units": {"wind_speed_10m": "mp/h"},
            "current": {**FORECAST["current"], "weather_code": 42}}
    w = weather.parse_forecast(data, fahrenheit=True)
    assert w.wind_unit == "mph" and w.kind == "cloudy" and w.label == "Weather code 42"


def test_parse_places_skips_missing_region():
    places = weather.parse_places(PLACES)
    assert [p.label for p in places] == ["Bangkok, Bangkok, Thailand", "Paris, France"]
    assert weather.parse_places({}) == []


@pytest.mark.parametrize("kind", sorted({k for k, _ in weather.WMO.values()}))
@pytest.mark.parametrize("is_day", [True, False])
def test_every_icon_draws_something(kind, is_day):
    icon = weather_icon(kind, is_day, 18)
    assert icon.size == (18, 18) and icon.getbbox() is not None


def test_icons_differ_by_kind_and_day():
    kinds = sorted({k for k, _ in weather.WMO.values()})
    by_day = {weather_icon(k, True, 18).tobytes() for k in kinds}
    assert len(by_day) == len(kinds)                            # one distinct icon per kind
    for k in ("clear", "partly"):                               # sun by day, moon at night
        assert weather_icon(k, True, 18).tobytes() != weather_icon(k, False, 18).tobytes()


@pytest.mark.parametrize("page", WEATHER_PAGES)
@pytest.mark.parametrize("size", [(64, 20), (32, 32), (96, 16)])
def test_every_page_fills_the_panel(page, size):
    w = weather.parse_forecast(FORECAST)
    card = weather_card(w, page, size, now=dt.datetime(2026, 10, 22, 20, 41))
    assert card.size == size and card.getbbox() is not None


def test_clock_page_follows_the_time_and_12_hour_setting():
    w = weather.parse_forecast(FORECAST)
    at = lambda h, m, h24=True: weather_card(w, "time", (64, 20), now=dt.datetime(2026, 1, 1, h, m),
                                             hour_24=h24).tobytes()
    assert at(20, 41) != at(20, 42)
    assert at(20, 41) != at(20, 41, h24=False)          # 20:41 vs 8:41
    assert at(8, 41, h24=False) == at(20, 41, h24=False)


class Panel:
    def __init__(self):
        from ledpanel.protocol import DeviceInfo
        self.is_connected, self.address, self.hw = True, "AA:BB", None
        self.info = DeviceInfo(0x85, 5, 64, 20, 4096, 16, False, b"")
        self.sent = []

    async def gif(self, data, slot=None):
        img = Image.open(io.BytesIO(data))
        self.sent.append((img.size, img.n_frames))

    async def delete_all(self):
        self.sent.append("delete_all")

    async def clear(self):
        self.sent.append("clear")

    async def disconnect(self):
        pass


def _fake(monkeypatch, temps):
    from ledpanel.web import server
    temps = iter(temps)
    fetches = []

    async def fake_current(lat, lon, fahrenheit=False):
        fetches.append((lat, lon, fahrenheit))
        data = {**FORECAST, "current": {**FORECAST["current"], "temperature_2m": next(temps)}}
        return weather.parse_forecast(data, fahrenheit)

    async def fake_search(name, count=6):
        return weather.parse_places(PLACES)

    monkeypatch.setattr(weather, "current", fake_current)
    monkeypatch.setattr(weather, "search", fake_search)
    monkeypatch.setattr(weather.Weather, "local_now", lambda self: dt.datetime(2026, 10, 22, 20, 41, 0))
    monkeypatch.setattr(server, "WEATHER_EVERY", 0.02)
    return server, fetches


def test_live_weather_loops_one_gif_and_reuploads_when_the_forecast_changes(monkeypatch):
    server, fetches = _fake(monkeypatch, [28.6, 28.6] + [30.2] * 1000)   # preview, show, refreshes

    async def go():
        app = server.make_app()
        panel = Panel()
        app[server.STATE].led = panel
        async with TestClient(TestServer(app)) as c:
            found = await (await c.post("/api/weather/search", json={"q": "bangkok"})).json()
            assert found[0] == {"label": "Bangkok, Bangkok, Thailand", "name": "Bangkok",
                                "lat": 13.75, "lon": 100.5}
            r = await c.post("/api/weather/preview", json={"lat": 13.75, "lon": 100.5,
                                                           "pages": ["now", "time", "details"]})
            assert r.status == 200 and Image.open(io.BytesIO(await r.read())).n_frames == 3
            assert panel.sent == []                           # preview never touches the panel

            r = await c.post("/api/weather/show", json={"lat": 13.75, "lon": 100.5, "name": "Bangkok",
                                                        "unit": "C", "pages": ["now", "time"]})
            assert (await r.json())["temp"] == 28.6
            for _ in range(200):
                st = await (await c.get("/api/status")).json()
                if len(panel.sent) >= 3 and st["following"]["label"].startswith("Bangkok 30°"):
                    break
                await asyncio.sleep(0.01)
            # memory emptied, then the 28.6° GIF, then the 30.2° one: both pages in one loop
            assert panel.sent == ["delete_all", ((64, 20), 2), ((64, 20), 2)]
            assert st["following"] == {"kind": "weather", "label": "Bangkok 30° · Clear sky"}
            await asyncio.sleep(0.1)
            assert len(fetches) >= 5 and len(panel.sent) == 3  # kept refreshing, no re-upload

            await c.post("/api/live/stop")
            await asyncio.sleep(0.05)
            assert len(panel.sent) == 3                       # loop really stopped
            assert (await (await c.get("/api/status")).json())["following"] is None
    asyncio.run(go())


def test_clock_change_reuploads(monkeypatch):
    server, _ = _fake(monkeypatch, [28.6] * 1000)
    minute = iter(range(41, 60))
    monkeypatch.setattr(weather.Weather, "local_now",
                        lambda self: dt.datetime(2026, 10, 22, 20, next(minute), 59, 999000))

    async def go():
        app = server.make_app()
        panel = Panel()
        app[server.STATE].led = panel
        async with TestClient(TestServer(app)) as c:
            await c.post("/api/weather/show", json={"lat": 1, "lon": 2, "pages": ["time"]})
            for _ in range(200):
                if len(panel.sent) >= 4:
                    break
                await asyncio.sleep(0.01)
            await c.post("/api/live/stop")
        assert panel.sent[:4] == ["delete_all"] + [((64, 20), 1)] * 3   # a new GIF each minute
    asyncio.run(go())
