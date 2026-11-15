"""无需麦克风或桌面，确定性验证 Alt 计时器竞态。"""
import ast
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest

source = ast.parse((Path(__file__).resolve().parents[1] / 'dictate.py').read_text())
node = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'HotkeyListener')

class Timer:
    def __init__(self, interval, callback, args=()):
        self.callback, self.args = callback, args
    def start(self): pass
    def cancel(self): pass
    def fire(self): self.callback(*self.args)

class KeyCode: pass
ALT = 'alt'
namespace = dict(threading=SimpleNamespace(RLock=threading.RLock, Timer=Timer),
                 keyboard=SimpleNamespace(Key=SimpleNamespace(alt_l=ALT), KeyCode=KeyCode),
                 HOLD_THRESHOLD=.25)
exec(compile(ast.Module(body=[node], type_ignores=[]), 'dictate.py', 'exec'), namespace)

class HotkeyTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        d = SimpleNamespace(start=lambda: self.events.append('start'),
                            stop=lambda commit: self.events.append(('stop', commit)))
        self.hk = namespace['HotkeyListener'](d)
        self.hk._break_alt_tap = lambda: None

    def test_short_tap_even_if_cancelled_timer_runs(self):
        self.hk.on_press(ALT)
        timer = self.hk.timer
        self.hk.on_release(ALT)
        timer.fire()
        self.assertEqual(self.events, [])

    def test_release_during_injection(self):
        self.hk.on_press(ALT)
        self.hk._break_alt_tap = lambda: self.hk.on_release(ALT)
        self.hk.timer.fire()
        self.assertEqual(self.events, [])
        self.assertFalse(self.hk.active)

    def test_old_timer_during_next_press(self):
        self.hk.on_press(ALT)
        old = self.hk.timer
        self.hk.on_release(ALT)
        self.hk.on_press(ALT)
        old.fire()
        self.assertEqual(self.events, [])
        self.hk.timer.fire()
        self.hk.on_release(ALT)
        self.assertEqual(self.events, ['start', ('stop', True)])

    def test_combo_cancels(self):
        self.hk.on_press(ALT)
        self.hk.timer.fire()
        self.hk.on_press('tab')
        self.hk.on_release(ALT)
        self.assertEqual(self.events, ['start', ('stop', False)])

    def test_combo_before_activation(self):
        self.hk.on_press(ALT)
        self.hk.on_press('tab')
        self.hk.timer.fire()
        self.hk.on_release(ALT)
        self.assertEqual(self.events, [])

if __name__ == '__main__':
    unittest.main()
