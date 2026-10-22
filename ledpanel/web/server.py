"""Local web interface: python -m ledpanel.web [--port 8765]."""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import functools
import io
import json
import logging
import mimetypes
import os
from pathlib import Path

from aiohttp import web
from bleak.exc import BleakError
from PIL import Image

from .. import protocol as p
from .. import sports, weather
from ..assets import Assets
from ..client import LedPanel, LedPanelError, discover
from ..render import (WEATHER_PAGES, animation_gif, encode_gif, score_card, scrolling_text_gif,
                      text_strip, weather_card)
from ..text import Span, TextEffect

log = logging.getLogger(__name__)


def default_panel_file() -> Path:
    """Where the last connected panel is remembered, so a restart reconnects to it."""
    return Path(os.environ.get("LEDPANEL_PANEL_FILE") or Path.home() / ".ledpanel" / "panel.json")
STATIC = Path(__file__).with_name("static")


class State:
    def __init__(self, assets: Assets, panel_file: Path | None = None):
        self.led: LedPanel | None = None
        self.assets = assets
        self.panel_file = panel_file
        self.lock = asyncio.Lock()
        self.follow: asyncio.Task | None = None
        self.follow_info: dict | None = None

    def stop_follow(self) -> None:
        """Stop the live display (sports score or weather); anything else sent to the
        screen replaces it."""
        if self.follow:
            self.follow.cancel()
        self.follow, self.follow_info = None, None

    def remember(self, address: str | None, pin: str | None = None) -> None:
        if not self.panel_file:
            return
        if address is None:
            self.panel_file.unlink(missing_ok=True)
            return
        self.panel_file.parent.mkdir(parents=True, exist_ok=True)
        self.panel_file.write_text(json.dumps({"address": address, "pin": pin}))
        self.panel_file.chmod(0o600)   # it may hold the panel's PIN

    def remembered(self) -> dict | None:
        try:
            return json.loads(self.panel_file.read_text()) if self.panel_file else None
        except (OSError, ValueError):
            return None

    async def relink(self) -> bool:
        """Reconnect a panel whose Bluetooth link dropped, or the remembered panel when
        none is connected yet (e.g. after a restart); True if it connected."""
        if self.led is None:
            saved = self.remembered()
            if not saved:
                return False
            log.info("connecting to the remembered panel %s", saved["address"])
            led = LedPanel(saved["address"], pin=saved.get("pin") or None)
            await led.connect()
            self.led = led
            return True
        if self.led.is_connected:
            return False
        log.warning("panel link lost, reconnecting")
        await self.led.connect()
        return True

    def need(self) -> LedPanel:
        if self.led is None:
            raise web.HTTPConflict(text="Not connected to a panel: click Scan, pick it, then Connect")
        return self.led


def _rgb(value) -> tuple[int, int, int]:
    if isinstance(value, str):
        v = value.lstrip("#")
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    return tuple(value)


def _segments(body: dict, assets: Assets, emoji_size: int) -> list:
    """[{text, color} | {emoji: name}] -> Span/Emoji list."""
    out = []
    for seg in body.get("segments", []):
        if "emoji" in seg:
            try:
                out.append(assets.emoji(str(seg["emoji"]), emoji_size))
            except KeyError:
                raise web.HTTPBadRequest(text=f"no emoji named {seg['emoji']!r} in {assets.root}")
        elif seg.get("text"):
            out.append(Span(seg["text"], _rgb(seg.get("color", "#ffffff"))))
    if not out:
        raise web.HTTPBadRequest(text="empty text")
    return out


def _slot(value) -> int | None:
    """Empty means show now (replacing what plays); a number stores into that slot."""
    return int(value) if value else None


def _size(led: LedPanel | None) -> tuple[int, int]:
    return (led.info.width, led.info.height) if led and led.info else (64, 20)


LINK_ERRORS = (BleakError, LedPanelError, asyncio.TimeoutError)


