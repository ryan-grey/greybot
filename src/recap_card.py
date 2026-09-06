"""The morning-after recap, drawn as a grid.

WHY AN IMAGE. The recap is six leaderboards, and the embed can only lay those out as
inline fields: three to a row on desktop, and ONE column on a phone, where six stacked
lists scroll for a screen and a half and read as a wall rather than a card. An image
renders the same two-by-three grid on every client -- a phone scales it down, it does not
re-flow it -- so the shape of the card is the same thing everybody sees.

WHAT IT COSTS, same as the kill card: text in a PNG cannot be selected, searched or read
by a screen reader. The embed keeps its own six fields as the fallback, so a recap whose
image could not be drawn or published is the card it was before, not a card with a hole
in it.

THE NUMBERS ARE THE SAME NUMBERS. This module draws `summary` exactly as discord.recap_embed
reads it -- same keys, same three-per-category cap, same formatting helpers -- so the
image and its fallback can never disagree about who topped what.

Every failure returns None. Decoration never costs the post it decorates.
"""

import io
import os

import kill_card
from kill_card import FONT_DIR, NAVY, ACCENT, INK, MUTED

# 3:2 at 1200 wide. Discord shows an embed image at roughly 550px on desktop and the width
# of the screen on a phone, so everything on it is drawn about twice the size it will be
# read at. Six cells, three across, is the layout the embed already tries for.
WIDTH, HEIGHT = 1200, 800
COLUMNS, ROWS = 3, 2
MARGIN, GAP = 28, 20
HEADER = 64                    # the strip above the grid naming the night
CELL_PAD = 24

PANEL = (20, 37, 60)           # a cell, one step lighter than the navy ground
RULE = (34, 56, 86)            # hairline under a cell's title
RANK = (110, 128, 152)         # the "1." -- present, not loud

# Blizzard's class colours, so a name reads as its class the way it does on every meter
# people already look at. Priest is nudged off pure white so it is not the same colour as
# the number beside it.
CLASS_COLOURS = {
    "DeathKnight": (196, 30, 58), "DemonHunter": (163, 48, 201),
    "Druid": (255, 124, 10), "Evoker": (51, 147, 127), "Hunter": (170, 211, 114),
    "Mage": (63, 199, 235), "Monk": (0, 255, 152), "Paladin": (244, 140, 186),
    "Priest": (232, 236, 244), "Rogue": (255, 244, 104), "Shaman": (0, 112, 221),
    "Warlock": (135, 136, 238), "Warrior": (198, 155, 109),
}

# The same three role colours a raid frame uses. Drawn as a small dot in front of the
# rank rather than an icon, because a 12px icon at phone scale is a smudge and a dot is
# still a dot.
ROLE_COLOURS = {"tank": (92, 168, 240), "healer": (86, 200, 120), "dps": (232, 96, 96)}


def _short(n):
    """Import-time cycle avoidance: discord imports nothing from here, and this module
    wants discord's number formatting so the image and the fallback print the same
    string. Resolved on first use."""
    import discord
    return discord._short(n)


def _dps_and_total(row):
    import discord
    return discord._dps_and_total(row)


def _cells(summary, top_n=3):
    """The six cells in grid order, each a title, its rows, and what to say when empty.

    A row is (name, class, value, role, note). Categories are read from `summary` with the
    same keys and the same cap as the embed, deliberately: this is the embed's field list
    drawn rather than typed.
    """
    def rows(key, fmt):
        return [(r["name"], r.get("class"), fmt(r), r.get("role"), None)
                for r in (summary.get(key) or [])[:top_n]]

    parses = summary.get("parses") or {}
    top = parses.get("top") or ([parses["best"]] if parses.get("best") else [])
    return [
        ("Top damage", rows("damage", _dps_and_total), "No damage table"),
        ("Top heals", rows("healing", _dps_and_total), "No healing table"),
        ("Damage taken", rows("damageTaken", lambda r: _short(r["total"])),
         "No damage-taken table"),
        ("Most deaths", rows("deaths", lambda r: str(r["deaths"])), "Nobody died"),
        ("Best parses", [(p["name"], p.get("class"), str(int(round(p["percent"]))),
                          p.get("role"), p.get("boss")) for p in top[:top_n]],
         "No ranked kills"),
        ("Item level", rows("itemLevel", lambda r: str(r["ilvl"])), "No gear data"),
    ]


