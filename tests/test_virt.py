"""P1 virtualization: a Forge setting that does what Bazzite's "ujust setup-virtualization"
does, and undoes only what Forge itself set up."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "files/system/usr/libexec/forge-virt")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
UNIT = os.path.join(ROOT, "files/system/usr/lib/systemd/system/forge-virt.service")

VM = "org.virt_manager.virt-manager"
QEMU = "org.virt_manager.virt_manager.Extension.Qemu"

# Fake system tools. Each logs its arguments and keeps its state in files under $FAKE.
FAKE = r'''#!/usr/bin/env python3
import os, sys
fake = os.environ["FAKE"]
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
with open(os.path.join(fake, "calls"), "a") as f:
    f.write(" ".join([name] + args) + "\n")
def path(*p): return os.path.join(fake, *p)
def read(p, d=""):
    try: return open(path(p)).read()
    except OSError: return d
def groups():
    out = {}
    for line in open(os.environ["FORGE_GROUP_FILE"]).read().splitlines():
        g, _, gid, mem = line.split(":")
        out[g] = (gid, [m for m in mem.split(",") if m])
    return out
def save_groups(gs):
    with open(os.environ["FORGE_GROUP_FILE"], "w") as f:
        for g, (gid, mem) in gs.items():
            f.write(f"{g}:x:{gid}:{','.join(mem)}\n")
if name == "flatpak":
    os.makedirs(path("flatpaks"), exist_ok=True)
    if args[0] == "info":
        sys.exit(0 if os.path.exists(path("flatpaks", args[-1])) else 1)
    if args[0] == "install":
        if os.path.exists(path("offline")):
            print("error: no network", file=sys.stderr); sys.exit(1)
        for a in args:
            if a.startswith("org."): open(path("flatpaks", a), "w").close()
    if args[0] == "uninstall":
        for a in args:
            if a.startswith("org.") and os.path.exists(path("flatpaks", a)): os.unlink(path("flatpaks", a))
elif name == "rpm-ostree":
    kargs = read("kargs").split()
    if args == ["kargs"]:
        print(" ".join(kargs))
    for a in args[1:]:
        k, v = a.split("=", 1)
        if k == "--append-if-missing" and v not in kargs: kargs.append(v)
        if k == "--delete-if-present" and v in kargs: kargs.remove(v)
    open(path("kargs"), "w").write(" ".join(kargs))
elif name == "systemctl":
    if args[:1] == ["is-enabled"]:
        state = read("unit-" + args[-1], "disabled").strip()
        print(state); sys.exit(0 if state == "enabled" else 1)
    if args[:1] in (["enable"], ["disable"]):
        open(path("unit-" + args[-1]), "w").write(args[0] + "d")
elif name == "groupadd":
    gs = groups(); gs[args[-1]] = ("975", []); save_groups(gs)
elif name == "usermod":
    gs = groups(); gid, mem = gs[args[-2]]; mem.append(args[-1]); save_groups(gs)
elif name == "gpasswd":
    gs = groups(); gid, mem = gs[args[-1]]
    if args[-2] in mem: mem.remove(args[-2])
    save_groups(gs)
'''


class VirtTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.bin = os.path.join(self.t, "bin")
        self.fake = os.path.join(self.t, "fake")
        self.state = os.path.join(self.t, "state")
        self.root = os.path.join(self.t, "root")
        os.makedirs(self.bin)
        os.makedirs(self.fake)
        for name in ("flatpak", "rpm-ostree", "systemctl", "groupadd", "usermod", "gpasswd",
                     "chown", "restorecon"):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(FAKE)
            os.chmod(p, 0o755)
        self.group_file = os.path.join(self.t, "group")
        self.write_groups({"root": [], "wheel": ["alice", "bob"], "users": ["alice", "carol"]})
        self.conf = os.path.join(self.t, "settings.json")
        # libvirtd.service is part of the image.
        os.makedirs(os.path.join(self.root, "usr/lib/systemd/system"))
        for unit in ("libvirtd.service", "bazzite-libvirtd-setup.service"):
            open(os.path.join(self.root, "usr/lib/systemd/system", unit), "w").close()

    def write_groups(self, gs):
        with open(self.group_file, "w") as f:
            for i, (g, mem) in enumerate(gs.items()):
                f.write(f"{g}:x:{i}:{','.join(mem)}\n")

    def members(self, group):
        for line in open(self.group_file).read().splitlines():
            g, _, _, mem = line.split(":")
            if g == group:
                return [m for m in mem.split(",") if m]
        return None

    def settings(self, on):
        with open(self.conf, "w") as f:
            json.dump({"virt.enabled": on}, f)

    def run_tool(self):
        env = dict(os.environ, FORGE_CONF=self.conf, FORGE_SCHEMA=SCHEMA, FORGE_LIB=LIB, FAKE=self.fake,
                   FORGE_VIRT_STATE=self.state, FORGE_VIRT_ROOT=self.root, FORGE_GROUP_FILE=self.group_file,
                   PATH=self.bin + ":" + os.environ["PATH"])
        return subprocess.run([sys.executable, TOOL], env=env, capture_output=True, text=True, timeout=30)

    def calls(self):
        p = os.path.join(self.fake, "calls")
        calls = open(p).read().splitlines() if os.path.exists(p) else []
        open(p, "w").close()
        return calls

    def changes(self, calls):
        """Calls that change something (not the read-only checks)."""
        return [c for c in calls if not (c.startswith("flatpak info") or c.startswith("systemctl is-enabled")
                                         or c == "rpm-ostree kargs")]

    def installed(self, app):
        return os.path.exists(os.path.join(self.fake, "flatpaks", app))

    def unit(self, name):
        p = os.path.join(self.fake, "unit-" + name)
        return open(p).read().strip() if os.path.exists(p) else "disabled"

    def kargs(self):
        p = os.path.join(self.fake, "kargs")
        return open(p).read().split() if os.path.exists(p) else []

    def test_off_by_default_changes_nothing(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.changes(self.calls()), [])

    def test_on_does_what_bazzite_does(self):
        self.settings(True)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.installed(VM) and self.installed(QEMU))
        self.assertIn("kvm.ignore_msrs=1", self.kargs())
        self.assertIn("kvm.report_ignored_msrs=0", self.kargs())
        self.assertEqual(self.unit("libvirtd.service"), "enabled")
        self.assertEqual(self.unit("bazzite-libvirtd-setup.service"), "enabled")
        calls = self.changes(self.calls())
        self.assertIn("flatpak install --system --noninteractive flathub " + VM + " " + QEMU, calls)
        self.assertIn("systemctl enable --now libvirtd.service", calls)
        swtpm = os.path.join(self.root, "var/lib/swtpm-localca")
        self.assertTrue(os.path.isdir(swtpm))
        self.assertIn("chown tss " + swtpm, calls)
        self.assertIn("restorecon -rv " + os.path.join(self.root, "var/lib/libvirt"), calls)
        self.assertIn("restorecon -rv " + os.path.join(self.root, "var/log/libvirt"), calls)
        self.assertIn("restart", r.stdout.lower())

    def test_only_administrators_get_vm_rights(self):
        # Managing system VMs is close to root access, so only wheel members get it
        # (Bazzite's recipe adds whoever runs it, which must be an administrator too).
        self.settings(True)
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual(sorted(self.members("libvirt")), ["alice", "bob"])
        self.assertNotIn("carol", self.members("libvirt"))

    def test_running_again_changes_nothing(self):
        self.settings(True)
        self.run_tool()
        self.calls()
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.changes(self.calls()), [])

    def test_off_undoes_what_forge_did(self):
        self.settings(True)
        self.run_tool()
        self.settings(False)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.installed(VM) or self.installed(QEMU))
        self.assertNotIn("kvm.ignore_msrs=1", self.kargs())
        self.assertNotIn("kvm.report_ignored_msrs=0", self.kargs())
        self.assertEqual(self.unit("libvirtd.service"), "disabled")
        self.assertEqual(self.unit("bazzite-libvirtd-setup.service"), "disabled")
        self.assertEqual(self.members("libvirt"), [])
        self.calls()
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual(self.changes(self.calls()), [])

    def test_off_never_removes_a_setup_forge_did_not_make(self):
        # Set up by hand with ujust before Forge was involved: Forge leaves it alone.
        for app in (VM, QEMU):
            os.makedirs(os.path.join(self.fake, "flatpaks"), exist_ok=True)
            open(os.path.join(self.fake, "flatpaks", app), "w").close()
        open(os.path.join(self.fake, "unit-libvirtd.service"), "w").write("enabled")
        open(os.path.join(self.fake, "kargs"), "w").write("quiet kvm.ignore_msrs=1 kvm.report_ignored_msrs=0")
        self.write_groups({"wheel": ["alice"], "libvirt": ["alice"]})
        self.settings(False)
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual(self.changes(self.calls()), [])

    def test_off_after_on_keeps_what_was_there_before(self):
        os.makedirs(os.path.join(self.fake, "flatpaks"))
        open(os.path.join(self.fake, "flatpaks", VM), "w").close()      # the user's own virt-manager
        open(os.path.join(self.fake, "unit-libvirtd.service"), "w").write("enabled")
        open(os.path.join(self.fake, "kargs"), "w").write("quiet kvm.ignore_msrs=1")
        self.write_groups({"wheel": ["alice", "bob"], "libvirt": ["alice"]})
        self.settings(True)
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertTrue(self.installed(QEMU))
        self.settings(False)
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertTrue(self.installed(VM))                   # was there before: kept
        self.assertFalse(self.installed(QEMU))                # Forge added it: removed
        self.assertEqual(self.unit("libvirtd.service"), "enabled")
        self.assertIn("kvm.ignore_msrs=1", self.kargs())      # was there before
        self.assertNotIn("kvm.report_ignored_msrs=0", self.kargs())
        self.assertEqual(self.members("libvirt"), ["alice"])  # bob was added by Forge

    def test_a_failed_download_is_retried_and_can_still_be_undone(self):
        open(os.path.join(self.fake, "offline"), "w").close()
        self.settings(True)
        r = self.run_tool()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no network", r.stderr)
        self.assertFalse(self.installed(VM))
        # What it did before the failure is recorded, so turning it off undoes it.
        self.settings(False)
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual(self.members("libvirt"), [])
        self.assertNotIn("kvm.ignore_msrs=1", self.kargs())
        # Back online and on again: it finishes.
        os.unlink(os.path.join(self.fake, "offline"))
        self.settings(True)
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertTrue(self.installed(VM) and self.installed(QEMU))

    def test_settings_with_problems_change_nothing(self):
        with open(self.conf, "w") as f:
            json.dump({"virt.enabled": "yes please"}, f)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.changes(self.calls()), [])

    def test_odd_names_in_the_record_are_never_used(self):
        self.settings(True)
        self.run_tool()
        rec = os.path.join(self.state, "state.json")
        data = json.load(open(rec))
        data["added_users"] = ["-rf", "bob;reboot", "../x"]
        data["installed_flatpaks"] = ["--all", "org.evil app"]
        json.dump(data, open(rec, "w"))
        self.settings(False)
        self.run_tool()
        calls = self.changes(self.calls())
        self.assertFalse([c for c in calls if "-rf" in c or "reboot" in c or "--all" in c or "evil" in c], calls)

    def test_a_record_that_is_a_link_is_not_followed(self):
        self.settings(True)
        self.run_tool()
        rec = os.path.join(self.state, "state.json")
        target = os.path.join(self.t, "elsewhere.json")
        shutil.move(rec, target)
        os.symlink(target, rec)
        self.settings(False)
        r = self.run_tool()
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.installed(VM))       # nothing undone from an untrusted record

    def test_the_record_is_private(self):
        self.settings(True)
        self.run_tool()
        st = os.stat(os.path.join(self.state, "state.json"))
        self.assertEqual(st.st_mode & 0o077, 0)

    def test_an_admin_who_is_demoted_loses_vm_rights(self):
        self.settings(True)
        self.run_tool()
        self.assertEqual(sorted(self.members("libvirt")), ["alice", "bob"])
        self.write_groups({"wheel": ["alice"], "libvirt": ["alice", "bob"], "users": ["bob"]})
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.members("libvirt"), ["alice"])

    def test_someone_forge_did_not_add_is_never_removed(self):
        self.write_groups({"wheel": ["alice"], "libvirt": ["dave"]})   # added by hand
        self.settings(True)
        self.run_tool()
        self.assertIn("dave", self.members("libvirt"))
        self.settings(False)
        self.run_tool()
        self.assertEqual(self.members("libvirt"), ["dave"])

    def replace_fake(self, name, old, new):
        self.assertIn(old, FAKE)
        with open(os.path.join(self.bin, name), "w") as f:
            f.write(FAKE.replace(old, new))

    def test_libvirtd_enabled_but_not_started_is_still_undone(self):
        # "enable --now" can enable the unit and then fail to start it.
        old = '        open(path("unit-" + args[-1]), "w").write(args[0] + "d")\n'
        self.replace_fake("systemctl", old, old + '        if args[:2] == ["enable", "--now"] and os.path.exists(path("start-fails")):\n'
                                               '            sys.exit(1)\n')
        open(os.path.join(self.fake, "start-fails"), "w").close()
        self.settings(True)
        self.assertNotEqual(self.run_tool().returncode, 0)
        self.assertEqual(self.unit("libvirtd.service"), "enabled")
        self.settings(False)
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual(self.unit("libvirtd.service"), "disabled")

    def test_a_file_where_the_swtpm_folder_goes_fails_cleanly(self):
        os.makedirs(os.path.join(self.root, "var/lib"))
        open(os.path.join(self.root, "var/lib/swtpm-localca"), "w").close()
        self.settings(True)
        r = self.run_tool()
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue(self.installed(VM))       # the other steps still ran

    def test_turned_off_while_setting_up_is_undone_in_the_same_run(self):
        # systemd joins a second start into the running one, so the run must notice the change itself.
        old = '    if args[0] == "install":\n'
        self.replace_fake("flatpak", old, old + '        import json\n'
                          '        json.dump({"virt.enabled": False}, open(os.environ["FORGE_CONF"], "w"))\n')
        self.settings(True)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.installed(VM))
        self.assertEqual(self.members("libvirt"), [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "state.json")))

    def test_no_wheel_group_found_removes_nobody(self):
        self.settings(True)
        self.run_tool()
        self.write_groups({"libvirt": ["alice", "bob"]})     # wheel can't be found
        self.run_tool()
        self.assertEqual(sorted(self.members("libvirt")), ["alice", "bob"])

    def test_service_unit(self):
        text = open(UNIT).read()
        self.assertIn("Type=oneshot", text)
        self.assertIn("ExecStart=/usr/libexec/forge-virt", text)
        self.assertIn("After=network-online.target", text)
        self.assertIn("Wants=network-online.target", text)
        self.assertIn("StateDirectory=forge-virt", text)
        self.assertIn("StateDirectoryMode=0700", text)
        self.assertIn("TimeoutStartSec=", text)


class ApplyVirtTest(unittest.TestCase):
    APPLY = os.path.join(ROOT, "files/system/usr/libexec/forge-apply")

    def run_apply(self, values):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        conf = os.path.join(root, "etc/forge/settings.json")
        os.makedirs(os.path.dirname(conf))
        with open(conf, "w") as f:
            json.dump(values, f)
        env = dict(os.environ, FORGE_ROOT=root, FORGE_DRYRUN="1", FORGE_CONF=conf, FORGE_SCHEMA=SCHEMA,
                   PYTHONPATH=LIB)
        r = subprocess.run([sys.executable, self.APPLY], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return root, r.stdout

    def test_vm_limits_are_written(self):
        root, _ = self.run_apply({"virt.memory_max_pct": 60, "virt.cpu_weight": 80})
        text = open(os.path.join(root, "etc/systemd/system/machine.slice.d/90-forge.conf")).read()
        self.assertTrue(text.endswith("[Slice]\nCPUWeight=80\nMemoryMax=60%\n"), text)

    def test_virt_service_is_started_without_waiting(self):
        # The download can take minutes, so saving settings must not wait for it.
        _, out = self.run_apply({"virt.enabled": True})
        self.assertIn("would run: systemctl --no-block start forge-virt.service", out)


class VirtSettingsTest(unittest.TestCase):
    def test_settings_exist_and_need_a_restart(self):
        schema = json.load(open(SCHEMA))
        keys = {s["key"]: s for s in schema["settings"]}
        self.assertIn("virt", [g["id"] for g in schema["groups"]])
        self.assertEqual(keys["virt.enabled"]["default"], False)
        self.assertEqual(keys["virt.enabled"]["restart"], "boot")
        for k in ("virt.memory_max_pct", "virt.cpu_weight"):
            self.assertEqual(keys[k]["type"], "number")
            self.assertEqual(keys[k]["group"], "virt")

    def test_tool_is_executable_in_the_image(self):
        with open(os.path.join(ROOT, "files/scripts/finalize.sh")) as f:
            self.assertIn("/usr/libexec/forge-virt", f.read())


if __name__ == "__main__":
    unittest.main()
