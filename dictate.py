#!/usr/bin/env python3
"""长按左 Alt 听写：按住录音，悬浮窗实时显示识别结果，松开后把文字输入到当前窗口。

识别用 SenseVoice（sherpa-onnx，本地 CPU 推理）。SenseVoice 本身是非流式模型，
但速度极快，这里按住期间每隔 PREVIEW_INTERVAL 秒对已录音频重新识别一次，
实现"边说边出字"；长段语音在静音处切段冻结，避免每次重算整段。
"""
import fcntl
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import sherpa_onnx
from pynput import keyboard
from PySide6.QtCore import QEasingCurve, QMimeData, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QWidget

HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE / "models" / "sense-voice"

# 没有 root 装不了 libportaudio2 时，install.sh 会把它解压到 .local/；系统有就不会走到这里
_LOCAL_PA = HERE / ".local/usr/lib/x86_64-linux-gnu/libportaudio.so.2"
if _LOCAL_PA.exists():
    import ctypes.util
    _find = ctypes.util.find_library
    ctypes.util.find_library = lambda n: _find(n) or (str(_LOCAL_PA) if n == "portaudio" else None)
import sounddevice as sd  # noqa: E402

SAMPLE_RATE = 16000
HOLD_THRESHOLD = 0.25    # 按住超过这么久才算听写，短按 Alt 照常给其他程序用
PREVIEW_INTERVAL = 0.35  # 实时预览刷新间隔（秒）
PREROLL = 0.3            # 按下前保留的音频，避免吞掉第一个字
SEGMENT_MIN = 12.0       # 当前段超过这么长，就在静音处切段
SEGMENT_MAX = 25.0       # 实在找不到静音，到这么长强制切段
NUM_THREADS = 4
PUNCT = " .,?!。，、？！；：…"
# 指定麦克风：PulseAudio 录音源名称里包含的字符串（pactl list sources short 查看）。
# 不设置时自动守护第一个 USB 麦克风。
MIC_MATCH = os.environ.get("DICTATE_MIC", "")
MIC_CHECK_INTERVAL = 2.0  # 每隔多久检查一次麦克风连接（秒）


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, file=sys.stderr, flush=True)


class Recognizer:
    def __init__(self):
        self.r = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(MODEL_DIR / "model.int8.onnx"),
            tokens=str(MODEL_DIR / "tokens.txt"),
            num_threads=NUM_THREADS,
            language="zh",  # 固定中文；auto 遇到噪声容易输出韩文/日文，中英混说不受影响
            use_itn=True,  # 输出标点和阿拉伯数字
        )
        self.lock = threading.Lock()

    def __call__(self, audio):
        if len(audio) < SAMPLE_RATE * 0.2:
            return ""
        with self.lock:
            s = self.r.create_stream()
            s.accept_waveform(SAMPLE_RATE, audio)
            self.r.decode_stream(s)
        text = s.result.text.strip()
        # 噪声或没说话时只会出个标点，丢掉
        return text if text.strip(PUNCT) else ""


def find_quiet_cut(audio):
    """在音频后半段找能量最低的 0.2 秒窗口，返回切点；找不到合适静音返回 None。"""
    win = int(SAMPLE_RATE * 0.2)
    n = len(audio) // win
    if n < 4:
        return None
    frames = audio[: n * win].reshape(n, win)
    energy = np.sqrt((frames ** 2).mean(axis=1))
    lo = n // 2
    i = lo + int(np.argmin(energy[lo:-1]))
    if energy[i] > 0.25 * np.median(energy):
        return None
    return i * win + win // 2


