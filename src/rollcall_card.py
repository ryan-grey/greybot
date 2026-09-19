"""The raid-night roll call, drawn: who was in the night's first kill, and who they are.

Attendance, not performance. One narrow list in the recap card's own markup -- the raid as
Warcraft Logs recorded it, grouped tank / healer / damage, one person to a row and read the
way you would say it: picture, Discord name, an arrow, the role glyph, the character in its
class colour. Whatever could not be paired goes underneath in two short lists: characters in
the kill with nobody beside them, and members who were around for the kill but had no
character in it.

The card says "Discord" and nothing more specific. Where the member list comes from is the
caller's business (rollcall.py); the card is the public part, and it names people, not how
they were found.

Narrower than the recap card on purpose. That one is six leaderboards across; this is a list
of names, and at 640 px every row was mostly empty space that a phone then shrank.

Bots cannot take screenshots, and a screenshot would not be evidence anyway: it shows whenever
someone remembered to take it. This is drawn from the kill's own timestamp, so it is the same
answer whether the poll noticed the kill in one minute or fourteen.

Pure drawing. The caller supplies everything, including each member's picture as bytes, so
this module makes no network calls and a missing picture is a drawn circle, not a failure.

Every failure returns None, as recap_card does: decoration never costs the post.
"""

import io

import recap_card as rc
import recap_page

ROLES = (("tank", "Tanks"), ("healer", "Healers"), ("dps", "Damage"))
WIDTH = 440                    # CSS px, against the recap card's 640
ROW = 26
ARROW = "→"
NOT_IN_DISCORD = "In kill but not in Discord"
NOT_IN_KILL = "In Discord but not in kill"


def _panel(canvas, x, y, w, h, title, badge):
    canvas.rect(x, y, x + w, y + h, fill=rc.BG, outline=rc.LINE, radius=rc.RADIUS)
    canvas.rect(x + 1, y + 1, x + w - 1, y + rc.COL_HEAD, fill=rc.CHIP, radius=rc.RADIUS)
    canvas.hline(x, x + w, y + rc.COL_HEAD, rc.LINE)
    head = canvas.font("semibold", 12)
    pill = canvas.font("semibold", 11)
    pw = canvas.width(badge, pill) + 14
    canvas.text(x + 12, y + 11, rc._ellipsis(canvas, title.upper(), head, w - pw - 40), head,
                rc.INK, spacing=0.6)
    canvas.rect(x + w - 10 - pw, y + 8, x + w - 10, y + 28, fill=rc.CHIP_ACCENT_BG, radius=10)
    canvas.text(x + w - 10 - pw + 7, y + 11, badge, pill, rc.ACCENT)


def _portrait(canvas, data, x, y, size):
    """A member's picture as a circle, or an empty one when there is nothing to draw."""
    from PIL import Image, ImageDraw
    s = rc.SCALE
    box = [x * s, y * s, (x + size) * s, (y + size) * s]
    try:
        picture = Image.open(io.BytesIO(data)).convert("RGBA") if data else None
    except Exception:                                          # noqa: BLE001
        picture = None
    if picture is None:
        canvas.draw.ellipse(box, fill=rc.CHIP, outline=rc.LINE, width=s)
        return
    px = int(size * s)
    picture = picture.resize((px, px), Image.Resampling.LANCZOS)
    mask = Image.new("L", (px * 4, px * 4), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, px * 4, px * 4], fill=255)
    canvas.image.paste(picture, (int(x * s), int(y * s)),
                       mask.resize((px, px), Image.Resampling.LANCZOS))


def _clean(name):
    return " ".join(str(name or "").split())


def pair(lineup, members):
    """(rows, unclaimed, outside): every character with the member who plays it or None, the
    characters nobody was paired with, and the members who had no character in the kill."""
    by_character = {id(m["character"]): m for m in members if m.get("character")}
    rows = [(p, by_character.get(id(p))) for p in lineup]
    return (rows, [p for p, m in rows if m is None],
            [m for m in members if not m.get("character")])


def _chip_rows(canvas, labels, font, width):
    """Chips wrapped to the card's width: [[(x, w, label), ...], ...]."""
    out, cx = [[]], rc.PAD
    for label in labels:
        cw = canvas.width(label, font) + 20
        if out[-1] and cx + cw > width - rc.PAD:
            out.append([])
            cx = rc.PAD
        out[-1].append((cx, cw, label))
        cx += cw + 8
    return out


