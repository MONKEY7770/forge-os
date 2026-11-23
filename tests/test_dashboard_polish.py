"""Forge Dashboard, the "Tempered" polish: shared tokens inlined when the page is served, readable buttons,
honest live state on Home only, translated activity titles, results and focus that survive a refresh,
skeletons, dialogs, motion that respects reduced motion, and slower polling in a hidden window.

Static checks run anywhere. Browser checks run Chrome through Playwright against the real server (the
fakes from test_dashboard), with the server and the browser both under the test uid, so they need root.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_dashboard as td  # noqa: E402
from test_settings_page import APP, LIB, OWNER, ROOT, as_uid  # noqa: E402

PAGE = os.path.join(LIB, "dashboard.html")
FORGEDASH = os.path.join(LIB, "forgedash.py")
TEMPERED = os.path.join(ROOT, "files/system/usr/share/forge/tempered.css")
I18N = os.path.join(ROOT, "files/system/usr/share/forge/i18n")
# New strings wait here until they are merged into the tables; the tests read them while a key is missing.
PENDING = "/tmp/i18n-dash.json"
CHROME = "/usr/bin/google-chrome"
HAVE = os.geteuid() == 0 and importlib.util.find_spec("playwright") is not None and os.path.exists(CHROME)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def merged_tables(into=None):
    """The string tables with the pending new keys added (a key already in a table wins), as
    {"en": {...}, "th": {...}}. With into, also written there as en.json and th.json."""
    pending = json.loads(read(PENDING)) if os.path.exists(PENDING) else {}
    out = {}
    for lang in ("en", "th"):
        doc = json.loads(read(os.path.join(I18N, lang + ".json")))
        for k, v in pending.get(lang, {}).items():
            # Thai fixes to existing keys are meant to replace the old text.
            if lang == "th" or k not in doc["strings"]:
                doc["strings"][k] = v
        out[lang] = doc["strings"]
        if into:
            with open(os.path.join(into, lang + ".json"), "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
            os.chmod(os.path.join(into, lang + ".json"), 0o644)
    return out


def rule(css, selector):
    """The declarations of the first rule whose selector list is exactly selector."""
    m = re.search(r"(?:^|[}\s])" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return m.group(1) if m else ""


def hex_lum(hx):
    hx = hx.lstrip("#")
    rgb = [int(hx[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((hex_lum(a), hex_lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def token(css, name, dark=False):
    """A token's value from tempered.css or the page (light block, or the prefers-color-scheme: dark block)."""
    blocks = re.split(r"@media \(prefers-color-scheme: dark\)", css)
    part = blocks[1] if dark else blocks[0]
    m = re.search(re.escape(name) + r":\s*(#[0-9a-fA-F]{6})", part)
    return m.group(1) if m else None


