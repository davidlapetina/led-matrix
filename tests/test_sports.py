"""ESPN scoreboard parsing, the score card and the live-follow server flow (no network)."""

import asyncio
import io

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from ledpanel import sports
from ledpanel.render import score_card


def _event(game_id, state, home, away, detail):
    team = lambda abbr, score, side: {
        "homeAway": side, "score": score,
        "team": {"abbreviation": abbr, "displayName": abbr + " FC",
                 "logo": f"https://x/{abbr}.png", "logoDark": f"https://x/{abbr}-dark.png"}}
    return {"id": game_id, "date": "2026-10-22T19:00Z",
            "status": {"type": {"state": state, "shortDetail": detail}},
            "competitions": [{"competitors": [team(*home, "home"), team(*away, "away")]}]}


SCOREBOARD = {"events": [_event("1", "in", ("BOU", "2"), ("SUN", "0"), "67'")]}


def test_every_league_has_an_espn_path():
    assert set(sports.LEAGUES) == {"nba", "epl", "seriea", "ligue1", "laliga", "bundesliga", "ucl"}
    assert sports.LEAGUES["ucl"] == ("soccer/uefa.champions", "Champions League", True)


def test_soccer_lists_home_first_and_nba_away_first():
    [g] = sports.parse_scoreboard(SCOREBOARD, "epl")
    assert (g.left.abbr, g.left.score, g.right.abbr, g.right.score) == ("BOU", "2", "SUN", "0")
    assert g.left.logo == "https://x/BOU-dark.png" and g.started and g.detail == "67'"
    [g] = sports.parse_scoreboard(SCOREBOARD, "nba")
    assert (g.left.abbr, g.right.abbr) == ("SUN", "BOU")


def _logo(color):
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (60, 80), (*color, 255)), (20, 10))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_score_card_puts_logos_at_the_edges_and_score_between():
    card = score_card(_logo((255, 0, 0)), _logo((0, 0, 255)), ("2", "1"), (64, 20))
    assert card.size == (64, 20)
    assert card.getpixel((8, 10))[0] > 200 and card.getpixel((56, 10))[2] > 200
    middle = [card.getpixel((x, y)) for x in range(22, 42) for y in range(20)]
    assert (255, 255, 255) in middle and all(p in ((0, 0, 0), (255, 255, 255)) for p in middle)  # crisp text


def test_wide_nba_score_stays_on_one_line_in_led_digits():
    from ledpanel.render import _pixel_text
    card = score_card(_logo((255, 0, 0)), _logo((0, 0, 255)), ("132", "125"), (64, 20))
    rows = {y for x in range(16, 48) for y in range(20) if card.getpixel((x, y)) == (255, 255, 255)}
    assert max(rows) - min(rows) == 6                     # a single 7-px line of LED digits
    assert card.getpixel((2, 10))[0] > 200 and card.getpixel((61, 10))[2] > 200


def test_dark_logos_are_brightened_for_leds():
    card = score_card(_logo((0, 0, 90)), _logo((0, 0, 90)), ("1", "0"), (64, 20))
    assert card.getpixel((10, 10))[2] > 130 and card.getpixel((10, 10))[:2] == (0, 0)  # 90 -> 144, still pure blue


class Panel:
    def __init__(self):
        from ledpanel.protocol import DeviceInfo
        self.is_connected, self.address, self.hw = True, "AA:BB", None
        self.info = DeviceInfo(0x85, 5, 64, 20, 4096, 16, False, b"")
        self.shown = []

    async def image(self, img, slot=None, fit="contain"):
        self.shown.append(img.size)

    async def clear(self):
        self.shown.append("clear")

    async def disconnect(self):
        pass


def test_follow_resends_on_score_change_and_stops_when_screen_is_replaced(monkeypatch):
    from ledpanel.web import server

    boards = [SCOREBOARD, {"events": [_event("1", "in", ("BOU", "3"), ("SUN", "0"), "80'")]}]
    calls = []

    async def fake_games(league, date=None):
        calls.append(league)
        return sports.parse_scoreboard(boards[0] if len(calls) <= 2 else boards[1], league)

    async def fake_logo(url):
        return _logo((200, 0, 0))

    monkeypatch.setattr(sports, "games", fake_games)
    monkeypatch.setattr(sports, "logo", fake_logo)
    monkeypatch.setattr(server, "FOLLOW_EVERY", 0.01)

    async def go():
        app = server.make_app()
        panel = Panel()
        app[server.STATE].led = panel
        async with TestClient(TestServer(app)) as c:
            listed = await (await c.post("/api/sports/games", json={"league": "epl"})).json()
            assert listed[0]["text"] == "2-0"
            r = await c.post("/api/sports/show", json={"league": "epl", "id": "1", "follow": True})
            assert (await r.json())["text"] == "2-0"
            for _ in range(100):
                if len(panel.shown) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert panel.shown == [(64, 20), (64, 20)]   # initial card + the 3-0 update
            st = await (await c.get("/api/status")).json()
            assert st["following"]["label"] == "BOU 3-0 SUN · 80'"
            await c.post("/api/clear")                     # anything else on screen ends following
            st = await (await c.get("/api/status")).json()
            assert st["following"] is None
    asyncio.run(go())


def test_small_scores_use_the_led_digit_font():
    from ledpanel.render import _pixel_text
    assert _pixel_text("6").tobytes() != _pixel_text("8").tobytes()
    card = score_card(_logo((255, 0, 0)), _logo((0, 0, 255)), ("116", "128"), (64, 20)).convert("L")
    expected = _pixel_text("116-128")
    x, y = (64 - expected.width) // 2, (20 - 7) // 2
    drawn = card.crop((x, y, x + expected.width, y + 7)).point(lambda v: 255 if v > 128 else 0).convert("1")
    assert drawn.tobytes() == expected.tobytes()


def test_wide_logo_gets_the_same_height_as_a_round_one():
    def logo(w, h):
        img = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
        img.paste(Image.new("RGBA", (w, h), (255, 0, 0, 255)), (0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    card = score_card(logo(140, 100), logo(100, 100), ("132", "125"), (64, 20))
    red = lambda x0, x1: {y for x in range(x0, x1) for y in range(20) if card.getpixel((x, y))[0] > 200
                          and card.getpixel((x, y))[1] < 80}
    assert len(red(0, 24)) == len(red(40, 64))   # both logos span the same rows
