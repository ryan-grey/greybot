"""The first-kill announcement, drawn as an image.

WHY AN IMAGE AT ALL. A Discord embed offers exactly two image slots -- `thumbnail`, always
top-right and small, and `image`, always full width underneath the text -- and the API has
no field for the position or the rendered size of either. Their docs are explicit that a
thumbnail's height and width are values Discord returns after fetching the file, not values
a sender sets. Art beside the text, filling the card's height, is therefore not something
an embed can be asked for. Drawing the card is the only way to have it.

WHAT IT COSTS, stated plainly because it is not free: the text in a PNG cannot be selected,
copied, searched or read by a screen reader, and no part of it can be clicked. The embed
keeps an author block so the Raider.IO attribution link survives, which is a requirement of
using their data rather than a nicety.

THE ART IS REAL ART. Blizzard's icon CDN serves 56px and 403s every larger size, so a
56px icon stretched to card height would be a blurred square -- which is what made this
look bad rather than the layout. The Game Data API's creature portraits are 600x600, the
bot already resolves them for the thumbnail, and at 600 there is enough to fill a 300px
panel without upscaling anything.

Fonts are DejaVu, vendored under assets/fonts with its licence. A Lambda has no system
fonts, and macOS's are Apple's to license rather than mine to ship.

Every failure here returns None. A card that could not be drawn falls back to the ordinary
embed, because an announcement that does not go out is a far worse outcome than one that
goes out looking like it did last week.
"""

import io
import os
import math
import urllib.request

def _font_dir():
    """Where the vendored fonts live, in the package and in the checkout.

    Resolved from THIS FILE rather than from the working directory. build-lambda.sh copies
    them to fonts/ beside the module, and a checkout has them under assets/fonts -- so the
    same code draws the same card whether it is running in Lambda or on a laptop, and
    neither depends on where the process happened to be started from.
    """
    override = os.environ.get("FONT_DIR")
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, "fonts"),
                      os.path.join(os.path.dirname(here), "assets", "fonts")):
        if os.path.isfile(os.path.join(candidate, "DejaVuSans-Bold.ttf")):
            return candidate
    return os.path.join(here, "fonts")


FONT_DIR = _font_dir()

# Brand, matching discord.BRAND_NAVY, BRAND_ACCENT and AOTC_GOLD.
NAVY = (14, 27, 44)
ACCENT = (92, 168, 240)
GOLD = (232, 180, 74)          # AOTC only, and the reason `accent` is a parameter
SILVER = (198, 208, 222)       # a Normal clear: a milestone, not the milestone
INK = (238, 243, 250)
MUTED = (150, 168, 190)

# 3.33:1. Discord scales an embed image to about 550px wide, so this lands near 550x165 on
# a desktop client -- wide enough for the boss name at a readable size, short enough that
# the card does not dominate a channel it posts into a handful of times a tier.
WIDTH, HEIGHT = 1000, 300
ART = 300                      # the art panel is square and full height, by definition


