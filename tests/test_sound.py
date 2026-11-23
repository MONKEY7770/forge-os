"""Calm, on-time notification sounds (PipeWire). Notification sounds (libcanberra) ask for a very
large buffer, which makes them late and does not reset (PipeWire issue 1908): the largest buffer is
capped at 2048 frames. Inside a VM, emulated sound cards crackle with small buffers: there the
smallest buffer is 2048 too. Real hardware keeps PipeWire's low-latency minimum."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(ROOT, "files/system/usr/share/pipewire/pipewire.conf.d/50-forge-sound.conf")
PULSE = os.path.join(ROOT, "files/system/usr/share/pipewire/pipewire-pulse.conf.d/50-forge-sound.conf")


def text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class SoundTests(unittest.TestCase):
    def test_largest_buffer_capped_everywhere(self):
        t = text(CORE)
        props = t.split("context.properties = {", 1)[1].split("}", 1)[0]
        self.assertRegex(props, r"default\.clock\.max-quantum\s*=\s*2048\b")
        self.assertNotIn("min-quantum", props)       # real hardware keeps its low-latency minimum

    def test_vm_only_minimum(self):
        for path, key, value in ((CORE, "default.clock.min-quantum", "2048"),
                                 (PULSE, "pulse.min.quantum", "2048/48000")):
            t = text(path)
            self.assertRegex(t, r"matches\s*=\s*\[\s*\{\s*cpu\.vm\.name\s*=\s*!null\s*\}\s*\]", path)
            self.assertIn(f"{key} = {value}", t, path)

    def test_balanced_braces(self):
        for path in (CORE, PULSE):
            t = re.sub(r"#.*", "", text(path))
            self.assertEqual(t.count("{"), t.count("}"), path)
            self.assertEqual(t.count("["), t.count("]"), path)


if __name__ == "__main__":
    unittest.main()
