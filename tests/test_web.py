import asyncio
import io
import pytest
from PIL import Image

pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from ledpanel.web.server import make_app  # noqa: E402

def run(check):
    async def go():
        async with TestClient(TestServer(make_app())) as client:
            await check(client)
    asyncio.run(go())


def test_index_and_status_without_panel():
    async def check(c):
        r = await c.get("/")
        assert r.status == 200 and "LED Panel Studio" in await r.text()
        st = await (await c.get("/api/status")).json()
        assert st == {"connected": False, "paired": False, "width": None, "height": None, "text_size": None,
                      "firmware": None, "address": None, "following": None}
    run(check)


def test_commands_need_a_connection():
    async def check(c):
        r = await c.post("/api/brightness", json={"level": 50})
        assert r.status == 409 and (await r.text()).startswith("Not connected to a panel")
    run(check)


def test_text_preview_renders_letter_colours():
    async def check(c):
        r = await c.post("/api/preview/text", json={"segments": [
            {"text": "I", "color": "#ff0000"}, {"text": "I", "color": "#00ff00"}]})
        assert r.status == 200 and r.content_type == "image/png"
        img = Image.open(io.BytesIO(await r.read())).convert("RGB")
        assert img.height == 20 and img.width == 64       # default panel size when offline
        colours = {px for _, px in img.getcolors(1 << 16) if px != (0, 0, 0)}
        assert any(r > 200 and g < 50 for r, g, b in colours)
        assert any(g > 200 and r < 50 for r, g, b in colours)
    run(check)


def test_user_assets_are_listed_for_the_panel_size_and_served(assets_dir):
    async def check(c):
        a = await (await c.post("/api/assets")).json()
        assert a["root"] == str(assets_dir)
        assert a["emoji"] == [{"name": "star", "file": "star.png"}, {"name": "sun", "file": "sun@16.jpg"}]
        assert a["backgrounds"] == ["any.gif", "fire@64x20.gif"]      # not big@32x32
        assert a["borders"] == ["frame@64x20.png"] and a["animations"] == ["blink@64x20.gif", "square.png"]
        r = await c.get("/assets/emoji/sun@16.jpg")
        assert r.status == 200 and r.content_type == "image/jpeg" and (await r.read())[:2] == b"\xff\xd8"
        r = await c.get("/assets/backgrounds/fire@64x20.gif")
        assert (await r.read())[:3] == b"GIF"
        assert (await c.get("/assets/animations/notes.txt")).status == 404
        assert (await c.get("/assets/secret/x.png")).status == 404
        assert (await c.get("/assets/emoji/..%2F..%2Fassets.py")).status == 404
    run(check)


class FlakyPanel:
    """Stands in for LedPanel: the link drops during the first command."""

    def __init__(self):
        self.is_connected, self.connects, self.levels = True, 0, []
        self.info = self.hw = None
        self.address = "AA:BB"

    async def disconnect(self):
        self.is_connected = False

    async def connect(self):
        self.connects += 1
        self.is_connected = True

    async def brightness(self, level):
        if self.connects == 0:
            self.is_connected = False
            from bleak.exc import BleakError
            raise BleakError("disconnected")
        self.levels.append(level)


def test_dropped_link_is_reconnected_and_the_command_retried():
    from ledpanel.web.server import STATE

    async def go():
        app = make_app()
        panel = FlakyPanel()
        app[STATE].led = panel
        async with TestClient(TestServer(app)) as c:
            r = await c.post("/api/brightness", json={"level": 40})
            assert await r.json() == {"ok": True}
            assert panel.connects == 1 and panel.levels == [40]
            panel.is_connected = False                     # dropped while idle
            st = await (await c.get("/api/status")).json()
            assert st["connected"] is False and st["paired"] is True
            await c.post("/api/brightness", json={"level": 70})
            assert panel.connects == 2 and panel.levels == [40, 70]
    asyncio.run(go())


