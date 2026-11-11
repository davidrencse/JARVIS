"""Draw the JARVIS Phone app icons into jarvis/web/phone/icons/ from the console's five-block mark.

Run it from the repo root with the project venv, which has Pillow:

    .venv/bin/python scripts/phone_icons.py

It lives outside jarvis/ on purpose: everything under jarvis/ ships to the nodes and counts toward
their code fingerprint, and this is a build step, not runtime code. The outputs are committed, so
the phone server only ever serves files and never draws anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "jarvis" / "web" / "phone" / "icons"

# The five blocks of jarvis/web/icon.svg in its 24-unit grid, as (x, y, width, height).
# Together they span x and y from 4 to 20, a 16-unit square.
BLOCKS = ((4, 4, 7, 7), (13, 4, 7, 3), (13, 9, 7, 2), (4, 13, 3, 7), (9, 13, 11, 7))
MARK_PATH = "M4 4h7v7H4zM13 4h7v3h-7zM13 9h7v2h-7zM4 13h3v7H4zM9 13h11v7H9z"
INK = (242, 242, 240)   # the console icon's ink, #f2f2f0
FIELD = (0, 0, 0)       # the phone app's field, #000 (the manifest's background and theme colour)

# The mark covers half the tile. Its corners then sit 35% of the tile from the centre, inside the
# 40% circle Android keeps when it masks an icon, so one PNG serves as "any" and as "maskable".
MARK = 0.5
SUPERSAMPLE = 4  # draw larger, then shrink, so the block edges come out clean

# name -> pixel size. Every PNG is opaque RGB: iOS fills transparent apple-touch pixels with black
# anyway, and Android's launcher shows an opaque square best.
SIZES = {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180}

# The browser tab icon: the console's tile (rounded, mark at 80%), on the phone's black.
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
       '<rect width="24" height="24" rx="4" fill="#000"/>'
       f'<path fill="#f2f2f0" d="{MARK_PATH}" transform="translate(2.4 2.4) scale(.8)"/></svg>\n')


def draw(size: int) -> Image.Image:
    """The mark centred on a black square of size x size pixels."""
    big = size * SUPERSAMPLE
    image = Image.new("RGB", (big, big), FIELD)
    pen = ImageDraw.Draw(image)
    unit = big * MARK / 16
    origin = (big - big * MARK) / 2
    for x, y, w, h in BLOCKS:
        left, top = origin + (x - 4) * unit, origin + (y - 4) * unit
        pen.rectangle((round(left), round(top), round(left + w * unit) - 1, round(top + h * unit) - 1), fill=INK)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main(out: Path = OUT) -> list[Path]:
    """Write every icon into out and return their paths."""
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, size in SIZES.items():
        path = out / name
        draw(size).save(path, "PNG", optimize=True)
        written.append(path)
    svg = out / "icon.svg"
    svg.write_text(SVG, encoding="utf-8")
    written.append(svg)
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    for p in main(target):
        print(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p)
