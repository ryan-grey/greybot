"""The morning-after recap, drawn as a grid -- in the recap PAGE's own markup.

WHY AN IMAGE. The recap is six leaderboards, and the embed can only lay those out as
inline fields: three to a row on desktop, and ONE column on a phone, where six stacked
lists scroll for a screen and a half and read as a wall rather than a card. An image
renders the same two-by-three grid on every client -- a phone scales it down, it does not
re-flow it -- so the shape of the card is the same thing everybody sees.

WHY IT LOOKS LIKE THE PAGE. The first drawing of this card was its own design: navy
panels, 30px class-coloured names, giant numbers. It looked like a scoreboard from a
different product, and the "Full recap here" link underneath it led to a page that looked
nothing like it. So this module is recap_page.STYLE transcribed into pixels: the same
GitHub Primer dark palette, the same bordered columns with a chip header bar, the same
role glyphs, class colours, realm sub-line, muted right-aligned values and quality-coloured
parse pills. The rules are named the way the CSS names them so a change on the page has an
obvious place to land here. Every colour and shape is read from recap_page rather than
copied, wherever the page exposes it.

Fonts: the page uses the system font stack, which a Lambda does not have. Inter is the
closest open-licensed match to the GitHub UI face and is vendored under assets/fonts with
its OFL licence; DejaVu is the fallback if it is somehow missing.

WHAT IT COSTS, same as the kill card: text in a PNG cannot be selected, searched or read
by a screen reader. The embed keeps its own six fields as the fallback, so a recap whose
image could not be drawn or published is the card it was before, not a card with a hole
in it.

THE NUMBERS ARE THE SAME NUMBERS. `_cells` reads `summary` exactly as discord.recap_embed
does -- same keys, same three-per-category cap, same formatting helpers -- so the image
and its fallback can never disagree about who topped what.

Every failure returns None. Decoration never costs the post it decorates.
"""

import io
import os

import kill_card
import recap_page

# Everything below is in CSS pixels, drawn at SCALE so the PNG survives Discord scaling
# it back down. 640 CSS px is a narrow desktop page: three columns of the page's own
# density, and on a phone it lands at roughly the width the page itself renders at.
SCALE = 2
WIDTH_CSS = 640
TOPBAR = 40
PAD = 20
COLUMNS, ROWS = 3, 2
COL_GAP = 16
COL_HEAD = 36                  # .col h3
ROW_H = 54                     # .col li with a name line and a realm sub-line
RADIUS = 6

# recap_page.STYLE's dark :root, as RGB.
BG = (13, 17, 23)              # --bg
INK = (240, 246, 252)          # --ink
MUTED = (145, 152, 161)        # --muted
LINE = (61, 68, 77)            # --line
ACCENT = (68, 147, 248)        # --accent
CHIP = (21, 27, 35)            # --chip
CHIP_ACCENT_BG = (19, 35, 58)  # --chip-accent-bg, rgba(56,139,253,.15) flattened on --bg
TOPBAR_BG = (1, 4, 9)          # --topbar
PILL_BORDER = (52, 56, 62)     # rgba(31,35,40,.15) over a pill, near enough

# The page's role glyphs (recap_page.ROLE_ICONS), as polygons in the same 16x16 box. The
# SVG paths are straight lines except the shield's two curves, which are sampled.
_SHIELD_CURVES = (((2.6, 7.4), (2.6, 10.7), (4.8, 13.4), (8, 14.8)),
                  ((8, 14.8), (11.2, 13.4), (13.4, 10.7), (13.4, 7.4)))


def _bezier(p0, p1, p2, p3, steps=8):
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0]
                    + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1]
                    + t ** 3 * p3[1]))
    return out


