#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
qqmusic_decrypt.py — QQ 音乐 QMC 加密音频批量解密工具（macOS / 跨平台）

支持格式：
  - musicex   (.mgg/.mflac, macOS 客户端 >= 19.57) : footer 内只有元数据，ekey 需调 GetEVkey API
  - QMC2 QTag (.mggl/.mflac/.mgg, 2021)            : ekey 内嵌文件尾部，离线可解
  - QMC2 V1   (尾 4 字节 LE = key 长度)             : ekey 内嵌，离线可解
  - QMC1 legacy (.qmc0/.qmc2/.qmc3/.qmcflac/...)   : 内嵌 key 或 Static 静态盒，离线可解
  - iMusic 缓存 (<song_id>-<type>.mgg/.mflac)       : 无 footer，用 sqlite 元数据 + GetEVkey API

自测：python3 qqmusic_decrypt.py --self-test
仅查看：python3 qqmusic_decrypt.py --info <文件...>
预演：python3 qqmusic_decrypt.py --dry-run ~/Downloads
批量：python3 qqmusic_decrypt.py --out-dir ~/Music/QQMusicDecrypted ~/Downloads ~/Library/Containers/.../iMusic

仅限解密你自己合法下载、用于个人备份的文件。
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import math
import os
import plistlib
import re
import shutil
import ssl
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import sqlite3
except ImportError:  # Windows embeddable Python 可能不带 sqlite3
    sqlite3 = None

IS_WINDOWS = (sys.platform == "win32") or (os.name == "nt")
if IS_WINDOWS:
    # Windows 控制台默认 GBK，统一输出 UTF-8 并容错替换，避免 emoji/中文崩溃
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    import ctypes
    from ctypes import wintypes  # noqa: F401  (ctypes 不会自动暴露 wintypes 子模块)
    import configparser
    try:
        import winreg
    except ImportError:
        winreg = None

# ----------------------------------------------------------------------------
# 平台常量（自动检测；--platform 可覆盖）
# ----------------------------------------------------------------------------

DEFAULT_API_PLATFORM = "27" if IS_WINDOWS else "20"  # Win 客户端=27, Mac 客户端=20
API_PLATFORM = DEFAULT_API_PLATFORM

# Win 缓存文件名前缀（与 Mac 资源前缀一致，实测）
WIN_CACHE_PREFIXES = ("F0M0", "O8M0", "O6M0", "O4M0", "M8M0")

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------

M32 = 0xFFFFFFFF
M64 = 0xFFFFFFFFFFFFFFFF
TEA_DELTA = 0x9E3779B9
TEA_ROUNDS = 16

FIRST_SEGMENT_SIZE = 0x80
SEGMENT_SIZE = 0x1400

ENCV2_PREFIX = b"QQMusic EncV2,Key:"
ENCV2_STAGE1_KEY = b"386ZJY!@#*$%^&)("
ENCV2_STAGE2_KEY = b"**#!(#$%&^a1cZ,T"

# QMC1 静态 S 盒（unlock-music QmcStaticCipher，256 字节）
STATIC_BOX = bytes.fromhex(
    "77483273DEF2C0C895EC30B251C3E1A0"
    "9EE69DCFFA7F14D1CEB8DCC34A6793D6"
    "28C29170CA8DA2A4F00861907E6FA2E0"
    "EBAE3EB667C792F491B5F66C5E8440F7"
    "F31B027FD5AB418928F425CC5211AD43"
    "68A6418B84B5FF2C924A26D8476A7C95"
    "61CCE6CBBB3F47588975C375A1D9AFCC"
    "087317DCAA9AA21641D8A206C68BFC66"
    "349FCF1823A00A74E72B277092E9AF37"
    "E68CA7BC62659CC208C988B3F343AC74"
    "2C0FD4AFA1C30164954E489FF4357895"
    "7A39D66AA06D40E84FA8EF111DF31B3F"
    "3F07DD6F5B193019FBEF0E37F00ECD16"
    "49FE5347131ABDA4F14019600EED6809"
    "065F4DCF3D1AFE2077E4D9DAF9A42B76"
    "1C71DB00BCFD0C6CA547F7F600794A11"
)

SUPPORTED_EXTS = {
    ".mgg", ".mflac", ".mggl", ".mgg0", ".mgg1", ".mflac0", ".mflac1",
    ".qmc0", ".qmc2", ".qmc3", ".qmcflac", ".qmcogg",
    ".bkcmp3", ".bkcflac", ".tkm",
}
LEGACY_EXTS = {
    ".qmc0", ".qmc2", ".qmc3", ".qmcflac", ".qmcogg",
    ".bkcmp3", ".bkcflac", ".tkm",
}
MGG_MFLAC_EXTS = {".mgg", ".mflac", ".mggl", ".mgg0", ".mgg1", ".mflac0", ".mflac1"}

API_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
API_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36") if IS_WINDOWS \
    else "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

# 音质 -> (CDN 前缀, 扩展名)。加密资源前缀规则来自客户端 CSongURL 反编译 + API 实测。
QUALITY_ENCRYPTED = {
    "flac": ("F0M0", "mflac"),
    "320": ("O8M0", "mgg"),
    "192": ("O6M0", "mgg"),
    "128": ("O4M0", "mgg"),
}
QUALITY_PLAIN = {
    "m4a": ("C400", "m4a"),
    "mp3-128": ("M500", "mp3"),
    "mp3-320": ("M800", "mp3"),
}
DEFAULT_QUALITY = "flac,320,192,128,m4a"

if IS_WINDOWS:
    DEFAULT_DB = ""
    DEFAULT_PREFS = ""
    CONTAINER_PREFS = ""
    DEFAULT_OUT_DIR = os.path.expanduser("~/Music/QQMusicDecrypted")
    WIN_CONFIG_INI = os.path.join(os.environ.get("APPDATA", ""),
                                  "Tencent", "QQMusic", "QQMusicServiceConfig.ini")
else:
    DEFAULT_DB = os.path.expanduser(
        "~/Library/Containers/com.tencent.QQMusicMac/Data/Library/Application "
        "Support/QQMusicMac/qqmusic.sqlite"
    )
    DEFAULT_PREFS = os.path.expanduser(
        "~/Library/Preferences/com.tencent.QQMusicMac.plist"
    )
    CONTAINER_PREFS = os.path.expanduser(
        "~/Library/Containers/com.tencent.QQMusicMac/Data/Library/Preferences/"
        "com.tencent.QQMusicMac.plist"
    )
    DEFAULT_OUT_DIR = os.path.expanduser("~/Music/QQMusicDecrypted")


def win_cache_root() -> Optional[str]:
    """Windows 客户端缓存根目录（注册表 CACHEPATH，回退默认盘符扫描）。"""
    if not IS_WINDOWS:
        return None
    if winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Tencent\QQMusic") as key:
                value, _ = winreg.QueryValueEx(key, "CACHEPATH")
                if value:
                    return str(value).rstrip("\\/")
        except OSError:
            pass
    for drive in ("D:", "E:", "C:"):
        cand = os.path.join(drive + "\\", "QQMusicCache")
        if os.path.isdir(cand):
            return cand
    return None


def default_library_dir() -> str:
    """默认本地加密音频库路径（Mac=iMusic 库；Win=duty 缓存目录）。"""
    if IS_WINDOWS:
        root = win_cache_root()
        if root:
            duty = os.path.join(root, "downloadproxyNew", "tp2p", ".tpfs", "duty")
            if os.path.isdir(duty):
                return duty
            return root
        return ""
    return os.path.expanduser(
        "~/Library/Containers/com.tencent.QQMusicMac/Data/Library/Application "
        "Support/QQMusicMac/iMusic"
    )


class QmcError(Exception):
    pass


class TeaError(QmcError):
    pass


class ApiError(QmcError):
    pass


# ----------------------------------------------------------------------------
# 腾讯魔改 TEA（16 轮，tweaked CBC；移植自 jixunmoe/tc_tea_rust）
# ----------------------------------------------------------------------------

def _tea_ecb_encrypt(block: int, k: Sequence[int]) -> int:
    y = (block >> 32) & M32
    z = block & M32
    s = 0
    for _ in range(TEA_ROUNDS):
        s = (s + TEA_DELTA) & M32
        y = (y + ((((z << 4) + k[0]) & M32) ^ ((z + s) & M32) ^ ((z >> 5) + k[1]))) & M32
        z = (z + ((((y << 4) + k[2]) & M32) ^ ((y + s) & M32) ^ ((y >> 5) + k[3]))) & M32
    return ((y << 32) | z) & M64


def _tea_ecb_decrypt(block: int, k: Sequence[int]) -> int:
    y = (block >> 32) & M32
    z = block & M32
    s = (TEA_DELTA * TEA_ROUNDS) & M32
    for _ in range(TEA_ROUNDS):
        z = (z - ((((y << 4) + k[2]) & M32) ^ ((y + s) & M32) ^ ((y >> 5) + k[3]))) & M32
        y = (y - ((((z << 4) + k[0]) & M32) ^ ((z + s) & M32) ^ ((z >> 5) + k[1]))) & M32
        s = (s - TEA_DELTA) & M32
    return ((y << 32) | z) & M64


def _tea_key_ints(key16: bytes) -> List[int]:
    if len(key16) != 16:
        raise TeaError("TEA 密钥必须为 16 字节")
    return [struct.unpack(">I", key16[i:i + 4])[0] for i in range(0, 16, 4)]


def tc_tea_decrypt(cipher: bytes, key16: bytes) -> bytes:
    """腾讯 TEA CBC 解密。密文帧: PadLen(1)+Padding+Salt(2)+Body+Zero(7)。"""
    if len(cipher) < 16 or len(cipher) % 8 != 0:
        raise TeaError(f"非法 TEA 密文长度: {len(cipher)}")
    k = _tea_key_ints(key16)
    plain = bytearray()
    iv1 = 0
    iv2 = 0
    for i in range(0, len(cipher), 8):
        c = struct.unpack(">Q", cipher[i:i + 8])[0]
        d = _tea_ecb_decrypt(c ^ iv2, k)
        plain += struct.pack(">Q", d ^ iv1)
        iv1, iv2 = c, d
    pad = plain[0] & 0x7
    start = 1 + pad + 2
    end = len(plain) - 7
    if any(plain[end:]):
        raise TeaError("TEA zero-check 失败（密钥错误或数据损坏）")
    return bytes(plain[start:end])


DEFAULT_TEA_SALT = bytes.fromhex("A56E35BC7C310455A0BF")


