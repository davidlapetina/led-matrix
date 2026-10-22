"""Render animations on the computer and upload them as GIFs.

The app does the same for "creative text" (text over a background GIF and a
border PNG, composited on the phone) and for DIY animations: the panel only
ever receives a finished GIF.
"""

from __future__ import annotations

import datetime as _dt
import io
import math
from collections.abc import Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageSequence

from .text import Color, Emoji, Segment, Span, _load_font


def _emoji_image(emoji: Emoji, height: int) -> Image.Image:
    """The icon as RGBA; its black JPEG backdrop becomes transparent so it sits on backgrounds."""
    img = Image.open(io.BytesIO(emoji.jpeg)).convert("RGB")
    if img.height > height:
        img = img.resize((max(1, img.width * height // img.height), height), Image.NEAREST)
    alpha = img.convert("L").point(lambda v: 255 if v > 24 else 0)
    img.putalpha(alpha)
    return img


def _runs(content: str | Iterable[Segment], default: Color,
          height: int) -> list[tuple[str, Color] | Image.Image]:
    parts = [content] if isinstance(content, str) else list(content)
    runs = []
    for part in parts:
        if isinstance(part, Emoji):
            runs.append(_emoji_image(part, height))
        else:
            runs.append((part.text, part.color) if isinstance(part, Span) else (part, default))
    return runs


def text_strip(content: str | Iterable[Segment], height: int, *,
               color: Color = (255, 255, 255), font: str | None = None,
               font_size: int | None = None) -> Image.Image:
    """Draw the whole text (and inline emoji) on one transparent RGBA strip, baseline-aligned."""
    fnt = _load_font(font, font_size or height)
    runs = _runs(content, color, height)

    def advance(run) -> float:
        return run.width if isinstance(run, Image.Image) else fnt.getlength(run[0])

    width = max(1, int(sum(advance(r) for r in runs)) + 1)
    strip = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(strip)
    ascent, descent = fnt.getmetrics()
    baseline = (height - (ascent + descent)) // 2 + ascent
    x = 0.0
    for run in runs:
        if isinstance(run, Image.Image):
            strip.alpha_composite(run, (round(x), (height - run.height) // 2))
        else:
            text, c = run
            draw.text((x, baseline), text, fill=(*c, 255), font=fnt, anchor="ls")
        x += advance(run)
    return strip


def _frames_of(gif: bytes | None, size: tuple[int, int]) -> list[Image.Image]:
    if not gif:
        return [Image.new("RGBA", size, (0, 0, 0, 255))]
    src = Image.open(io.BytesIO(gif))
    return [f.convert("RGBA").resize(size) for f in ImageSequence.Iterator(src)]


FIT_MODES = ("contain", "cover", "stretch")


def fit_picture(img: Image.Image, size: tuple[int, int], fit: str = "contain") -> Image.Image:
    """Fit any picture to the panel as RGB: upright (EXIF), transparency over black.

    contain: whole picture, black bars; cover: fill the panel, cropping the
    overflow; stretch: fill the panel, ignoring the aspect ratio."""
    if fit not in FIT_MODES:
        raise ValueError(f"fit must be one of {FIT_MODES}")
    img = ImageOps.exif_transpose(img)
    rgba = img.convert("RGBA")
    flat = Image.new("RGB", rgba.size, (0, 0, 0))
    flat.paste(rgba, mask=rgba.getchannel("A"))
    if flat.size == size:
        return flat
    if fit == "contain":
        return ImageOps.pad(flat, size, Image.LANCZOS, color=(0, 0, 0))
    if fit == "cover":
        return ImageOps.fit(flat, size, Image.LANCZOS)
    return flat.resize(size, Image.LANCZOS)


def animation_gif(data: bytes, size: tuple[int, int], fit: str = "contain") -> bytes:
    """Any animation Pillow reads (GIF, animated WebP, APNG) as a panel-sized GIF,
    keeping each frame's own duration."""
    src = Image.open(io.BytesIO(data))
    frames, delays = [], []
    for frame in ImageSequence.Iterator(src):
        frames.append(fit_picture(frame, size, fit))
        delays.append(max(20, int(frame.info.get("duration") or src.info.get("duration") or 100)))
    return encode_gif(frames, delays)


def _shared_palette(frames: list[Image.Image]) -> Image.Image:
    """One 256-colour palette for the whole animation, from up to 64 sampled frames."""
    sample = frames[::max(1, len(frames) // 64)][:64]
    w, h = sample[0].size
    sheet = Image.new("RGB", (w, h * len(sample)))
    for k, f in enumerate(sample):
        sheet.paste(f, (0, k * h))
    return sheet.quantize(256, method=Image.Quantize.MEDIANCUT)


def _encode(frames: list[Image.Image], delays: list[int]) -> bytes:
    # The panel's decoder stalls on the first frame whose local colour table is bigger
    # than the global one (it then loops the frames before it). Like the app's own GIFs,
    # use a single global palette and no per-frame tables.
    rgb = [f.convert("RGB") for f in frames]
    palette = _shared_palette(rgb)
    indexed = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in rgb]
    buf = io.BytesIO()
    indexed[0].save(buf, format="GIF", save_all=True, append_images=indexed[1:], loop=0,
                    duration=delays, disposal=1, optimize=False)
    return buf.getvalue()


def encode_gif(frames: list[Image.Image], delay_ms: int | list[int]) -> bytes:
    """Encode RGB(A) frames as a looping GIF the panel can play (one delay, or one per frame)."""
    delays = list(delay_ms) if isinstance(delay_ms, list) else [delay_ms] * len(frames)
    return _encode(frames, delays)


def scrolling_text_gif(content: str | Iterable[Segment], size: tuple[int, int], *,
                       color: Color = (255, 255, 255), direction: str = "left",
                       step: int = 1, blink_period: int | None = None,
                       background: bytes | None = None, border: bytes | None = None,
                       font: str | None = None, font_size: int | None = None,
                       delay_ms: int = 50) -> bytes:
    """Text that scrolls across the panel, optionally blinking at the same time.

    blink_period: frames per on/off cycle (e.g. 10 = 5 frames on, 5 off).
    background: a GIF (e.g. from Assets().backgrounds(w, h)) looped behind the text.
    border: an RGBA PNG (e.g. from Assets().borders(w, h)) drawn on top.
    direction: "left" (enters right, exits left), "right", or "none" (centred).
    """
    w, h = size
    strip = text_strip(content, h, color=color, font=font, font_size=font_size)
    bg_frames = _frames_of(background, size)
    border_img = Image.open(io.BytesIO(border)).convert("RGBA").resize(size) if border else None
    if direction == "none":
        positions = [(w - strip.width) // 2] * max(len(bg_frames), blink_period or 1)
    else:
        travel = list(range(w, -strip.width - 1, -step))
        positions = travel if direction == "left" else travel[::-1]
    frames = []
    for i, x in enumerate(positions):
        frame = bg_frames[i % len(bg_frames)].copy()
        visible = blink_period is None or (i % blink_period) < blink_period / 2
        if visible:
            frame.paste(strip, (x, 0), strip)  # mask = strip alpha; works off-screen
        if border_img is not None:
            frame.alpha_composite(border_img)
        frames.append(frame)
    return encode_gif(frames, delay_ms)


def _led_boost(img: Image.Image, gamma: float = 0.55) -> Image.Image:
    """Lift dark colours, keeping their hue: LEDs show navy or dark green almost black,
    so dark logos vanish against the panel's black background."""
    h, s, v = img.convert("RGB").convert("HSV").split()
    v = v.point(lambda x: round(255 * (x / 255) ** gamma))
    out = Image.merge("HSV", (h, s, v)).convert("RGBA")
    out.putalpha(img.getchannel("A"))
    return out


def _trimmed_logo(png: bytes) -> Image.Image:
    """A team logo cropped to its clearly visible pixels (faint glow and sparkles ignored)."""
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    bbox = img.getchannel("A").point(lambda a: 255 if a > 64 else 0).getbbox()
    return img.crop(bbox) if bbox else img


def _logo_at(img: Image.Image, height: int, max_width: int) -> Image.Image:
    """Scale a trimmed logo to the given height (narrower if it would exceed max_width)."""
    width = max(1, round(img.width * height / img.height))
    if width > max_width:
        width, height = max_width, max(1, round(img.height * max_width / img.width))
    return _led_boost(img.resize((width, height), Image.LANCZOS))


# 4x7 LED digits (plus the few symbols weather needs): TrueType fonts below ~12 px render malformed digits (a "6" reads as "8").
_DIGITS = {
    "0": ".##.|#..#|#..#|#..#|#..#|#..#|.##.", "1": "..#.|.##.|..#.|..#.|..#.|..#.|.###",
    "2": ".##.|#..#|...#|..#.|.#..|#...|####", "3": ".##.|#..#|...#|..#.|...#|#..#|.##.",
    "4": "..#.|.##.|#.#.|#.#.|####|..#.|..#.", "5": "####|#...|###.|...#|...#|#..#|.##.",
    "6": ".##.|#...|#...|###.|#..#|#..#|.##.", "7": "####|...#|..#.|..#.|.#..|.#..|.#..",
    "8": ".##.|#..#|#..#|.##.|#..#|#..#|.##.", "9": ".##.|#..#|#..#|.###|...#|...#|.##.",
    "-": "...|...|...|###|...|...|...", ":": ".|.|#|.|#|.|.",
    "°": ".#.|#.#|.#.|...|...|...|...", "%": "##...|##..#|...#.|..#..|.#...|#..##|...##",
    "/": "...#|...#|..#.|..#.|.#..|.#..|#...", "k": "#...|#...|#..#|#.#.|##..|#.#.|#..#",
    "m": ".....|.....|##.#.|#.#.#|#.#.#|#.#.#|#.#.#", "h": "#...|#...|###.|#..#|#..#|#..#|#..#",
    "p": "....|....|###.|#..#|###.|#...|#...",
}


def _pixel_text(text: str, scale: int = 1) -> Image.Image | None:
    """text in the 4x7 LED digit font as a 1-bit image, or None if a character is missing."""
    if not text or any(ch not in _DIGITS for ch in text):
        return None
    glyphs = [_DIGITS[ch].split("|") for ch in text]
    width = sum(len(g[0]) for g in glyphs) + len(glyphs) - 1
    img = Image.new("1", (width, 7))
    x = 0
    for g in glyphs:
        for y, row in enumerate(g):
            for dx, bit in enumerate(row):
                if bit == "#":
                    img.putpixel((x + dx, y), 1)
        x += len(g[0]) + 1
    return img.resize((width * scale, 7 * scale), Image.NEAREST) if scale > 1 else img


def _fit_font(text: str, width: int, height: int, font: str | None):
    """The largest font whose rendering of text fits width x height (at least 6 px)."""
    for size in range(height, 5, -1):
        fnt = _load_font(font, size)
        left, top, right, bottom = fnt.getbbox(text)
        if right - left <= width and bottom - top <= height:
            return fnt, size
    return _load_font(font, 6), 6


def score_card(left_logo: bytes, right_logo: bytes, text: str | tuple[str, str],
               size: tuple[int, int], *, color: Color = (255, 255, 255),
               font: str | None = None) -> Image.Image:
    """Logo1 score1-score2 Logo2 on one line at panel size; text is the two scores or
    one string (e.g. a kick-off time).

    Both logos get the same height, so a wide logo is not shrunk next to a round one
    (a wide one may take up to 1.6x its height in width). A short score ("2-1") gets a
    large smooth font with full-height logos; a wider one ("132-125") uses the crisp
    4x7 LED digits and the logos take the height that is left (at least 60 %). The
    text is centred between the logos, without anti-aliasing so it stays crisp."""
    w, h = size
    line = text if isinstance(text, str) else f"{text[0]}-{text[1]}"
    fill = (*color, 255)
    logos = (_trimmed_logo(left_logo), _trimmed_logo(right_logo))
    min_height = max(6, round(h * 0.6))

    def sized(height: int) -> tuple[Image.Image, Image.Image]:
        return tuple(_logo_at(img, height, round(height * 1.6)) for img in logos)

    def compose(left: Image.Image, right: Image.Image, draw_text) -> Image.Image:
        card = Image.new("RGBA", size, (0, 0, 0, 255))
        card.alpha_composite(left, (0, (h - left.height) // 2))
        card.alpha_composite(right, (w - right.width, (h - right.height) // 2))
        draw_text(card, left.width, w - right.width)
        return card.convert("RGB")

    def smooth(fnt):
        def draw_text(card, x0, x1):
            draw = ImageDraw.Draw(card)
            draw.fontmode = "1"
            draw.text(((x0 + x1) / 2, h / 2), line, fill=fill, font=fnt, anchor="mm")
        return draw_text

    left, right = sized(h)
    fnt, fs = _fit_font(line, w - left.width - right.width - 2, h, font)
    if fs >= h * 0.6:
        return compose(left, right, smooth(fnt))
    for scale in range(h // 7, 0, -1):
        glyphs = _pixel_text(line, scale)
        if glyphs is None:
            break
        for height in range(h, min_height - 1, -1):
            left, right = sized(height)
            if left.width + right.width + glyphs.width + 2 <= w:
                def led(card, x0, x1, glyphs=glyphs):
                    pos = (x0 + (x1 - x0 - glyphs.width) // 2, (h - glyphs.height) // 2)
                    card.paste(Image.new("RGBA", glyphs.size, fill), pos, glyphs)
                return compose(left, right, led)
    # Not digits, or too wide even for the LED font: smallest logos, smooth font.
    left, right = sized(min_height)
    fnt, _ = _fit_font(line, w - left.width - right.width - 2, h, font)
    return compose(left, right, smooth(fnt))


# --- weather ------------------------------------------------------------------

_SUN, _MOON, _CLOUD, _DARK_CLOUD = (255, 200, 0), (255, 240, 170), (225, 230, 240), (150, 155, 170)
_RAIN, _SNOW, _BOLT = (60, 150, 255), (255, 255, 255), (255, 220, 0)
_DROP = "..#..|..#..|.###.|.###.|#####|#####|.###."
_WIND = "....##.|......#|######.|.......|#####..|.....#.|....#.."


def weather_icon(kind: str, is_day: bool, size: int) -> Image.Image:
    """A size x size RGBA weather icon: clear, partly, cloudy, fog, drizzle, rain, snow or
    thunder (moon instead of sun at night). Drawn at 4x and scaled down, so it stays
    clean at any panel height."""
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = lambda *v: [round(x * big) for x in v]
    stroke = max(1, round(big * 0.07))

    def sun(cx, cy, r):
        for k in range(8):
            a = k * math.pi / 4
            d.line(u(cx + math.cos(a) * r * 1.5, cy + math.sin(a) * r * 1.5,
                     cx + math.cos(a) * r * 2.1, cy + math.sin(a) * r * 2.1), fill=_SUN, width=stroke)
        d.ellipse(u(cx - r, cy - r, cx + r, cy + r), fill=_SUN)

    def moon(cx, cy, r):
        d.ellipse(u(cx - r, cy - r, cx + r, cy + r), fill=_MOON)
        d.ellipse(u(cx - 0.3 * r, cy - 1.3 * r, cx + 1.7 * r, cy + 0.7 * r), fill=(0, 0, 0, 0))

    def sky(cx, cy, r):
        (sun if is_day else moon)(cx, cy, r)

    def cloud(top, color):  # top: y of the cloud's highest point
        for cx, cy, r in ((0.30, top + 0.20, 0.15), (0.52, top + 0.13, 0.20), (0.74, top + 0.22, 0.14)):
            d.ellipse(u(cx - r, cy - r, cx + r, cy + r), fill=color)
        d.rounded_rectangle(u(0.14, top + 0.20, 0.88, top + 0.36), radius=round(big * 0.08), fill=color)

    if kind == "clear":
        sky(0.5, 0.5, 0.2 if is_day else 0.3)
    elif kind == "partly":
        sky(0.36, 0.34, 0.15 if is_day else 0.2)
        cloud(0.42, _CLOUD)
    elif kind == "cloudy":
        cloud(0.28, _CLOUD)
    elif kind == "fog":
        for k, y in enumerate((0.3, 0.45, 0.6, 0.75)):
            x0 = 0.1 if k % 2 == 0 else 0.2
            d.rounded_rectangle(u(x0, y, x0 + 0.7, y + 0.07), radius=stroke, fill=_DARK_CLOUD)
    elif kind in ("drizzle", "rain"):
        cloud(0.12, _DARK_CLOUD if kind == "rain" else _CLOUD)
        xs = (0.3, 0.5, 0.7) if kind == "rain" else (0.36, 0.64)
        for k, x in enumerate(xs):
            y = 0.58 + 0.08 * (k % 2)
            d.line(u(x, y, x - 0.06, y + (0.25 if kind == "rain" else 0.14)), fill=_RAIN, width=stroke)
    elif kind == "snow":
        cloud(0.12, _CLOUD)
        for x, y in ((0.3, 0.62), (0.52, 0.72), (0.72, 0.62), (0.4, 0.86), (0.64, 0.86)):
            d.ellipse(u(x - 0.05, y - 0.05, x + 0.05, y + 0.05), fill=_SNOW)
    elif kind == "thunder":
        cloud(0.1, _DARK_CLOUD)
        d.polygon(u(0.55, 0.5, 0.36, 0.74, 0.5, 0.74, 0.42, 0.97, 0.68, 0.64, 0.53, 0.64, 0.62, 0.5),
                  fill=_BOLT)
    else:
        raise ValueError(f"unknown weather kind {kind!r}")
    return img.resize((size, size), Image.LANCZOS)


def _temp_color(temp: float, fahrenheit: bool) -> Color:
    c = (temp - 32) * 5 / 9 if fahrenheit else temp
    for limit, color in ((0, (120, 190, 255)), (10, (90, 230, 255)), (20, (150, 255, 150)),
                         (28, (255, 210, 60))):
        if c < limit:
            return color
    return (255, 110, 60)


def _mono(pattern: str, color: Color) -> Image.Image:
    rows = pattern.split("|")
    img = Image.new("RGBA", (len(rows[0]), len(rows)), (0, 0, 0, 0))
    for y, row in enumerate(rows):
        for x, bit in enumerate(row):
            if bit == "#":
                img.putpixel((x, y), (*color, 255))
    return img


def _glyph_run(parts: list[Image.Image | tuple[str, Color]], scale: int = 1) -> Image.Image:
    """Icons and coloured LED-font strings laid out left to right, 1 px apart, as one strip."""
    pieces = []
    for part in parts:
        if isinstance(part, tuple):
            text, color = part
            mask = _pixel_text(text, scale)
            piece = Image.new("RGBA", mask.size, (0, 0, 0, 0))
            piece.paste(Image.new("RGBA", mask.size, (*color, 255)), (0, 0), mask)
        else:
            piece = part
        pieces.append(piece)
    width = sum(p.width for p in pieces) + len(pieces) - 1
    strip = Image.new("RGBA", (width, max(p.height for p in pieces)), (0, 0, 0, 0))
    x = 0
    for p in pieces:
        strip.alpha_composite(p, (x, (strip.height - p.height) // 2))
        x += p.width + 1
    return strip


WEATHER_PAGES = ("now", "time", "details")


def weather_card(w, page: str, size: tuple[int, int], *, now: _dt.datetime | None = None,
                 hour_24: bool = True) -> Image.Image:
    """One page of the weather display (w is an ledpanel.weather.Weather):

    now: icon, temperature (coloured from cold blue to hot red), today's high/low;
    time: icon and the location's local time; details: rain chance and wind."""
    width, h = size
    card = Image.new("RGBA", size, (0, 0, 0, 255))
    scale = max(1, h // 9)

    def right_of_icon(strip: Image.Image) -> None:
        x0 = h + 1
        card.alpha_composite(strip, (x0 + max(0, (width - x0 - strip.width) // 2),
                                     (h - strip.height) // 2))

    if page in ("now", "time"):
        card.alpha_composite(weather_icon(w.kind, w.is_day, h), (0, 0))
    if page == "now":
        room = width - h - 1
        temp = (f"{round(w.temp)}°", _temp_color(w.temp, w.fahrenheit))
        for s in range(scale, 0, -1):
            main = _glyph_run([temp], s)
            hilo = _glyph_run([(str(round(w.high)), (255, 120, 90))])
            lo = _glyph_run([(str(round(w.low)), (110, 170, 255))])
            column = Image.new("RGBA", (max(hilo.width, lo.width), hilo.height + lo.height + 2),
                               (0, 0, 0, 0))
            column.alpha_composite(hilo, (column.width - hilo.width, 0))
            column.alpha_composite(lo, (column.width - lo.width, hilo.height + 2))
            if main.width + 3 + column.width <= room and column.height <= h:
                both = Image.new("RGBA", (main.width + 3 + column.width,
                                          max(main.height, column.height)), (0, 0, 0, 0))
                both.alpha_composite(main, (0, (both.height - main.height) // 2))
                both.alpha_composite(column, (main.width + 3, (both.height - column.height) // 2))
                right_of_icon(both)
                break
            if main.width <= room:
                right_of_icon(main)
                break
    elif page == "time":
        now = now or w.local_now()
        text = f"{now:%H:%M}" if hour_24 else f"{now:%I:%M}".lstrip("0")
        for s in range(scale, 0, -1):
            strip = _glyph_run([(text, (255, 255, 255))], s)
            if strip.width <= width - h - 1:
                right_of_icon(strip)
                break
    elif page == "details":
        rows = []
        if w.rain_chance is not None:
            rows.append(_glyph_run([_mono(_DROP, _RAIN), (f"{w.rain_chance}%", (255, 255, 255))]))
        rows.append(_glyph_run([_mono(_WIND, (200, 220, 255)),
                                (f"{round(w.wind)}{w.wind_unit}", (255, 255, 255))]))
        gap = 2
        total = sum(r.height for r in rows) + gap * (len(rows) - 1)
        y = (h - total) // 2
        for r in rows:
            card.alpha_composite(r, ((width - r.width) // 2, y))
            y += r.height + gap
    else:
        raise ValueError(f"page must be one of {WEATHER_PAGES}")
    return card.convert("RGB")