ROLE_SHAPES = {
    "tank": [[(8, 1.2), (2.6, 3)] + _bezier(*_SHIELD_CURVES[0])
             + _bezier(*_SHIELD_CURVES[1]) + [(13.4, 3)]],
    "healer": [[(6.4, 2), (9.6, 2), (9.6, 6.4), (14, 6.4), (14, 9.6), (9.6, 9.6),
                (9.6, 14), (6.4, 14), (6.4, 9.6), (2, 9.6), (2, 6.4), (6.4, 6.4)]],
    "dps": [[(11.6, 1.4), (14.6, 1), (14.2, 4), (9.2, 9), (6.6, 6.4)],
            [(4.4, 1.4), (1.4, 1), (1.8, 4), (6.8, 9), (9.4, 6.4)],
            [(3.2, 13.4), (4.6, 14.8), (8.6, 10.8), (7.2, 9.4)],
            [(12.8, 14.8), (14.2, 13.4), (10.2, 9.4), (8.8, 10.8)]],
}


def _rgb(hex_color):
    return recap_page._rgb(hex_color)


def _font_path(name):
    """Inter if it is there, DejaVu if it is not. Both live in kill_card.FONT_DIR."""
    inter = {"regular": "Inter-Regular.ttf", "semibold": "Inter-SemiBold.ttf",
             "bold": "Inter-Bold.ttf"}[name]
    path = os.path.join(kill_card.FONT_DIR, inter)
    if os.path.isfile(path):
        return path
    return os.path.join(kill_card.FONT_DIR,
                        "DejaVuSans.ttf" if name == "regular" else "DejaVuSans-Bold.ttf")


def _short(n):
    return recap_page._short(n)


def _rate_and_total(row, field="total"):
    """"78K/145M" -- the page's cell() and discord's _dps_and_total, same string."""
    per = row.get("perSecond")
    if isinstance(per, (int, float)) and per > 0:
        return f"{_short(per)}/{_short(row[field])}"
    return _short(row[field])


def _quality(percent):
    """(bg, fg) RGB for a percent on the page's quality bands, or (None, None)."""
    if percent is None:
        return None, None
    bg, fg = recap_page.parse_colors(percent)
    return _rgb(bg), _rgb(fg)


def _cells(summary, top_n=3):
    """The six cells in grid order: (title, icon, rows, empty text, badge).

    A row is (name, class, server, value, role, pill, colour): `pill` is a parse percent
    to draw as the page's quality-coloured pill instead of a plain value, and `colour`
    tints a plain value -- an item level takes its quality colour as text, the way the
    page's `.ilvl` does. `badge` is (label, bg, fg) drawn as a pill in the header, the
    page's raid-average pill. Same keys and the same cap as discord.recap_embed,
    deliberately: this is the embed's field list drawn.
    """
    def rows(key, fmt, colour=lambda r: None):
        return [(r["name"], r.get("class"), r.get("server"), fmt(r), r.get("role"), None,
                 colour(r))
                for r in (summary.get(key) or [])[:top_n]]

    parses = summary.get("parses") or {}
    top = parses.get("top") or ([parses["best"]] if parses.get("best") else [])

    import recap
    scale = summary.get("ilvlScale")
    avg = summary.get("ilvlAverage")
    badge = None
    if isinstance(avg, (int, float)):
        bg, fg = _quality(recap.ilvl_percent(avg, scale))
        badge = (str(int(round(avg))), bg or CHIP_ACCENT_BG, fg or INK)

    return [
        ("DPS / Damage", "dps", rows("damage", _rate_and_total),
         "No damage table could be read.", None),
        ("HPS / Healing", "healer", rows("healing", _rate_and_total),
         "No healing table could be read.", None),
        ("Damage taken", "tank", rows("damageTaken", lambda r: _short(r["total"])),
         "No damage-taken table could be read.", None),
        ("Deaths", "skull", rows("deaths", lambda r: str(r["deaths"])),
         "Nobody died. Genuinely.", None),
        ("Best parses", None,
         [(p["name"], p.get("class"), p.get("boss") or p.get("server"), None,
           p.get("role"), float(p["percent"]), None) for p in top[:top_n]],
         "No ranked kills.", None),
        ("Item level", None,
         rows("itemLevel", lambda r: str(r["ilvl"]),
              colour=lambda r: _quality(recap.ilvl_percent(r["ilvl"], scale))[0]),
         "No gear data could be read.", badge),
    ]


