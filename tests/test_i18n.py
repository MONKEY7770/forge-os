"""Thai and English text for Forge Settings and the Forge Dashboard (forgei18n and the string tables).

Mechanism tests: language choice, the per user switch, the tables and the JSON put into the pages.
Coverage tests: every text the pages show has an English and a Thai entry, the two agree on placeholders,
and neither has an em dash or a double hyphen.
"""
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
I18N = os.path.join(ROOT, "files/system/usr/share/forge/i18n")
SETTINGS_SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
LOOK_SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/appearance.json")
APP = os.path.join(ROOT, "files/system/usr/bin/forge-settings")
DASH = os.path.join(LIB, "dashboard.html")
PREVIEWS = os.path.join(ROOT, "files/system/usr/share/forge/appearance-preview")

sys.path.insert(0, LIB)

ID_RE = re.compile(r"[a-z0-9_.-]{1,120}")
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def table(lang):
    with open(os.path.join(I18N, lang + ".json"), encoding="utf-8") as f:
        return json.load(f)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(self.tmp, "config"),
                                                "FORGE_I18N_DIR": I18N}, clear=False)
        self.env.start()
        for k in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            os.environ.pop(k, None)
        import forgei18n
        self.i = importlib.reload(forgei18n)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class LanguageTest(Base):
    def test_system_locale_decides_when_the_choice_is_auto(self):
        self.assertEqual(self.i.system_language({}), "en")
        self.assertEqual(self.i.system_language({"LANG": "th_TH.UTF-8"}), "th")
        self.assertEqual(self.i.system_language({"LANG": "en_US.UTF-8", "LC_MESSAGES": "th_TH.UTF-8"}), "th")
        self.assertEqual(self.i.system_language({"LANG": "th_TH.UTF-8", "LC_ALL": "C.UTF-8"}), "en")
        self.assertEqual(self.i.system_language({"LANGUAGE": "th:en_US", "LANG": "en_US.UTF-8"}), "th")
        self.assertEqual(self.i.system_language({"LANGUAGE": "en_US:th", "LANG": "th_TH.UTF-8"}), "en")
        self.assertEqual(self.i.system_language({"LANG": "de_DE.UTF-8"}), "en")
        self.assertEqual(self.i.system_language({"LANG": "thx"}), "en")

    def test_the_switch_is_saved_per_user_and_wins_over_the_locale(self):
        os.environ["LANG"] = "en_US.UTF-8"
        self.assertEqual(self.i.preference(), "auto")
        self.assertEqual(self.i.language(), "en")
        self.i.set_preference("th")
        self.assertEqual(self.i.language(), "th")
        p = os.path.join(self.tmp, "config", "forge", "ui.json")
        self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(os.path.dirname(p)).st_mode & 0o777), "0o700")
        self.i.set_preference("auto")
        self.assertEqual(self.i.language(), "en")
        for bad in ("fr", "", None, 1, "th\n", "../th"):
            with self.assertRaises(ValueError):
                self.i.set_preference(bad)

    def test_a_broken_or_odd_preference_file_means_auto(self):
        p = os.path.join(self.tmp, "config", "forge", "ui.json")
        os.makedirs(os.path.dirname(p))
        for text in ("{", '{"language": "xx"}', '["th"]', "x" * 100000):
            with open(p, "w") as f:
                f.write(text)
            self.assertEqual(self.i.preference(), "auto", text[:20])
        os.unlink(p)
        os.symlink("/etc/passwd", p)
        self.assertEqual(self.i.preference(), "auto")
        with self.assertRaises(OSError):
            self.i.set_preference("th")          # never writes through a link
        self.assertTrue(os.path.islink(p))