def tc_tea_encrypt(plain: bytes, key16: bytes, salt: bytes = DEFAULT_TEA_SALT) -> bytes:
    """腾讯 TEA CBC 加密（用于自测生成样本）。"""
    k = _tea_key_ints(key16)
    fixed = 1 + 2 + 7  # PadLen + Salt + Zero
    pad = (8 - ((fixed + len(plain)) & 0x7)) & 0x7
    header_len = 1 + pad + 2
    header = bytearray(16)
    header[:header_len] = salt[:header_len]
    header[0] = (header[0] & ~0x7) | pad

    n_copy = min(16 - header_len, len(plain))
    header[header_len:header_len + n_copy] = plain[:n_copy]
    rest = plain[n_copy:]

    out = bytearray()
    iv1 = 0
    iv2 = 0

    def enc_round(block: bytes) -> None:
        nonlocal iv1, iv2, out
        p = struct.unpack(">Q", block)[0]
        x = p ^ iv1
        c = _tea_ecb_encrypt(x, k) ^ iv2
        out += struct.pack(">Q", c)
        iv1, iv2 = c, x

    enc_round(bytes(header[:8]))
    enc_round(bytes(header[8:16]))
    last_len = len(rest) % 8
    whole, last = rest[:len(rest) - last_len], rest[len(rest) - last_len:]
    for i in range(0, len(whole), 8):
        enc_round(whole[i:i + 8])
    if last_len:
        enc_round(last + b"\x00" * (8 - last_len))
    return bytes(out)


def simple_make_key(seed: int, n: int) -> bytes:
    return bytes((int(abs(math.tan(seed + i * 0.1)) * 100.0) & 0xFF) for i in range(n))


def derive_tea_key(ekey_header: bytes) -> bytes:
    smk = simple_make_key(106, 8)
    out = bytearray(16)
    for i in range(16):
        out[i] = smk[i // 2] if i % 2 == 0 else ekey_header[i // 2]
    return bytes(out)


def parse_ekey(ekey: str) -> bytes:
    """ekey -> QMC2 流密钥。支持 EncV2 双包装 / EncV1 / API 原生密钥。"""
    s = ekey.strip("\x00").strip()
    if not s:
        raise QmcError("ekey 为空")
    try:
        raw = base64.b64decode(s)
    except Exception as e:  # noqa: BLE001
        raise QmcError(f"ekey base64 解码失败: {e}") from e

    if raw.startswith(ENCV2_PREFIX):
        blob = raw[len(ENCV2_PREFIX):]
        try:
            stage1 = tc_tea_decrypt(blob, ENCV2_STAGE1_KEY)
            stage2 = tc_tea_decrypt(stage1, ENCV2_STAGE2_KEY)
            raw = base64.b64decode(stage2)
        except Exception as e:  # noqa: BLE001
            raise QmcError(f"EncV2 解包失败: {e}") from e

    if len(raw) < 8:
        raise QmcError(f"解码后密钥过短: {len(raw)} 字节")
    header, body = raw[:8], raw[8:]
    if not body:
        return header
    try:
        tail = tc_tea_decrypt(body, derive_tea_key(header))
    except TeaError:
        # API 可能直接返回原生密钥（未经 TEA 包装）
        return raw
    return header + tail


# ----------------------------------------------------------------------------
# QMC2 流密码（Map / RC4 变体）+ QMC1 Static
# ----------------------------------------------------------------------------

def _rot8(v: int, i: int) -> int:
    r = (i + 4) % 8
    return ((v << r) | (v >> r)) & 0xFF


class MapCipher:
    def __init__(self, key: bytes):
        self.key = key
        self.n = len(key)

    def decrypt(self, buf: bytearray, offset: int) -> None:
        key, n = self.key, self.n
        for i in range(len(buf)):
            o = offset + i
            if o > 0x7FFF:
                o %= 0x7FFF
            idx = (o * o + 71214) % n
            buf[i] ^= _rot8(key[idx], idx & 0x7)


class RC4Cipher:
    def __init__(self, key: bytes):
        self.key = key
        self.n = len(key)
        self.s = bytearray(i & 0xFF for i in range(self.n))
        j = 0
        for i in range(self.n):
            j = (self.s[i] + j + key[i % self.n]) % self.n
            self.s[i], self.s[j] = self.s[j], self.s[i]
        h = 1
        for v in key:
            if v == 0:
                continue
            nh = (h * v) & M32
            if nh == 0 or nh <= h:
                break
            h = nh
        self.hash = h

    def _segkey(self, seg_id: int, seed: int) -> int:
        div = (seg_id + 1) * seed
        if div == 0:
            val = float("inf")
        else:
            val = float(self.hash) / float(div) * 100.0
        if math.isinf(val):
            val = float(M64)  # 对应 Rust f64->u64 饱和转换
        return int(val)

    def decrypt(self, buf: bytearray, offset: int) -> None:
        n = self.n
        key = self.key
        i = 0
        total = len(buf)

        # 首段 0x80 字节：直接查表
        if offset < FIRST_SEGMENT_SIZE:
            cnt = min(total, FIRST_SEGMENT_SIZE - offset)
            for _ in range(cnt):
                k1 = key[offset % n]
                k2 = self._segkey(offset, k1) % n
                buf[i] ^= key[k2]
                offset += 1
                i += 1

        # 后续按 0x1400 分段：每段按 seg_id 丢弃若干字节再 PRGA
        while i < total:
            seg_id = offset // SEGMENT_SIZE
            seg_small = seg_id & 0x1FF
            skip = (self._segkey(seg_id, key[seg_small]) & 0x1FF) + (offset % SEGMENT_SIZE)
            s = bytearray(self.s)
            j = 0
            k = 0
            for _ in range(skip):
                j = (j + 1) % n
                k = (s[j] + k) % n
                s[j], s[k] = s[k], s[j]
            cnt = min(total - i, SEGMENT_SIZE - (offset % SEGMENT_SIZE))
            for _ in range(cnt):
                j = (j + 1) % n
                k = (s[j] + k) % n
                s[j], s[k] = s[k], s[j]
                buf[i] ^= s[(s[j] + s[k]) % n]
                i += 1
                offset += 1


class StaticCipher:
    def decrypt(self, buf: bytearray, offset: int) -> None:
        box = STATIC_BOX
        for i in range(len(buf)):
            o = offset + i
            if o > 0x7FFF:
                o %= 0x7FFF
            buf[i] ^= box[(o * o + 27) & 0xFF]


def make_qmc2_cipher(key: bytes):
    return RC4Cipher(key) if len(key) > 300 else MapCipher(key)


def qmc2_decrypt(data: bytes, key: bytes) -> bytearray:
    buf = bytearray(data)
    make_qmc2_cipher(key).decrypt(buf, 0)
    return buf


# ----------------------------------------------------------------------------
# 文件格式识别与解析
# ----------------------------------------------------------------------------

@dataclass
class FileInfo:
    kind: str            # musicex | qtag | v1 | static | cache
    audio_len: int
    ext: str
    key: Optional[bytes] = None
    ekey: Optional[str] = None
    song_id: Optional[int] = None
    media_mid: Optional[str] = None
    api_filename: Optional[str] = None
    details: Dict[str, object] = field(default_factory=dict)


def _utf16le(data: bytes, offset: int, max_len: int) -> str:
    out: List[str] = []
    end = min(offset + max_len, len(data))
    i = offset
    while i + 1 < end:
        c = struct.unpack("<H", data[i:i + 2])[0]
        if c == 0:
            break
        out.append(chr(c))
        i += 2
    return "".join(out)


def _parse_musicex(data: bytes) -> Optional[FileInfo]:
    if len(data) < 16 or data[-8:] != b"musicex\x00":
        return None
    footer_size = struct.unpack("<I", data[-16:-12])[0]
    version = struct.unpack("<I", data[-12:-8])[0]
    if version != 1 or footer_size < 16 or footer_size > len(data):
        raise QmcError(f"musicex footer 异常: size=0x{footer_size:x} version={version}")
    footer = data[len(data) - footer_size:]
    song_id = struct.unpack("<I", footer[0:4])[0]
    media_mid = _utf16le(footer, 0x0C, 60)
    filename = _utf16le(footer, 0x48, 68)
    if not media_mid or not filename:
        raise QmcError("musicex footer 缺少 media_mid/filename")
    return FileInfo(
        kind="musicex", audio_len=len(data) - footer_size, ext=".musicex",
        song_id=song_id, media_mid=media_mid, api_filename=filename,
        details={"footer_size": footer_size, "version": version},
    )


def _parse_qtag(data: bytes) -> Optional[FileInfo]:
    if data[-4:] != b"QTag":
        return None
    key_size = struct.unpack(">I", data[-8:-4])[0]
    audio_len = len(data) - key_size - 8
    if audio_len <= 0 or audio_len + key_size + 8 > len(data):
        raise QmcError("QTag 尾部长度字段非法")
    raw = data[audio_len:len(data) - 8]
    parts = raw.split(b",", 2)
    if len(parts) < 3:
        raise QmcError("QTag 元数据缺逗号分段")
    ekey = parts[0].decode("ascii", "replace")
    song_id_txt = parts[1].decode("ascii", "replace")
    try:
        song_id = int(song_id_txt) if song_id_txt else None
    except ValueError:
        song_id = None
    return FileInfo(
        kind="qtag", audio_len=audio_len, ext=".qtag", ekey=ekey,
        song_id=song_id,
        details={"key_size": key_size, "media_ver": parts[2].decode("ascii", "replace")},
    )


def _parse_v1(data: bytes) -> Optional[FileInfo]:
    key_size = struct.unpack("<I", data[-4:])[0]
    if not (0 < key_size <= 0x400):
        return None
    audio_len = len(data) - key_size - 4
    if audio_len <= 0:
        raise QmcError("V1 尾部 key 长度非法")
    ekey = data[audio_len:len(data) - 4].decode("utf-8", "replace")
    return FileInfo(kind="v1", audio_len=audio_len, ext=".v1", ekey=ekey,
                    details={"key_size": key_size})


def _parse_win_cache_name(path: str) -> Optional[Tuple[str, str]]:
    """Win duty 缓存文件名 <前缀+mid>.<ext> → (prefix, mid)。"""
    if Path(path).suffix.lower() not in (".mgg", ".mflac"):
        return None
    stem = Path(path).stem
    for prefix in WIN_CACHE_PREFIXES:
        if stem.startswith(prefix) and len(stem) > len(prefix):
            mid = stem[len(prefix):]
            if re.fullmatch(r"[0-9A-Za-z]{6,}", mid):
                return prefix, mid
    return None


def parse_file(data: bytes, path: str) -> FileInfo:
    ext = Path(path).suffix.lower()
    info = _parse_musicex(data)
    if info:
        info.ext = ext
        return info
    info = _parse_qtag(data)
    if info:
        info.ext = ext
        return info
    info = _parse_v1(data)
    if info:
        info.ext = ext
        return info
    if ext in MGG_MFLAC_EXTS:
        win = _parse_win_cache_name(path)
        if win:
            prefix, mid = win
            return FileInfo(kind="win_cache", audio_len=len(data), ext=ext,
                            media_mid=mid, api_filename=os.path.basename(path),
                            details={"prefix": prefix})
        return FileInfo(kind="cache", audio_len=len(data), ext=ext)
    if ext in LEGACY_EXTS:
        return FileInfo(kind="static", audio_len=len(data), ext=ext)
    raise QmcError(f"不支持的后缀: {ext}")


# ----------------------------------------------------------------------------
# 结果嗅探
# ----------------------------------------------------------------------------

def sniff_audio_ext(data: bytes) -> Optional[str]:
    if len(data) < 12:
        return None
    if data[:4] == b"OggS":
        return ".ogg"
    if data[:4] == b"fLaC":
        return ".flac"
    if data[:3] == b"ID3" or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return ".mp3"
    if data[4:8] == b"ftyp":
        return ".m4a"
    if data[:4] == b"RIFF":
        return ".wav"
    return None


def tool_path(name: str) -> Optional[str]:
    """定位 ffmpeg/ffprobe：优先环境变量与 PyInstaller 内置二进制，回退系统 PATH。"""
    env_key = f"QQMUSIC_{name.upper()}"
    if os.environ.get(env_key):
        return os.environ[env_key]
    base = getattr(sys, "_MEIPASS", None)  # PyInstaller onefile 解包目录
    if base:
        names = (name, f"{name}.exe") if IS_WINDOWS else (name,)
        for cand_dir in (base, os.path.join(base, "bin"),
                         os.path.join(base, "ffmpeg")):
            for n in names:
                cand = os.path.join(cand_dir, n)
                ok = os.path.isfile(cand) and (IS_WINDOWS or os.access(cand, os.X_OK))
                if os.environ.get("QQMUSIC_DEBUG_TOOLS") == "1":
                    print(f"[tool_path] {name} cand={cand!r} isfile="
                          f"{os.path.isfile(cand)} iswin={IS_WINDOWS} ok={ok}",
                          file=sys.stderr)
                if ok:
                    return cand
    return shutil.which(name)


def ffprobe_duration(path: str) -> Optional[float]:
    ffprobe = tool_path("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return float(r.stdout.strip())
    except Exception:  # noqa: BLE001
        pass
    return None


# ----------------------------------------------------------------------------
# 凭据（macOS plist / NSKeyedArchiver）
# ----------------------------------------------------------------------------

@dataclass
class Credentials:
    uin: str
    authst: str
    login_type: str = ""
    candidates: List[str] = field(default_factory=list)  # 备用 authst（Win 内存中可能多个）


def _resolve_nskeyed(o, objects: list, depth: int = 0):
    if depth > 10:
        return None
    if isinstance(o, plistlib.UID):
        return _resolve_nskeyed(objects[o.data], objects, depth + 1)
    if isinstance(o, dict):
        return {k: _resolve_nskeyed(v, objects, depth + 1) for k, v in o.items()}
    if isinstance(o, list):
        return [_resolve_nskeyed(x, objects, depth + 1) for x in o]
    return o


def load_macos_credentials(prefs_path: Optional[str] = None) -> Credentials:
    path = prefs_path or DEFAULT_PREFS
    if not os.path.exists(path):
        path = CONTAINER_PREFS
    if not os.path.exists(path):
        raise QmcError(f"未找到 QQ 音乐偏好文件: {path}（请用 --prefs 指定或改用 --uin/--authst）")
    with open(path, "rb") as f:
        plist = plistlib.load(f)
    blob = plist.get("AutoLoginUserInfo")
    if not isinstance(blob, bytes):
        raise QmcError("plist 中无 AutoLoginUserInfo（QQ 音乐未登录？）")
    inner = plistlib.loads(blob)
    objects = inner.get("$objects", [])
    for obj in objects:
        r = _resolve_nskeyed(obj, objects)
        if isinstance(r, dict) and "strAuthst" in r:
            uin = r.get("nUserId") or r.get("strUserAccount")
            authst = r.get("strAuthst")
            if not uin or not authst:
                raise QmcError("UserInfo 中缺少 nUserId/strAuthst")
            return Credentials(uin=str(uin), authst=str(authst),
                               login_type=str(r.get("loginType", "")))
    raise QmcError("NSKeyedArchiver 中未找到 UserInfo/strAuthst")


# ----------------------------------------------------------------------------
# Windows 凭据：Uin 在 ini，authst 只在 QQMusic.exe 内存
# ----------------------------------------------------------------------------

_WIN_PROCESS_NAMES = ("QQMusic.exe", "WeChatAppEx.exe", "qmbrowser.exe")


def _win_read_uin(ini_path: Optional[str] = None) -> str:
    path = ini_path or WIN_CONFIG_INI
    if not os.path.exists(path):
        raise QmcError(f"未找到 QQ 音乐配置: {path}（请登录客户端或 --uin/--authst 指定）")
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8-sig")
    uin = (cp.get("Account", "Uin", fallback="") or "").strip()
    if not uin or uin == "0":
        raise QmcError(f"{path} 中无有效 Uin（请登录 QQ 音乐客户端）")
    return uin


def _win_memory_authst_candidates() -> List[str]:
    """在运行中的客户端进程内存里搜 authst（只读，不落盘）。"""
    if not IS_WINDOWS:
        raise QmcError("仅 Windows 支持内存凭据扫描")

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    MEM_COMMIT = 0x1000
    MEM_PRIVATE = 0x20000
    MEM_IMAGE = 0x1000000

    class _MBI(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", ctypes.wintypes.DWORD),
            ("PartitionId", ctypes.wintypes.WORD),  # Win10 1803+ 必须包含
            ("RegionSize", ctypes.c_size_t),
            ("State", ctypes.wintypes.DWORD),
            ("Protect", ctypes.wintypes.DWORD),
            ("Type", ctypes.wintypes.DWORD),
        ]

    k32 = ctypes.windll.kernel32
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_=-")
    markers = (b'"authst":"', b'"authst": "', b'"authst" : "')

    def extract(data: bytes) -> List[str]:
        out = []
        for marker in markers:
            start = 0
            while True:
                i = data.find(marker, start)
                if i < 0:
                    break
                j = i + len(marker)
                end = data.find(b'"', j)
                if end < 0:
                    end = len(data)
                val = data[j:end]
                if 10 <= len(val) <= 512 and all(chr(c) in allowed for c in val):
                    out.append(val.decode("ascii"))
                start = j
        return out

    found: List[str] = []
    buf = ctypes.create_string_buffer(1024 * 1024)
    for proc_name in _WIN_PROCESS_NAMES:
        pids = []
        snapshot = k32.CreateToolhelp32Snapshot(0x2, 0)
        if snapshot and snapshot != -1:
            class _PE(ctypes.Structure):
                _fields_ = [("dwSize", ctypes.wintypes.DWORD),
                            ("cntUsage", ctypes.wintypes.DWORD),
                            ("th32ProcessID", ctypes.wintypes.DWORD),
                            ("th32DefaultHeapID", ctypes.c_void_p),
                            ("th32ModuleID", ctypes.wintypes.DWORD),
                            ("cntThreads", ctypes.wintypes.DWORD),
                            ("th32ParentProcessID", ctypes.wintypes.DWORD),
                            ("pcPriClassBase", ctypes.c_long),
                            ("dwFlags", ctypes.wintypes.DWORD),
                            ("szExeFile", ctypes.c_char * 260)]
            pe = _PE()
            pe.dwSize = ctypes.sizeof(_PE)
            if k32.Process32First(snapshot, ctypes.byref(pe)):
                while True:
                    exe = pe.szExeFile.decode("gbk", "ignore")
                    if exe.lower() == proc_name.lower():
                        pids.append(pe.th32ProcessID)
                    if not k32.Process32Next(snapshot, ctypes.byref(pe)):
                        break
            k32.CloseHandle(snapshot)
        for pid in pids:
            h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
            if not h:
                continue
            mbi = _MBI()
            addr = 0
            while True:
                if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr),
                                          ctypes.byref(mbi), ctypes.sizeof(_MBI)):
                    break
                if mbi.State == MEM_COMMIT and mbi.Type in (MEM_PRIVATE, MEM_IMAGE):
                    off = 0
                    while off < mbi.RegionSize:
                        chunk = min(1024 * 1024, mbi.RegionSize - off)
                        read = ctypes.c_size_t(0)
                        ok = k32.ReadProcessMemory(h, ctypes.c_void_p(addr + off),
                                                   buf, chunk, ctypes.byref(read))
                        if ok and read.value > 0:
                            for v in extract(ctypes.string_at(buf, read.value)):
                                if v not in found:
                                    found.append(v)
                        off += chunk
                addr += mbi.RegionSize
            k32.CloseHandle(h)
    return found


