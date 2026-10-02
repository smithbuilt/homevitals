"""The window's look: the app icon's blue-to-teal colours, soft rounded corners, a branded header.

Plain ttk on Windows draws native, square widgets that can't be recoloured, so this
switches to the "clam" theme (which takes colours) and replaces the button, entry
and card borders with small rounded images drawn here (anti-aliased by blending the
edge pixels into the window background, so no transparency is needed). Drawing
only: nothing here knows about people or syncing.
"""
from __future__ import annotations

import contextlib
import tkinter as tk
from tkinter import ttk

# From the icon (scripts/make_icon.py): blue at the top, teal at the bottom.
BLUE = "#2463A6"
TEAL = "#16A096"
BLUE_DARK = "#1B4F86"
BLUE_PRESSED = "#173F6B"
BLUE_LIGHT = "#E8F1F8"
BG = "#F5F8FB"            # window background
PANEL = "#FFFFFF"         # list and messages
BORDER = "#CFDCE6"
BORDER_STRONG = "#A9C0D2"
TEXT = "#1D2A35"
MUTED = "#5B6B78"
DANGER = "#B42318"
DISABLED_TEXT = "#9AA8B3"
SELECTED = "#C9DFF2"

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI Semibold", 10)
FONT_TITLE = ("Segoe UI Semibold", 11)
FONT_HEADER = ("Segoe UI Semibold", 15)
FONT_SUB = ("Segoe UI", 9)

RADIUS = 7
_images: list[tk.PhotoImage] = []      # ttk keeps no reference of its own


def _mix(a: str, b: str, t: float) -> str:
    """Colour a blended toward b by t (0..1)."""
    ra, ga, ba = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    rb, gb, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return "#%02x%02x%02x" % (round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t))


def _inside(px: float, py: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    dx = max(x0 + r - px, 0.0, px - (x1 - r))
    dy = max(y0 + r - py, 0.0, py - (y1 - r))
    return px >= x0 and px <= x1 and py >= y0 and py <= y1 and dx * dx + dy * dy <= r * r


def rounded_image(master: tk.Misc, fill: str, border: str, outside: str = BG, radius: int = RADIUS,
                  border_width: int = 1) -> tk.PhotoImage:
    """A rounded rectangle for a 9-slice ttk image element (corners kept, middle tiled).

    Tk fills the middle by repeating it, so the middle is made wide (48 px): a narrow
    one means thousands of tiny copies per redraw and a window that paints slowly.
    Only the four corners need smoothing; the rest is plain edge or fill.
    """
    size = 2 * radius + 48
    samples = 4
    n = samples * samples
    rows = []
    for y in range(size):
        row = []
        corner_y = y < radius or y >= size - radius
        for x in range(size):
            if corner_y and (x < radius or x >= size - radius):
                outer = inner = 0
                for sy in range(samples):
                    for sx in range(samples):
                        px, py = x + (sx + 0.5) / samples, y + (sy + 0.5) / samples
                        if _inside(px, py, 0, 0, size, size, radius):
                            outer += 1
                            if _inside(px, py, border_width, border_width, size - border_width,
                                       size - border_width, max(radius - border_width, 0)):
                                inner += 1
                colour = _mix(outside, border, outer / n)
                colour = _mix(colour, fill, inner / n) if inner else colour
            elif x < border_width or y < border_width or x >= size - border_width or y >= size - border_width:
                colour = border
            else:
                colour = fill
            row.append(colour)
        rows.append("{" + " ".join(row) + "}")
    image = tk.PhotoImage(master=master, width=size, height=size)
    image.put(" ".join(rows))
    _images.append(image)
    return image


def _element(style: ttk.Style, name: str, master: tk.Misc, states: dict[str, tuple[str, str]],
             outside: str = BG) -> None:
    """An image element with one rounded image per state ("" is the normal one)."""
    normal = rounded_image(master, *states[""], outside=outside)
    specs = [(state, rounded_image(master, *colours, outside=outside)) for state, colours in states.items() if state]
    # border: the corners Tk must not stretch; padding: the room the element itself takes inside.
    # width/height: the element's own size; the big image is only a source to slice from.
    small = 2 * RADIUS + 4
    style.element_create(name, "image", normal, *specs, border=RADIUS + 2, padding=3, sticky="nsew",
                         width=small, height=small)


def _paint(master: tk.Misc, width: int, height: int, layers, outside: str = BG) -> tk.PhotoImage:
    """An anti-aliased image: each layer is (inside(px, py) -> bool, colour), painted in order."""
    samples = 4
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            colour = outside
            for inside, layer_colour in layers:
                hits = sum(inside(x + (sx + 0.5) / samples, y + (sy + 0.5) / samples)
                           for sy in range(samples) for sx in range(samples))
                if hits:
                    colour = _mix(colour, layer_colour, hits / (samples * samples))
            row.append(colour)
        rows.append("{" + " ".join(row) + "}")
    image = tk.PhotoImage(master=master, width=width, height=height)
    image.put(" ".join(rows))
    _images.append(image)
    return image


def _near_segment(px, py, ax, ay, bx, by, half_width) -> bool:
    vx, vy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy)))
    dx, dy = px - (ax + t * vx), py - (ay + t * vy)
    return dx * dx + dy * dy <= half_width * half_width