class _Canvas:
    """Drawing in CSS pixels onto a canvas SCALE times larger."""

    def __init__(self, image, draw, fonts):
        self.image, self.draw, self.fonts = image, draw, fonts

    def font(self, weight, size):
        from PIL import ImageFont
        key = (weight, size)
        if key not in self.fonts:
            self.fonts[key] = ImageFont.truetype(_font_path(weight), int(size * SCALE))
        return self.fonts[key]

    def width(self, text, font):
        return self.draw.textlength(text, font=font) / SCALE

    def text(self, x, y, text, font, fill, spacing=0):
        """Text at a CSS position. `spacing` is CSS letter-spacing, drawn glyph by glyph
        because Pillow has no tracking of its own."""
        if not spacing:
            self.draw.text((x * SCALE, y * SCALE), text, font=font, fill=fill)
            return
        for ch in text:
            self.draw.text((x * SCALE, y * SCALE), ch, font=font, fill=fill)
            x += self.width(ch, font) + spacing

    def rect(self, x0, y0, x1, y1, fill=None, outline=None, radius=0):
        box = [x0 * SCALE, y0 * SCALE, x1 * SCALE, y1 * SCALE]
        if radius:
            self.draw.rounded_rectangle(box, radius=radius * SCALE, fill=fill,
                                        outline=outline, width=SCALE if outline else 0)
        else:
            self.draw.rectangle(box, fill=fill, outline=outline,
                                width=SCALE if outline else 0)

    def hline(self, x0, x1, y, fill):
        self.draw.rectangle([x0 * SCALE, y * SCALE, x1 * SCALE, y * SCALE + SCALE - 1],
                            fill=fill)

    def glyph(self, role, x, y, size):
        """One of the page's role glyphs, `size` CSS px square, top-left at (x, y)."""
        colour = _rgb(recap_page.ROLE_COLORS.get(role, "#9198a1"))
        for poly in ROLE_SHAPES.get(role, ()):
            pts = [((x + px * size / 16) * SCALE, (y + py * size / 16) * SCALE)
                   for px, py in poly]
            self.draw.polygon(pts, fill=colour)

    def skull(self, x, y, size):
        """The page's deaths header uses an emoji; no font here has one, so a skull is
        drawn: a dome, two eyes, a jaw. It reads at 13px, which is the whole brief."""
        s = SCALE
        dome = [x * s, y * s, (x + size) * s, (y + size * 0.78) * s]
        self.draw.ellipse(dome, fill=INK)
        self.draw.rectangle([(x + size * 0.22) * s, (y + size * 0.6) * s,
                             (x + size * 0.78) * s, (y + size) * s], fill=INK)
        for ex in (0.28, 0.6):
            self.draw.ellipse([(x + size * ex) * s, (y + size * 0.3) * s,
                               (x + size * (ex + 0.16)) * s, (y + size * 0.5) * s],
                              fill=CHIP)
        self.draw.rectangle([(x + size * 0.44) * s, (y + size * 0.72) * s,
                             (x + size * 0.56) * s, (y + size) * s], fill=CHIP)


def _ellipsis(canvas, text, font, limit):
    """The page's `.who` rule: overflow hidden, text-overflow ellipsis, no shrinking."""
    if canvas.width(text, font) <= limit:
        return text
    while text and canvas.width(text + "…", font) > limit:
        text = text[:-1]
    return (text + "…") if text else ""