def api(handler, *, relink: bool = True, screen: bool = False, ble: bool = True):
    """JSON in/out, one BLE operation at a time, errors as {error}.

    With relink, a dropped Bluetooth link is reconnected before the request, and a
    request that fails because the link dropped midway is retried once. screen marks
    commands that replace what the panel shows, which ends live score updates. ble=False
    is for handlers that never touch the panel: no lock, so they don't wait for uploads."""
    async def run(request: web.Request, state: State):
        if screen:
            state.stop_follow()
        if relink:
            await state.relink()
        try:
            return await handler(request, state)
        except LINK_ERRORS:
            if not (relink and await state.relink()):
                raise
            return await handler(request, state)

    async def wrapped(request: web.Request) -> web.StreamResponse:
        state: State = request.app[STATE]
        try:
            if ble:
                async with state.lock:
                    result = await run(request, state)
            else:
                result = await handler(request, state)
        except web.HTTPException:
            raise
        except Exception as e:  # always answer with a message the page can show
            log.exception("request failed")
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)
        if isinstance(result, web.StreamResponse):
            return result
        return web.json_response(result if result is not None else {"ok": True})
    return wrapped


screen_api = functools.partial(api, screen=True)


async def _json(request: web.Request) -> dict:
    return await request.json() if request.body_exists else {}  # body is cached, so retries can re-read it


# --- connection -------------------------------------------------------------

@functools.partial(api, relink=False)
async def scan(request, state):
    return [{"address": d.address, "name": d.name} for d in await discover()]


@functools.partial(api, relink=False)
async def connect(request, state):
    body = await _json(request)
    if state.led:
        await state.led.disconnect()
    led = LedPanel(body["address"], pin=body.get("pin") or None)
    await led.connect()
    state.led = led
    state.remember(body["address"], body.get("pin") or None)
    return await status_dict(state)


@functools.partial(api, relink=False)
async def disconnect(request, state):
    if state.led:
        await state.led.disconnect()
    state.led = None
    state.remember(None)   # an explicit disconnect also stops reconnecting after a restart


async def status_dict(state: State) -> dict:
    """connected: link up now. paired: a panel was chosen (it reconnects on the next command)."""
    led = state.led
    known = bool(led and led.info)
    return {
        "connected": bool(led and led.is_connected),
        "paired": led is not None,
        "width": led.info.width if known else None,
        "height": led.info.height if known else None,
        "text_size": led.info.text_size if known else None,
        "firmware": led.hw.mcu_version if known and led.hw else None,
        "address": led.address if led else None,
        "following": state.follow_info,
    }


async def status(request: web.Request) -> web.Response:
    """Not behind the BLE lock, so the page can poll it during long uploads."""
    return web.json_response(await status_dict(request.app[STATE]))


# --- settings and modes ------------------------------------------------------

@api
async def power(request, state):
    await state.need().power((await _json(request))["on"])


@api
async def brightness(request, state):
    await state.need().brightness(int((await _json(request))["level"]))


@api
async def flip(request, state):
    await state.need().upside_down((await _json(request))["flipped"])


@screen_api
async def clock(request, state):
    b = await _json(request)
    await state.need().clock(int(b.get("style", 1)), b.get("hour_24", True), b.get("show_date", True))


@screen_api
async def countdown(request, state):
    b = await _json(request)
    await state.need().countdown(p.TimerAction[b["action"]], int(b.get("minutes", 0)),
                                 int(b.get("seconds", 0)))


@screen_api
async def stopwatch(request, state):
    await state.need().stopwatch(p.TimerAction[(await _json(request))["action"]])


@screen_api
async def scoreboard(request, state):
    b = await _json(request)
    await state.need().scoreboard(int(b["left"]), int(b["right"]))


@screen_api
async def spectrum(request, state):
    b = await _json(request)
    await state.need().spectrum([int(v) for v in b["bars"]], int(b.get("style", 0)))


@screen_api
async def rhythm(request, state):
    b = await _json(request)
    await state.need().rhythm(int(b["level"]), int(b.get("mode", 0)))


@screen_api
async def sport(request, state):
    b = await _json(request)
    await state.need().sport(p.SportMode[b["mode"]], float(b.get("speed", 0)))


@screen_api
async def remote(request, state):
    await state.need().remote_preset(int((await _json(request))["index"]))


@screen_api
async def clear(request, state):
    await state.need().clear()


@screen_api
async def exit_mode(request, state):
    await state.need().exit_mode()


