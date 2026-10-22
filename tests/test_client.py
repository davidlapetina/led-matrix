"""Upload slot choice, with the BLE transport replaced by a recorder."""

import asyncio

from ledpanel import protocol as p
from ledpanel.client import LedPanel


class Recorder(LedPanel):
    def __init__(self, mcu_major: int):
        super().__init__("00:00:00:00:00:00")
        self.hw = p.HardwareInfo(mcu_major, 22, 1, 0)
        self.sent: list[bytes] = []

    async def request(self, data: bytes, timeout: float | None = None) -> bytes:
        self.sent.append(data)
        return bytes([5, 0, data[2], data[3], 1])

    async def send_frames(self, frames, advance=(p.AckStatus.NEXT,), require_done=True):
        self.sent.extend(frames)


def slot_of(upload_frame: bytes) -> int:
    return upload_frame[14]


def test_show_now_on_new_firmware_plays_only_the_show_slot():
    led = Recorder(mcu_major=15)
    asyncio.run(led.gif(b"GIF89a-data"))
    assert led.sent[0] == p.delete_slots([p.SLOT_SHOW])  # a GIF over stored text reboots the panel
    assert led.sent[1] == p.set_playlist([p.SLOT_SHOW]) == bytes([7, 0, 8, 0x80, 1, 0, 100])
    assert slot_of(led.sent[2]) == p.SLOT_SHOW


def test_show_now_on_old_firmware_uses_the_temporary_slot():
    led = Recorder(mcu_major=4)
    asyncio.run(led.gif(b"GIF89a-data"))
    assert len(led.sent) == 1 and slot_of(led.sent[0]) == p.SLOT_TEMPORARY


def test_explicit_slot_is_stored_and_made_the_playing_one():
    led = Recorder(mcu_major=15)
    asyncio.run(led.gif(b"GIF89a-data", slot=7))
    assert led.sent[:2] == [p.delete_slots([7]), p.set_playlist([7])] and slot_of(led.sent[2]) == 7


class Wire(LedPanel):
    """Records raw writes and answers like the panel (05 00 cmd sub status)."""

    def __init__(self):
        super().__init__("00:00:00:00:00:00")
        self.hw = p.HardwareInfo(15, 22, 1, 0)
        self.sent: list[bytes] = []

    async def _write(self, data: bytes) -> None:
        self._playing = None
        self.sent.append(data)
        ok = 1 if data[2:4] == bytes([8, 0x80]) else 3   # playlist: accepted, uploads: done
        self._rx.put_nowait(bytes([5, 0, data[2], data[3], ok]))


def test_replacing_a_playing_gif_skips_the_slot_reset():
    """Emptying the show slot makes the panel flash its logo, so repeat GIF updates
    (the weather clock each minute) overwrite it directly."""
    led = Wire()
    asyncio.run(led.gif(b"GIF89a-one"))
    assert led.sent[:2] == [p.delete_slots([p.SLOT_SHOW]), p.set_playlist([p.SLOT_SHOW])]
    led.sent.clear()
    asyncio.run(led.gif(b"GIF89a-two"))
    assert len(led.sent) == 1 and slot_of(led.sent[0]) == p.SLOT_SHOW


def test_anything_else_sent_brings_the_slot_reset_back():
    from PIL import Image
    led = Wire()
    asyncio.run(led.gif(b"GIF89a-one"))
    asyncio.run(led.brightness(50))
    led.sent.clear()
    asyncio.run(led.gif(b"GIF89a-two"))
    assert led.sent[0] == p.delete_slots([p.SLOT_SHOW])
    led.sent.clear()
    asyncio.run(led.image(Image.new("RGB", (8, 8))))   # a picture over the GIF: different kind
    assert led.sent[0] == p.delete_slots([p.SLOT_SHOW])