def _column(canvas, x, y, w, title, icon, rows, empty, badge=None):
    """One `.col`: bordered box, chip header with icon, uppercase title and an optional
    badge pill, three rows."""
    h = COL_HEAD + ROW_H * 3
    canvas.rect(x, y, x + w, y + h, fill=BG, outline=LINE, radius=RADIUS)
    # Header bar, squared at the bottom and rounded at the top like the CSS overflow.
    canvas.rect(x, y, x + w, y + COL_HEAD, fill=CHIP, radius=RADIUS)
    canvas.rect(x, y + RADIUS, x + w, y + COL_HEAD, fill=CHIP)
    canvas.rect(x, y, x + w, y + h, outline=LINE, radius=RADIUS)
    canvas.hline(x, x + w, y + COL_HEAD, LINE)

    head_font = canvas.font("bold", 11)
    label = title.upper()
    spacing = 0.6
    tw = sum(canvas.width(c, head_font) + spacing for c in label) - spacing
    icon_w = 13 + 7 if icon else 0
    badge_font = canvas.font("semibold", 11)
    badge_w = (max(34, canvas.width(badge[0], badge_font) + 14) + 7) if badge else 0
    hx = x + (w - tw - icon_w - badge_w) / 2
    hy = y + (COL_HEAD - 13) / 2
    if icon == "skull":
        canvas.skull(hx, hy, 13)
    elif icon:
        canvas.glyph(icon, hx, hy, 13)
    canvas.text(hx + icon_w, hy - 1, label, head_font, INK, spacing=spacing)
    if badge:
        # .parse-badge: the raid's average in the header, same pill as the rows use.
        text, bg, fg = badge
        bx = hx + icon_w + tw + 7
        bw = badge_w - 7
        by = y + (COL_HEAD - 18) / 2
        canvas.rect(bx, by, bx + bw, by + 18, fill=bg, outline=PILL_BORDER, radius=9)
        canvas.text(bx + (bw - canvas.width(text, badge_font)) / 2, by + 2, text,
                    badge_font, fg)

    if not rows:
        canvas.text(x + 14, y + COL_HEAD + 12, empty, canvas.font("regular", 13), MUTED)
        return

    name_font = canvas.font("semibold", 14)
    sub_font = canvas.font("regular", 11)
    val_font = canvas.font("regular", 13)
    pill_font = canvas.font("semibold", 12)
    for n, (name, klass, sub, value, role, pill, vcolour) in enumerate(rows[:3]):
        ry = y + COL_HEAD + n * ROW_H
        if n:
            canvas.hline(x, x + w, ry, LINE)
        left = x + 12
        right = x + w - 12
        # The value first, so the name knows how much room it has.
        if pill is not None:
            bg, fg = recap_page.parse_colors(pill)
            label = str(int(round(pill)))
            pw = max(38, canvas.width(label, pill_font) + 16)
            py = ry + (ROW_H - 20) / 2
            canvas.rect(right - pw, py, right, py + 20, fill=_rgb(bg),
                        outline=PILL_BORDER, radius=10)
            canvas.text(right - pw + (pw - canvas.width(label, pill_font)) / 2, py + 2,
                        label, pill_font, _rgb(fg))
            vw = pw
        else:
            # A coloured value is the page's `.ilvl`: semibold in its quality colour.
            font = canvas.font("semibold", 13) if vcolour else val_font
            vw = canvas.width(value, font)
            canvas.text(right - vw, ry + (ROW_H - 16) / 2, value, font, vcolour or MUTED)
        # Role glyph, then the name in its class colour, then the realm under it.
        gy = ry + 12
        canvas.glyph(role, left, gy + 2, 13)
        nx = left + 13 + 6
        limit = right - vw - 10 - nx
        colour = recap_page.class_color(klass)
        canvas.text(nx, gy - 2, _ellipsis(canvas, name, name_font, limit), name_font,
                    _rgb(colour) if colour else INK)
        if sub:
            # Same bound as the name: on the page the sub-line lives inside `.who`, which
            # stops where `.val` starts, so a long boss name ends in an ellipsis rather
            # than running under the pill.
            canvas.text(nx, gy + 17, _ellipsis(canvas, str(sub), sub_font, limit),
                        sub_font, MUTED)


