"""Dock appearance (Settings, Appearance, Dock): three styles, per user, no root.

Default restores the stock Plasma panel from the first backup, Floating clean changes the same panel
through Plasma desktop scripting, Animated runs Krema (~/.config/kremarc, ~/.config/autostart).
Plasma, D-Bus, systemd and Krema are fakes here: a fake dbus-send runs each Plasma script in
node against tests/data/fake_plasma.js, a stand-in for Plasma's scripting API. Not Plasma itself.
"""
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
BACKEND = os.path.join(ROOT, "files/system/usr/libexec/forge-appearance-dock")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/appearance.json")
PREVIEW = os.path.join(ROOT, "files/system/usr/share/forge/appearance-preview/dock.js")
HARNESS = os.path.join(ROOT, "tests/data/fake_plasma.js")
GOLDEN_JS = os.path.join(ROOT, "tests/data/dock-floating-script.js")
KREMA_SCRIPT = os.path.join(ROOT, "files/scripts/install-krema.sh")
RECIPE = os.path.join(ROOT, "recipes/common-modules.yml")
FINALIZE = os.path.join(ROOT, "files/scripts/finalize.sh")
OWNER = 65533
NODE = shutil.which("node") or ("/opt/node22/bin/node" if os.path.exists("/opt/node22/bin/node") else None)
NEEDS_NODE = unittest.skipUnless(NODE, "node is needed to run the Plasma stand-in")

sys.path.insert(0, LIB)


def read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


