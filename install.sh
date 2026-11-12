#!/bin/bash
# 一键安装：系统依赖 -> Python 环境(.venv) -> 模型 -> 开机自启
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [ "${XDG_SESSION_TYPE:-x11}" = "wayland" ]; then
    echo "警告：当前是 Wayland 会话。本程序依赖 X11（全局按键监听和 xdotool），安装可以继续，"
    echo "      但必须注销后在登录界面右下角齿轮选择 \"Ubuntu on Xorg\" 才能使用。"
fi

# 1. 系统依赖（没有 sudo 时，把 xdotool/portaudio/libxcb-cursor 解压到项目内 .local/ 作为替代）
PKGS="xdotool x11-utils libportaudio2 libxcb-cursor0 fonts-noto-cjk python3-venv"
MISSING=""
for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
if [ -n "$MISSING" ]; then
    echo "安装系统依赖:$MISSING"
    if ! { sudo apt-get update && sudo apt-get install -y $MISSING; }; then
        echo "sudo 不可用，改为解压到 $DIR/.local（python3-venv 无法这样装，下面会用 pip 引导）"
        mkdir -p .debs
        (cd .debs && apt-get download xdotool libxdo3 libportaudio2 libxcb-cursor0 libjack-jackd2-0) &&
            for f in .debs/*.deb; do dpkg -x "$f" .local; done
    fi
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
        $PY -m venv .venv || { rm -rf .venv; $PY -m venv --without-pip .venv; }
    elif command -v conda >/dev/null; then
        echo "系统 Python 低于 3.9，用 conda 创建环境到 .venv"
        conda create -y -q -p "$DIR/.venv" python=3.11
    else
        echo "需要 Python 3.9+（带 venv）或 conda。Ubuntu 20.04 可用: sudo apt install python3.9 python3.9-venv" >&2
        exit 1
    fi
fi
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then  # 缺 python3-venv 时 venv 里没有 pip
    echo "引导 pip"
    curl -fsSL --max-time 60 -o /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py \
        && .venv/bin/python /tmp/get-pip.py -q
fi
.venv/bin/python -m pip install -q --upgrade pip
# pynput 在 Linux 上依赖 evdev，需要编译（要 python3-dev）；X11 后端用不到它，装不上就 --no-deps
if ! .venv/bin/python -m pip install -q -r requirements.txt; then
    echo "完整安装失败，跳过 evdev 重试"
    grep -v '^pynput' requirements.txt | .venv/bin/python -m pip install -q -r /dev/stdin python-xlib six
    .venv/bin/python -m pip install -q --no-deps pynput
fi

# 3. 模型
./download_model.sh

# 4. 开机自启：systemd 用户服务，随图形会话启动，崩溃自动重启
chmod +x run.sh download_model.sh uninstall.sh
rm -f ~/.config/autostart/voice-dictation.desktop  # 旧版的 XDG autostart，留着会和服务各起一份
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/voice-dictation.service <<UNIT
[Unit]
Description=Voice Dictation (长按左 Alt 语音听写)
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=$DIR/run.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
UNIT
systemctl --user daemon-reload
systemctl --user enable voice-dictation.service

# 5. 立即启动
systemctl --user restart voice-dictation.service
echo
echo "安装完成，已在后台启动。长按左 Alt 说话，松开输入。日志: $DIR/dictation.log"
