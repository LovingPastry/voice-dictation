"""无需麦克风或桌面，验证上一次录音没收尾时再次开始会结束旧的预览线程。"""
import ast
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest

source = ast.parse((Path(__file__).resolve().parents[1] / 'dictate.py').read_text())
node = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'Dictation')

class Thread:
    def __init__(self, target, args=(), daemon=None): pass
    def start(self): pass

namespace = dict(threading=SimpleNamespace(Event=threading.Event, Thread=Thread),
                 SimpleNamespace=SimpleNamespace)
exec(compile(ast.Module(body=[node], type_ignores=[]), 'dictate.py', 'exec'), namespace)

class SessionTests(unittest.TestCase):
    def test_start_twice_stops_previous_session(self):
        d = object.__new__(namespace['Dictation'])
        d.lock, d.recording, d.preroll, d.ui = threading.Lock(), False, [], SimpleNamespace(put=lambda m: None)
        d.start()
        first = d.sess
        d.start()
        self.assertTrue(first.stop.is_set())
        self.assertFalse(d.sess.stop.is_set())

if __name__ == '__main__':
    unittest.main()
