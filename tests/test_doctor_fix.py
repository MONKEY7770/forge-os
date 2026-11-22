"""forge-doctor --fix: guided fixes, each one confirmed on its own, root steps only through a fixed helper."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
TOOL = os.path.join(ROOT, "files/system/usr/bin/forge-doctor")
HELPER = os.path.join(ROOT, "files/system/usr/libexec/forge-doctor-fix")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
sys.path.insert(0, LIB)

from forgecore import doctor as D  # noqa: E402

HELPER_PATH = "/usr/libexec/forge-doctor-fix"
NV_IMAGE = "ostree-image-signed:docker://ghcr.io/monkey7770/forge-os:latest"
UNI_IMAGE = "ostree-image-signed:docker://ghcr.io/monkey7770/forge-os-universal:latest"
HOSTILE = "EVIL$(touch /tmp/pwned)`id`;--now --root=/ x\n restart"

# Every argv a fix may ever run as root, and nothing else.
EXPECTED = {
    "restart-ollama": [["systemctl", "restart", "ollama.service"]],
    "restart-forge-core": [["systemctl", "restart", "forge-core.service"]],
    "usb-unlock-off": [["forge-config", "set", "security.usb_unlock", "off"]],
    "run-hwcheck": [["systemctl", "start", "forge-hwcheck.service"]],
    "start-forge-virt": [["systemctl", "--no-block", "start", "forge-virt.service"]],
}
PRINT_ONLY = {
    "switch-to-nvidia-image": [["rpm-ostree", "rebase", NV_IMAGE]],
    "switch-to-universal-image": [["rpm-ostree", "rebase", UNI_IMAGE]],
}

# Fake system tools. Each logs its argv (one JSON list per line) and keeps state in files under $FAKE.
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
fake = os.environ["FAKE"]
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
with open(os.path.join(fake, "calls"), "a") as f:
    f.write(json.dumps([name] + args) + "\n")
def path(*p): return os.path.join(fake, *p)
def read(p, d=""):
    try: return open(path(p)).read().strip()
    except OSError: return d
if name == "pkexec":
    if args[:1] == ["/usr/libexec/forge-doctor-fix"]:
        os.execv(sys.executable, [sys.executable, os.environ["FAKE_HELPER"]] + args[1:])
    sys.exit(127)
elif name == "systemctl":
    if args[:1] == ["is-active"]:
        s = read("active-" + args[-1], "active"); print(s); sys.exit(0 if s == "active" else 3)
    if args[:1] == ["is-enabled"]:
        s = read("enabled-" + args[-1], "enabled"); print(s); sys.exit(0 if s == "enabled" else 1)
    if os.path.exists(path("fail")):
        sys.exit(1)
    if args[-2:] == ["start", "forge-virt.service"]:
        open(path("enabled-libvirtd.service"), "w").write("enabled")
    elif args[-2:] == ["start", "forge-hwcheck.service"]:
        open(os.environ["HW_JSON"], "w").write(open(path("hw.json")).read())
    elif args[0] in ("start", "restart"):
        open(path("active-" + args[-1]), "w").write("active")
elif name == "forge-config":
    conf = os.path.join(os.environ["FAKE_ROOT"], "etc/forge/settings.json")
    data = json.load(open(conf))
    data[args[1]] = args[2] == "on"
    json.dump(data, open(conf, "w"))
elif name == "rpm-ostree":
    print(read("rpm-ostree.json"))
elif name == "mokutil":
    print("SecureBoot enabled")
'''


def write(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def ostree(ref):
    return json.dumps({"deployments": [{"booted": True, "version": "45.1", "container-image-reference": ref}]})


class FakeRun:
    """For build_report: answers commands from a table; anything else is "not installed"."""

    def __init__(self, answers):
        self.answers = answers

    def __call__(self, cmd, **kw):
        for prefix, (rc, out) in self.answers.items():
            if " ".join(cmd).startswith(prefix):
                return subprocess.CompletedProcess(cmd, rc, out, "")
        raise FileNotFoundError(cmd[0])


HW = {"type": "desktop", "cpu": "Ryzen 7", "ram_gb": 64, "gpus": ["NVIDIA RTX 5070"], "disks": ["SSD (2000 GB)"],
      "battery": False, "details": {"gpus": [{"vendor": "NVIDIA", "driver": "nvidia", "vram_mb": 12227}],
                                    "battery": [], "network": [], "bluetooth": []}, "checks": []}


class Box(unittest.TestCase):
    """A healthy fake PC; tests break one thing at a time."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.hw_path = os.path.join(self.root, "var/lib/forge-hw/hardware.json")
        self.hw = json.loads(json.dumps(HW))
        self.save_hw()
        write(os.path.join(self.root, "usr/share/forge/settings.json"), open(SCHEMA).read())
        write(os.path.join(self.root, "etc/forge/settings.json"), "{}")
        write(os.path.join(self.root, "etc/crypttab"), "luks-root UUID=abc none discard\n")
        write(os.path.join(self.root, "proc/cpuinfo"), "flags : fpu svm\n")
        write(os.path.join(self.root, "dev/kvm"), "")
        self.answers = {"rpm-ostree status --json": (0, ostree(NV_IMAGE)), "mokutil --sb-state": (0, "SecureBoot enabled"),
                        "systemctl is-active": (0, "active\n"), "systemctl is-enabled": (0, "enabled\n")}

    def save_hw(self):
        write(self.hw_path, json.dumps(self.hw))

    def settings(self, **kv):
        write(os.path.join(self.root, "etc/forge/settings.json"), json.dumps({k.replace("__", "."): v for k, v in kv.items()}))

    def report(self):
        return D.build_report(root=self.root, run=FakeRun(self.answers))

    def ids(self):
        return [f["id"] for f in self.report()["fixes"]]


