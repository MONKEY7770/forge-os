#!/usr/bin/env python3
"""Screenshot test of a built Forge OS image (the screenshot-test workflow).

  pick-tag   which image tag to test: by hand the chosen channel, after a build the branch it
             came from (main: latest, testing: testing). Reads EVENT, CHANNEL_INPUT, RUN_BRANCH.
             Prints "tag=<tag>" for $GITHUB_OUTPUT.
  install    installs the test image to a raw virtual disk with the image's own bootc
             ("bootc install to-disk --via-loopback", run by root podman). No other installer image.
  boot       boots the disk in QEMU with UEFI firmware (OVMF, Secure Boot off, no network) and
             saves screenshots into --out:
               boot/frame-NNN.png  every few seconds until the desktop is up (the boot splash is
                                   in there; which frame shows it is not checked)
               desktop.png         the Plasma desktop of the test account, which logs in by itself
               settings.png        Forge Settings, opened by the probe inside the VM
               last.png            the last screen when something timed out
               result.txt, result.json, probe.log
             Passes when the desktop is reached within --desktop-minutes and Forge Settings starts.
  firstboot-tag  which image tag the first-boot test uses: like pick-tag, except that a run by hand
             with IMAGE_TAG_INPUT set tests that tag (checked by forge_release.py's iso-tag rule,
             the same check build-iso uses). Prints "tag=<tag>".
  firstboot  boots a disk of the first-boot test image (.github/screenshot/firstboot: no account at
             all, only a probe) and checks that Plasma Setup, the first account wizard, appears:
             passes when plasma-setup.service ran (not skipped), a graphical session of the user
             plasma-setup is active (loginctl), there is no account with UID 1000 to 60000 yet, and
             the plasma-setup user's UID is below 1000. Saves into --out boot/frame-NNN.png,
             wizard.png and wizard-2.png (or failure.png / last.png), guest-logs.txt (journal of
             plasma-setup and plasmalogin, failed units, sessions), result.txt, result.json and
             probe.log. Also reports, without failing on it, whether the wheel group is in
             /etc/group at that point.

The test image (.github/screenshot) adds the test account "forgetest" (no password, automatic
login), Firefox (the real image installs Firefox as a Flatpak on first boot, online) and a probe
service that talks to this script over a virtio serial port: it says "desktop-ready" when the test
account's plasmashell runs, and opens Forge Settings when asked. Guest text is untrusted: only
known event names are used, and it is printed without control characters.

The first-boot probe sends "state" lines (fixed words and numbers only, checked strictly here)
and, when asked, the guest logs as "log" lines.

Reuses the QMP and screenshot code of iso_boot_check.py. Exit codes: 0 pass, 1 fail, 2 bad use.
"""
import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from iso_boot_check import OVMF_DIR, Qmp, firmware_files, print_untrusted, printable, read_ppm, to_png  # noqa: E402
import forge_release  # noqa: E402

EVENTS = ("started", "desktop-ready", "settings-started", "settings-failed")
FIRSTBOOT_EVENTS = ("started", "state", "log", "logs-end")
LINE_MAX = 4096
_IMAGE = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+(?::[A-Za-z0-9_.-]+|@sha256:[0-9a-f]{64})")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def pick_tag(event, channel_input, branch):
    if event == "workflow_dispatch":
        tags = {"stable": "latest", "testing": "testing"}
        if channel_input not in tags:
            raise ValueError("choose stable or testing")
        return tags[channel_input]
    if event == "workflow_run":
        tags = {"main": "latest", "testing": "testing"}
        if branch not in tags:
            raise ValueError("only builds from main or testing are tested")
        return tags[branch]
    raise ValueError(f"unexpected event {event!r}")


def firstboot_tag(event, channel_input, branch, tag_input):
    """The first-boot test's tag. By hand with an image tag: that tag, checked by the same rule as
    build-iso (forge_release.iso_tag: latest, testing, br-<branch>-candidate-NN, <sha7>-candidate-NN).
    Otherwise the same choice as pick_tag."""
    if tag_input:
        if event != "workflow_dispatch":
            raise ValueError("an image tag can only be chosen in a run by hand")
        return forge_release.iso_tag(tag_input)
    return pick_tag(event, channel_input, branch)