def _indicators(style: ttk.Style, master: tk.Misc) -> None:
    """Rounded tick boxes and round radio buttons, with a little room before the text."""
    box, gap = 16, 7
    width = box + gap

    def square(inset, radius):
        return lambda px, py: _inside(px, py, inset, inset, box - inset, box - inset, radius)

    def tick(px, py):
        return (_near_segment(px, py, 4.0, 8.5, 7.0, 11.5, 1.1) or _near_segment(px, py, 7.0, 11.5, 12.2, 5.0, 1.1))

    def circle(radius):
        return lambda px, py: (px - box / 2) ** 2 + (py - box / 2) ** 2 <= radius * radius

    check_off = _paint(master, width, box, [(square(0, 4), BORDER_STRONG), (square(1, 3), PANEL)])
    check_on = _paint(master, width, box, [(square(0, 4), BLUE), (tick, "#FFFFFF")])
    check_off_disabled = _paint(master, width, box, [(square(0, 4), BORDER), (square(1, 3), BG)])
    check_on_disabled = _paint(master, width, box, [(square(0, 4), _mix(BLUE, BG, 0.55)), (tick, "#FFFFFF")])
    style.element_create("Rounded.Checkbutton.indicator", "image", check_off,
                         ("disabled selected", check_on_disabled), ("disabled", check_off_disabled),
                         ("selected", check_on), sticky="w")
    radio_off = _paint(master, width, box, [(circle(8), BORDER_STRONG), (circle(7), PANEL)])
    radio_on = _paint(master, width, box, [(circle(8), BLUE), (circle(7), PANEL), (circle(4), BLUE)])
    radio_off_disabled = _paint(master, width, box, [(circle(8), BORDER), (circle(7), BG)])
    style.element_create("Rounded.Radiobutton.indicator", "image", radio_off,
                         ("disabled", radio_off_disabled), ("selected", radio_on), sticky="w")
    for kind in ("Checkbutton", "Radiobutton"):
        style.layout(f"T{kind}", [(f"{kind}.padding", {"sticky": "nswe", "children": [
            (f"Rounded.{kind}.indicator", {"side": "left", "sticky": ""}),
            (f"{kind}.focus", {"side": "left", "sticky": "w", "children": [
                (f"{kind}.label", {"sticky": "nswe"})]})]})])


