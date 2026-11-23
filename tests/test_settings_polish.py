"""Forge Settings and Welcome pages, the "Tempered" polish: shared design tokens, honest state, dialogs,
toasts, loading and error states, number fields that never store a cleared value, and the Welcome steps.

Static checks run anywhere. Browser checks run Chrome through Playwright, with the server and the browser
both under a test uid (the page only works for the user who started it), so they need root.
"""
import importlib.machinery
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
TEMPERED = os.path.join(ROOT, "files/system/usr/share/forge/tempered.css")
I18N = os.path.join(ROOT, "files/system/usr/share/forge/i18n")
# New strings wait here until they are merged into the tables; the tests read them if a key is missing.
PENDING = "/tmp/i18n-settings.json"
DOCK = os.path.join(PREVIEWS, "dock.js")
CHROME = "/usr/bin/google-chrome"
OWNER = 65533
HAVE = os.geteuid() == 0 and importlib.util.find_spec("playwright") is not None and os.path.exists(CHROME)


def load_app():
    os.environ.setdefault("FORGE_LIB", LIB)
    loader = importlib.machinery.SourceFileLoader("forge_settings_polish", APP)
    spec = importlib.util.spec_from_loader("forge_settings_polish", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def merged_tables(into):
    """The string tables with any pending new keys added (keys already in a table win)."""
    pending = {}
    if os.path.exists(PENDING):
        with open(PENDING, encoding="utf-8") as f:
            pending = json.load(f)
    for lang in ("en", "th"):
        with open(os.path.join(I18N, lang + ".json"), encoding="utf-8") as f:
            doc = json.load(f)
        for k, v in pending.get(lang, {}).items():
            doc["strings"].setdefault(k, v)
        with open(os.path.join(into, lang + ".json"), "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.chmod(os.path.join(into, lang + ".json"), 0o644)


def page_source(name):
    with open(APP, encoding="utf-8") as f:
        src = f.read()
    return src.split(name + ' = r"""', 1)[1].split('"""', 1)[0]


class StaticTest(unittest.TestCase):
    """No browser needed."""

    def test_tokens_are_inlined_and_the_pages_have_no_colour_block_of_their_own(self):
        for name in ("PAGE", "WELCOME_PAGE"):
            src = page_source(name)
            self.assertIn("/*__TEMPERED__*/", src, name)
            self.assertNotRegex(src, r":root\s*\{[^}]*--bg:", name)
            self.assertNotIn("font-weight: 650", src, name)
            self.assertNotIn("ui-monospace", src, name)
            self.assertNotIn("confirm(", src, name)
            self.assertNotIn("prompt(", src, name)

    def test_tempered_css_is_read_from_the_override_or_falls_back(self):
        mod = load_app()
        css = mod.tempered_css(TEMPERED)
        self.assertIn("--radius-s", css)
        self.assertIn("forge-cool", css)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertIn("--bg", mod.tempered_css(os.path.join(tmp, "missing.css")))     # built-in fallback
        bad = os.path.join(tmp, "bad.css")
        with open(bad, "w") as f:
            f.write(":root { --bg: red; } </style><script>alert(1)</script>")
        self.assertNotIn("</style>", mod.tempered_css(bad))
        self.assertNotIn("--bg: red", mod.tempered_css(bad))
        big = os.path.join(tmp, "big.css")
        with open(big, "w") as f:
            f.write(":root { --bg: blue; }" + " " * (300 * 1024))
        self.assertNotIn("blue", mod.tempered_css(big))

    def test_no_em_dash_or_double_hyphen_in_new_text(self):
        for name in ("PAGE", "WELCOME_PAGE"):
            src = page_source(name)
            self.assertNotIn("—", src, name)
            # "--" only as a CSS custom property, an argument like --now, or a decrement.
            prose = re.sub(r"--[a-z0-9]", "", src)
            prose = re.sub(r"\w--", "", prose)
            self.assertNotIn(" -- ", prose, name)
        with open(DOCK, encoding="utf-8") as f:
            self.assertNotIn("—", f.read())

    def test_dock_zoom_writes_transforms_only_once_per_frame(self):
        with open(DOCK, encoding="utf-8") as f:
            js = f.read()
        self.assertIn("requestAnimationFrame", js)
        self.assertNotRegex(js, r"margin\s*\+?=?\s*\"?\s*\+?\s*st\.", "no margin animation")
        self.assertNotIn("margin \" +", js)
        self.assertNotIn("ease-in,", js)
        self.assertNotRegex(js, r"ms ease-in\b", "zoom eases out both ways")

    def test_pending_strings_are_well_formed(self):
        if not os.path.exists(PENDING):
            self.skipTest("no pending strings")
        with open(PENDING, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(set(doc["en"]), set(doc["th"]) | {k for k in doc["en"] if k.startswith("set.opt.")})
        for lang in ("en", "th"):
            for k, v in doc[lang].items():
                self.assertRegex(k, r"^[a-z0-9_.-]{1,120}$")
                self.assertNotIn("—", v, k)
                self.assertNotRegex(v, r"--(?![a-z])", k)
                if lang == "th":
                    self.assertNotIn("ครับ", v, k)
                    self.assertNotIn("ค่ะ", v, k)
                else:
                    self.assertNotIn("!", v, k)
                    self.assertNotIn("please", v.lower(), k)
        for k in doc["th"]:
            self.assertEqual(sorted(re.findall(r"\{[a-z_]+\}", doc["en"][k])),
                             sorted(re.findall(r"\{[a-z_]+\}", doc["th"][k])), k)


DRIVER = r'''
import json, os, sys
from playwright.sync_api import sync_playwright
url, scenario, width, scheme = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
out = {"errors": [], "native_dialogs": 0}
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: out["errors"].append(str(e)[:300]))
    def native(d):
        out["native_dialogs"] += 1
        d.dismiss()
    pg.on("dialog", native)
    held = []
    if scenario == "load-fail":
        pg.route("**/api/settings", lambda r: held.append(r))
        pg.goto(url)
        pg.wait_for_timeout(500)
        out["skeleton"] = pg.locator("#content .skel").count()
        held[0].fulfill(status=500, body="{}")
        pg.wait_for_selector("#content .load-error")
        out["error_text"] = pg.inner_text("#content .load-error")
        pg.unroute("**/api/settings")
        pg.click("#content .load-error button")
        pg.wait_for_selector("#nav a")
        out["nav"] = pg.locator("#nav a").count()
        out["title"] = pg.inner_text("#page-title")
    elif scenario == "welcome":
        pg.route("**/api/doctor", lambda r: held.append(r))
        pg.goto(url)
        pg.wait_for_timeout(300)
        out["skeleton"] = pg.locator("#report .skel").count()
        held[0].continue_()
        pg.unroute("**/api/doctor")
        pg.wait_for_timeout(1500)
        out["save_disabled_at_start"] = pg.is_disabled("#save")
        out["switch_roles"] = pg.eval_on_selector_all("[data-key]", "cs => cs.map(c => c.getAttribute('role'))")
        pg.click("label:has([data-key='location.wifi_lookup']) .knob", force=True)
        out["save_disabled_after_change"] = pg.is_disabled("#save")
        pg.click("label:has([data-key='location.wifi_lookup']) .knob", force=True)
        out["save_disabled_after_change_back"] = pg.is_disabled("#save")
        out["h1_accent"] = pg.eval_on_selector_all("h1 span", "ss => ss.map(s => getComputedStyle(s).color)")
        out["steps"] = pg.locator(".step-n").count()
        out["empty_items"] = pg.eval_on_selector_all("#report li", "ls => ls.filter(l => !l.textContent.trim() || /:\\s*$/.test(l.textContent)).length")
        out["favicon"] = pg.get_attribute("link[rel=icon]", "href")[:22]
        b0 = pg.locator("[data-copy]").first
        b0.click()
        pg.wait_for_timeout(300)
        out["copy_now"] = b0.inner_text()
        pg.wait_for_timeout(2300)
        out["copy_later"] = b0.inner_text()
        pg.click("#done")
        pg.wait_for_timeout(400)
        out["done"] = "closed" if pg.is_closed() else pg.inner_text("#done-note")
    else:
        pg.goto(url)
        pg.wait_for_selector("#nav a")
        pg.wait_for_timeout(300)
    if scenario == "number-field":
        pg.evaluate("location.hash = '#ai'")
        pg.wait_for_timeout(400)
        row = '.row[data-key="ai.max_loaded_models"]'
        inp = row + " input"
        out["range"] = pg.inner_text(row + " .range")
        pg.fill(inp, "")
        pg.wait_for_timeout(100)
        out["empty_invalid"] = pg.get_attribute(inp, "aria-invalid")
        out["empty_error"] = pg.inner_text(row + " .err")
        out["empty_count"] = pg.inner_text("#count")
        pg.click("#page-title")
        pg.wait_for_timeout(100)
        out["after_blur"] = pg.input_value(inp)
        pg.fill(inp, "99")
        pg.click("#page-title")
        pg.wait_for_timeout(100)
        out["clamped"] = pg.input_value(inp)
        out["clamped_count"] = pg.inner_text("#count")
        out["clamped_invalid"] = pg.get_attribute(inp, "aria-invalid")
    elif scenario == "system-page":
        pg.evaluate("location.hash = '#system'")
        pg.wait_for_timeout(400)
        out["tags_on_page"] = pg.locator("#content .rtags button").count()
        out["grid"] = pg.locator("#g-modes .modes-grid").count()
        out["grid_heads"] = pg.eval_on_selector_all("#g-modes .modes-grid thead th", "ths => ths.map(t => t.textContent.trim())")
        out["option_text"] = pg.eval_on_selector_all('[data-key="modes.gaming.power"] select option', "os => os.map(o => o.textContent)")
        out["content_live"] = pg.get_attribute("#content", "aria-live")
        out["status_node"] = pg.locator("[role=status]#live").count()
        pg.select_option('[data-key="modes.quiet.power"] select', "balanced")
        out["count_after_grid_change"] = pg.inner_text("#count")
        out["focus_after_nav"] = None
        pg.click("#nav a[href='#data']")
        pg.wait_for_timeout(500)
        out["focus_after_nav"] = pg.evaluate("document.activeElement.id")
        out["file_on_data_page"] = pg.locator("#content #file").count()
        out["file_in_bar"] = pg.locator("#bar #file").count()
        pg.fill("#search", "dns")
        pg.wait_for_timeout(400)
        out["tags_in_search"] = pg.locator("#content .rtags button").count()
    elif scenario == "channel-dialog":
        pg.evaluate("location.hash = '#system'")
        pg.wait_for_timeout(400)
        pg.select_option('[data-key="updates.channel"] select', "testing")
        pg.click("#save")
        pg.wait_for_selector("#ask[open]")
        out["ask_title"] = pg.inner_text("#ask-title")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        out["ask_open"] = pg.evaluate("document.querySelector('#ask').open")
        out["toast"] = pg.inner_text("#toasts")
    elif scenario == "export-toast":
        pg.evaluate("location.hash = '#system'")
        pg.wait_for_timeout(400)
        pg.click("#more")
        out["menu_items"] = pg.eval_on_selector_all("#more-menu [role=menuitem]", "ms => ms.map(m => m.id)")
        with pg.expect_download():
            pg.click("#export")
        pg.wait_for_timeout(300)
        out["toast"] = pg.inner_text("#toasts")
        out["menu_hidden"] = pg.evaluate("document.querySelector('#more-menu').hidden")
    elif scenario == "appearance":
        pg.evaluate("location.hash = '#appearance'")
        pg.wait_for_selector("#presets")
        btn = "#presets .ps-save button"
        out["preset_save_empty"] = pg.is_disabled(btn)
        pg.fill("#ps-name", "Evening")
        out["preset_save_named"] = pg.is_disabled(btn)
        pg.fill("#ps-name", "   ")
        out["preset_save_blank"] = pg.is_disabled(btn)
        hexes = pg.locator("input.hex:visible")
        out["hex_count"] = hexes.count()
        if hexes.count():
            hexes.first.fill("#12")
            out["hex_bad"] = hexes.first.get_attribute("aria-invalid")
            hexes.first.fill("#123456")
            out["hex_good"] = hexes.first.get_attribute("aria-invalid")
        out["no_apply_pills"] = pg.locator(".look-head .pill").count()
        out["nothing_chosen"] = pg.eval_on_selector_all(".media-pick", "ms => ms.map(m => (m.textContent.match(/Nothing chosen/g) || []).length)")
        sl = pg.locator(".row input[type=range]").first
        row = sl.locator("xpath=ancestor::div[contains(@class,'row')][1]")
        out["slider_text"] = row.inner_text()
        # The preview node is kept across a change elsewhere on the page.
        pg.evaluate("document.querySelector('.fa-preview').dataset.keep = 'same'")
        pg.fill("#ps-name", "x")
        sel = pg.locator('[data-look="background"][data-key="type"] select')
        sel.select_option(index=1)
        pg.wait_for_timeout(200)
        out["preview_kept"] = pg.evaluate("document.querySelector('.fa-preview').dataset.keep || ''")
    elif scenario == "dashboard-revert":
        pg.click("#dash")
        pg.wait_for_timeout(500)
        out["now"] = pg.inner_text("#dash")
        pg.wait_for_timeout(3200)
        out["later"] = pg.inner_text("#dash")
    elif scenario == "idle-banner":
        out["banner_at_start"] = pg.is_visible("#conn")
        pg.route("**/api/ping", lambda r: r.abort())
        pg.evaluate("window.dispatchEvent(new Event('focus'))")
        pg.wait_for_timeout(800)
        out["banner"] = pg.is_visible("#conn")
        out["banner_text"] = pg.inner_text("#conn") if out["banner"] else ""
        pg.unroute("**/api/ping")
        pg.click("#conn button")
        pg.wait_for_timeout(800)
        out["banner_after"] = pg.is_visible("#conn")
    elif scenario == "narrow":
        for sec in ("system", "appearance", "data"):
            pg.evaluate(f"location.hash = '#{sec}'")
            pg.wait_for_timeout(400)
            out[sec + "_bar_rows"] = pg.evaluate("""() => { const ks = [...document.querySelectorAll('#bar button, #bar #count')].filter(e => e.offsetParent);
              const cs = ks.map(k => { const r = k.getBoundingClientRect(); return r.top + r.height / 2; }).sort((a, b) => a - b);
              return cs.filter((c, i) => i === 0 || c - cs[i - 1] > 8).length; }""")
            out[sec + "_wide"] = pg.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
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
        self.i18n = os.path.join(self.tmp, "i18n")
        os.mkdir(self.i18n)
        merged_tables(self.i18n)
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

    def run_page(self, scenario, args=(), width=1280, scheme="light", lang="C.UTF-8"):
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "LANG": lang, "FORGE_LIB": LIB, "FORGE_SCHEMA": SCHEMA,
               "FORGE_CONF": os.path.join(self.tmp, "settings.json"), "FORGE_NO_BROWSER": "1",
               "FORGE_APPLY": "/bin/false", "FORGE_APPEARANCE_SCHEMA": LOOK,
               "FORGE_APPEARANCE_LIBEXEC": os.path.join(self.tmp, "libexec"), "FORGE_APPEARANCE_PREVIEWS": PREVIEWS,
               "FORGE_TEMPERED_CSS": TEMPERED, "FORGE_I18N_DIR": self.i18n}
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
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["native_dialogs"], 0, "no confirm() or prompt()")
        return out

    def test_a_cleared_number_is_never_stored_and_is_clamped_on_blur(self):
        out = self.run_page("number-field")
        self.assertEqual(out["range"], "1 to 8")
        self.assertEqual(out["empty_invalid"], "true")
        self.assertIn("1 to 8", out["empty_error"])
        self.assertEqual(out["empty_count"], "No unsaved changes.")
        self.assertEqual(out["after_blur"], "1")
        self.assertEqual(out["clamped"], "8")
        self.assertEqual(out["clamped_count"], "1 unsaved change.")
        self.assertNotEqual(out["clamped_invalid"], "true")

    def test_the_page_never_stays_blank_when_loading_fails(self):
        out = self.run_page("load-fail")
        self.assertGreater(out["skeleton"], 0)
        self.assertIn("Retry", out["error_text"])
        self.assertEqual(out["nav"], 6)
        self.assertTrue(out["title"])

    def test_system_page(self):
        out = self.run_page("system-page")
        self.assertEqual(out["tags_on_page"], 0, "tags only in search results")
        self.assertGreater(out["tags_in_search"], 0)
        self.assertEqual(out["grid"], 1)
        self.assertEqual(out["grid_heads"][1:], ["Gaming", "Agents", "Creative", "Quiet"])
        self.assertEqual(out["option_text"], ["Performance", "Balanced", "Power saver"])
        self.assertIsNone(out["content_live"])
        self.assertEqual(out["status_node"], 1)
        self.assertEqual(out["count_after_grid_change"], "1 unsaved change.")
        self.assertEqual(out["focus_after_nav"], "page-title")
        self.assertEqual(out["file_on_data_page"], 1)
        self.assertEqual(out["file_in_bar"], 0)

    def test_channel_switch_asks_in_a_page_dialog(self):
        out = self.run_page("channel-dialog")
        self.assertIn("Testing", out["ask_title"])
        self.assertFalse(out["ask_open"])
        self.assertIn("Not saved", out["toast"])

    def test_export_is_in_a_menu_and_confirms_with_a_toast(self):
        out = self.run_page("export-toast")
        self.assertEqual(out["menu_items"], ["export", "import", "undo"])
        self.assertIn("Exported", out["toast"])
        self.assertTrue(out["menu_hidden"])

    def test_appearance_details(self):
        out = self.run_page("appearance")
        self.assertTrue(out["preset_save_empty"])
        self.assertFalse(out["preset_save_named"])
        self.assertTrue(out["preset_save_blank"])
        if out["hex_count"]:
            self.assertEqual(out["hex_bad"], "true")
            self.assertNotEqual(out["hex_good"], "true")
        self.assertEqual(out["no_apply_pills"], 0, "one 'not available' message, in the section footer")
        self.assertTrue(all(n <= 1 for n in out["nothing_chosen"]), out["nothing_chosen"])
        self.assertRegex(out["slider_text"], r"\d")
        self.assertEqual(out["preview_kept"], "same")

    def test_dashboard_button_text_reverts(self):
        out = self.run_page("dashboard-revert")
        self.assertNotEqual(out["now"], "Open Forge Dashboard")
        self.assertEqual(out["later"], "Open Forge Dashboard")

    def test_lost_connection_shows_a_banner(self):
        out = self.run_page("idle-banner")
        self.assertFalse(out["banner_at_start"])
        self.assertTrue(out["banner"])
        self.assertIn("Reconnect", out["banner_text"])
        self.assertFalse(out["banner_after"])

    def test_save_bar_stays_one_row_when_narrow(self):
        for lang in ("C.UTF-8", "th_TH.UTF-8"):
            out = self.run_page("narrow", width=700, lang=lang)
            for sec in ("system", "appearance", "data"):
                self.assertLessEqual(out[sec + "_bar_rows"], 1, (lang, sec))
                self.assertFalse(out[sec + "_wide"], (lang, sec))

    def test_welcome_page(self):
        out = self.run_page("welcome", args=("--welcome",))
        self.assertGreater(out["skeleton"], 0)
        self.assertTrue(out["save_disabled_at_start"])
        self.assertEqual(set(out["switch_roles"]), {"switch"})
        self.assertFalse(out["save_disabled_after_change"])
        self.assertTrue(out["save_disabled_after_change_back"])
        self.assertNotEqual(out["h1_accent"][0], out["h1_accent"][1], "only Forge OS in the accent")
        self.assertEqual(out["steps"], 5)
        self.assertEqual(out["empty_items"], 0)
        self.assertTrue(out["favicon"].startswith("data:image/svg+xml"))
        self.assertNotEqual(out["copy_now"], "Copy")
        self.assertEqual(out["copy_later"], "Copy")
        self.assertTrue(out["done"])


if __name__ == "__main__":
    unittest.main()
