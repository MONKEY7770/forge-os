"""Release builder: the screenshot test of the built image (.github/scripts/forge_screenshot.py on the
runner, .github/screenshot/ for the test-only image and its probe inside the VM).

It can't boot a real image here: QEMU, bootc and the guest are replaced by fakes, and the logic
(which tag, the install and QEMU command lines, the probe protocol, the screenshot plan, timeouts)
is tested on its own. The real run is Untested (needs CI)."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.path.join(ROOT, ".github/scripts/forge_screenshot.py")
TEST_IMAGE = os.path.join(ROOT, ".github/screenshot")
PROBE = os.path.join(TEST_IMAGE, "files/usr/libexec/forge-screenshot-probe")


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class HostTests(unittest.TestCase):
    def setUp(self):
        self.h = load(HOST, "forge_screenshot_under_test")

    def test_pick_tag(self):
        self.assertEqual(self.h.pick_tag("workflow_dispatch", "stable", ""), "latest")
        self.assertEqual(self.h.pick_tag("workflow_dispatch", "testing", "whatever"), "testing")
        self.assertEqual(self.h.pick_tag("workflow_run", "", "main"), "latest")
        self.assertEqual(self.h.pick_tag("workflow_run", "", "testing"), "testing")
        for bad in (("workflow_run", "", "feature"), ("workflow_run", "", "main\nx"), ("workflow_dispatch", "nightly", ""),
                    ("push", "stable", "main")):
            with self.assertRaises(ValueError, msg=bad):
                self.h.pick_tag(*bad)

    def test_install_command_uses_bootc_loopback_from_the_test_image(self):
        argv = self.h.install_argv("localhost/forge-screenshot-test:latest", "/work")
        self.assertEqual(argv[:3], ["podman", "run", "--rm"])
        for flag in ("--privileged", "--pid=host", "-v", "/dev:/dev", "/var/lib/containers:/var/lib/containers",
                     "/work:/output", "--security-opt", "label=type:unconfined_t"):
            self.assertIn(flag, argv)
        i = argv.index("localhost/forge-screenshot-test:latest")
        self.assertEqual(argv[i + 1:i + 4], ["bootc", "install", "to-disk"])
        for flag in ("--via-loopback", "--generic-image", "--wipe", "--skip-fetch-check", "--filesystem", "btrfs"):
            self.assertIn(flag, argv[i:])
        self.assertEqual(argv[-1], "/output/disk.raw")
        with self.assertRaises(ValueError):
            self.h.install_argv("localhost/x:latest;rm", "/work")

    def test_qemu_has_no_network_a_probe_port_and_no_commas_in_paths(self):
        argv = self.h.qemu_argv("qemu-system-x86_64", "/w/disk.raw", "/o/CODE.fd", "/p/VARS.fd", "/o/serial.log",
                                "/p/qmp.sock", "/p/probe.sock", kvm=True, memory_mb=8192, cpus=4)
        text = " ".join(argv)
        self.assertIn("-nic none", text)
        self.assertIn("virtserialport,chardev=probe,name=org.forgeos.probe", text)
        self.assertIn("socket,id=probe,path=/p/probe.sock,server=on,wait=off", text)
        self.assertIn("file=/w/disk.raw,format=raw,if=virtio", text)
        self.assertIn("accel=kvm", text)
        self.assertIn("-sandbox", argv)
        with self.assertRaises(ValueError):
            self.h.qemu_argv("q", "/w/a,b.raw", "c", "n", "s", "q", "p", kvm=False, memory_mb=4096, cpus=2)

    def test_probe_lines_are_parsed_strictly(self):
        events, rest = self.h.parse_probe(b"junk\nFORGE-PROBE desktop-ready\nFORGE-PROBE settings-failed no \x1b[31mbrowser\n"
                                          b"FORGE-PROBE ::error::x\nFORGE-PROBE settings-sta")
        self.assertEqual(events[0], ("desktop-ready", ""))
        self.assertEqual(events[1][0], "settings-failed")
        self.assertNotIn("\x1b", events[1][1])
        self.assertEqual(len(events), 2, "unknown event names are dropped")
        self.assertEqual(rest, b"FORGE-PROBE settings-sta")
        _, rest = self.h.parse_probe(b"x" * 10000)
        self.assertLessEqual(len(rest), 4096, "a line without an end can't grow forever")

    def session(self, **kw):
        args = dict(start=0.0, desktop_timeout=600, desktop_settle=20, settings_timeout=300, settings_settle=30,
                    max_frames=5)
        args.update(kw)
        return self.h.Session(**args)

    def test_happy_path_screenshots_in_order(self):
        s = self.session()
        acts = []
        for t in range(0, 5):
            acts += s.tick(float(t), [])
        self.assertEqual([a for a in acts if a[0] == "frame"], [("frame", i) for i in range(5)])
        self.assertEqual(s.tick(5.0, []), [], "frame cap: no more boot frames")
        self.assertEqual(s.tick(10.0, [("desktop-ready", "")]), [])
        self.assertEqual(s.tick(29.0, []), [])
        self.assertEqual(s.tick(30.0, []), [("shot", "desktop"), ("send", "open-settings")])
        self.assertEqual(s.tick(40.0, [("settings-started", "")]), [])
        self.assertEqual(s.tick(70.0, []), [("shot", "settings"), ("done", True,
                                                                    "desktop reached after 10 s; Forge Settings opened")])
        self.assertTrue(s.finished)

    def test_desktop_timeout_fails(self):
        s = self.session(max_frames=0)
        self.assertEqual(s.tick(599.0, []), [])
        acts = s.tick(601.0, [])
        self.assertEqual(acts[0], ("shot", "last"))
        self.assertEqual(acts[1][:2], ("done", False))
        self.assertIn("not reached within 10 minutes", acts[1][2])

    def test_settings_failure_and_timeout_fail_but_keep_the_screen(self):
        s = self.session(max_frames=0)
        s.tick(1.0, [("desktop-ready", "")])
        s.tick(21.0, [])
        acts = s.tick(22.0, [("settings-failed", "no browser")])
        self.assertEqual(acts[0], ("shot", "settings"))
        self.assertEqual(acts[1][:2], ("done", False))
        self.assertIn("no browser", acts[1][2])
        s = self.session(max_frames=0)
        s.tick(1.0, [("desktop-ready", "")])
        s.tick(21.0, [])
        self.assertEqual(s.tick(300.0, []), [])
        acts = s.tick(322.0, [])
        self.assertEqual(acts[0], ("shot", "settings"))
        self.assertFalse(acts[1][1])

    def test_done_is_final(self):
        s = self.session(max_frames=0)
        s.tick(601.0, [])
        self.assertEqual(s.tick(700.0, [("desktop-ready", "")]), [])


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.p = load(PROBE, "forge_probe_under_test")
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def proc(self, pid, comm, uid):
        d = os.path.join(self.root, "proc", str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(comm + "\n")
        with open(os.path.join(d, "status"), "w") as f:
            f.write(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")

    def test_desktop_is_plasmashell_of_the_test_user(self):
        proc = os.path.join(self.root, "proc")
        os.makedirs(proc)
        self.assertFalse(self.p.running(proc, 1500, "plasmashell"))
        self.proc(10, "plasmashell", 0)
        self.assertFalse(self.p.running(proc, 1500, "plasmashell"), "another user's process does not count")
        self.proc(11, "plasmashell", 1500)
        self.assertTrue(self.p.running(proc, 1500, "plasmashell"))
        os.makedirs(os.path.join(proc, "self"))
        self.assertTrue(self.p.running(proc, 1500, "plasmashell"))

    def test_session_environment(self):
        run = os.path.join(self.root, "run/user/1500")
        os.makedirs(run)
        self.assertIsNone(self.p.session_env(os.path.join(self.root, "run/user"), 1500))
        open(os.path.join(run, "wayland-0.lock"), "w").close()
        socket_path = os.path.join(run, "wayland-0")
        s = socket.socket(socket.AF_UNIX)
        s.bind(socket_path)
        self.addCleanup(s.close)
        env = self.p.session_env(os.path.join(self.root, "run/user"), 1500)
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")
        self.assertEqual(env["XDG_RUNTIME_DIR"], run)
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={run}/bus")

    def test_settings_command(self):
        argv = self.p.settings_argv("forgetest", {"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1500"})
        self.assertEqual(argv[:4], ["/usr/sbin/runuser", "-u", "forgetest", "--"])
        self.assertEqual(argv[-1], "/usr/bin/forge-settings")
        self.assertIn("WAYLAND_DISPLAY=wayland-0", argv)

    def test_protocol_with_a_fake_host(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        launched = []
        state = {"desktop": False}

        def fake_running(proc_root, uid, name):
            if name == "plasmashell":
                return state["desktop"]
            return bool(launched)
        t = threading.Thread(target=self.p.serve, args=(b.fileno(),),
                             kwargs=dict(running_fn=fake_running, env_fn=lambda *x: {"WAYLAND_DISPLAY": "wayland-0"},
                                         launch_fn=launched.append, poll=0.01, settle=0.01, wait_cmd=5))
        t.start()
        f = a.makefile("rwb", buffering=0)
        self.assertEqual(f.readline(), b"FORGE-PROBE started\n")
        state["desktop"] = True
        self.assertEqual(f.readline(), b"FORGE-PROBE desktop-ready\n")
        f.write(b"open-settings\n")
        self.assertEqual(f.readline(), b"FORGE-PROBE settings-started\n")
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0][-1], "/usr/bin/forge-settings")


class TestImageFilesTests(unittest.TestCase):
    def read(self, rel):
        with open(os.path.join(TEST_IMAGE, rel)) as f:
            return f.read()

    def test_test_account_logs_in_by_itself_and_has_no_password(self):
        users = self.read("files/usr/lib/sysusers.d/forge-screenshot-test.conf")
        self.assertRegex(users, r'(?m)^u forgetest 1500 ')
        for conf in ("files/etc/plasmalogin.conf.d/zz-forge-screenshot-test.conf",
                     "files/etc/sddm.conf.d/zz-forge-screenshot-test.conf"):
            text = self.read(conf)
            self.assertIn("[Autologin]\nUser=forgetest\nSession=plasma.desktop\n", text)
        self.assertNotIn("passwd", self.read("Containerfile"))

    def test_containerfile_only_adds_test_pieces(self):
        text = self.read("Containerfile")
        self.assertIn("ARG BASE\nFROM ${BASE}\n", text)
        self.assertIn("systemctl enable forge-screenshot-probe.service", text)
        self.assertIn("rm -f /etc/xdg/autostart/forge-welcome.desktop", text)
        unit = self.read("files/usr/lib/systemd/system/forge-screenshot-probe.service")
        self.assertIn("ExecStart=/usr/libexec/forge-screenshot-probe", unit)


if __name__ == "__main__":
    unittest.main()
