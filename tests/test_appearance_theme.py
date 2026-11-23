"""Theme appearance (Settings, Appearance, Theme): colors, accent, icons, pointer, title bars, Plasma style, fonts.

The KDE tools are fakes here that record their argv (plasma-apply-colorscheme, plasma-apply-cursortheme,
plasma-apply-desktoptheme, plasma-changeicons, kwriteconfig6, kreadconfig6, dbus-send, fc-list). Not Plasma.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_settings_page import as_uid  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
BACKEND = os.path.join(ROOT, "files/system/usr/libexec/forge-appearance-theme")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/appearance.json")
PREVIEW = os.path.join(ROOT, "files/system/usr/share/forge/appearance-preview/theme.js")
FINALIZE = os.path.join(ROOT, "files/scripts/finalize.sh")
OWNER = 65533

sys.path.insert(0, LIB)

# Each fake tool appends {"tool": name, "argv": [...]} to $FAKE_LOG. kwriteconfig6 also keeps the values in
# $FAKE_STORE so kreadconfig6 can read them back. FAKE_FAIL=<tool> makes that tool exit 1.
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"tool": name, "argv": sys.argv[1:]}) + "\n")
store_path = os.environ["FAKE_STORE"]
try:
    store = json.load(open(store_path))
except (OSError, ValueError):
    store = {}
if name in os.environ.get("FAKE_FAIL", "").split(","):
    print("fake failure"); sys.exit(1)
a = sys.argv[1:]
def opt(n):
    return a[a.index(n) + 1] if n in a else None
if name == "kwriteconfig6":
    k = "%s/%s/%s" % (opt("--file"), opt("--group"), opt("--key"))
    if "--delete" in a:
        store.pop(k, None)
    else:
        store[k] = a[-1]
    json.dump(store, open(store_path, "w"))
elif name == "kreadconfig6":
    print(store.get("%s/%s/%s" % (opt("--file"), opt("--group"), opt("--key")), opt("--default") or ""))
elif name == "plasma-apply-colorscheme":
    if os.environ.get("FAKE_NOT_FOUND"):
        print('Could not find theme "%s". The theme should be one of the following options: BreezeLight' % a[-1])
    elif "--accent-color" in a:
        print("Successfully applied the accent color " + opt("--accent-color"))
    else:
        store["kdeglobals/General/ColorScheme"] = a[-1]
        json.dump(store, open(store_path, "w"))
        print("Successfully applied the color scheme %s to your current Plasma session" % a[-1])
elif name == "dbus-send":
    if "org.kde.PlasmaShell.color" in a:
        print("method return time=1 sender=:1.2 -> destination=:1.9 serial=5 reply_serial=2")
        print("   uint32 " + os.environ.get("FAKE_WALL_COLOR", "4282684346"))
elif name == "fc-list":
    if ":spacing=100" in a:
        print("Hack\nNoto Sans Mono\nBad,Name")
    else:
        print("Noto Sans\nNoto Sans Thai\nHack\nNoto Sans\nInter")
'''


def theme_fields():
    with open(SCHEMA) as f:
        raw = json.load(f)
    return next(s for s in raw["sections"] if s["id"] == "theme")["fields"]


def defaults():
    return {f["key"]: f["default"] for f in theme_fields()}


class Fake:
    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        os.chmod(self.tmp, 0o755)
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        self.libexec = os.path.join(self.tmp, "libexec")
        self.share = os.path.join(self.tmp, "usr-share")
        self.plugins = os.path.join(self.tmp, "plugins")
        for d in (self.home, self.bin, self.libexec, self.share, self.plugins):
            os.makedirs(d)
        self.log = os.path.join(self.tmp, "log.jsonl")
        self.store = os.path.join(self.tmp, "store.json")
        for name in ("plasma-apply-colorscheme", "plasma-apply-cursortheme", "plasma-apply-desktoptheme",
                     "kwriteconfig6", "kreadconfig6", "dbus-send", "fc-list"):
            self.tool(os.path.join(self.bin, name))
        self.tool(os.path.join(self.libexec, "plasma-changeicons"))
        self.make_themes()
        for root, _, _ in os.walk(self.tmp):
            os.chmod(root, 0o755)
        for p in (self.log, self.store):
            open(p, "w").close()
            os.chmod(p, 0o666)
        os.chown(self.home, OWNER, OWNER)

    def tool(self, path):
        with open(path, "w") as f:
            f.write(FAKE.replace("/usr/bin/env python3", sys.executable, 1))
        os.chmod(path, 0o755)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o644)

    def make_themes(self):
        s = self.share
        self.write(f"{s}/color-schemes/BreezeLight.colors", "[General]\nName=Breeze Light\n")
        self.write(f"{s}/color-schemes/BreezeDark.colors", "[General]\nName=Breeze Dark\n")
        self.write(f"{s}/color-schemes/VGUI.colors", "[General]\nName=Steam Deck\n")
        self.write(f"{s}/color-schemes/-evil.colors", "[General]\nName=Evil\n")
        self.write(f"{s}/icons/breeze/index.theme", "[Icon Theme]\nName=Breeze\nDirectories=actions/16\n")
        self.write(f"{s}/icons/Papirus/index.theme", "[Icon Theme]\nName=Papirus\nDirectories=16x16/apps\n")
        self.write(f"{s}/icons/hidden/index.theme", "[Icon Theme]\nName=Hidden\nHidden=true\nDirectories=a\n")
        self.write(f"{s}/icons/breeze_cursors/index.theme", "[Icon Theme]\nName=Breeze Cursors\n")
        self.write(f"{s}/icons/breeze_cursors/cursors/left_ptr", "x")
        self.write(f"{s}/icons/Bibata/cursors/left_ptr", "x")
        self.write(f"{s}/plasma/desktoptheme/default/metadata.json", json.dumps({"KPlugin": {"Id": "default", "Name": "Breeze"}}))
        self.write(f"{s}/plasma/desktoptheme/Sweet/metadata.desktop", "[Desktop Entry]\nName=Sweet\n")
        self.write(f"{s}/aurorae/themes/Sweet-Dark/metadata.desktop", "[Desktop Entry]\nName=Sweet Dark\n")
        self.write(f"{s}/aurorae/themes/Plain/Plainrc", "[General]\n")
        self.write(f"{s}/aurorae/themes/Empty/readme.txt", "not a theme")
        self.write(f"{self.plugins}/org.kde.kdecoration3/org.kde.breeze.so", "")
        self.write(f"{self.plugins}/org.kde.kdecoration3/org.kde.kwin.aurorae.so", "")

    def calls(self):
        with open(self.log) as f:
            return [json.loads(line) for line in f if line.strip()]

    def stored(self):
        with open(self.store) as f:
            text = f.read()
        return json.loads(text) if text.strip() else {}

    def run(self, values=None, argv=("apply",), stdin=None, extra_env=None, root=False):
        vals = defaults()
        vals.update(values or {})
        body = stdin if stdin is not None else json.dumps({"section": "theme", "values": vals, "all": {"theme": vals},
                                                           "fields": theme_fields(), "media": {}})
        env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "LANG": "C.UTF-8",
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/fake-bus", "FORGE_LIB": LIB,
               "FAKE_LOG": self.log, "FAKE_STORE": self.store, "FORGE_THEME_LIBEXEC": self.libexec,
               "FORGE_THEME_DATA_DIRS": self.share, "FORGE_THEME_PLUGIN_DIRS": self.plugins}
        env.update(extra_env or {})
        drop = as_uid(OWNER) if os.geteuid() == 0 and not root else None
        return subprocess.run([sys.executable, BACKEND, *argv], input=body, env=env, preexec_fn=drop,
                              capture_output=True, text=True, timeout=60, cwd=self.home)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class SchemaTest(unittest.TestCase):
    def test_theme_section_is_valid(self):
        import forgeappearance as fa
        with open(SCHEMA) as f:
            raw = json.load(f)
        self.assertEqual(fa.schema_problems(raw), [])
        keys = [f["key"] for f in theme_fields()]
        self.assertEqual(keys, ["scheme_mode", "scheme", "accent", "accent_color", "icons", "cursor", "cursor_size",
                                "decoration", "plasma_style", "font", "font_size", "mono_font", "mono_size"])
        by = {f["key"]: f for f in theme_fields()}
        for k in ("scheme", "icons", "cursor", "decoration", "plasma_style", "font", "mono_font"):
            self.assertEqual(by[k].get("choices"), "backend", k)
            self.assertEqual(by[k]["default"], "", k)
        for k in ("scheme_mode", "accent", "cursor_size", "font_size", "mono_size"):
            self.assertEqual(by[k]["default"], "keep", k)

    def test_backend_is_executable_in_the_image(self):
        with open(FINALIZE) as f:
            self.assertIn("/usr/libexec/forge-appearance-theme", f.read())
        self.assertTrue(os.access(BACKEND, os.X_OK))

    def test_preview_hook_is_wrapped_and_uses_no_inner_html(self):
        with open(PREVIEW) as f:
            js = f.read()
        self.assertIn('registerPreview("theme"', js)
        self.assertRegex(js.strip(), r"^(//[^\n]*\n)*(\{|\(function)")
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("setAttribute(\"style\"", js)


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.f = Fake()

    def tearDown(self):
        self.f.close()

    def test_defaults_change_nothing(self):
        r = self.f.run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.f.calls(), [])
        self.assertIn("nothing to change", r.stdout)

    def test_dark_mode_runs_plasma_apply_colorscheme(self):
        r = self.f.run({"scheme_mode": "dark"})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn({"tool": "plasma-apply-colorscheme", "argv": ["BreezeDark"]}, self.f.calls())

    def test_light_and_dark_prefer_forge_tempered_when_installed(self):
        s = self.f.share
        self.f.write(f"{s}/color-schemes/ForgeTemperedDark.colors", "[General]\nName=Forge Tempered Dark\n")
        r = self.f.run({"scheme_mode": "dark"})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn({"tool": "plasma-apply-colorscheme", "argv": ["ForgeTemperedDark"]}, self.f.calls())

    def test_custom_scheme_must_be_installed_and_safe(self):
        r = self.f.run({"scheme_mode": "custom", "scheme": "VGUI"})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn({"tool": "plasma-apply-colorscheme", "argv": ["VGUI"]}, self.f.calls())
        for bad in ("Nope", "-evil", "../BreezeDark", "a/b", "x\ny", "$(id)", ""):
            open(self.f.log, "w").close()
            r = self.f.run({"scheme_mode": "custom", "scheme": bad})
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertEqual([c for c in self.f.calls() if c["tool"] == "plasma-apply-colorscheme"], [], bad)

    def test_a_scheme_the_tool_does_not_find_is_a_failure_even_with_exit_0(self):
        r = self.f.run({"scheme_mode": "dark"}, extra_env={"FAKE_NOT_FOUND": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Colors", r.stdout)

    def test_custom_accent(self):
        r = self.f.run({"accent": "custom", "accent_color": "#ff7a3d"})
        self.assertEqual(r.returncode, 0, r.stdout)
        calls = self.f.calls()
        self.assertIn({"tool": "kwriteconfig6", "argv": ["--file", "kdeglobals", "--group", "General", "--key",
                                                          "accentColorFromWallpaper", "--type", "bool", "--notify",
                                                          "false"]}, calls)
        self.assertIn({"tool": "kwriteconfig6", "argv": ["--file", "kdeglobals", "--group", "General", "--key",
                                                          "AccentColor", "255,122,61"]}, calls)
        self.assertEqual(calls[-1], {"tool": "plasma-apply-colorscheme", "argv": ["--accent-color", "#ff7a3d"]})

    def test_accent_from_wallpaper(self):
        r = self.f.run({"accent": "wallpaper"})
        self.assertEqual(r.returncode, 0, r.stdout)
        calls = self.f.calls()
        self.assertIn({"tool": "kwriteconfig6", "argv": ["--file", "kdeglobals", "--group", "General", "--key",
                                                          "accentColorFromWallpaper", "--type", "bool", "--notify",
                                                          "true"]}, calls)
        dbus = [c for c in calls if c["tool"] == "dbus-send"]
        self.assertEqual(dbus[0]["argv"], ["--session", "--print-reply", "--reply-timeout=10000",
                                           "--dest=org.kde.plasmashell", "/PlasmaShell", "org.kde.PlasmaShell.color"])
        # 4282684346 = 0xff4493ba
        self.assertEqual(calls[-1], {"tool": "plasma-apply-colorscheme", "argv": ["--accent-color", "#4493ba"]})

    def test_accent_from_wallpaper_without_a_color_yet_still_turns_it_on(self):
        r = self.f.run({"accent": "wallpaper"}, extra_env={"FAKE_WALL_COLOR": "0"})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(any("--accent-color" in c["argv"] for c in self.f.calls()))

    def test_icons_use_plasma_changeicons_and_only_listed_themes(self):
        r = self.f.run({"icons": "Papirus"})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn({"tool": "plasma-changeicons", "argv": ["Papirus"]}, self.f.calls())
        open(self.f.log, "w").close()
        for bad in ("hidden", "breeze_cursors", "nope", "../breeze"):
            r = self.f.run({"icons": bad})
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertEqual(self.f.calls(), [])

    def test_cursor_theme_and_size(self):
        r = self.f.run({"cursor": "Bibata", "cursor_size": "32"})
        self.assertEqual(r.returncode, 0, r.stdout)
        calls = self.f.calls()
        self.assertEqual(calls, [
            {"tool": "kwriteconfig6", "argv": ["--file", "kcminputrc", "--group", "Mouse", "--key", "cursorSize", "--notify", "32"]},
            {"tool": "plasma-apply-cursortheme", "argv": ["--size", "32", "Bibata"]},
            {"tool": "dbus-send", "argv": ["--session", "--type=signal", "/KGlobalSettings",
                                           "org.kde.KGlobalSettings.notifyChange", "int32:5", "int32:0"]}])

    def test_cursor_size_alone(self):
        r = self.f.run({"cursor_size": "48"})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual([c["tool"] for c in self.f.calls()], ["kwriteconfig6", "dbus-send"])

    def test_window_decoration_breeze_and_aurorae(self):
        r = self.f.run({"decoration": "org.kde.kwin.aurorae|__aurorae__svg__Sweet-Dark"})
        self.assertEqual(r.returncode, 0, r.stdout)
        calls = self.f.calls()
        self.assertEqual(calls, [
            {"tool": "kwriteconfig6", "argv": ["--file", "kwinrc", "--group", "org.kde.kdecoration2", "--key", "library",
                                               "org.kde.kwin.aurorae"]},
            {"tool": "kwriteconfig6", "argv": ["--file", "kwinrc", "--group", "org.kde.kdecoration2", "--key", "theme",
                                               "__aurorae__svg__Sweet-Dark"]},
            {"tool": "dbus-send", "argv": ["--session", "--type=method_call", "--dest=org.kde.KWin", "/KWin",
                                           "org.kde.KWin.reconfigure"]}])
        open(self.f.log, "w").close()
        for bad in ("org.kde.evil|x", "org.kde.breeze|Breeze|x", "org.kde.kwin.aurorae|__aurorae__svg__Nope"):
            self.assertNotEqual(self.f.run({"decoration": bad}).returncode, 0, bad)
        self.assertEqual(self.f.calls(), [])

    def test_plasma_style(self):
        r = self.f.run({"plasma_style": "Sweet"})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn({"tool": "plasma-apply-desktoptheme", "argv": ["Sweet"]}, self.f.calls())

    def test_fonts(self):
        r = self.f.run({"font": "Inter", "font_size": "11", "mono_font": "Hack", "mono_size": "12"})
        self.assertEqual(r.returncode, 0, r.stdout)
        s = self.f.stored()
        self.assertEqual(s["kdeglobals/General/font"], "Inter,11,-1,5,400,0,0,0,0,0,0,0,0,0,0,1")
        self.assertEqual(s["kdeglobals/General/menuFont"], "Inter,11,-1,5,400,0,0,0,0,0,0,0,0,0,0,1")
        self.assertEqual(s["kdeglobals/General/toolBarFont"], "Inter,11,-1,5,400,0,0,0,0,0,0,0,0,0,0,1")
        self.assertEqual(s["kdeglobals/General/smallestReadableFont"], "Inter,9,-1,5,400,0,0,0,0,0,0,0,0,0,0,1")
        self.assertEqual(s["kdeglobals/WM/activeFont"], "Inter,11,-1,5,400,0,0,0,0,0,0,0,0,0,0,1")
        self.assertEqual(s["kdeglobals/General/fixed"], "Hack,12,-1,5,400,0,0,0,0,0,0,0,0,0,0,1")
        self.assertEqual(self.f.calls()[-1], {"tool": "dbus-send", "argv": [
            "--session", "--type=signal", "/KDEPlatformTheme", "org.kde.KDEPlatformTheme.refreshFonts"]})

    def test_keys_are_written_with_notify_like_the_kcms(self):
        """Review proof (rvLook): without Notify, kde-gtk-config never passes the change to GTK and XWayland apps."""
        r = self.f.run({"cursor_size": "48", "font": "Inter", "font_size": "12", "mono_font": "Hack"})
        self.assertEqual(r.returncode, 0, r.stdout)
        writes = {c["argv"][c["argv"].index("--key") + 1]: c["argv"] for c in self.f.calls() if c["tool"] == "kwriteconfig6"}
        for key in ("cursorSize", "font", "menuFont", "toolBarFont", "smallestReadableFont", "activeFont", "fixed"):
            self.assertIn("--notify", writes[key], key)

    def test_both_aurorae_plugins_follow_the_systems_default_kwinrc(self):
        """With both Aurorae plugins installed, the one the system's kwinrc names wins (else the first)."""
        open(f"{self.f.plugins}/org.kde.kdecoration3/org.kde.kwin.aurorae.v2.so", "w").close()
        xdg = os.path.join(self.f.tmp, "etc-xdg")
        os.makedirs(xdg)
        with open(os.path.join(xdg, "kwinrc"), "w") as f:
            f.write("[org.kde.kdecoration2]\nlibrary=org.kde.kwin.aurorae.v2\ntheme=__aurorae__svg__Sweet-Dark\n")
        os.chmod(xdg, 0o755)
        os.chmod(os.path.join(xdg, "kwinrc"), 0o644)
        vals = lambda env: [c["value"] for c in json.loads(self.f.run(argv=("options",), extra_env=env).stdout)  # noqa: E731
                            ["fields"]["decoration"]["choices"]]
        self.assertIn("org.kde.kwin.aurorae.v2|__aurorae__svg__Sweet-Dark", vals({"FORGE_THEME_CONFIG_DIRS": xdg}))
        self.assertIn("org.kde.kwin.aurorae|__aurorae__svg__Sweet-Dark", vals({"FORGE_THEME_CONFIG_DIRS": self.f.tmp}))

    def test_aurorae_v2_library_when_only_that_plugin_is_installed(self):
        os.rename(f"{self.f.plugins}/org.kde.kdecoration3/org.kde.kwin.aurorae.so",
                  f"{self.f.plugins}/org.kde.kdecoration3/org.kde.kwin.aurorae.v2.so")
        d = json.loads(self.f.run(argv=("options",)).stdout)["fields"]["decoration"]["choices"]
        self.assertIn("org.kde.kwin.aurorae.v2|__aurorae__svg__Sweet-Dark", [c["value"] for c in d])
        r = self.f.run({"decoration": "org.kde.kwin.aurorae.v2|__aurorae__svg__Sweet-Dark"})
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_font_size_alone_keeps_the_current_family(self):
        with open(self.f.store, "w") as f:
            json.dump({"kdeglobals/General/font": "Noto Sans,10,-1,5,400,0,0,0,0,0,0,0,0,0,0,1"}, f)
        r = self.f.run({"font_size": "12"})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.f.stored()["kdeglobals/General/font"], "Noto Sans,12,-1,5,400,0,0,0,0,0,0,0,0,0,0,1")

    def test_font_names_are_checked(self):
        for bad in ("Bad,Name", "Nope Font", "-x"):
            r = self.f.run({"mono_font": bad})
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertFalse(any(c["tool"] == "kwriteconfig6" for c in self.f.calls()))

    def test_hostile_stdin_is_refused_or_ignored(self):
        for body in ("", "[]", '{"values": 5}', "x" * 3000000):
            r = self.f.run(stdin=body)
            self.assertNotEqual(r.returncode, 0)
        vals = defaults()
        vals.update({"scheme_mode": "rm -rf", "accent": "custom", "accent_color": "red;", "cursor_size": "9999"})
        r = self.f.run(stdin=json.dumps({"values": vals}))
        self.assertEqual(self.f.calls(), [])

    def test_one_part_failing_does_not_stop_the_others(self):
        r = self.f.run({"scheme_mode": "dark", "plasma_style": "Sweet"},
                       extra_env={"FAKE_FAIL": "plasma-apply-colorscheme"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn({"tool": "plasma-apply-desktoptheme", "argv": ["Sweet"]}, self.f.calls())

    def test_refuses_root_and_unknown_commands(self):
        self.assertEqual(self.f.run(argv=("frobnicate",)).returncode, 2)
        if os.geteuid() == 0:
            r = self.f.run({"scheme_mode": "dark"}, root=True)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(self.f.calls(), [])

    def test_options_lists_what_is_installed(self):
        r = self.f.run(argv=("options",))
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)["fields"]
        vals = lambda k: [c["value"] for c in d[k]["choices"]]  # noqa: E731
        self.assertEqual(vals("scheme"), ["BreezeDark", "BreezeLight", "VGUI"])
        self.assertEqual([c["label"] for c in d["scheme"]["choices"]], ["Breeze Dark", "Breeze Light", "Steam Deck"])
        self.assertEqual(vals("icons"), ["breeze", "Papirus"])
        self.assertEqual(vals("cursor"), ["Bibata", "breeze_cursors"])
        self.assertEqual(vals("plasma_style"), ["default", "Sweet"])
        self.assertEqual(vals("decoration"), ["org.kde.breeze|Breeze", "org.kde.kwin.aurorae|__aurorae__svg__Plain",
                                              "org.kde.kwin.aurorae|__aurorae__svg__Sweet-Dark"])
        self.assertEqual(vals("font"), ["Hack", "Inter", "Noto Sans", "Noto Sans Thai"])
        self.assertEqual(vals("mono_font"), ["Hack", "Noto Sans Mono"])

    def test_options_through_forgeappearance_keeps_only_valid_choices(self):
        """The real framework call (section_choices) with this backend, run as a normal user."""
        if os.geteuid() != 0:
            self.skipTest("needs root to switch to a test uid")
        code = ("import json, os, sys; sys.path.insert(0, %r); import forgeappearance as fa; "
                "print(json.dumps(fa.section_choices('theme', fa.load_schema())))") % LIB
        env = {"HOME": self.f.home, "PATH": "/usr/bin:/bin", "FORGE_APPEARANCE_SCHEMA": SCHEMA,
               "FORGE_APPEARANCE_LIBEXEC": os.path.dirname(BACKEND)}
        r = subprocess.run([sys.executable, "-c", code], env=env, preexec_fn=as_uid(OWNER), capture_output=True,
                           text=True, timeout=60, cwd=self.f.home)
        state, fields, _ = json.loads(r.stdout)
        self.assertEqual(state, "ok", r.stderr)
        self.assertIn("scheme", fields)


if __name__ == "__main__":
    unittest.main()
