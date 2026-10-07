#!/usr/bin/env python3
"""Genera el icono de la app (Pokebola + monograma DKB).

Salida: tools/icon.ico (multi-tamano) y tools/icon.png (256).

Uso: python tools/make_icon.py
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
ICO = os.path.join(OUT_DIR, "icon.ico")
PNG = os.path.join(OUT_DIR, "icon.png")

S = 1024  # lienzo grande; se reescala con antialias
RED = (225, 21, 25, 255)
RED_DARK = (150, 12, 18, 255)
WHITE = (245, 246, 248, 255)
INK = (24, 24, 28, 255)
GREY = (170, 174, 180, 255)

FONTS = [
    r"C:\Windows\Fonts\seguibl.ttf", r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\impact.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONTS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def build() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = int(S * 0.05)
    box = (m, m, S - m, S - m)
    mid = S // 2

    # sombra suave
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse((m + 14, m + 20, S - m + 14, S - m + 20), fill=(0, 0, 0, 70))
    from PIL import ImageFilter
    img = Image.alpha_composite(sh.filter(ImageFilter.GaussianBlur(18)), img)
    d = ImageDraw.Draw(img)

    # cuerpo: mitad superior roja, inferior blanca
    d.pieslice(box, 180, 360, fill=RED)
    d.pieslice(box, 0, 180, fill=WHITE)
    # degradado sutil arriba
    d.pieslice((m, m, S - m, mid), 180, 360, fill=None, outline=None)

    # banda central negra
    band = int(S * 0.085)
    d.rectangle((m, mid - band // 2, S - m, mid + band // 2), fill=INK)
    # contorno del cuerpo
    d.ellipse(box, outline=INK, width=int(S * 0.03))
    d.line((m, mid, S - m, mid), fill=INK, width=int(S * 0.012))

    # boton central
    r_out = int(S * 0.135)
    r_in = int(S * 0.085)
    d.ellipse((mid - r_out, mid - r_out, mid + r_out, mid + r_out), fill=INK)
    d.ellipse((mid - r_in, mid - r_in, mid + r_in, mid + r_in), fill=WHITE)
    d.ellipse((mid - r_in, mid - r_in, mid + r_in, mid + r_in), outline=GREY, width=4)

    # monograma DKB en la mitad blanca
    font = _font(int(S * 0.30))
    text = "DKB"
    tb = d.textbbox((0, 0), text, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    tx = mid - tw // 2 - tb[0]
    ty = int(mid + S * 0.14)
    # sombra
    d.text((tx + 6, ty + 6), text, font=font, fill=(0, 0, 0, 60))
    d.text((tx, ty), text, font=font, fill=RED_DARK)
    return img


def main() -> int:
    img = build()
    sizes = [16, 24, 32, 48, 64, 128, 256]
    img.save(ICO, format="ICO", sizes=[(s, s) for s in sizes])
    img.resize((256, 256), Image.LANCZOS).save(PNG)
    print(f"escrito {ICO} y {PNG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
