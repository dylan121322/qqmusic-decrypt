# -*- mode: python ; coding: utf-8 -*-
# 内置 ffmpeg/ffprobe：优先环境变量，其次 PATH，最后回退 Homebrew 默认路径
import os
import shutil

def _tool(name):
    env = os.environ.get("QQMUSIC_" + name.upper())
    if env and os.path.exists(env):
        return env
    found = shutil.which(name)
    if found:
        return found
    fallback = "/opt/homebrew/bin/" + name
    if os.path.exists(fallback):
        return fallback
    raise SystemExit("找不到 %s，无法打包" % name)

FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")
print("bundling ffmpeg:", FFMPEG)
print("bundling ffprobe:", FFPROBE)

a = Analysis(
    ['qqmusic_decrypt.py'],
    pathex=[],
    binaries=[(FFMPEG, 'bin'), (FFPROBE, 'bin')],
    datas=[],
    hiddenimports=[],
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
    name='qqmusic-decrypt-mac',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