class DetectionTest(Box):
    def test_healthy_box_has_no_fixes(self):
        self.assertEqual(self.ids(), [])

    def test_ollama_stopped_while_its_setting_is_on(self):
        self.answers = {"systemctl is-active ollama.service": (3, "failed\n"), **self.answers}
        self.assertEqual(self.ids(), ["restart-ollama"])

    def test_fixable_problems_count_in_the_summary(self):
        self.answers = {"systemctl is-active forge-core.service": (3, "inactive\n"), **self.answers}
        rep = self.report()
        self.assertEqual(rep["summary"]["fixable"], len(rep["fixes"]))
        self.assertGreaterEqual(rep["summary"]["fixable"], 1)
        self.assertIn(f"{rep['summary']['fixable']} with a guided fix", D.to_text(rep))

    def test_ollama_off_on_purpose_is_not_a_problem(self):
        self.settings(ai__ollama=False)
        self.answers = {"systemctl is-active ollama.service": (3, "inactive\n"), **self.answers}
        self.assertEqual(self.ids(), [])

    def test_forge_core_stopped(self):
        self.answers = {"systemctl is-active forge-core.service": (3, "inactive\n"), **self.answers}
        self.assertEqual(self.ids(), ["restart-forge-core"])
        self.settings(core__enabled=False)
        self.assertEqual(self.ids(), [])

    def test_usb_unlock_without_encryption(self):
        self.settings(security__usb_unlock=True)
        self.assertEqual(self.ids(), [])                      # encrypted: fine
        os.unlink(os.path.join(self.root, "etc/crypttab"))
        self.assertEqual(self.ids(), ["usb-unlock-off"])
        write(os.path.join(self.root, "etc/crypttab"), "# only a comment\n")
        self.assertEqual(self.ids(), ["usb-unlock-off"])

    def test_usb_unlock_with_unreadable_crypttab_is_not_sure(self):
        self.settings(security__usb_unlock=True)
        tab = os.path.join(self.root, "etc/crypttab")
        os.unlink(tab)
        os.mkdir(tab)
        self.assertEqual(self.ids(), [])

    def test_broken_settings_file_offers_no_setting_based_fix(self):
        write(os.path.join(self.root, "etc/forge/settings.json"), "{broken")
        self.answers = {"systemctl is-active ollama.service": (3, "failed\n"), **self.answers}
        self.assertEqual(self.ids(), [])

    def test_image_does_not_match_gpu(self):
        self.answers["rpm-ostree status --json"] = (0, ostree(UNI_IMAGE))
        self.assertEqual(self.ids(), ["switch-to-nvidia-image"])
        self.hw["details"]["gpus"] = [{"vendor": "AMD", "driver": "amdgpu", "vram_mb": 16000}]
        self.save_hw()
        self.answers["rpm-ostree status --json"] = (0, ostree("ostree-unverified-registry:ghcr.io/monkey7770/forge-os:latest"))
        self.assertEqual(self.ids(), ["switch-to-universal-image"])

    def test_unknown_image_offers_no_rebase(self):
        self.hw["details"]["gpus"] = [{"vendor": "AMD"}]
        self.save_hw()
        for ref in ("ostree-image-signed:docker://ghcr.io/x/bazzite:stable",
                    "ostree-image-signed:docker://ghcr.io/someone/forge-os:latest",      # a fork
                    "ostree-image-signed:docker://ghcr.io/monkey7770/forge-os:latest x"):
            self.answers["rpm-ostree status --json"] = (0, ostree(ref))
            self.assertEqual(self.ids(), [], ref)

    def test_missing_hardware_profile(self):
        os.unlink(self.hw_path)
        self.assertEqual(self.ids(), ["run-hwcheck"])

    def test_vms_on_but_libvirtd_not_set_up(self):
        self.answers = {"systemctl is-enabled libvirtd.service": (1, "disabled\n"), **self.answers}
        self.assertEqual(self.ids(), [])                      # VMs off: nothing to do
        self.settings(virt__enabled=True)
        self.assertEqual(self.ids(), ["start-forge-virt"])

    def test_fix_metadata(self):
        self.settings(security__usb_unlock=True, virt__enabled=True)
        os.unlink(os.path.join(self.root, "etc/crypttab"))
        self.answers = {"systemctl is-active ollama.service": (3, "failed\n"),
                        "systemctl is-active forge-core.service": (3, "failed\n"),
                        "systemctl is-enabled libvirtd.service": (1, "disabled\n"), **self.answers}
        self.answers["rpm-ostree status --json"] = (0, ostree(UNI_IMAGE))
        fixes = self.report()["fixes"]
        self.assertEqual(sorted(f["id"] for f in fixes),
                         sorted(set(EXPECTED) - {"run-hwcheck"} | {"switch-to-nvidia-image"}))
        for f in fixes:
            self.assertEqual(set(f), {"id", "title", "problem", "explanation", "commands", "needs_root",
                                      "reversible", "risk", "automatic", "run_with"}, f["id"])
            self.assertIn(f["risk"], ("low", "medium"))
            for text in (f["title"], f["problem"], f["explanation"]):
                self.assertTrue(text and "\u2014" not in text and "--" not in text.replace("--no-block", ""), f["id"])
            if f["id"] in EXPECTED:
                self.assertEqual(f["commands"], EXPECTED[f["id"]])
                self.assertEqual(f["run_with"], ["pkexec", HELPER_PATH, f["id"]])
                self.assertTrue(f["automatic"])
            else:
                self.assertEqual(f["commands"], PRINT_ONLY[f["id"]])
                self.assertEqual(f["risk"], "medium")
                self.assertFalse(f["automatic"])
                self.assertIsNone(f["run_with"])

    def test_problems_without_a_fix_get_a_how_to_line(self):
        self.answers["mokutil --sb-state"] = (0, "SecureBoot disabled")
        self.hw["details"]["gpus"] = [{"vendor": "NVIDIA", "driver": "nouveau", "vram_mb": 12227}]
        self.save_hw()
        os.unlink(os.path.join(self.root, "etc/crypttab"))
        rep = self.report()
        sec = {s["name"]: s for s in rep["sections"]}
        self.assertTrue(any(i.startswith("How to fix:") and "MOK" in i for i in sec["Security"]["items"]))
        self.assertTrue(any(i.startswith("How to fix:") and "reinstall" in i for i in sec["Security"]["items"]))
        self.assertTrue(any(i.startswith("How to fix:") for i in sec["AI readiness"]["items"]))
        text = D.to_text(rep)
        self.assertNotIn("\u2014", text)

    def test_text_report_lists_the_fixes(self):
        self.answers = {"systemctl is-active ollama.service": (3, "failed\n"), **self.answers}
        text = D.to_text(self.report())
        self.assertIn("forge-doctor --fix", text)
        self.assertIn("Start Ollama again", text)


