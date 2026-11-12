#!/bin/sh
# 启动听写程序；已在运行时自动退出（dictate.py 内有单实例锁）。日志写到同目录 dictation.log
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
# 无 root 时 install.sh 把 xdotool 等解压在 .local/，存在才加入搜索路径
if [ -d "$DIR/.local/usr" ]; then
    PATH="$DIR/.local/usr/bin:$PATH"
    LD_LIBRARY_PATH="$DIR/.local/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export PATH LD_LIBRARY_PATH
fi
# Qt 在 Wayland 会话里默认走 wayland 后端，悬浮窗无法置顶定位；固定用 xcb
export QT_QPA_PLATFORM=xcb
exec "$DIR/.venv/bin/python" -I "$DIR/dictate.py" >> "$DIR/dictation.log" 2>&1
