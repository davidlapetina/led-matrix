"""Pure frame builders for the LED_BLE panel protocol (as used by the iPixel Color app).

Reverse engineered from com.wifiled.ipixels (iPixel Color). Every command is a
little-endian length-prefixed frame:

    [len_lo, len_hi, cmd, sub, payload...]   (len = total frame length)

Nothing in this module touches Bluetooth, so it can be unit tested offline.
"""

from __future__ import annotations

import datetime as _dt
import struct
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum

SERVICE_UUID = "000000fa-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fa02-0000-1000-8000-00805f9b34fb"
DEVICE_NAME_MARKER = "LED_BLE"

# Slot byte the app writes when content should be shown immediately rather
# than stored in a numbered program slot (SendCore: bytes[14] = 101).
SLOT_TEMPORARY = 101

# Slot used for "show now" on MCU majors > 4. There the app never uses 101 for a plain
# send: it plays only one slot (08 80 [slot]) and uploads into it. Uploading to 101 on
# these firmwares adds the item to the rotation instead (seen on MCU 15.22). 100 keeps
# clear of the app's low auto-numbered slots, 101, 111+ (remote buttons) and 120+ (alarms).
SLOT_SHOW = 100

# Largest chunk the app writes per GATT write after negotiating MTU 512.
MAX_WRITE_CHUNK = 509


class DataType(IntEnum):
    """Bulk upload kinds (SendCore.getDataType)."""

    IMAGE = 2  # PNG file bytes, wire type 02 00
    GIF = 3  # GIF file bytes, wire type 03 00
    TEXT = 4  # rendered text block, wire type 00 01


_WIRE_TYPE = {
    DataType.IMAGE: (0x02, 0x00),
    DataType.GIF: (0x03, 0x00),
    DataType.TEXT: (0x00, 0x01),
}

# Byte 13 of the bulk header (SendCore.payloadChannel): 2 for GIFs, 0 otherwise.
_CHANNEL_FLAG = {DataType.IMAGE: 0, DataType.GIF: 2, DataType.TEXT: 0}


class DiyMode(IntEnum):
    """DiyImageFun: entering/leaving the live pixel drawing mode."""

    QUIT_NOSAVE_KEEP_PREV = 0
    ENTER_CLEAR_CUR_SHOW = 1
    QUIT_STILL_CUR_SHOW = 2
    ENTER_NO_CLEAR_CUR_SHOW = 3


class DiyMoveType(IntEnum):
    """DiyImageMoveType: how a drawn pixel is mirrored or animated."""

    NO_EFFECT = 0
    HORIZONTAL_MIRROR = 1
    VERTICAL_MIRROR = 2
    OVERALL_MOVEMENT = 3
    ERASE = 4


# AppConfig.ledSizeMap: led type index -> (width, height)
LED_SIZES: list[tuple[int, int]] = [
    (64, 64), (96, 16), (32, 32), (64, 16), (32, 16), (64, 20), (128, 32),
    (144, 16), (192, 16), (48, 24), (64, 32), (96, 32), (128, 32), (96, 32),
    (160, 32), (192, 32), (256, 32), (320, 32), (384, 32), (448, 32), (16, 16),
    (32, 32), (32, 16), (48, 24), (64, 64), (96, 64), (128, 64), (160, 64),
    (192, 64), (256, 64), (320, 64), (384, 64), (448, 64), (512, 64),
    (576, 64), (640, 64),
]

# ChooseActivity.setLedType: device-reported byte -> led type index.
# Some entries depend on the advertised cid/pid; the generic choice is used here.
_TYPE_BYTE_TO_LED_TYPE = {
    0x00: 0, 0x80: 0, 0x81: 2, 0x82: 4, 0x83: 3, 0x84: 1, 0x85: 5, 0x86: 6,
    0x87: 7, 0x88: 8, 0x89: 9, 0x8A: 10, 0x8B: 11, 0x8C: 12, 0x8D: 13,
    0x8E: 14, 0x8F: 15, 0x90: 16, 0x91: 17, 0x92: 18, 0x93: 19, 0x94: 20,
    0x95: 24, 0x96: 25, 0x97: 26, 0x98: 27, 0x99: 28, 0x9A: 29, 0x9B: 30,
    0x9C: 31, 0x9D: 32, 0x9E: 33, 0x9F: 34, 0xA0: 35,
}

# Default text height the app picks per led type (ChooseActivity.setLedType).
_TEXT_SIZE_BY_LED_TYPE = {6: 32, 9: 24, 23: 24, 20: 16, 24: 64, 25: 64}
_TEXT_SIZE_BY_LED_TYPE.update({t: 32 for t in range(10, 20)})


@dataclass(frozen=True)
class DeviceInfo:
    type_byte: int
    led_type: int
    width: int
    height: int
    frame_size: int
    text_size: int
    password_required: bool
    raw: bytes