def load_windows_credentials(ini_path: Optional[str] = None) -> Credentials:
    """Uin 读 ini；authst 从内存提取（多个候选都保留，调用时自动轮换）。"""
    uin = _win_read_uin(ini_path)
    candidates = _win_memory_authst_candidates()
    if not candidates:
        raise QmcError("未在 QQMusic.exe 内存中找到 authst（请启动并登录客户端）")
    first, rest = candidates[0], candidates[1:]
    # 优先取第一个能过歌单接口的候选
    for authst in candidates:
        c = Credentials(uin=uin, authst=authst)
        try:
            req = musicu(c, "music.musicasset.PlaylistBaseRead",
                         "GetPlaylistByUin", {"uin": uin, "v_cache": []})
            data = req.get("data") or {}
            if req.get("code") in (0, None) and data.get("v_playlist") is not None:
                first, rest = authst, [a for a in candidates if a != authst]
                break
        except Exception:  # noqa: BLE001
            continue
    return Credentials(uin=uin, authst=first, candidates=rest)


def load_credentials(opts) -> Credentials:
    if opts.uin and opts.authst:
        return Credentials(str(opts.uin), str(opts.authst))
    if IS_WINDOWS:
        return load_windows_credentials(opts.prefs)
    return load_macos_credentials(opts.prefs)


# ----------------------------------------------------------------------------
# GetEVkey API
# ----------------------------------------------------------------------------

_warned_ssl = False