def pactl(*args):
    """运行 pactl，成功返回标准输出，失败返回 None。固定 C locale 方便解析。"""
    try:
        r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5,
                           env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _blocks(text, header):
    """按 'Card #3' 这类标题把 pactl list 的输出切块，返回 [(序号, 块文本)]。"""
    parts = re.split(rf"^{header} #(\d+)$", text, flags=re.M)
    return [(int(parts[i]), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


PROFILE_RE = re.compile(r"^\t\t(\S+): .*\(sinks: \d+, sources: (\d+), "
                        r"priority: (\d+), available: (\w+)\)$", re.M)


def parse_cards(text):
    cards = []
    for _, body in _blocks(text, "Card"):
        name = re.search(r"^\tName: (.+)$", body, re.M)
        active = re.search(r"^\tActive Profile: (.+)$", body, re.M)
        if not name:
            continue
        profiles = [dict(name=m[1], sources=int(m[2]), priority=int(m[3]), available=m[4] == "yes")
                    for m in PROFILE_RE.finditer(body)]
        cards.append(dict(name=name[1], active=active[1] if active else None, profiles=profiles))
    return cards


def input_profile_to_restore(card):
    """声卡当前配置没有录音源时（初始化失败退成 off 等），返回应切换到的录音配置。"""
    by_name = {p["name"]: p for p in card["profiles"]}
    current = by_name.get(card["active"])
    if current and current["sources"] > 0:
        return None
    usable = [p for p in card["profiles"] if p["sources"] > 0 and p["available"]]
    return max(usable, key=lambda p: p["priority"])["name"] if usable else None


def parse_sources(text):
    """pactl list sources short -> {名称: 序号}"""
    out = {}
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and cols[0].isdigit():
            out[cols[1]] = int(cols[0])
    return out


def parse_source_outputs(text):
    outs = []
    for index, body in _blocks(text, "Source Output"):
        src = re.search(r"^\tSource: (\d+)$", body, re.M)
        pid = re.search(r'^\t\tapplication\.process\.id = "(\d+)"$', body, re.M)
        if src:
            outs.append(dict(index=index, source=int(src[1]), pid=int(pid[1]) if pid else None))
    return outs


class MicKeeper:
    """守护麦克风连接。

    显示器自带的摄像头麦克风会随显示器断电消失。PulseAudio 会把录音流挪到别的源
    （常常是扬声器回环 *.monitor），麦克风回来后也不一定挪回去；设备刚上电时
    PulseAudio 还可能初始化失败，把声卡留在没有录音源的配置上。这里定期检查：
    把声卡切回录音配置，把本进程的录音流挪回目标麦克风，麦克风不在时标记 ok=False。
    """

    def __init__(self, match="", pid=None, run=pactl, log=print):
        self.explicit = bool(match)
        self.match = match or "usb-"
        self.pid = pid if pid is not None else os.getpid()
        self.run = run
        self.log = log
        self.armed = self.explicit  # 自动模式下，见过目标麦克风后才开始守护
        self.ok = True
        self.target = None

    def _is_mic(self, name):
        return self.match in name and not name.endswith(".monitor")

    def check(self):
        for card in parse_cards(self.run("list", "cards") or ""):
            if self.match not in card["name"]:
                continue
            profile = input_profile_to_restore(card)
            if profile and self.run("set-card-profile", card["name"], profile) is not None:
                self.log(f"声卡 {card['name']} 配置 {card['active']} -> {profile}")

        sources = parse_sources(self.run("list", "sources", "short") or "")
        mics = sorted(n for n in sources if self._is_mic(n))
        if not mics:
            self._set_ok(not self.armed, None)
            return
        self.armed = True
        target = self.target if self.target in mics else mics[0]
        outs = [o for o in parse_source_outputs(self.run("list", "source-outputs") or "")
                if o["pid"] == self.pid]
        ok = True
        for o in outs:
            if o["source"] == sources[target]:
                continue
            if self.run("move-source-output", str(o["index"]), target) is not None:
                self.log(f"录音流切回 {target}")
            else:
                ok = False
        self._set_ok(ok, target)

    def _set_ok(self, ok, target):
        if ok and (not self.ok or target != self.target) and target:
            self.log(f"麦克风可用：{target}")
        elif not ok and self.ok:
            self.log("麦克风断开，等待重新连接")
        self.ok, self.target = ok, target


class Dictation:
    def __init__(self, ui_queue):
        self.ui = ui_queue
        self.rec = Recognizer()
        self.lock = threading.Lock()
        self.recording = False
        self.chunks = []        # 当前录音的音频块
        self.preroll = []       # 未录音时的环形缓冲
        self.level = 0.0        # 当前音量 0~1，给悬浮窗的波形用
        self.mic = MicKeeper(MIC_MATCH, log=log) if shutil.which("pactl") else None
        if self.mic is None:
            log("没有 pactl，不守护麦克风连接")
        self.stream = None
        self._open_stream()
        if self.mic:
            threading.Thread(target=self._watch_mic, daemon=True).start()

    @property
    def mic_ok(self):
        return self.mic is None or self.mic.ok

    def _open_stream(self):
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=int(SAMPLE_RATE * 0.05), callback=self._on_audio)
        self.stream.start()

    def _watch_mic(self):
        while True:
            try:
                self.mic.check()
                if not self.stream.active:  # 录音流本身出错停了，重新打开
                    log("录音流已停止，重新打开")
                    self.stream.close()
                    self._open_stream()
            except Exception as e:
                log(f"检查麦克风出错：{e!r}")
            time.sleep(MIC_CHECK_INTERVAL)

    def _on_audio(self, data, frames, t, status):
        block = data[:, 0].copy()
        if not self.mic_ok:  # 麦克风不在时录到的是别的源（如扬声器回环），丢弃
            block[:] = 0
        db = 20 * np.log10(np.sqrt((block ** 2).mean()) + 1e-9)
        self.level = float(np.clip((db + 50) / 35, 0, 1))
        with self.lock:
            if self.recording:
                self.chunks.append(block)
            else:
                self.preroll.append(block)
                keep = int(PREROLL / 0.05)
                if len(self.preroll) > keep:
                    del self.preroll[:-keep]

    def start(self):
        # 每次录音一个独立会话，快速连按时前后两次互不干扰
        sess = SimpleNamespace(stop=threading.Event(), frozen_text="", frozen_len=0)
        sess.thread = threading.Thread(target=self._preview_loop, args=(sess,), daemon=True)
        with self.lock:
            if self.recording:  # 上一次没收尾就又开始：先结束它，否则它的预览线程会一直跑
                self.sess.stop.set()
            self.recording = True
            self.chunks = list(self.preroll)
            self.preroll = []
            self.sess = sess
            self.ui.put(("show", ""))
        sess.thread.start()

    def _take_audio(self):
        with self.lock:
            return np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)

    def _preview_loop(self, sess):
        # frozen_*：已在静音处切段、不再重算的部分
        while not sess.stop.wait(PREVIEW_INTERVAL):
            tail = self._take_audio()[sess.frozen_len:]
            if len(tail) > SAMPLE_RATE * SEGMENT_MIN:
                cut = find_quiet_cut(tail)
                if cut is None and len(tail) > SAMPLE_RATE * SEGMENT_MAX:
                    cut = len(tail) - SAMPLE_RATE  # 强制切段，留最后 1 秒继续
                if cut is not None:
                    sess.frozen_text += self.rec(tail[:cut])
                    sess.frozen_len += cut
                    tail = tail[cut:]
            if sess.stop.is_set():
                return
            text = sess.frozen_text + self.rec(tail)
            # 和 stop() 在同一把锁里判断，保证 "hide" 之后不会再冒出 "show" 把悬浮窗重新打开
            with self.lock:
                if not sess.stop.is_set():
                    self.ui.put(("show", text))

    def stop(self, commit=True):
        with self.lock:
            if not self.recording:
                return
            self.recording = False
            audio = np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)
            sess = self.sess
            sess.stop.set()
            self.ui.put(("hide", None))
        if commit:
            threading.Thread(target=self._finalize, args=(sess, audio), daemon=True).start()

    def _finalize(self, sess, audio):
        sess.thread.join()  # 等预览线程退出，复用它已冻结的分段
        t0 = time.time()
        text = sess.frozen_text + self.rec(audio[sess.frozen_len:])
        # 默认不把识别内容写进日志；排查问题时设 DICTATE_DEBUG=1
        shown = text if os.environ.get("DICTATE_DEBUG") else f"{len(text)} 字"
        log(f"{len(audio)/SAMPLE_RATE:.1f}s 音频, 识别 {time.time()-t0:.2f}s: {shown}")
        if text:
            self.ui.put(("paste", text))