def load_helper():
    loader = importlib.machinery.SourceFileLoader("forge_doctor_fix", HELPER)
    spec = importlib.util.spec_from_loader("forge_doctor_fix", loader)
    mod = importlib.util.module_from_spec(spec)
    old, sys.dont_write_bytecode = sys.dont_write_bytecode, True   # no .pyc inside files/
    try:
        loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = old
    return mod


class FakeTools(Box):
    def setUp(self):
        super().setUp()
        self.fake = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.fake, True)
        self.bin = os.path.join(self.fake, "bin")
        os.mkdir(self.bin)
        for name in ("pkexec", "systemctl", "forge-config", "rpm-ostree", "mokutil"):
            p = os.path.join(self.bin, name)
            write(p, FAKE)
            os.chmod(p, 0o755)
        write(os.path.join(self.fake, "rpm-ostree.json"), ostree(NV_IMAGE))
        write(os.path.join(self.fake, "hw.json"), json.dumps(HW))
        self.env = dict(os.environ, FAKE=self.fake, FAKE_HELPER=HELPER, FAKE_ROOT=self.root, HW_JSON=self.hw_path,
                        FORGE_LIB=LIB, FORGE_DOCTOR_ROOT=self.root, HOME=self.fake,
                        PATH=self.bin + ":/usr/bin:/bin", FORGE_TEST_HELPER_PATH=self.bin + ":/usr/bin:/bin",
                        FORGE_ROOT=self.root)

    def test_helper_ignores_the_callers_path(self):
        # Root runs only the real system tools, never one found first on a caller's PATH.
        env = dict(self.env)
        env.pop("FORGE_TEST_HELPER_PATH")
        r = subprocess.run([sys.executable, HELPER, "restart-ollama"], env=env, capture_output=True, text=True,
                           timeout=30)
        self.assertFalse([c for c in self.calls() if c[:1] == ["systemctl"]], self.calls())
        self.assertNotIn("Traceback", r.stderr)

    def calls(self):
        try:
            with open(os.path.join(self.fake, "calls")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def changing(self):
        """Calls that change something (not the read-only status questions)."""
        return [c for c in self.calls() if not (c[0] == "systemctl" and c[1] in ("is-active", "is-enabled"))
                and c[0] not in ("rpm-ostree", "mokutil")]

    def state(self, name, value):
        write(os.path.join(self.fake, name), value)

    def helper(self, *args):
        return subprocess.run([sys.executable, HELPER, *args], env=self.env, capture_output=True, text=True, timeout=60)

    def doctor(self, *args, answers=""):
        return subprocess.run([sys.executable, TOOL, *args], env=self.env, input=answers, capture_output=True,
                              text=True, timeout=120)


class HelperTest(FakeTools):
    def test_each_fix_runs_its_exact_argv(self):
        for fid, argvs in EXPECTED.items():
            os.unlink(os.path.join(self.fake, "calls")) if os.path.exists(os.path.join(self.fake, "calls")) else None
            if fid == "usb-unlock-off":
                self.settings(security__usb_unlock=True)
            r = self.helper(fid)
            self.assertEqual(r.returncode, 0, (fid, r.stderr))
            self.assertEqual(self.calls(), argvs, fid)

    def test_unknown_ids_are_refused(self):
        for args in ([], [""], ["restart-ollama "], ["RESTART-OLLAMA"], ["restart-ollama;id"], ["../restart-ollama"],
                     ["restart-ollama", "extra"], ["--help"], ["switch-to-nvidia-image"],
                     ["switch-to-universal-image"], ["__class__"], ["restart-ollama\n"], ["keys"]):
            r = self.helper(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertEqual(self.calls(), [], args)

    def test_failure_is_reported(self):
        self.state("fail", "1")
        r = self.helper("restart-ollama")
        self.assertNotEqual(r.returncode, 0)

    def test_helper_table_is_the_doctor_catalog(self):
        h = load_helper()
        self.assertEqual(h.FIXES, EXPECTED)
        auto = {k: v["commands"] for k, v in D.FIXES.items() if v["automatic"]}
        self.assertEqual(auto, EXPECTED)
        self.assertEqual({k: v["commands"] for k, v in D.FIXES.items() if not v["automatic"]}, PRINT_ONLY)

    def test_helper_never_uses_a_shell(self):
        src = open(HELPER).read()
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)
        self.assertTrue(src.startswith("#!/usr/bin/env python3"))

    def test_finalize_makes_the_helper_executable(self):
        self.assertIn("/usr/libexec/forge-doctor-fix", open(os.path.join(ROOT, "files/scripts/finalize.sh")).read())


APPLY = os.path.join(ROOT, "files/system/usr/libexec/forge-apply")


def load(name, path, environ):
    """Import a tool as a module with exactly this environment."""
    from unittest import mock
    with mock.patch.dict(os.environ, environ, clear=True):
        loader = importlib.machinery.SourceFileLoader(name, path)
        spec = importlib.util.spec_from_loader(name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
    return mod


class HelperEnvTest(unittest.TestCase):
    """What the root helper hands to forge-config (and so to forge-apply)."""

    def test_caller_is_passed_on_so_forge_apply_records_them(self):
        from unittest import mock
        h = load("fix_env1", HELPER, {"PKEXEC_UID": "65534", "PATH": "/evil", "LD_PRELOAD": "/evil.so"})
        self.assertEqual(h.SAFE_ENV.get("PKEXEC_UID"), "65534")
        self.assertEqual(h.SAFE_ENV["PATH"], "/usr/sbin:/usr/bin")
        self.assertNotIn("LD_PRELOAD", h.SAFE_ENV)
        fa = load("apply_who", APPLY, {})
        with mock.patch.dict(os.environ, h.SAFE_ENV, clear=True):
            self.assertEqual(fa.who(), "nobody")
        h = load("fix_env2", HELPER, {"SUDO_USER": "alice"})
        self.assertEqual(h.SAFE_ENV.get("SUDO_USER"), "alice")
        with mock.patch.dict(os.environ, h.SAFE_ENV, clear=True):
            self.assertEqual(fa.who(), "alice")

    def test_odd_caller_values_are_dropped(self):
        for env in ({"PKEXEC_UID": "0 x"}, {"PKEXEC_UID": "-1"}, {"PKEXEC_UID": "1" * 30},
                    {"SUDO_USER": "../x$(id)"}, {"SUDO_USER": "a\nb"}, {"SUDO_USER": "x" * 64}):
            h = load("fix_env3", HELPER, env)
            self.assertEqual(set(h.SAFE_ENV), {"PATH", "LANG"}, env)

    def test_fake_tool_path_needs_the_test_marker(self):
        h = load("fix_env4", HELPER, {"FORGE_TEST_HELPER_PATH": "/tmp/evil"})
        self.assertEqual(h.SAFE_ENV["PATH"], "/usr/sbin:/usr/bin")
        h = load("fix_env5", HELPER, {"FORGE_TEST_HELPER_PATH": "/tmp/fake", "FORGE_ROOT": "/tmp/r"})
        self.assertEqual(h.SAFE_ENV["PATH"], "/tmp/fake")

    def test_fake_tool_path_never_from_usr(self):
        src = open(HELPER).read()
        installed = os.path.join(tempfile.mkdtemp(), "usr/libexec/forge-doctor-fix")
        self.addCleanup(shutil.rmtree, os.path.dirname(os.path.dirname(os.path.dirname(installed))), True)
        write(installed, src)
        from unittest import mock
        with mock.patch.object(os.path, "realpath", lambda p: "/usr/libexec/forge-doctor-fix"):
            h = load("fix_env6", installed, {"FORGE_TEST_HELPER_PATH": "/tmp/evil", "FORGE_ROOT": "/tmp/r"})
        self.assertEqual(h.SAFE_ENV["PATH"], "/usr/sbin:/usr/bin")


class CliTest(FakeTools):
    def break_ollama(self):
        self.state("active-ollama.service", "failed")

    def test_no_fix_without_confirmation(self):
        self.break_ollama()
        for answers in ("", "\n", "n\n", "no\n", "N\n", "maybe\n", "yy\n", " \n"):
            r = self.doctor("--fix", answers=answers)
            self.assertNotIn("Traceback", r.stderr, answers)
            self.assertEqual(self.changing(), [], answers)
            self.assertIn("Skipped", r.stdout, answers)

    def test_confirmed_fix_runs_through_pkexec_and_is_rechecked(self):
        self.break_ollama()
        r = self.doctor("--fix", answers="y\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.changing(), [["pkexec", HELPER_PATH, "restart-ollama"],
                                           ["systemctl", "restart", "ollama.service"]])
        # Checked again afterwards: the re-check asks systemctl after the restart.
        calls = self.calls()
        restart = calls.index(["systemctl", "restart", "ollama.service"])
        self.assertIn(["systemctl", "is-active", "ollama.service"], calls[restart + 1:])
        self.assertIn("Checked again", r.stdout)
        self.assertIn("fixed", r.stdout.lower())

    def test_recheck_says_when_the_problem_is_still_there(self):
        self.break_ollama()
        self.state("fail", "1")
        r = self.doctor("--fix", answers="y\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("still found", r.stdout.lower())

    def test_each_fix_is_asked_separately(self):
        self.break_ollama()
        self.state("active-forge-core.service", "failed")
        r = self.doctor("--fix", answers="n\ny\n")
        ran = [c for c in self.changing() if c[0] == "pkexec"]
        self.assertEqual(len(ran), 1, r.stdout)
        self.assertEqual(r.stdout.count("[y/N]"), 2)

    def test_dry_run_runs_nothing(self):
        self.break_ollama()
        self.state("active-forge-core.service", "failed")
        r = self.doctor("--fix", "--dry-run", answers="y\ny\ny\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.changing(), [])
        self.assertIn("systemctl restart ollama.service", r.stdout)
        self.assertIn("pkexec /usr/libexec/forge-doctor-fix restart-ollama", r.stdout)
        self.assertNotIn("[y/N]", r.stdout)

    def test_image_switch_is_only_shown(self):
        write(os.path.join(self.fake, "rpm-ostree.json"), ostree(UNI_IMAGE))
        r = self.doctor("--fix", answers="y\ny\ny\nyes\n")
        self.assertEqual(self.changing(), [])
        self.assertIn("rpm-ostree rebase " + NV_IMAGE, r.stdout)
        self.assertIn("medium", r.stdout.lower())
        self.assertNotIn("[y/N]", r.stdout)

    def test_usb_unlock_turned_off_through_forge_config(self):
        self.settings(security__usb_unlock=True)
        os.unlink(os.path.join(self.root, "etc/crypttab"))
        r = self.doctor("--fix", answers="y\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.changing(), [["pkexec", HELPER_PATH, "usb-unlock-off"],
                                           ["forge-config", "set", "security.usb_unlock", "off"]])
        self.assertIn("fixed", r.stdout.lower())

    def test_nothing_to_fix(self):
        r = self.doctor("--fix")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Nothing to fix", r.stdout)
        self.assertEqual(self.changing(), [])

    def test_odd_arguments_are_refused(self):
        for args in (["--fix", "--yes"], ["--fix", "restart-ollama"], ["--dry-run"], ["--fix", "--dry-run", "x"]):
            r = self.doctor(*args, answers="y\n")
            self.assertNotEqual(r.returncode, 0, args)
            self.assertEqual(self.changing(), [], args)

    def test_json_has_fix_metadata(self):
        self.break_ollama()
        r = self.doctor("--json")
        fixes = json.loads(r.stdout)["fixes"]
        self.assertEqual([f["id"] for f in fixes], ["restart-ollama"])
        self.assertEqual(self.changing(), [])

    def test_report_text_never_reaches_an_argv(self):
        # Hostile names from sysfs (a USB disk can name itself anything), odd checks and an odd image name.
        self.hw["disks"] = [HOSTILE, "--now"]
        self.hw["gpus"] = [HOSTILE]
        self.hw["cpu"] = HOSTILE
        self.hw["checks"] = [HOSTILE, "rpm-ostree rebase " + HOSTILE]
        self.hw["details"]["network"] = [{"name": HOSTILE, "kind": HOSTILE}]
        self.save_hw()
        self.settings(security__usb_unlock=True, virt__enabled=True)
        os.unlink(os.path.join(self.root, "etc/crypttab"))
        write(os.path.join(self.fake, "rpm-ostree.json"),
              ostree("ostree-image-signed:docker://ghcr.io/x/" + HOSTILE + "/forge-os-universal:latest"))
        self.break_ollama()
        self.state("active-forge-core.service", "failed")
        self.state("enabled-libvirtd.service", "disabled")
        r = self.doctor("--fix", answers="y\n" * 10)
        self.assertNotIn("Traceback", r.stderr)
        allowed = [a for argvs in EXPECTED.values() for a in argvs] + \
                  [["pkexec", HELPER_PATH, fid] for fid in EXPECTED]
        changing = self.changing()
        self.assertTrue(changing)
        for c in changing:
            self.assertIn(c, allowed)
        self.assertNotIn("EVIL", json.dumps(self.calls()))
        self.assertNotIn("pwned", json.dumps(self.calls()))
        # The odd image name is not our image, so no rebase is offered at all.
        fixes = self.report_json()["fixes"]
        self.assertNotIn("EVIL", json.dumps(fixes))
        self.assertFalse([f for f in fixes if f["id"].startswith("switch-")], fixes)

    def report_json(self):
        return json.loads(self.doctor("--json").stdout)


class ApiTest(Box):
    """GET /api/doctor carries the fix list, and the API has no way to run a fix."""

    def setUp(self):
        super().setUp()
        try:
            import fastapi  # noqa: F401
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.environ.setdefault("FORGE_SCHEMA", SCHEMA)
        os.environ.setdefault("FORGE_CONF", os.path.join(tmp, "settings.json"))
        os.environ.setdefault("FORGE_CORE_RUN", os.path.join(tmp, "run"))
        os.environ.setdefault("FORGE_AGENTS_SYSTEM", os.path.join(ROOT, "files/system/usr/share/forge/agents"))
        from forgecore import app as core
        self.answers = {"systemctl is-active ollama.service": (3, "failed\n"), **self.answers}

        class Col:
            def current(self):
                return {"ts": 1}

            def start(self):
                pass

            def stop(self):
                pass

        class Oll:
            def available(self):
                return False

            def list(self):
                return []
        self.app = core.create_app(token="admin-t", viewer_token="view-t", collector=Col(), ollama=Oll(),
                                   start_collector=False, doctor=self.report, data_dir=os.path.join(tmp, "d"))
        self.c = TestClient(self.app)

    def test_fixes_are_in_the_report_as_metadata(self):
        r = self.c.get("/api/doctor", headers={"Authorization": "Bearer admin-t"})
        self.assertEqual(r.status_code, 200)
        [f] = r.json()["fixes"]
        self.assertEqual(f["id"], "restart-ollama")
        self.assertEqual(f["commands"], EXPECTED["restart-ollama"])

    def test_no_route_runs_a_fix(self):
        paths = [(getattr(r, "path", ""), sorted(getattr(r, "methods", None) or [])) for r in self.app.routes]
        doctor = [p for p in paths if "doctor" in p[0] or "fix" in p[0]]
        self.assertEqual([p[0] for p in doctor], ["/api/doctor"])
        self.assertEqual([m for m in doctor[0][1] if m != "HEAD"], ["GET"])
        for path in ("/api/doctor", "/api/doctor/fix", "/api/doctor/fix/restart-ollama", "/api/doctor/restart-ollama"):
            r = self.c.post(path, headers={"Authorization": "Bearer admin-t"}, json={"id": "restart-ollama"})
            self.assertIn(r.status_code, (404, 405), path)


if __name__ == "__main__":
    unittest.main()
