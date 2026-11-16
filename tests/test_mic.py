"""无需真实声卡，用 pactl 输出样本验证麦克风守护逻辑。"""
import ast
import os
import re
from pathlib import Path
import unittest

NAMES = {"pactl", "_blocks", "PROFILE_RE", "parse_cards", "input_profile_to_restore",
         "parse_sources", "parse_source_outputs", "MicKeeper"}
source = ast.parse((Path(__file__).resolve().parents[1] / "dictate.py").read_text())
nodes = [n for n in source.body
         if (isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in NAMES)
         or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in NAMES for t in n.targets))]
ns = dict(os=os, re=re)
exec(compile(ast.Module(body=nodes, type_ignores=[]), "dictate.py", "exec"), ns)

CARD = "alsa_card.usb-Generic_FULL_HD_1080P_Webcam_200901010001-02"
MIC = "alsa_input.usb-Generic_FULL_HD_1080P_Webcam_200901010001-02.analog-stereo"
MONITOR = "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"


def cards_text(active):
    return f"""Card #0
\tName: alsa_card.pci-0000_00_1f.3
\tProfiles:
\t\toutput:analog-stereo+input:analog-stereo: Analog Stereo Duplex (sinks: 1, sources: 1, priority: 6565, available: yes)
\tActive Profile: output:analog-stereo+input:analog-stereo

Card #18
\tName: {CARD}
\tProperties:
\t\tdevice.description = "FULL HD 1080P Webcam"
\tProfiles:
\t\tinput:analog-stereo: 模拟立体声 输入 (sinks: 0, sources: 1, priority: 65, available: yes)
\t\tinput:iec958-stereo: 数字立体声(IEC958) 输入 (sinks: 0, sources: 1, priority: 55, available: yes)
\t\toff: 关 (sinks: 0, sources: 0, priority: 0, available: yes)
\tActive Profile: {active}
\tPorts:
\t\tanalog-input-mic: Microphone (priority: 8700, latency offset: 0 usec)
"""


def outputs_text(source, pid=4242):
    return f"""Source Output #26
\tDriver: protocol-native.c
\tSource: {source}
\tProperties:
\t\tapplication.name = "ALSA plug-in [python3.11]"
\t\tapplication.process.id = "{pid}"

Source Output #30
\tSource: {source}
\tProperties:
\t\tapplication.process.id = "999"
"""


class FakePulse:
    def __init__(self, active="input:analog-stereo", mic_present=True, stream_source=2):
        self.active, self.mic_present, self.stream_source = active, mic_present, stream_source
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args == ("list", "cards"):
            return cards_text(self.active)
        if args == ("list", "sources", "short"):
            rows = [f"2\t{MONITOR}\tmodule-alsa-card.c\ts16le 2ch 48000Hz\tRUNNING"]
            if self.mic_present:
                rows.append(f"36\t{MIC}\tmodule-alsa-card.c\ts16le 2ch 48000Hz\tRUNNING")
            return "\n".join(rows) + "\n"
        if args == ("list", "source-outputs"):
            return outputs_text(self.stream_source)
        if args[0] == "set-card-profile":
            self.active = args[2]
            return ""
        if args[0] == "move-source-output":
            self.stream_source = 36
            return ""
        return None

    def writes(self):
        return [c for c in self.calls if c[0] in ("set-card-profile", "move-source-output")]


class ParseTests(unittest.TestCase):
    def test_parse_cards(self):
        cards = ns["parse_cards"](cards_text("off"))
        self.assertEqual([c["name"] for c in cards], ["alsa_card.pci-0000_00_1f.3", CARD])
        self.assertEqual(cards[1]["active"], "off")
        self.assertEqual([p["name"] for p in cards[1]["profiles"]],
                         ["input:analog-stereo", "input:iec958-stereo", "off"])

    def test_restore_profile_only_when_no_source(self):
        restore = ns["input_profile_to_restore"]
        cards = lambda a: ns["parse_cards"](cards_text(a))[1]
        self.assertEqual(restore(cards("off")), "input:analog-stereo")
        self.assertIsNone(restore(cards("input:iec958-stereo")))
        self.assertIsNone(restore(cards("input:analog-stereo")))

    def test_parse_source_outputs(self):
        outs = ns["parse_source_outputs"](outputs_text(2))
        self.assertEqual(outs, [dict(index=26, source=2, pid=4242), dict(index=30, source=2, pid=999)])


class KeeperTests(unittest.TestCase):
    def keeper(self, pulse, match=""):
        self.logs = []
        return ns["MicKeeper"](match, pid=4242, run=pulse, log=self.logs.append)

    def test_moves_own_stream_back_from_monitor(self):
        pulse = FakePulse(stream_source=2)
        k = self.keeper(pulse)
        k.check()
        self.assertEqual(pulse.writes(), [("move-source-output", "26", MIC)])
        self.assertTrue(k.ok)
        self.assertEqual(k.target, MIC)

    def test_no_writes_when_already_fine(self):
        pulse = FakePulse(stream_source=36)
        k = self.keeper(pulse)
        k.check()
        k.check()
        self.assertEqual(pulse.writes(), [])
        self.assertTrue(k.ok)

    def test_disconnect_then_reconnect(self):
        pulse = FakePulse(stream_source=36)
        k = self.keeper(pulse)
        k.check()
        # 显示器断电：麦克风消失，PulseAudio 把录音流挪到扬声器回环
        pulse.mic_present, pulse.stream_source = False, 2
        k.check()
        self.assertFalse(k.ok)
        # 显示器上电：声卡初始化失败，停在 off
        pulse.active = "off"
        k.check()
        self.assertEqual(pulse.writes()[-1], ("set-card-profile", CARD, "input:analog-stereo"))
        pulse.mic_present = True
        k.check()
        self.assertEqual(pulse.writes()[-1], ("move-source-output", "26", MIC))
        self.assertTrue(k.ok)
        self.assertIn("麦克风断开，等待重新连接", self.logs)

    def test_auto_mode_not_armed_without_usb_mic(self):
        pulse = FakePulse(mic_present=False, stream_source=2)
        k = self.keeper(pulse)
        k.check()
        self.assertTrue(k.ok)  # 从没见过 USB 麦克风：不拦截，照常用默认设备
        self.assertEqual(pulse.writes(), [])

    def test_explicit_match_armed_from_start(self):
        pulse = FakePulse(mic_present=False, stream_source=2)
        k = self.keeper(pulse, match="Webcam")
        k.check()
        self.assertFalse(k.ok)

    def test_pactl_failure_is_harmless(self):
        k = self.keeper(lambda *a: None)
        k.check()
        self.assertTrue(k.ok)


if __name__ == "__main__":
    unittest.main()
