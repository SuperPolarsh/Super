# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = []
binaries = []
# app.py imports the pipeline lazily (lazy_imports.py) so the window opens in
# ~1 s instead of ~3.5 s. PyInstaller cannot follow those string-based imports,
# so name them explicitly or the packaged build crashes on first Generate.
hiddenimports = ['main', 'source_downloader', 'editor', 'lazy_imports']
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('moviepy')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('whisper')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# OpenCV's bundled face-detection cascades are not picked up automatically.
# Without them 9:16 subject tracking fails inside the packaged exe.
datas += collect_data_files('cv2', includes=['data/*.xml'])

# Brand assets loaded at runtime through runtime_utils.resource_path().
datas += [
    ('assets/logo_lockup.png', 'assets'),
    ('assets/logo_mark.png', 'assets'),
    ('assets/super.ico', 'assets'),
]


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# One folder instead of one file: the launcher starts this immediately, while
# a single-file build would unpack ~350 MB on every launch (~8 s of the 12 s
# start-up time).
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SuperApp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/super.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SuperApp',
)
