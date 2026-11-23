"""Forge Settings page in a real browser (Chrome through Playwright), for things only a browser shows:
upload messages, search and the sidebar, preview hooks, contrast, narrow windows.

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
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_settings_page import as_uid  # noqa: E402
from test_appearance import png  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "files/system/usr/bin/forge-settings")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
DEMO = os.path.join(ROOT, "tests/data/appearance-demo.json")
CHROME = "/usr/bin/google-chrome"
OWNER = 65533

HAVE = os.geteuid() == 0 and importlib.util.find_spec("playwright") is not None and os.path.exists(CHROME)

# Runs as OWNER: opens the page, runs one scenario, prints a JSON result.
DRIVER = r'''
import json, os, sys
from playwright.sync_api import sync_playwright
url, scenario, width, scheme = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]  # argv[5]: extra data
out = {"errors": []}
CONTRAST = """
(sel) => [...document.querySelectorAll(sel)].map(e => {
  const lum = c => { const m = c.match(/[\\d.]+/g).map(Number); const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
                     return 0.2126 * f(m[0]) + 0.7152 * f(m[1]) + 0.0722 * f(m[2]); };
  const bg = x => { while (x) { const b = getComputedStyle(x).backgroundColor; const m = b.match(/[\\d.]+/g);
                    if (m && (m.length < 4 || Number(m[3]) > 0.5)) return b; x = x.parentElement; } return "rgb(255,255,255)"; };
  let op = 1, x = e; while (x) { op *= Number(getComputedStyle(x).opacity); x = x.parentElement; }
  const a = lum(getComputedStyle(e).color), b = lum(bg(e));
  return {text: e.textContent.trim(), ratio: (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05), opacity: op};
})
"""
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme).new_page()
    pg.on("pageerror", lambda e: out["errors"].append(str(e)[:200]))
    pg.goto(url)
    pg.wait_for_selector("#nav a")
    pg.wait_for_timeout(300)
    if scenario == "upload-status":
        bad = os.path.join(os.environ["HOME"], "notes.png")
        open(bad, "w").write("hello, not a picture")
        pg.evaluate("location.hash = '#appearance'")
        pg.wait_for_selector("#library")
        pg.set_input_files("#mediafile", bad)
        pg.wait_for_timeout(1500)
        out["library"] = pg.inner_text("#lib-progress")
        pg.select_option('[data-look="background"][data-key="type"] select', "image")
        with pg.expect_file_chooser() as fc:
            pg.click('[data-look="background"][data-key="image"] button:has-text("Upload")')
        fc.value.set_files(bad)
        pg.wait_for_timeout(1500)
        out["field"] = pg.inner_text('[data-look="background"][data-key="image"]')
    elif scenario == "remove-in-use":
        good = os.path.join(os.environ["HOME"], "pic.png")
        open(good, "wb").write(bytes.fromhex(sys.argv[5]))
        pg.evaluate("location.hash = '#appearance'")
        pg.wait_for_selector("#library")
        pg.select_option('[data-look="background"][data-key="type"] select', "image")
        with pg.expect_file_chooser() as fc:
            pg.click('[data-look="background"][data-key="image"] button:has-text("Upload")')
        fc.value.set_files(good)
        pg.wait_for_selector("#library .tile")
        pg.wait_for_timeout(500)
        out["before"] = pg.inner_text('[data-look="background"][data-key="image"] .mp-status')
        pg.click("#library button:has-text('Remove')")
        pg.click("#ask-ok")          # the page's own dialog asks first
        pg.wait_for_timeout(800)
        out["field"] = pg.inner_text('[data-look="background"][data-key="image"]')
        out["library"] = pg.inner_text("#lib-progress")
    elif scenario == "nav-while-searching":
        pg.click("#nav a[href='#system']")
        pg.fill("#search", "dns")
        pg.wait_for_timeout(200)
        pg.click("#nav a[href='#system']")
        pg.wait_for_timeout(300)
        out["search"] = pg.input_value("#search")
        out["title"] = pg.inner_text("#page-title")
    elif scenario == "preview-hooks":
        pg.evaluate("location.hash = '#appearance'")
        pg.wait_for_selector(".fa-preview")
        pg.wait_for_timeout(300)
        out["ran"] = pg.eval_on_selector_all(".fa-preview", "ps => ps.map(p => p.dataset.section + '=' + (p.dataset.hookRan || 'NO'))")
    elif scenario == "contrast":
        out["brand"] = pg.evaluate(CONTRAST, ".brand h1, .brand h1 span")
        pg.fill("#search", "dns")
        pg.wait_for_timeout(200)
        out["nav"] = pg.evaluate(CONTRAST, "#nav a .n")
    elif scenario == "narrow-reset":
        pg.click("#nav a[href='#ai']")
        pg.wait_for_timeout(200)
        out["reset_widths"] = pg.eval_on_selector_all(".row:not(.custom) .reset", "rs => rs.map(r => r.getBoundingClientRect().width)")
    b.close()
print(json.dumps(out))
'''


@unittest.skipUnless(HAVE, "needs root, Playwright and Chrome")
class BrowserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.chmod(self.tmp, 0o755)
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        os.chown(self.home, OWNER, OWNER)
        self.previews = os.path.join(self.tmp, "previews")
        os.mkdir(self.previews)
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

    def run_page(self, scenario, width=1280, scheme="light", extra=""):
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "FORGE_LIB": LIB, "FORGE_SCHEMA": SCHEMA,
               "FORGE_CONF": os.path.join(self.tmp, "settings.json"), "FORGE_NO_BROWSER": "1",
               "FORGE_APPLY": "/bin/false", "FORGE_APPEARANCE_SCHEMA": DEMO,
               "FORGE_APPEARANCE_LIBEXEC": os.path.join(self.tmp, "libexec"),
               "FORGE_APPEARANCE_PREVIEWS": self.previews}
        self.proc = subprocess.Popen([sys.executable, "-u", APP], env=env, preexec_fn=as_uid(OWNER),
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        ready, _, _ = select.select([self.proc.stdout], [], [], 10)
        self.assertTrue(ready)
        url = re.search(r"http://\S+", self.proc.stdout.readline()).group(0)
        r = subprocess.run([sys.executable, self.driver, url, scenario, str(width), scheme, extra], env=env,
                           preexec_fn=as_uid(OWNER), capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["errors"], [])
        self.proc.terminate()
        self.proc.wait()
        self.proc.stdout.close()
        self.proc = None
        return out

    def test_upload_messages_stay_visible(self):
        out = self.run_page("upload-status")
        self.assertIn("Not added", out["library"])
        self.assertIn("Not added", out["field"])

    def test_removing_the_media_in_use_clears_its_upload_message(self):
        out = self.run_page("remove-in-use", extra=png().hex())
        self.assertIn("Added", out["before"])
        self.assertNotIn("Added", out["field"])
        self.assertIn("Nothing chosen", out["field"])
        self.assertIn("Removed", out["library"])

    def test_nav_entry_of_the_current_page_ends_the_search(self):
        out = self.run_page("nav-while-searching")
        self.assertEqual(out["search"], "")
        self.assertEqual(out["title"], "System")

    def test_preview_hooks_survive_html_looking_text(self):
        for sid in ("background", "dock"):
            with open(os.path.join(self.previews, sid + ".js"), "w") as f:
                f.write('{\nconst marker = "<!--<script>";   // looks like HTML, is only text\n'
                        f'forgeAppearance.registerPreview("{sid}", (el) => {{ el.dataset.hookRan = "yes"; }});\n}}\n')
        out = self.run_page("preview-hooks")
        self.assertIn("background=yes", out["ran"])
        self.assertIn("dock=yes", out["ran"])

    def test_brand_and_sidebar_contrast(self):
        for scheme in ("light", "dark"):
            with self.subTest(scheme=scheme):
                out = self.run_page("contrast", scheme=scheme)
                for item in out["brand"] + out["nav"]:
                    self.assertGreaterEqual(item["ratio"], 4.5, item)
                    self.assertEqual(item["opacity"], 1, item)

    def test_hidden_reset_leaves_no_gap_in_a_narrow_window(self):
        out = self.run_page("narrow-reset", width=600)
        self.assertTrue(out["reset_widths"])
        self.assertEqual(set(out["reset_widths"]), {0})


if __name__ == "__main__":
    unittest.main()