class TemperedInlineTest(unittest.TestCase):
    """forgedash puts tempered.css into the page; the page has no colour block of its own."""

    @classmethod
    def setUpClass(cls):
        cls.fd = td.forgedash_module()
        cls.page = read(PAGE)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def css_at(self, text):
        p = os.path.join(self.tmp, "t.css")
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_page_has_the_mark_and_no_colour_block_of_its_own(self):
        style = self.page.split("<style", 1)[1].split("</style>", 1)[0]
        self.assertEqual(style.count("/*__TEMPERED__*/"), 1)
        self.assertNotRegex(style, r":root\s*\{[^}]*--bg:")
        for name in ("--bg", "--panel", "--text", "--muted", "--line", "--accent", "--ok", "--bad", "--warn", "--rail"):
            self.assertNotRegex(style, re.escape(name) + r":\s*#", name)

    def test_tempered_css_is_inlined(self):
        with mock.patch.object(self.fd, "TEMPERED_CSS", TEMPERED):
            page = self.fd.page_template()
        self.assertNotIn("/*__TEMPERED__*/", page)
        self.assertIn(read(TEMPERED), page)
        self.assertIn("forge-cool", page)
        self.assertLess(page.index("--accent-fill"), page.index("--on-bad"))   # shared tokens come first

    def test_missing_or_odd_file_gives_the_fallback(self):
        for path in (os.path.join(self.tmp, "missing.css"),
                     self.css_at(":root { --bg: red; } </style><script>alert(1)</script>"),
                     self.css_at(":root { --bg: blue; }" + " " * (100 * 1024)),
                     self.css_at("@import 'x.css';"), self.css_at(":root { --bg: url(x); }"), self.css_at("  ")):
            with mock.patch.object(self.fd, "TEMPERED_CSS", path):
                css = self.fd.tempered_css()
            self.assertEqual(css, self.fd.TEMPERED_FALLBACK, path)
        fb = self.fd.TEMPERED_FALLBACK
        for name in ("--bg", "--accent-fill", "--on-accent", "--accent-text", "--bad", "--info", "--radius-s", "--font"):
            self.assertIn(name + ":", fb, name)
        self.assertNotIn("<", fb)

    def test_a_link_is_not_followed(self):
        link = os.path.join(self.tmp, "link.css")
        os.symlink(TEMPERED, link)
        with mock.patch.object(self.fd, "TEMPERED_CSS", link):
            self.assertEqual(self.fd.tempered_css(), self.fd.TEMPERED_FALLBACK)

    def test_override_only_with_the_test_switch(self):
        code = ("import os, sys; sys.path.insert(0, sys.argv[1]); import forgedash; print(forgedash.TEMPERED_CSS)")
        env = dict(os.environ, FORGE_TEMPERED_CSS="/tmp/x.css", FORGE_SCHEMA=td.SCHEMA)
        env.pop("FORGE_DASH_TEST", None)
        out = subprocess.run([sys.executable, "-c", code, LIB], env=env, capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "/usr/share/forge/tempered.css")
        env["FORGE_DASH_TEST"] = "1"
        out = subprocess.run([sys.executable, "-c", code, LIB], env=env, capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "/tmp/x.css")

    def test_served_page_carries_the_tokens(self):
        src = read(APP)
        dash = src.split("def dashboard_page():", 1)[1].split("\ndef ", 1)[0]
        if "page_template" not in dash and "inline_tempered" not in dash and "TEMPERED" not in dash:
            self.skipTest("forge-settings does not inline tempered.css into the Dashboard yet "
                          "(dashboard_page() should start from forgedash.page_template())")
        if os.geteuid() != 0:
            self.skipTest("needs root to run the server as the test uid")
        t = td.DashboardTest("test_home")
        t.setUp()
        try:
            t.open()
            self.assertNotIn("/*__TEMPERED__*/", t.page)
            self.assertIn("--accent-fill", t.page)
        finally:
            t.tearDown()


