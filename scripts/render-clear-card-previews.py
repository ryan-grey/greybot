#!/usr/bin/env python3
"""Render all production clear-card variants without posting or publishing anything."""

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import discord  # noqa: E402
import kill_card  # noqa: E402


CASES = (
    ("Saturday Raid", "Normal"), ("Saturday Raid", "Heroic"),
    ("Meer's Raid", "Normal"), ("Meer's Raid", "Heroic"),
    ("Prog Raid", "Normal"), ("Prog Raid", "Heroic"),
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--art-url", help="Existing boss-art URL (or file:// URL) for layout review")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    from PIL import Image, ImageDraw, ImageFont

    tiles = []
    for team, difficulty in CASES:
        copy = discord.clear_card_copy(team, "The Venomous Abyss",
                                       "September 19, 2026 at 10:44 PM EDT", difficulty)
        accent = kill_card.GOLD if difficulty == "Heroic" else kill_card.SILVER
        animated = kill_card.render_clear(copy["difficulty"], copy["headline"], copy["lines"],
                                          art_url=args.art_url, accent=accent)
        if not animated:
            raise RuntimeError(f"could not render {team} {difficulty}")
        name = f"{team.lower().replace(' ', '-').replace(chr(39), '')}-{difficulty.lower()}"
        (args.out_dir / f"{name}.gif").write_bytes(animated)
        image = Image.open(io.BytesIO(animated)).convert("RGB")
        tiles.append((f"{team} · {difficulty}", image.copy()))

    label_height, padding = 42, 24
    sheet = Image.new("RGB", (kill_card.WIDTH * 2 + padding * 3,
                               (kill_card.HEIGHT + label_height) * 3 + padding * 4),
                      (8, 17, 29))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(str(Path(kill_card.FONT_DIR) / "DejaVuSans-Bold.ttf"), 25)
    for index, (label, image) in enumerate(tiles):
        col, row = index % 2, index // 2
        x = padding + col * (kill_card.WIDTH + padding)
        y = padding + row * (kill_card.HEIGHT + label_height + padding)
        draw.text((x, y), label, font=font, fill=kill_card.INK)
        sheet.paste(image, (x, y + label_height))
    sheet.save(args.out_dir / "contact-sheet.png", optimize=True)
    print(args.out_dir / "contact-sheet.png")


if __name__ == "__main__":
    main()
