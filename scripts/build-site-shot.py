#!/usr/bin/env python3
"""Composite the greyBot shot for the ryangrey.dev project card.

    .venv/bin/python scripts/build-site-shot.py \
        --card https://raids.ryangrey.dev/cards/the-venomous-abyss/vashnik-the-malignant.png \
        --card https://raids.ryangrey.dev/cards/the-venomous-abyss/meers-raid/normal/nekzali-the-soulcoiler.png \
        --card ~/Documents/greybot-assets/aotc-card.png \
        --page recap-dark.png --out greybot-cards.png

Left column: three of the bot's own drawn cards, stacked, exactly as they were posted --
a Heroic first kill, a Normal first kill from the second team, and the gold AOTC card.
Right column: the top of a published recap page. Every pixel is something the bot
produced; nothing here is a screen capture of Discord, so it stays crisp and stays honest
about what the bot draws versus what Discord draws around it.

The page capture is a headless-Chrome screenshot of the live page at 1200px wide:

    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
        --disable-gpu --hide-scrollbars --window-size=1200,1500 \
        --screenshot=recap-dark.png https://raids.ryangrey.dev/<night>/

Output is indexed to 256 colours, which the flat card art and the page survive without
visible loss and which is what keeps the file small enough for a page that loads nothing
else it does not need.
"""

import argparse
import io
import os
import sys
import urllib.request

from PIL import Image

NAVY = (14, 27, 44)
CARD_W, CARD_H = 1000, 300
GAP = 40


def load(src):
    if src.startswith("http://") or src.startswith("https://"):
        with urllib.request.urlopen(src, timeout=20) as res:
            return Image.open(io.BytesIO(res.read())).convert("RGB")
    return Image.open(os.path.expanduser(src)).convert("RGB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", action="append", required=True,
                    help="path or URL of a 1000x300 card; give three")
    ap.add_argument("--page", required=True, help="screenshot of a recap page")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if len(args.card) != 3:
        sys.exit("give exactly three --card inputs")

    cards = [load(c) for c in args.card]
    for c in cards:
        if c.size != (CARD_W, CARD_H):
            sys.exit(f"card is {c.size}, expected {(CARD_W, CARD_H)}")
    column_h = CARD_H * 3 + GAP * 2

    page = load(args.page)
    # Scale the page to the card width and keep as much of its top as the column is tall.
    scale = CARD_W / page.width
    page = page.resize((CARD_W, round(page.height * scale)), Image.LANCZOS)
    page = page.crop((0, 0, CARD_W, min(column_h, page.height)))

    out = Image.new("RGB", (CARD_W * 2 + GAP, column_h), NAVY)
    for i, c in enumerate(cards):
        out.paste(c, (0, i * (CARD_H + GAP)))
    out.paste(page, (CARD_W + GAP, 0))

    indexed = out.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    indexed.save(args.out, format="PNG", optimize=True)
    print(f"{args.out}: {out.size[0]}x{out.size[1]}, {os.path.getsize(args.out) // 1024} KB")


if __name__ == "__main__":
    main()
