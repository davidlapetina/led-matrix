"""Frames are checked against the byte arrays hard-coded in the decompiled app."""

import datetime as dt
import struct
import zlib

import pytest

from ledpanel import protocol as p
from ledpanel.text import Emoji, Span, TextEffect, build_text_block, glyph_bitmap, rainbow


def test_simple_commands_match_app_arrays():
    assert p.power(True) == bytes([5, 0, 7, 1, 1])
    assert p.power(False) == bytes([5, 0, 7, 1, 0])
    assert p.brightness(50) == bytes([5, 0, 4, 0x80, 50])
    assert p.upside_down(True) == bytes([5, 0, 6, 0x80, 1])
    assert p.delete_all() == bytes([4, 0, 3, 0x80])
    assert p.exit_mode() == bytes([4, 0, 1, 1])
    assert p.hardware_info() == bytes([4, 0, 5, 0x80])
    assert p.text_speed(7) == bytes([5, 0, 3, 1, 7])
    assert p.set_weekday(3) == bytes([5, 0, 0x12, 0x80, 3])
    assert p.countdown(p.TimerAction.START, 5, 30) == bytes([7, 0, 0x0D, 0x80, 1, 5, 30])
    assert p.stopwatch(p.TimerAction.PAUSE) == bytes([5, 0, 9, 0x80, 2])
    assert p.rhythm(3, 1) == bytes([6, 0, 0, 2, 3, 1])
    assert p.diy_mode(p.DiyMode.ENTER_CLEAR_CUR_SHOW) == bytes([5, 0, 4, 1, 1])
    assert p.set_playlist([4]) == bytes([7, 0, 8, 0x80, 1, 0, 4])


def test_sync_time():
    now = dt.datetime(2026, 10, 22, 18, 5, 9)
    assert p.sync_time(now) == bytes([8, 0, 1, 0x80, 18, 5, 9, 0])


def test_clock_mode_layout():
    f = p.clock_mode(2, hour_24=False, show_date=True, date=dt.date(2026, 10, 22))
    # Thursday -> 4 (Monday=1 .. Sunday=7)
    assert f == bytes([11, 0, 6, 1, 2, 0, 1, 26, 10, 22, 4])


def test_scoreboard_little_endian():
    assert p.scoreboard(258, 3) == bytes([8, 0, 0x0A, 0x80, 2, 1, 3, 0])


def test_delete_slots():
    assert p.delete_slots([1, 5]) == bytes([8, 0, 2, 1, 2, 0, 1, 5])


def test_password_frames():
    assert p.verify_password("123456") == bytes([7, 0, 5, 2, 12, 34, 56])
    assert p.verify_password("1234", length=4) == bytes([7, 0, 5, 2, 12, 34, 0])
    assert p.set_password(True, "123456") == bytes([8, 0, 4, 2, 1, 12, 34, 56])
    with pytest.raises(ValueError):
        p.verify_password("12a456")


def test_rhythm_bars_scaling():
    f = p.rhythm_bars(1, [0, 255, 10, 128])
    assert f[:5] == bytes([16, 0, 1, 2, 1])
    assert list(f[5:]) == [1, 15, 1, 7] + [1] * 7  # clamped to 1..15 like the app


def test_diy_pixels():
    f = p.diy_pixels((255, 0, 16), [(3, 4), (5, 6)])
    assert f == bytes([12, 0, 5, 1, 0, 255, 0, 16, 3, 4, 5, 6])


def test_bulk_single_frame_header():
    data = b"\x89PNG fake"
    [fr] = p.bulk_frames(p.DataType.IMAGE, data)
    length, t0, t1, opt, size, crc = struct.unpack_from("<HBBBII", fr)
    assert length == len(fr) == len(data) + 15
    assert (t0, t1, opt, size) == (2, 0, 0, len(data))
    assert crc == zlib.crc32(data)
    assert fr[13:15] == bytes([0, p.SLOT_TEMPORARY])
    assert fr[15:] == data


