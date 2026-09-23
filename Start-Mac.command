#!/bin/bash
# qqmusic-decrypt macOS 启动器
# 作用：经 Terminal 运行主程序，避免 Finder 直启时 macOS TCC 拦截“可移动卷宗”访问
cd "$(dirname "$0")" || exit 1
xattr -dr com.apple.quarantine . 2>/dev/null
chmod +x ./qqmusic-decrypt-mac 2>/dev/null
exec ./qqmusic-decrypt-mac "$@"