def uses_new_text_format(mcu_major: int, mcu_minor: int) -> bool:
    """TextActivity:1822 picks NewCommonTextProcess for these MCU versions."""
    v = mcu_major * 100 + mcu_minor
    return 1809 <= v < 1900 or 1412 <= v < 1500 or 2100 <= v < 2700 or 2800 <= v < 6000


def frame(cmd: int, sub: int, payload: bytes = b"") -> bytes:
    """Wrap a command in the standard length-prefixed frame."""
    length = 4 + len(payload)
    return struct.pack("<HBB", length, cmd, sub) + payload


def _byte(value: int, name: str, lo: int = 0, hi: int = 255) -> int:
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be in [{lo}, {hi}], got {value}")
    return value


# --- simple commands (BaseSend / SendCore) ---------------------------------

def sync_time(now: _dt.datetime | None = None, language: int = 0) -> bytes:
    """Set the clock and ask for the device type (SendCore.getLedType)."""
    now = now or _dt.datetime.now()
    return frame(0x01, 0x80, bytes([now.hour, now.minute, now.second, language]))


def hardware_info() -> bytes:
    """Query hardware info (SendCore.getHwInfo)."""
    return frame(0x05, 0x80)


def power(on: bool) -> bytes:
    return frame(0x07, 0x01, bytes([1 if on else 0]))


def brightness(level: int) -> bytes:
    """Panel brightness 0-100 (BaseSend.setLedLight)."""
    return frame(0x04, 0x80, bytes([_byte(level, "level", 0, 100)]))


def upside_down(flipped: bool) -> bytes:
    return frame(0x06, 0x80, bytes([1 if flipped else 0]))


def set_weekday(day: int) -> bytes:
    """ISO weekday, Monday=1 .. Sunday=7 (DateUtils.getWeekOfDate)."""
    return frame(0x12, 0x80, bytes([_byte(day, "day", 1, 7)]))


def clock_mode(style: int = 1, hour_24: bool = True, show_date: bool = True,
               date: _dt.date | None = None) -> bytes:
    """Switch to a built-in clock face (BaseSend.sendColockMode). Styles are 1-based."""
    date = date or _dt.date.today()
    return frame(0x06, 0x01, bytes([
        _byte(style, "style", 1),
        1 if hour_24 else 0,
        1 if show_date else 0,
        date.year - 2000, date.month, date.day, date.isoweekday(),
    ]))


def text_speed(speed: int) -> bytes:
    return frame(0x03, 0x01, bytes([_byte(speed, "speed")]))


def exit_mode() -> bytes:
    """Leave the current special mode (SendCore.sendExitCmd)."""
    return frame(0x01, 0x01)


def delete_all() -> bytes:
    """Erase all stored programs (BaseSend.deleteAllData)."""
    return frame(0x03, 0x80)


def delete_slots(slots: list[int]) -> bytes:
    """Delete stored program slots (SendCore.sendChannelDelIndex)."""
    body = struct.pack("<H", len(slots)) + bytes(_byte(s, "slot") for s in slots)
    return frame(0x02, 0x01, body)


class TimerAction(IntEnum):
    """Flag byte shared by countdown (0D 80) and stopwatch (09 80) UI callers."""

    RESET = 0
    START = 1
    PAUSE = 2
    RESUME = 3


def countdown(action: TimerAction, minutes: int = 0, seconds: int = 0) -> bytes:
    return frame(0x0D, 0x80, bytes([int(action), _byte(minutes, "minutes", 0, 90),
                                    _byte(seconds, "seconds", 0, 59)]))


def stopwatch(action: TimerAction) -> bytes:
    return frame(0x09, 0x80, bytes([int(action)]))


def week_byte(days: Iterable[int] = (), enabled: bool = True) -> int:
    """bit0 = enabled, bits 1..7 = ISO weekdays Monday..Sunday; no days = once."""
    value = 1 if enabled else 0
    for d in days:
        value |= 1 << _byte(d, "day", 1, 7)
    return value


def timer_switch(on: bool, index: int, hour: int, minute: int,
                 days: Iterable[int] = (), enabled: bool = True) -> bytes:
    """Scheduled screen on/off entry 0..9 (TimingData). Ack: 05 00 11 80 01."""
    return frame(0x11, 0x80, bytes([1 if on else 0, _byte(index, "index", 0, 9),
                                    week_byte(days, enabled), _byte(hour, "hour", 0, 23),
                                    _byte(minute, "minute", 0, 59)]))


def set_playlist(slots: list[int]) -> bytes:
    """Choose which stored slots rotate (08 80). The app uploads into each slot after
    the device answers 05 00 08 80 01; sending it alone to replay slots is unverified."""
    if not 1 <= len(slots) <= 255 - 6:
        raise ValueError("between 1 and 249 slots")
    body = bytes([len(slots), 0] + [_byte(s, "slot") for s in slots])
    return bytes([6 + len(slots), 0, 0x08, 0x80]) + body


