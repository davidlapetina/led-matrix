# LED Panel Studio: control Bluetooth LED matrix panels from Python and a web UI

A Python library (`ledpanel`) and a browser control panel (`ledpanel-web`) for the cheap Bluetooth LE RGB LED matrix panels sold under many brands. Use them for signs, car and backpack displays, desktop pixel boards and the like. Everything the phone app does is available, plus extras such as live sports scores and weather. No phone app or cloud account is needed.

## Which panels it works with

Panels that speak the protocol described below. In practice that means:

- **Bluetooth:** the panel advertises a name containing `LED_BLE` and has GATT service `0x00FA`, with write characteristic `0xFA02`.
- **Chipset:** the panels also expose the JieLi OTA service (`AE00`/`AE01`/`AE02`), so their Bluetooth controller is a JieLi (Zhuhai Jieli) BLE SoC. The exact part number is not reported over the air.
- **Firmware:** the panel reports two versions, MCU and BLE (`hardware_info`, `04 00 05 80`). The library adapts to the MCU major version:
  - Version 4 and below shows content immediately from slot 101.
  - Above 4, content is stored in slot 100 and made the only slot playing, as the app does on that firmware.
- **Sizes:** the device type byte in the connection reply selects one of the app's panel sizes. Examples are 16×16, 32×16, 32×32, 48×24, 64×16, 64×20, 64×32 and 64×64, up to 640×64 (`protocol.LED_SIZES`). Text height and upload block size follow the type.
- **Example:** the panels controlled by the **iPixel Color** Android/iOS app (`com.wifiled.ipixels`). The protocol was reverse engineered from that app. It is packed with Baidu protect, so its code was unpacked by dumping the dex files from a running emulator. The source file and method names quoted below are taken from the decompiled app.
- **Tested:** a 64×20 panel with MCU firmware 15.22. Other sizes and firmware are supported from the app's tables but untested. If yours advertises `LED_BLE`, `python -m ledpanel scan` and `info` will tell you its size and firmware.

## Installation

