#!/bin/sh
# 启动听写程序；已在运行时自动退出（dictate.py 内有单实例锁）。日志写到同目录 dictation.log
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
exec "$DIR/.venv/bin/python" -I "$DIR/dictate.py" >> "$DIR/dictation.log" 2>&1