def install_argv(image, work):
    """Root podman runs the test image's own bootc to install it to work/disk.raw."""
    if not _IMAGE.fullmatch(image) or "," in work or ":" in work:
        raise ValueError("unexpected image name or folder")
    return ["podman", "run", "--rm", "--privileged", "--pid=host", "--ipc=host",
            "--security-opt", "label=type:unconfined_t", "-v", "/dev:/dev",
            "-v", "/var/lib/containers:/var/lib/containers", "-v", f"{work}:/output",
            image, "bootc", "install", "to-disk", "--via-loopback", "--generic-image", "--wipe",
            "--skip-fetch-check", "--filesystem", "btrfs", "/output/disk.raw"]


def qemu_argv(qemu, disk, code, nvram, serial, qmp, probe, kvm, memory_mb, cpus):
    for p in (disk, code, nvram, serial, qmp, probe):
        if "," in p:
            raise ValueError(f"commas are not allowed in paths: {p!r}")
    return [qemu, "-name", "forge-screenshot-test",
            "-machine", f"q35,accel={'kvm' if kvm else 'tcg'}", "-cpu", "host" if kvm else "max",
            "-smp", str(cpus), "-m", str(memory_mb),
            "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
            "-drive", f"if=pflash,format=raw,unit=1,file={nvram}",
            "-drive", f"file={disk},format=raw,if=virtio",
            "-vga", "virtio", "-display", "none", "-nic", "user,model=virtio-net-pci",
            "-device", "virtio-serial-pci",
            "-chardev", f"socket,id=probe,path={probe},server=on,wait=off",
            "-device", "virtserialport,chardev=probe,name=org.forgeos.probe",
            "-serial", f"file:{serial}", "-qmp", f"unix:{qmp},server=on,wait=off",
            "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny"]


def parse_probe(buf, names=EVENTS):
    """Complete lines from the guest -> ([(event, detail)], rest). Unknown lines are dropped."""
    *lines, rest = buf.split(b"\n")
    events = []
    for raw in lines:
        text = _CONTROL.sub(" ", raw.decode("utf-8", "replace"))[:300].strip()
        parts = text.split(" ", 2)
        if len(parts) >= 2 and parts[0] == "FORGE-PROBE" and parts[1] in names:
            events.append((parts[1], parts[2].strip() if len(parts) > 2 else ""))
    return events, rest[-LINE_MAX:]


class Session:
    """What to do when, from the time and the probe's events. No I/O, so it can be tested alone.
    tick() returns actions: ("frame", n), ("shot", name), ("send", text), ("done", ok, text)."""

    def __init__(self, start, desktop_timeout, desktop_settle, settings_timeout, settings_settle, max_frames):
        self.start, self.desktop_timeout, self.desktop_settle = start, desktop_timeout, desktop_settle
        self.settings_timeout, self.settings_settle, self.max_frames = settings_timeout, settings_settle, max_frames
        self.state, self.since, self.frames, self.finished, self.desktop_at = "booting", start, 0, False, None

    def _done(self, ok, text, shot):
        self.finished, self.state = True, "done"
        return [("shot", shot), ("done", ok, text)]

    def tick(self, now, events):
        if self.finished:
            return []
        acts = []
        for name, detail in events:
            if name == "desktop-ready" and self.state == "booting":
                self.state, self.since, self.desktop_at = "desktop-settle", now, now
            elif name == "settings-started" and self.state == "settings-wait":
                self.state, self.since = "settings-settle", now
            elif name == "settings-failed" and self.state == "settings-wait":
                return self._done(False, f"Forge Settings did not start: {detail or 'no reason given'}", "settings")
        if self.state == "booting":
            if now - self.start > self.desktop_timeout:
                return self._done(False, f"the desktop was not reached within {int(self.desktop_timeout // 60)} minutes",
                                  "last")
            if self.frames < self.max_frames:
                acts.append(("frame", self.frames))
                self.frames += 1
        elif self.state == "desktop-settle" and now - self.since >= self.desktop_settle:
            self.state, self.since = "settings-wait", now
            acts += [("shot", "desktop"), ("send", "open-settings")]
        elif self.state == "settings-wait" and now - self.since > self.settings_timeout:
            return self._done(False, f"Forge Settings did not start within {int(self.settings_timeout)} s", "settings")
        elif self.state == "settings-settle" and now - self.since >= self.settings_settle:
            self.finished, self.state = True, "done"
            acts += [("shot", "settings"), ("done", True, f"desktop reached after {int(self.desktop_at - self.start)} s; "
                                                         "Forge Settings opened")]
        return acts


