"""The boot splash progress bar (shown while updates install) stays visible on any background:
it was always white on grey, which disappears on a light background."""
import importlib.machinery
import importlib.util
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "files/system/usr/libexec/forge-boot-apply")


def load():
    loader = importlib.machinery.SourceFileLoader("forge_boot_apply_colors", PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def bar(theme):
    return (re.search(r"ProgressBarBackgroundColor=0x([0-9a-f]{6})", theme).group(1),
            re.search(r"ProgressBarForegroundColor=0x([0-9a-f]{6})", theme).group(1))


def lum(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a, b):
    x, y = sorted((lum(a), lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


class SplashBarTest(unittest.TestCase):
    def test_bar_is_visible_on_light_and_dark_backgrounds(self):
        m = load()
        for bg in ("#000000", "#141312", "#102030", "#f7f6f3", "#ffffff", "#ffd000"):
            back, fore = bar(m.theme_text("loop", bg, False))
            self.assertGreaterEqual(contrast(fore, bg[1:]), 3, bg)     # the filled part shows
            self.assertGreaterEqual(contrast(fore, back), 3, bg)       # and stands out from the track

    def test_dark_background_keeps_the_old_colours(self):
        self.assertEqual(bar(load().theme_text("loop", "#102030", False)), ("606060", "ffffff"))


if __name__ == "__main__":
    unittest.main()