class TableTest(Base):
    def test_tables_load_and_thai_falls_back_to_english(self):
        en, th = self.i.table("en"), self.i.table("th")
        self.assertEqual(en["ui.nav.appearance"], "Appearance")
        self.assertNotEqual(th["ui.nav.appearance"], "Appearance")
        with self.assertRaises(ValueError):
            self.i.table("../en")

    def test_broken_table_files_give_an_empty_table(self):
        d = os.path.join(self.tmp, "i18n")
        os.mkdir(d)
        os.environ["FORGE_I18N_DIR"] = d
        i = importlib.reload(self.i)
        for text in ("{", '{"strings": []}', '{"strings": {"Bad Id": "x"}}', '{"strings": {"a": 1}}'):
            with open(os.path.join(d, "th.json"), "w") as f:
                f.write(text)
            self.assertEqual(i.table("th"), {}, text)

    def test_page_json_is_safe_inside_a_script_element(self):
        blob = self.i.page_json("th", {"x.y": "</script><script>alert(1)</script>& "})
        self.assertNotIn("<", blob)
        self.assertNotIn(">", blob)
        self.assertNotIn("&", blob)
        self.assertNotIn(" ", blob)
        data = json.loads(blob)
        self.assertEqual(data["lang"], "th")
        self.assertEqual(data["strings"]["x.y"], "</script><script>alert(1)</script>& ")
        self.assertIn("ui.nav.appearance", data["fallback"])

    def test_tr_is_fast_on_long_lines_and_still_translates(self):
        """Review proof (rvLook, redos.js): a 16 KB server line took 21 s in Thai. Long lines are left as they are,
        and matching has no regular expression backtracking."""
        import shutil as sh
        import subprocess
        import time
        node = sh.which("node") or ("/opt/node22/bin/node" if os.path.exists("/opt/node22/bin/node") else None)
        if not node:
            self.skipTest("node is needed")
        script = os.path.join(self.tmp, "tr.js")
        with open(script, "w") as f:
            f.write("const data = require('fs').readFileSync(process.argv[2], 'utf8');\n"
                    "global.document = {documentElement: {}, getElementById: () => ({textContent: data}), "
                    "querySelector: () => null};\n" + self.i.I18N_JS +
                    "\nconst out = {};\nlet t0 = Date.now();\n"
                    "for (const n of [600, 8000, 16000]) tr('x, y: '.repeat(Math.floor(n / 6)));\n"
                    "for (let i = 0; i < 200; i++) tr('a: b: c: d: e: f: g: h: '.repeat(10));\n"
                    "out.ms = Date.now() - t0;\n"
                    "out.a = tr('Not added: the file is empty.');\n"
                    "out.b = tr('Theme: Colors: not changed, the font Comic is not installed on this PC.');\n"
                    "out.c = tr('dock.size: must be between 24 and 96\\nUnknown text stays');\n"
                    "console.log(JSON.stringify(out));\n")
        data = os.path.join(self.tmp, "th.json")
        with open(data, "w", encoding="utf-8") as f:
            f.write(self.i.page_json("th"))
        r = subprocess.run([node, script, data], capture_output=True, text=True, timeout=120)
        out = json.loads(r.stdout)
        self.assertLess(out["ms"], 2000)
        self.assertEqual(out["a"], "ไม่ได้เพิ่ม: ไฟล์ว่างเปล่า")
        self.assertEqual(out["b"], "ธีม: สี: ไม่ได้เปลี่ยน ไม่ได้ติดตั้งฟอนต์ Comic ในพีซีเครื่องนี้")
        self.assertEqual(out["c"], "dock.size: ต้องอยู่ระหว่าง 24 ถึง 96\nUnknown text stays")

    def test_page_block_has_the_nonce_and_the_runtime(self):
        block = self.i.page_block("th", "NONCE123")
        self.assertIn('<script type="application/json" id="forge-i18n" nonce="NONCE123">', block)
        self.assertIn('<script nonce="NONCE123">', block)
        self.assertIn("function applyStatic", block)
        self.assertNotRegex(block, r"<script(?![^>]*nonce=)")


def schema_strings():
    """(id, English text) for every label and help text in the two schemas, and appearance option labels."""
    out = []
    with open(SETTINGS_SCHEMA) as f:
        s = json.load(f)
    for g in s["groups"]:
        out.append((f"set.group.{g['id']}", g["label"]))
    for x in s["settings"]:
        out.append((f"set.{x['key']}.label", x["label"]))
        if x.get("help"):
            out.append((f"set.{x['key']}.help", x["help"]))
        for o in x.get("options", []):
            out.append((f"set.opt.{o}", o))
    with open(LOOK_SCHEMA) as f:
        a = json.load(f)
    for sec in a["sections"]:
        out.append((f"look.{sec['id']}.label", sec["label"]))
        if sec.get("help"):
            out.append((f"look.{sec['id']}.help", sec["help"]))
        for fd in sec["fields"]:
            base = f"look.{sec['id']}.f.{fd['key']}"
            out.append((base + ".label", fd["label"]))
            if fd.get("help"):
                out.append((base + ".help", fd["help"]))
            for o in fd.get("options", []):
                out.append((f"{base}.opt.{o['value']}", o["label"]))
            if fd.get("unit"):
                out.append(("look.unit." + ("pct" if fd["unit"] == "%" else fd["unit"]), fd["unit"]))
    return out


