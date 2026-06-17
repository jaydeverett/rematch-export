#!/usr/bin/env python3
"""Lay out the drag-to-install DMG window WITHOUT mounting anything.

build.sh images a *staging folder* with `hdiutil makehybrid` (this avoids the
/Volumes temp-mount that macOS TCC blocks for an automated build host). So the
window styling has to live as files INSIDE that folder before it's imaged:

  staging/
    RematchExport.app
    Applications -> /Applications
    .background/background.png   <- the arrow art (this script draws it)
    .DS_Store                    <- icon positions + window + background ref

Finder reads .DS_Store from the DMG root and renders the classic installer
window: the app on the left, an arrow, and the Applications folder on the right.

Build deps (pip): Pillow, ds-store, mac-alias.

Usage:  python dmg_layout.py <staging_dir> <volume_name>
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont
from ds_store import DSStore
from mac_alias import Alias

# Window content size in points (the .background image is drawn at this pixel
# size; Finder maps it 1:1 to the icon-view area).
WIN_W, WIN_H = 620, 430
ICON_SIZE = 112
APP_POS = (158, 210)          # icon centers
APPS_POS = (462, 210)

# Brand palette (matches the app UI: ivory bg, ink text, tennis-green accent).
IVORY = (247, 243, 236)
INK = (43, 43, 43)
INK_SOFT = (120, 116, 108)
GREEN = (74, 124, 89)


def draw_background(path: str) -> None:
    """Draw the ivory window background: title, an arrow app->Applications."""
    img = Image.new("RGB", (WIN_W, WIN_H), IVORY)
    d = ImageDraw.Draw(img)

    def font(size, bold=False):
        # Fall back gracefully across the fonts present on a stock macOS.
        for name in (
            "/System/Library/Fonts/SFNS.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial.ttf",
        ):
            if os.path.exists(name):
                try:
                    return ImageFont.truetype(name, size)
                except Exception:
                    pass
        return ImageFont.load_default()

    def centered(text, y, fnt, fill):
        w = d.textbbox((0, 0), text, font=fnt)[2]
        d.text(((WIN_W - w) / 2, y), text, font=fnt, fill=fill)

    centered("Install RematchExport", 34, font(26), INK)
    centered("Drag the app onto the Applications folder", 70, font(14), INK_SOFT)

    # Arrow from just right of the app icon to just left of Applications.
    y = APP_POS[1]
    x0 = APP_POS[0] + ICON_SIZE // 2 + 18
    x1 = APPS_POS[0] - ICON_SIZE // 2 - 18
    d.line([(x0, y), (x1 - 4, y)], fill=GREEN, width=4)
    d.polygon([(x1, y), (x1 - 16, y - 9), (x1 - 16, y + 9)], fill=GREEN)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)


def write_ds_store(staging: str, volume_name: str, bg_rel: str) -> None:
    ds_path = os.path.join(staging, ".DS_Store")
    bg_abs = os.path.join(staging, bg_rel)

    # The background is referenced by a Finder alias. We can't point it at the
    # final /Volumes/<vol> path (nothing is mounted), so build it from the
    # staging file. mac_alias's CNID path serialization breaks on APFS staging
    # (negative/huge CNIDs), so null it out — Finder resolves the background by
    # its relative component (/.background/background.png) on the read-only DMG
    # anyway. If the alias still can't be built, fall back to a solid ivory
    # background; either way the icons stay positioned (a clean install window).
    icvp = {
        "viewOptionsVersion": 1,
        "iconSize": float(ICON_SIZE),
        "gridSpacing": 100.0,
        "gridOffsetX": 0.0,
        "gridOffsetY": 0.0,
        "labelOnBottom": True,
        "showIconPreview": True,
        "showItemInfo": False,
        "textSize": 12.0,
        "arrangeBy": "none",
    }
    try:
        a = Alias.for_file(bg_abs)
        a.target.cnid_path = None
        icvp["backgroundType"] = 2  # picture
        icvp["backgroundImageAlias"] = a.to_bytes()
    except Exception as exc:
        print("  (background image alias unavailable, using solid color):", exc)
        icvp["backgroundType"] = 1  # solid color
        icvp["backgroundColorRed"] = IVORY[0] / 255.0
        icvp["backgroundColorGreen"] = IVORY[1] / 255.0
        icvp["backgroundColorBlue"] = IVORY[2] / 255.0

    with DSStore.open(ds_path, "w+") as ds:
        ds["."]["vSrn"] = ("long", 1)
        ds["."]["ICVO"] = ("bool", True)
        ds["."]["bwsp"] = {
            "WindowBounds": "{{120, 120}, {%d, %d}}" % (WIN_W, WIN_H),
            "ShowToolbar": False,
            "ShowStatusBar": False,
            "ShowPathbar": False,
            "ShowSidebar": False,
        }
        ds["."]["icvp"] = icvp
        ds["RematchExport.app"]["Iloc"] = APP_POS
        ds["Applications"]["Iloc"] = APPS_POS


def main() -> None:
    staging, volume_name = sys.argv[1], sys.argv[2]
    bg_rel = os.path.join(".background", "background.png")
    draw_background(os.path.join(staging, bg_rel))
    write_ds_store(staging, volume_name, bg_rel)
    print("DMG layout written:", staging)


if __name__ == "__main__":
    main()
