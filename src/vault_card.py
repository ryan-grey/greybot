"""The weekly vault and gear check, drawn in the roll call card's markup.

One table, one row per Prog Raider, read the way the roll call reads: picture, Discord name,
an arrow, the role glyph, the character in its class colour. Then four columns, the same on
every row so they can be read straight down:

  RAID      three boxes, filled when that vault slot is filled by Heroic or Mythic bosses
  M+ 10+    three boxes holding the key level each slot pays out at; green at +10 and up,
            amber below, an empty outline when the slot was never reached
  GEMS      OK, or how many sockets are empty (red) or below the top gem rank (amber)
  ENCHANTS  OK, or how many enchants are missing (red) or below max rank (amber)

Raiders with anything to fix come first. The slot names behind a count are in the post's
text, not on the card, which has room for a number and not a list.

An amber "RIO <date>" beside a character means Raider.IO had not refreshed them for over a
day before reset, so a short Mythic+ count may be Raider.IO's rather than theirs.

Pure drawing, like rollcall_card: pictures arrive as bytes and every failure returns None.
"""

import io
from datetime import datetime

import recap_card as rc
import recap_page
import vault
from rollcall_card import ARROW, ROW, _clean, _chip_rows, _panel, _portrait

WIDTH = 760
BOX, BOX_GAP, COL_GAP = 22, 4, 16
CELL = 88                          # the gems and enchants columns
GOOD = (63, 185, 80)               # Primer success
GOOD_BG = (18, 38, 26)
LOW = (210, 153, 34)               # Primer attention
LOW_BG = (43, 33, 12)
BAD = (248, 81, 73)                # Primer danger


def _group():
    return 3 * BOX + 2 * BOX_GAP


def _box(canvas, x, ry, text, colour, fill):
    font = canvas.font("semibold", 11)
    canvas.rect(x, ry + 4, x + BOX, ry + 22, fill=fill, outline=colour, radius=4)
    if text:
        canvas.text(x + (BOX - canvas.width(text, font)) / 2, ry + 6, text, font, colour)


def _vault(canvas, x, ry, raid_slots, levels):
    for i in range(3):
        on = i < raid_slots
        _box(canvas, x + i * (BOX + BOX_GAP), ry, "H" if on else "",
             GOOD if on else rc.LINE, GOOD_BG if on else None)
    x += _group() + COL_GAP
    for i, level in enumerate(levels):
        bx = x + i * (BOX + BOX_GAP)
        if level is None:
            _box(canvas, bx, ry, "", rc.LINE, None)
        elif level >= vault.MPLUS_LEVEL:
            _box(canvas, bx, ry, str(level), GOOD, GOOD_BG)
        else:
            _box(canvas, bx, ry, str(level), LOW, LOW_BG)


def gear_cell(gear, missing, low):
    """(text, colour) for one gear column."""
    if gear is None:
        return "unknown", rc.MUTED
    gone, weak = len(gear[missing]), len(gear[low])
    if gone and weak:
        return f"{gone} miss · {weak} low", BAD
    if gone:
        return f"{gone} missing", BAD
    if weak:
        return f"{weak} low rank", LOW
    return "OK", GOOD