def _urlopen(req: urllib.request.Request, timeout: int = 60):
    global _warned_ssl

    def _open(verify: bool):
        ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
        return urllib.request.urlopen(req, context=ctx, timeout=timeout)

    def _warn():
        global _warned_ssl
        if not _warned_ssl:
            print("[warn] 系统证书链缺失，回退为不校验证书的 HTTPS（本机 Python 环境问题）",
                  file=sys.stderr)
            _warned_ssl = True

    try:
        return _open(True)
    except ssl.SSLCertVerificationError:
        _warn()
        return _open(False)
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            _warn()
            return _open(False)
        raise


def _http_post_json(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": API_USER_AGENT,
            "Referer": "https://y.qq.com/",
        },
    )
    with _urlopen(req, timeout=30) as resp:
        raw = resp.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ApiError(f"API 响应不是 JSON: {raw[:200]!r}") from e


def fetch_url_bytes(url: str, referer: str = "https://y.qq.com/",
                    retries: int = 3) -> bytes:
    """下载 URL 内容；对 IncompleteRead/连接中断做重试（每次退避 1.5s）。"""
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": API_USER_AGENT, "Referer": referer},
            )
            with _urlopen(req, timeout=120) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001  (URLError / IncompleteRead / 超时等)
            last = e
            if attempt + 1 < retries:
                time.sleep(1.5)
    raise ApiError(f"下载失败（重试 {retries} 次）: {last}")


def _api_comm(creds: Credentials) -> dict:
    return {"uin": creds.uin, "authst": creds.authst, "ct": "19",
            "cv": "1859", "tmeLoginType": "3"}


def musicu(creds: Credentials, module: str, method: str, param: dict,
           req_key: str = "req") -> dict:
    resp = _http_post_json(API_URL, {
        "comm": _api_comm(creds),
        req_key: {"module": module, "method": method, "param": param},
    })
    return resp.get(req_key) or {}


def call_get_evkey(creds: Credentials, filename: str, songmid: str) -> dict:
    body = {
        "comm": _api_comm(creds),
        "req_1": {
            "module": "music.vkey.GetEVkey", "method": "CgiGetEVkey",
            "param": {
                "filename": [filename], "guid": "10000",
                "songmid": [songmid], "songtype": [1],
                "uin": creds.uin, "loginflag": 1,
                "platform": API_PLATFORM, "ctx": 1,
            },
        },
    }
    try:
        resp = _http_post_json(API_URL, body)
    except urllib.error.URLError as e:
        raise ApiError(f"网络错误: {e}") from e
    if resp.get("code") not in (0, None):
        raise ApiError(f"API 顶层错误 code={resp.get('code')}（authst 可能已过期，请重新登录 QQ 音乐）")
    req1 = resp.get("req_1") or {}
    if req1.get("code") not in (0, None):
        raise ApiError(f"req_1 错误 code={req1.get('code')}")
    data = req1.get("data") or {}
    infos = data.get("midurlinfo") or []
    if not infos:
        raise ApiError("API 响应 midurlinfo 为空")
    info = infos[0]
    result = info.get("result")
    if result != 0:
        raise ApiError(f"GetEVkey result={result} subcode={info.get('subcode')} "
                       f"(文件名={filename})")
    # ekey 可能为空（该资源是普通文件或条目失效），交由下载分支按普通文件尝试
    info["sip"] = data.get("sip") or []
    return info


def call_get_vkey(creds: Credentials, filename: str, songmid: str) -> dict:
    """普通（非加密）资源的播放 URL。"""
    req = musicu(
        creds, "vkey.GetVkeyServer", "CgiGetVkey",
        {"filename": [filename], "guid": "10000", "songmid": [songmid],
         "songtype": [0], "uin": creds.uin, "loginflag": 1,
         "platform": API_PLATFORM},
        req_key="req_1",
    )
    data = req.get("data") or {}
    infos = data.get("midurlinfo") or []
    if not infos:
        raise ApiError("GetVkey 响应 midurlinfo 为空")
    info = infos[0]
    if info.get("result") not in (0, None):
        raise ApiError(f"GetVkey result={info.get('result')} (文件名={filename})")
    purl = info.get("purl") or ""
    if not purl:
        raise ApiError(f"GetVkey 未返回 purl ({filename})")
    info["sip"] = data.get("sip") or []
    return info


# 普通资源在 Win 端 sip 可能为空，按实测可用性顺序回退 HTTPS CDN
PLAIN_CDN_HOSTS = (
    "https://dl.stream.qqmusic.qq.com/",
    "https://ws.stream.qqmusic.qq.com/",
    "https://isure.stream.qqmusic.qq.com/",
    "https://aqqmusic.tc.qq.com/",
)


def resolve_download_urls(info: dict) -> List[str]:
    """返回候选下载 URL 列表（sip 优先，为空时回退已知 CDN）。"""
    purl = info.get("purl") or ""
    if not purl:
        raise ApiError("API 响应缺少 purl")
    if purl.startswith("http://") or purl.startswith("https://"):
        return [purl]
    hosts = [h for h in (info.get("sip") or []) if h]
    hosts += list(PLAIN_CDN_HOSTS)
    out: List[str] = []
    seen = set()
    for h in hosts:
        u = h.rstrip("/") + "/" + purl.lstrip("/")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def fetch_audio_from_urls(info: dict) -> bytes:
    """逐个候选 URL 下载，取第一个非空且像音频的内容（单 URL 内部已重试）。"""
    last_err: Optional[Exception] = None
    for url in resolve_download_urls(info):
        try:
            data = fetch_url_bytes(url)
        except (ApiError, urllib.error.URLError) as e:
            last_err = e
            continue
        if len(data) < 4096:
            last_err = QmcError(f"响应过小 {len(data)}B（可能被 CDN 拒绝）")
            continue
        return data
    raise ApiError(f"所有下载地址均失败，最后错误: {last_err}")


# ----------------------------------------------------------------------------
# 歌曲元数据库
# ----------------------------------------------------------------------------

@dataclass
class SongMeta:
    song_id: int
    name: str = ""
    singer: str = ""
    reserve1: str = ""   # media_mid / songmid
    reserve8: str = ""   # 资源 ID


class SongDb:
    def __init__(self, db_path: str):
        if sqlite3 is None:
            raise QmcError("当前 Python 环境缺少 sqlite3 模块，无法读取歌曲数据库")
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._cache: Dict[int, Optional[SongMeta]] = {}

    def close(self):
        self.conn.close()

    def get(self, song_id: int) -> Optional[SongMeta]:
        if song_id in self._cache:
            return self._cache[song_id]
        row = None
        for q in (
            "SELECT id,name,singer,K_SONG_RESERVE1,K_SONG_RESERVE8 FROM SONGS "
            "WHERE id=? AND type=13 LIMIT 1",
            "SELECT id,name,singer,K_SONG_RESERVE1,K_SONG_RESERVE8 FROM SONGS "
            "WHERE id=? LIMIT 1",
        ):
            cur = self.conn.execute(q, (song_id,))
            row = cur.fetchone()
            if row:
                break
        meta = SongMeta(row[0], row[1] or "", row[2] or "", row[3] or "", row[4] or "") if row else None
        self._cache[song_id] = meta
        return meta


def cache_api_candidates(meta: SongMeta, ext: str) -> List[str]:
    rid = meta.reserve8 or meta.reserve1
    if not rid:
        return []
    if ext == ".mgg":
        # 加密资源前缀（客户端 CSongURL 反编译 + API 实测）：
        #   O8M0 = 320k OGG；O4M0 = 128k/标准 OGG；O6M0 = 192k OGG；M8M0 = 320k MP3
        # 缓存文件不记录当时质量，逐个试，以解密后音频签名为准。
        return [f"O8M0{rid}.mgg", f"O4M0{rid}.mgg", f"O6M0{rid}.mgg",
                f"M8M0{rid}.mgg"]
    if ext == ".mflac":
        return [f"F0M0{rid}.mflac"]
    return []


# ----------------------------------------------------------------------------
# 歌单 / 我喜欢 / 在线下载
# ----------------------------------------------------------------------------

@dataclass
class PlaylistItem:
    tid: int
    dirid: int
    name: str
    song_num: int


def get_created_playlists(creds: Credentials) -> List[PlaylistItem]:
    # Win 客户端: GetPlaylistByUin；Mac 客户端: GetPlaylistInfoDiff（模块同名）
    method = "GetPlaylistByUin" if API_PLATFORM == "27" else "GetPlaylistInfoDiff"
    req = musicu(creds, "music.musicasset.PlaylistBaseRead",
                 method, {"uin": creds.uin, "v_cache": []})
    if req.get("code") not in (0, None):
        raise ApiError(f"获取创建歌单失败: code={req.get('code')}")
    out = []
    for p in (req.get("data") or {}).get("v_playlist") or []:
        out.append(PlaylistItem(tid=int(p.get("tid") or 0),
                                dirid=int(p.get("dirId") or 0),
                                name=str(p.get("dirName") or ""),
                                song_num=int(p.get("songNum") or 0)))
    return out


def get_collected_playlists(creds: Credentials) -> List[PlaylistItem]:
    # Win: GetPlaylistFavInfo；Mac: GetPlaylistInfoDiff
    method = "GetPlaylistFavInfo" if API_PLATFORM == "27" else "GetPlaylistInfoDiff"
    req = musicu(creds, "music.musicasset.PlaylistFavRead",
                 method, {"uin": creds.uin, "v_cache": []})
    if req.get("code") not in (0, None):
        raise ApiError(f"获取收藏歌单失败: code={req.get('code')}")
    out = []
    for p in (req.get("data") or {}).get("v_list") or []:
        out.append(PlaylistItem(tid=int(p.get("tid") or 0),
                                dirid=int(p.get("dirId") or 0),
                                name=str(p.get("name") or ""),
                                song_num=int(p.get("songnum") or 0)))
    return out


def get_playlist_detail(creds: Credentials, tid: int, max_songs: int = 0,
                        page_size: int = 50):
    """返回 (title, songs)。songs 每项含 mid/name/singer/album。"""
    songs: List[dict] = []
    title = ""
    begin = 0
    if API_PLATFORM == "27":
        module, method = "music.srfDissInfo.DissInfoForPc", "uniform_get_Dissinfo"
        param = {"disstid": tid, "song_num": page_size, "song_begin": begin}
    else:
        module, method = "music.srfDissInfo.DissInfo", "CgiGetDiss"
        param = {"disstid": tid, "dirid": 0, "host_uin": "0",
                 "login_uin": creds.uin, "song_num": page_size,
                 "song_begin": begin}
    while True:
        req = musicu(creds, module, method, param)
        if req.get("code") not in (0, None):
            raise ApiError(f"获取歌单详情失败: code={req.get('code')}")
        data = req.get("data") or {}
        if data.get("code") not in (0, None):
            raise ApiError(f"歌单详情业务错误: code={data.get('code')} "
                           f"msg={data.get('msg')}")
        di = data.get("dirinfo") or {}
        title = str(di.get("title") or title)
        batch = data.get("songlist") or []
        for s in batch:
            if s.get("mid"):
                songs.append(s)
            if max_songs and len(songs) >= max_songs:
                return title, songs[:max_songs]
        if max_songs and len(songs) >= max_songs:
            return title, songs[:max_songs]
        if not batch or not data.get("hasmore"):
            return title, songs
        begin += len(batch)
        param["song_begin"] = begin


