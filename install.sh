#!/bin/bash
# 一键安装：系统依赖 -> Python 环境(.venv) -> 模型 -> 开机自启
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [ "${XDG_SESSION_TYPE:-x11}" = "wayland" ]; then
    echo "警告：当前是 Wayland 会话。本程序依赖 X11（全局按键监听和 xdotool），请在登录界面选择 Xorg 会话。"
fi

# 1. 系统依赖
PKGS="xdotool x11-utils libportaudio2 libxcb-cursor0 fonts-noto-cjk"
MISSING=""
for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
if [ -n "$MISSING" ]; then
    echo "安装系统依赖:$MISSING"
    sudo apt-get update && sudo apt-get install -y $MISSING
fi

# 2. Python 环境（需要 3.9+；系统 Python 太旧时退回用 conda）
if [ ! -x .venv/bin/python ]; then
    PY=""
    for c in python3.12 python3.11 python3.10 python3.9 python3; do
        if command -v $c >/dev/null && $c -c 'import sys,venv,ensurepip; sys.exit(sys.version_info < (3,9))' 2>/dev/null; then
            PY=$c; break
        fi
    done
    if [ -n "$PY" ]; then
        echo "用 $($PY --version) 创建 .venv"
        $PY -m venv .venv
    elif command -v conda >/dev/null; then
        echo "系统 Python 低于 3.9，用 conda 创建环境到 .venv"
        conda create -y -q -p "$DIR/.venv" python=3.11
    else
        echo "需要 Python 3.9+（带 venv）或 conda。Ubuntu 20.04 可用: sudo apt install python3.9 python3.9-venv" >&2
        exit 1
    fi
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

# 3. 模型
./download_model.sh

# 4. 开机自启（GNOME / KDE / XFCE 等遵循 XDG autostart 的桌面）
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/voice-dictation.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Voice Dictation
Comment=长按左 Alt 语音听写
Exec=$DIR/run.sh
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=5
DESK
chmod +x run.sh download_model.sh uninstall.sh

# 5. 立即启动
setsid ./run.sh >/dev/null 2>&1 < /dev/null &
echo
echo "安装完成，已在后台启动。长按左 Alt 说话，松开输入。日志: $DIR/dictation.log"