def render(rows, start, end, team_name, pictures=None):
    """PNG bytes, or None. `rows` is vault.build's output; `pictures` maps member id to bytes."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    try:
        pictures = pictures or {}
        full = WIDTH - 2 * rc.PAD
        short = sum(1 for r in rows if r["flagged"])
        geared = sum(1 for r in rows if r["gear"] and any(r["gear"].values()))
        labels = [f"{len(rows)} raiders", f"{short} short on M+ {vault.MPLUS_LEVEL}+",
                  f"{geared} with gems or enchants to fix"]
        measure_image = Image.new("RGB", (1, 1))
        measure = rc._Canvas(measure_image, ImageDraw.Draw(measure_image), {})
        chips = _chip_rows(measure, labels, measure.font("semibold", 12), WIDTH)

        body = rc.COL_HEAD + 8 + (1 + max(len(rows), 1)) * ROW + 8
        head = 24 + 18 + 34 + 22 + 30 * len(chips) + 8
        height = rc.TOPBAR + head + body + rc.PAD

        image = Image.new("RGB", (WIDTH * rc.SCALE, int(height * rc.SCALE)), rc.BG)
        canvas = rc._Canvas(image, ImageDraw.Draw(image), {})

        canvas.rect(0, 0, WIDTH, rc.TOPBAR, fill=rc.TOPBAR_BG)
        canvas.hline(0, WIDTH, rc.TOPBAR, rc.LINE)
        canvas.text(rc.PAD, (rc.TOPBAR - 18) / 2, "ryangrey.dev", canvas.font("semibold", 15),
                    rc.INK)
        lede = canvas.font("regular", 15)
        canvas.text(WIDTH - rc.PAD - canvas.width("greyBot", lede), (rc.TOPBAR - 18) / 2,
                    "greyBot", lede, rc.MUTED)

        y = rc.TOPBAR + 24
        kicker = " · ".join(s for s in ("VAULT & GEAR", (team_name or "").upper(), "WEEKLY") if s)
        canvas.text(rc.PAD, y, kicker, canvas.font("regular", 12), rc.MUTED, spacing=2.5)
        y += 18
        canvas.text(rc.PAD, y, vault.week_label(start, end), canvas.font("bold", 26), rc.INK)
        y += 34
        sub_font = canvas.font("regular", 13)
        sub = (f"Raid: Heroic+ bosses 2 / 4 / 6  ·  Mythic+: keys 1 / 4 / 8 at +{vault.MPLUS_LEVEL}"
               f"  ·  Gear: missing or below max rank")
        canvas.text(rc.PAD, y, rc._ellipsis(canvas, sub, sub_font, full), sub_font, rc.MUTED)
        y += 22
        chip = canvas.font("semibold", 12)
        for line in chips:
            for cx, cw, label in line:
                canvas.rect(cx, y + 4, cx + cw, y + 26, fill=rc.CHIP_ACCENT_BG, radius=11)
                canvas.text(cx + 10, y + 7, label, chip, rc.ACCENT)
            y += 30
        y += 8

        name_font, small = canvas.font("semibold", 13), canvas.font("regular", 11)
        member_font, cell_font = canvas.font("regular", 13), canvas.font("semibold", 12)
        gap = canvas.width(" ", name_font) + 2
        left, edge = rc.PAD + 12, rc.PAD + full - 12
        enchants_x = edge - CELL
        gems_x = enchants_x - COL_GAP - CELL
        mplus_x = gems_x - COL_GAP - _group()
        raid_x = mplus_x - COL_GAP - _group()
        limit = raid_x - 16

        _panel(canvas, rc.PAD, y, full, body, "Raiders", str(len(rows)))
        ry = y + rc.COL_HEAD + 8
        for x, title in ((left, "RAIDER → CHARACTER"), (raid_x, "RAID"),
                         (mplus_x, f"M+ {vault.MPLUS_LEVEL}+"), (gems_x, "GEMS"),
                         (enchants_x, "ENCHANTS")):
            canvas.text(x, ry + 7, title, small, rc.MUTED, spacing=1.2)
        ry += ROW

        for r in rows:
            _portrait(canvas, pictures.get(r["id"]), left, ry + 3, 20)
            label = r.get("character") if r.get("realm") else "no character on file"
            want = 14 + gap + canvas.width(label or "", name_font)
            room = limit - left - 20 - gap - want - 2 * gap - canvas.width(ARROW, member_font)
            text = rc._ellipsis(canvas, _clean(r["member"]), member_font, max(room, 48))
            canvas.text(left + 20 + gap, ry + 5, text, member_font, rc.INK)
            x = left + 20 + gap + canvas.width(text, member_font)
            canvas.text(x + gap, ry + 5, ARROW, member_font, rc.MUTED)
            x += 2 * gap + canvas.width(ARROW, member_font)
            if not r.get("realm"):
                canvas.text(x, ry + 5, rc._ellipsis(canvas, label, member_font, limit - x),
                            member_font, rc.MUTED)
            else:
                if r.get("role"):
                    canvas.glyph(r["role"], x, ry + 6, 14)
                    x += 14 + gap
                colour = rc._rgb(recap_page.class_color(r.get("class") or "") or "#f0f6fc")
                name = rc._ellipsis(canvas, r["character"], name_font, limit - x)
                canvas.text(x, ry + 5, name, name_font, colour)
                if r.get("seen") and r["flagged"]:
                    seen = datetime.fromisoformat(r["seen"])
                    note = f"RIO {seen:%b} {seen.day}"
                    nx = x + canvas.width(name, name_font) + gap
                    if nx + canvas.width(note, small) <= limit:
                        canvas.text(nx, ry + 7, note, small, LOW)
            _vault(canvas, raid_x, ry, r["raid_slots"], r["mplus_levels"])
            for cx, keys in ((gems_x, ("gem_empty", "gem_low")),
                             (enchants_x, ("enchant_missing", "enchant_low"))):
                cell, colour = gear_cell(r["gear"], *keys) if r.get("realm") else ("", rc.MUTED)
                canvas.text(cx, ry + 5, rc._ellipsis(canvas, cell, cell_font, CELL), cell_font,
                            colour)
            ry += ROW

        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:                                          # noqa: BLE001
        return None