# First-boot test. The probe's "state" line: every field, always, in any order, for example
# "unit=ran session=yes regular=0 setup-uid=974 wheel=yes wizard=yes autologin=yes".
UNIT_WORDS = ("waiting", "disabled", "running", "ran", "skipped-condition", "skipped-exec-condition", "failed",
              "not-loaded")
_YES_NO = re.compile(r"yes|no")
STATE_FIELDS = {"unit": re.compile("|".join(UNIT_WORDS)), "session": _YES_NO, "regular": re.compile(r"[0-9]{1,6}"),
                "setup-uid": re.compile(r"[0-9]{1,10}|none"), "wheel": _YES_NO, "wizard": _YES_NO,
                "autologin": _YES_NO}
SETUP_USER = "plasma-setup"


def parse_state(detail):
    """The probe's state line -> dict, or None unless it has exactly the known fields with valid values."""
    state = {}
    for item in detail.split():
        key, sep, value = item.partition("=")
        if not sep or key not in STATE_FIELDS or key in state or not STATE_FIELDS[key].fullmatch(value):
            return None
        state[key] = value
    if set(state) != set(STATE_FIELDS):
        return None
    state["regular"] = int(state["regular"])
    state["setup-uid"] = None if state["setup-uid"] == "none" else int(state["setup-uid"])
    return state


def firstboot_verdict(state):
    """(True, text) when the wizard is up as it should be, (False, text) when it can't come any more,
    None while it may still come."""
    unit, uid = state["unit"], state["setup-uid"]
    if unit == "not-loaded":
        return False, "plasma-setup.service is not loaded (not installed in the image, or masked)"
    if unit == "skipped-exec-condition":
        return False, ("plasma-setup.service was skipped: its ExecCondition (forge-first-user check) said an account "
                       f"with UID 1000 to 60000 exists (the probe counts {state['regular']})")
    if unit == "skipped-condition":
        return False, "plasma-setup.service was skipped: a Condition of the unit failed (see guest-logs.txt)"
    if unit == "failed":
        return False, "plasma-setup.service failed (see guest-logs.txt)"
    if uid is not None and uid >= 1000:
        return False, (f"the {SETUP_USER} user has UID {uid}, 1000 or more: forge-first-user check counts it as a "
                       "regular account, so the wizard would never run")
    if state["regular"]:
        return False, f"an account with UID 1000 to 60000 exists before the wizard ({state['regular']} found)"
    if unit == "ran" and state["session"] == "yes":
        return True, f"plasma-setup.service ran and a graphical session of {SETUP_USER} is active, with no account yet"
    return None


def describe_state(state):
    if state is None:
        return "the probe never reported"
    return (f"last report: plasma-setup.service {state['unit']}, {SETUP_USER} session {state['session']}, "
            f"accounts with UID 1000 to 60000: {state['regular']}")


def info_lines(state):
    """What is reported but does not decide the result."""
    if state is None:
        return ["wheel group in /etc/group: unknown (no report from the probe)", f"{SETUP_USER} UID: unknown"]
    uid = state["setup-uid"]
    if uid is None:
        uid_text = "none (the user does not exist)"
    else:
        uid_text = f"{uid} ({'below 1000, ok' if uid < 1000 else '1000 or more, FAIL'})"
    return [f"wheel group in /etc/group: {state['wheel']}", f"{SETUP_USER} UID: {uid_text}",
            f"wizard process (plasma-setup) running: {state['wizard']}",
            f"autologin file /etc/plasmalogin.conf.d/99-plasma-setup.conf: {state['autologin']}"]