class PageCssTest(unittest.TestCase):
    """The look, checked in the page's own CSS."""

    @classmethod
    def setUpClass(cls):
        cls.page = read(PAGE)
        cls.css = cls.page.split("<style", 1)[1].split("</style>", 1)[0]
        cls.tempered = read(TEMPERED)

    def test_destructive_confirm_button_is_a_red_fill_with_readable_text(self):
        r = rule(self.css, ".btn.primary.danger")
        self.assertIn("background: var(--bad)", r)
        self.assertIn("border-color: var(--bad)", r)
        self.assertIn("color: var(--on-bad)", r)
        for dark in (False, True):
            bad = token(self.tempered, "--bad", dark)
            on = token(self.css, "--on-bad", dark)
            self.assertGreaterEqual(contrast(bad, on), 4.5, ("dark" if dark else "light", bad, on))

    def test_primary_buttons_and_pressed_chips_use_the_accent_fill(self):
        for sel in (".btn.primary", '.chip[aria-pressed="true"]'):
            r = rule(self.css, sel)
            self.assertIn("background: var(--accent-fill)", r, sel)
            self.assertIn("color: var(--on-accent)", r, sel)
        self.assertNotIn("color: #fff", self.css)
        for dark in (False, True):
            self.assertGreaterEqual(contrast(token(self.tempered, "--accent-fill", dark),
                                             token(self.tempered, "--on-accent", dark)), 4.5)

    def test_chips_have_a_hover_state(self):
        self.assertIn("color: var(--text)", rule(self.css, ".chip:hover"))

    def test_accent_text_where_the_accent_is_text(self):
        self.assertIn("color: var(--accent-text)", rule(self.css, ".brand h1 span"))
        self.assertIn("color: var(--accent-text)", rule(self.css, ".pill.accent"))

    def test_main_is_centred_and_the_rail_matches_settings(self):
        self.assertIn("margin: 0 auto", rule(self.css, "main"))
        self.assertIn("grid-template-columns: 248px", rule(self.css, ".shell"))
        self.assertIn("padding: 8px 10px", rule(self.css, ".rail nav a"))

    def test_corners(self):
        self.assertIn("border-radius: var(--radius-s)", rule(self.css, ".btn"))
        self.assertIn("border-radius: var(--radius-m)", rule(self.css, ".panel"))
        self.assertNotRegex(self.css, r"border-radius:\s*(?:12|14)px")

    def test_activity_time_column(self):
        self.assertIn("grid-template-columns: max-content 14px minmax(0, 1fr)", rule(self.css, ".ev"))

    def test_backup_facts_in_three_columns(self):
        self.assertIn("repeat(3, minmax(0, 1fr))", rule(self.css, ".facts-grid.three"))

    def test_live_dot_and_label(self):
        r = rule(self.css, ".live i")
        self.assertIn("width: 8px", r)
        self.assertIn("box-shadow: 0 0 0 3px", r)
        self.assertIn("color: var(--text)", rule(self.css, ".live"))

    def test_narrow_nav_fades_and_fifth_gauge_spans(self):
        narrow = self.css.split("@media (max-width: 760px)", 1)[1]
        self.assertIn("mask-image: linear-gradient", narrow)
        self.assertIn("overflow-x: auto", narrow)
        self.assertIn(".gauge:nth-child(5) { grid-column: span 2; }", narrow)

    def test_log_filters_one_height(self):
        self.assertIn("height: 32px", rule(self.css, ".filters select, .filters input"))
        self.assertIn("height: 32px", rule(self.css, ".filters .btn"))

    def test_confirm_body_keeps_line_breaks(self):
        self.assertIn("white-space: pre-wrap", rule(self.css, "#c-body"))

    def test_activity_marks_are_tokens(self):
        marks = re.findall(r"\.ev \.mark[^{]*\{[^}]*\}", self.css)
        self.assertTrue(marks)
        for m in marks:
            self.assertNotRegex(m, r"#[0-9a-fA-F]{3,6}", m)
        self.assertIn("var(--info)", self.css)
        self.assertIsNotNone(token(self.css, "--violet"))
        self.assertIsNotNone(token(self.css, "--violet", dark=True))

    def test_motion_is_transform_and_opacity_only(self):
        for name, body in re.findall(r"@keyframes ([\w-]+)\s*\{(.*?\})\s*\}", self.css, re.S):
            props = set(re.findall(r"([a-z-]+)\s*:", body))
            self.assertLessEqual(props, {"opacity", "transform"}, name)
        self.assertNotIn("transition", self.css)
        for d in re.findall(r"animation:\s*[\w-]+\s+([^;]+);", self.css):
            self.assertRegex(d, r"var\(--dur-[123]\)|infinite", d)

    def test_no_em_dash_or_double_hyphen_in_prose(self):
        for path in (PAGE, FORGEDASH):
            text = read(path)
            self.assertNotIn("\u2014", text, path)
            prose = re.sub(r"(?m)^\s*(?://|#) -{4,} .* -{4,}$", "", text)   # section rulers in the code
            prose = prose.replace("<!--__I18N__-->", "")
            prose = re.sub(r"--[a-zA-Z0-9]", "", prose)    # CSS custom properties, command line options
            prose = re.sub(r"\w--", "", prose)             # a decrement
            self.assertNotIn("--", prose, path)