def page_ids():
    """Every string id the pages use: t("..."), tn("..."), data-i18n*="..." attributes."""
    ids = set()
    sources = [APP, DASH, os.path.join(LIB, "forgei18n.py")]
    if os.path.isdir(PREVIEWS):
        sources += [os.path.join(PREVIEWS, n) for n in os.listdir(PREVIEWS) if n.endswith(".js")]
    for path in sources:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        ids |= set(re.findall(r"""\bt\(\s*["']([a-z0-9_.-]+)["']""", text))
        ids |= {i + ".one" for i in re.findall(r"""\btn\(\s*["']([a-z0-9_.-]+)["']""", text)}
        ids |= {i + ".other" for i in re.findall(r"""\btn\(\s*["']([a-z0-9_.-]+)["']""", text)}
        ids |= set(re.findall(r'data-i18n(?:-[a-z-]+)?="([a-z0-9_.-]+)"', text))
    # Ids the pages build from a prefix and a known list.
    ids = {i for i in ids if not i.endswith(".")}
    ids |= {f"ui.nav.{s}" for s in ("appearance", "system", "network", "privacy", "ai", "data")}
    ids |= {f"ui.sec.{s}.help" for s in ("appearance", "system", "network", "privacy", "ai", "data")}
    ids |= {f"preset.state.{s}" for s in ("applied", "missing", "failed")}
    return ids


class CoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.en, cls.th = table("en"), table("th")

    def test_files_have_the_right_shape(self):
        for lang, t in (("en", self.en), ("th", self.th)):
            self.assertEqual(t["lang"], lang)
            self.assertIsInstance(t["strings"], dict)
            for k, v in t["strings"].items():
                self.assertRegex(k, ID_RE, k)
                self.assertIsInstance(v, str, k)
                self.assertTrue(v.strip(), k)

    def test_every_english_id_has_thai_and_the_other_way_round(self):
        en, th = set(self.en["strings"]), set(self.th["strings"])
        self.assertEqual(sorted(en - th), [], "English ids without Thai")
        self.assertEqual(sorted(th - en - {i for i, _ in schema_strings()}), [], "Thai ids without English")

    def test_every_id_the_pages_use_exists(self):
        missing = sorted(i for i in page_ids() if i not in self.en["strings"] or i not in self.th["strings"])
        self.assertEqual(missing, [])

    def test_every_schema_label_help_and_option_has_thai(self):
        missing = sorted(i for i, _ in schema_strings() if i not in self.th["strings"])
        self.assertEqual(missing, [])

    def test_placeholders_match(self):
        for k, v in self.en["strings"].items():
            if k in self.th["strings"]:
                self.assertEqual(sorted(PLACEHOLDER.findall(v)), sorted(PLACEHOLDER.findall(self.th["strings"][k])), k)

    def test_thai_is_thai(self):
        """Most Thai entries hold Thai letters (names like Plasma or Steam may stay Latin)."""
        thai = sum(1 for v in self.th["strings"].values() if re.search(r"[฀-๿]", v))
        self.assertGreater(thai / len(self.th["strings"]), 0.8)

    def test_no_em_dash_or_double_hyphen(self):
        for lang, t in (("en", self.en), ("th", self.th)):
            for k, v in t["strings"].items():
                self.assertNotIn("\u2014", v, f"{lang} {k}")
                # A command line option like --target is fine; "--" as punctuation is not.
                self.assertNotRegex(v, r"--(?![a-z])", f"{lang} {k}")

    def test_pages_have_no_untranslated_literal_text(self):
        """Text put into the page goes through t() (or comes from data). Catches new English literals."""
        with open(APP, encoding="utf-8") as f:
            page = f.read().split('PAGE = r"""', 1)[1]
        with open(DASH, encoding="utf-8") as f:
            dash = f.read()
        for name, text in (("forge-settings", page), ("dashboard.html", dash)):
            script = text.split("<script", 1)[1]
            hits = re.findall(r"""(?:text:\s*|textContent\s*=\s*|title:\s*|placeholder:\s*|"aria-label":\s*)["'`][A-Z][a-z]+ [a-z]""",
                              script)
            hits += re.findall(r"""\b(?:h|el|btn|row|empty|errorLine|pill)\([^)]{0,40}?["'][A-Z][a-z]+(?: [a-z]+){2}""", script)
            hits += re.findall(r"""\b(?:confirm|alert|prompt|note|show|status)\(\s*(?:(?:true|false|ok),\s*)?["'`][A-Z][a-z]+ [a-z]""", script)
            self.assertEqual(hits, [], name)


if __name__ == "__main__":
    unittest.main()
