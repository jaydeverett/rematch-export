#!/usr/bin/env bash
#
# Build, sign, and package RematchExport into a notarizable DMG.
#
# Prereqs (one-time, on a machine with network):
#   python3.13 -m venv .venv && source .venv/bin/activate
#   pip install pyinstaller flask
#
# Then:   ./build.sh
#
# This produces dist/RematchExport.dmg (signed). Notarize + staple + upload are
# printed at the end — run those yourself with your Apple notarization credentials.

set -euo pipefail
cd "$(dirname "$0")"

IDENTITY="Developer ID Application: Jay Deverett (T7Y63TMUY9)"
APP="dist/RematchExport.app"
DMG="dist/RematchExport.dmg"

command -v pyinstaller >/dev/null || { echo "pyinstaller not found — activate the venv (see header)"; exit 1; }

echo "==> Clean"
rm -rf build dist

echo "==> PyInstaller (onedir .app + custom icon)"
pyinstaller RematchExport.spec --noconfirm

echo "==> Codesign (hardened runtime + entitlements)"
# --deep is deprecated but reliable for self-contained PyInstaller bundles; the prior
# notarized release was signed the same way. --timestamp is required for notarization.
codesign --force --options runtime --timestamp \
  --entitlements entitlements.plist \
  --sign "$IDENTITY" --deep "$APP"

echo "==> Verify signature"
codesign --verify --strict --verbose=2 "$APP"

echo "==> Build DMG"
rm -f "$DMG"
hdiutil create -volname "RematchExport" -srcfolder "$APP" -ov -format UDZO "$DMG"
codesign --force --timestamp --sign "$IDENTITY" "$DMG"

echo
echo "================================================================"
echo "Built + signed:  $DMG"
echo
echo "NOW NOTARIZE + STAPLE + UPLOAD (run these yourself):"
echo
echo "  # Option A (recommended) — reuse the ASC API key you already have for eas submit:"
echo "  xcrun notarytool submit \"$DMG\" \\"
echo "      --key ../rematch-mobile/AuthKey_2V28Q72UV3.p8 \\"
echo "      --key-id 2V28Q72UV3 \\"
echo "      --issuer ad0f8e3b-fef1-4497-8341-39ed0d504c60 --wait"
echo
echo "  # Option B — Apple ID + app-specific password (appleid.apple.com):"
echo "  # xcrun notarytool submit \"$DMG\" --apple-id <id> --team-id T7Y63TMUY9 --password <app-pw> --wait"
echo
echo "  xcrun stapler staple \"$DMG\""
echo "  xcrun stapler validate \"$DMG\""
echo "  spctl -a -t open --context context:primary-signature -v \"$DMG\""
echo
echo "  # Publish the new release (new tag -> create; --latest repoints the public download URL):"
echo "  gh release create v1.5.1 \"$DMG\" --repo jaydeverett/rematch-export --title v1.5.1 --notes \"Contact names restored, light mode, tab selector contrast, FDA waiting page auto-reloads\" --latest"
echo "================================================================"
