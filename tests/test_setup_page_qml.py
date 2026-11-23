"""The first-boot AI storage page loads in a real Qt 6 QML engine. Plasma Setup and Kirigami are
replaced by small stand-ins (tests/fixtures/qml-stubs) built from Plasma Setup 6.8.3's own type
description: SetupModule is a QObject with a required contentItem and a nextRequested signal.
Skipped when Qt for Python (PySide6) is not installed."""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "files/system/usr/share/plasma/packages/org.forgeos.setup.aistorage/contents/ui/main.qml")
STUBS = os.path.join(ROOT, "tests/fixtures/qml-stubs")

LOADER = r'''
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"; os.environ["QT_QUICK_CONTROLS_STYLE"] = "Basic"
from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlEngine, QQmlComponent
app = QGuiApplication(sys.argv)
eng = QQmlEngine(); eng.addImportPath(sys.argv[2])
c = QQmlComponent(eng, QUrl.fromLocalFile(sys.argv[1]))
o = None if c.isError() else c.create()
if o is None:
    print("ERRORS", [e.toString() for e in c.errors()]); sys.exit(1)
o.setProperty("mode", "percent"); o.setProperty("pct", 40)
print("OK", o.property("previewReserve"), o.property("mode"))
'''


class SetupPageQmlTest(unittest.TestCase):
    def test_page_loads_and_is_created(self):
        try:
            import PySide6  # noqa: F401
        except ImportError:
            self.skipTest("PySide6 is not installed")
        r = subprocess.run([sys.executable, "-c", LOADER, PAGE, STUBS], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK -1.0 percent", r.stdout)   # no disk.ini here: no preview, but no errors


if __name__ == "__main__":
    unittest.main()
