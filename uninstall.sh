#!/bin/sh
# 停止程序并移除开机自启；项目目录（含模型和 .venv）需手动删除
DIR="$(cd "$(dirname "$0")" && pwd)"
pkill -f "$DIR/dictate.py" 2>/dev/null || true
rm -f ~/.config/autostart/voice-dictation.desktop
echo "已停止并移除自启。删除全部文件: rm -rf \"$DIR\""
