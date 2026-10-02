"""The window's look (homevitals/theme.py): colours from the icon, rounded images, styles.

Each test skips when there is no screen (tkinter needs one).
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from tkinter import ttk  # noqa: E402

from homevitals import theme  # noqa: E402


@pytest.fixture(scope="module")
def root():
    # One Tk for the module: creating Tk over and over on Windows sometimes fails to read its own files.
    import time
    error = None
    for _ in range(5):
        try:
            r = tk.Tk()
            break
        except tk.TclError as e:
            error = e
            time.sleep(0.3)
    else:
        pytest.skip(f"no display ({error})")
    r.withdraw()
    yield r
    r.destroy()


def _pixel(image, x, y):
    value = image.get(x, y)
    return "#%02x%02x%02x" % tuple(value) if isinstance(value, tuple) else value


def test_rounded_image_has_soft_corners_a_border_and_a_wide_middle(root):
    image = theme.rounded_image(root, "#ffffff", "#000000", outside="#ff0000", radius=6)
    size = image.width()
    assert size == image.height() >= 2 * 6 + 40       # a wide middle keeps Tk's tiling cheap
    assert _pixel(image, 0, 0) == "#ff0000"           # the corner shows what's behind
    assert _pixel(image, size // 2, 0) == "#000000"   # straight edge: the border
    assert _pixel(image, size // 2, size // 2) == "#ffffff"


def test_apply_sets_up_the_styles_once(root):
    style = theme.apply(root)
    for element in ("Rounded.Button.border", "Primary.Button.border", "Rounded.Entry.field", "Card.border",
                    "Panel.border", "Rounded.Checkbutton.indicator", "Rounded.Radiobutton.indicator"):
        assert element in style.element_names()
    assert style.lookup("Primary.TButton", "foreground") == "#FFFFFF"
    assert style.lookup("Danger.TButton", "foreground") == theme.DANGER
    assert theme.apply(root) is not None              # a second call doesn't try to create them again


def test_colours_come_from_the_icon():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("make_icon", Path(__file__).parent.parent / "scripts" / "make_icon.py")
    make_icon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_icon)
    assert theme.BLUE.lower() == "#%02x%02x%02x" % make_icon.TOP
    assert theme.TEAL.lower() == "#%02x%02x%02x" % make_icon.BOTTOM


def test_header_draws_the_name_and_logo(root):
    theme.apply(root)
    header = theme.Header(root, "HomeVitals", "subtitle", None)
    header.pack(fill="x")
    root.update_idletasks()
    header.draw()
    texts = [header.itemcget(i, "text") for i in header.find_all() if header.type(i) == "text"]
    assert texts == ["HomeVitals", "subtitle"]
    assert isinstance(ttk.Style(root).theme_use(), str)
