# -*- mode: python ; coding: utf-8 -*-
# Builds SuperSetup.exe — the small online bootstrapper users download.
#
#   python -m PyInstaller SuperBootstrapper.spec --noconfirm
#
# The bootstrapper embeds UPDATE_MANIFEST_URL from version.py, downloads the
# Inno Setup payload from that manifest, then installs it silently. Keep it
# small: it must not pull in the video pipeline.
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
    ['bootstrapper.py'],
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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SuperSetup',
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