def song_display(song: dict) -> str:
    artists = "、".join(s.get("name", "") for s in song.get("singer") or [])
    album = (song.get("album") or {}).get("name", "")
    return f"{artists} - {song.get('name')} [{album}] ({song.get('mid')})"


def strip_musicex_footer(data: bytes) -> bytes:
    if len(data) >= 16 and data[-8:] == b"musicex\x00":
        try:
            size = struct.unpack("<I", data[-16:-12])[0]
            if 16 <= size <= len(data):
                return data[:len(data) - size]
        except Exception:  # noqa: BLE001
            pass
    return data


def download_song_bytes(ctx: BatchContext, song: dict, qualities: Sequence[str]):
    """按音质顺序尝试加密/普通下载，返回 (audio_bytes, ext, quality)。"""
    mid = song.get("mid") or ""
    if not mid:
        raise QmcError("歌曲缺少 mid")
    errors: List[str] = []
    for q in qualities:
        try:
            if q in QUALITY_ENCRYPTED:
                prefix, ext = QUALITY_ENCRYPTED[q]
                filename = f"{prefix}{mid}.{ext}"
                info = ctx.get_evkey_info(filename, mid)
                if info.get("ekey"):
                    raw = fetch_audio_from_urls(info)
                    raw = strip_musicex_footer(raw)
                    key = parse_ekey(info["ekey"])
                    out = qmc2_decrypt(raw, key)
                    sniff = sniff_audio_ext(out)
                    if sniff:
                        return bytes(out), sniff[1:], q
                    errors.append(f"{q}: 解密后签名校验失败")
                else:
                    # 服务端返回了 purl 但没有 ekey → 当作普通文件直接下载
                    raw = fetch_audio_from_urls(info)
                    sniff = sniff_audio_ext(raw)
                    if sniff:
                        return raw, sniff[1:], q
                    errors.append(f"{q}: 无 ekey 且下载内容签名校验失败")
            elif q in QUALITY_PLAIN:
                prefix, ext = QUALITY_PLAIN[q]
                filename = f"{prefix}{mid}.{ext}"
                info = ctx.get_vkey_info(filename, mid)
                raw = fetch_audio_from_urls(info)
                sniff = sniff_audio_ext(raw)
                if sniff:
                    return raw, sniff[1:], q
                errors.append(f"{q}: 下载内容签名校验失败")
            else:
                errors.append(f"{q}: 未知音质")
        except (ApiError, QmcError) as e:
            errors.append(f"{q}: {e}")
        except urllib.error.URLError as e:
            errors.append(f"{q}: 下载网络错误 {e}")
    hint = ""
    joined = "; ".join(errors)
    if re.search(r"104003|104011", joined):
        hint = "｜提示：104003/104011 表示账号无该歌曲可用资源（未购买付费单曲、版权限制或已下架），不是工具问题"
    raise QmcError("所有音质均失败 -> " + joined + hint)


def apply_tags(path: str, title: str, artist: str, album: str) -> bool:
    ffmpeg = tool_path("ffmpeg")
    if not ffmpeg:
        return False
    tmp = f"{path}.tag{Path(path).suffix}"  # 保留扩展名让 ffmpeg 推断容器格式
    cmd = [ffmpeg, "-v", "error", "-y", "-i", path, "-c", "copy",
           "-metadata", f"title={title}",
           "-metadata", f"artist={artist}"]
    if album:
        cmd += ["-metadata", f"album={album}"]
    cmd += [tmp]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, path)
            return True
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:  # noqa: BLE001
        if os.path.exists(tmp):
            os.remove(tmp)
    return False

def sanitize_name(name: str, max_len: int = 120) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "unnamed"
    return name[:max_len].rstrip()


def pick_output_path(out_dir: str, source: Path, title: Optional[str],
                     ext: str, in_place: bool) -> str:
    if in_place:
        base = source.parent
        stem = source.name.rsplit(".", 1)[0]
    else:
        base = Path(out_dir)
        stem = sanitize_name(title) if title else source.name.rsplit(".", 1)[0]
    base.mkdir(parents=True, exist_ok=True)
    return str(base / f"{stem}{ext}")


@dataclass
class Result:
    path: str
    status: str
    message: str = ""
    output: str = ""
    duration: Optional[float] = None


class BatchContext:
    def __init__(self, opts: argparse.Namespace):
        self.opts = opts
        self.creds: Optional[Credentials] = None
        self.db: Optional[SongDb] = None
        self.evkey_cache: Dict[Tuple[str, str], dict] = {}
        self.vkey_cache: Dict[Tuple[str, str], dict] = {}
        self.api_calls = 0
        self.stop = False
        self.stop_reason = ""

    def get_creds(self) -> Credentials:
        if self.creds is None:
            self.creds = load_credentials(self.opts)
        return self.creds

    def get_db(self) -> SongDb:
        if self.db is None:
            if not os.path.exists(self.opts.db):
                raise QmcError(f"歌曲数据库不存在: {self.opts.db}")
            self.db = SongDb(self.opts.db)
        return self.db

    def _api_wait(self):
        if self.opts.delay > 0:
            time.sleep(self.opts.delay)

    def _api_with_creds(self, caller):
        """调用 API；Win 内存中可能有多个 authst，失败时自动轮换候选。"""
        creds = self.get_creds()
        last: Optional[Exception] = None
        while True:
            try:
                info = caller(creds)
            except ApiError as e:
                last = e
                if creds.candidates:
                    creds.authst = creds.candidates.pop(0)
                    continue
                if re.search(r"500001|authst", str(e)):
                    self.stop = True
                    self.stop_reason = str(e)
                raise
            self.api_calls += 1
            self._api_wait()
            return info

    def get_evkey_info(self, filename: str, songmid: str) -> dict:
        key = (filename, songmid)
        if key not in self.evkey_cache:
            if self.opts.no_api:
                raise QmcError(f"--no-api 模式下无法为 {filename} 获取 ekey")
            self.evkey_cache[key] = self._api_with_creds(
                lambda c: call_get_evkey(c, filename, songmid))
        return self.evkey_cache[key]

    def get_ekey(self, filename: str, songmid: str) -> str:
        return self.get_evkey_info(filename, songmid)["ekey"]

    def get_vkey_info(self, filename: str, songmid: str) -> dict:
        key = (filename, songmid)
        if key not in self.vkey_cache:
            if self.opts.no_api:
                raise QmcError(f"--no-api 模式下无法为 {filename} 获取播放地址")
            self.vkey_cache[key] = self._api_with_creds(
                lambda c: call_get_vkey(c, filename, songmid))
        return self.vkey_cache[key]

    def close(self):
        if self.db is not None:
            self.db.close()


def decrypt_with_ekey_candidates(data: bytes, candidates: List[Tuple[str, str]],
                                 ctx: BatchContext) -> bytes:
    attempts: List[str] = []
    for filename, songmid in candidates:
        try:
            ekey = ctx.get_ekey(filename, songmid)
            key = parse_ekey(ekey)
            out = qmc2_decrypt(data, key)
            if sniff_audio_ext(out):
                return out
            attempts.append(f"{filename}: 解密结果签名校验失败")
        except Exception as e:  # noqa: BLE001
            attempts.append(f"{filename}: {e}")
    raise QmcError("所有候选 ekey 均失败 -> " + "; ".join(attempts))


def info_line(path: str, data: bytes, info: FileInfo) -> str:
    bits = [f"kind={info.kind}", f"ext={info.ext}", f"audio_len={info.audio_len}"]
    if info.song_id:
        bits.append(f"song_id={info.song_id}")
    if info.media_mid:
        bits.append(f"media_mid={info.media_mid}")
    if info.api_filename:
        bits.append(f"api_filename={info.api_filename}")
    for k, v in info.details.items():
        bits.append(f"{k}={v}")
    return f"{path}: " + ", ".join(bits)


