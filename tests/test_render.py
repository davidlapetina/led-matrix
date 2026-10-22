import io

from PIL import Image, ImageSequence

from ledpanel import Assets, Span
from ledpanel.render import animation_gif, fit_picture, scrolling_text_gif, text_strip


def test_emoji_are_drawn_inline_with_transparent_backdrop():
    a = Assets()
    plain = text_strip([Span("A", (255, 0, 0))], 20)
    strip = text_strip([Span("A", (255, 0, 0)), a.emoji("sun", 16)], 20)
    assert strip.width - plain.width == 16          # the 16 px icon takes its own width
    icon = strip.crop((plain.width - 1, 0, strip.width, 20))
    assert set(icon.getchannel("A").tobytes()) == {0, 255}                     # icon pixels opaque, its black JPEG backdrop clear


def test_animated_text_with_emoji_and_background_renders():
    a = Assets()
    bg = a.read("backgrounds", "fire@64x20.gif")
    gif = scrolling_text_gif([Span("Hi", (0, 255, 0)), a.emoji("star", 20)], (64, 20), background=bg)
    im = Image.open(io.BytesIO(gif))
    assert im.size == (64, 20) and im.n_frames > 10


def test_fit_modes():
    wide = Image.new("RGB", (200, 100), (255, 255, 255))
    contain = fit_picture(wide, (64, 20), "contain")
    assert contain.size == (64, 20)
    assert contain.getpixel((0, 10)) == (0, 0, 0) and contain.getpixel((32, 10)) == (255, 255, 255)
    assert fit_picture(wide, (64, 20), "cover").getpixel((0, 10)) == (255, 255, 255)
    assert fit_picture(wide, (64, 20), "stretch").getpixel((0, 0)) == (255, 255, 255)


def test_transparency_becomes_black_and_exif_rotation_is_applied():
    clear = Image.new("RGBA", (64, 20), (255, 0, 0, 0))
    assert fit_picture(clear, (64, 20)).getpixel((5, 5)) == (0, 0, 0)
    tall = Image.new("RGB", (20, 64), (0, 0, 255))
    exif = tall.getexif()
    exif[0x0112] = 6                              # phone photo stored sideways
    buf = io.BytesIO()
    tall.save(buf, format="JPEG", exif=exif)
    upright = fit_picture(Image.open(buf), (64, 20), "contain")
    assert upright.getpixel((2, 10))[2] > 200     # rotated to landscape: fills the width, no bars


def test_animated_webp_becomes_a_panel_sized_gif_with_frame_timing():
    frames = [Image.new("RGB", (32, 32), c) for c in [(255, 0, 0), (0, 255, 0)]]
    buf = io.BytesIO()
    frames[0].save(buf, format="WEBP", save_all=True, append_images=frames[1:], duration=[80, 200], loop=0)
    gif = Image.open(io.BytesIO(animation_gif(buf.getvalue(), (64, 20))))
    assert gif.format == "GIF" and gif.size == (64, 20) and gif.n_frames == 2
    assert [f.info["duration"] for f in ImageSequence.Iterator(gif)] == [80, 200]



def test_gif_uses_one_global_palette_no_bigger_local_tables():
    """The panel loops the first frames when a local colour table outgrows the global one."""
    a = Assets()
    bg = a.read("backgrounds", "fire@64x20.gif")
    gif = scrolling_text_gif([Span("Bao ", (255, 48, 48)), a.emoji("sun", 20)], (64, 20), background=bg)
    flags = gif[10]
    global_size = 2 << (flags & 7)
    assert flags & 0x80 and global_size == 256
    pos, local_sizes = 13 + 3 * global_size, []
    while gif[pos] != 0x3B:
        if gif[pos] == 0x21:
            pos += 2
            while gif[pos]:
                pos += gif[pos] + 1
            pos += 1
        else:
            packed = gif[pos + 9]
            local = (2 << (packed & 7)) if packed & 0x80 else 0
            local_sizes.append(local)
            pos += 10 + 3 * local + 1
            while gif[pos]:
                pos += gif[pos] + 1
            pos += 1
    assert len(local_sizes) > 50 and max(local_sizes) <= global_size


def test_assets_folder_rules(tmp_path):
    from ledpanel.assets import split_name
    assert split_name("bg@64x20.gif") == ("bg", (64, 20))
    assert split_name("smile@16.png") == ("smile", (16, 16))
    assert split_name("my@logo.png") == ("my@logo", None)
    assert split_name("plain.png") == ("plain", None)
    empty = Assets(tmp_path / "missing")
    assert empty.emoji_names() == [] and empty.backgrounds(64, 20) == []


def test_emoji_is_a_square_jpeg_at_any_text_height():
    a = Assets()
    assert a.emoji_file("sun", 16) == "sun@16.jpg" and a.emoji_file("sun", 24) == "sun@20.jpg"
    assert a.emoji("sun", 16).jpeg == a.read("emoji", "sun@16.jpg")            # exact size: sent as is
    for name, h in (("sun", 24), ("star", 16), ("star", 32)):
        img = Image.open(io.BytesIO(a.emoji(name, h).jpeg))
        assert img.format == "JPEG" and img.size == (h, h)
    corner = Image.open(io.BytesIO(a.emoji("star", 16).jpeg)).convert("RGB").getpixel((0, 0))
    assert max(corner) < 30                                                    # transparency became black