# --- text ---------------------------------------------------------------------

def _text_kwargs(b: dict) -> dict:
    return dict(
        effect=TextEffect[b.get("effect", "FIXED")],
        vertical=bool(b.get("vertical")),
        speed=int(b.get("speed", 65)),
        gradient=int(b["gradient"]) if b.get("gradient") else None,
        background=_rgb(b["background"]) if b.get("background") else None,
        h_align=int(b.get("h_align", 0)),
        v_align=int(b.get("v_align", 0)),
        slot=_slot(b.get("slot")),
    )


@screen_api
async def text(request, state):
    b = await _json(request)
    led = state.need()
    await led.text(_segments(b, state.assets, led.info.text_size), **_text_kwargs(b))


@screen_api
async def animated_text(request, state):
    b = await _json(request)
    led = state.need()
    w, h = _size(led)
    bg = state.assets.read("backgrounds", b["bg_asset"]) if b.get("bg_asset") else None
    border = state.assets.read("borders", b["border_asset"]) if b.get("border_asset") else None
    segs = _segments(b, state.assets, h)
    gif = scrolling_text_gif(segs, (w, h), direction=b.get("direction", "left"),
                             blink_period=int(b["blink"]) if b.get("blink") else None,
                             background=bg, border=border, step=int(b.get("step", 1)),
                             delay_ms=int(b.get("delay", 50)))
    await led.gif(gif, _slot(b.get("slot")))


@functools.partial(api, ble=False)
async def preview_text(request, state):
    """PNG of how the text renders at panel resolution (first frame, no effect)."""
    b = await _json(request)
    w, h = _size(state.led)
    strip = text_strip(_segments(b, state.assets, h), h)
    canvas = Image.new("RGB", (max(w, strip.width), h), _rgb(b["background"]) if b.get("background") else (0, 0, 0))
    canvas.paste(strip, (0, 0), strip)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return web.Response(body=buf.getvalue(), content_type="image/png")


# --- pictures -------------------------------------------------------------------

async def _upload(request) -> tuple[bytes, dict]:
    form = await request.post()  # cached by aiohttp, so a retried request can read it again
    data, fields = b"", {}
    for name, value in form.items():
        if name == "file" and isinstance(value, web.FileField):
            value.file.seek(0)
            data = value.file.read()
        else:
            fields[name] = str(value)
    return data, fields


@screen_api
async def upload_media(request, state):
    data, fields = await _upload(request)
    if not data:
        raise web.HTTPBadRequest(text="no file")
    led = state.need()
    slot, fit, size = _slot(fields.get("slot")), fields.get("fit", "contain"), _size(led)
    try:
        img = Image.open(io.BytesIO(data))
    except Image.UnidentifiedImageError:
        raise web.HTTPBadRequest(text="not a picture format this server can read "
                                      "(PNG, JPEG, GIF, WebP, BMP, TIFF…)")
    if getattr(img, "is_animated", False):
        # A GIF already at panel size goes as-is; anything else is re-encoded to fit.
        same = img.format == "GIF" and img.size == size
        await led.gif(data if same else animation_gif(data, size, fit), slot)
    else:
        await led.image(img, slot, fit=fit)


@screen_api
async def frame(request, state):
    """Live frame: {rgb: base64 of width*height*3 bytes}."""
    b = await _json(request)
    await state.need().frame(base64.b64decode(b["rgb"]))


@screen_api
async def asset_gif(request, state):
    b = await _json(request)
    led = state.need()
    data = state.assets.read("animations", b["name"])
    with Image.open(io.BytesIO(data)) as img:
        as_is = img.format == "GIF" and img.size == _size(led)
    # anything else (another size, PNG, WebP...) is converted to a panel-size GIF
    await led.gif(data if as_is else animation_gif(data, _size(led), "cover"))


# --- storage and schedules -----------------------------------------------------

@screen_api
async def delete_all(request, state):
    await state.need().delete_all()


@api
async def delete_slots(request, state):
    await state.need().delete_slots([int(s) for s in (await _json(request))["slots"]])


@screen_api
async def playlist(request, state):
    reply = await state.need().playlist([int(s) for s in (await _json(request))["slots"]])
    return {"reply": reply.hex()}


