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


def render_clear(boss_name, headline, lines, art_url=None, accent=GOLD, achievement=False):
    """The looping GIF used only for Normal and Heroic full-raid clear cards."""
    try:
        if achievement:
            return render_achievement(headline, boss_name, lines)
        art = _card_art(art_url)
        frames = [_render_frame(boss_name, headline, lines, art, accent, frame=i)
                  for i in range(24)]
        out = io.BytesIO()
        frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:],
                       duration=100, loop=0, optimize=True, disposal=2)
        return out.getvalue()
    except Exception:                                          # noqa: BLE001
        return None


def render_achievement(headline, difficulty, lines):
    """Canvas dragon-frame Heroic clear animation; Normal remains render_clear's card."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        here = os.path.dirname(os.path.abspath(__file__))
        asset = next((p for p in (os.path.join(here, "aotc-golden-dragon-frame.png"),
                                  os.path.join(os.path.dirname(here), "assets", "aotc-golden-dragon-frame.png"))
                      if os.path.isfile(p)), "")
        border = Image.open(asset).convert("RGBA")
        regular = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans.ttf"), 28)
        big = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"), 62)
        head = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans.ttf"), 31)
        frames=[]
        for i in range(24):
            card=Image.new("RGBA", border.size, NAVY+(255,)); draw=ImageDraw.Draw(card)
            for cx,cy,phase in ((380,180,.15),(820,180,.55),(600,120,.82)):
                age=(i/24+phase)%1; radius=15+age*90
                for ray in range(16):
                    angle=math.tau*ray/16; x=cx+math.cos(angle)*radius; y=cy+math.sin(angle)*radius
                    draw.ellipse((x-2,y-2,x+2,y+2),fill=(235,177,56,int(80*(1-age))))
            card.alpha_composite(border)
            def label(text,y,font,fill):
                box=draw.textbbox((0,0),text,font=font); draw.text(((1200-(box[2]-box[0]))/2,y),text,font=font,fill=fill)
            label(headline,118,head,MUTED+(255,)); label(difficulty,154,big,GOLD+(255,)); label(lines[0],228,regular,INK+(255,)); label(lines[1],268,regular,INK+(255,))
            frames.append(card.convert("P",palette=Image.Palette.ADAPTIVE))
        out=io.BytesIO(); frames[0].save(out,format="GIF",save_all=True,append_images=frames[1:],duration=100,loop=0,optimize=True,disposal=2); return out.getvalue()
    except Exception:
        return None

# Approved AOTC achievement treatment: a full-frame gold halo, moving sheen, and fireworks.
def render_achievement(headline, difficulty, lines):
    try:
        from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
        import random
        W, H, ox, oy = 1280, 500, 40, 40
        here = os.path.dirname(os.path.abspath(__file__))
        asset = next(p for p in (os.path.join(here, "aotc-golden-dragon-frame.png"),
                                  os.path.join(os.path.dirname(here), "assets", "aotc-golden-dragon-frame.png")) if os.path.isfile(p))
        art = Image.open(asset).convert("RGBA").resize((1200, 420), Image.LANCZOS)
        frame = Image.new("RGBA", (W, H)); frame.alpha_composite(art, (ox, oy))
        mask = Image.new("L", (W, H)); src, dst = frame.load(), mask.load()
        for y in range(H):
            for x in range(W):
                r,g,b,a = src[x,y]; dst[x,y] = max(0, min(255, int((r+g-1.25*b)*.95))) if a > 10 and r >= 55 and g >= 32 and r >= b*1.15 else 0
        regular = ImageFont.truetype(os.path.join(FONT_DIR,"DejaVuSans.ttf"),31)
        datefont = ImageFont.truetype(os.path.join(FONT_DIR,"DejaVuSans.ttf"),24)
        bold = ImageFont.truetype(os.path.join(FONT_DIR,"DejaVuSans-Bold.ttf"),66)
        frames=[]
        for i in range(24):
            t=i/24; canvas=Image.new("RGBA",(W,H),(10,19,35,255)); draw=ImageDraw.Draw(canvas,"RGBA")
            for cx,cy,phase,seed in ((413,191,.06,11),(873,205,.4,17),(724,164,.72,23)):
                age=(t+phase)%1; radius=20+100*age; rng=random.Random(seed)
                for ray in range(28):
                    a=math.tau*ray/28+rng.uniform(-.04,.04); x=cx+math.cos(a)*radius; y=cy+math.sin(a)*radius*.72
                    draw.line((cx,cy,x,y),fill=(255,170,28,int(150*(1-age))),width=2)
                    draw.ellipse((x-2,y-2,x+2,y+2),fill=(255,218,90,int(210*(1-age))))
            canvas.alpha_composite(frame)
            pulse=.56+.44*(.5+.5*math.sin(t*math.tau))
            for blur,col,opacity in ((27,(255,139,8),.74),(15,(255,181,23),.88),(5,(255,219,76),.82)):
                layer=Image.new("RGBA",(W,H),col+(0,)); layer.putalpha(mask.filter(ImageFilter.GaussianBlur(blur)).point(lambda v:int(v*opacity*pulse))); canvas.alpha_composite(layer)
            per=Image.new("L",(W,H)); ImageDraw.Draw(per).rounded_rectangle((254,122,1026,372),radius=24,outline=255,width=7)
            layer=Image.new("RGBA",(W,H),(255,190,25,0)); layer.putalpha(per.filter(ImageFilter.GaussianBlur(13)).point(lambda v:int(v*.92*pulse))); canvas.alpha_composite(layer)
            sweep=Image.new("L",(W,H)); c=-260+(W+520)*((t+.08)%1); ImageDraw.Draw(sweep).polygon([(c-155,-20),(c-65,-20),(c+155,H+20),(c+65,H+20)],fill=255); sheen=ImageChops.multiply(mask,sweep.filter(ImageFilter.GaussianBlur(12))); layer=Image.new("RGBA",(W,H),(255,249,201,0)); layer.putalpha(sheen.point(lambda v:int(v*.94))); canvas.alpha_composite(layer)
            d=ImageDraw.Draw(canvas)
            for text,y,font,color in ((headline,172,regular,(238,244,255)),(difficulty,201,bold,(255,183,36)),(lines[0],277,regular,(238,244,255)),(lines[1],318,datefont,(238,244,255))):
                b=d.textbbox((0,0),text,font=font); x=(W-(b[2]-b[0]))//2; d.text((x,y),text,font=font,fill=(3,8,16,220),stroke_width=3,stroke_fill=(3,8,16,220)); d.text((x,y),text,font=font,fill=color)
            frames.append(canvas.convert("P",palette=Image.Palette.ADAPTIVE))
        out=io.BytesIO(); frames[0].save(out,format="GIF",save_all=True,append_images=frames[1:],duration=80,loop=0,disposal=2,optimize=False); return out.getvalue()
    except Exception:
        return None