Requirements:
- Python 3.10 or newer.
- A computer with Bluetooth LE: macOS, Linux (BlueZ) or Windows 10/11. Bluetooth goes through [bleak](https://github.com/hbldh/bleak).
- On macOS, the first scan asks for Bluetooth permission for your terminal app. Allow it under System Settings → Privacy & Security → Bluetooth.

```bash
git clone https://github.com/davidlapetina/led-matrix.git
cd led-matrix
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e '.[web]'                # library, command line tool and web UI
```

Use `pip install -e .` for the library and command line tool only, or `pip install -e '.[test,web]'` to also run the tests with `pytest`.

Then turn the panel on and close the phone app. A panel accepts only one Bluetooth connection at a time.

## Web interface: LED Panel Studio

```bash
ledpanel-web                           # then open http://127.0.0.1:8765
ledpanel-web --assets ~/my-assets      # use another assets folder (see Assets below)
ledpanel-web --host 0.0.0.0 --port 8765   # reachable from other devices on your network
```

Click **Scan**, pick your panel and click **Connect**. Enter the PIN first if your panel has one. Power and brightness are in the top bar. If the Bluetooth link drops, the UI reconnects on your next action. The tabs are:
- **Text**: per-letter colours with a live preview, effects (fixed, scroll, blink, …), gradients and inline emoji.
- **Animated text**: text that scrolls or blinks over a background animation, with a border and emoji, rendered at full quality as a GIF.
- **Images & GIFs**: drag and drop any picture (PNG, JPEG, WebP, BMP, GIF, animated WebP/APNG). Pick *contain*, *cover* or *stretch* to fit the panel. Below that is a gallery of your own animations.
- **Draw**: a live pixel canvas.
- **Clock & timers**: the panel's built-in clock faces, countdown and stopwatch.
- **Music**: a spectrum from your microphone, and the built-in rhythm modes.
- **Scoreboard**: the panel's built-in two-number scoreboard.
- **Live sports**: real scores with team logos (see below).
- **Weather**: weather with an icon, the temperature and a clock (see below).
- **Riding**: turn signals and other riding modes.
- **Remote**: the panel's 20 built-in presets.
- **Alarms & schedules**, **Storage** (program slots) and **Device** (upside down, exit mode, disconnect).

Anything sent to the panel replaces what it was showing. **⬛ Clear screen** in the top bar blanks it. While sports or weather is live, a ● LIVE chip with a Stop button shows in the top bar.

The server listens on localhost only unless you pass `--host`. It has no login, so only expose it on a network you trust.

## Assets: your own emoji, backgrounds, borders and animations

No artwork ships with this project. The emoji picker, the text backgrounds and borders, and the animations gallery show what you put in your assets folder. That folder is, in order of preference:
1. the path given to `ledpanel-web --assets` or `Assets(root)`;
2. `$LEDPANEL_ASSETS`;
3. `~/.ledpanel/assets`.

```
~/.ledpanel/assets/
├── emoji/          inline icons for text, any image format; square looks best
│   ├── heart.png
│   ├── sun@16.jpg  ┐ the same icon drawn pixel-perfect at 16 px and 20 px:
│   └── sun@20.jpg  ┘ the size matching the text height is used, otherwise it is scaled
├── backgrounds/    GIFs (or still images) looped behind animated text
│   ├── fire@64x20.gif
│   └── stars.gif
├── borders/        PNGs with transparency drawn over animated text
│   └── frame@64x20.png
└── animations/     GIFs (or still images) to play on the panel
    └── pacman.gif
```

Rules:
- Supported formats: PNG, JPEG, GIF, WebP and BMP. Other files are ignored.
- **Size tags are optional.** `name@WxH.ext` is listed only for a panel of that size, so one folder can serve several panels. Untagged files are listed for every panel and scaled to fit: stretched for backgrounds and borders, cropped to fill for animations.
- **Emoji**: files that differ only by their `@N` tag are one icon in several sizes. Transparent pixels become black, which the panel shows as off.
- **Picking up changes:** reload the web page after adding files; no restart is needed.

From Python, `Assets()` lists and reads the same folder: `emoji_names()`, `emoji(name, height)`, `backgrounds(w, h)`, `borders(w, h)` and `animations(w, h)`.

## Command line

```bash
ledpanel scan                              # list nearby panels and their addresses
ledpanel info <address>                    # size, device type and firmware versions
ledpanel text <address> "Hello" --color ff0000 --effect 1
ledpanel image <address> picture.jpg
ledpanel gif <address> animation.gif
ledpanel clock <address> --style 1 --date
ledpanel brightness <address> 60
ledpanel score <address> 2 1
ledpanel clear <address>                   # also: on, off
```

Add `--pin 123456` if your panel is protected, and `-v` for a Bluetooth trace. On macOS the address is a UUID that `scan` prints, not a MAC address.

## Python

```python
import asyncio
from ledpanel import LedPanel, Span, discover

async def main():
    [dev] = await discover()
    async with LedPanel(dev) as led:          # pin="123456" if the panel has one
        print(led.info.width, led.info.height)
        await led.brightness(60)
        await led.text([Span("Hello ", (255, 0, 0)), Span("world", (0, 255, 0))])
        await led.image("photo.jpg", fit="cover")
        await led.clear()

asyncio.run(main())
```

### Live sports

The **Live sports** tab lists the games of the NBA, Premier League, Serie A, Ligue 1, LaLiga, Bundesliga and Champions League for a chosen day, using ESPN's public scoreboard API. That API needs no key, but it is unofficial and undocumented, so ESPN may change it.
- Pick a game to show `Logo score-score Logo` on the panel. Soccer lists the home team first, the NBA the away team, following each sport's convention.
- Before kick-off, the panel shows the start time in your local time.
- **Follow live** checks the game every 30 s and updates the panel when the score changes. It stops after the final whistle, or as soon as you send something else to the screen.
- The list refreshes every 20 s while a game is live, and every minute otherwise.

From Python: `ledpanel.sports.games(league, date)` returns the games, `sports.logo(url)` the team logo, and `render.score_card(left_logo, right_logo, (s1, s2), size)` draws the card.
- Logos get the same height, and dark ones are brightened for LEDs.
- Short scores use a large font. Wide ones like `132-125` use a crisp 4×7 LED digit font, because TrueType digits below about 12 px are malformed.

### Weather

The **Weather** tab uses Open-Meteo, which is free and documented and needs no key. Search for a city or use your browser's location. The panel cycles through the pages you pick:
- **Temperature**: a weather icon, the temperature coloured from cold blue to hot red, and today's high and low.
- **Clock**: the icon and the location's local time, in 24-hour or 12-hour format.
- **Rain & wind**: today's rain chance and the wind speed.

The page also shows the feels-like temperature, humidity, sunrise and sunset. The pages are uploaded as one looping GIF to the show-now slot, so the panel cycles them itself with no gaps. Starting the weather first erases the panel's stored programs (`04 00 03 80`, reply `05 00 03 80 01`). A new GIF is uploaded only when the picture changes: once a minute for the clock, and when the forecast changes. Live frames (`frame`) don't work for this: they only cover the stored program briefly, and with an empty memory the panel's "LED" start-up logo flashes in between. The forecast refreshes every 5 minutes. The display runs until you press Stop or send something else to the screen.

From Python: `ledpanel.weather.search(name)` and `weather.current(lat, lon, fahrenheit)` fetch the data, and `render.weather_card(w, page, size)` and `render.weather_icon(kind, is_day, size)` draw it.

### Feature overview

| Area | API |
|---|---|
| Device | `power`, `brightness` (0-100), `clear` (blank the screen), `upside_down`, `sync_time`, `hardware_info`, `set_password` |
| Text | `text(content, effect=TextEffect.*, vertical, speed, color, gradient=1..8, background, h_align/v_align, font)`. `content` can mix `str`, `Span(text, rgb)` for per-letter colour (or `rainbow()`), and `Emoji` icons. |
| Rendered animations | `animated_text(...)`: scroll and blink together, with emoji, over a background GIF and a border PNG (creative text), sent as a GIF. `render.animation_gif` turns animated WebP/APNG/GIF into a panel-sized GIF. |
| Pictures | `image` (any format Pillow reads: PNG, JPEG, WebP, BMP…; `fit=` contain, cover or stretch; sent as PNG), `gif`, `frame` (live RGB frames for games, camera feeds and video) |
| Drawing | `diy_start`, `pixels`, `diy_stop` |
| Built-in modes | `clock`, `countdown(TimerAction, min, sec)`, `stopwatch(TimerAction)`, `scoreboard`, `rhythm`, `spectrum`, `sport(SportMode, speed)`, `remote_preset(1..20)` |
| Storage | `slot=` on any upload, `playlist([slots])`, `delete_slots`, `delete_all` |
| Schedules | `alarm(index, h, m, image_or_gif, days, duration, buzzer)`, `timer_switch(index, on, h, m, days)` |
| Assets | `Assets(root)`: `emoji_names()`, `emoji(name, height)`, `backgrounds(w, h)`, `borders(w, h)`, `animations(w, h)` (see Assets) |

What has been confirmed on a 64×20 panel (MCU 15.22):
- Discovery, connection, time sync, device info and firmware info.
- Text: classic format, per-letter colours, gradient preset, emoji icons, and the fixed, move-left and blink effects.
- Uploads: GIF and PNG image.
- Live RGB frames.
- Built-in modes: countdown, stopwatch, scoreboard, clock, spectrum bars and riding turn signal.
- Brightness.

Not yet tried on hardware: alarms, timer switch, playlists and slots, remote presets, and DIY pixel drawing. These match the app's byte layouts and are covered by unit tests. On this panel `MOVE_RIGHT` mirrors the text.

## Transport

| | |
|---|---|
| Advertised name | contains `LED_BLE` |
| Service | `000000fa-0000-1000-8000-00805f9b34fb` |
| Write | `0000fa02-…` (write with response), up to 509 bytes per write after MTU 512 |
| Notify | every notify characteristic in the service (the app enables all of them) |
| `AE00/AE01/AE02` | JieLi OTA service, not used here |

## Frames

`[len_lo, len_hi, cmd, sub, payload…]`. `len` is the length of the whole frame, little-endian.

| Function | Frame |
|---|---|
| Sync time and get device type | `08 00 01 80 hh mm ss lang`. The reply's byte 4 is the type and byte 10 is the PIN flag. |
| Hardware info | `04 00 05 80` |
| Power on/off | `05 00 07 01 {1,0}` |
| Brightness 0-100 | `05 00 04 80 lvl` |
| Flip display | `05 00 06 80 {0,1}` |
| Clock face | `0B 00 06 01 style h24 showDate yy mm dd weekday` |
| Weekday | `05 00 12 80 d` (Monday=1 … Sunday=7) |
| Text speed | `05 00 03 01 speed` |
| Exit mode | `04 00 01 01` |
| Delete all programs | `04 00 03 80` |
| Delete slots | `len 02 01 count16 idx…` |
| Countdown | `07 00 0D 80 run min sec` |
| Stopwatch | `05 00 09 80 flag` |
| Scoreboard | `08 00 0A 80 left16le right16le` |
| Rhythm | `06 00 00 02 level mode`, spectrum `10 00 01 02 mode b0..b10` |
| Set / verify PIN | `08 00 04 02 en p1 p2 p3`, `07 00 05 02 p1 p2 p3` (pairs of digits) |
| DIY mode | `05 00 04 01 mode` |
| DIY pixels | `len 05 01 move R G B x y [x y…]` |

## Bulk uploads (image, GIF, text)

The data is split into frames of 12288 bytes (types `0x00/0x80/0x81`) or 4096 bytes (other types):

```
len16 type0 type1 option size32 crc32 flag slot chunk…
```

- `type`: image (PNG file) is `02 00`, GIF file is `03 00`, text block is `00 01`.
- `option` is `0` on the first frame and `2` on each following frame.
- `size` and `crc32` (zlib) cover the whole upload.
- `flag` is `2` for GIFs and `0` otherwise.
- `slot` is a program slot number to store the content, or `101` (0x65) to show it now on older firmware.
- On MCU firmware above 4, the app never uses 101 for a plain send. It first sends `07 00 08 80 01 00 <slot>` ("play only this slot"), waits for `05 00 08 80 01`, then uploads into that slot. Uploading to 101 on these panels adds the item to the rotation, so text, GIFs and images pile up. `ledpanel` does what the app does and uses slot 100 for "show now" (confirmed on MCU 15.22: each send replaces the previous one).
- Storing into a slot can take several seconds per frame, so the client waits up to 8 s for each ack, like the app.
- Overwriting a slot that holds text with a GIF reboots the panel (MCU 15.22). The client first deletes the slot (`len 02 01 01 00 slot`), and the panel answers `05 00 02 01 01`.
- GIFs: the panel's decoder stalls on the first frame whose local colour table is bigger than the global table, then loops the frames before it. Pillow's default output does exactly that once text adds colours, so `ledpanel` encodes every animation with one 256-colour global palette, like the app's own GIFs. With that, a 213 KB, 125-frame GIF plays in full on a 64×20 panel.

After each frame the panel replies `05 00 t0 t1 status`. Status `1` means send the next frame, `3` means done, and `0` means error.

A text block is a 14-byte header (`count16, h_align, v_align, effect, speed, color_mode, R, G, B, bg_on, bgR, bgG, bgB`) followed by, for each character, `size_type R G B` and a 1-bit bitmap.

- `effect` values: 0 fixed, 1 move left, 2 move right, 3 up, 4 down, 5 blink, 6 breathe, 7 snowflake, 8 laser. Add 0x80 for vertical text.
- `speed` ranges from 1 to 100.
- `color_mode` is `0` to give each character its own colour (confirmed on a 64×20 panel) and `1` for a single colour taken from the header. Values from `2` up are the app's preset and gradient modes. The bitmap is row-major, each row is padded to 8 bits, and bits are packed LSB-first. The app renders the glyphs on the phone, so `ledpanel.text` does the same with Pillow.

## Unverified

- Ack status `2`: the meaning is not confirmed.
- `effect`, `speed`, `align` and clock `style` values: they are passed through as raw integers, the same way the app's UI passes them.

## License

MIT, see [LICENSE](LICENSE). This project is not affiliated with the makers of the panels or of the iPixel Color app. Product names are used only to identify compatible hardware.