@api
async def alarm(request, state):
    data, f = await _upload(request)
    if not data:
        raise web.HTTPBadRequest(text="an alarm needs an image or GIF")
    days = [int(d) for d in f.get("days", "").split(",") if d]
    await state.need().alarm(int(f["index"]), int(f["hour"]), int(f["minute"]), data,
                             days=days, duration=int(f.get("duration", 10)),
                             buzzer=f.get("buzzer") == "1", enabled=f.get("enabled", "1") == "1")


@api
async def timer_switch(request, state):
    b = await _json(request)
    reply = await state.need().timer_switch(int(b["index"]), bool(b["on"]), int(b["hour"]),
                                            int(b["minute"]), days=b.get("days", []),
                                            enabled=b.get("enabled", True))
    return {"reply": reply.hex()}


# --- live sports ----------------------------------------------------------------

FOLLOW_EVERY = 30  # seconds between score checks while following a game


def _date(value) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def _card_text(game: sports.Game) -> str:
    """The score once the game has started, otherwise the local kick-off time."""
    if game.started:
        return f"{game.left.score}-{game.right.score}"
    return game.start.astimezone().strftime("%H:%M")


async def _card(game: sports.Game, size: tuple[int, int]) -> Image.Image:
    left, right = await asyncio.gather(sports.logo(game.left.logo), sports.logo(game.right.logo))
    text = (game.left.score, game.right.score) if game.started else _card_text(game)
    return score_card(left, right, text, size)


def _follow_label(game: sports.Game) -> str:
    return f"{game.left.abbr} {_card_text(game)} {game.right.abbr} · {game.detail}"


def _game_json(game: sports.Game) -> dict:
    team = lambda t: {"name": t.name, "abbr": t.abbr, "logo": t.logo, "score": t.score}
    return {"id": game.id, "league": game.league, "left": team(game.left),
            "right": team(game.right), "state": game.state, "detail": game.detail,
            "start": game.start.isoformat(), "text": _card_text(game)}


async def sports_leagues(request: web.Request) -> web.Response:
    return web.json_response([{"key": k, "label": label} for k, (_, label, _) in sports.LEAGUES.items()])


@functools.partial(api, ble=False)
async def sports_games(request, state):
    b = await _json(request)
    return [_game_json(g) for g in await sports.games(b["league"], _date(b.get("date")))]


@functools.partial(api, ble=False)
async def sports_preview(request, state):
    b = await _json(request)
    game = await sports.game(b["league"], b["id"], _date(b.get("date")))
    buf = io.BytesIO()
    (await _card(game, _size(state.led))).save(buf, format="PNG")
    return web.Response(body=buf.getvalue(), content_type="image/png")


async def _follow(state: State, league: str, game_id: str, date: dt.date | None, shown: str) -> None:
    """Re-send the score card whenever the score (or kick-off) text changes; stop after the final."""
    while True:
        await asyncio.sleep(FOLLOW_EVERY)
        try:
            game = await sports.game(league, game_id, date)
            if state.follow_info is not None:
                state.follow_info["label"] = _follow_label(game)
            if _card_text(game) != shown:
                card = await _card(game, _size(state.led))
                async with state.lock:
                    await state.relink()
                    await state.need().image(card)
                shown = _card_text(game)
                log.info("score update %s: %s", game_id, shown)
            if game.state == "post":
                break
        except asyncio.CancelledError:
            raise
        except Exception:  # a network or Bluetooth hiccup must not end the follow
            log.exception("score update failed; retrying in %d s", FOLLOW_EVERY)
    state.follow, state.follow_info = None, None


@screen_api
async def sports_show(request, state):
    b = await _json(request)
    led = state.need()
    date = _date(b.get("date"))
    game = await sports.game(b["league"], b["id"], date)
    await led.image(await _card(game, _size(led)))
    if b.get("follow") and game.state != "post":
        state.follow_info = {"kind": "sports", "league": game.league, "id": game.id,
                             "label": _follow_label(game)}
        state.follow = asyncio.create_task(_follow(state, game.league, game.id, date,
                                                   _card_text(game)))
    return _game_json(game)


@api
async def live_stop(request, state):
    state.stop_follow()


