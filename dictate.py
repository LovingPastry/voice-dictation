#!/usr/bin/env python3
"""长按左 Alt 听写：按住录音，悬浮窗实时显示识别结果，松开后把文字输入到当前窗口。

识别用 SenseVoice（sherpa-onnx，本地 CPU 推理）。SenseVoice 本身是非流式模型，
但速度极快，这里按住期间每隔 PREVIEW_INTERVAL 秒对已录音频重新识别一次，
实现"边说边出字"；长段语音在静音处切段冻结，避免每次重算整段。
"""
import fcntl
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import sherpa_onnx
import sounddevice as sd
from pynput import keyboard
from PySide6.QtCore import QEasingCurve, QMimeData, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QWidget

HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE / "models" / "sense-voice"

SAMPLE_RATE = 16000
HOLD_THRESHOLD = 0.25    # 按住超过这么久才算听写，短按 Alt 照常给其他程序用
PREVIEW_INTERVAL = 0.35  # 实时预览刷新间隔（秒）
PREROLL = 0.3            # 按下前保留的音频，避免吞掉第一个字
SEGMENT_MIN = 12.0       # 当前段超过这么长，就在静音处切段
SEGMENT_MAX = 25.0       # 实在找不到静音，到这么长强制切段
NUM_THREADS = 4
PUNCT = " .,?!。，、？！；：…"


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


class Dictation:
    def __init__(self, ui_queue):
        self.ui = ui_queue
        self.rec = Recognizer()
        self.lock = threading.Lock()
        self.recording = False
        self.chunks = []        # 当前录音的音频块
        self.preroll = []       # 未录音时的环形缓冲
        self.level = 0.0        # 当前音量 0~1，给悬浮窗的波形用
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=int(SAMPLE_RATE * 0.05), callback=self._on_audio)
        self.stream.start()

    def _on_audio(self, data, frames, t, status):
        block = data[:, 0].copy()
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
        with self.lock:
            self.recording = True
            self.chunks = list(self.preroll)
            self.preroll = []
        # 每次录音一个独立会话，快速连按时前后两次互不干扰
        sess = SimpleNamespace(stop=threading.Event(), frozen_text="", frozen_len=0)
        sess.thread = threading.Thread(target=self._preview_loop, args=(sess,), daemon=True)
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

    def on_press(self, key):
        if self._is_injected(key):
            return
        if key == keyboard.Key.alt_l:
            if self.alt_down:   # 自动重复
                return
            self.alt_down = True
            self.combo = False
            self.timer = threading.Timer(HOLD_THRESHOLD, self._activate)
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

    def _activate(self):
        if self.alt_down and not self.combo:
            self.active = True
            self._break_alt_tap()
            self.d.start()

    def _break_alt_tap(self):
        # 很多程序（Firefox、GTK 菜单等）在"单独按下再松开 Alt"时会激活菜单栏。
        # 按住期间注入一个无用按键，让它们认为这是组合键，松开 Alt 时就不弹菜单。
        subprocess.run(["xdotool", "key", "F20"], timeout=1)

    def on_release(self, key):
        if key == keyboard.Key.alt_l:
            self.alt_down = False
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

    def __init__(self, ui_queue, level_fn):
        self.q = ui_queue
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
            self.label.setStyleSheet("color: rgba(255,255,255,0.55);")
            self.label.setText("正在聆听…")
            self.label.setFixedWidth(self.fm.horizontalAdvance("正在聆听…") + 2)
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
    overlay = Overlay(ui_queue, lambda: d.level)
    d.rec(np.zeros(SAMPLE_RATE, np.float32))  # 预热
    hk = HotkeyListener(d)
    listener = keyboard.Listener(on_press=hk.on_press, on_release=hk.on_release)
    listener.daemon = True
    listener.start()
    log("就绪：长按左 Alt 说话，松开输入")
    overlay.run()


if __name__ == "__main__":
    main()
