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
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    try:
        card = Image.new("RGB", (WIDTH, HEIGHT), NAVY)
        draw = ImageDraw.Draw(card)

        regular = os.path.join(FONT_DIR, "DejaVuSans.ttf")
        bold = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")

        text_left = 40
        if art_url:
            try:
                art = Image.open(io.BytesIO(_load(art_url))).convert("RGB")
                # Square-crop first so a non-square source is not squashed, then resize
                # ONCE. Resizing a stretched image bakes the stretch in.
                side = min(art.size)
                left = (art.width - side) // 2
                top = (art.height - side) // 2
                art = art.crop((left, top, left + side, top + side))
                art = art.resize((ART, ART), Image.LANCZOS)
                card.paste(art, (0, 0))
                # A hairline in the accent, so the art reads as part of the card rather
                # than as a picture someone dropped on top of it.
                draw.rectangle([ART, 0, ART + 2, HEIGHT], fill=accent)
                text_left = ART + 34
            except Exception:                                  # noqa: BLE001
                # No art is a layout, not a failure: the text simply starts at the margin.
                text_left = 40

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

        out = io.BytesIO()
        card.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:                                          # noqa: BLE001
        return None
