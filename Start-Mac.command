#!/bin/bash
# qqmusic-decrypt macOS 启动器
# 作用：经 Terminal 运行主程序，避免 Finder 直启时 macOS TCC 拦截“可移动卷宗”访问
set -u
cd "$(dirname "$0")" || exit 1
xattr -dr com.apple.quarantine . 2>/dev/null

CANDIDATES=(
  "./qqmusic-decrypt-mac"
  "./dist/qqmusic-decrypt-mac"
  "./release/qqmusic-decrypt-macos-arm64/qqmusic-decrypt-mac"
)

BIN=""
for c in "${CANDIDATES[@]}"; do
  if [ -f "$c" ]; then BIN="$c"; break; fi
done

if [ -z "$BIN" ]; then
  echo "未找到 qqmusic-decrypt-mac 二进制。"
  echo "  - 若使用发布包：请解压 release/qqmusic-decrypt-macos-arm64.zip，并在解压出的目录里运行本启动器"
  echo "  - 若使用源码：先构建 (PyInstaller) 或直接运行 python3 qqmusic_decrypt.py"
  exit 1
fi

chmod +x "$BIN" 2>/dev/null
echo "使用: $BIN"
exec "$BIN" "$@"