class FirstbootSession:
    """Like Session, for the first-boot test. tick() returns the same kinds of actions. After the
    verdict the screen is saved, then the probe is asked for the guest logs ("send-logs") and the run
    ends when they are in ("logs-end") or after logs_timeout."""

    def __init__(self, start, wizard_timeout, shots=((30, "wizard"), (90, "wizard-2"), (600, "wizard-3")), logs_timeout=180,
                 max_frames=150):
        self.start, self.wizard_timeout, self.shots = start, wizard_timeout, shots
        self.logs_timeout, self.max_frames = logs_timeout, max_frames
        self.phase, self.since, self.frames, self.finished = "booting", start, 0, False
        self.state, self.final_state, self.verdict, self.probe_seen, self.next_shot = None, None, None, False, 0

    def _logs(self, now):
        if not self.probe_seen:
            self.finished, self.phase = True, "done"
            return [("done", *self.verdict)]
        self.phase, self.since = "logs", now
        return [("send", "send-logs")]

    def _fail(self, now, text, shot):
        self.verdict, self.final_state = (False, text), self.state
        return [("shot", shot)] + self._logs(now)

    def tick(self, now, events):
        if self.finished:
            return []
        for name, detail in events:
            if name == "started":
                self.probe_seen = True
            elif name == "state":
                parsed = parse_state(detail)
                if parsed is not None:
                    self.state, self.probe_seen = parsed, True
            elif name == "logs-end" and self.phase == "logs":
                self.finished, self.phase = True, "done"
                return [("done", *self.verdict)]
        acts = []
        if self.phase == "booting":
            verdict = firstboot_verdict(self.state) if self.state else None
            if verdict is not None and not verdict[0]:
                return self._fail(now, verdict[1], "failure")
            if verdict is not None:
                self.phase, self.since = "settle", now
                self.verdict = (True, f"after {int(now - self.start)} s: {verdict[1]}")
                self.final_state = self.state
            elif now - self.start > self.wizard_timeout:
                return self._fail(now, f"no wizard session after {int(self.wizard_timeout // 60)} minutes "
                                       f"({describe_state(self.state)})", "last")
            elif self.frames < self.max_frames:
                acts.append(("frame", self.frames))
                self.frames += 1
        if self.phase == "settle":
            while self.next_shot < len(self.shots) and now - self.since >= self.shots[self.next_shot][0]:
                acts.append(("shot", self.shots[self.next_shot][1]))
                self.next_shot += 1
            if self.next_shot == len(self.shots):
                acts += self._logs(now)
        elif self.phase == "logs" and now - self.since > self.logs_timeout:
            self.finished, self.phase = True, "done"
            acts.append(("done", self.verdict[0], self.verdict[1] + "; the guest logs did not arrive"))
        return acts


def run_root(argv, timeout):
    if os.geteuid() != 0:
        argv = ["sudo", "--non-interactive", *argv]
    return subprocess.run(argv, timeout=timeout).returncode


def cmd_install(a):
    work = os.path.abspath(a.work)
    os.makedirs(work, exist_ok=True)
    disk = os.path.join(work, "disk.raw")
    with open(disk, "wb") as f:
        f.truncate(a.disk_gb * 1024 ** 3)     # sparse: only what is written uses space
    rc = run_root(install_argv(a.image, work), timeout=3600)
    if rc != 0:
        print(f"ERROR: bootc install to-disk failed (exit code {rc})")
        return 1
    run_root(["chown", f"{os.getuid()}:{os.getgid()}", disk], timeout=60)
    print(f"Installed {a.image} to {disk}")
    return 0


def shoot(qmp, dump, target):
    qmp.cmd("screendump", filename=dump)
    with open(dump, "rb") as f:
        w, h, rgb = read_ppm(f.read())
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        f.write(to_png(w, h, rgb))