def process_file(path: str, ctx: BatchContext, idx: int, total: int) -> Result:
    opts = ctx.opts
    src = Path(path)
    try:
        data = src.read_bytes()
        info = parse_file(data, path)
        if opts.info:
            print(info_line(path, data, info))
            return Result(path, "info", "")
        if opts.dry_run:
            print(f"[dry-run] {path} -> kind={info.kind}")
            return Result(path, "dry-run", "")

        # 元数据（标题/API 参数）优先用歌曲 ID 查库
        if info.kind == "cache" and info.song_id is None:
            m = re.match(r"^(\d+)-\d+\.", src.name)
            if m:
                info.song_id = int(m.group(1))
        meta: Optional[SongMeta] = None
        if info.song_id is not None:
            try:
                meta = ctx.get_db().get(info.song_id)
            except QmcError:
                meta = None

        title = f"{meta.singer} - {meta.name}".strip(" -") if meta else None

        # 重跑时先按后缀猜输出路径，已存在则直接 SKIP（省一次 API/解密）
        _guess = {
            ".mgg": ".ogg", ".mgg0": ".ogg", ".mgg1": ".ogg", ".mggl": ".ogg",
            ".qmc2": ".ogg", ".qmcogg": ".ogg",
            ".mflac": ".flac", ".mflac0": ".flac", ".mflac1": ".flac",
            ".qmcflac": ".flac", ".bkcflac": ".flac",
            ".qmc0": ".mp3", ".qmc3": ".mp3", ".bkcmp3": ".mp3",
            ".tkm": ".m4a",
        }.get(info.ext)
        if _guess and not opts.overwrite:
            _cand = pick_output_path(
                opts.out_dir if not opts.in_place else str(src.parent),
                src, title, _guess, opts.in_place)
            if os.path.exists(_cand):
                print(f"[{idx}/{total}] SKIP {path}  输出已存在: {_cand}")
                return Result(path, "skip", f"输出已存在: {_cand}")

        if info.kind == "musicex":
            assert info.api_filename and info.media_mid
            ekey = ctx.get_ekey(info.api_filename, info.media_mid)
            out = qmc2_decrypt(data[:info.audio_len], parse_ekey(ekey))
        elif info.kind in ("qtag", "v1"):
            assert info.ekey is not None
            out = qmc2_decrypt(data[:info.audio_len], parse_ekey(info.ekey))
        elif info.kind == "static":
            out = bytearray(data[:info.audio_len])
            StaticCipher().decrypt(out, 0)
        elif info.kind == "win_cache":
            # Win duty 缓存：文件名自带质量前缀 + mid，无 footer、无需数据库
            assert info.api_filename and info.media_mid
            out = decrypt_with_ekey_candidates(
                data[:info.audio_len],
                [(info.api_filename, info.media_mid)],
                ctx,
            )
        elif info.kind == "cache":
            if info.song_id is None or meta is None:
                raise QmcError("iMusic 缓存文件无 footer，且无法从文件名/数据库取到歌曲 ID")
            candidates = cache_api_candidates(meta, info.ext)
            if not candidates:
                raise QmcError("数据库缺少 K_SONG_RESERVE8/1，无法构造 API 文件名")
            out = decrypt_with_ekey_candidates(
                data[:info.audio_len],
                [(c, meta.reserve1 or meta.reserve8) for c in candidates],
                ctx,
            )
        else:  # pragma: no cover
            raise QmcError(f"未知类型 {info.kind}")

        out_ext = sniff_audio_ext(out)
        if out_ext is None:
            head = bytes(out[:16]).hex()
            raise QmcError(f"解密结果未通过音频签名校验，头部: {head}")
        if out_ext == ".mp3" and info.ext in (".mflac", ".qmcflac", ".bkcflac"):
            raise QmcError(f"期望无损文件却得到 MP3 签名（{out_ext}），可能用错 ekey")

        out_path = pick_output_path(
            opts.out_dir if not opts.in_place else str(src.parent),
            src, title, out_ext, opts.in_place,
        )
        if os.path.exists(out_path) and not opts.overwrite:
            print(f"[{idx}/{total}] SKIP {path}  输出已存在: {out_path}")
            return Result(path, "skip", f"输出已存在: {out_path}")
        Path(out_path).write_bytes(out)

        dur = ffprobe_duration(out_path) if not opts.no_ffprobe else None
        msg = f"{out_ext[1:].upper()} {os.path.getsize(out_path)}B"
        if dur is not None:
            msg += f" {dur:.1f}s"
        print(f"[{idx}/{total}] OK  {out_path}  ({msg})")
        return Result(path, "ok", msg, out_path, dur)

    except QmcError as e:
        print(f"[{idx}/{total}] FAIL {path}  {e}", file=sys.stderr)
        return Result(path, "fail", str(e))
    except Exception as e:  # noqa: BLE001
        print(f"[{idx}/{total}] FAIL {path}  未预期错误: {e!r}", file=sys.stderr)
        return Result(path, "fail", repr(e))


def discover(inputs: Sequence[str], recursive: bool) -> List[str]:
    files: List[str] = []
    lib = default_library_dir()
    library_abs = os.path.abspath(lib) if lib else ""
    for item in inputs:
        if os.path.isfile(item):
            files.append(item)
        elif os.path.isdir(item):
            # iMusic 库是两层目录结构，默认也递归
            do_walk = recursive or os.path.abspath(item) == library_abs
            if do_walk:
                for root, _dirs, names in os.walk(item):
                    for n in sorted(names):
                        if Path(n).suffix.lower() in SUPPORTED_EXTS:
                            files.append(os.path.join(root, n))
            else:
                for n in sorted(os.listdir(item)):
                    p = os.path.join(item, n)
                    if os.path.isfile(p) and Path(n).suffix.lower() in SUPPORTED_EXTS:
                        files.append(p)
    return sorted(set(os.path.abspath(f) for f in files))


def run_self_test() -> None:
    print("== self-test ==")

    # 1. TC-TEA ECB / CBC 官方测试向量（jixunmoe/tc_tea_rust）
    t = _tea_ecb_decrypt(
        int.from_bytes(bytes.fromhex("56276ba980b9ec16"), "big"),
        [0x01020304, 0x05060708, 0x090A0B0C, 0x0D0E0F00],
    )
    assert t.to_bytes(8, "big") == bytes(range(1, 9)), "ECB 向量失败"
    assert tc_tea_decrypt(
        bytes.fromhex("91095162e3f5b6dc6b414b50d1a5b84ec50d0c1b1196fd3c"),
        bytes.fromhex("31323334353637384142434445464748"),
    ) == bytes(range(1, 9)), "CBC 向量失败"
    print("  TC-TEA (16轮, tweaked CBC): OK")

    # 2. simpleMakeKey
    assert simple_make_key(106, 8) == bytes.fromhex("695646382b20150b")
    print("  simpleMakeKey: OK")

    # 3. EncV1 / EncV2 ekey 包装往返
    import random as _random
    rnd = _random.Random(42)
    header = bytes(rnd.randrange(256) for _ in range(8))
    body_plain = bytes(rnd.randrange(256) for _ in range(300))
    tea_key = derive_tea_key(header)
    encv1_raw = header + tc_tea_encrypt(body_plain, tea_key)
    encv1_b64 = base64.b64encode(encv1_raw).decode()
    assert parse_ekey(encv1_b64) == header + body_plain, "EncV1 往返失败"
    stage = tc_tea_encrypt(tc_tea_encrypt(encv1_b64.encode(), ENCV2_STAGE2_KEY),
                           ENCV2_STAGE1_KEY)
    encv2 = base64.b64encode(ENCV2_PREFIX + stage).decode()
    assert parse_ekey(encv2) == header + body_plain, "EncV2 往返失败"
    print("  ekey EncV1 / EncV2 往返: OK")

    # 4. Map / RC4 加解密往返
    for key, expect_rc4 in ((bytes(rnd.randrange(256) for _ in range(128)), False),
                            (header + body_plain, True)):
        plain = b"OggS\x00\x02" + bytes(rnd.randrange(256) for _ in range(4096))
        cipher = make_qmc2_cipher(key)
        buf = bytearray(plain)
        cipher.decrypt(buf, 0)
        cipher2 = make_qmc2_cipher(key)
        cipher2.decrypt(buf, 0)
        assert bytes(buf) == plain, f"{'RC4' if expect_rc4 else 'Map'} 往返失败"
        assert isinstance(cipher, RC4Cipher) == expect_rc4
    print("  QMC2 Map / RC4 往返: OK")

    # 5. QTag / V1 样本生成与解析解密
    plain_ogg = b"OggS\x00\x02\x00\x00" + bytes(rnd.randrange(256) for _ in range(8192))
    key_rc4 = header + body_plain
    ciphertext = qmc2_decrypt(plain_ogg, key_rc4)
    ekey_b64 = encv1_b64.encode()
    # QTag: [key,songid,ver][u32 BE len]["QTag"]
    meta_block = ekey_b64 + b",123456,2"
    qtag = bytes(ciphertext) + meta_block + struct.pack(">I", len(meta_block)) + b"QTag"
    info = parse_file(qtag, "sample.mggl")
    assert info.kind == "qtag" and info.song_id == 123456
    assert qmc2_decrypt(qtag[:info.audio_len], parse_ekey(info.ekey)) == plain_ogg
    # V1: [cipher][key][u32 LE len]
    v1 = bytes(ciphertext) + ekey_b64 + struct.pack("<I", len(ekey_b64))
    info = parse_file(v1, "sample.mgg")
    assert info.kind == "v1"
    assert qmc2_decrypt(v1[:info.audio_len], parse_ekey(info.ekey)) == plain_ogg
    print("  QTag / V1 尾块解析+解密: OK")

    # 6. QMC1 Static 往返（static 文件无尾部，需保证末 4 字节密文不会误判成 V1 key 长度）
    plain_mp3 = b"ID3\x03\x00" + bytes(rnd.randrange(256) for _ in range(4096))
    ct = bytearray(plain_mp3)
    StaticCipher().decrypt(ct, 0)
    while struct.unpack("<I", bytes(ct[-4:]))[0] <= 0x400:
        plain_mp3 = plain_mp3[:-1] + bytes([(plain_mp3[-1] + 1) & 0xFF])
        ct = bytearray(plain_mp3)
        StaticCipher().decrypt(ct, 0)
    info = parse_file(bytes(ct), "sample.qmc3")
    assert info.kind == "static"
    out = bytearray(ct[:info.audio_len])
    StaticCipher().decrypt(out, 0)
    assert bytes(out) == plain_mp3
    print("  QMC1 Static: OK")

    # 7. musicex footer 解析
    footer = bytearray(0xC0)
    struct.pack_into("<I", footer, 0x00, 337457358)
    struct.pack_into("<I", footer, 0x04, 2)
    struct.pack_into("<I", footer, 0x08, 2)
    for off, s in ((0x0C, "001WZk3f29SpSn"), (0x48, "O4M0001WZk3f29SpSn.mgg")):
        footer[off:off + len(s) * 2] = s.encode("utf-16-le")
    footer[0xB0:0xB4] = struct.pack("<I", 0xC0)
    footer[0xB4:0xB8] = struct.pack("<I", 1)
    footer[0xB8:0xC0] = b"musicex\x00"
    fake = b"\x00" * 64 + bytes(footer)
    info = parse_file(fake, "sample.mgg")
    assert info.kind == "musicex"
    assert info.song_id == 337457358 and info.media_mid == "001WZk3f29SpSn"
    assert info.api_filename == "O4M0001WZk3f29SpSn.mgg"
    print("  musicex footer 解析: OK")

    # 8. Win duty 缓存文件名解析
    info = parse_file(b"\x00" * 64, "O4M0001WZk3f29SpSn.mgg")
    assert info.kind == "win_cache"
    assert info.media_mid == "001WZk3f29SpSn"
    assert info.api_filename == "O4M0001WZk3f29SpSn.mgg"
    info = parse_file(b"\x00" * 64, "F0M0002RELDn2XQBlT.mflac")
    assert info.kind == "win_cache" and info.media_mid == "002RELDn2XQBlT"
    info = parse_file(b"\x00" * 64, "391934297-13.mgg")
    assert info.kind == "cache"
    print("  Win duty 缓存文件名解析: OK")

    print("== 全部自测通过 ==")


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="QQ 音乐 QMC 加密音频批量解密工具（仅限个人合法备份）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("paths", nargs="*",
                   help="文件或目录；缺省时扫描 ~/Downloads 与 QQ 音乐 iMusic 缓存库")
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="输出目录")
    p.add_argument("--in-place", action="store_true", help="输出到源文件同目录")
    p.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出")
    p.add_argument("--recursive", action="store_true", help="目录递归扫描")
    p.add_argument("--limit", type=int, default=0, help="最多处理 N 个文件（0=不限）")
    p.add_argument("--dry-run", action="store_true", help="只打印识别结果，不调 API 不写文件")
    p.add_argument("--info", action="store_true", help="只打印每个文件的格式信息")
    p.add_argument("--no-api", action="store_true", help="禁用 API（只能解内嵌 ekey/静态格式）")
    p.add_argument("--no-ffprobe", action="store_true", help="不用 ffprobe 验证时长")
    p.add_argument("--delay", type=float, default=0.15, help="每次 API 成功后的间隔秒数")
    p.add_argument("--db", default=DEFAULT_DB, help="QQ 音乐 sqlite 数据库路径")
    p.add_argument("--prefs", default=None,
                   help="Mac: 偏好 plist 路径；Win: QQMusicServiceConfig.ini 路径")
    p.add_argument("--uin", default=None, help="手动指定 uin（跳过自动凭据）")
    p.add_argument("--authst", default=None, help="手动指定 authst（跳过自动凭据）")
    p.add_argument("--platform", default="auto", choices=["auto", "20", "27"],
                   help="API platform 参数：auto 自动检测（Win=27, Mac=20）")
    p.add_argument("--list-playlists", action="store_true",
                   help="列出当前账号的创建歌单和收藏歌单后退出")
    p.add_argument("--playlist", action="append", type=int, default=[],
                   help="下载指定歌单（传 tid，可重复；先 --list-playlists 查看）")
    p.add_argument("--favorites", action="store_true",
                   help="下载「我喜欢」列表（dirId=201）")
    p.add_argument("--quality", default=DEFAULT_QUALITY,
                   help="下载音质优先级，逗号分隔：flac,320,192,128,m4a,mp3-128,mp3-320")
    p.add_argument("--tag", dest="tag", action="store_true", default=True,
                   help="用 ffmpeg 写入标题/歌手/专辑标签（默认开，失败不报错）")
    p.add_argument("--no-tag", dest="tag", action="store_false",
                   help="不写音频标签")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="进入交互式菜单")
    p.add_argument("--debug-tools", action="store_true",
                   help="打印内置 ffmpeg/ffprobe 定位与版本后退出")
    p.add_argument("--self-test", action="store_true", help="运行内置自测后退出")
    return p


