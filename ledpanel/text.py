"""Render text into the panel text-block format (CommonTextProcess).

Layout of the block uploaded with DataType.TEXT:

    header (14 bytes, ITextProcess.getTextHead)
        count_lo, count_hi, h_align, v_align, effect, speed, color_mode,
        R, G, B, bg_enabled, bgR, bgG, bgB
        (+ border_type, border_speed, border_effect on led types 16-19 and >= 24)
    per character:
        size_type, R, G, B, 1-bit bitmap

Bitmaps are row-major, each row padded to a multiple of 8 pixels, bits packed
LSB-first (TextAgreement.getTextData). Half-width glyphs are height/2 wide.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum

from PIL import Image, ImageDraw, ImageFont

Color = tuple[int, int, int]

_SIZE_TYPE = {16: 1, 32: 3, 48: 5, 64: 7}

# CommonTextProcess: marker byte that precedes an inline emoji JPEG, per text height.
_EMOJI_MARKER = {16: 8, 24: 11, 32: 9, 64: 10}
# NewCommonTextProcess adds 20 px emoji.
_NEW_EMOJI_MARKER = {**_EMOJI_MARKER, 20: 12}


class HAlign(IntEnum):
    LEFT = 0
    CENTER = 1
    RIGHT = 2


class VAlign(IntEnum):
    """App values (UI 3..5 minus 3); not yet confirmed on hardware."""

    TOP = 0
    CENTER = 1
    BOTTOM = 2


class TextEffect(IntEnum):
    """Position in TextEffectFragment's effect list, sent as-is."""

    FIXED = 0
    MOVE_LEFT = 1
    MOVE_RIGHT = 2
    UP = 3
    DOWN = 4
    BLINK = 5
    BREATHE = 6
    SNOWFLAKE = 7
    LASER = 8


# TextActivity adds this to the effect byte when the text direction is vertical.
VERTICAL_FLAG = 0x80

# Header byte 6. 2 and up select the app's preset colour/gradient modes.
COLOR_MODE_PER_CHAR = 0
COLOR_MODE_SOLID = 1


@dataclass(frozen=True)
class Emoji:
    """Inline icon: a JPEG sized to the text height (see ledpanel.Assets.emoji)."""

    jpeg: bytes


@dataclass(frozen=True)
class Span:
    """Text drawn in its own colour; every character carries its own RGB on the wire."""

    text: str
    color: Color


Segment = str | Span | Emoji


def rainbow(text: str, colors: Iterable[Color] | None = None) -> list[Span]:
    """One Span per character, cycling through colors (default: a 7-colour rainbow)."""
    palette = list(colors or [(255, 0, 0), (255, 127, 0), (255, 255, 0), (0, 255, 0),
                              (0, 0, 255), (75, 0, 130), (148, 0, 211)])
    return [Span(c, palette[i % len(palette)]) for i, c in enumerate(text)]


def _size_type(width: int, height: int) -> int:
    t = _SIZE_TYPE.get(height, 1)
    return t - 1 if height // width == 2 else t