def cmd_boot(a):
    if not os.path.isfile(a.disk):
        print(f"ERROR: disk not found: {a.disk}")
        return 2
    code, vars_template = firmware_files(False, a.ovmf_dir)
    qemu = shutil.which(a.qemu)
    if not qemu or not os.path.isfile(code):
        print("ERROR: QEMU or OVMF missing (apt install qemu-system-x86 ovmf)")
        return 2
    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    start = time.monotonic()
    private = tempfile.mkdtemp(prefix="forge-shots-")
    proc = log = qmp = probe = session = None
    verdict, probe_log, guest_log = (False, "not started"), [], []
    firstboot = a.cmd == "firstboot"

    def stop(signum, frame):
        raise KeyboardInterrupt(f"stopped by signal {signum}")
    old = signal.signal(signal.SIGTERM, stop)
    try:
        nvram = os.path.join(private, "VARS.fd")
        shutil.copyfile(vars_template, nvram)
        sock, psock, dump = (os.path.join(private, n) for n in ("qmp.sock", "probe.sock", "screen.ppm"))
        argv = qemu_argv(qemu, os.path.abspath(a.disk), code, nvram, os.path.join(out, "serial.log"), sock, psock,
                         kvm, a.memory, a.cpus)
        log = open(os.path.join(out, "qemu.log"), "wb")
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        qmp = Qmp(sock, proc)
        minutes = a.wizard_minutes if firstboot else a.desktop_minutes
        limit = minutes * 60 if kvm else minutes * 180
        if firstboot:
            session = FirstbootSession(start=time.monotonic(), wizard_timeout=limit)
        else:
            session = Session(start=time.monotonic(), desktop_timeout=limit, desktop_settle=20, settings_timeout=300,
                              settings_settle=30, max_frames=150)
        buf = b""
        print(f"Booting {a.disk}: {'KVM' if kvm else 'no KVM (TCG, slow)'}, "
              f"{'wizard' if firstboot else 'desktop'} limit {int(limit // 60)} min", flush=True)
        while not session.finished:
            time.sleep(a.interval)
            if proc.poll() is not None:
                verdict = (False, f"QEMU stopped (exit code {proc.returncode})")
                break
            if probe is None:
                try:
                    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    probe.connect(psock)
                    probe.setblocking(False)
                except OSError:
                    probe.close()
                    probe = None
            events = []
            if probe is not None:
                try:
                    while True:
                        chunk = probe.recv(65536)
                        if not chunk:
                            probe.close()
                            probe = None
                            break
                        buf += chunk
                except BlockingIOError:
                    pass
                except OSError:
                    probe = None
                events, buf = parse_probe(buf, FIRSTBOOT_EVENTS if firstboot else EVENTS)
            for name, detail in events:
                if name == "log":     # guest logs go to guest-logs.txt only
                    if len(guest_log) < 5000:
                        guest_log.append(detail)
                    continue
                probe_log.append(f"{int(time.monotonic() - start)} s: {name} {detail}".strip())
                print(f"[{time.monotonic() - start:6.0f} s] probe: {printable(name + ' ' + detail, 300)}", flush=True)
            for act in session.tick(time.monotonic(), events):
                if act[0] == "frame":
                    shoot(qmp, dump, os.path.join(out, "boot", f"frame-{act[1]:03d}.png"))
                elif act[0] == "shot":
                    shoot(qmp, dump, os.path.join(out, f"{act[1]}.png"))
                elif act[0] == "send" and probe is not None:
                    probe.setblocking(True)
                    probe.sendall(act[1].encode() + b"\n")
                    probe.setblocking(False)
                elif act[0] == "done":
                    verdict = (act[1], act[2])
    except (OSError, ValueError) as e:
        verdict = (False, f"{e.__class__.__name__}: {e}")
    finally:
        signal.signal(signal.SIGTERM, old)
        if qmp:
            try:
                qmp.cmd("quit")
            except OSError:
                pass
            qmp.close()
        if proc is not None:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if log:
            log.close()
        if probe is not None:
            probe.close()
        shutil.rmtree(private, ignore_errors=True)
    ok, text = verdict
    result = {"ok": ok, "result": text, "seconds": int(time.monotonic() - start), "kvm": kvm}
    info = []
    if firstboot:
        state = session and (session.final_state or session.state)
        info = info_lines(state)
        result.update(state=state, info=info)
        with open(os.path.join(out, "guest-logs.txt"), "w") as f:
            f.write(printable("\n".join(guest_log), 2000000) + "\n" if guest_log else "(no logs from the guest)\n")
    with open(os.path.join(out, "probe.log"), "w") as f:
        f.write(printable("\n".join(probe_log), 20000) + "\n")
    with open(os.path.join(out, "result.json"), "w") as f:
        json.dump(result, f, indent=2)
    with open(os.path.join(out, "result.txt"), "w") as f:
        f.write(f"{'PASS' if ok else 'FAIL'}: {text}\n" + "".join(f"{line}\n" for line in info))
    print(f"{'PASS' if ok else 'FAIL'}: {text}")
    for line in info:
        print(f"  {line}")
    if not ok and guest_log:
        print_untrusted("Last guest log lines:", "\n".join(guest_log), 4000)
    if not ok:
        try:
            with open(os.path.join(out, "serial.log"), "rb") as f:
                print_untrusted("Last serial output:", f.read().decode("utf-8", "replace"))
        except OSError:
            pass
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pick-tag")
    i = sub.add_parser("install")
    i.add_argument("--image", required=True)
    i.add_argument("--work", required=True)
    i.add_argument("--disk-gb", type=int, default=60)
    b = sub.add_parser("boot")
    b.add_argument("--disk", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--desktop-minutes", type=int, default=20)
    b.add_argument("--interval", type=float, default=3.0)
    b.add_argument("--memory", type=int, default=8192)
    b.add_argument("--cpus", type=int, default=min(4, os.cpu_count() or 1))
    b.add_argument("--ovmf-dir", default=OVMF_DIR)
    b.add_argument("--qemu", default="qemu-system-x86_64")
    sub.add_parser("firstboot-tag")
    fb = sub.add_parser("firstboot")
    fb.add_argument("--disk", required=True)
    fb.add_argument("--out", required=True)
    fb.add_argument("--wizard-minutes", type=int, default=20)
    fb.add_argument("--interval", type=float, default=3.0)
    fb.add_argument("--memory", type=int, default=8192)
    fb.add_argument("--cpus", type=int, default=min(4, os.cpu_count() or 1))
    fb.add_argument("--ovmf-dir", default=OVMF_DIR)
    fb.add_argument("--qemu", default="qemu-system-x86_64")
    a = ap.parse_args(argv)
    if a.cmd == "firstboot-tag":
        try:
            tag = firstboot_tag(os.environ.get("EVENT", ""), os.environ.get("CHANNEL_INPUT", ""),
                                os.environ.get("RUN_BRANCH", ""), os.environ.get("IMAGE_TAG_INPUT", ""))
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(f"tag={tag}")
        return 0
    if a.cmd == "pick-tag":
        try:
            tag = pick_tag(os.environ.get("EVENT", ""), os.environ.get("CHANNEL_INPUT", ""),
                           os.environ.get("RUN_BRANCH", ""))
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(f"tag={tag}")
        return 0
    if a.cmd == "install":
        if not 20 <= a.disk_gb <= 200:
            print("ERROR: --disk-gb out of range")
            return 2
        try:
            return cmd_install(a)
        except ValueError as e:
            print(f"ERROR: {e}")
            return 2
    minutes = a.wizard_minutes if a.cmd == "firstboot" else a.desktop_minutes
    if not (1 <= minutes <= 120 and 0.5 <= a.interval <= 60 and 2048 <= a.memory <= 65536 and 1 <= a.cpus <= 64):
        print("ERROR: a value is out of range")
        return 2
    return cmd_boot(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Stopped: QEMU was stopped and the private folder removed.", flush=True)
        sys.exit(130)