def parse_qualities(spec: str) -> List[str]:
    known = set(QUALITY_ENCRYPTED) | set(QUALITY_PLAIN)
    out = []
    for item in spec.split(","):
        q = item.strip().lower()
        if not q:
            continue
        if q not in known:
            raise QmcError(f"未知音质 {q!r}，可选: {', '.join(sorted(known))}")
        if q not in out:
            out.append(q)
    if not out:
        raise QmcError("音质列表为空")
    return out


def print_playlists(creds: Credentials) -> None:
    created = get_created_playlists(creds)
    collected = get_collected_playlists(creds)
    print(f"创建的歌单 ({len(created)}):")
    for p in created:
        print(f"  tid={p.tid:<12} dirId={p.dirid:<4} {p.name}  ({p.song_num} 首)")
    print(f"收藏的歌单 ({len(collected)}):")
    for p in collected:
        print(f"  tid={p.tid:<12} dirId={p.dirid:<4} {p.name}  ({p.song_num} 首)")


def download_playlist_songs(ctx: BatchContext, tid: int, default_name: str,
                            results: List[Result]) -> None:
    opts = ctx.opts
    creds = ctx.get_creds()
    qualities = parse_qualities(opts.quality)
    title, songs = get_playlist_detail(creds, tid, max_songs=opts.limit)
    folder = Path(opts.out_dir) / sanitize_name(title or default_name)
    print(f"歌单「{title or default_name}」: {len(songs)} 首 -> {folder}")
    if opts.dry_run:
        for i, s in enumerate(songs, 1):
            print(f"  [dry-run] {i}/{len(songs)} {song_display(s)}")
        return
    folder.mkdir(parents=True, exist_ok=True)
    ok = fail = skip = 0
    for i, song in enumerate(songs, 1):
        name = str(song.get("name") or "未知歌曲")
        artists = "、".join(s.get("name", "") for s in song.get("singer") or [])
        album = str((song.get("album") or {}).get("name", ""))
        try:
            audio, ext, quality = download_song_bytes(ctx, song, qualities)
            fname = sanitize_name(f"{artists} - {name}") if artists else sanitize_name(name)
            out_path = folder / f"{fname}.{ext}"
            if out_path.exists() and not opts.overwrite:
                print(f"[{i}/{len(songs)}] SKIP {fname}.{ext}  (已存在)")
                skip += 1
                results.append(Result(f"{artists} - {name}", "skip",
                                      f"输出已存在: {out_path}"))
                continue
            out_path.write_bytes(audio)
            if opts.tag:
                apply_tags(str(out_path), name, artists, album)
            dur = ffprobe_duration(str(out_path)) if not opts.no_ffprobe else None
            msg = f"{ext.upper()} {quality} {len(audio)}B"
            if dur is not None:
                msg += f" {dur:.1f}s"
            print(f"[{i}/{len(songs)}] OK  {out_path}  ({msg})")
            ok += 1
            results.append(Result(f"{artists} - {name}", "ok", msg,
                                  str(out_path), dur))
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(songs)}] FAIL {artists} - {name}  {e}", file=sys.stderr)
            fail += 1
            results.append(Result(f"{artists} - {name}", "fail", str(e)))
        if ctx.stop:
            print(f"!! 凭据失效，停止本歌单剩余 {len(songs) - i} 首: {ctx.stop_reason}",
                  file=sys.stderr)
            break
    print(f"  小计: 成功 {ok}, 跳过 {skip}, 失败 {fail}")


def resolve_favorites_tid(creds: Credentials) -> int:
    for p in get_created_playlists(creds):
        if p.dirid == 201 or p.name == "我喜欢":
            return p.tid
    raise QmcError("未找到「我喜欢」歌单（dirId=201）")


# ----------------------------------------------------------------------------
# 交互式 CLI
# ----------------------------------------------------------------------------

MENU_BANNER = """
╔══════════════════════════════════════════════╗
║   QQ 音乐 解密 / 歌单下载 交互式工具          ║
║   仅限本人账号资源、个人备份用途              ║
╚══════════════════════════════════════════════╝"""

MENU_MAIN = """
  [1] 查看我的歌单（创建 + 收藏）
  [2] 下载「我喜欢」
  [3] 下载指定歌单
  [4] 本地加密文件批量解密
  [5] 本地文件预演 / 格式查看
  [6] 设置（音质 / 输出目录 / 上限 / 标签…）
  [7] 运行自测
  [8] 帮助
  [0] 退出"""

MENU_SETTINGS = """
  [1] 下载音质优先级  当前: {quality}
  [2] 输出目录        当前: {out_dir}
  [3] 每歌单/每批上限 当前: {limit}
  [4] 覆盖已有文件    当前: {overwrite}
  [5] 写入音频标签    当前: {tag}
  [6] API 调用间隔    当前: {delay}s
  [0] 返回"""