def render(summary, guild_name=None, night_text=None, raid_name=None, difficulty=None,
           raiders=None):
    """The grid as PNG bytes, or None if anything at all went wrong."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    try:
        card = Image.new("RGB", (WIDTH, HEIGHT), NAVY)
        draw = ImageDraw.Draw(card)
        regular = os.path.join(FONT_DIR, "DejaVuSans.ttf")
        bold = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")

        # Header: who and when on the left, what on the right. Both optional; the strip
        # is drawn either way so the grid sits at the same place on every card.
        left = " · ".join(s for s in (guild_name, night_text) if s)
        right = " ".join(s for s in (difficulty, raid_name) if s)
        if raiders:
            right = f"{right} · {raiders} raiders" if right else f"{raiders} raiders"
        y = MARGIN + 8
        if left:
            text, font = kill_card._fit(left, bold, draw, WIDTH // 2 - MARGIN, 30, 20)
            draw.text((MARGIN, y), text, font=font, fill=INK)
        if right:
            text, font = kill_card._fit(right, regular, draw, WIDTH // 2 - MARGIN, 26, 18)
            w = draw.textlength(text, font=font)
            draw.text((WIDTH - MARGIN - w, y + 3), text, font=font, fill=MUTED)

        top = MARGIN + HEADER
        cell_w = (WIDTH - 2 * MARGIN - (COLUMNS - 1) * GAP) // COLUMNS
        cell_h = (HEIGHT - top - MARGIN - (ROWS - 1) * GAP) // ROWS
        title_font = ImageFont.truetype(bold, 26)
        rank_font = ImageFont.truetype(regular, 26)
        value_font = ImageFont.truetype(bold, 30)
        stacked_font = ImageFont.truetype(bold, 26)
        empty_font = ImageFont.truetype(regular, 24)

        for i, (title, rows, empty) in enumerate(_cells(summary)):
            cx = MARGIN + (i % COLUMNS) * (cell_w + GAP)
            cy = top + (i // COLUMNS) * (cell_h + GAP)
            draw.rounded_rectangle([cx, cy, cx + cell_w, cy + cell_h], radius=14,
                                   fill=PANEL)
            x0, x1 = cx + CELL_PAD, cx + cell_w - CELL_PAD
            ty = cy + CELL_PAD - 4
            draw.text((x0, ty), title.upper(), font=title_font, fill=ACCENT)
            ty += 40
            draw.line([x0, ty, x1, ty], fill=RULE, width=2)
            ty += 18

            if not rows:
                draw.text((x0, ty + 6), empty, font=empty_font, fill=MUTED)
                continue

            # Rows share the cell's remaining height, so three parses with a boss line
            # each and three bare numbers both end inside the panel.
            has_notes = any(r[4] for r in rows)
            row_h = (cy + cell_h - CELL_PAD - ty) // 3
            inner = x1 - (x0 + 58)
            # A "112K/288M" beside a name leaves the name three letters and an ellipsis,
            # which fails the one job the line has. When any value in a cell is wider
            # than 45% of the row -- "288M" is not, "112K/288M" is -- every value in
            # that cell moves to its own line under the name. Per cell, not per row, so
            # a column stays a column.
            stacked = any(draw.textlength(r[2], font=value_font) > inner * 0.45
                          for r in rows)
            for n, (name, klass, value, role, note) in enumerate(rows[:3], 1):
                ry = ty + (n - 1) * row_h
                dot = ROLE_COLOURS.get(role)
                if dot:
                    draw.ellipse([x0, ry + 11, x0 + 12, ry + 23], fill=dot)
                rx = x0 + 22
                draw.text((rx, ry + 2), f"{n}.", font=rank_font, fill=RANK)
                rx += 36
                if stacked:
                    vw = draw.textlength(value, font=stacked_font)
                    draw.text((x1 - vw, ry + 36), value, font=stacked_font, fill=INK)
                    limit = x1 - rx
                else:
                    vw = draw.textlength(value, font=value_font)
                    draw.text((x1 - vw, ry), value, font=value_font, fill=INK)
                    limit = (x1 - vw) - rx - 14
                text, font = kill_card._fit(name, bold, draw, limit, 30, 20)
                draw.text((rx, ry), text, font=font,
                          fill=CLASS_COLOURS.get(klass or "", INK))
                if note and has_notes:
                    text, font = kill_card._fit(note, regular, draw, x1 - rx, 21, 16)
                    draw.text((rx, ry + 36), text, font=font, fill=MUTED)

        out = io.BytesIO()
        card.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:                                          # noqa: BLE001
        return None