def load_backend():
    loader = importlib.machinery.SourceFileLoader("forge_dock_backend", BACKEND)
    spec = importlib.util.spec_from_loader("forge_dock_backend", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def dock_fields():
    with open(SCHEMA) as f:
        raw = json.load(f)
    return next(s for s in raw["sections"] if s["id"] == "dock")["fields"]


def defaults():
    return {f["key"]: f["default"] for f in dock_fields()}


# The stock Bazzite layout, simplified: a bottom panel with the app list and a second panel without.
STOCK_PANELS = [
    {"id": 1, "widgets": ["org.kde.plasma.kickoff", "org.kde.plasma.pager", "org.kde.plasma.icontasks",
                          "org.kde.plasma.marginsseparator", "org.kde.plasma.systemtray",
                          "org.kde.plasma.digitalclock", "org.kde.plasma.showdesktop"],
     "props": {"location": "bottom", "height": 44, "lengthMode": "fill", "maximumLength": 1920,
               "minimumLength": 1920, "offset": 0, "alignment": "center", "hiding": "none",
               "opacity": "adaptive", "floating": True, "floatingApplets": False}},
    {"id": 7, "widgets": ["org.kde.plasma.appmenu"],
     "props": {"location": "top", "height": 28, "lengthMode": "custom", "maximumLength": 800,
               "minimumLength": 600, "offset": 12, "alignment": "left", "hiding": "dodgewindows",
               "opacity": "opaque", "floating": False, "floatingApplets": True}},
]

FAKE_TOOL = r'''#!@PY@
import json, os, subprocess, sys
st = os.environ["FAKE_STATE"]
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
with open(os.path.join(st, "calls.jsonl"), "a") as f:
    f.write(json.dumps([name] + args) + "\n")
def p(n): return os.path.join(st, n)
def has(n): return os.path.exists(p(n))
def touch(n): open(p(n), "w").close()
def drop(n):
    try: os.unlink(p(n))
    except FileNotFoundError: pass
HEAD = "method return time=1.5 sender=:1.0 -> destination=:1.9 serial=3 reply_serial=2\n"
if name == "dbus-send":
    dest = next((a.split("=", 1)[1] for a in args if a.startswith("--dest=")), "")
    rest = [a for a in args if not a.startswith("--")]
    method, params = rest[1], rest[2:]
    if method == "org.freedesktop.DBus.NameHasOwner":
        if has("no_bus"):
            print('Failed to open connection to "session" message bus: Unable to autolaunch a dbus-daemon '
                  'without a $DISPLAY for X11', file=sys.stderr); sys.exit(1)
        n = params[0].split(":", 1)[1]
        flag = {"org.kde.plasmashell": "plasma_up", "com.bhyoo.krema": "krema_up"}.get(n)
        print(HEAD + "   boolean " + ("true" if flag and has(flag) else "false"))
        sys.exit(0)
    if dest == "org.kde.plasmashell":
        if not has("plasma_up"):
            print("Error org.freedesktop.DBus.Error.ServiceUnknown: no plasmashell", file=sys.stderr); sys.exit(1)
        if method == "org.kde.PlasmaShell.dumpCurrentLayoutJS":
            data = open(p("layout.js"), "rb").read()
            print(HEAD + "   array of bytes [\n      " + " ".join("%02x" % b for b in data) + "\n   ]")
            sys.exit(0)
        if method == "org.kde.PlasmaShell.evaluateScript":
            if has("locked"):
                print("Error org.freedesktop.DBus.Error.Failed: Widgets are locked", file=sys.stderr); sys.exit(1)
            os.makedirs(p("scripts"), exist_ok=True)
            sp = os.path.join(p("scripts"), "%03d.js" % len(os.listdir(p("scripts"))))
            with open(sp, "w") as f:
                f.write(params[0].split(":", 1)[1])
            r = subprocess.run([os.environ["FAKE_NODE"], os.environ["FAKE_HARNESS"], sp, p("panels.json")],
                               capture_output=True, text=True)
            if r.returncode:
                print("Error org.freedesktop.DBus.Error.Failed: " + (r.stderr.strip().splitlines() or ["?"])[-1],
                      file=sys.stderr); sys.exit(1)
            print(HEAD + '   string "' + r.stdout + '"')
            sys.exit(0)
    if dest == "com.bhyoo.krema" and method == "org.qtproject.Qt.QCoreApplication.quit":
        if not has("krema_up"):
            print("Error org.freedesktop.DBus.Error.ServiceUnknown: no krema", file=sys.stderr); sys.exit(1)
        drop("krema_up"); print(HEAD); sys.exit(0)
    print("Error org.freedesktop.DBus.Error.UnknownMethod: " + method, file=sys.stderr); sys.exit(1)
elif name == "systemctl":
    verb = [a for a in args if not a.startswith("--")]
    if verb[0] == "is-active":
        print("active" if has("plasma_up") else "inactive"); sys.exit(0 if has("plasma_up") else 3)
    if verb[0] == "stop": drop("plasma_up")
    if verb[0] == "start": touch("plasma_up")
elif name == "systemd-run":
    if has("krema_up"):
        print("Failed to start transient service unit: Unit forge-krema.service already exists.", file=sys.stderr)
        sys.exit(1)
    touch("krema_up")
'''


def as_uid(uid):
    def drop():
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    return drop


class Desktop:
    """A fake Plasma session: home folder, fake tools on PATH, panel state for the Plasma stand-in."""

    def __init__(self, krema=True, panels=STOCK_PANELS):
        self.tmp = tempfile.mkdtemp()
        os.chmod(self.tmp, 0o755)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (self.home, self.state, self.bin):
            os.mkdir(d)
        self.config = os.path.join(self.home, ".config")
        os.mkdir(self.config)
        tool = FAKE_TOOL.replace("@PY@", sys.executable)
        for name in ("dbus-send", "systemctl", "systemd-run"):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(tool)
            os.chmod(path, 0o755)
        if krema:
            path = os.path.join(self.bin, "krema")
            with open(path, "w") as f:
                f.write("#!/bin/sh\necho 'the fake krema must never run' >&2\nexit 9\n")
            os.chmod(path, 0o755)
        self.write("state/panels.json", json.dumps(panels))
        self.write("state/layout.js", "var plasma = getApiVersion(1);\n// stock layout é\n")
        open(os.path.join(self.state, "plasma_up"), "w").close()
        self.write("home/.config/plasmashellrc", "[PlasmaViews][Panel 1]\nfloating=1\n")
        self.write("home/.config/plasma-org.kde.plasma.desktop-appletsrc", "[Containments][1]\nplugin=org.kde.panel\n")
        if os.geteuid() == 0:
            for d, _, files in os.walk(self.tmp):
                for n in [d] + [os.path.join(d, x) for x in files]:
                    os.chown(n, OWNER, OWNER)

    def write(self, rel, text):
        path = os.path.join(self.tmp, rel)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        if os.geteuid() == 0:
            os.chown(path, OWNER, OWNER)
        return path

    def read(self, rel):
        with open(os.path.join(self.tmp, rel), encoding="utf-8") as f:
            return f.read()

    def panels(self):
        return {p["id"]: p["props"] for p in json.loads(self.read("state/panels.json"))}

    def calls(self):
        try:
            with open(os.path.join(self.state, "calls.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def scripts(self):
        d = os.path.join(self.state, "scripts")
        return [read(os.path.join(d, n)) for n in sorted(os.listdir(d))] if os.path.isdir(d) else []

    def up(self, name):
        return os.path.exists(os.path.join(self.state, name + "_up"))

    def set_up(self, name, on=True):
        path = os.path.join(self.state, name + "_up")
        if on:
            open(path, "w").close()
        elif os.path.exists(path):
            os.unlink(path)

    def apply(self, values=None, stdin=None, argv=("apply",), root=False):
        vals = defaults()
        vals.update(values or {})
        body = stdin if stdin is not None else json.dumps({"section": "dock", "values": vals, "all": {"dock": vals},
                                                           "fields": dock_fields(), "media": {}})
        env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "LANG": "C.UTF-8",
               "XDG_CONFIG_HOME": self.config, "XDG_DATA_HOME": os.path.join(self.home, ".local/share"),
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/fake-bus", "FORGE_LIB": LIB,
               "FAKE_STATE": self.state, "FAKE_NODE": NODE or "/bin/false", "FAKE_HARNESS": HARNESS}
        drop = as_uid(OWNER) if os.geteuid() == 0 and not root else None
        return subprocess.run([sys.executable, BACKEND, *argv], input=body, env=env, preexec_fn=drop,
                              capture_output=True, text=True, timeout=60, cwd=self.home)

    @property
    def backup(self):
        return os.path.join(self.home, ".local/share/forge/appearance/dock-backup")

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class SchemaTest(unittest.TestCase):
    def test_dock_fields_are_valid_and_complete(self):
        import forgeappearance
        with open(SCHEMA) as f:
            raw = json.load(f)
        self.assertEqual(forgeappearance.schema_problems(raw), [])
        keys = [f["key"] for f in dock_fields()]
        self.assertEqual(keys[0], "style")
        for want in ("position", "icon_size", "zoom", "zoom_style", "zoom_speed", "spacing", "radius", "background",
                     "opacity", "tint", "shadow", "visibility", "show_delay", "hide_delay", "gap", "badges",
                     "previews", "monitors", "stock_panel", "alignment", "panel_look"):
            self.assertIn(want, keys)
        style = dock_fields()[0]
        self.assertEqual(style["default"], "default")
        self.assertEqual([o["value"] for o in style["options"]], ["default", "floating", "animated"])
        # Every other option belongs to a style that really has it.
        for f in dock_fields()[1:]:
            self.assertEqual(f.get("show_if", {}).get("key"), "style", f["key"])

    def test_backend_table_matches_the_schema(self):
        mod = load_backend()
        fields = {f["key"]: f for f in dock_fields()}
        self.assertEqual(set(mod.FIELDS), set(fields))
        for key, f in fields.items():
            spec = mod.FIELDS[key]
            self.assertEqual(spec["default"], f["default"], key)
            if f["type"] == "select":
                self.assertEqual(list(spec["options"]), [o["value"] for o in f["options"]], key)
            if f["type"] in ("number", "slider"):
                self.assertEqual((spec["min"], spec["max"]), (f["min"], f["max"]), key)

    def test_no_dashes_in_user_text(self):
        for f in dock_fields():
            for text in [f["label"], f.get("help", "")] + [o["label"] for o in f.get("options", [])]:
                self.assertNotIn("—", text)
                self.assertNotIn("--", text)


KREMA_DEFAULT = """# Written by Forge OS (Settings, Appearance, Dock). Changes here are replaced on the next apply.
[General]
IconSize=48
IconSpacing=4
MaxZoomFactor=1.6
ZoomStyle=0
ZoomAnimationPreset=0
ZoomInDuration=180
ZoomOutDuration=240
ZoomInEasing=3
ZoomOutEasing=3
CornerRadius=12
Floating=true
FloatingMargin=8
VisibilityMode=0
ReserveScreenSpace=true
DodgeActiveOnly=false
Edge=1
ShowDelay=200
HideDelay=400
BackgroundOpacity=0.6
BackgroundStyle=0
UseAccentColor=false
UseSystemColor=true
TintColor=#1e3a5f
ShadowEnabled=false
ShadowIntensity=0.3
PreviewEnabled=true
PreviewThumbnailSize=200
AttentionAnimation=2
BadgeDisplayMode=0
MonitorMode=0
"""


class KremarcTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_backend()

    def text(self, values=None, existing=""):
        v = defaults()
        v.update(values or {})
        return self.mod.kremarc_text(self.mod.clean_values(v), existing)

    def test_defaults_exact(self):
        self.assertEqual(self.text(), KREMA_DEFAULT)

    def test_every_choice_maps_to_the_krema_values(self):
        t = self.text({"style": "animated", "position": "left", "icon_size": 64, "zoom": 200, "zoom_style": "in_place",
                       "zoom_speed": "custom", "zoom_in_ms": 300, "zoom_out_ms": 0, "zoom_easing": "linear",
                       "spacing": 10, "radius": 24, "gap": 0, "background": "tinted", "opacity": 85,
                       "color_source": "custom", "tint": "#a0b1c2", "shadow": True, "shadow_strength": 55,
                       "visibility": "dodge", "dodge_active_only": True, "reserve_space": False, "show_delay": 0,
                       "hide_delay": 1500, "badges": "dot", "previews": False, "preview_size": 320,
                       "attention": "bounce", "monitors": "follow"})
        for line in ("IconSize=64", "MaxZoomFactor=2", "ZoomStyle=1", "ZoomAnimationPreset=4", "ZoomInDuration=300",
                     "ZoomOutDuration=0", "ZoomInEasing=0", "ZoomOutEasing=0", "IconSpacing=10", "CornerRadius=24",
                     "Floating=false", "FloatingMargin=1", "Edge=2", "BackgroundStyle=2", "BackgroundOpacity=0.85",
                     "UseSystemColor=false", "UseAccentColor=false", "TintColor=#a0b1c2", "ShadowEnabled=true",
                     "ShadowIntensity=0.55", "VisibilityMode=2", "DodgeActiveOnly=true", "ReserveScreenSpace=false",
                     "ShowDelay=0", "HideDelay=1500", "BadgeDisplayMode=1", "PreviewEnabled=false",
                     "PreviewThumbnailSize=320", "AttentionAnimation=1", "MonitorMode=2"):
            self.assertIn("\n" + line + "\n", t)
        t = self.text({"color_source": "accent", "background": "acrylic", "position": "top"})
        for line in ("UseAccentColor=true", "UseSystemColor=true", "BackgroundStyle=3", "Edge=0"):
            self.assertIn("\n" + line + "\n", t)

    def test_hostile_values_are_clamped_or_default(self):
        t = self.text({"icon_size": 9999, "zoom": -5, "spacing": 1e9, "radius": -1, "gap": 500, "opacity": 250,
                       "shadow_strength": float("nan"), "show_delay": -100, "hide_delay": 10 ** 12,
                       "preview_size": 5, "zoom_in_ms": True, "zoom_out_ms": "100",
                       "tint": "#fff\n[General]\nIconSize=999", "position": "bottom\nEdge=3",
                       "background": "acrylic\"); evil(\"", "shadow": "yes", "previews": 1, "monitors": None,
                       "badges": ["dot"], "attention": {"a": 1}})
        g = dict(line.split("=", 1) for line in t.splitlines() if "=" in line)
        self.assertEqual(g["IconSize"], "96")
        self.assertEqual(g["MaxZoomFactor"], "1")
        self.assertEqual(g["IconSpacing"], "64")
        self.assertEqual(g["CornerRadius"], "0")
        self.assertEqual(g["FloatingMargin"], "128")
        self.assertEqual(g["BackgroundOpacity"], "1")
        self.assertEqual(g["ShadowIntensity"], "0.3")       # NaN: the default
        self.assertEqual(g["ShowDelay"], "0")
        self.assertEqual(g["HideDelay"], "5000")
        self.assertEqual(g["PreviewThumbnailSize"], "120")
        self.assertEqual(g["ZoomInDuration"], "180")         # a toggle is not a number
        self.assertEqual(g["ZoomOutDuration"], "240")        # text is not a number
        self.assertEqual(g["TintColor"], "#1e3a5f")
        self.assertEqual(g["Edge"], "1")
        self.assertEqual(g["BackgroundStyle"], "0")
        self.assertEqual(g["ShadowEnabled"], "false")
        self.assertEqual(g["PreviewEnabled"], "true")
        self.assertEqual(g["MonitorMode"], "0")
        self.assertEqual(g["BadgeDisplayMode"], "0")
        self.assertEqual(g["AttentionAnimation"], "2")
        self.assertEqual(t.count("["), 1, "only the [General] group")
        self.assertEqual(len(t.splitlines()), len(KREMA_DEFAULT.splitlines()))

    def test_keeps_known_krema_keys_and_drops_the_rest(self):
        existing = ("[General]\nPinnedLaunchers=applications:org.kde.dolphin.desktop,applications:firefox.desktop\n"
                    "IconSize=30\nIconOpacityInactive=7\nShadowColor=#40000000\nShadowColor[$e]=$(rm -rf ~)\n"
                    "NotAKremaKey=1\nSelectedOutputs=DP-1,HDMI-A-1\nShowDelay=99999999\nSeparateLaunchers=true\n"
                    "BadgeCustomColor=red; evil\n"
                    "[Screen-DP-1]\nIconSize=90\nPinnedLaunchers=applications:org.kde.konsole.desktop\n"
                    "[Screen-../../x]\nPinnedLaunchers=a\n[Other]\nX=1\n")
        t = self.text({}, existing)
        self.assertIn("\nIconSize=48\n", t)                          # Forge's value wins
        self.assertIn("\nIconOpacityInactive=1\n", t)                # kept, clamped to Krema's range
        self.assertIn("\nShadowColor=#40000000\n", t)
        self.assertIn("\nPinnedLaunchers=applications:org.kde.dolphin.desktop,applications:firefox.desktop\n", t)
        self.assertIn("\nSelectedOutputs=DP-1,HDMI-A-1\n", t)
        self.assertIn("\nSeparateLaunchers=true\n", t)
        self.assertIn("\nShowDelay=200\n", t)                        # managed: the Forge value
        # Per-screen choices made in Krema stay (Krema's own per-screen keys only, clamped).
        self.assertIn("[Screen-DP-1]\nIconSize=90\nPinnedLaunchers=applications:org.kde.konsole.desktop\n", t)
        for bad in ("NotAKremaKey", "$e", "rm -rf", "IconSize=30", "Screen-..", "[Other]",
                    "BadgeCustomColor", "X=1"):
            self.assertNotIn(bad, t)
        # The kept keys sit in Krema's own order.
        order = [line.split("=")[0] for line in t.split("[Screen-DP-1]")[0].splitlines() if "=" in line]
        self.assertLess(order.index("IconOpacityInactive"), order.index("MaxZoomFactor"))
        self.assertLess(order.index("SelectedOutputs"), order.index("PinnedLaunchers"))

    def test_per_screen_overrides_are_clamped_and_unicode_pins_kept(self):
        existing = ("[Screen-HDMI-A-1]\nIconSize=500\nCornerRadius=7\nShowDelay=5\nEdge=9\n"
                    "[General]\nPinnedLaunchers=applications:NieR\u2122.desktop\n"
                    "[Screen-DP-2]\nPinnedLaunchers=a\u2028b\n")
        t = self.text({}, existing)
        self.assertIn("[Screen-HDMI-A-1]\nIconSize=96\nCornerRadius=7\nEdge=3\n", t)   # Krema's key order
        self.assertNotIn("ShowDelay=5", t)
        self.assertIn("\nPinnedLaunchers=applications:NieR\u2122.desktop\n", t)
        self.assertNotIn("Screen-DP-2", t)
        self.assertNotIn("\u2028", t)

    def test_a_byte_order_mark_is_read_like_kconfig(self):
        # KConfig (kconfigini.cpp) only trims whitespace: "\ufeff[General]" is no group header there,
        # so Krema itself would not see these pins. Forge reads it the same way.
        t = self.text({}, "\ufeff[General]\nPinnedLaunchers=applications:a.desktop\n")
        self.assertNotIn("a.desktop", t)
        self.assertNotIn("\ufeff", t)

    @unittest.skipUnless(os.environ.get("KREMA_KCFG"), "set KREMA_KCFG to a krema.kcfg to compare against it")
    def test_key_table_matches_krema_kcfg(self):
        import xml.etree.ElementTree as ET
        ns = {"k": "http://www.kde.org/standards/kcfg/1.0"}
        root = ET.parse(os.environ["KREMA_KCFG"]).getroot()
        entries = root.findall("k:group[@name='General']/k:entry", ns)
        self.assertEqual([e.get("name") for e in entries], list(self.mod.KCFG))
        for e in entries:
            typ, lo, hi = self.mod.KCFG[e.get("name")]
            self.assertEqual(typ, e.get("type"), e.get("name"))
            if e.find("k:min", ns) is not None:
                self.assertEqual(float(e.find("k:min", ns).text), lo, e.get("name"))
                self.assertEqual(float(e.find("k:max", ns).text), hi, e.get("name"))


class ScriptTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_backend()

    def saved(self):
        return [{"id": p["id"], "props": dict(p["props"])} for p in STOCK_PANELS]

    def test_floating_script_exact(self):
        v = self.mod.clean_values(dict(defaults(), style="floating"))
        script = self.mod.panel_script(self.saved(), self.mod.style_props(v))
        with open(GOLDEN_JS) as f:
            self.assertEqual(script, f.read())

    def test_values_only_enter_as_one_json_literal(self):
        script = self.mod.panel_script(self.saved(), {"location": "top"})
        head, data = script.split("var forge = ", 1)
        data = data.split(";\n", 1)[0]
        self.assertEqual(json.loads(data), {"saved": self.saved(), "style": {"location": "top"}})
        self.assertEqual(script.count("var forge = "), 1)
        self.assertTrue(data.isascii())

    def test_saved_props_from_the_backup_are_checked(self):
        bad = [{"id": 1, "props": {"location": "top\"; evil(); \"", "height": "44", "floating": "yes",
                                   "alignment": "center", "hiding": "windowsgobelow", "offset": 10 ** 9,
                                   "unknown": 1, "opacity": "translucent"}},
               {"id": "2; evil()", "props": {"location": "top"}}, "junk", {"id": True, "props": {}}]
        clean = self.mod.clean_saved(bad)
        self.assertEqual(clean, [{"id": 1, "props": {"alignment": "center", "hiding": "windowsgobelow",
                                                     "opacity": "translucent"}}])
        self.assertEqual(self.mod.clean_saved({"not": "a list"}), [])

    def test_style_props(self):
        sp = self.mod.style_props
        v = self.mod.clean_values(dict(defaults(), style="floating"))
        self.assertEqual(sp(v), {"location": "bottom", "floating": True, "lengthMode": "fit", "alignment": "center",
                                 "height": 56, "hiding": "none", "opacity": "translucent"})
        v = self.mod.clean_values(dict(defaults(), style="floating", position="left", panel_width="fill",
                                       alignment="right", icon_size=24, visibility="dodge", panel_look="opaque"))
        self.assertEqual(sp(v), {"location": "left", "floating": True, "lengthMode": "fill", "alignment": "right",
                                 "height": 32, "hiding": "dodgewindows", "opacity": "opaque"})
        v = self.mod.clean_values(dict(defaults(), style="animated"))
        self.assertEqual(sp(v), {"location": "top", "height": 30, "lengthMode": "fill", "floating": False,
                                 "hiding": "none"})
        v = self.mod.clean_values(dict(defaults(), style="animated", position="top"))
        self.assertEqual(sp(v)["location"], "bottom")
        v = self.mod.clean_values(dict(defaults(), style="animated", stock_panel="autohide"))
        self.assertEqual(sp(v), {"hiding": "autohide"})
        v = self.mod.clean_values(dict(defaults(), style="animated", stock_panel="keep"))
        self.assertEqual(sp(v), {})
        self.assertIsNone(sp(self.mod.clean_values(defaults())))

    @NEEDS_NODE
    def test_generated_scripts_parse(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        scripts = [self.mod.READ_SCRIPT, self.mod.COUNT_SCRIPT, self.mod.panel_script(self.saved(), None)]
        for style in ("floating", "animated"):
            scripts.append(self.mod.panel_script(self.saved(), self.mod.style_props(
                self.mod.clean_values(dict(defaults(), style=style)))))
        for i, s in enumerate(scripts):
            p = os.path.join(tmp, f"{i}.js")
            with open(p, "w") as f:
                f.write(s)
            r = subprocess.run([NODE, "--check", p], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_every_command_fits_in_one_time_budget(self):
        import time
        self.mod.DEADLINE = time.monotonic() + 1
        t0 = time.monotonic()
        with self.assertRaises(self.mod.DockError):
            self.mod.run(["sleep", "5"], timeout=30)
        self.assertLess(time.monotonic() - t0, 3)
        with self.assertRaises(self.mod.DockError):
            self.mod.run(["true"])
        self.assertFalse(self.mod.wait_for(lambda: False, 30))
        self.assertLess(self.mod.BUDGET_S, 110)

    def test_dbus_send_replies_are_parsed(self):
        # Formats copied from a real dbus-send against a test service (see NOTES.md).
        reply = ('method return time=1795111789.003703 sender=:1.0 -> destination=:1.1 serial=3 reply_serial=2\n'
                 '   string "line1\n"quoted" é\nFORGE-BEGIN{"a":1}FORGE-END\nhello"\n')
        self.assertEqual(self.mod.parse_string(reply), 'line1\n"quoted" é\nFORGE-BEGIN{"a":1}FORGE-END\nhello')
        self.assertEqual(self.mod.extract(self.mod.parse_string(reply)), {"a": 1})
        hexed = ('method return time=1 sender=:1.0 -> destination=:1.3 serial=5 reply_serial=2\n'
                 '   array of bytes [\n      76 61 72 20 78 0a 09 c3 a9\n   ]\n')
        self.assertEqual(self.mod.parse_bytes(hexed), b"var x\n\t\xc3\xa9")
        plain = 'method return time=1 sender=:1.0 -> destination=:1.4 serial=6 reply_serial=2\n   array of bytes "abc def"\n'
        self.assertEqual(self.mod.parse_bytes(plain), b"abc def")
        with self.assertRaises(self.mod.DockError):
            self.mod.extract("no markers here")
        self.assertIs(self.mod.parse_bool("method return\n   boolean true\n"), True)
        self.assertIs(self.mod.parse_bool("method return\n   boolean false\n"), False)


@NEEDS_NODE
class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.d = Desktop()
        self.addCleanup(self.d.close)

    def ok(self, values=None):
        r = self.d.apply(values)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1, r.stdout)
        return r.stdout

    def original(self):
        return {p["id"]: p["props"] for p in STOCK_PANELS}

    def test_floating_backs_up_once_then_changes_only_the_app_panel(self):
        out = self.ok({"style": "floating"})
        self.assertIn("Floating clean", out)
        first = os.path.join(self.d.backup, "first")
        self.assertEqual(stat.S_IMODE(os.stat(self.d.backup).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(first).st_mode), 0o700)
        for name in ("layout.js", "panels.json", "plasmashellrc", "plasma-org.kde.plasma.desktop-appletsrc", "meta.json"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(first, name)).st_mode), 0o600, name)
        self.assertEqual(read(os.path.join(first, "layout.js")), self.d.read("state/layout.js"))
        self.assertEqual(read(os.path.join(first, "plasmashellrc")), "[PlasmaViews][Panel 1]\nfloating=1\n")
        saved = json.loads(read(os.path.join(first, "panels.json")))
        self.assertEqual({p["id"]: p["props"] for p in saved}, self.original())
        # The files were copied while plasmashell was stopped, and it was started again.
        verbs = [c[1:] for c in self.d.calls() if c[0] == "systemctl"]
        self.assertIn(["--user", "stop", "plasma-plasmashell.service"], verbs)
        self.assertEqual(verbs[-1], ["--user", "start", "plasma-plasmashell.service"])
        self.assertTrue(self.d.up("plasma"))
        panels = self.d.panels()
        want = dict(self.original()[1], floating=True, lengthMode="fit", alignment="center", height=56,
                    hiding="none", opacity="translucent", location="bottom")
        self.assertEqual(panels[1], want)
        self.assertEqual(panels[7], self.original()[7])
        # Nothing about Krema happened.
        self.assertFalse(os.path.exists(os.path.join(self.d.config, "kremarc")))
        self.assertFalse([c for c in self.d.calls() if c[0] == "systemd-run"])

    def test_default_restores_exactly_and_keeps_the_backup(self):
        self.ok({"style": "floating", "position": "left", "panel_width": "fill", "visibility": "autohide"})
        self.assertNotEqual(self.d.panels(), self.original())
        out = self.ok({"style": "default"})
        self.assertIn("back to how it was", out)
        self.assertEqual(self.d.panels(), self.original())
        self.assertTrue(os.path.isdir(os.path.join(self.d.backup, "first")))
        # Applying Default again changes nothing.
        n = len(self.d.scripts())
        out = self.ok({"style": "default"})
        self.assertEqual(len(self.d.scripts()), n)

    def test_windows_go_below_survives_the_round_trip(self):
        # Plasma's scripting reads "windows go below" as "none"; plasmashellrc has the real value (3).
        panels = json.loads(json.dumps(STOCK_PANELS))
        panels[1]["props"]["hiding"] = "none"
        self.d.close()
        self.d = Desktop(panels=panels)
        self.d.write("home/.config/plasmashellrc", "[PlasmaViews][Panel 7]\npanelVisibility=3\n"
                                                   "[PlasmaViews][Panel 7][Defaults]\nthickness=28\n"
                                                   "[PlasmaViews][Panel 1]\npanelVisibility=0\n")
        self.ok({"style": "floating"})
        saved = {p["id"]: p["props"] for p in json.loads(read(os.path.join(self.d.backup, "first", "panels.json")))}
        self.assertEqual(saved[7]["hiding"], "windowsgobelow")
        self.assertEqual(saved[1]["hiding"], "none")
        self.ok({"style": "default"})
        self.assertEqual(self.d.panels()[7], dict(self.original()[7], hiding="windowsgobelow"))

    def test_default_with_nothing_to_restore_touches_nothing(self):
        out = self.ok({"style": "default"})
        self.assertIn("has not changed", out)
        self.assertEqual(self.d.scripts(), [])
        self.assertFalse(os.path.exists(self.d.backup))

    def test_backup_is_taken_only_once(self):
        self.ok({"style": "floating"})
        first = os.path.join(self.d.backup, "first")
        before = {n: hashlib.sha256(read(os.path.join(first, n), "rb")).hexdigest() for n in os.listdir(first)}
        self.d.write("home/.config/plasmashellrc", "[changed after the backup]\n")
        self.d.write("state/layout.js", "// changed after the backup\n")
        self.ok({"style": "animated"})
        self.ok({"style": "default"})
        self.ok({"style": "floating", "alignment": "left"})
        after = {n: hashlib.sha256(read(os.path.join(first, n), "rb")).hexdigest() for n in os.listdir(first)}
        self.assertEqual(before, after)
        stops = [c for c in self.d.calls() if c[:3] == ["systemctl", "--user", "stop"]]
        self.assertEqual(len(stops), 1)
        # first/, state.json, and the Krema settings kept when Animated was left (for the next Animated).
        self.assertEqual(sorted(os.listdir(self.d.backup)), ["first", "kremarc-kept", "kremarc-kept.restored", "state.json"])

    def test_animated_writes_kremarc_autostart_and_starts_krema(self):
        self.d.write("home/.config/kremarc", "[General]\nIconSize=30\nPinnedLaunchers=applications:firefox.desktop\n")
        self.d.set_up("krema")                      # the user's own Krema is running
        out = self.ok({"style": "animated", "icon_size": 64})
        self.assertIn("Krema", out)
        krc = self.d.read("home/.config/kremarc")
        self.assertIn("\nIconSize=64\n", krc)
        self.assertIn("\nPinnedLaunchers=applications:firefox.desktop\n", krc)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.d.config, "kremarc")).st_mode), 0o600)
        auto = self.d.read("home/.config/autostart/forge-krema.desktop")
        self.assertIn("\nExec=" + os.path.join(self.d.bin, "krema") + "\n", auto)
        self.assertIn("\nX-Forge-Managed=dock\n", auto)
        calls = self.d.calls()
        quit_i = calls.index(["dbus-send", "--session", "--print-reply", "--reply-timeout=20000",
                              "--dest=com.bhyoo.krema", "/MainApplication",
                              "org.qtproject.Qt.QCoreApplication.quit"])
        run_i = calls.index(["systemd-run", "--user", "--collect", "--quiet", "--unit=forge-krema", "--",
                             os.path.join(self.d.bin, "krema")])
        self.assertLess(quit_i, run_i)
        self.assertTrue(self.d.up("krema"))
        # The stock panel became a slim bar at the top; the other panel is untouched.
        p = self.d.panels()
        self.assertEqual(p[1], dict(self.original()[1], location="top", height=30, lengthMode="fill", floating=False,
                                    hiding="none"))
        self.assertEqual(p[7], self.original()[7])
        # The user's kremarc from before Forge is in the backup.
        self.assertEqual(read(os.path.join(self.d.backup, "first", "kremarc")),
                         "[General]\nIconSize=30\nPinnedLaunchers=applications:firefox.desktop\n")

    def test_animated_restarts_krema_it_started_itself(self):
        self.ok({"style": "animated"})
        self.ok({"style": "animated", "zoom": 120})
        self.assertIn("\nMaxZoomFactor=1.2\n", self.d.read("home/.config/kremarc"))
        runs = [c for c in self.d.calls() if c[0] == "systemd-run"]
        quits = [c for c in self.d.calls() if c[-1] == "org.qtproject.Qt.QCoreApplication.quit"]
        self.assertEqual((len(runs), len(quits)), (2, 1))
        self.assertTrue(self.d.up("krema"))

    def test_switching_back_and_forth_ends_where_it_started(self):
        self.d.write("home/.config/kremarc", "[General]\nIconSize=30\n")
        for style, extra in (("floating", {}), ("animated", {"stock_panel": "autohide"}), ("floating", {"position": "top"}),
                             ("animated", {}), ("default", {}), ("animated", {"stock_panel": "keep"}),
                             ("floating", {}), ("default", {})):
            self.ok(dict({"style": style}, **extra))
            auto = os.path.exists(os.path.join(self.d.config, "autostart/forge-krema.desktop"))
            self.assertEqual(auto, style == "animated", style)
            self.assertEqual(self.d.up("krema"), style == "animated", style)
            if style == "default":
                self.assertEqual(self.d.panels(), self.original())
        self.assertEqual(self.d.read("home/.config/kremarc"), "[General]\nIconSize=30\n")

    def test_default_removes_kremarc_when_there_was_none(self):
        self.ok({"style": "animated"})
        self.ok({"style": "default"})
        self.assertFalse(os.path.exists(os.path.join(self.d.config, "kremarc")))
        self.assertFalse(os.path.exists(os.path.join(self.d.config, "autostart/forge-krema.desktop")))

    def test_a_users_own_autostart_file_is_left_alone(self):
        os.makedirs(os.path.join(self.d.config, "autostart"))
        self.d.write("home/.config/autostart/forge-krema.desktop", "[Desktop Entry]\nName=mine\n")
        self.ok({"style": "floating"})
        self.assertEqual(self.d.read("home/.config/autostart/forge-krema.desktop"), "[Desktop Entry]\nName=mine\n")

    def test_a_dangling_kremarc_link_is_refused_and_left_alone(self):
        link = os.path.join(self.d.config, "kremarc")
        os.symlink(os.path.join(self.d.home, "nowhere"), link)
        r = self.d.apply({"style": "animated"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("is a link", r.stdout)
        self.assertTrue(os.path.islink(link))
        self.assertFalse(os.path.exists(os.path.join(self.d.home, "nowhere")))
        self.assertEqual(self.d.panels(), self.original())

    def test_locked_widgets_fail_clearly(self):
        open(os.path.join(self.d.state, "locked"), "w").close()
        r = self.d.apply({"style": "floating"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Widgets are locked", r.stdout)
        self.assertEqual(self.d.panels(), self.original())

    def test_plasma_not_running_changes_nothing(self):
        self.d.set_up("plasma", False)
        for style in ("floating", "animated"):
            r = self.d.apply({"style": style})
            self.assertEqual(r.returncode, 1)
            self.assertIn("Plasma is not running", r.stdout)
        self.assertFalse(os.path.exists(self.d.backup))
        self.assertFalse(os.path.exists(os.path.join(self.d.config, "kremarc")))

    def test_no_session_bus_is_a_plain_message(self):
        open(os.path.join(self.d.state, "no_bus"), "w").close()
        r = self.d.apply({"style": "floating"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Plasma is not running", r.stdout)
        self.assertIn("session bus", r.stdout)

    def test_hostile_stdin(self):
        r = self.d.apply(stdin="not json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not read", r.stdout)
        r = self.d.apply(stdin=json.dumps({"values": {"style": "floating\"); evil(\""}}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)     # an unknown style is the default
        self.assertEqual(self.d.scripts(), [])
        r = self.d.apply(stdin=json.dumps({"values": {"style": "floating", "icon_size": "1); evil(", "position": 5}}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.d.panels()[1]["height"], 56)
        for s in self.d.scripts():
            self.assertNotIn("evil", s)

    def test_backup_folder_that_is_a_link_is_refused(self):
        os.makedirs(os.path.join(self.d.home, ".local/share/forge/appearance"))
        elsewhere = os.path.join(self.d.tmp, "elsewhere")
        os.mkdir(elsewhere)
        os.symlink(elsewhere, self.d.backup)
        r = self.d.apply({"style": "floating"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a private folder", r.stdout)
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual(self.d.panels(), self.original())

    def test_wrong_argv_and_root(self):
        r = self.d.apply(argv=())
        self.assertEqual(r.returncode, 2)
        r = self.d.apply(argv=("apply", "extra"))
        self.assertEqual(r.returncode, 2)
        if os.geteuid() == 0:
            r = self.d.apply({"style": "floating"}, root=True)
            self.assertEqual(r.returncode, 2)
            self.assertIn("root", r.stdout + r.stderr)
            self.assertEqual(self.d.calls(), [])


@NEEDS_NODE
class NoKremaTest(unittest.TestCase):
    def test_animated_without_krema_says_so_and_changes_nothing(self):
        d = Desktop(krema=False)
        self.addCleanup(d.close)
        r = d.apply({"style": "animated"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Krema is not installed", r.stdout)
        self.assertEqual(d.scripts(), [])
        self.assertFalse(os.path.exists(d.backup))
        self.assertFalse(os.path.exists(os.path.join(d.config, "kremarc")))
        self.assertEqual(d.panels(), {p["id"]: p["props"] for p in STOCK_PANELS})


class PreviewTest(unittest.TestCase):
    def setUp(self):
        with open(PREVIEW) as f:
            self.js = f.read()

    def test_follows_the_page_rules(self):
        self.assertIn('forgeAppearance.registerPreview("dock"', self.js)
        for bad in ("innerHTML", "outerHTML", "setAttribute(\"style\"", "eval(", "new Function", "http:", "https:",
                    "insertAdjacentHTML", "document.write", "—"):
            self.assertNotIn(bad, self.js)
        self.assertLess(len(self.js.encode()), 256 * 1024)

    @NEEDS_NODE
    def test_runs_for_every_style(self):
        # A tiny stand-in DOM: enough to run the hook and read back the styles it set.
        harness = r'''
const vm = require("vm"), fs = require("fs");
function node(tag) {
  const n = {tagName: tag, children: [], style: {}, dataset: {}, classList: {add() {}, toggle() {}},
             listeners: {}, clientWidth: 480, clientHeight: 300,
             append(...k) { k.forEach(c => { c.parentNode = n; n.children.push(c); }); },
             appendChild(c) { n.append(c); return c; },
             querySelector(s) { return n.children.find(c => c.className === s.slice(1)) || null; },
             querySelectorAll(s) { return s === "i" ? n.children.filter(c => c.tagName === "i") : []; },
             addEventListener(t, f) { (n.listeners[t] = n.listeners[t] || []).push(f); },
             getBoundingClientRect() { return {left: 0, top: 0, width: 200, height: 40}; }};
  n.style.setProperty = (k, v) => { n.style[k] = v; };
  return n;
}
let hook = null;
const sandbox = {forgeAppearance: {registerPreview(s, f) { if (s === "dock") hook = f; }},
                 document: {createElement: node}, matchMedia: () => ({matches: false}), Math, JSON, String, Number,
                 window: {}};
sandbox.window = sandbox;
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), sandbox);
const values = JSON.parse(process.argv[3]);
const out = {};
for (const style of ["default", "floating", "animated"]) {
  const el = node("div"), dock = node("div"), wall = node("div"), win = node("div");
  for (let i = 0; i < 6; i++) dock.append(node("i"));
  el.append(wall, win, dock);
  const v = Object.assign({}, values, {style});
  hook(el, {values: v, all: {dock: v}, media: {}, fields: [], wall, window: win, dock, boot: node("div")});
  hook(el, {values: v, all: {dock: v}, media: {}, fields: [], wall, window: win, dock, boot: node("div")});
  (dock.listeners.mousemove || []).forEach(f => f({clientX: 60, clientY: 10}));
  out[style] = {dock: dock.style, icon: dock.children[0].style, moves: (dock.listeners.mousemove || []).length,
                extra: el.children.length};
}
console.log(JSON.stringify(out));
'''
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        h = os.path.join(tmp, "h.js")
        with open(h, "w") as f:
            f.write(harness)
        r = subprocess.run([NODE, h, PREVIEW, json.dumps(defaults())], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["default"]["dock"]["borderRadius"], "0px")         # the stock full-width panel
        self.assertEqual(out["default"]["dock"]["left"], "0px")
        self.assertNotEqual(out["floating"]["dock"]["borderRadius"], "0px")
        self.assertIn("translate", out["floating"]["dock"]["transform"])
        self.assertEqual(out["animated"]["moves"], 1, "one hover listener, even after two calls")
        self.assertIn("scale", out["animated"]["icon"].get("transform", ""))      # hover zoom
        self.assertEqual(out["floating"]["moves"], 1)
        self.assertNotIn("scale(1.", out["floating"]["icon"].get("transform", ""))   # no zoom outside Animated


class KremaImageTest(unittest.TestCase):
    """files/scripts/install-krema.sh: optional, pinned, and never fails the image build."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "root")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(os.path.join(self.root, "etc/xdg/autostart"))
        os.makedirs(self.bin)
        log = os.path.join(self.tmp, "calls")
        for name in ("curl", "dnf"):
            with open(os.path.join(self.bin, name), "w") as f:
                f.write(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
                if name == "dnf":
                    f.write(f'[ -e "{self.tmp}/dnf-fails" ] && exit 1\n'
                            f'echo x > "{self.root}/etc/xdg/autostart/com.bhyoo.krema.autostart.desktop"\n')
            os.chmod(os.path.join(self.bin, name), 0o755)
        self.log = log

    def run_script(self, script, **env):
        e = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], FORGE_KREMA_ROOT=self.root, **env)
        return subprocess.run(["bash", script], env=e, capture_output=True, text=True, timeout=60)

    def calls(self):
        return read(self.log).splitlines() if os.path.exists(self.log) else []

    def test_shipped_pins_are_the_reviewed_ones(self):
        # Reviewed 2026-11-23: the newest COPR build for Fedora 45, and the COPR project's key.
        text = read(KREMA_SCRIPT)
        self.assertRegex(text, r'(?m)^KREMA_VERSION="0.13.0-1.fc45"$')
        self.assertRegex(text, r'(?m)^KREMA_KEY_FPR="18B2C521BF9E618E9F9D950C124E1771EDE61218"$')

    def test_empty_pins_skip_krema_and_the_environment_cannot_fill_them(self):
        path = self.pinned_copy("", version="")
        r = self.run_script(path, KREMA_VERSION="0.13.0-1.fc45", KREMA_KEY_FPR="A" * 40)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("install-krema: skipped", r.stdout)
        self.assertEqual(self.calls(), [], "nothing is downloaded or installed")

    def pinned_copy(self, fpr, version="0.13.0-1.fc45"):
        text = read(KREMA_SCRIPT)
        text = re.sub(r'(?m)^KREMA_VERSION=".*"$', f'KREMA_VERSION="{version}"', text)
        text = re.sub(r'(?m)^KREMA_KEY_FPR=".*"$', f'KREMA_KEY_FPR="{fpr}"', text)
        path = os.path.join(self.tmp, "install-krema.sh")
        with open(path, "w") as f:
            f.write(text)
        return path

    def key(self):
        home = os.path.join(self.tmp, "g")
        os.makedirs(home, mode=0o700)
        env = dict(os.environ, GNUPGHOME=home)
        subprocess.run(["gpg", "--batch", "--passphrase", "", "--quick-gen-key", "Fake <f@example.com>",
                        "rsa2048", "sign", "never"], env=env, check=True, capture_output=True)
        keys = os.path.join(self.tmp, "keys")
        os.makedirs(keys)
        with open(os.path.join(keys, "krema.key"), "w") as f:
            subprocess.run(["gpg", "--armor", "--export"], env=env, check=True, stdout=f)
        out = subprocess.run(["gpg", "--with-colons", "--list-keys"], env=env, check=True, capture_output=True,
                             text=True).stdout
        fpr = [line.split(":")[9] for line in out.splitlines() if line.startswith("fpr")][0]
        return keys, fpr

    @unittest.skipUnless(shutil.which("gpg"), "gpg not installed")
    def test_right_key_installs_the_exact_version_and_removes_the_global_autostart(self):
        keys, fpr = self.key()
        r = self.run_script(self.pinned_copy(fpr), FORGE_KEY_TEST_DIR=keys)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls(), ["dnf -y --enablerepo=forge-krema install krema-0.13.0-1.fc45"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "etc/xdg/autostart/com.bhyoo.krema.autostart.desktop")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "etc/yum.repos.d/forge-krema.repo")))
        self.assertIn("install-krema: installed krema-0.13.0-1.fc45", r.stdout)

    @unittest.skipUnless(shutil.which("gpg"), "gpg not installed")
    def test_wrong_key_or_failed_install_skips_without_failing_the_build(self):
        keys, fpr = self.key()
        r = self.run_script(self.pinned_copy("B" * 40), FORGE_KEY_TEST_DIR=keys)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("NOT installed", r.stdout)
        self.assertEqual(self.calls(), [])
        open(os.path.join(self.tmp, "dnf-fails"), "w").close()
        r = self.run_script(self.pinned_copy(fpr), FORGE_KEY_TEST_DIR=keys)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("NOT installed", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.root, "etc/yum.repos.d/forge-krema.repo")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "etc/pki/rpm-gpg/forge-krema.gpg")))

    def test_bad_pin_text_is_refused(self):
        r = self.run_script(self.pinned_copy("A" * 40, version="1.0; rm -rf /"))
        self.assertEqual(r.returncode, 0)
        self.assertIn("NOT installed", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_recipe_runs_it_after_packages_and_finalize_marks_the_backend(self):
        recipe = read(RECIPE)
        self.assertIn("- install-krema.sh", recipe)
        self.assertLess(recipe.find("type: dnf"), recipe.find("install-krema.sh"))
        self.assertTrue(os.access(KREMA_SCRIPT, os.X_OK))
        self.assertIn("/usr/libexec/forge-appearance-dock", read(FINALIZE))


if __name__ == "__main__":
    unittest.main()