class InteractiveCli:
    def __init__(self, opts: argparse.Namespace):
        self.opts = opts
        self.ctx = BatchContext(opts)
        self._pl_cache: Optional[Tuple[List[PlaylistItem], List[PlaylistItem]]] = None

    # ---- 基础输入 ----
    @staticmethod
    def ask(prompt: str, default: Optional[str] = None) -> Optional[str]:
        try:
            s = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not s and default is not None:
            return str(default)
        return s

    @staticmethod
    def confirm(prompt: str) -> bool:
        ans = InteractiveCli.ask(prompt + " [y/N] ")
        return (ans or "").lower() in ("y", "yes")

    def pause(self):
        try:
            input("\n按回车返回菜单...")
        except (EOFError, KeyboardInterrupt):
            print()

    def _reset_stop(self):
        self.ctx.stop = False
        self.ctx.stop_reason = ""

    def close(self):
        self.ctx.close()

    def _creds(self) -> Optional[Credentials]:
        try:
            return self.ctx.get_creds()
        except QmcError as e:
            print(f"[错误] {e}")
            return None

    # ---- 歌单 ----
    def _all_playlists(self):
        creds = self._creds()
        if creds is None:
            return None
        if self._pl_cache is None:
            try:
                self._pl_cache = (get_created_playlists(creds),
                                  get_collected_playlists(creds))
            except QmcError as e:
                print(f"[错误] {e}")
                return None
        return self._pl_cache

    def show_playlists(self):
        pl = self._all_playlists()
        if pl is None:
            return
        created, collected = pl
        print(f"\n创建的歌单 ({len(created)}):")
        for p in created:
            print(f"  {p.tid:<12} dirId={p.dirid:<4} {p.name}  ({p.song_num} 首)")
        print(f"收藏的歌单 ({len(collected)}):")
        for p in collected:
            print(f"  {p.tid:<12} dirId={p.dirid:<4} {p.name}  ({p.song_num} 首)")

    def choose_playlist(self) -> Optional[Tuple[int, str, int]]:
        pl = self._all_playlists()
        if pl is None:
            return None
        created, collected = pl
        all_items = [(p, "创建") for p in created] + [(p, "收藏") for p in collected]
        if not all_items:
            print("没有可用的歌单。")
            return None
        print()
        for i, (p, kind) in enumerate(all_items, 1):
            print(f"  [{i}] [{kind}] {p.name}  (tid={p.tid}, {p.song_num} 首)")
        print("  [0] 返回")
        ans = self.ask("选择歌单序号: ")
        if not ans or ans == "0":
            return None
        try:
            idx = int(ans)
            if 1 <= idx <= len(all_items):
                p, _ = all_items[idx - 1]
                return p.tid, p.name, p.song_num
        except ValueError:
            pass
        print("[错误] 序号无效")
        return None

    # ---- 动作 ----
    def do_favorites(self):
        creds = self._creds()
        if creds is None:
            return
        self._reset_stop()
        try:
            tid = resolve_favorites_tid(creds)
        except QmcError as e:
            print(f"[错误] {e}")
            return
        total = self.opts.limit or None
        print(f"\n「我喜欢」tid={tid}" + (f"，本次上限 {total} 首" if total else "，全量下载"))
        if not self.opts.limit and not self.confirm("将下载全部歌曲，确认？"):
            print("已取消。")
            return
        try:
            download_playlist_songs(self.ctx, tid, "我喜欢", [])
        except QmcError as e:
            print(f"[错误] {e}")

    def do_playlist(self):
        choice = self.choose_playlist()
        if choice is None:
            return
        tid, name, song_num = choice
        self._reset_stop()
        print(f"\n歌单「{name}」({song_num} 首)" +
              (f"，本次上限 {self.opts.limit} 首" if self.opts.limit else ""))
        if not self.opts.limit and song_num > 20 and not self.confirm("歌单较大，确认下载？"):
            print("已取消。")
            return
        try:
            download_playlist_songs(self.ctx, tid, name, [])
        except QmcError as e:
            print(f"[错误] {e}")

    def _choose_local_source(self, for_info: bool):
        sources: List[Tuple[str, List[str], bool]] = []
        downloads = os.path.expanduser("~/Downloads")
        if os.path.isdir(downloads):
            sources.append((f"Downloads ({downloads})", [downloads], False))
        lib = default_library_dir()
        if lib and os.path.isdir(lib):
            label = "QQ 音乐 duty 缓存库（递归）" if IS_WINDOWS else "QQ 音乐 iMusic 缓存库（递归）"
            sources.append((label, [lib], True))
        print()
        for i, (label, _paths, _rec) in enumerate(sources, 1):
            print(f"  [{i}] {label}")
        print("  [c] 自定义目录/文件")
        print("  [0] 返回")
        ans = self.ask("选择来源: ")
        if ans in (None, "0"):
            return None
        if ans.lower() == "c":
            path = self.ask("输入目录或文件路径: ")
            if not path or not os.path.exists(path):
                print("[错误] 路径不存在")
                return None
            if os.path.isfile(path):
                return [path], False
            rec = self.confirm("递归扫描子目录？")
            return [path], rec
        try:
            idx = int(ans)
            if 1 <= idx <= len(sources):
                _label, paths, rec = sources[idx - 1]
                return paths, rec
        except ValueError:
            pass
        print("[错误] 选择无效")
        return None

    def do_local(self, dry_run: bool = False, info_only: bool = False):
        src = self._choose_local_source(for_info=info_only)
        if src is None:
            return
        paths, recursive = src
        files = discover(paths, recursive or self.opts.recursive)
        if not files:
            print("没有找到受支持的文件。")
            return
        print(f"\n发现 {len(files)} 个候选文件")
        if not dry_run and not info_only and len(files) > 10 and not self.confirm("确认处理？"):
            print("已取消。")
            return
        old_dry, old_info = self.opts.dry_run, self.opts.info
        self.opts.dry_run, self.opts.info = dry_run, info_only
        self._reset_stop()
        results: List[Result] = []
        try:
            for i, f in enumerate(files, 1):
                results.append(process_file(f, self.ctx, i, len(files)))
                if self.ctx.stop:
                    print(f"!! 凭据失效，停止后续: {self.ctx.stop_reason}", file=sys.stderr)
                    break
        finally:
            self.opts.dry_run, self.opts.info = old_dry, old_info
        ok = sum(1 for r in results if r.status == "ok")
        skip = sum(1 for r in results if r.status == "skip")
        fail = sum(1 for r in results if r.status == "fail")
        print(f"  本地处理完成: 成功 {ok}, 跳过 {skip}, 失败 {fail}, "
              f"API 调用 {self.ctx.api_calls} 次")

    def do_settings(self):
        while True:
            print(MENU_SETTINGS.format(
                quality=self.opts.quality,
                out_dir=self.opts.out_dir,
                limit=self.opts.limit or "全部",
                overwrite="是" if self.opts.overwrite else "否",
                tag="是" if self.opts.tag else "否",
                delay=self.opts.delay,
            ))
            ans = self.ask("选择: ")
            if ans in (None, "0"):
                return
            if ans == "1":
                cur = self.ask(f"音质优先级（逗号分隔）[{self.opts.quality}]: ",
                               self.opts.quality)
                try:
                    self.opts.quality = ",".join(parse_qualities(cur or ""))
                except QmcError as e:
                    print(f"[错误] {e}")
            elif ans == "2":
                cur = self.ask(f"输出目录 [{self.opts.out_dir}]: ",
                               self.opts.out_dir)
                if cur:
                    self.opts.out_dir = os.path.expanduser(cur)
            elif ans == "3":
                cur = self.ask(f"每歌单/每批上限（0=全部）[{self.opts.limit}]: ",
                               str(self.opts.limit))
                try:
                    self.opts.limit = max(0, int(cur or 0))
                except ValueError:
                    print("[错误] 需要数字")
            elif ans == "4":
                self.opts.overwrite = not self.opts.overwrite
                print(f"  覆盖已有文件: {'是' if self.opts.overwrite else '否'}")
            elif ans == "5":
                self.opts.tag = not self.opts.tag
                print(f"  写入音频标签: {'是' if self.opts.tag else '否'}")
            elif ans == "6":
                cur = self.ask(f"API 间隔秒数 [{self.opts.delay}]: ",
                               str(self.opts.delay))
                try:
                    self.opts.delay = max(0.0, float(cur or 0))
                except ValueError:
                    print("[错误] 需要数字")

    @staticmethod
    def show_help():
        print("""
帮助 / 使用说明
  本工具分两部分：
  1) 本地解密：musicex(.mgg/.mflac)、iMusic 缓存、QMC2 QTag/V1、QMC1 legacy。
  2) 在线下载：我的歌单、「我喜欢」、指定歌单（flac/320/192/128/m4a 等音质）。

命令行等价用法（可脚本化）：
  python3 qqmusic_decrypt.py --list-playlists
  python3 qqmusic_decrypt.py --favorites --limit 1
  python3 qqmusic_decrypt.py --playlist <tid> --quality flac
  python3 qqmusic_decrypt.py ~/Downloads --dry-run
  python3 qqmusic_decrypt.py --self-test

提示：
  - 歌单下载默认输出到 <输出目录>/<歌单名>/；
  - 重复运行自动 SKIP 已存在文件；
  - authst 过期会提示重新登录 QQ 音乐；
  - 技术原理见 TECHNICAL.md。
仅限本人账号资源、个人备份用途。""")


    def run(self):
        print(MENU_BANNER)
        while True:
            print(MENU_MAIN)
            ans = self.ask("\n选择操作: ")
            if ans is None or ans == "0":
                print("再见。")
                return
            try:
                if ans == "1":
                    self.show_playlists()
                elif ans == "2":
                    self.do_favorites()
                elif ans == "3":
                    self.do_playlist()
                elif ans == "4":
                    self.do_local(dry_run=False)
                elif ans == "5":
                    sub = self.ask("  [1] 预演(dry-run)  [2] 格式信息(info): ")
                    if sub == "1":
                        self.do_local(dry_run=True)
                    elif sub == "2":
                        self.do_local(info_only=True)
                elif ans == "6":
                    self.do_settings()
                elif ans == "7":
                    run_self_test()
                elif ans == "8":
                    self.show_help()
                else:
                    print("[错误] 无效选择，输入 0-8")
            except QmcError as e:
                print(f"[错误] {e}")
            self.pause()


def run_interactive(opts: argparse.Namespace) -> int:
    cli = InteractiveCli(opts)
    try:
        cli.run()
    finally:
        cli.close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    opts = build_arg_parser().parse_args(argv)
    global API_PLATFORM
    if opts.platform != "auto":
        API_PLATFORM = opts.platform
    if opts.self_test:
        run_self_test()
        return 0
    if opts.debug_tools:
        print("frozen:", getattr(sys, "frozen", None))
        print("_MEIPASS:", getattr(sys, "_MEIPASS", None))
        base = getattr(sys, "_MEIPASS", None)
        if base:
            try:
                print("MEIPASS top:", sorted(os.listdir(base))[:30])
                bdir = os.path.join(base, "bin")
                if os.path.isdir(bdir):
                    print("MEIPASS bin:", sorted(os.listdir(bdir))[:30])
                    for _f in ("ffmpeg.exe", "ffprobe.exe"):
                        _p = os.path.join(bdir, _f)
                        print(_f, "isfile=", os.path.isfile(_p),
                              "access=", os.access(_p, os.X_OK) if os.path.isfile(_p) else None)
            except Exception as e:  # noqa: BLE001
                print("list err", e)
        for name in ("ffmpeg", "ffprobe"):
            p = tool_path(name)
            print(f"{name}: {p}")
            if p:
                r = subprocess.run([p, "-version"], capture_output=True,
                                   text=True, timeout=30)
                print(f"  rc={r.returncode} out={r.stdout[:80]!r} "
                      f"err={r.stderr[:120]!r}")
        return 0

    # 无参数启动 → 交互式菜单；显式 -i/--interactive 同
    no_args = (argv is None and len(sys.argv) == 1) or (
        argv is not None and len(argv) == 0)
    if opts.interactive or no_args:
        return run_interactive(opts)

    remote_requested = bool(opts.list_playlists or opts.playlist or opts.favorites)
    ctx = BatchContext(opts)
    results: List[Result] = []
    try:
        # 1) 本地加密文件解密（显式 paths 才做；缺省目录扫描只在纯本地模式启用）
        files: List[str] = []
        if opts.paths:
            files = discover(list(opts.paths), opts.recursive)
        elif not remote_requested:
            inputs = [os.path.expanduser("~/Downloads")]
            lib = default_library_dir()
            if lib and os.path.isdir(lib):
                inputs.append(lib)
            files = discover(inputs, opts.recursive)
        if opts.limit > 0 and not remote_requested:
            files = files[: opts.limit]
        if files:
            print(f"发现 {len(files)} 个候选文件", file=sys.stderr)
            for i, f in enumerate(files, 1):
                results.append(process_file(f, ctx, i, len(files)))
                if ctx.stop:
                    print(f"!! 凭据失效，停止后续 {len(files) - i} 个文件: "
                          f"{ctx.stop_reason}", file=sys.stderr)
                    break

        # 2) 在线歌单 / 我喜欢
        if remote_requested:
            creds = ctx.get_creds()
            if opts.list_playlists:
                print_playlists(creds)
            tasks: List[Tuple[int, str]] = []
            if opts.favorites:
                tasks.append((resolve_favorites_tid(creds), "我喜欢"))
            for tid in opts.playlist:
                tasks.append((int(tid), f"歌单{tid}"))
            seen: set = set()
            ordered: List[Tuple[int, str]] = []
            for t in tasks:
                if t[0] not in seen:
                    seen.add(t[0])
                    ordered.append(t)
            for tid, label in ordered:
                download_playlist_songs(ctx, tid, label, results)
                if ctx.stop:
                    break
    finally:
        ctx.close()

    ok = [r for r in results if r.status == "ok"]
    fail = [r for r in results if r.status == "fail"]
    skip = [r for r in results if r.status == "skip"]
    print(f"\n完成: 成功 {len(ok)}, 跳过 {len(skip)}, 失败 {len(fail)}, "
          f"API 调用 {ctx.api_calls} 次")
    if fail:
        for r in fail:
            print(f"  FAIL {r.path}: {r.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