def _is_full_width(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def _load_font(font: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if font:
        return ImageFont.truetype(font, size)
    return ImageFont.load_default(size)


def glyph_bitmap(ch: str, width: int, height: int, font) -> bytes:
    """Rasterise one character to the device's packed 1-bit format."""
    img = Image.new("1", (width, height), 0)
    draw = ImageDraw.Draw(img)
    # Every glyph shares one baseline (like Android Paint.drawText), so short
    # letters sit on the line instead of being centred in their cell.
    ascent, descent = font.getmetrics()
    baseline = (height - (ascent + descent)) // 2 + ascent
    x = (width - font.getlength(ch)) / 2
    draw.text((x, baseline), ch, fill=1, font=font, anchor="ls")
    row_bits = (width + 7) // 8 * 8
    out = bytearray(row_bits * height // 8)
    px = img.load()
    for yy in range(height):
        for xx in range(width):
            if px[xx, yy]:
                i = yy * row_bits + xx
                out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def _items(content: str | Iterable[Segment], default: Color) -> list[tuple[str, Color] | Emoji]:
    parts = [content] if isinstance(content, str) else list(content)
    items: list[tuple[str, Color] | Emoji] = []
    for part in parts:
        if isinstance(part, Emoji):
            items.append(part)
        else:
            text, color = (part.text, part.color) if isinstance(part, Span) else (part, default)
            items.extend((c, color) for c in text if c != "\n")
    return items


def build_text_block(content: str | Iterable[Segment], *, height: int = 16,
                     color: Color = (255, 255, 255), background: Color | None = None,
                     gradient: int | None = None,
                     effect: int = TextEffect.FIXED, vertical: bool = False, speed: int = 65,
                     h_align: int = HAlign.LEFT, v_align: int = VAlign.TOP,
                     font: str | None = None, new_format: bool = False,
                     border: tuple[int, int, int] | None = None) -> bytes:
    """Build the payload for a DataType.TEXT upload.

    content is a string or a mix of strings, Span (per-run colour) and Emoji
    icons; plain strings use `color`, which is also the header colour. speed is 1-100
    (the app's slider; default 65).
    gradient 1-8 selects one of the firmware's gradient presets (overrides colours).
    new_format uses NewCommonTextProcess's variable-width records, which the app
    only sends to some MCU versions (see protocol.uses_new_text_format).
    border (type, speed, effect) is only understood by led types 16-19 and >= 24.
    """
    if gradient is not None:
        # GradientColor: mode = preset + 1 (2..9); RGB fields carry (0, 0, mode).
        mode = _byte_range(gradient, "gradient", 1, 8) + 1
        color = (0, 0, mode)
        content = _plain_text(content)
    items = _items(content, color)
    if not items:
        raise ValueError("text must not be empty")
    if len(items) > 0xFFFF:
        raise ValueError("text too long")
    markers = _NEW_EMOJI_MARKER if new_format else _EMOJI_MARKER
    if any(isinstance(i, Emoji) for i in items) and height not in markers:
        raise ValueError(f"emoji need a text height of {sorted(markers)}")
    if not new_format and height not in _SIZE_TYPE:
        raise ValueError(f"classic text format supports heights {sorted(_SIZE_TYPE)}")
    effect_byte = int(effect) | (VERTICAL_FLAG if vertical else 0)
    if gradient is not None:
        color_mode = gradient + 1
    else:
        # Colour mode 0 makes the panel use each character's own RGB (verified on a
        # 64x20 panel); mode 1 paints every character with the header colour.
        per_char = any(not isinstance(i, Emoji) and i[1] != color for i in items)
        color_mode = COLOR_MODE_PER_CHAR if per_char else COLOR_MODE_SOLID
    fnt = _load_font(font, height)
    bg = background or (0, 0, 0)
    header = bytes([
        len(items) & 0xFF, len(items) >> 8, int(h_align), int(v_align), effect_byte, speed,
        color_mode, *color, 1 if background else 0, *bg,
    ])
    if border:
        header += bytes(border)
    body = bytearray()
    for item in items:
        if isinstance(item, Emoji):
            n = len(item.jpeg)
            body += bytes([markers[height], n & 0xFF, (n >> 8) & 0xFF, n >> 16])
            body += item.jpeg
            continue
        ch, ch_color = item
        if new_format:
            # NewCommonTextProcess: [0x80, R, G, B, w, h]; proportional width
            # (ceil(measureText) + 1) like getCharMeasureWidthBitmap.
            width = height if _is_full_width(ch) else min(255, math.ceil(fnt.getlength(ch)) + 1)
            body += bytes([0x80, *ch_color, width, height])
        else:
            width = height if _is_full_width(ch) else height // 2
            body += bytes([_size_type(width, height), *ch_color])
        body += glyph_bitmap(ch, width, height, fnt)
    return header + bytes(body)


def _byte_range(value: int, name: str, lo: int, hi: int) -> int:
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be in [{lo}, {hi}], got {value}")
    return value


def _plain_text(content: str | Iterable[Segment]) -> list[str | Emoji]:
    parts = [content] if isinstance(content, str) else list(content)
    return [p.text if isinstance(p, Span) else p for p in parts]