class PageScriptTest(unittest.TestCase):
    """The page's behaviour, read from its code."""

    @classmethod
    def setUpClass(cls):
        page = read(PAGE)
        cls.page = page
        cls.script = re.search(r"<script nonce=\"__NONCE__\">(.*?)</script>", page, re.S).group(1)

    def test_live_indicator_is_on_home_only(self):
        home = self.page.split('id="p-home"', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="live"', home)
        self.assertEqual(self.page.count('id="live"'), 1)

    def test_hostname_is_shown_once(self):
        self.assertNotIn("home-when", self.page)
        self.assertEqual(self.script.count('$("#host").textContent'), 1)

    def test_underscores_all_replaced(self):
        self.assertNotIn('.replace("_", " ")', self.script)
        self.assertIn('.replaceAll("_", " ")', self.script)

    def test_sizes_in_1024s_and_log_times_in_the_page_language(self):
        size = self.script.split("function size(b)", 1)[1].split("\n}", 1)[0]
        self.assertIn("b >= 1024", size)
        self.assertNotIn("1000", size)
        self.assertIn("toLocaleString(LOCALE", self.script.split("const logWhen", 1)[1].split("\n", 1)[0])
        self.assertNotIn("toLocaleString([]", self.script)

    def test_results_live_outside_the_rebuilt_panels(self):
        for rid in ("backup-out", "sync-out", "hub-out", "approvals-out", "shared-out", "fix-out", "rollback-out"):
            self.assertIn(f'id="{rid}"', self.page, rid)
        self.assertNotIn('const out = h("div", "result")', self.script)

    def test_pkexec_commands_are_behind_a_disclosure(self):
        self.assertIn('<details id="c-more"', self.page)
        self.assertIn('opts.command.startsWith("pkexec ")', self.script)

    def test_hidden_window_refreshes_slowly(self):
        self.assertIn("hidden: 300000", self.script)
        self.assertIn("hidden ? REFRESH.hidden", self.script)

    def test_every_new_string_has_english_and_thai(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import test_i18n
        tables = merged_tables()
        ids = {i for i in re.findall(r"""\bt\(\s*["']([a-z0-9_.-]+)["']""", self.page)}
        ids |= set(re.findall(r'data-i18n(?:-[a-z-]+)?="([a-z0-9_.-]+)"', self.page))
        ids = {i for i in ids if not i.endswith(".")}
        missing = sorted(i for i in ids if i not in tables["en"] or i not in tables["th"])
        self.assertEqual(missing, [])
        for lang in ("en", "th"):
            for k in ids:
                v = tables[lang][k]
                self.assertNotIn("\u2014", v, k)
                self.assertNotRegex(v, r"--(?![a-z])", k)
                self.assertNotIn("!", v, k)
                if lang == "th":
                    self.assertNotRegex(v, "ครับ|ค่ะ", k)
        for k in ids & set(tables["th"]):
            self.assertEqual(sorted(test_i18n.PLACEHOLDER.findall(tables["en"][k])),
                             sorted(test_i18n.PLACEHOLDER.findall(tables["th"][k])), k)

    def test_thai_names(self):
        th = merged_tables()["th"]
        self.assertEqual(th["dash.brand"], "แดชบอร์ด")
        self.assertEqual(th["dash.live"], "อัปเดตสด")
        self.assertEqual(th["dash.na"], "ไม่มีข้อมูล")
        self.assertEqual(th["ui.applying"], "กำลังนำไปใช้")
        self.assertRegex(th["dash.nav.hub"], r"[฀-๿]")
        self.assertNotIn("Dashboard", " ".join(v for k, v in th.items() if k.startswith("dash.")))


DRIVER = r'''
import json, sys
from playwright.sync_api import sync_playwright
url, scenario, tempered = sys.argv[1], sys.argv[2], sys.argv[3]
out = {}
LUM = """(c) => { const m = c.match(/[\\d.]+/g).slice(0, 3).map(Number).map(v => v / 255)
  .map(v => v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
  return 0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]; }"""
CONTRAST = "(el) => { const L = " + LUM + "; const s = getComputedStyle(el); const a = L(s.color), b = L(s.backgroundColor);" \
           " return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); }"

def ready(pg):
    pg.wait_for_selector("#nav a")
    # forge-settings may not inline the shared tokens yet: then put them in for the checks.
    if pg.evaluate("document.querySelector('style').textContent.includes('/*__TEMPERED__*/')"):
        pg.add_style_tag(content=open(tempered).read())
    pg.wait_for_timeout(900)

def go(pg, page, wait=700):
    pg.evaluate(f"location.hash = '#{page}'")
    pg.wait_for_timeout(wait)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    scheme = "dark" if scenario.endswith("-dark") else "light"
    motion = "reduce" if scenario == "reduced" else "no-preference"
    ctx = b.new_context(viewport={"width": 1280, "height": 800}, color_scheme=scheme, reduced_motion=motion,
                        bypass_csp=True)
    ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)[:300]))
    if scenario == "polling":
        pg.add_init_script("""(() => { const si = window.setInterval, ci = window.clearInterval;
          window.__iv = {}; window.setInterval = (f, ms, ...a) => { const id = si(f, ms, ...a); window.__iv[id] = ms; return id; };
          window.clearInterval = id => { delete window.__iv[id]; ci(id); }; })()""")
    pg.goto(url)
    ready(pg)
    if scenario == "main":
        out["live_home"] = pg.is_visible("#live")
        out["live_color_is_text"] = pg.evaluate("getComputedStyle(document.querySelector('#live')).color === getComputedStyle(document.body).color")
        out["host_count"] = pg.evaluate("[...document.querySelectorAll('body *')].filter(e => e.children.length === 0 && e.textContent.trim() === document.querySelector('#host').textContent.trim()).length")
        out["recent_titles"] = pg.eval_on_selector_all("#home-recent .ev > div > div:first-child", "ds => ds.map(d => d.textContent)")
        out["recent_marks_x"] = pg.eval_on_selector_all("#home-recent .ev .mark", "ms => ms.map(m => Math.round(m.getBoundingClientRect().left))")
        go(pg, "backups", 1200)
        out["live_backups"] = pg.is_visible("#live")
        out["focus_after_switch"] = pg.evaluate("document.activeElement === document.querySelector('#p-backups h2')")
        out["scroll_after_switch"] = pg.evaluate("window.scrollY")
        out["fact_columns"] = pg.evaluate("getComputedStyle(document.querySelector('#backup .facts-grid')).gridTemplateColumns.split(' ').length")
        out["sizes"] = pg.eval_on_selector_all("#backup .fact .v", "vs => vs.map(v => v.textContent)")
        out["backup_rows"] = pg.eval_on_selector_all("#backup .row", "rs => rs.map(r => r.innerText)")
        out["restore_code"] = pg.eval_on_selector_all("#backup .cmd code", "cs => cs.map(c => c.textContent)")
        out["restore_copy"] = pg.eval_on_selector_all("#backup .cmd button", "bs => bs.map(b => b.textContent)")
        # The confirm dialog: Esc and the backdrop close it, the body keeps its line breaks.
        pg.click("#backup .btn.primary")
        pg.wait_for_timeout(300)
        out["dialog_open"] = pg.evaluate("document.querySelector('#confirm').open")
        out["dialog_plain_cmd"] = pg.is_visible("#c-cmd") and pg.inner_text("#c-cmd")
        out["dialog_more_hidden"] = not pg.is_visible("#c-more")
        out["body_ws"] = pg.evaluate("getComputedStyle(document.querySelector('#c-body')).whiteSpace")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        out["closed_by_esc"] = not pg.evaluate("document.querySelector('#confirm').open")
        pg.click("#backup .btn.primary")
        pg.wait_for_timeout(300)
        pg.mouse.click(5, 5)
        pg.wait_for_timeout(300)
        out["closed_by_backdrop"] = not pg.evaluate("document.querySelector('#confirm').open")
        pg.click("#backup .btn.primary")
        pg.wait_for_timeout(300)
        pg.click("#c-ok")
        pg.wait_for_timeout(800)
        out["result_before"] = pg.inner_text("#backup-out")
        # A refresh keeps the result and the keyboard focus.
        pg.focus("#backup .row:last-child .btn")
        out["focus_text_before"] = pg.evaluate("document.activeElement.textContent")
        pg.evaluate("loadBackups()")
        pg.wait_for_timeout(800)
        out["result_after"] = pg.inner_text("#backup-out")
        out["focus_text_after"] = pg.evaluate("document.activeElement.textContent")
        out["focus_in_backup"] = pg.evaluate("document.querySelector('#backup').contains(document.activeElement)")
        # Copy: a check mark, then the label again after 2 seconds.
        pg.click("#backup .cmd button")
        pg.wait_for_timeout(200)
        out["copy_now"] = pg.inner_text("#backup .cmd button")
        out["clip"] = pg.evaluate("navigator.clipboard.readText()")
        pg.wait_for_timeout(2200)
        out["copy_later"] = pg.inner_text("#backup .cmd button")
        go(pg, "updates", 1000)
        out["auto_row"] = pg.eval_on_selector_all("#releases .row", "rs => rs.map(r => r.innerText)")
        go(pg, "activity", 1000)
        out["titles"] = pg.eval_on_selector_all("#timeline .ev > div > div:first-child", "ds => ds.map(d => d.textContent)")
        out["marks_x"] = pg.eval_on_selector_all("#timeline .ev .mark", "ms => ms.map(m => Math.round(m.getBoundingClientRect().left))")
        out["agent_mark"] = pg.evaluate("getComputedStyle(document.querySelector('#timeline .mark.agent')).backgroundColor")
        # The pressed chip keeps the focus when the chips are drawn again.
        pg.focus("#kinds .chip:nth-child(2)")
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(300)
        out["chip_focus"] = pg.evaluate("document.activeElement.getAttribute('aria-pressed')")
        out["chip_contrast"] = pg.evaluate(CONTRAST, pg.query_selector("#kinds .chip[aria-pressed=true]"))
        go(pg, "hub", 1000)
        out["hub_apps"] = pg.eval_on_selector_all("#hub .row .t", "ts => ts.map(t => t.textContent)")
        go(pg, "logs", 1500)
        out["filter_heights"] = pg.eval_on_selector_all(".filters select, .filters input, .filters .btn", "es => es.map(e => e.offsetHeight)")
        out["filter_bottoms"] = pg.eval_on_selector_all(".filters select, .filters input, .filters .btn", "es => es.map(e => Math.round(e.getBoundingClientRect().bottom))")
        # Errors: an icon and a button that loads the panel again.
        pg.route("**/api/dash/sync", lambda r: r.fulfill(status=500, body="{}"))
        go(pg, "sync", 1000)
        out["sync_error"] = pg.locator("#sync .error").count()
        out["sync_error_icon"] = pg.locator("#sync .error .ico").count()
        out["sync_retry"] = pg.eval_on_selector_all("#sync .error button", "bs => bs.map(b => b.textContent)")
        pg.unroute("**/api/dash/sync")
        pg.click("#sync .error button")
        pg.wait_for_timeout(1000)
        out["sync_after_retry"] = pg.locator("#sync .error").count()
        out["sync_rows"] = pg.locator("#sync .row").count()
        # Skeleton rows while a panel loads.
        held = []
        pg.route("**/api/dash/vms", lambda r: held.append(r))
        go(pg, "vms", 500)
        out["vms_skel"] = pg.locator("#vms .skel").count()
        out["vms_min_height"] = pg.evaluate("parseFloat(getComputedStyle(document.querySelector('#vms')).minHeight)")
        pg.unroute("**/api/dash/vms")
        for r in held:
            try:
                r.continue_()
            except Exception:
                pass   # unroute may already have let it through
        pg.wait_for_timeout(800)
        out["vms_skel_after"] = pg.locator("#vms .skel").count()
        # Approvals: the list you entered your password for stays through a refresh.
        go(pg, "agents", 800)
        pg.click("#approvals .btn.primary")
        pg.wait_for_timeout(1200)
        out["approvals_rows"] = pg.locator("#approvals .row").count()
        pg.evaluate("loadAgents()")
        pg.wait_for_timeout(800)
        out["approvals_rows_after"] = pg.locator("#approvals .row").count()
        pg.click("#approvals .btn.danger")
        pg.wait_for_timeout(300)
        out["deny_plain_hidden"] = not pg.is_visible("#c-plain")
        out["deny_more_visible"] = pg.is_visible("#c-more summary")
        out["deny_more_closed"] = not pg.evaluate("document.querySelector('#c-more').open")
        out["deny_cmd"] = pg.evaluate("document.querySelector('#c-cmd-root').textContent")
        out["deny_bg_is_bad"] = pg.evaluate("getComputedStyle(document.querySelector('#c-ok')).backgroundColor === getComputedStyle(document.querySelector('#c-ok')).borderTopColor")
        out["deny_contrast"] = pg.evaluate(CONTRAST, pg.query_selector("#c-ok"))
        pg.keyboard.press("Escape")
        out["primary_contrast"] = pg.evaluate(CONTRAST, pg.query_selector("#approvals .btn.primary"))
        pg.set_viewport_size({"width": 1920, "height": 1000})
        pg.wait_for_timeout(200)
        out["main_margins"] = pg.evaluate("(() => { const m = document.querySelector('main').getBoundingClientRect(); const s = document.querySelector('.rail').getBoundingClientRect(); return [Math.round(m.left - s.right), Math.round(innerWidth - m.right)]; })()")
        pg.set_viewport_size({"width": 700, "height": 900})
        go(pg, "home", 1000)
        out["narrow_nav"] = pg.evaluate("(() => { const s = getComputedStyle(document.querySelector('#nav')); return [s.overflowX, s.maskImage || s.webkitMaskImage]; })()")
        out["fifth_gauge"] = pg.evaluate("Math.round(document.querySelector('#g-temp').getBoundingClientRect().width) === Math.round(document.querySelector('#strip').getBoundingClientRect().width - 2)")
    elif scenario in ("contrast-dark", "contrast-light"):
        go(pg, "agents", 800)
        pg.click("#approvals .btn.primary")
        pg.wait_for_timeout(1200)
        out["primary_contrast"] = pg.evaluate(CONTRAST, pg.query_selector("#approvals .btn.primary"))
        pg.click("#approvals .btn.danger")
        pg.wait_for_timeout(300)
        out["deny_contrast"] = pg.evaluate(CONTRAST, pg.query_selector("#c-ok"))
    elif scenario in ("tween", "reduced"):
        pg.evaluate("clearInterval(metricTimer)")
        pg.evaluate("setGauge(GAUGES[0], Object.assign({}, latest, {cpu_pct: 90}))")
        out["now"] = pg.evaluate("document.querySelector('#g-cpu .big').firstChild.data")
        pg.wait_for_timeout(450)
        out["later"] = pg.evaluate("document.querySelector('#g-cpu .big').firstChild.data")
    elif scenario == "polling":
        out["visible"] = sorted(pg.evaluate("Object.values(window.__iv)"))
        pg.evaluate("Object.defineProperty(document, 'hidden', {configurable: true, get: () => true}); document.dispatchEvent(new Event('visibilitychange'))")
        out["hidden"] = sorted(pg.evaluate("Object.values(window.__iv)"))
        out["hidden_label"] = pg.inner_text("#live")
        pg.evaluate("Object.defineProperty(document, 'hidden', {configurable: true, get: () => false}); document.dispatchEvent(new Event('visibilitychange'))")
        pg.wait_for_timeout(500)
        out["back"] = sorted(pg.evaluate("Object.values(window.__iv)"))
        go(pg, "backups", 500)
        out["other_page"] = sorted(pg.evaluate("Object.values(window.__iv)"))
    elif scenario == "thai":
        out["brand"] = pg.inner_text(".brand h1 span")
        out["hub"] = pg.inner_text('#nav a[data-page="hub"] .n')
        out["live"] = pg.inner_text("#live")
        out["log_when"] = pg.evaluate("logWhen(1763550000)")
        out["na"] = pg.evaluate("(setGauge(GAUGES[1], Object.assign({}, latest, {gpu_util_pct: null})), document.querySelector('#g-gpu .big').textContent)")
        out["titles"] = pg.eval_on_selector_all("#home-recent .ev > div > div:first-child", "ds => ds.map(d => d.textContent)")
    out["errors"] = errors
    b.close()
print(json.dumps(out))
'''


@unittest.skipUnless(HAVE, "needs root, Playwright and Chrome")
class BrowserTest(unittest.TestCase):
    def setUp(self):
        self.dash = td.DashboardTest("test_home")
        self.dash.setUp()
        self.addCleanup(self.dash.tearDown)
        tmp = self.dash.tmp
        self.i18n = os.path.join(tmp, "i18n")
        os.mkdir(self.i18n)
        os.chmod(self.i18n, 0o755)
        merged_tables(self.i18n)
        self.home = os.path.join(tmp, "home")
        os.mkdir(self.home)
        os.chown(self.home, OWNER, OWNER)
        self.driver = os.path.join(tmp, "driver.py")
        with open(self.driver, "w") as f:
            f.write(DRIVER)
        os.chmod(self.driver, 0o644)
        self.dash.env.update(FORGE_I18N_DIR=self.i18n, FORGE_TEMPERED_CSS=TEMPERED)

    def run_page(self, scenario, lang="C.UTF-8"):
        self.dash.env["LANG"] = lang
        base, k = self.dash.start()
        env = dict(self.dash.env, HOME=self.home)
        r = subprocess.run([sys.executable, self.driver, f"{base}/?k={k}", scenario, TEMPERED], env=env,
                           preexec_fn=as_uid(OWNER), capture_output=True, text=True, timeout=240)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out.pop("errors"), [])
        return out

    def test_main_flows(self):
        o = self.run_page("main")
        # 54: the live indicator only on Home, its label in the text colour (62)
        self.assertTrue(o["live_home"])
        self.assertFalse(o["live_backups"])
        self.assertTrue(o["live_color_is_text"])
        # 56: the PC name once
        self.assertEqual(o["host_count"], 1)
        # 58: titles from the string tables
        self.assertEqual(o["recent_titles"], ["Mode changed to Gaming by owner", "Agent system-checkup: done"])
        self.assertEqual(o["titles"], o["recent_titles"])
        # 57: one time column for every row
        self.assertEqual(len(set(o["recent_marks_x"])), 1, o["recent_marks_x"])
        self.assertEqual(len(set(o["marks_x"])), 1, o["marks_x"])
        self.assertNotEqual(o["agent_mark"], "rgb(138, 92, 208)")
        # 72: a page switch starts at the top with the heading focused
        self.assertTrue(o["focus_after_switch"])
        self.assertEqual(o["scroll_after_switch"], 0)
        # 60, 77: three columns of facts, sizes in 1024s (52 000 000 000 bytes is 48 GB)
        self.assertEqual(o["fact_columns"], 3)
        self.assertIn("48 GB", o["sizes"])
        self.assertIn("373 GB", o["sizes"])
        # 61: Back up now says what it does; restoring has its own row with Copy
        now = next(r for r in o["backup_rows"] if r.startswith("Back up now"))
        self.assertIn("Saves what changed in your home folder", now)
        self.assertNotIn("Restore from a terminal", now)
        self.assertEqual(o["restore_code"], ["forge-backup restore latest --target ~/Restored"])
        self.assertEqual(o["restore_copy"], ["Copy"])
        # 67, 73: the dialog
        self.assertTrue(o["dialog_open"])
        self.assertEqual(o["dialog_plain_cmd"], "forge-backup run")
        self.assertTrue(o["dialog_more_hidden"])
        self.assertEqual(o["body_ws"], "pre-wrap")
        self.assertTrue(o["closed_by_esc"])
        self.assertTrue(o["closed_by_backdrop"])
        # 70: the result and the focus survive a refresh
        self.assertIn("Backup started", o["result_before"])
        self.assertEqual(o["result_after"], o["result_before"])
        self.assertEqual(o["focus_text_before"], "Test now")
        self.assertEqual(o["focus_text_after"], "Test now")
        self.assertTrue(o["focus_in_backup"])
        # 65, 75: Copy with a check mark, back after 2 seconds
        self.assertIn("✓", o["copy_now"])
        self.assertIn("Copied", o["copy_now"])
        self.assertEqual(o["clip"], "forge-backup restore latest --target ~/Restored")
        self.assertEqual(o["copy_later"], "Copy")
        # 59: no raw "ok" for the automatic updates
        auto = next(r for r in o["auto_row"] if r.startswith("Automatic updates"))
        self.assertNotRegex(auto, r"(?m)^ok$")
        self.assertIn("Worked", auto)
        # 81: the pressed chip keeps the focus and is readable
        self.assertEqual(o["chip_focus"], "true")
        self.assertGreaterEqual(o["chip_contrast"], 4.5)
        # 80: app names
        self.assertEqual(o["hub_apps"][-2:-1], ["Blender"])
        # 64: one height for the log filters, on one line
        self.assertEqual(set(o["filter_heights"]), {32})
        self.assertEqual(len(set(o["filter_bottoms"])), 1, o["filter_bottoms"])
        # 82: an error with an icon and Try again, which loads the panel again
        self.assertEqual(o["sync_error"], 1)
        self.assertEqual(o["sync_error_icon"], 1)
        self.assertEqual(o["sync_retry"], ["Try again"])
        self.assertEqual(o["sync_after_retry"], 0)
        self.assertGreater(o["sync_rows"], 0)
        # 71: skeleton rows while loading
        self.assertGreater(o["vms_skel"], 0)
        self.assertGreaterEqual(o["vms_min_height"], 100)
        self.assertEqual(o["vms_skel_after"], 0)
        # 70: the approvals list stays; 66: pkexec behind Show command; 51: a readable red button
        self.assertEqual(o["approvals_rows"], 2)
        self.assertEqual(o["approvals_rows_after"], 2)
        self.assertTrue(o["deny_plain_hidden"])
        self.assertTrue(o["deny_more_visible"])
        self.assertTrue(o["deny_more_closed"])
        self.assertTrue(o["deny_cmd"].startswith("pkexec forge-api POST /api/approvals/"))
        self.assertTrue(o["deny_bg_is_bad"])
        self.assertGreaterEqual(o["deny_contrast"], 4.5)
        self.assertGreaterEqual(o["primary_contrast"], 4.5)
        # 55: centred on a wide screen
        left, right = o["main_margins"]
        self.assertLessEqual(abs(left - right), 2, o["main_margins"])
        # 63: narrow
        self.assertEqual(o["narrow_nav"][0], "auto")
        self.assertIn("linear-gradient", o["narrow_nav"][1])
        self.assertTrue(o["fifth_gauge"])

    def test_dark_buttons_are_readable(self):
        o = self.run_page("contrast-dark")
        self.assertGreaterEqual(o["deny_contrast"], 4.5)
        self.assertGreaterEqual(o["primary_contrast"], 4.5)

    def test_gauge_numbers_move_and_reduced_motion_jumps(self):
        o = self.run_page("tween")
        self.assertNotEqual(o["now"], "90")
        self.assertEqual(o["later"], "90")
        o = self.run_page("reduced")
        self.assertEqual(o["now"], "90")

    def test_hidden_window_polls_slowly(self):
        o = self.run_page("polling")
        self.assertIn(4000, o["visible"])
        self.assertIn(60000, o["visible"])
        self.assertNotIn(4000, o["hidden"])
        self.assertIn(300000, o["hidden"])
        self.assertNotIn(60000, o["hidden"])
        self.assertEqual(o["hidden_label"], "Live updates paused")
        self.assertIn(4000, o["back"])
        self.assertIn(60000, o["back"])
        self.assertEqual(o["other_page"].count(30000), 2)   # the page refresh and the keep-alive ping
        self.assertNotIn(4000, o["other_page"])

    def test_thai(self):
        o = self.run_page("thai", "th_TH.UTF-8")
        self.assertEqual(o["brand"], "แดชบอร์ด")
        self.assertRegex(o["hub"], r"[฀-๿]")
        self.assertEqual(o["live"], "อัปเดตสด")
        self.assertRegex(o["log_when"], r"[฀-๿]")
        self.assertEqual(o["na"], "ไม่มีข้อมูล")
        self.assertEqual(o["titles"][0], "owner เปลี่ยนเป็นโหมดเล่นเกม")


if __name__ == "__main__":
    unittest.main()
