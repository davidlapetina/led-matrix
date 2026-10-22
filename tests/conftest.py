"""Every test reads a small generated assets folder, never the user's own."""

import pytest
from PIL import Image, ImageDraw


def _gif(path, size, colors):
    frames = [Image.new("RGB", size, c) for c in colors]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=80, loop=0)


@pytest.fixture(scope="session")
def assets_dir(tmp_path_factory):
    root = tmp_path_factory.mktemp("assets")
    for d in ("emoji", "backgrounds", "borders", "animations"):
        (root / d).mkdir()
    # one emoji in two pixel-perfect sizes, one untagged transparent PNG
    for size, color in ((16, (255, 200, 0)), (20, (255, 120, 0))):
        img = Image.new("RGB", (size, size), (0, 0, 0))
        ImageDraw.Draw(img).ellipse((1, 1, size - 2, size - 2), fill=color)
        img.save(root / "emoji" / f"sun@{size}.jpg", quality=95)
    star = Image.new("RGBA", (40, 30), (0, 0, 0, 0))
    ImageDraw.Draw(star).polygon([(20, 0), (40, 30), (0, 30)], fill=(0, 200, 255, 255))
    star.save(root / "emoji" / "star.png")
    _gif(root / "backgrounds" / "fire@64x20.gif", (64, 20), [(120, 0, 0), (200, 60, 0), (255, 140, 0)] * 6)
    _gif(root / "backgrounds" / "big@32x32.gif", (32, 32), [(0, 0, 90), (0, 0, 160)])
    _gif(root / "backgrounds" / "any.gif", (100, 50), [(0, 60, 0), (0, 120, 0)])
    border = Image.new("RGBA", (64, 20), (0, 0, 0, 0))
    ImageDraw.Draw(border).rectangle((0, 0, 63, 19), outline=(255, 255, 255, 255))
    border.save(root / "borders" / "frame@64x20.png")
    _gif(root / "animations" / "blink@64x20.gif", (64, 20), [(255, 0, 0), (0, 0, 0)])
    Image.new("RGB", (30, 30), (0, 255, 0)).save(root / "animations" / "square.png")
    (root / "animations" / "notes.txt").write_text("not an image")
    return root


@pytest.fixture(autouse=True)
def _use_test_assets(assets_dir, monkeypatch):
    monkeypatch.setenv("LEDPANEL_ASSETS", str(assets_dir))
