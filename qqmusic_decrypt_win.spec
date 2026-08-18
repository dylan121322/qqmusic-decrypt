# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec：Windows 单文件打包，内置 FFMPEG_BIN 目录下的 ffmpeg/ffprobe 及依赖 DLL
import glob
import os

ffdir = os.environ.get("FFMPEG_BIN", "")
binaries = []
if ffdir:
    for name in sorted(os.listdir(ffdir)):
        p = os.path.join(ffdir, name)
        if os.path.isfile(p):
            low = name.lower()
            if low in ("ffmpeg.exe", "ffprobe.exe") or low.endswith(".dll"):
                binaries.append((p, "bin"))

a = Analysis(
    ["qqmusic_decrypt.py"],
    pathex=["."],
    binaries=binaries,
    datas=[],
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
    a.binaries,
    a.datas,
    [],
    name="qqmusic-decrypt-win",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