def test_bulk_multi_frame_gif():
    data = bytes(range(256)) * 100  # 25600 bytes -> 3 frames of <= 12288
    frames = p.bulk_frames(p.DataType.GIF, data, slot=3)
    assert len(frames) == 3
    assert [f[4] for f in frames] == [0, 2, 2]
    for f in frames:
        assert f[2:4] == bytes([3, 0])
        assert struct.unpack_from("<I", f, 5)[0] == len(data)
        assert f[13:15] == bytes([2, 3])
    assert b"".join(f[15:] for f in frames) == data


def test_text_wire_type():
    [fr] = p.bulk_frames(p.DataType.TEXT, b"x")
    assert fr[2:4] == bytes([0, 1])


def test_parse_device_info():
    info = p.parse_device_info(bytes([11, 0, 1, 0x80, 0x81, 0, 0, 0, 0, 0, 1]))
    assert (info.width, info.height, info.frame_size) == (32, 32, 12288)
    assert info.password_required
    info = p.parse_device_info(bytes([9, 0, 1, 0x80, 0x86, 0, 0, 0, 0]))
    assert (info.width, info.height, info.frame_size, info.text_size) == (128, 32, 4096, 32)


def test_parse_ack():
    assert p.parse_ack(bytes([5, 0, 2, 0, 1])) is p.AckStatus.NEXT
    assert p.parse_ack(bytes([5, 0, 3, 0, 3])) is p.AckStatus.DONE
    assert p.parse_ack(bytes([9, 0, 1, 0x80, 0x81])) is None


def test_glyph_bitmap_packing():
    from PIL import ImageFont

    bits = glyph_bitmap("I", 8, 16, ImageFont.load_default(16))
    assert len(bits) == 16  # 8 px rows -> 1 byte per row
    assert any(bits)


def test_text_block_layout():
    block = build_text_block("Hi", height=16, color=(255, 0, 0))
    assert block[:2] == bytes([2, 0])
    assert block[6:10] == bytes([1, 255, 0, 0])
    assert block[10] == 0  # no background
    # each half-width 8x16 glyph: 4-byte prefix + 16 bitmap bytes
    assert len(block) == 14 + 2 * (4 + 16)
    assert block[14:18] == bytes([0, 255, 0, 0])  # size type 0 = 16 high, half width


def test_per_letter_colours():
    block = build_text_block(["A", Span("B", (0, 255, 0)), "C"], color=(255, 0, 0))
    glyph = 4 + 16  # prefix + 8x16 bitmap
    prefixes = [block[14 + i * glyph:14 + i * glyph + 4] for i in range(3)]
    assert prefixes == [bytes([0, 255, 0, 0]), bytes([0, 0, 255, 0]), bytes([0, 255, 0, 0])]
    assert block[7:10] == bytes([255, 0, 0])  # header keeps the default colour
    assert block[6] == 0  # mixed colours -> per-character colour mode
    assert build_text_block("ABC", color=(255, 0, 0))[6] == 1  # single colour -> solid


def test_rainbow_cycles_palette():
    spans = rainbow("abc", [(1, 2, 3), (4, 5, 6)])
    assert [s.color for s in spans] == [(1, 2, 3), (4, 5, 6), (1, 2, 3)]


def test_effect_and_vertical_flag():
    block = build_text_block("A", effect=TextEffect.SNOWFLAKE, speed=80)
    assert block[4:6] == bytes([7, 80])
    assert build_text_block("A", effect=TextEffect.UP, vertical=True)[4] == 0x83


def test_inline_emoji_entry():
    jpeg = b"\xff\xd8fakejpeg\xff\xd9"
    block = build_text_block(["A", Emoji(jpeg)], height=16)
    assert block[:2] == bytes([2, 0])
    entry = block[14 + 20:]
    assert entry[:4] == bytes([8, len(jpeg), 0, 0]) and entry[4:] == jpeg
    with pytest.raises(ValueError):
        build_text_block([Emoji(jpeg)], height=48)


