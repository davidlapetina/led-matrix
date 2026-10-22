"""Async BLE client for LED_BLE matrix panels, built on bleak."""

from __future__ import annotations

import asyncio
import datetime as _dt
import io
import logging
from collections.abc import Iterable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import protocol as p
from .render import fit_picture, scrolling_text_gif
from .text import Color, HAlign, Segment, TextEffect, VAlign, build_text_block

log = logging.getLogger(__name__)


class LedPanelError(RuntimeError):
    pass


async def discover(timeout: float = 8.0) -> list[BLEDevice]:
    """Find panels; the app keeps devices whose name contains LED_BLE."""
    devices = await BleakScanner.discover(timeout=timeout)
    return [d for d in devices if d.name and p.DEVICE_NAME_MARKER in d.name]


class LedPanel:
    """Connection to one panel.

        async with LedPanel("AA:BB:...") as led:
            await led.brightness(60)
            await led.text("Hello", color=(255, 0, 0))
    """

    def __init__(self, address: str | BLEDevice, *, pin: str | None = None,
                 pin_length: int = 6, ack_timeout: float = 8.0, retries: int = 3):
        self._address = address
        self._client = BleakClient(address)
        self._used = False
        # (slot, kind) this client last uploaded and made the only playing slot; cleared by
        # any other write, since that may change what the panel shows.
        self._playing: tuple[int, p.DataType] | None = None
        self._pin = pin
        self._pin_length = pin_length
        self._ack_timeout = ack_timeout
        self._retries = retries
        self._rx: asyncio.Queue[bytes] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self.info: p.DeviceInfo | None = None
        self.hw: p.HardwareInfo | None = None

    async def __aenter__(self) -> LedPanel:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    # --- connection ---------------------------------------------------------

    async def connect(self) -> p.DeviceInfo:
        """Connect (or reconnect after a dropped link) and read the panel's info."""
        if self._used:
            # macOS keeps notification state on a dropped client; start from a clean one.
            self._client = BleakClient(self._address)
        self._used = True
        await self._client.connect()
        self._drain()
        service = self._client.services.get_service(p.SERVICE_UUID)
        if service is None:
            raise LedPanelError("LED_BLE service 0x00FA not found on device")
        notify = [c for c in service.characteristics if "notify" in c.properties]
        if not notify:
            raise LedPanelError("no notify characteristic in service 0x00FA")
        for char in notify:
            await self._client.start_notify(char, self._on_notify)
        self._chunk = min(p.MAX_WRITE_CHUNK, max(20, self._client.mtu_size - 3))
        reply = await self.request(p.sync_time())
        self.info = p.parse_device_info(reply)
        log.info("connected: %s", self.info)
        if self.info.password_required:
            if self._pin is None:
                raise LedPanelError("device requires a PIN; pass pin=...")
            await self.request(p.verify_password(self._pin, self._pin_length))
        self.hw = p.parse_hardware_info(await self.request(p.hardware_info()))
        log.info("firmware: mcu %s", self.hw.mcu_version)
        return self.info

    @property
    def new_text_format(self) -> bool:
        return bool(self.hw) and p.uses_new_text_format(self.hw.mcu_major, self.hw.mcu_minor)

    @property
    def address(self) -> str:
        return str(self._client.address)

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    async def disconnect(self) -> None:
        await self._client.disconnect()

    def _on_notify(self, _char, data: bytearray) -> None:
        log.debug("rx %s", bytes(data).hex())
        self._rx.put_nowait(bytes(data))

    # --- transport ----------------------------------------------------------

    def _drain(self) -> None:
        while not self._rx.empty():
            self._rx.get_nowait()

    async def _write(self, data: bytes) -> None:
        log.debug("tx %s", data.hex())
        self._playing = None
        for off in range(0, len(data), self._chunk):
            await self._client.write_gatt_char(p.WRITE_UUID, data[off:off + self._chunk],
                                               response=True)

    async def send(self, data: bytes) -> None:
        """Write a frame without waiting for a reply."""
        async with self._lock:
            await self._write(data)

    async def request(self, data: bytes, timeout: float | None = None) -> bytes:
        """Write a frame and return the next notification."""
        async with self._lock:
            self._drain()
            await self._write(data)
            return await asyncio.wait_for(self._rx.get(), timeout or self._ack_timeout)

    @property
    def frame_size(self) -> int:
        return self.info.frame_size if self.info else 4096

    async def upload(self, kind: p.DataType, data: bytes, slot: int | None = None) -> None:
        """Send a bulk upload frame by frame, waiting for each ack like the app.

        slot=None shows the content now, replacing whatever was playing. A slot number
        stores it there. On MCU majors > 4 the app first makes that slot the only one
        playing (07 00 08 80 01 00 <slot>, reply 05 00 08 80 01), and "show now" goes
        through SLOT_SHOW the same way; older firmware shows it from SLOT_TEMPORARY.
        """
        new_firmware = bool(self.hw) and self.hw.mcu_major > 4
        if slot is None:
            slot = p.SLOT_SHOW if new_firmware else p.SLOT_TEMPORARY
        if slot != p.SLOT_TEMPORARY and new_firmware and self._playing != (slot, kind):
            # Writing a GIF over a slot that holds text reboots the panel (MCU 15.22),
            # so empty the slot first; the panel answers 05 00 02 01 01. When the slot is
            # already playing the same kind of content this is skipped: an empty slot shows
            # the panel's "LED" logo until the upload ends, a visible blink on every update.
            await self.request(p.delete_slots([slot]))
            reply = await self.request(p.set_playlist([slot]))
            if reply != b"\x05\x00\x08\x80\x01":
                raise LedPanelError(f"slot {slot} not accepted: {reply.hex()}")
        await self.send_frames(p.bulk_frames(kind, data, slot, self.frame_size))
        if new_firmware:
            self._playing = (slot, kind)

    async def send_frames(self, frames: list[bytes],
                          advance: tuple[p.AckStatus, ...] = (p.AckStatus.NEXT,),
                          require_done: bool = True) -> None:
        """Send pre-built frames, waiting for a 5-byte ack after each.

        advance: statuses meaning "send the next frame". Uploads use NEXT (1);
        multi-block live frames use status 2 (SendCore.sendCameraData).
        """
        async with self._lock:
            self._drain()
            for i, fr in enumerate(frames):
                status = await self._send_frame(fr)
                if status is p.AckStatus.DONE:
                    return
                if status not in advance:
                    raise LedPanelError(f"frame {i + 1}/{len(frames)} rejected: {status!r}")
            if require_done:
                raise LedPanelError("device never reported upload completion")

    async def _send_frame(self, fr: bytes) -> p.AckStatus:
        for attempt in range(self._retries):
            await self._write(fr)
            try:
                while True:
                    reply = await asyncio.wait_for(self._rx.get(), self._ack_timeout)
                    status = p.parse_ack(reply)
                    if status is not None:
                        return status
                    log.debug("ignoring non-ack notification %s", reply.hex())
            except asyncio.TimeoutError:
                log.warning("no ack (attempt %d/%d)", attempt + 1, self._retries)
        raise LedPanelError("upload timed out waiting for ack")

    # --- device settings ----------------------------------------------------

    async def power(self, on: bool) -> None:
        await self.send(p.power(on))

    async def brightness(self, level: int) -> None:
        """0-100."""
        await self.send(p.brightness(level))

    async def upside_down(self, flipped: bool) -> None:
        await self.send(p.upside_down(flipped))

    async def sync_time(self, now: _dt.datetime | None = None) -> p.DeviceInfo:
        return p.parse_device_info(await self.request(p.sync_time(now)))

    async def hardware_info(self) -> p.HardwareInfo:
        return p.parse_hardware_info(await self.request(p.hardware_info()))

    async def set_password(self, enabled: bool, pin: str) -> bytes:
        return await self.request(p.set_password(enabled, pin, self._pin_length))

    # --- built-in modes -----------------------------------------------------

    async def clock(self, style: int = 1, hour_24: bool = True, show_date: bool = True) -> None:
        await self.send(p.clock_mode(style, hour_24, show_date))

    async def countdown(self, action: p.TimerAction = p.TimerAction.START,
                        minutes: int = 0, seconds: int = 0) -> None:
        """START takes minutes (0-90) and seconds; PAUSE/RESUME/RESET ignore them."""
        await self.send(p.countdown(action, minutes, seconds))

    async def stopwatch(self, action: p.TimerAction = p.TimerAction.START) -> None:
        await self.send(p.stopwatch(action))

    async def scoreboard(self, left: int, right: int) -> None:
        await self.send(p.scoreboard(left, right))

    async def rhythm(self, level: int, mode: int = 0) -> None:
        """Built-in music animation; call about every 100 ms with level 0-7."""
        await self.send(p.rhythm(level, mode))

    async def spectrum(self, bars: list[int], style: int = 0) -> None:
        """11 bands, 0-255 each, in style 0-4; call about every 100 ms."""
        await self.send(p.rhythm_bars(style, bars))

    async def sport(self, mode: p.SportMode, speed_kmh: float = 0.0) -> None:
        """Riding display: turn signals, stop, SOS, auto flash, speedometer."""
        await self.send(p.sport(mode, speed_kmh))

    async def remote_preset(self, index: int, chinese: bool = False) -> None:
        """One of the 20 remote-control presets (car-sign style animations)."""
        await self.send(p.remote_preset(index, chinese))

    async def text_speed(self, speed: int) -> None:
        await self.send(p.text_speed(speed))

    async def exit_mode(self) -> None:
        await self.send(p.exit_mode())

    # --- stored programs, alarms, schedules ----------------------------------

    async def delete_all(self) -> bytes:
        """Erase every stored program. Waits for the panel's reply, which otherwise arrives
        late and gets taken as the answer to the next request."""
        return await self.request(p.delete_all())

    async def delete_slots(self, slots: Iterable[int]) -> bytes:
        """Delete stored slots; returns the panel's reply (05 00 02 01 01 on MCU 15.22)."""
        return await self.request(p.delete_slots(list(slots)))

    async def playlist(self, slots: list[int]) -> bytes:
        """Select which stored slots rotate. The app always re-uploads each slot
        afterwards; using this alone to replay stored slots is unverified."""
        return await self.request(p.set_playlist(slots))

    async def alarm(self, index: int, hour: int, minute: int, media, *,
                    days: Iterable[int] = (), enabled: bool = True, duration: int = 10,
                    buzzer: bool = True) -> None:
        """Alarm 0-9 that shows an image or GIF for `duration` s (10/30/60/300/900).
        media: GIF bytes/path, PNG/other image path/bytes, or PIL.Image.
        days: ISO weekdays 1-7 to repeat on; empty = once."""
        data = _load_bytes(media)
        if data[:6] in (b"GIF87a", b"GIF89a"):
            kind = p.AlarmMedia.GIF
        else:
            kind, data = p.AlarmMedia.PNG, self._png(media)
        await self.send_frames(p.alarm_frames(index, hour, minute, kind, data, days=days,
                                              enabled=enabled, duration=duration,
                                              buzzer=buzzer, frame_size=self.frame_size))

    async def timer_switch(self, index: int, on: bool, hour: int, minute: int, *,
                           days: Iterable[int] = (), enabled: bool = True) -> bytes:
        """Scheduled screen on/off entry 0-9."""
        return await self.request(p.timer_switch(on, index, hour, minute, days, enabled))

    # --- content ------------------------------------------------------------

    def _png(self, source, resize: bool = True, fit: str = "contain") -> bytes:
        img = _open_image(source)
        if resize and self.info:
            img = fit_picture(img, (self.info.width, self.info.height), fit)
        else:
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    async def image(self, source, slot: int | None = None, resize: bool = True,
                    fit: str = "contain") -> None:
        """Show a still picture in any format Pillow reads (PNG, JPEG, WebP, BMP, …;
        path, bytes or PIL.Image), sent as PNG. fit: contain, cover or stretch."""
        await self.upload(p.DataType.IMAGE, self._png(source, resize, fit), slot)

    async def gif(self, source, slot: int | None = None) -> None:
        """Play a GIF (path or bytes); it should already match the panel size."""
        await self.upload(p.DataType.GIF, _load_bytes(source), slot)

    async def clear(self) -> None:
        """Blank the screen: show an all-black picture in place of the current content."""
        from PIL import Image

        size = (self.info.width, self.info.height) if self.info else (64, 16)
        await self.image(Image.new("RGB", size), resize=False)

    async def text(self, content: str | Iterable[Segment], *, color: Color = (255, 255, 255),
                   background: Color | None = None, gradient: int | None = None,
                   height: int | None = None, effect: int = TextEffect.FIXED,
                   vertical: bool = False, speed: int = 65, h_align: int = HAlign.LEFT,
                   v_align: int = VAlign.TOP, font: str | None = None,
                   slot: int | None = None) -> None:
        """Render text (Spans for per-letter colours, Emoji icons) locally, as the
        app does, and upload it. gradient 1-8 uses a firmware gradient preset."""
        height = height or (self.info.text_size if self.info else 16)
        block = build_text_block(content, height=height, color=color, background=background,
                                 gradient=gradient, effect=effect, vertical=vertical,
                                 speed=speed, h_align=h_align, v_align=v_align, font=font,
                                 new_format=self.new_text_format)
        await self.upload(p.DataType.TEXT, block, slot)

    async def animated_text(self, content: str | Iterable[Segment], *,
                            color: Color = (255, 255, 255), direction: str = "left",
                            blink_period: int | None = None, background: bytes | None = None,
                            border: bytes | None = None, font: str | None = None,
                            delay_ms: int = 50, slot: int | None = None) -> None:
        """Effects the text block can't combine (e.g. scroll + blink, or text over
        a background animation and border): rendered here and sent as a GIF."""
        size = (self.info.width, self.info.height) if self.info else (64, 16)
        await self.gif(scrolling_text_gif(content, size, color=color, direction=direction,
                                          blink_period=blink_period, background=background,
                                          border=border, font=font, delay_ms=delay_ms), slot)

    # --- live drawing and streaming ----------------------------------------

    async def diy_start(self, clear: bool = True) -> None:
        await self.send(p.diy_mode(p.DiyMode.ENTER_CLEAR_CUR_SHOW if clear
                                   else p.DiyMode.ENTER_NO_CLEAR_CUR_SHOW))

    async def diy_stop(self, keep: bool = True) -> None:
        await self.send(p.diy_mode(p.DiyMode.QUIT_STILL_CUR_SHOW if keep
                                   else p.DiyMode.QUIT_NOSAVE_KEEP_PREV))

    async def pixels(self, color: Color, points: list[tuple[int, int]],
                     move: p.DiyMoveType = p.DiyMoveType.NO_EFFECT) -> None:
        """Set pixels live; call diy_start() first."""
        await self.send(p.diy_pixels(color, points, move))

    async def frame(self, source) -> None:
        """Push one full picture immediately (games/camera path): a PIL.Image,
        path, or raw RGB888 bytes of exactly width*height*3. Fire-and-forget, so
        call it in a loop for animation (the app paces ~120 ms per frame)."""
        if isinstance(source, (bytes, bytearray)) and self.info and \
                len(source) == self.info.width * self.info.height * 3:
            data = bytes(source)
        else:
            img = _open_image(source).convert("RGB")
            if self.info:
                img = img.resize((self.info.width, self.info.height))
            data = img.tobytes()
        frames = p.raw_frames(data, self.frame_size)
        if len(frames) == 1:
            await self.send(frames[0])
        else:
            await self.send_frames(frames, advance=(p.AckStatus.NEXT, p.AckStatus.NO_SPACE),
                                   require_done=False)


def _load_bytes(source) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if hasattr(source, "save"):  # PIL.Image
        buf = io.BytesIO()
        source.save(buf, format="PNG")
        return buf.getvalue()
    with open(source, "rb") as f:
        return f.read()


def _open_image(source):
    from PIL import Image

    if isinstance(source, Image.Image):
        return source
    if isinstance(source, (bytes, bytearray)):
        return Image.open(io.BytesIO(source))
    return Image.open(source)