# 这些窗口里粘贴快捷键是 Ctrl+Shift+V（VS Code 内置终端认 Ctrl+V，不在此列）
TERMINAL_CLASSES = ("terminal", "terminator", "tilix", "konsole", "alacritty",
                    "kitty", "xterm", "urxvt", "wezterm")


def paste_keys():
    wid = subprocess.run(["xdotool", "getactivewindow"],
                         capture_output=True, text=True).stdout.strip()
    if not wid:
        return "ctrl+v"
    cls = subprocess.run(["xprop", "-id", wid, "WM_CLASS"],
                         capture_output=True, text=True).stdout.lower()
    return "ctrl+shift+v" if any(c in cls for c in TERMINAL_CLASSES) else "ctrl+v"


class HotkeyListener:
    """长按左 Alt 触发；按住期间若按了别的键（Alt+Tab 等），视为普通快捷键，不录音。"""

    def __init__(self, dictation):
        self.d = dictation
        self.alt_down = False
        self.active = False
        self.combo = False
        self.timer = None
        self.lock = threading.RLock()
        self.generation = 0

    def on_press(self, key):
        with self.lock:
            self._on_press(key)

    def _on_press(self, key):
        if self._is_injected(key):
            return
        if key == keyboard.Key.alt_l:
            if self.alt_down:   # 自动重复
                return
            self.alt_down = True
            self.combo = False
            self.generation += 1
            self.timer = threading.Timer(HOLD_THRESHOLD, self._activate,
                                         args=(self.generation,))
            self.timer.start()
        elif self.alt_down:
            self.combo = True
            if self.timer:
                self.timer.cancel()
            if self.active:     # 录音中按了别的键：取消本次听写
                self.active = False
                self.d.stop(commit=False)

    @staticmethod
    def _is_injected(key):
        # _break_alt_tap 注入的 F20 没有键位映射，xdotool 临时借键码发出，pynput 看到的是 vk=0
        return isinstance(key, keyboard.KeyCode) and not key.vk and not key.char

    def _activate(self, generation):
        with self.lock:
            if generation != self.generation or not self.alt_down or self.combo:
                return
        self._break_alt_tap()
        # 注入期间可能已经松键；重新校验，并把启动与停止串行化。
        with self.lock:
            if generation != self.generation or not self.alt_down or self.combo:
                return
            self.d.start()
            self.active = True

    def _break_alt_tap(self):
        # 很多程序（Firefox、GTK 菜单等）在"单独按下再松开 Alt"时会激活菜单栏。
        # 按住期间注入一个无用按键，让它们认为这是组合键，松开 Alt 时就不弹菜单。
        subprocess.run(["xdotool", "key", "F20"], timeout=1)

    def on_release(self, key):
        with self.lock:
            self._on_release(key)

    def _on_release(self, key):
        if key == keyboard.Key.alt_l:
            self.alt_down = False
            self.generation += 1
            if self.timer:
                self.timer.cancel()
            if self.active:
                self.active = False
                self.d.stop(commit=True)


