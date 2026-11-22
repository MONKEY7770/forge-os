"""Thai in a real browser (Chrome through Playwright): the language switch, and the layout with longer Thai
text (no page wider than the window, no clipped buttons or labels, no script errors), light and dark.

The page only works for the user who started it, so the server and the browser both run as a test uid.
Skipped without root, Playwright or Chrome.
"""
import importlib.util
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_settings_page import as_uid  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "files/system/usr/bin/forge-settings")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
LOOK = os.path.join(ROOT, "files/system/usr/share/forge/appearance.json")
PREVIEWS = os.path.join(ROOT, "files/system/usr/share/forge/appearance-preview")
CHROME = "/usr/bin/google-chrome"
OWNER = 65533
HAVE = os.geteuid() == 0 and importlib.util.find_spec("playwright") is not None and os.path.exists(CHROME)

DRIVER = r'''
import json, sys
from playwright.sync_api import sync_playwright
url, scenario, width, scheme = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
out = {"errors": [], "bad": []}
CHECK = """() => {
  const bad = [];
  if (document.documentElement.scrollWidth > innerWidth + 1) bad.push("page is " + document.documentElement.scrollWidth + "px wide");
  document.querySelectorAll("button, .label, .pill, .nm, .n, h2, h3, select, .btn, .t, .k").forEach(e => {
    if (e.offsetParent === null) return;
    const st = getComputedStyle(e);
    if (e.scrollWidth > e.clientWidth + 2 && st.textOverflow !== "ellipsis" && e.tagName !== "SELECT")
      bad.push("clipped: " + e.textContent.trim().slice(0, 50));
    if (e.getBoundingClientRect().right > innerWidth + 1) bad.push("past the right edge: " + e.textContent.trim().slice(0, 50));
  });
  return bad;
}"""
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_context(viewport={"width": width, "height": 800}, color_scheme=scheme).new_page()
    pg.on("pageerror", lambda e: out["errors"].append(str(e)[:300]))
    pg.goto(url)
    pg.wait_for_selector("#nav a")
    pg.wait_for_timeout(300)
    if scenario == "switch":
        out["start"] = pg.inner_text("#nav a[href='#system'] .n")
        pg.click("#lang button[data-lang='th']")
        pg.wait_for_timeout(400)
        out["thai"] = pg.inner_text("#nav a[href='#system'] .n")
        out["lang_attr"] = pg.evaluate("document.documentElement.lang")
        out["save_button"] = pg.inner_text("#save")
        pg.click("#lang button[data-lang='en']")
        pg.wait_for_timeout(400)
        out["back"] = pg.inner_text("#nav a[href='#system'] .n")
        out["pressed"] = pg.get_attribute("#lang button[data-lang='en']", "aria-pressed")
    elif scenario == "layout":
        for sec in ("appearance", "system", "network", "privacy", "ai", "data"):
            pg.evaluate(f"location.hash = '#{sec}'")
            pg.wait_for_timeout(300)
            out["bad"] += [sec + ": " + x for x in pg.evaluate(CHECK)]
        pg.evaluate("location.hash = '#appearance'")
        pg.wait_for_selector("#presets")
        out["presets_title"] = pg.inner_text("#ps-h")
    elif scenario == "dashboard":
        for sec in ("home", "agents", "activity", "updates", "backups", "storage", "sync", "vms", "hub", "diagnostics"):
            pg.evaluate(f"location.hash = '#{sec}'")
            pg.wait_for_timeout(500)
            out["bad"] += [sec + ": " + x for x in pg.evaluate(CHECK)]
        out["home"] = pg.inner_text("#nav a[href='#home'] .n")
    b.close()
print(json.dumps(out))
'''


@unittest.skipUnless(HAVE, "needs root, Playwright and Chrome")
class ThaiBrowserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.chmod(self.tmp, 0o755)
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        os.chown(self.home, OWNER, OWNER)
        self.driver = os.path.join(self.tmp, "driver.py")
        with open(self.driver, "w") as f:
            f.write(DRIVER)
        os.chmod(self.driver, 0o644)
        self.proc = None

    def tearDown(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait()
            self.proc.stdout.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_page(self, scenario, lang="C.UTF-8", width=1280, scheme="light", args=()):
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "LANG": lang, "FORGE_LIB": LIB, "FORGE_SCHEMA": SCHEMA,
               "FORGE_CONF": os.path.join(self.tmp, "settings.json"), "FORGE_NO_BROWSER": "1",
               "FORGE_APPLY": "/bin/false", "FORGE_APPEARANCE_SCHEMA": LOOK,
               "FORGE_APPEARANCE_LIBEXEC": os.path.join(self.tmp, "libexec"), "FORGE_APPEARANCE_PREVIEWS": PREVIEWS}
        self.proc = subprocess.Popen([sys.executable, "-u", APP, *args], env=env, preexec_fn=as_uid(OWNER),
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        ready, _, _ = select.select([self.proc.stdout], [], [], 10)
        self.assertTrue(ready)
        url = re.search(r"http://\S+", self.proc.stdout.readline()).group(0)
        r = subprocess.run([sys.executable, self.driver, url, scenario, str(width), scheme], env=env,
                           preexec_fn=as_uid(OWNER), capture_output=True, text=True, timeout=180)
        self.proc.terminate()
        self.proc.wait()
        self.proc.stdout.close()
        self.proc = None
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_the_switch_changes_the_page_at_once(self):
        out = self.run_page("switch")
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["start"], "System")
        self.assertEqual(out["thai"], "ระบบ")
        self.assertEqual(out["lang_attr"], "th")
        self.assertEqual(out["save_button"], "บันทึกและใช้งาน")
        self.assertEqual(out["back"], "System")
        self.assertEqual(out["pressed"], "true")

    def test_thai_settings_fit_light_and_dark_wide_and_narrow(self):
        for width, scheme in ((1280, "light"), (1280, "dark"), (760, "light")):
            out = self.run_page("layout", lang="th_TH.UTF-8", width=width, scheme=scheme)
            self.assertEqual(out["errors"], [], (width, scheme))
            self.assertEqual(out["bad"], [], (width, scheme))
            self.assertEqual(out["presets_title"], "พรีเซ็ต")

    def test_thai_dashboard_fits(self):
        out = self.run_page("dashboard", lang="th_TH.UTF-8", args=("--dashboard",))
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["bad"], [])
        self.assertEqual(out["home"], "หน้าหลัก")


if __name__ == "__main__":
    unittest.main()