class RecordingPanel:
    """Stands in for LedPanel and records what each upload became."""

    def __init__(self):
        from ledpanel.protocol import DeviceInfo
        self.is_connected, self.address, self.hw = True, "AA:BB", None
        self.info = DeviceInfo(0x85, 5, 64, 20, 4096, 16, False, b"")
        self.calls = []

    async def disconnect(self):
        self.is_connected = False

    async def image(self, img, slot=None, fit="contain"):
        self.calls.append(("image", img.format, fit))

    async def gif(self, data, slot=None):
        self.calls.append(("gif", Image.open(io.BytesIO(data)).size))


def _encoded(fmt, n_frames=1, size=(40, 30)):
    frames = [Image.new("RGB", size, (i * 80, 0, 0)) for i in range(n_frames)]
    buf = io.BytesIO()
    extra = dict(save_all=True, append_images=frames[1:], duration=100) if n_frames > 1 else {}
    frames[0].save(buf, format=fmt, **extra)
    return buf.getvalue()


def test_media_upload_accepts_common_formats():
    from aiohttp import FormData
    from ledpanel.web.server import STATE

    async def go():
        app = make_app()
        panel = RecordingPanel()
        app[STATE].led = panel
        async with TestClient(TestServer(app)) as c:
            async def send(data, fit="cover"):
                form = FormData()
                form.add_field("file", data, filename="x")
                form.add_field("fit", fit)
                return await c.post("/api/media", data=form)
            for fmt in ("PNG", "JPEG", "WEBP", "BMP"):
                assert (await send(_encoded(fmt))).status == 200
            assert panel.calls == [("image", f, "cover") for f in ("PNG", "JPEG", "WEBP", "BMP")]
            panel.calls.clear()
            for data in (_encoded("GIF", 3), _encoded("WEBP", 3), _encoded("PNG", 3), _encoded("GIF", 3, (64, 20))):
                assert (await send(data)).status == 200
            assert panel.calls == [("gif", (64, 20))] * 4   # every animation ends up panel-sized
            r = await send(b"not a picture")
            assert r.status == 400 and "PNG, JPEG" in await r.text()
    asyncio.run(go())


def test_animations_of_another_size_are_converted_before_sending():
    from tests.test_weather import Panel   # records GIF size and frame count

    async def go():
        app = make_app()
        from ledpanel.web.server import STATE
        panel = Panel()
        app[STATE].led = panel
        async with TestClient(TestServer(app)) as c:
            assert (await c.post("/api/asset_gif", json={"name": "square.png"})).status == 200
            assert (await c.post("/api/asset_gif", json={"name": "blink@64x20.gif"})).status == 200
            assert (await c.post("/api/asset_gif", json={"name": "../x.gif"})).status >= 400
        assert panel.sent == [((64, 20), 1), ((64, 20), 2)]
    asyncio.run(go())


def test_remembered_panel_reconnects_after_a_restart(tmp_path, monkeypatch):
    """After a server restart the last panel is reconnected on the next command, instead
    of every upload failing with 'not connected'."""
    from ledpanel.web import server
    connected = []

    class FakePanel:
        def __init__(self, address, pin=None):
            from ledpanel.protocol import DeviceInfo
            self.address, self.pin, self.is_connected, self.hw = address, pin, False, None
            self.info = DeviceInfo(0x85, 5, 64, 20, 4096, 16, False, b"")

        async def connect(self):
            self.is_connected = True
            connected.append((self.address, self.pin))

        async def disconnect(self):
            self.is_connected = False

        async def clear(self):
            pass

    monkeypatch.setattr(server, "LedPanel", FakePanel)
    panel_file = tmp_path / "panel.json"

    async def go():
        async with TestClient(TestServer(server.make_app(panel_file=panel_file))) as c:
            assert (await c.post("/api/clear")).status == 409          # nothing remembered yet
            await c.post("/api/connect", json={"address": "AA:BB", "pin": "1234"})
        assert panel_file.stat().st_mode & 0o077 == 0                     # private: may hold the PIN
        connected.clear()
        async with TestClient(TestServer(server.make_app(panel_file=panel_file))) as c:   # "restart"
            await asyncio.sleep(0.05)                                    # startup auto-connect
            assert connected == [("AA:BB", "1234")]
            assert (await c.post("/api/clear")).status == 200
            await c.post("/api/disconnect")
        assert not panel_file.exists()                                   # disconnect forgets it
    asyncio.run(go())