def apply(root: tk.Misc) -> ttk.Style:
    """Set up every style once per Tk interpreter; safe to call again."""
    style = ttk.Style(root)
    if "Rounded.Button.border" in style.element_names():
        return style
    with contextlib.suppress(tk.TclError):
        style.theme_use("clam")

    style.configure(".", background=BG, foreground=TEXT, font=FONT, bordercolor=BORDER,
                    troughcolor=BG, focuscolor=BLUE, selectbackground=SELECTED, selectforeground=TEXT)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Title.TLabel", font=FONT_TITLE, foreground=BLUE_DARK)
    style.configure("Panel.TLabel", background=PANEL, foreground=MUTED)
    style.configure("Problem.TLabel", foreground=DANGER)
    style.configure("Name.TLabel", font=FONT_TITLE, foreground=TEXT)

    # Buttons: outlined by default, filled blue for the main action, red text for removing.
    _element(style, "Rounded.Button.border", root, {
        "": (PANEL, BORDER_STRONG), "pressed": (BLUE_LIGHT, BLUE), "active": (BLUE_LIGHT, BORDER_STRONG),
        "disabled": (BG, BORDER)})
    _element(style, "Primary.Button.border", root, {
        "": (BLUE, BLUE), "pressed": (BLUE_PRESSED, BLUE_PRESSED), "active": (BLUE_DARK, BLUE_DARK),
        "disabled": (_mix(BLUE, BG, 0.55), _mix(BLUE, BG, 0.55))})
    for name, element in (("TButton", "Rounded.Button.border"), ("Primary.TButton", "Primary.Button.border"),
                          ("Danger.TButton", "Rounded.Button.border")):
        style.layout(name, [(element, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
    style.configure("TButton", padding=(10, 2), foreground=BLUE_DARK, background=PANEL, anchor="center")
    style.map("TButton", foreground=[("disabled", DISABLED_TEXT)])
    style.configure("Primary.TButton", foreground="#FFFFFF", font=FONT_BOLD)
    style.map("Primary.TButton", foreground=[("disabled", "#F0F4F8")])
    style.configure("Danger.TButton", foreground=DANGER)
    style.map("Danger.TButton", foreground=[("disabled", DISABLED_TEXT)])

    # Text boxes: white, rounded, a blue edge while typing.
    _element(style, "Rounded.Entry.field", root, {
        "": (PANEL, BORDER_STRONG), "focus": (PANEL, BLUE), "disabled": (BG, BORDER), "readonly": (BG, BORDER)})
    style.layout("TEntry", [("Rounded.Entry.field", {"sticky": "nsew", "children": [
        ("Entry.padding", {"sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]})]})])
    style.configure("TEntry", padding=(5, 1), fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT)
    style.map("TEntry", foreground=[("disabled", DISABLED_TEXT), ("readonly", MUTED)])
    style.configure("TCombobox", padding=(6, 3), fieldbackground=PANEL, background=PANEL, arrowcolor=BLUE_DARK,
                    bordercolor=BORDER_STRONG, lightcolor=PANEL, darkcolor=PANEL)
    style.map("TCombobox", fieldbackground=[("disabled", BG), ("readonly", PANEL)],
              foreground=[("disabled", MUTED)], bordercolor=[("focus", BLUE)])

    # Cards: the account boxes (window colour inside) and the white panels (list, messages).
    _element(style, "Card.border", root, {"": (BG, BORDER)})
    _element(style, "Panel.border", root, {"": (PANEL, BORDER)})
    style.layout("Card.TFrame", [("Card.border", {"sticky": "nsew"})])
    style.layout("Panel.TFrame", [("Panel.border", {"sticky": "nsew"})])
    style.configure("Card.TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)

    _indicators(style, root)
    style.configure("TCheckbutton", background=BG, foreground=TEXT, padding=(0, 2))
    style.map("TCheckbutton", background=[("active", BG)], foreground=[("disabled", DISABLED_TEXT)])
    style.configure("TRadiobutton", background=BG, foreground=TEXT, padding=(0, 2))
    style.map("TRadiobutton", background=[("active", BG)], foreground=[("disabled", DISABLED_TEXT)])

    # The people list.
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=28,
                    borderwidth=0, relief="flat")
    style.map("Treeview", background=[("selected", SELECTED)], foreground=[("selected", TEXT)])
    style.configure("Treeview.Heading", background=BLUE_LIGHT, foreground=BLUE_DARK, font=FONT_BOLD,
                    relief="flat", borderwidth=0, padding=(6, 5))
    style.map("Treeview.Heading", background=[("active", _mix(BLUE_LIGHT, BLUE, 0.12))])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nsew"})])

    style.configure("Vertical.TScrollbar", background=_mix(BORDER, PANEL, 0.2), troughcolor=PANEL,
                    bordercolor=PANEL, arrowcolor=MUTED, lightcolor=PANEL, darkcolor=PANEL, gripcount=0)
    return style


class Header(tk.Canvas):
    """The blue-to-teal band at the top of the main window, with the logo and the name."""

    def __init__(self, parent: tk.Misc, title: str, subtitle: str, icon_png: str | None, height: int = 64) -> None:
        super().__init__(parent, height=height, highlightthickness=0, bd=0, background=BLUE)
        self.title = title
        self.subtitle = subtitle
        self.logo: tk.PhotoImage | None = None
        if icon_png:
            with contextlib.suppress(tk.TclError):
                full = tk.PhotoImage(master=self, file=icon_png)
                factor = max(1, full.width() // 40)
                self.logo = full.subsample(factor, factor)
        self.bind("<Configure>", lambda _e: self.draw())

    def draw(self) -> None:
        self.delete("all")
        width, height = max(self.winfo_width(), 1), int(self["height"])
        steps = 48
        for i in range(steps):
            x0 = width * i // steps
            x1 = width * (i + 1) // steps + 1
            self.create_rectangle(x0, 0, x1, height, outline="", fill=_mix(BLUE, TEAL, i / (steps - 1)))
        x = 18
        if self.logo is not None:
            self.create_image(x, height // 2, image=self.logo, anchor="w")
            x += self.logo.width() + 12
        self.create_text(x, height // 2 - 9, text=self.title, anchor="w", fill="#FFFFFF", font=FONT_HEADER)
        self.create_text(x, height // 2 + 13, text=self.subtitle, anchor="w", fill="#E6F4F3", font=FONT_SUB)