def render(summary, guild_name=None, night_text=None, raid_name=None, difficulty=None,
           raiders=None, *, cells=None, kicker="RAID RECAP"):
    """The grid as PNG bytes, or None if anything at all went wrong."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    try:
        cells = _cells(summary) if cells is None else cells
        if len(cells) != COLUMNS * ROWS:
            raise ValueError("A recap card requires six categories")
        col_w = (WIDTH_CSS - 2 * PAD - (COLUMNS - 1) * COL_GAP) / COLUMNS
        col_h = COL_HEAD + ROW_H * 3

        # The page's header, measured before the canvas exists so the height is exact.
        chips = list(summary.get("bossLabels") or summary.get("bosses") or [])
        measure_image = Image.new("RGB", (1, 1))
        measure = _Canvas(measure_image, ImageDraw.Draw(measure_image), {})
        chip_font = measure.font("semibold", 12)
        chip_rows = [[]]
        cx = PAD
        for label in chips:
            label = _ellipsis(measure, label, chip_font, WIDTH_CSS - 2 * PAD - 20)
            cw = measure.width(label, chip_font) + 20
            if chip_rows[-1] and cx + cw > WIDTH_CSS - PAD:
                chip_rows.append([])
                cx = PAD
            chip_rows[-1].append((cx, cw, label))
            cx += cw + 8
        chip_height = 30 * len(chip_rows) if chips else 24
        head_h = 24 + 18 + 34 + 24 + chip_height + 8
        height = TOPBAR + head_h + ROWS * col_h + (ROWS - 1) * COL_GAP + PAD

        image = Image.new("RGB", (WIDTH_CSS * SCALE, int(height * SCALE)), BG)
        canvas = _Canvas(image, ImageDraw.Draw(image), {})

        # .topbar
        canvas.rect(0, 0, WIDTH_CSS, TOPBAR, fill=TOPBAR_BG)
        canvas.hline(0, WIDTH_CSS, TOPBAR, LINE)
        brand = canvas.font("semibold", 15)
        canvas.text(PAD, (TOPBAR - 18) / 2, "ryangrey.dev", brand, INK)
        lede_font = canvas.font("regular", 15)
        canvas.text(WIDTH_CSS - PAD - canvas.width("greyBot", lede_font),
                    (TOPBAR - 18) / 2, "greyBot", lede_font, MUTED)

        # .kicker, h1, .lede, .killed
        y = TOPBAR + 24
        canvas.text(PAD, y, kicker, canvas.font("regular", 12), MUTED, spacing=2.5)
        y += 18
        title = " — ".join(s for s in (guild_name, night_text) if s)
        h1 = canvas.font("bold", 26)
        canvas.text(PAD, y, _ellipsis(canvas, title, h1, WIDTH_CSS - 2 * PAD), h1, INK)
        y += 34
        sub = " ".join(s for s in (difficulty, raid_name) if s)
        if raiders:
            sub = f"{sub} · {int(raiders)} raiders" if sub else f"{int(raiders)} raiders"
        canvas.text(PAD, y, sub, lede_font, MUTED)
        y += 24
        if chips:
            chip_font = canvas.font("semibold", 12)
            for row in chip_rows:
                for cx, cw, label in row:
                    canvas.rect(cx, y + 4, cx + cw, y + 26,
                                fill=CHIP_ACCENT_BG, radius=11)
                    canvas.text(cx + 10, y + 7, label, chip_font, ACCENT)
                y += 30
        else:
            canvas.text(PAD, y, "No kills — a full night on progression.", lede_font,
                        MUTED)
            y += 24
        y += 8

        # .cols
        for i, (title, icon, rows, empty, badge) in enumerate(cells):
            x = PAD + (i % COLUMNS) * (col_w + COL_GAP)
            cy = y + (i // COLUMNS) * (col_h + COL_GAP)
            _column(canvas, x, cy, col_w, title, icon, rows, empty, badge)

        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:                                          # noqa: BLE001
        return None