def render(team_name, boss, sub, lineup, members, pictures=None):
    """PNG bytes, or None.

    `lineup` is wcl.kill_lineup's list. `members` is rollcall.assign's output: [{"id", "name",
    "character": the lineup row they played, or None}]. `pictures` maps member id to bytes.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    try:
        pictures = pictures or {}
        rows, unclaimed, outside = pair(lineup, members)
        full = WIDTH - 2 * rc.PAD
        labels = [f"{len(lineup)} in the kill"]
        if outside:
            labels.append(f"{len(outside)} in Discord, not in kill")
        if unclaimed:
            labels.append(f"{len(unclaimed)} in kill, not in Discord")
        measure_image = Image.new("RGB", (1, 1))
        measure = rc._Canvas(measure_image, ImageDraw.Draw(measure_image), {})
        chips = _chip_rows(measure, labels, measure.font("semibold", 12), WIDTH)

        def box(count):
            return rc.COL_HEAD + 8 + max(count, 1) * ROW + 8

        heads = sum(1 for role, _t in ROLES if any(p["role"] == role for p in lineup))
        body = box(len(lineup) + heads)
        tails = [(title, people) for title, people in ((NOT_IN_DISCORD, unclaimed),
                                                       (NOT_IN_KILL, outside)) if people]
        head = 24 + 18 + 34 + 22 + 30 * len(chips) + 8
        height = (rc.TOPBAR + head + body + sum(rc.COL_GAP + box(len(p)) for _t, p in tails)
                  + rc.PAD)

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
        kicker = " · ".join(s for s in ("ROLL CALL", (team_name or "").upper(), "FIRST KILL") if s)
        canvas.text(rc.PAD, y, kicker, canvas.font("regular", 12), rc.MUTED, spacing=2.5)
        y += 18
        h1 = canvas.font("bold", 26)
        canvas.text(rc.PAD, y, rc._ellipsis(canvas, boss, h1, full), h1, rc.INK)
        y += 34
        sub_font = canvas.font("regular", 13)
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
        member_font = canvas.font("regular", 13)
        gap = canvas.width(" ", name_font) + 2

        def character(p, x, ry, limit):
            canvas.glyph(p["role"], x, ry + 6, 14)
            colour = rc._rgb(recap_page.class_color(p.get("class") or "") or "#f0f6fc")
            canvas.text(x + 14 + gap, ry + 5,
                        rc._ellipsis(canvas, p["name"], name_font, limit - x - 14 - gap),
                        name_font, colour)

        def member(m, x, ry, limit, font=member_font):
            """Picture and name; returns where the name ended."""
            _portrait(canvas, pictures.get(m["id"]), x, ry + 3, 20)
            text = rc._ellipsis(canvas, _clean(m["name"]), font, limit - x - 20 - gap)
            canvas.text(x + 20 + gap, ry + 5, text, font, rc.INK)
            return x + 20 + gap + canvas.width(text, font)

        left, edge = rc.PAD + 12, rc.PAD + full - 12
        _panel(canvas, rc.PAD, y, full, body, "In the kill", str(len(lineup)))
        ry = y + rc.COL_HEAD + 8
        for role, title in ROLES:
            group = [(p, m) for p, m in rows if p["role"] == role]
            if not group:
                continue
            canvas.text(left, ry + 7, f"{title.upper()} · {len(group)}", small, rc.MUTED,
                        spacing=1.2)
            ry += ROW
            for p, m in group:
                x = left
                if m:
                    # The character is never the part that gets cut: the member's name gives way.
                    need = 14 + gap + canvas.width(p["name"], name_font)
                    x = member(m, left, ry, edge - need - 2 * gap - canvas.width(ARROW, member_font))
                    canvas.text(x + gap, ry + 5, ARROW, member_font, rc.MUTED)
                    x += 2 * gap + canvas.width(ARROW, member_font)
                character(p, x, ry, edge)
                ry += ROW
        if not lineup:
            canvas.text(left, ry + 7, "The log lists nobody for this kill.", small, rc.MUTED)

        ty = y + body
        for title, people in tails:
            ty += rc.COL_GAP
            _panel(canvas, rc.PAD, ty, full, box(len(people)), title, str(len(people)))
            for i, person in enumerate(people):
                row_y = ty + rc.COL_HEAD + 8 + i * ROW
                if title == NOT_IN_KILL:
                    member(person, left, row_y, edge, font=name_font)
                else:
                    character(person, left, row_y, edge)
            ty += box(len(people))

        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:                                          # noqa: BLE001
        return None
