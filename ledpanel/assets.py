"""Your own icons, text backgrounds, borders and animations, read from an assets folder.

Nothing is bundled. The folder is, in order of preference: the path you pass,
$LEDPANEL_ASSETS, or ~/.ledpanel/assets. Inside it:

    emoji/        inline text icons, any image format; square ones look best
    backgrounds/  GIFs (or still images) looped behind animated text
    borders/      PNGs with transparency drawn over animated text
    animations/   GIFs (or still images) to play on the panel

A name may carry a size tag before the extension: `name@64x20.gif` for backgrounds,
borders and animations, `name@16.png` for emoji. Tagged files are only listed for a
panel of that size, so one folder can serve several panels; untagged files are listed
everywhere and scaled to fit. Emoji with the same name and different tags are one
icon in several pixel-perfect sizes; the one matching the text height is used as is,
otherwise the largest is scaled.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path

from PIL import Image

from .text import Emoji

FOLDERS = ("emoji", "backgrounds", "borders", "animations")
EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_TAG = re.compile(r"^(?P<stem>.+?)(?:@(?P<w>\d+)(?:x(?P<h>\d+))?)?$")


def default_root() -> Path:
    return Path(os.environ.get("LEDPANEL_ASSETS") or Path.home() / ".ledpanel" / "assets")


def _natural(s: str) -> list:
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]


def split_name(filename: str) -> tuple[str, tuple[int, int] | None]:
    """'bg@64x20.gif' -> ('bg', (64, 20)); 'smile@16.png' -> ('smile', (16, 16));
    'logo.png' -> ('logo', None)."""
    m = _TAG.match(Path(filename).stem)
    if not m.group("w"):
        return m.group("stem"), None
    w = int(m.group("w"))
    return m.group("stem"), (w, int(m.group("h") or w))


class Assets:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else default_root()

    def files(self, folder: str) -> list[str]:
        """Image files in <root>/<folder>/, naturally sorted (empty if it doesn't exist)."""
        if folder not in FOLDERS:
            raise KeyError(folder)
        d = self.root / folder
        if not d.is_dir():
            return []
        return sorted((f.name for f in d.iterdir()
                       if f.is_file() and f.suffix.lower() in EXTENSIONS and not f.name.startswith(".")),
                      key=_natural)

    def for_panel(self, folder: str, width: int, height: int) -> list[str]:
        """Files tagged for this panel size plus untagged ones."""
        return [n for n in self.files(folder) if split_name(n)[1] in (None, (width, height))]

    def backgrounds(self, width: int, height: int) -> list[str]:
        return self.for_panel("backgrounds", width, height)

    def borders(self, width: int, height: int) -> list[str]:
        return self.for_panel("borders", width, height)

    def animations(self, width: int, height: int) -> list[str]:
        return self.for_panel("animations", width, height)

    def read(self, folder: str, name: str) -> bytes:
        if (folder not in FOLDERS or Path(name).name != name or name.startswith(".")
                or Path(name).suffix.lower() not in EXTENSIONS):
            raise KeyError(f"{folder}/{name}")
        path = self.root / folder / name
        if not path.is_file():
            raise KeyError(f"{folder}/{name}")
        return path.read_bytes()

    # --- emoji ------------------------------------------------------------------

    def _emoji_variants(self) -> dict[str, dict[int | None, str]]:
        out: dict[str, dict[int | None, str]] = {}
        for n in self.files("emoji"):
            stem, size = split_name(n)
            out.setdefault(stem, {})[size[1] if size else None] = n
        return out

    def emoji_names(self) -> list[str]:
        return sorted(self._emoji_variants(), key=_natural)

    def emoji_file(self, name: str, height: int) -> str:
        """The file that best shows emoji `name` at `height`: the exact size, else the
        largest one (untagged counts as largest)."""
        variants = self._emoji_variants().get(name)
        if not variants:
            raise KeyError(f"emoji/{name}")
        if height in variants:
            return variants[height]
        if None in variants:
            return variants[None]
        return variants[max(variants)]

    def emoji(self, name: str, height: int) -> Emoji:
        """Inline text icon `name` as a height x height JPEG, the format the panel takes."""
        filename = self.emoji_file(name, height)
        data = self.read("emoji", filename)
        img = Image.open(io.BytesIO(data))
        if img.size == (height, height) and img.format == "JPEG":
            return Emoji(data)
        img = img.convert("RGBA")
        img.thumbnail((height, height), Image.LANCZOS)
        square = Image.new("RGBA", (height, height), (0, 0, 0, 255))   # the panel's black
        square.alpha_composite(img, ((height - img.width) // 2, (height - img.height) // 2))
        buf = io.BytesIO()
        square.convert("RGB").save(buf, format="JPEG", quality=95)
        return Emoji(buf.getvalue())