FONT_FAMILY = "Noto Sans CJK SC"
TEXT_MAX_WIDTH = 560     # 文字区最大宽度，超出换行
TEXT_MAX_LINES = 2       # 最多显示几行，更早的内容从前面截掉


class Waveform(QWidget):
    """随麦克风音量跳动的竖条。"""
    BARS = 5
    SHAPE = (0.45, 0.8, 1.0, 0.8, 0.45)

    def __init__(self, level_fn):
        super().__init__()
        self.level_fn = level_fn
        self.heights = [0.0] * self.BARS
        self.phase = 0.0
        self.setFixedSize(30, 26)
        self.timer = QTimer(self, interval=33, timeout=self._tick)

    def _tick(self):
        self.phase += 0.35
        lv = self.level_fn()
        for i in range(self.BARS):
            wobble = 0.75 + 0.25 * np.sin(self.phase + i * 1.3)
            target = lv * self.SHAPE[i] * wobble
            # 上升快、回落慢，看起来更自然
            k = 0.6 if target > self.heights[i] else 0.18
            self.heights[i] += (target - self.heights[i]) * k
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#7aa2ff"))
        bw, gap = 3.0, 3.0
        x0 = (self.width() - (self.BARS * bw + (self.BARS - 1) * gap)) / 2
        for i, h in enumerate(self.heights):
            bh = 4 + h * (self.height() - 4)
            p.drawRoundedRect(QRectF(x0 + i * (bw + gap), (self.height() - bh) / 2, bw, bh), 1.5, 1.5)


class Panel(QWidget):
    """深色半透明圆角胶囊背景。"""

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor(255, 255, 255, 28))
        p.setBrush(QColor(28, 29, 33, 235))
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawRoundedRect(r, 22, 22)