# --- weather --------------------------------------------------------------------

WEATHER_EVERY = 300                                      # seconds between forecast refreshes
PAGE_SECONDS = {"now": 8, "time": 6, "details": 5}      # how long each page stays up


def _weather_args(b: dict) -> tuple[float, float, bool, bool, list[str]]:
    pages = [p for p in b.get("pages", WEATHER_PAGES) if p in WEATHER_PAGES] or ["now"]
    return float(b["lat"]), float(b["lon"]), b.get("unit") == "F", b.get("hour_24", True), pages


def _weather_json(w: weather.Weather) -> dict:
    return {"temp": w.temp, "feels_like": w.feels_like, "humidity": w.humidity, "wind": w.wind,
            "wind_unit": w.wind_unit, "kind": w.kind, "label": w.label, "is_day": w.is_day,
            "high": w.high, "low": w.low, "rain_chance": w.rain_chance, "sunrise": w.sunrise,
            "sunset": w.sunset, "unit": "F" if w.fahrenheit else "C",
            "local_time": f"{w.local_now():%H:%M}"}


@functools.partial(api, ble=False)
async def weather_search(request, state):
    places = await weather.search((await _json(request))["q"])
    return [{"label": p.label, "name": p.name, "lat": p.lat, "lon": p.lon} for p in places]


@functools.partial(api, ble=False)
async def weather_now(request, state):
    lat, lon, fahrenheit, _, _ = _weather_args(await _json(request))
    return _weather_json(await weather.current(lat, lon, fahrenheit))


@functools.partial(api, ble=False)
async def weather_preview(request, state):
    """The pages as an animated GIF at panel size, exactly what the panel will loop."""
    lat, lon, fahrenheit, hour_24, pages = _weather_args(await _json(request))
    w = await weather.current(lat, lon, fahrenheit)
    return web.Response(body=_weather_gif(w, pages, _size(state.led), hour_24),
                        content_type="image/gif")


def _weather_gif(w: weather.Weather, pages: list[str], size: tuple[int, int], hour_24: bool,
                 now=None) -> bytes:
    frames = [weather_card(w, p, size, now=now, hour_24=hour_24) for p in pages]
    return encode_gif(frames, [PAGE_SECONDS[p] * 1000 for p in pages])


async def _weather_live(state: State, name: str, lat: float, lon: float, fahrenheit: bool,
                        hour_24: bool, pages: list[str], w: weather.Weather) -> None:
    """Keep the pages playing as one looping GIF in the show-now slot.

    Live frames only cover the stored program for a moment (the panel's "LED" logo flashes
    in between when its memory is empty), so the panel loops the GIF itself. A new GIF is
    uploaded only when it changes: each minute for the clock, and when the forecast
    (re-fetched every WEATHER_EVERY seconds) changes."""
    loop = asyncio.get_running_loop()
    fetched, sent = loop.time(), None
    while True:
        if loop.time() - fetched >= WEATHER_EVERY:
            try:
                w = await weather.current(lat, lon, fahrenheit)
                fetched = loop.time()
            except Exception:  # keep showing the last forecast; try again next minute
                log.exception("weather refresh failed")
        if state.follow_info is not None:
            state.follow_info["label"] = f"{name} {round(w.temp)}° · {w.label}"
        now = w.local_now()
        gif = _weather_gif(w, pages, _size(state.led), hour_24, now=now)
        if gif != sent:
            try:
                async with state.lock:
                    await state.relink()
                    await state.need().gif(gif)
                sent = gif
            except asyncio.CancelledError:
                raise
            except Exception:  # a Bluetooth hiccup must not end the display; retry soon
                log.exception("weather upload failed")
                await asyncio.sleep(5)
                continue
        # wake just after the next minute starts, sooner if the forecast is due
        next_minute = 60 - now.second - now.microsecond / 1e6 + 0.5
        await asyncio.sleep(max(0.05, min(next_minute, WEATHER_EVERY - (loop.time() - fetched))))