def remote_preset(index: int, chinese: bool = False) -> bytes:
    """Remote-control button 1..20 (07 80): built-in car-sign style presets."""
    return frame(0x07, 0x80, bytes([_byte(index, "index", 1, 20), 1 if chinese else 0]))


class AlarmMedia(IntEnum):
    GIF = 1
    PNG = 2


ALARM_DURATIONS = (10, 30, 60, 300, 900)
ALARM_SLOT_BASE = 120


def alarm_frames(index: int, hour: int, minute: int, media: AlarmMedia, data: bytes, *,
                 days: Iterable[int] = (), enabled: bool = True, duration: int = 10,
                 buzzer: bool = True, frame_size: int = 12288) -> list[bytes]:
    """Alarm 0..9 showing a GIF/PNG for `duration` seconds (SendCore.payLoadAlarmClockData).

    Frame: len16 00 80 index week hour min dur16 media buzzer option size32 crc32
    00 00 (index+120) chunk. Acks: 05 00 00 80 01 next, ...03 done.
    """
    if duration not in ALARM_DURATIONS:
        raise ValueError(f"duration must be one of {ALARM_DURATIONS}")
    if not data:
        raise ValueError("an alarm needs an image or GIF")
    _byte(index, "index", 0, 9)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    week = week_byte(days, enabled)
    frames = []
    for i, off in enumerate(range(0, len(data), frame_size)):
        chunk = data[off:off + frame_size]
        frames.append(struct.pack(
            "<HBBBBBBHBBBII", len(chunk) + 24, 0x00, 0x80, index, week,
            _byte(hour, "hour", 0, 23), _byte(minute, "minute", 0, 59), duration,
            int(media), 1 if buzzer else 0, 0 if i == 0 else 2, len(data), crc,
        ) + bytes([0, 0, ALARM_SLOT_BASE + index]) + chunk)
    return frames


def scoreboard(left: int, right: int) -> bytes:
    return frame(0x0A, 0x80, struct.pack("<HH", _byte(left, "left", 0, 9999),
                                         _byte(right, "right", 0, 9999)))


def rhythm(level: int, mode: int = 0) -> bytes:
    """Built-in music animation (BaseSend.sendRhythm): level 0-7, mode 0 or 1.
    The app sends one every 100 ms (cycling level, or mic loudness * 7 / 100)."""
    return frame(0x00, 0x02, bytes([_byte(level, "level", 0, 7), _byte(mode, "mode", 0, 1)]))