class Overlay:
    MARGIN = 24  # 给阴影留的透明边距

    def __init__(self, ui_queue, level_fn, mic_ok_fn=lambda: True):
        self.q = ui_queue
        self.mic_ok_fn = mic_ok_fn
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.win = QWidget(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                           | Qt.WindowDoesNotAcceptFocus | Qt.X11BypassWindowManagerHint)
        self.win.setAttribute(Qt.WA_TranslucentBackground)
        self.win.setAttribute(Qt.WA_ShowWithoutActivating)

        panel = Panel()
        shadow = QGraphicsDropShadowEffect(blurRadius=28, offset=0)
        shadow.setYOffset(6)
        shadow.setColor(QColor(0, 0, 0, 110))
        panel.setGraphicsEffect(shadow)

        self.wave = Waveform(level_fn)
        self.font = QFont(FONT_FAMILY, 13)
        self.label = QLabel()
        self.label.setFont(self.font)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.PlainText)
        self.fm = QFontMetrics(self.font)

        row = QHBoxLayout(panel)
        row.setContentsMargins(16, 11, 22, 11)
        row.setSpacing(12)
        row.addWidget(self.wave, 0, Qt.AlignVCenter)
        row.addWidget(self.label, 1, Qt.AlignVCenter)
        outer = QHBoxLayout(self.win)
        outer.setContentsMargins(self.MARGIN, self.MARGIN, self.MARGIN, self.MARGIN)
        outer.addWidget(panel)

        self.fade = QPropertyAnimation(self.win, b"windowOpacity", duration=140)
        self.fade.setEasingCurve(QEasingCurve.OutCubic)
        self.fade.finished.connect(lambda: self.win.windowOpacity() == 0 and self.win.hide())
        self.timer = QTimer(interval=30, timeout=self._poll)
        self.timer.start()

    def _set_text(self, text):
        if not text:
            hint = "正在聆听…" if self.mic_ok_fn() else "麦克风未连接，等待重新连接…"
            self.label.setStyleSheet("color: rgba(255,255,255,0.55);")
            self.label.setText(hint)
            self.label.setFixedWidth(self.fm.horizontalAdvance(hint) + 2)
            return
        self.label.setStyleSheet("color: #f2f3f5;")
        # 超过 TEXT_MAX_LINES 行就从前面截掉，保留最新说的话
        flags = Qt.TextWordWrap
        def lines(t):
            return self.fm.boundingRect(0, 0, TEXT_MAX_WIDTH, 10000, flags, t).height() // self.fm.lineSpacing()
        if lines(text) > TEXT_MAX_LINES:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi) // 2
                if lines("…" + text[mid:]) > TEXT_MAX_LINES:
                    lo = mid + 1
                else:
                    hi = mid
            text = "…" + text[lo:]
        w = min(self.fm.horizontalAdvance(text) + 2, TEXT_MAX_WIDTH)
        self.label.setFixedWidth(w)
        self.label.setText(text)

    def _show(self, text):
        self._set_text(text)
        self.win.adjustSize()
        self._place()
        if not self.win.isVisible() or self.fade.endValue() == 0:
            self.wave.timer.start()
            self.fade.stop()
            if not self.win.isVisible():
                self.win.setWindowOpacity(0)
                self.win.show()
            self.fade.setStartValue(self.win.windowOpacity())
            self.fade.setEndValue(1.0)
            self.fade.start()

    def _hide(self):
        if not self.win.isVisible():
            return
        self.wave.timer.stop()
        self.fade.stop()
        self.fade.setStartValue(self.win.windowOpacity())
        self.fade.setEndValue(0.0)
        self.fade.start()

    def _place(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        g = screen.availableGeometry()
        size = self.win.sizeHint()
        self.win.resize(size)
        self.win.move(g.x() + (g.width() - size.width()) // 2,
                      g.y() + g.height() - size.height() - 70)

    def _poll(self):
        try:
            while True:
                cmd, arg = self.q.get_nowait()
                if cmd == "show":
                    self._show(arg)
                elif cmd == "hide":
                    self._hide()
                elif cmd == "paste":
                    self._paste(arg)
        except queue.Empty:
            pass

    def _paste(self, text):
        # xdotool type 输入中文会丢字乱序，改走剪贴板粘贴，完事后恢复原剪贴板
        cb = QGuiApplication.clipboard()
        old = cb.mimeData()
        old = {f: old.data(f) for f in old.formats()} if old else {}
        cb.setText(text)
        self.app.processEvents()
        subprocess.run(["xdotool", "key", "--clearmodifiers", paste_keys()])

        def restore():
            if not old:
                return
            m = QMimeData()
            for f, data in old.items():
                m.setData(f, data)
            cb.setMimeData(m)
        QTimer.singleShot(500, restore)

    def run(self):
        self.app.exec()


def main():
    # 单实例：已有一个在运行就直接退出
    lock = open(HERE / ".dictate.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("已经在运行，退出")
        return
    if not (MODEL_DIR / "model.int8.onnx").exists():
        sys.exit(f"找不到模型，请先运行 {HERE / 'download_model.sh'}")
    ui_queue = queue.Queue()
    log("加载模型…")
    d = Dictation(ui_queue)
    overlay = Overlay(ui_queue, lambda: d.level, lambda: d.mic_ok)
    d.rec(np.zeros(SAMPLE_RATE, np.float32))  # 预热
    hk = HotkeyListener(d)
    listener = keyboard.Listener(on_press=hk.on_press, on_release=hk.on_release)
    listener.daemon = True
    listener.start()
    log("就绪：长按左 Alt 说话，松开输入")
    overlay.run()


if __name__ == "__main__":
    main()
