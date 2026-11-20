# -*- mode: python ; coding: utf-8 -*-
# Builds Super.exe - the small launcher that checks for updates and starts the
# app. It must stay light: everything heavy lives in the SuperApp folder.
#
#   python -m PyInstaller SuperLauncher.spec --noconfirm
from PyInstaller.utils.hooks import collect_all

datas = [
    ('assets/logo_mark.png', 'assets'),
    ('assets/super.ico', 'assets'),
]
binaries = []
hiddenimports = []

tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'whisper', 'cv2', 'moviepy', 'numpy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Super',
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
