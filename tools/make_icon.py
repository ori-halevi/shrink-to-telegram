# -*- coding: utf-8 -*-
"""Draws the application icon.

Run once with Pillow installed (``pip install pillow``) to regenerate
``assets/icon.ico``.  The application itself only loads the finished file, so it
keeps its "no dependencies" promise.

The mark: a video play triangle held between two walls closing in on it - the
whole program in one glance, without borrowing anyone's branding.
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024                      # drawn big, scaled down for every icon size
TOP = (56, 166, 245)             # light blue, top of the gradient
BOTTOM = (8, 92, 190)            # deep blue, bottom
WHITE = (255, 255, 255)
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1),
                                           radius=radius, fill=255)
    return mask


def gradient(size: int, top, bottom) -> Image.Image:
    ramp = Image.new("RGB", (1, size))
    pixels = ramp.load()
    for y in range(size):
        t = y / float(size - 1)
        pixels[0, y] = tuple(int(round(top[i] + (bottom[i] - top[i]) * t))
                             for i in range(3))
    return ramp.resize((size, size), Image.BILINEAR)


def draw_icon() -> Image.Image:
    """Two play triangles pressed towards each other: video, being squeezed."""
    size = SIZE
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.paste(gradient(size, TOP, BOTTOM), (0, 0),
               rounded_mask(size, int(size * 0.22)))

    art = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(art)
    middle = size / 2.0
    half_h = size * 0.205                  # triangle height
    width = size * 0.225                   # triangle width
    gap = size * 0.050                     # the space they are closing
    draw.polygon([(middle - gap - width, middle - half_h),
                  (middle - gap - width, middle + half_h),
                  (middle - gap, middle)], fill=WHITE + (255,))
    draw.polygon([(middle + gap + width, middle - half_h),
                  (middle + gap + width, middle + half_h),
                  (middle + gap, middle)], fill=WHITE + (255,))
    icon.alpha_composite(art)
    return icon


def main() -> int:
    icon = draw_icon()
    os.makedirs(os.path.join(HERE, "assets"), exist_ok=True)
    png = os.path.join(HERE, "assets", "icon.png")
    ico = os.path.join(HERE, "assets", "icon.ico")
    icon.resize((256, 256), Image.LANCZOS).save(png)
    frames = []
    for edge in ICO_SIZES:
        frame = icon.resize((edge, edge), Image.LANCZOS)
        if edge <= 32:                       # tiny sizes need the edges sharpened
            frame = frame.filter(ImageFilter.UnsharpMask(radius=1, percent=70))
        frames.append(frame)
    frames[-1].save(ico, format="ICO",
                    sizes=[(edge, edge) for edge in ICO_SIZES])
    print("wrote %s and %s" % (png, ico))

    preview = Image.new("RGBA", (700, 300), (32, 32, 36, 255))
    x = 20
    for edge in (256, 128, 64, 48, 32, 24, 16):
        preview.alpha_composite(icon.resize((edge, edge), Image.LANCZOS), (x, 20))
        x += edge + 16
    preview.save(os.path.join(HERE, "assets", "icon-preview.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
