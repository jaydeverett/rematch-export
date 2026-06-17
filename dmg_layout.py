#!/usr/bin/env python3
"""Lay out the drag-to-install DMG window WITHOUT mounting anything.

build.sh images a *staging folder* with `hdiutil makehybrid` (this avoids the
/Volumes temp-mount that macOS TCC blocks for an automated build host, and that
also defeats create-dmg/appdmg/dmgbuild/Finder here). So the window styling has
to live as a file INSIDE that folder before it's imaged:

  staging/
    RematchExport.app
    Applications -> /Applications
    .DS_Store      <- window size + icon positions + background color

Finder reads .DS_Store from the DMG root and renders the classic installer
layout: the app on the left, the Applications folder on the right, inviting the
drag. We use a SOLID background color, not a picture: a background *image* is
referenced by a Finder alias that can't be built reliably without mounting the
final volume (mac_alias chokes on APFS CNIDs), and a broken alias paints the
window white. A solid color always renders, so the result is predictable on
every machine. The two positioned icons are the recognized "drag this onto that"
affordance on their own.

Build dep (pip): ds-store.

Usage:  python dmg_layout.py <staging_dir> <volume_name>
"""
import os
import sys

from ds_store import DSStore

WIN_W, WIN_H = 620, 430
ICON_SIZE = 112
APP_POS = (158, 210)             # icon centers
APPS_POS = (462, 210)
IVORY = (247, 243, 236)          # matches the app UI background


def write_ds_store(staging: str) -> None:
    with DSStore.open(os.path.join(staging, ".DS_Store"), "w+") as ds:
        ds["."]["vSrn"] = ("long", 1)
        ds["."]["ICVO"] = ("bool", True)
        ds["."]["bwsp"] = {
            "WindowBounds": "{{120, 120}, {%d, %d}}" % (WIN_W, WIN_H),
            "ShowToolbar": False,
            "ShowStatusBar": False,
            "ShowPathbar": False,
            "ShowSidebar": False,
        }
        ds["."]["icvp"] = {
            "viewOptionsVersion": 1,
            "backgroundType": 1,            # 1 = solid color (always renders)
            "backgroundColorRed": IVORY[0] / 255.0,
            "backgroundColorGreen": IVORY[1] / 255.0,
            "backgroundColorBlue": IVORY[2] / 255.0,
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
        ds["RematchExport.app"]["Iloc"] = APP_POS
        ds["Applications"]["Iloc"] = APPS_POS


def main() -> None:
    staging = sys.argv[1]
    write_ds_store(staging)
    print("DMG layout written:", staging)


if __name__ == "__main__":
    main()
