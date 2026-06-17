#!/usr/bin/env python3
"""Regenerate RematchExport.icns as a proper macOS (Big Sur+) app icon.

The source art is a full-bleed iOS-style square. macOS icons sit on the standard
grid: a rounded "squircle" body inset within the 1024 canvas with a soft drop
shadow, so the app looks native next to others in the Dock / Finder / the Full
Disk Access list. This rounds + insets the art and rebuilds the .icns, and also
emits a small rounded PNG for the browser-tab favicon.

Deps: Pillow.  Usage: python make_icon.py
"""
import os
import subprocess

from PIL import Image, ImageDraw, ImageFilter

SRC_ICNS = "RematchExport_source.icns"   # the original full-bleed square art (tracked)
ICNS = "RematchExport.icns"              # the rounded macOS icon the build uses
CANVAS = 1024
BODY = 824                       # squircle body (macOS Big Sur grid ≈ 80%)
RADIUS = 185                     # continuous-corner radius for the body
MARGIN = (CANVAS - BODY) // 2    # 100px inset, leaves room for the shadow
FAVICON_OUT = "/tmp/rmx_favicon.png"
PREVIEW_OUT = "/tmp/rmx_icon_preview.png"


def source_1024() -> Image.Image:
    subprocess.run(["iconutil", "-c", "iconset", SRC_ICNS, "-o", "/tmp/src.iconset"], check=True)
    return Image.open("/tmp/src.iconset/icon_512x512@2x.png").convert("RGBA")


def rounded_icon(src: Image.Image) -> Image.Image:
    body = src.resize((BODY, BODY), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (BODY, BODY), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, BODY - 1, BODY - 1], radius=RADIUS, fill=255)
    body.putalpha(mask)

    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))

    # Soft drop shadow under the body.
    shadow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [MARGIN, MARGIN + 12, MARGIN + BODY, MARGIN + BODY + 12], radius=RADIUS, fill=(0, 0, 0, 70)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    canvas = Image.alpha_composite(canvas, shadow)

    canvas.paste(body, (MARGIN, MARGIN), body)
    return canvas


def build_icns(icon: Image.Image) -> None:
    iconset = "/tmp/rmx_new.iconset"
    os.makedirs(iconset, exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = size * scale
            name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            icon.resize((px, px), Image.LANCZOS).save(os.path.join(iconset, name))
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", ICNS], check=True)


def main() -> None:
    icon = rounded_icon(source_1024())
    icon.save(PREVIEW_OUT)
    icon.resize((64, 64), Image.LANCZOS).save(FAVICON_OUT)
    build_icns(icon)
    print("rounded .icns rebuilt; preview:", PREVIEW_OUT, "favicon:", FAVICON_OUT)


if __name__ == "__main__":
    main()