def rhythm_bars(style: int, bars: list[int]) -> bytes:
    """11-band spectrum in one of 5 styles (0-4); bands 0-255 scaled to 1-15
    exactly like BaseSend.sendRhythmChart (silence still shows height 1)."""
    if len(bars) > 11:
        raise ValueError("at most 11 bars")
    scaled = [min(15, max(1, _byte(v, "bar") * 15 // 255)) for v in bars]
    scaled += [1] * (11 - len(scaled))
    return frame(0x01, 0x02, bytes([_byte(style, "style", 0, 4)] + scaled))


class SportMode(IntEnum):
    """Riding mode buttons (BaseSend.sendSportData, 07 00 06 00)."""

    LEFT_TURN = 0
    RIGHT_TURN = 1
    GO = 2
    STOP = 3
    SOS = 4
    AUTO_FLASH = 5
    SPEEDOMETER = 6


def sport(mode: SportMode, speed_kmh: float = 0.0) -> bytes:
    """Riding display. Speed is only used by SPEEDOMETER (whole km/h + tenths);
    the app resends AUTO_FLASH/SPEEDOMETER every 2 s."""
    whole = tenths = 0
    if mode is SportMode.SPEEDOMETER:
        whole, tenths = divmod(round(speed_kmh * 10), 10)
    return frame(0x06, 0x00, bytes([int(mode), _byte(whole, "speed"), tenths]))


def raw_frames(data: bytes, frame_size: int = 4096) -> list[bytes]:
    """Live picture (SendCore.sendCameraData / sendUnRedoImageData, wire type 00 00).

    data is RGB888 row-major at panel size (w*h*3), or a JPEG/PNG file; the
    firmware accepts all three. Header: len16 00 00 option size32 where size32
    is always the panel frame size, not len(data). No CRC, no slot. Blocks are
    12288 bytes; frames up to that size are a single write with no ack.
    """
    if not data:
        raise ValueError("data must not be empty")
    frames = []
    for i, off in enumerate(range(0, len(data), 12288)):
        chunk = data[off:off + 12288]
        frames.append(struct.pack("<HBBBI", 9 + len(chunk), 0, 0, 0 if i == 0 else 2,
                                  frame_size) + chunk)
    return frames


def _pin_digits(pin: str, length: int) -> list[int]:
    if len(pin) != length or not pin.isdigit():
        raise ValueError(f"pin must be {length} digits")
    pairs = [int(pin[i:i + 2]) for i in range(0, length, 2)]
    return pairs + [0] * (3 - len(pairs))


def set_password(enabled: bool, pin: str, length: int = 6) -> bytes:
    """Set/clear the connection PIN (4 digits on some models, 6 on others)."""
    return frame(0x04, 0x02, bytes([1 if enabled else 0] + _pin_digits(pin, length)))


def verify_password(pin: str, length: int = 6) -> bytes:
    return frame(0x05, 0x02, bytes(_pin_digits(pin, length)))


def diy_mode(mode: DiyMode) -> bytes:
    return frame(0x04, 0x01, bytes([int(mode)]))


def diy_pixels(color: tuple[int, int, int], points: list[tuple[int, int]],
               move: DiyMoveType = DiyMoveType.NO_EFFECT) -> bytes:
    """Paint pixels live while in DIY mode (SendCore.sendDiyImageData).

    Payload is R,G,B then x,y pairs; the header uses wire type 05 01 with the
    move type in the option byte and no size field.
    """
    if not points:
        raise ValueError("points must not be empty")
    body = bytes(color) + bytes(c for p in points for c in p)
    return struct.pack("<HBBB", 5 + len(body), 0x05, 0x01, int(move)) + body


# --- bulk uploads (SendCore.payloadChannel + sendDataInner) ---------------

def bulk_frames(kind: DataType, data: bytes, slot: int = SLOT_TEMPORARY,
                frame_size: int = 12288) -> list[bytes]:
    """Split an upload into the frames the app sends one at a time.

    Each frame: len(2) type(2) option(1) total_size(4) crc32(total)(4)
    flag(1) slot(1) chunk. option is 0 on the first frame, 2 afterwards.
    The device acknowledges each frame with a 5-byte notification.
    """
    if not data:
        raise ValueError("data must not be empty")
    t0, t1 = _WIRE_TYPE[kind]
    crc = zlib.crc32(data) & 0xFFFFFFFF
    frames = []
    for i, off in enumerate(range(0, len(data), frame_size)):
        chunk = data[off:off + frame_size]
        header = struct.pack("<HBBBII", len(chunk) + 15, t0, t1,
                             0 if i == 0 else 2, len(data), crc)
        frames.append(header + bytes([_CHANNEL_FLAG[kind], _byte(slot, "slot")]) + chunk)
    return frames


# --- responses -------------------------------------------------------------

class AckStatus(IntEnum):
    """Last byte of a 5-byte upload ack (BleManager.bleNotifyCallback)."""

    ERROR = 0  # app resets its queues and counts a CRC failure
    NEXT = 1  # frame accepted, send the next one
    NO_SPACE = 2  # alarm upload UI shows "insufficient device space"
    DONE = 3  # whole upload accepted


def parse_ack(data: bytes) -> AckStatus | None:
    if len(data) == 5 and data[0] == 5 and data[1] == 0:
        try:
            return AckStatus(data[4])
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class HardwareInfo:
    mcu_major: int
    mcu_minor: int
    ble_major: int  # WiFi firmware version on WiFi models
    ble_minor: int

    @property
    def mcu_version(self) -> str:
        return f"{self.mcu_major}.{self.mcu_minor:02d}"


def parse_hardware_info(data: bytes) -> HardwareInfo:
    """Reply to hardware_info(): 08 00 05 80 mcuMaj mcuMin bleMaj bleMin."""
    if len(data) < 8 or data[2:4] != b"\x05\x80":
        raise ValueError(f"not a hardware info reply: {data.hex()}")
    return HardwareInfo(*data[4:8])


def parse_device_info(data: bytes) -> DeviceInfo:
    """Decode the reply to sync_time() (ChooseActivity.setGetLedTypeValue)."""
    if len(data) < 5:
        raise ValueError(f"device info reply too short: {data.hex()}")
    type_byte = data[4]
    led_type = _TYPE_BYTE_TO_LED_TYPE.get(type_byte)
    if led_type is None:
        raise ValueError(f"unknown device type byte 0x{type_byte:02x}")
    width, height = LED_SIZES[led_type]
    frame_size = 12288 if type_byte in (0x00, 0x80, 0x81) else 4096
    return DeviceInfo(
        type_byte=type_byte,
        led_type=led_type,
        width=width,
        height=height,
        frame_size=frame_size,
        text_size=_TEXT_SIZE_BY_LED_TYPE.get(led_type, 16),
        password_required=len(data) >= 11 and data[10] == 1,
        raw=bytes(data),
    )
