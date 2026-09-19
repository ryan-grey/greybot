"""The night's grey parses, drawn: spec icon, role glyph, class-coloured name, parse.

WHY A DRAWN CARD RATHER THAN TEXT. Discord will render an emoji inside an embed, so the
spec and role icons could have been inline text -- but it will not colour a word, and the
class colour is the part that makes a list of names readable at a glance. Once the card is
being drawn for the colour, the icons may as well be real artwork rather than emoji.

WHY IT IS ATTACHED AND NOT PUBLISHED. Every other card this bot draws goes to S3 and is
linked from a public page. This one names people's worst nights, so it is uploaded straight
into the direct message instead: `raids.ryangrey.dev` has no copy, and there is no URL to
forward. That is the whole reason the feature is a DM.

Spec artwork is fetched from Discord's CDN at render time (`spec_icons`), so no Blizzard
texture is committed here. Every fetch is optional: an icon that will not load leaves the
row with its role glyph and class colour, which is still the answer.

Returns None on any failure, as the other cards do — the caller then sends its text list.
"""

import io
import urllib.request

import recap_card as rc
import recap_page
import spec_icons

WIDTH = 460
ROW = 26
HEAD = 22                # a boss heading line
USER_AGENT = "greyBot/1.0 (+https://greybot.ryangrey.dev/about)"
MAX_ROWS = 28


def fetch_icons(entries, opener=urllib.request.urlopen):
    """{(class, spec): bytes} for the specs on this card. Best effort, one fetch per
    distinct spec rather than per row."""
    out = {}
    for entry in entries:
        pair = (entry.get("class") or "", entry.get("spec") or "")
        if pair in out or not spec_icons.url(*pair):
            continue
        try:
            request = urllib.request.Request(spec_icons.url(*pair),
                                             headers={"User-Agent": USER_AGENT})
            with opener(request, timeout=4) as response:
                out[pair] = response.read(262144)
        except Exception:                                      # noqa: BLE001
            continue
    return out


def _icon(canvas, data, x, y, size):
    from PIL import Image
    if not data:
        return
    try:
        art = Image.open(io.BytesIO(data)).convert("RGBA").resize(
            (int(size * rc.SCALE), int(size * rc.SCALE)), Image.Resampling.LANCZOS)
    except Exception:                                          # noqa: BLE001
        return
    canvas.image.paste(art, (int(x * rc.SCALE), int(y * rc.SCALE)), art)


def render(entries, *, team_name="", difficulty="", raid="", night_text="", threshold=25.0,
           icons=None):
    """PNG bytes, or None."""
    if not entries:
        return None
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    try:
        icons = icons if icons is not None else fetch_icons(entries)
        shown = entries[:MAX_ROWS]
        bosses = len({e["boss"] for e in shown})
        full = WIDTH - 2 * rc.PAD
        body = rc.COL_HEAD + 8 + len(shown) * ROW + bosses * HEAD + 8
        head = 24 + 18 + 34 + 22 + 8
        extra = 20 if len(entries) > MAX_ROWS else 0
        height = rc.TOPBAR + head + body + extra + rc.PAD

        image = Image.new("RGB", (WIDTH * rc.SCALE, int(height * rc.SCALE)), rc.BG)
        canvas = rc._Canvas(image, ImageDraw.Draw(image), {})

        canvas.rect(0, 0, WIDTH, rc.TOPBAR, fill=rc.TOPBAR_BG)
        canvas.hline(0, WIDTH, rc.TOPBAR, rc.LINE)
        canvas.text(rc.PAD, (rc.TOPBAR - 18) / 2, "ryangrey.dev", canvas.font("semibold", 15), rc.INK)
        lede = canvas.font("regular", 15)
        canvas.text(WIDTH - rc.PAD - canvas.width("greyBot", lede), (rc.TOPBAR - 18) / 2,
                    "greyBot", lede, rc.MUTED)

        y = rc.TOPBAR + 24
        canvas.text(rc.PAD, y, "GREY PARSES · PRIVATE", canvas.font("regular", 12), rc.MUTED,
                    spacing=2.5)
        y += 18
        h1 = canvas.font("bold", 24)
        canvas.text(rc.PAD, y, rc._ellipsis(canvas, night_text or "Last raid", h1, full), h1, rc.INK)
        y += 32
        sub_font = canvas.font("regular", 13)
        people = len({e["name"] for e in entries})
        sub = " · ".join(s for s in (team_name, difficulty, raid) if s)
        canvas.text(rc.PAD, y, rc._ellipsis(canvas, sub, sub_font, full), sub_font, rc.MUTED)
        y += 20
        note = (f"{len(entries)} parse{'s' if len(entries) != 1 else ''} under {threshold:g}% "
                f"from {people} raider{'s' if people != 1 else ''}")
        canvas.text(rc.PAD, y, note, canvas.font("regular", 12), rc.ACCENT)
        y += 22

        # The page's real role artwork rather than the drawn polygons. Set on this canvas
        # only: recap_card reads it off the instance, and setting it on the class would
        # change how every other card in the same Lambda draws.
        canvas.signup_role_icons = True
        canvas.rect(rc.PAD, y, rc.PAD + full, y + body, fill=rc.BG, outline=rc.LINE,
                    radius=rc.RADIUS)
        canvas.rect(rc.PAD + 1, y + 1, rc.PAD + full - 1, y + rc.COL_HEAD, fill=rc.CHIP,
                    radius=rc.RADIUS)
        canvas.hline(rc.PAD, rc.PAD + full, y + rc.COL_HEAD, rc.LINE)
        canvas.text(rc.PAD + 12, y + 11, "IN THE KILL, PARSING GREY", canvas.font("semibold", 12),
                    rc.INK, spacing=0.6)

        name_font, small = canvas.font("semibold", 13), canvas.font("regular", 11)
        mono = canvas.font("semibold", 12)
        ry = y + rc.COL_HEAD + 8
        current = object()
        for entry in shown:
            if entry["boss"] != current:
                current = entry["boss"]
                number = f"Boss {entry['number']} · " if entry.get("number") else ""
                canvas.text(rc.PAD + 12, ry + 5,
                            rc._ellipsis(canvas, (number + entry["boss"]).upper(), small, full - 24),
                            small, rc.MUTED, spacing=1.2)
                ry += HEAD
            x = rc.PAD + 12
            _icon(canvas, icons.get((entry.get("class") or "", entry.get("spec") or "")),
                  x, ry + 4, 18)
            x += 24
            canvas.glyph(entry.get("role") or "dps", x, ry + 6, 14)
            x += 20
            value = f"{entry['percent']:.0f}%"
            width = canvas.width(value, mono)
            colour = rc._rgb(recap_page.class_color(entry.get("class") or "") or "#f0f6fc")
            room = rc.PAD + full - 12 - width - 10 - x
            label = entry["name"] + (f"  {entry['spec']}" if entry.get("spec") else "")
            canvas.text(x, ry + 5, rc._ellipsis(canvas, label, name_font, room), name_font, colour)
            canvas.text(rc.PAD + full - 12 - width, ry + 5, value, mono, rc.INK)
            ry += ROW
        if len(entries) > MAX_ROWS:
            canvas.text(rc.PAD, y + body + 6, f"…and {len(entries) - MAX_ROWS} more.", small, rc.MUTED)

        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:                                          # noqa: BLE001
        return None
