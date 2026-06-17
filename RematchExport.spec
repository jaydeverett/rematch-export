# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for RematchExport (the iMessage desktop exporter).
#
# Build with:  ./build.sh   (clean -> PyInstaller -> codesign -> DMG; notarize/staple
# commands are printed at the end for you to run with your Apple credentials).
#
# Why --onedir (a real .app bundle) and NOT --onefile: --onefile re-unpacks the whole
# Python runtime to a temp dir on EVERY launch, which is the slow-cold-start behaviour.
# --onedir lays the runtime down once inside Contents/Frameworks, so repeat launches are
# fast. The custom icon (the iPhone app icon) comes from RematchExport.icns below.

a = Analysis(
    ['messages_desktop_app.py'],
    pathex=[],
    binaries=[],
    datas=[('templates', 'templates')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RematchExport',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                # --windowed: no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,       # signed explicitly (post-build) in build.sh
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='RematchExport',
)

app = BUNDLE(
    coll,
    name='RematchExport.app',
    icon='RematchExport.icns',
    bundle_identifier='RematchExport',
    info_plist={
        'CFBundleShortVersionString': '1.5.2',
        'CFBundleVersion': '1.5.2',
        'NSHighResolutionCapable': True,
        'LSApplicationCategoryType': 'public.app-category.utilities',
    },
)