def test_alarm_frames_layout():
    data = b"GIF89a" + bytes(100)
    [f] = p.alarm_frames(2, 7, 30, p.AlarmMedia.GIF, data, days=[1, 5], duration=60)
    assert struct.unpack_from("<H", f)[0] == len(f) == len(data) + 24
    assert f[2:12] == bytes([0, 0x80, 2, 0b100011, 7, 30, 60, 0, 1, 1])
    assert f[12] == 0  # option: first chunk
    assert struct.unpack_from("<II", f, 13) == (len(data), zlib.crc32(data))
    assert f[21:24] == bytes([0, 0, 122]) and f[24:] == data
    frames = p.alarm_frames(0, 7, 30, p.AlarmMedia.PNG, bytes(9000), frame_size=4096)
    assert [x[12] for x in frames] == [0, 2, 2]
    with pytest.raises(ValueError):
        p.alarm_frames(0, 7, 30, p.AlarmMedia.PNG, data, duration=45)


def test_week_byte_and_timer_switch():
    assert p.week_byte([]) == 1 and p.week_byte([7], enabled=False) == 0x80
    assert p.timer_switch(True, 3, 22, 15, [1, 2]) == bytes([9, 0, 0x11, 0x80, 1, 3, 0b111, 22, 15])


def test_playlist_and_remote():
    assert p.set_playlist([1, 2, 3]) == bytes([9, 0, 8, 0x80, 3, 0, 1, 2, 3])
    assert p.remote_preset(5, chinese=True) == bytes([6, 0, 7, 0x80, 5, 1])


def test_sport_frames():
    assert p.sport(p.SportMode.SOS) == bytes([7, 0, 6, 0, 4, 0, 0])
    assert p.sport(p.SportMode.SPEEDOMETER, 23.7) == bytes([7, 0, 6, 0, 6, 23, 7])
    assert p.sport(p.SportMode.LEFT_TURN, 50) == bytes([7, 0, 6, 0, 0, 0, 0])


def test_raw_frames():
    rgb = bytes(64 * 20 * 3)
    [f] = p.raw_frames(rgb, 4096)
    assert f[:9] == struct.pack("<HBBBI", 9 + len(rgb), 0, 0, 0, 4096) and f[9:] == rgb
    frames = p.raw_frames(bytes(30000), 12288)
    assert [x[4] for x in frames] == [0, 2, 2] and len(frames[0]) == 9 + 12288


def test_hardware_info_and_text_format_choice():
    hw = p.parse_hardware_info(bytes.fromhex("080005800f160105"))
    assert (hw.mcu_major, hw.mcu_minor, hw.mcu_version) == (15, 22, "15.22")
    assert not p.uses_new_text_format(15, 22)
    assert p.uses_new_text_format(18, 50) and p.uses_new_text_format(30, 0)


def test_gradient_text_header():
    block = build_text_block(["ab", Span("c", (1, 2, 3))], gradient=3)
    assert block[6:10] == bytes([4, 0, 0, 4])
    assert block[14:18] == bytes([0, 0, 0, 4])  # every char carries the preset too


def test_new_format_records():
    block = build_text_block("Hi", height=20, new_format=True)
    rec = block[14:]
    assert rec[0] == 0x80 and rec[4:6] == bytes([rec[4], 20])
    width = rec[4]
    assert len(rec) == 2 * 6 + ((width + 7) // 8) * 20 + ((rec[6 + (width + 7) // 8 * 20 + 4] + 7) // 8) * 20
    with pytest.raises(ValueError):
        build_text_block("Hi", height=20)  # classic format has no 20 px size type


def test_scrolling_text_gif():
    from PIL import Image
    import io
    from ledpanel.render import scrolling_text_gif

    gif = scrolling_text_gif("Hi", (64, 20), step=8, blink_period=4)
    img = Image.open(io.BytesIO(gif))
    assert img.size == (64, 20) and img.n_frames > 5