@screen_api
async def weather_show(request, state):
    b = await _json(request)
    lat, lon, fahrenheit, hour_24, pages = _weather_args(b)
    w = await weather.current(lat, lon, fahrenheit)
    # Empty the panel's memory: any stored program (the last text, picture or score card)
    # would otherwise come back between the live weather frames.
    await state.need().delete_all()
    name = b.get("name") or f"{lat:.2f},{lon:.2f}"
    state.follow_info = {"kind": "weather", "label": f"{name} {round(w.temp)}° · {w.label}"}
    state.follow = asyncio.create_task(_weather_live(state, name, lat, lon, fahrenheit,
                                                     hour_24, pages, w))
    return _weather_json(w)


# --- assets ---------------------------------------------------------------------

@functools.partial(api, ble=False)
async def assets_list(request, state):
    w, h = _size(state.led)
    a = state.assets
    size = state.led.info.text_size if state.led else 16
    return {
        "root": str(a.root),
        "backgrounds": a.backgrounds(w, h),
        "borders": a.borders(w, h),
        "animations": a.animations(w, h),
        "emoji": [{"name": n, "file": a.emoji_file(n, size)} for n in a.emoji_names()],
    }


async def asset_file(request: web.Request) -> web.Response:
    state: State = request.app[STATE]
    folder, name = request.match_info["folder"], request.match_info["name"]
    try:
        data = state.assets.read(folder, name)
    except KeyError:
        raise web.HTTPNotFound()
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return web.Response(body=data, content_type=ctype, headers={"Cache-Control": "max-age=86400"})


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


STATE: web.AppKey["State"] = web.AppKey("state")
AUTOCONNECT: web.AppKey[asyncio.Task] = web.AppKey("autoconnect")


async def _connect_remembered(app: web.Application) -> None:
    state = app[STATE]

    async def go():
        try:
            async with state.lock:
                await state.relink()
        except Exception as e:  # panel off or out of range: the next command tries again
            log.warning("could not reach the remembered panel: %s", e)
    app[AUTOCONNECT] = asyncio.create_task(go())


def make_app(assets_dir: str | Path | None = None, panel_file: Path | None = None) -> web.Application:
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app[STATE] = State(Assets(assets_dir) if assets_dir else Assets(),
                       panel_file or default_panel_file())
    app.on_startup.append(_connect_remembered)
    r = app.router
    r.add_get("/", index)
    r.add_get("/api/status", status)
    r.add_post("/api/scan", scan)
    r.add_post("/api/connect", connect)
    r.add_post("/api/disconnect", disconnect)
    for path, h in [("power", power), ("brightness", brightness), ("flip", flip),
                    ("clock", clock), ("countdown", countdown), ("stopwatch", stopwatch),
                    ("scoreboard", scoreboard), ("spectrum", spectrum), ("rhythm", rhythm),
                    ("sport", sport), ("remote", remote), ("exit", exit_mode), ("clear", clear),
                    ("text", text), ("animated_text", animated_text),
                    ("preview/text", preview_text), ("media", upload_media), ("frame", frame),
                    ("asset_gif", asset_gif), ("delete_all", delete_all),
                    ("delete_slots", delete_slots), ("playlist", playlist), ("alarm", alarm),
                    ("timer_switch", timer_switch), ("assets", assets_list)]:
        r.add_post(f"/api/{path}", h)
    r.add_get("/assets/{folder}/{name}", asset_file)
    r.add_get("/api/sports/leagues", sports_leagues)
    for path, h in [("games", sports_games), ("preview", sports_preview), ("show", sports_show),
                    ("stop", live_stop)]:
        r.add_post(f"/api/sports/{path}", h)
    for path, h in [("search", weather_search), ("now", weather_now),
                    ("preview", weather_preview), ("show", weather_show)]:
        r.add_post(f"/api/weather/{path}", h)
    r.add_post("/api/live/stop", live_stop)

    async def cleanup(app):
        app[STATE].stop_follow()
        led = app[STATE].led
        if led:
            await led.disconnect()
    app.on_cleanup.append(cleanup)
    return app


def main() -> None:
    ap = argparse.ArgumentParser(prog="ledpanel-web")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--assets", help="your assets folder (default: $LEDPANEL_ASSETS or ~/.ledpanel/assets)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    print(f"LED Panel Studio on http://{args.host}:{args.port}")
    app = make_app(args.assets)
    print(f"Assets from {app[STATE].assets.root}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