def _load(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "greybot/1.0 (+card)"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def _fit(text, path, draw, limit, size, floor):
    """The largest font size at which `text` fits, shrinking before it ever truncates.

    Truncation is the wrong first move for this card. The two longest strings on it are a
    boss name and a raid name, and those are exactly the two things a reader is looking
    for -- "Nek'zali the Soulc…" fails at the one job the line has. Shrinking costs a few
    points of type on the longest names and nothing at all on the short ones.

    An ellipsis is still the floor, because a name long enough to defeat even the smallest
    size has to stop somewhere.
    """
    from PIL import ImageFont
    while size > floor:
        font = ImageFont.truetype(path, size)
        if draw.textlength(text, font=font) <= limit:
            return text, font
        size -= 2
    font = ImageFont.truetype(path, floor)
    if draw.textlength(text, font=font) <= limit:
        return text, font
    while text and draw.textlength(text + "…", font=font) > limit:
        text = text[:-1]
    return (text + "…") if text else "", font


def _draw_fireworks(draw, frame, accent):
    """Paint a quiet, deterministic burst field behind a clear card's foreground.

    It is deliberately geometry rather than downloaded GIF art: cards keep working in
    Lambda without a new network dependency, and the same event renders identically on
    retries.  The dark, low-alpha-looking colours keep the raid name readable while the
    changing particle radius makes the animation visibly celebrate a tier clear.
    """
    progress = frame / 24
    bursts = ((390, 95, 0.92), (725, 75, 0.68), (875, 210, 0.48))
    for cx, cy, phase in bursts:
        age = (progress + phase) % 1.0
        radius = 14 + age * 118
        # A brief bright core and fading trails: enough movement to read as fireworks,
        # never enough opaque colour to fight the foreground typography.
        for ray in range(18):
            angle = (math.tau * ray / 18) + phase * 3
            inner = max(4, radius - 25)
            outer = radius
            x1 = cx + math.cos(angle) * inner
            y1 = cy + math.sin(angle) * inner
            x2 = cx + math.cos(angle) * outer
            y2 = cy + math.sin(angle) * outer
            fade = int(88 * (1 - age))
            colour = tuple(min(255, c // 3 + fade) for c in accent)
            draw.line((x1, y1, x2, y2), fill=colour, width=2)
            draw.ellipse((x2 - 2, y2 - 2, x2 + 2, y2 + 2), fill=colour)
        core = tuple(min(255, c // 2 + 80) for c in accent)
        draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=core)


def _card_art(art_url):
    """Fetch and fit optional encounter art once for an entire card render."""
    if not art_url:
        return None
    try:
        from PIL import Image
        art = Image.open(io.BytesIO(_load(art_url))).convert("RGB")
        side = min(art.size)
        left = (art.width - side) // 2
        top = (art.height - side) // 2
        return art.crop((left, top, left + side, top + side)).resize((ART, ART), Image.LANCZOS)
    except Exception:                                          # noqa: BLE001
        return None


def _render_frame(boss_name, headline, lines, art, accent, frame=None):
    from PIL import Image, ImageDraw

    card = Image.new("RGB", (WIDTH, HEIGHT), NAVY)
    draw = ImageDraw.Draw(card)
    if frame is not None:
        _draw_fireworks(draw, frame, accent)

    regular = os.path.join(FONT_DIR, "DejaVuSans.ttf")
    bold = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")

    text_left = 40
    if art:
        card.paste(art, (0, 0))
        # A hairline in the accent, so the art reads as part of the card rather
        # than as a picture someone dropped on top of it.
        draw.rectangle([ART, 0, ART + 2, HEIGHT], fill=accent)
        text_left = ART + 34

    limit = WIDTH - text_left - 40
    y = 44
    text, font = _fit(headline, regular, draw, limit, 30, 22)
    draw.text((text_left, y), text, font=font, fill=MUTED)

    y += 44
    text, font = _fit(boss_name, bold, draw, limit, 54, 32)
    draw.text((text_left, y), text, font=font, fill=accent)

    y += 78
    for line in lines or ():
        text, font = _fit(line, regular, draw, limit, 30, 21)
        draw.text((text_left, y), text, font=font, fill=INK)
        y += 40
    return card


def render(boss_name, headline, lines, art_url=None, accent=ACCENT):
    """The card as PNG bytes, or None if anything at all went wrong.

    `lines` is the body text already composed by the caller -- this module decides how a
    card looks and nothing about what it says, so the wording lives in one place with the
    embed's.

    `accent` colours the headline word and the hairline beside the art, and is the ONLY
    thing that separates a kill card from an AOTC one. Two renderers would be two places
    for the layout to drift; the gold is a parameter precisely so it cannot.
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        card = _render_frame(boss_name, headline, lines, _card_art(art_url), accent)

        out = io.BytesIO()
        card.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:                                          # noqa: BLE001
        return None


def render_clear(boss_name, headline, lines, art_url=None, accent=GOLD):
    """The looping GIF used only for Normal and Heroic full-raid clear cards."""
    try:
        art = _card_art(art_url)
        frames = [_render_frame(boss_name, headline, lines, art, accent, frame=i)
                  for i in range(24)]
        out = io.BytesIO()
        frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:],
                       duration=100, loop=0, optimize=True, disposal=2)
        return out.getvalue()
    except Exception:                                          # noqa: BLE001
        return None
