"""UEFI boot test for the installer ISOs (.github/scripts/iso_boot_check.py).

The screen check and the QEMU loop are tested here with synthetic screens and a fake QEMU
(it speaks just enough QMP). Real QEMU runs are done by hand (see the report), because the CI
test container has no QEMU or OVMF. The test copy with a serial console needs xorriso and
mtools; those tests are skipped without them.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, ".github/scripts/iso_boot_check.py")


def load():
    spec = importlib.util.spec_from_file_location("iso_boot_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Synthetic screens, as (width, height, rgb bytes).
def solid(w, h, rgb):
    return w, h, bytes(rgb) * (w * h)


def text_console(w=1280, h=800):
    """Black screen with grey 'text' lines (like GRUB, the OVMF shell or a kernel console)."""
    px = bytearray(w * h * 3)
    for y in range(100, 600, 20):
        for x in range(240, 1040):
            if (x // 3 + y) % 4 == 0:
                o = (y * w + x) * 3
                px[o:o + 3] = b"\xaa\xaa\xaa"
    return w, h, bytes(px)


def firmware_menu(w=640, h=480):
    """Light grey menu with a blue title and a black bar (the OVMF Boot Manager look)."""
    px = bytearray()
    for y in range(h):
        for x in range(w):
            if y > 380:
                px += b"\x00\x00\x00"
            elif y < 80 and x % 5 == 0:
                px += b"\x00\x00\xaa"
            else:
                px += b"\xaa\xaa\xaa"
    return w, h, bytes(px)


def gui(w=1024, h=768, base=(255, 255, 255)):
    """A graphical installer stand-in: a gradient sidebar, a light panel and a colourful picture."""
    px = bytearray()
    for y in range(h):
        for x in range(w):
            if x < 300:
                px += bytes((40 + y * 60 // h, 80 + y * 100 // h, 160 + y * 60 // h))
            elif 360 <= x < 960 and 60 <= y < 360:
                px += bytes(((x * 7) % 256, (y * 5) % 256, (x + y) % 256))
            else:
                px += bytes(base)
    return w, h, bytes(px)


def logo_on_black(w=1280, h=800):
    """A boot splash: a small colourful logo in the middle of a black screen."""
    px = bytearray(w * h * 3)
    for y in range(350, 450):
        for x in range(580, 700):
            o = (y * w + x) * 3
            px[o:o + 3] = bytes(((x * 7) % 256, (y * 5) % 256, (x + y) % 256))
    return w, h, bytes(px)


def ppm(w, h, rgb, comment=False):
    head = b"P6\n" + (b"# made by a test\n" if comment else b"") + b"%d %d\n255\n" % (w, h)
    return head + rgb


KERNEL = "[    0.000000] Linux version 6.17.1-300.fc45.x86_64 (mockbuild@fedoraproject.org) #1 SMP PREEMPT_DYNAMIC\r\n"
NONCE = "0123456789abcdef0123456789abcdef"   # fake test mark, not a secret  # pragma: allowlist secret
# The kernel's own line with the test copy's random mark (printed early, replayed to ttyS0).
CMDLINE = ("[    0.000000] Command line: BOOT_IMAGE=/images/pxeboot/vmlinuz forge.boottest=" + NONCE
           + " inst.stage2=hd:LABEL=Forge-OS-45 console=tty0 console=ttyS0,115200n8\r\n")
FW_OK = 'BdsDxe: starting Boot0001 "UEFI QEMU DVD-ROM QM00005 " from PciRoot(0x0)/Pci(0x1F,0x2)\r\n'

FAKE_QEMU = r'''#!/usr/bin/env python3
# Fake qemu-system-x86_64: enough QMP for iso_boot_check.py. FAKE_QEMU_MODE picks the story.
import json, os, socket, sys, time
mode = os.environ.get("FAKE_QEMU_MODE", "gui")
args = sys.argv[1:]
with open(os.environ["FAKE_QEMU_ARGS"], "w") as f:
    json.dump(args, f)
def opt(name):
    return args[args.index(name) + 1]
serial = opt("-serial")[len("file:"):]
sock_path = opt("-qmp")[len("unix:"):].split(",")[0]
with open(os.environ["FAKE_QEMU_ARGS"] + ".pid", "w") as f:
    f.write(str(os.getpid()))
# The kernel prints its command line, with the mark the test copy put on the kernel line.
iso = [a for a in args if "media=cdrom" in a][0].split(",")[0][len("file="):]
import re
found = re.search(r"forge\.boottest=([0-9a-f]{32})", open(iso, encoding="latin-1").read())
nonce = found.group(1) if found else "none"
CMD = "[    0.000000] Command line: BOOT_IMAGE=/images/pxeboot/vmlinuz forge.boottest=" + nonce + " console=ttyS0,115200n8\r\n"
KERNEL = "[    0.000000] Linux version 6.17.1-300.fc45.x86_64 (mockbuild@fedoraproject.org) #1 SMP\r\n" + CMD
with open(serial, "w") as f:
    if mode != "gui-no-start":
        f.write("BdsDxe: starting Boot0001 \"UEFI QEMU DVD-ROM QM00005 \"\r\n")
    if mode == "shell":
        f.write("UEFI Interactive Shell v2.2\r\n")
    if mode == "denied":
        f.write("BdsDxe: failed to load Boot0001 \"UEFI QEMU DVD-ROM\": Access Denied\r\n")
        f.write("BdsDxe: No bootable option or device was found.\r\n")
    if mode == "hostile":
        f.write("::error::pwned\r\n::add-mask::x\r\n  ::warning file=a::indented\r\n::stop-commands::guess\r\n::guess::\r\n::set-output name=x::y\r\n")
# Lines the guest prints later, after the given number of screenshots.
LATER = {
    "installer": (2, KERNEL + "Run /init as init process\r\nanaconda 45.1-1.fc45 for Forge OS 45 started.\r\n"),
    "installer-systemd": (2, KERNEL + "[  OK  ] Started anaconda.service - Anaconda.\r\n"),
    "kernel-only": (1, KERNEL + "Welcome to Fedora Linux 45 dracut-107-1.fc45 (Initramfs)!\r\n"),
    "guest-denied": (1, KERNEL + "audit: op=load Access Denied\r\nStarting installer, one moment...\r\n"),
    "panic": (1, KERNEL + "Kernel panic - not syncing: VFS: Unable to mount root fs\r\n"),
    "gui": (1, "anaconda 45.1 for Forge OS 45 started.\r\n"),   # installer words, but no kernel
    # The reviewer's GRUB cases: grub.cfg echo lines and menu titles, no kernel at all.
    "grub-echo": (1, "[    0.000000] Linux version 6.8.0-fake (printed by grub.cfg echo)\r\nStarting installer, one moment...\r\n"),
    "grub-menu": (1, "\x1b[05;:0H *Linux version 6.8 (just a menu title)      Starting installer, one moment\r\n"),
    # A guessed or stale mark is not the kernel's.
    "wrong-nonce": (1, CMD.replace(nonce, "f" * 32) + "Starting installer, one moment...\r\n"),
    # Installer words before the kernel's command line do not count.
    "installer-first": (1, "Starting installer, one moment...\r\n" + KERNEL + "Run /init as init process\r\n"),
}
if mode == "exit":
    sys.exit(3)
def screen(n):
    w, h = 320, 240
    good = mode in ("gui", "gui-no-start") or (mode == "gui-late" and n >= 3) or (mode == "flicker" and n % 2 == 0)
    if not good:
        return w, h, bytes(w * h * 3)
    px = bytearray()
    for y in range(h):
        for x in range(w):
            px += bytes(((x * 7) % 256, (y * 5) % 256, (x + y) % 256)) if x > 80 else b"\xff\xff\xff"
    return w, h, bytes(px)
srv = socket.socket(socket.AF_UNIX)
srv.bind(sock_path)
srv.listen(1)
conn, _ = srv.accept()
f = conn.makefile("rw")
f.write(json.dumps({"QMP": {"version": {}, "capabilities": []}}) + "\n"); f.flush()
dumps = 0
for line in f:
    cmd = json.loads(line)
    if cmd["execute"] == "screendump":
        w, h, px = screen(dumps)
        dumps += 1
        with open(cmd["arguments"]["filename"], "wb") as out:
            out.write(b"P6\n%d %d\n255\n" % (w, h) + px)
        if mode in LATER and dumps == LATER[mode][0]:
            with open(serial, "a") as s:
                s.write(LATER[mode][1])
    f.write(json.dumps({"return": {}}) + "\n"); f.flush()
    if cmd["execute"] == "quit":
        break
'''


class ScreenCheckTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def verdict(self, screen):
        return self.m.looks_like_installer(self.m.screen_stats(*screen))

    def test_graphical_screens_count_as_installer(self):
        self.assertTrue(self.verdict(gui()))
        # A dark theme is fine too: dark grey is not "black".
        self.assertTrue(self.verdict(gui(base=(36, 36, 36))))

    def test_boot_and_failure_screens_do_not(self):
        for name, screen in [("black", solid(1280, 800, (0, 0, 0))),
                             ("text console", text_console()),
                             ("firmware menu", firmware_menu()),
                             ("boot logo", logo_on_black()),
                             ("plain colour", solid(1280, 800, (60, 110, 180))),
                             ("tiny", gui(8, 32))]:
            self.assertFalse(self.verdict(screen), name)

    def test_ppm_parsing(self):
        w, h, rgb = gui(64, 48)
        self.assertEqual(self.m.read_ppm(ppm(w, h, rgb)), (w, h, rgb))
        self.assertEqual(self.m.read_ppm(ppm(w, h, rgb, comment=True)), (w, h, rgb))
        for bad in (b"", b"P5\n2 2\n255\n" + bytes(4), b"P6\n2 2\n255\n" + bytes(5),
                    b"P6\n2 2\n65535\n" + bytes(24), b"P6\n0 2\n255\n", b"P6\nx 2\n255\n"):
            with self.assertRaises(ValueError):
                self.m.read_ppm(bad)

    def test_png_output_is_a_valid_png_of_the_same_pixels(self):
        w, h, rgb = gui(64, 48)
        data = self.m.to_png(w, h, rgb)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", data[16:24]), (w, h))
        # Collect IDAT chunks, check CRCs, and undo the per-row filter byte (filter 0).
        pos, idat = 8, b""
        while pos < len(data):
            n, kind = struct.unpack(">I4s", data[pos:pos + 8])
            body = data[pos + 8:pos + 8 + n]
            crc = struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0]
            self.assertEqual(crc, zlib.crc32(kind + body) & 0xffffffff)
            if kind == b"IDAT":
                idat += body
            pos += 12 + n
        raw = zlib.decompress(idat)
        rows = [raw[i * (w * 3 + 1):(i + 1) * (w * 3 + 1)] for i in range(h)]
        self.assertTrue(all(r[0] == 0 for r in rows))
        self.assertEqual(b"".join(r[1:] for r in rows), rgb)


class FirmwareLogTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_known_firmware_failures(self):
        # Lines as OVMF 2024.02 (Ubuntu 24.04) prints them on the serial port.
        cases = [
            'BdsDxe: failed to load Boot0001 "UEFI QEMU DVD-ROM QM00005 " from PciRoot(0x0): Access Denied\r\n',
            "BdsDxe: No bootable option or device was found.\r\n",
            "\x1b[2J\x1b[01;01HUEFI Interactive Shell v2.2\r\nEDK II\r\n",
        ]
        for text in cases:
            self.assertTrue(self.m.firmware_failure(text), text)

    def test_printed_log_has_no_terminal_codes(self):
        text = self.m.printable("\x1b[2J\x1b[01;01H\x1b[=3h\x1b[1m\x1b[33mMapping table\x1b[0m\r\nok\x07")
        self.assertEqual(text.strip(), "Mapping table\nok")

    def test_normal_boot_is_not_a_failure(self):
        text = ('BdsDxe: loading Boot0001 "UEFI QEMU DVD-ROM QM00005 " from PciRoot(0x0)\r\n'
                'BdsDxe: starting Boot0001 "UEFI QEMU DVD-ROM QM00005 " from PciRoot(0x0)\r\n'
                "Install Forge OS\r\nTest this media & install Forge OS\r\n")
        self.assertIsNone(self.m.firmware_failure(text))


class SerialEvidenceTests(unittest.TestCase):
    """Installer run: the kernel's own command line (with the random mark) and then the installer."""

    def setUp(self):
        self.m = load()

    def ev(self, text):
        return self.m.installer_evidence(text, NONCE)

    def test_kernel_then_installer_passes(self):
        for line in ("anaconda 45.1-1.fc45 for Forge OS 45 started.\r\n",
                     "Starting installer, one moment...\r\n",
                     "[  OK  ] Started anaconda.service - Anaconda.\r\n",
                     "[  OK  ] Started anaconda-direct.service - Anaconda (direct).\r\n",
                     "[  OK  ] Reached target anaconda.target - Anaconda System Services.\r\n"):
            ev = self.ev(FW_OK + KERNEL + CMDLINE + "[    2.1] systemd[1]: ok\r\n" + line)
            self.assertTrue(ev, line)
            self.assertIn("Command line: BOOT_IMAGE=", ev[0])
            self.assertIn("forge.boottest=" + NONCE, ev[0])
            self.assertEqual(ev[1], line.strip())
        # init/main.c prints it again as "Kernel command line:", without a timestamp on some kernels.
        other = CMDLINE.replace("[    0.000000] Command line:", "Kernel command line:")
        self.assertTrue(self.ev(FW_OK + other + "Starting installer, one moment...\r\n"))

    def test_kernel_alone_is_not_enough(self):
        self.assertIsNone(self.ev(
            FW_OK + KERNEL + CMDLINE + "Welcome to Fedora Linux 45 dracut-107 (Initramfs)!\r\n"
            "[  OK  ] Started dracut-initqueue.service - dracut initqueue hook.\r\n"))

    def test_installer_words_before_the_kernel_do_not_count(self):
        # A GRUB menu or picture can say anything. Only lines after the kernel's command line count.
        grub = "Install Forge OS\r\nanaconda 45 for Forge OS started.\r\nStarting installer, one moment...\r\n"
        self.assertIsNone(self.ev(FW_OK + grub))
        self.assertIsNone(self.ev(FW_OK + grub + KERNEL + CMDLINE))
        self.assertIsNone(self.ev(FW_OK + KERNEL + grub + CMDLINE))

    def test_grub_text_without_the_kernel_is_no_evidence(self):
        # Review finding: grub.cfg "echo" lines and menu titles passed the installer run with no kernel.
        echo = FW_OK + "[    0.000000] Linux version 6.8.0-fake (printed by grub.cfg echo, no kernel)\r\nStarting installer, one moment...\r\n"
        menu = (FW_OK + "a command-line.\x1b[05;:0H *Linux version 6.8 (just a menu title)          "
                "Starting installer, one moment\r\n")
        for text in (echo, menu):
            self.assertIsNone(self.ev(text))
            self.assertEqual(self.m.split_at_kernel(text, NONCE)[1], "", "GRUB text is not the kernel")

    def test_only_the_kernel_s_own_line_with_this_runs_mark_counts(self):
        after = "Starting installer, one moment...\r\n"
        for fake in (CMDLINE.replace(NONCE, "f" * 32),                      # another run's or a guessed mark
                     CMDLINE.replace(NONCE, NONCE + "0"),                    # longer value
                     CMDLINE.replace("forge.boottest=", "xforge.boottest="),  # part of another word
                     CMDLINE.replace("Command line:", "Command line"),
                     "linux /images/pxeboot/vmlinuz forge.boottest=" + NONCE + "\r\n",   # grub.cfg text (cat)
                     "echo Command line: forge.boottest=" + NONCE + "\r\n",
                     "loader/efi/linux.c:100: Command line: forge.boottest=" + NONCE + "\r\n"):
            self.assertIsNone(self.ev(FW_OK + KERNEL + fake + after), fake)

    def test_nothing_on_serial_is_no_evidence(self):
        self.assertIsNone(self.ev(""))
        self.assertIsNone(self.ev(FW_OK))

    def test_access_denied_from_the_guest_is_not_a_firmware_failure(self):
        guest = FW_OK + KERNEL + CMDLINE + "audit: Access Denied\r\nUEFI Interactive Shell\r\nNo bootable option or device was found\r\n"
        self.assertIsNone(self.m.firmware_failure(guest, NONCE))
        self.assertTrue(self.m.firmware_failure(
            'BdsDxe: failed to load Boot0001 "UEFI QEMU DVD-ROM": Access Denied\r\n' + KERNEL + CMDLINE, NONCE))
        # A fake "Linux version" line from GRUB does not hide a later firmware failure.
        self.assertTrue(self.m.firmware_failure(
            "Linux version 6.8 (menu title)\r\nBdsDxe: failed to load Boot0002: Access Denied\r\n", NONCE))

    def test_guest_failures(self):
        self.assertTrue(self.m.guest_failure(KERNEL + CMDLINE + "Kernel panic - not syncing: VFS: Unable to mount root fs\r\n", NONCE))
        self.assertTrue(self.m.guest_failure(KERNEL + CMDLINE + "You are in emergency mode. Entering emergency mode. Exit the shell to continue.\r\n", NONCE))
        self.assertIsNone(self.m.guest_failure("Kernel panic - not syncing (a GRUB menu title)\r\n", NONCE))
        self.assertIsNone(self.m.guest_failure(KERNEL + "Kernel panic - not syncing (before the command line)\r\n", NONCE))
        self.assertIsNone(self.m.guest_failure(KERNEL + CMDLINE + "Starting installer, one moment...\r\n", NONCE))
        # The firmware run has no mark: there the kernel's first line still splits.
        self.assertTrue(self.m.guest_failure(KERNEL + "Kernel panic - not syncing: x\r\n"))

    def test_boot_loader_started(self):
        self.assertTrue(self.m.boot_loader_started(FW_OK))
        self.assertFalse(self.m.boot_loader_started('BdsDxe: loading Boot0001 "UEFI QEMU DVD-ROM"\r\n'))
        self.assertFalse(self.m.boot_loader_started(KERNEL + FW_OK))


class GrubConfigTests(unittest.TestCase):
    """The test copy changes only the kernel lines of grub.cfg."""

    CFG = ("set default=\"1\"\n"
           "function load_video {\n  insmod all_video\n}\n"
           "search --no-floppy --set=root -l 'Forge-OS-45'\n"
           "menuentry 'Install Forge OS 45 (quiet)' --class fedora {\n"
           "\tlinux /images/pxeboot/vmlinuz inst.stage2=hd:LABEL=Forge-OS-45 quiet rhgb\n"
           "\tinitrd /images/pxeboot/initrd.img\n}\n"
           "menuentry 'Test this media & install Forge OS 45' {\r\n"
           "    linuxefi /images/pxeboot/vmlinuz inst.stage2=hd:LABEL=Forge-OS-45 rd.live.check console=tty1 quiet\r\n"
           "\tinitrd /images/pxeboot/initrd.img\r\n}\r\n")

    def setUp(self):
        self.m = load()

    def serial(self, text):
        return self.m.serial_grub_cfg(text, NONCE)

    def test_kernel_lines_get_a_serial_console(self):
        new, n = self.serial(self.CFG)
        self.assertEqual(n, 2)
        old_lines, new_lines = self.CFG.split("\n"), new.split("\n")
        self.assertEqual(len(old_lines), len(new_lines))
        for a, b in zip(old_lines, new_lines):
            if a.strip().startswith(("linux ", "linuxefi ")):
                self.assertTrue(b.rstrip("\r").endswith("console=tty0 console=ttyS0,115200n8"), b)
                self.assertEqual(a.endswith("\r"), b.endswith("\r"), "line ending kept")
                words = b.split()
                for gone in ("quiet", "rhgb", "rd.live.check", "console=tty1"):
                    self.assertNotIn(gone, words)
                self.assertIn("inst.stage2=hd:LABEL=Forge-OS-45", words)
                self.assertEqual(words[1], "/images/pxeboot/vmlinuz")
                # The random mark goes right after the kernel, so a long line can't push it out.
                self.assertEqual(words[2], "forge.boottest=" + NONCE)
            else:
                self.assertEqual(a, b)

    def test_no_kernel_line_means_nothing_to_boot(self):
        fake = 'insmod png\nbackground_image /bg.png\nmenuentry "Install Forge OS (no kernel here)" { echo "nothing" }\n'
        self.assertEqual(self.serial(fake), (fake, 0))

    def test_quoting_and_variables_are_kept(self):
        line = "  linux /vmlinuz 'inst.stage2=hd:LABEL=a  b' \"x  y\" quiet $opts ${extra} a\\ b\n"
        new, n = self.serial(line)
        self.assertEqual(n, 1)
        self.assertEqual(new, "  linux /vmlinuz forge.boottest=" + NONCE + " 'inst.stage2=hd:LABEL=a  b' \"x  y\" $opts ${extra} "
                              "a\\ b console=tty0 console=ttyS0,115200n8\n")

    def test_a_trailing_comment_stays_at_the_end(self):
        new, n = self.serial("\tlinux /vmlinuz quiet # boots the installer\n")
        self.assertEqual(new, "\tlinux /vmlinuz forge.boottest=" + NONCE + " console=tty0 console=ttyS0,115200n8 # boots the installer\n")
        # "#" inside a word is not a comment in GRUB.
        new, _ = self.serial("linux /vmlinuz a#b\n")
        self.assertIn(" a#b console=tty0", new)

    def test_lines_end_only_at_newline_like_grub(self):
        # Review finding: splitlines() also splits at these, GRUB does not. The text after them is
        # still part of the echo command, so it must not get the mark.
        for sep in ("\x0c", "\x1c", "\x1d", "\x1e", "\x0b", "\x85", "\u2028", "\u2029"):
            cfg = "echo Command" + sep + "linux /vmlinuz quiet\nlinux /vmlinuz quiet\n"
            new, n = self.serial(cfg)
            self.assertEqual(n, 1, repr(sep))
            self.assertTrue(new.startswith("echo Command" + sep + "linux /vmlinuz quiet\n"), repr(sep))

    def test_carriage_returns_like_grub(self):
        # GRUB drops every \r (lib/getline.c): CRLF files work, and "li\rnux" is a kernel line.
        new, n = self.serial("linux /vmlinuz quiet\r\n")
        self.assertEqual((new, n), ("linux /vmlinuz forge.boottest=" + NONCE + " console=tty0 console=ttyS0,115200n8\r\n", 1))
        for cfg in ("li\rnux /vmlinuz quiet\n", "linux /vmlinuz\r quiet\n", "\rlinux /vmlinuz\n"):
            with self.assertRaises(self.m.CopyError, msg=repr(cfg)):
                self.serial(cfg)

    def test_control_characters_on_a_kernel_line_are_refused(self):
        for c in ("\x0c", "\x1b", "\x07", "\x85", "\u2028", "\u202e", "\x7f", "\udcff"):
            with self.assertRaises(self.m.CopyError, msg=repr(c)):
                self.serial("linux /vmlinuz quiet" + c + "x\n")
        with self.assertRaises(self.m.CopyError):
            self.serial("echo a\x00b\nlinux /vmlinuz\n")      # GRUB would cut that line at the NUL

    def test_continued_or_quoted_lines_are_not_kernel_lines(self):
        # GRUB joins these into the echo command: the "linux" line is text, not a command.
        for cfg in ("echo Command \\\nlinux /vmlinuz quiet\n",
                    'echo "Command\nlinux /vmlinuz quiet"\n',
                    "echo 'Command\nlinux /vmlinuz quiet'\n",
                    'echo "a \\"\nlinux /vmlinuz quiet"\n',
                    "echo a#'\nlinux /vmlinuz quiet'\n"):
            self.assertEqual(self.serial(cfg), (cfg, 0), repr(cfg))

    def test_lines_this_check_cannot_follow_are_refused(self):
        for cfg in ("linux /vmlinuz quiet \\\n  rhgb\n",           # continuation of a kernel line
                    "linux /vmlinuz 'quiet\n  rhgb'\n",              # quote across lines
                    "linux /vmlinuz quiet; echo hi\n",              # a second command on the line
                    "linux /vmlinuz quiet }\n",
                    "linux /vmlinuz quiet | x\n",
                    "linux\n",                                     # no kernel
                    "echo \"never closed\n",                         # ends inside a quote
                    "echo a \\",                                    # ends with a continuation
                    "# don't\nlinux /vmlinuz\n",                     # quote in a comment
                    "#x \\\nlinux /vmlinuz\n",
                    "echo ${a\"b}\nlinux /vmlinuz\n"):
            with self.assertRaises(self.m.CopyError, msg=repr(cfg)):
                self.serial(cfg)

    def test_marker_text_in_grub_cfg_is_refused(self):
        # Belt and braces: a grub.cfg that already says what the check looks for is refused.
        for text in ("echo forge.boottest=" + "a" * 32, 'echo "[ 0.0] Linux version 6.8"', "menuentry 'Command line: x' {",
                     "echo Starting installer, one moment", "echo anaconda 45 for Forge started",
                     "echo Started anaconda.service", "function linux { echo x }", "function  linuxefi {"):
            with self.assertRaises(self.m.CopyError, msg=text):
                self.serial(text + "\nlinux /vmlinuz\n")

    def test_grub_commands_that_read_files_or_build_commands_are_refused(self):
        # Third review: GRUB could "cat" its own changed grub.cfg (which holds the mark) after a
        # split "Command line:" prefix, and pass with no kernel at all. Split spellings too.
        grubcat = ('linux /images/pxeboot/does-not-exist\necho ""\necho -n Command "line:" ""\n'
                   'cat /boot/grub/grub.cfg\necho ""\necho -n "Starting inst"\necho "aller, one moment"\n')
        for cfg in (grubcat, 'linux /vmlinuz\n"c"at /boot/grub/grub.cfg\n', "linux /vmlinuz\nc\\at /x\n",
                    "linux /vmlinuz\nhexdump /boot/grub/grub.cfg\n", "linux /vmlinuz\nsource /x.cfg\n",
                    "linux /vmlinuz\nconfigfile /x.cfg\n", "linux /vmlinuz\neval \"c\" \"at /x\"\n",
                    "linux /vmlinuz\nset x=cat\n$x /boot/grub/grub.cfg\n", "linux /vmlinuz\nsmbios --type 1\n",
                    "if true; then cat /x; fi\nlinux /vmlinuz\n", "linux /vmlinuz\nloadenv /x\n"):
            with self.assertRaises(self.m.CopyError, msg=repr(cfg)):
                self.serial(cfg)

    def test_a_fedora_like_grub_cfg_is_still_accepted(self):
        cfg = ("set default=\"1\"\nfunction load_video {\n  insmod efi_gop\n  insmod all_video\n}\nload_video\n"
               "set gfxpayload=keep\ninsmod gzio\ninsmod part_gpt\ninsmod ext2\nset timeout=60\n"
               "search --no-floppy --set=root -l 'Fedora-KDE-ostree-x86_64-45'\n"
               "menuentry 'Install Forge OS' --class fedora --class gnu-linux --class gnu --class os {\n"
               "\tlinux /images/pxeboot/vmlinuz inst.stage2=hd:LABEL=Fedora quiet\n"
               "\tinitrd /images/pxeboot/initrd.img\n}\n"
               "submenu 'Troubleshooting -->' {\n  menuentry 'Rescue' {\n\tlinux /images/pxeboot/vmlinuz rescue\n"
               "\tinitrd /images/pxeboot/initrd.img\n  }\n}\n")
        new, changed = self.serial(cfg)
        self.assertEqual(changed, 2)

    def test_the_mark_must_be_a_fresh_random_value(self):
        with self.assertRaises(ValueError):
            self.m.serial_grub_cfg(self.CFG, "x; echo")


def tool(name):
    return shutil.which(name)


@unittest.skipUnless(tool("xorriso") and tool("mcopy") and tool("mformat") and tool("mdir"), "needs xorriso and mtools")
class SerialCopyTests(unittest.TestCase):
    """The test copy: only grub.cfg changes, everything else stays byte-identical (sha256)."""

    def setUp(self):
        self.m = load()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = dict(os.environ, MTOOLS_SKIP_CHECK="1")

    def make_iso(self, name, cfg=GrubConfigTests.CFG, kernel=b"kernel" * 50000, extra=None, hidden_efi=False):
        root = os.path.join(self.tmp, name + "-root")
        for d in ("EFI/BOOT", "boot/grub2", "images/pxeboot"):
            os.makedirs(os.path.join(root, d))
        files = {"EFI/BOOT/BOOTX64.EFI": os.urandom(70000), "EFI/BOOT/grubx64.efi": os.urandom(90000),
                 "images/pxeboot/vmlinuz": kernel, "images/pxeboot/initrd.img": os.urandom(300000)}
        for rel, data in (extra or {}).items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            files[rel] = data
        for rel, data in files.items():
            with open(os.path.join(root, rel), "wb") as f:
                f.write(data)
        for rel in ("EFI/BOOT/grub.cfg", "boot/grub2/grub.cfg"):
            with open(os.path.join(root, rel), "w", newline="") as f:
                f.write(cfg)
        # hidden_efi: like Fedora's boot.iso (lorax), the UEFI boot image is not a file in the ISO but
        # a partition appended after it, which El Torito points to.
        img = os.path.join(self.tmp, name + "-efiboot.img") if hidden_efi else os.path.join(root, "images/efiboot.img")
        subprocess.run(["mformat", "-C", "-f", "2880", "-v", "ANACONDA", "-i", img, "::"], check=True, env=self.env)
        subprocess.run(["mmd", "-i", img, "::/EFI", "::/EFI/BOOT"], check=True, env=self.env)
        for rel in ("EFI/BOOT/BOOTX64.EFI", "EFI/BOOT/grubx64.efi", "EFI/BOOT/grub.cfg"):
            subprocess.run(["mcopy", "-i", img, os.path.join(root, rel), "::/" + rel], check=True, env=self.env)
        iso = os.path.join(self.tmp, name + ".iso")
        efi = (["-append_partition", "2", "0xef", img, "-appended_part_as_gpt",
                "-e", "--interval:appended_partition_2:all::"] if hidden_efi else ["-e", "images/efiboot.img"])
        subprocess.run(["xorriso", "-as", "mkisofs", "-V", "Forge-OS-45", "-R", "-J", *efi,
                        "-no-emul-boot", "-o", iso, root], check=True, capture_output=True)
        return iso, root

    def hidden_image(self, iso):
        """The appended UEFI boot image of `iso`, read where El Torito points."""
        out = subprocess.run(["xorriso", "-indev", iso, "-report_el_torito", "plain"],
                             capture_output=True, text=True, check=True).stdout
        n, lba = re.search(r"^El Torito boot img :\s+(\d+)\s+UEFI\b.*\s(\d+)\s*$", out, re.M).groups()
        self.assertNotRegex(out, rf"El Torito img path :\s+{n}\s", "the UEFI image is not a file in the ISO")
        img = os.path.join(self.tmp, "hidden-" + hashlib.sha256(iso.encode()).hexdigest()[:12] + ".img")
        with open(iso, "rb") as f, open(img, "wb") as o:
            f.seek(int(lba) * 2048)
            o.write(f.read(2880 * 1024))
        return img

    def extract(self, iso, path):
        out = os.path.join(self.tmp, "x-" + hashlib.sha256((iso + path).encode()).hexdigest()[:12])
        subprocess.run(["xorriso", "-osirrox", "on", "-indev", iso, "-extract", path, out], check=True, capture_output=True)
        with open(out, "rb") as f:
            return f.read()

    def test_copy_changes_only_grub_cfg(self):
        iso, root = self.make_iso("good")
        dest = os.path.join(self.tmp, "copy.iso")
        report = self.m.make_serial_copy(iso, dest, self.tmp, NONCE)
        text = "\n".join(report)
        for rel in ("EFI/BOOT/BOOTX64.EFI", "EFI/BOOT/grubx64.efi", "images/pxeboot/vmlinuz", "images/pxeboot/initrd.img"):
            with open(os.path.join(root, rel), "rb") as f:
                want = f.read()
            self.assertEqual(self.extract(dest, "/" + rel), want, rel)
            self.assertIn(hashlib.sha256(want).hexdigest(), text, "boot binaries are listed with their sha256")
        for rel in ("/EFI/BOOT/grub.cfg", "/boot/grub2/grub.cfg"):
            cfg = self.extract(dest, rel).decode()
            self.assertIn("console=ttyS0,115200n8", cfg)
            self.assertEqual(cfg, self.m.serial_grub_cfg(GrubConfigTests.CFG, NONCE)[0])
            self.assertIn("forge.boottest=" + NONCE, cfg)
        # The grub.cfg inside the UEFI boot image too, and its binaries are unchanged.
        img = os.path.join(self.tmp, "efiboot-copy.img")
        with open(img, "wb") as f:
            f.write(self.extract(dest, "/images/efiboot.img"))
        inner = subprocess.run(["mcopy", "-n", "-i", img, "::/EFI/BOOT/grub.cfg", "-"], capture_output=True, env=self.env).stdout
        self.assertIn(b"console=ttyS0,115200n8", inner)
        self.assertIn(b"forge.boottest=" + NONCE.encode(), inner)
        shim = subprocess.run(["mcopy", "-n", "-i", img, "::/EFI/BOOT/BOOTX64.EFI", "-"], capture_output=True, env=self.env).stdout
        with open(os.path.join(root, "EFI/BOOT/BOOTX64.EFI"), "rb") as f:
            self.assertEqual(shim, f.read())
        # Same label (the installer finds its files by it) and the same UEFI boot image.
        self.assertEqual(self.m.volume_id(dest), "Forge-OS-45")
        self.assertEqual(self.m.efi_boot_images(dest), ["/images/efiboot.img"])

    def test_appended_uefi_partition_like_fedora(self):
        # CI finding: Fedora's boot.iso keeps its UEFI boot image in a partition appended after the
        # ISO (no file in the ISO), so the test copy was refused with "no UEFI boot image file".
        iso, root = self.make_iso("appended", hidden_efi=True)
        self.assertNotIn("/images/efiboot.img", self.m.iso_files(iso))
        dest = os.path.join(self.tmp, "copy.iso")
        text = "\n".join(self.m.make_serial_copy(iso, dest, self.tmp, NONCE))
        self.assertIn("changed: appended partition 2:/EFI/BOOT/grub.cfg", text)
        img = self.hidden_image(dest)
        inner = subprocess.run(["mcopy", "-n", "-i", img, "::/EFI/BOOT/grub.cfg", "-"], capture_output=True, env=self.env).stdout
        self.assertEqual(inner.decode(), self.m.serial_grub_cfg(GrubConfigTests.CFG, NONCE)[0])
        for rel in ("EFI/BOOT/BOOTX64.EFI", "EFI/BOOT/grubx64.efi"):
            data = subprocess.run(["mcopy", "-n", "-i", img, "::/" + rel, "-"], capture_output=True, env=self.env).stdout
            with open(os.path.join(root, rel), "rb") as f:
                self.assertEqual(data, f.read(), rel)
        for rel in ("/EFI/BOOT/grub.cfg", "/boot/grub2/grub.cfg"):
            self.assertIn("forge.boottest=" + NONCE, self.extract(dest, rel).decode())
        self.assertEqual(self.m.volume_id(dest), "Forge-OS-45")
        self.assertEqual(set(self.m.hidden_efi_images(dest)), {"appended partition 2"})

    def test_a_changed_appended_uefi_partition_is_caught(self):
        a, _ = self.make_iso("ha", hidden_efi=True)
        b = os.path.join(self.tmp, "hb.iso")
        shutil.copyfile(a, b)
        # Only the boot loader inside the appended partition changes (same size, same place).
        img = self.hidden_image(b)
        other = os.path.join(self.tmp, "other.efi")
        with open(other, "wb") as f:
            f.write(os.urandom(90000))
        subprocess.run(["mcopy", "-o", "-i", img, other, "::/EFI/BOOT/grubx64.efi"], check=True, env=self.env)
        out = subprocess.run(["xorriso", "-indev", b, "-report_el_torito", "plain"], capture_output=True, text=True).stdout
        lba = int(re.search(r"^El Torito boot img :\s+\d+\s+UEFI\b.*\s(\d+)\s*$", out, re.M).group(1))
        with open(img, "rb") as f, open(b, "r+b") as o:
            o.seek(lba * 2048)
            o.write(f.read())
        with self.assertRaises(self.m.CopyError) as cm:
            self.m.compare_isos(a, b, changed=set(), work=self.tmp)
        self.assertIn("appended partition 2", str(cm.exception))

    def test_hidden_or_system_files_inside_the_uefi_image_are_compared(self):
        # Review finding: mdir leaves out files with the FAT hidden or system flag, which firmware
        # still loads, so a changed boot loader with that flag went unseen.
        a, _ = self.make_iso("fa", hidden_efi=True)
        img = self.hidden_image(a)
        subprocess.run(["mattrib", "-i", img, "+h", "+s", "::/EFI/BOOT/BOOTX64.EFI"], check=True, env=self.env)
        out = subprocess.run(["xorriso", "-indev", a, "-report_el_torito", "plain"], capture_output=True, text=True).stdout
        lba = int(re.search(r"^El Torito boot img :\s+\d+\s+UEFI\b.*\s(\d+)\s*$", out, re.M).group(1))
        with open(img, "rb") as f, open(a, "r+b") as o:
            o.seek(lba * 2048)
            o.write(f.read())
        b = os.path.join(self.tmp, "fb.iso")
        shutil.copyfile(a, b)
        img = self.hidden_image(b)
        other = os.path.join(self.tmp, "other-shim.efi")
        with open(other, "wb") as f:
            f.write(os.urandom(70000))
        subprocess.run(["mcopy", "-o", "-i", img, other, "::/EFI/BOOT/BOOTX64.EFI"], check=True, env=self.env)
        subprocess.run(["mattrib", "-i", img, "+h", "+s", "::/EFI/BOOT/BOOTX64.EFI"], check=True, env=self.env)
        with open(img, "rb") as f, open(b, "r+b") as o:
            o.seek(lba * 2048)
            o.write(f.read())
        with self.assertRaises(self.m.CopyError) as cm:
            self.m.compare_isos(a, b, changed=set(), work=self.tmp)
        self.assertIn("BOOTX64.EFI", str(cm.exception))

    def test_iso_without_a_kernel_line_is_refused(self):
        iso, _ = self.make_iso("nokernel", cfg='insmod png\nmenuentry "Install" { echo "nothing" }\n')
        with self.assertRaises(self.m.CopyError):
            self.m.make_serial_copy(iso, os.path.join(self.tmp, "copy.iso"), self.tmp, NONCE)

    def test_only_the_grub_cfg_files_grub_reads_are_changed(self):
        # Review finding: any file named grub.cfg was edited, so GRUB could print an edited copy
        # (with the mark) from another file. Only GRUB's own config paths change now.
        other = b"linux /images/pxeboot/vmlinuz quiet\n"
        iso, root = self.make_iso("samename", extra={"docs/GRUB.CFG": other, "docs/grub.cfg": other,
                                                     "EFI/BOOT/x/grub.cfg": other})
        dest = os.path.join(self.tmp, "copy.iso")
        text = "\n".join(self.m.make_serial_copy(iso, dest, self.tmp, NONCE))
        for rel in ("/docs/GRUB.CFG", "/docs/grub.cfg", "/EFI/BOOT/x/grub.cfg"):
            self.assertEqual(self.extract(dest, rel), other, rel)
            self.assertNotIn(rel, text.split("byte-identical")[0])
        self.assertIn("changed: /EFI/BOOT/grub.cfg", text)
        self.assertIn("changed: /boot/grub2/grub.cfg", text)

    def test_file_names_with_control_characters_are_refused(self):
        # Review finding: names with a newline (and \x1c, \x85 ...) were skipped by the per-file
        # hashing, so a change to that file went unseen.
        for n, name in enumerate(("a\nb", "c\x1cd", "e\x1bf", "g\x85h", "i\rj", "k\u2028l")):
            iso, _ = self.make_iso(f"names{n}", extra={"docs/" + name: b"x"})
            with self.assertRaises(self.m.CopyError, msg=repr(name)) as cm:
                self.m.iso_files(iso)
            self.assertIn("file name", str(cm.exception))
            self.assertNotIn(name, str(cm.exception), "the name is shown escaped, not raw")

    def test_grub_cfg_with_forbidden_text_is_refused_before_copying(self):
        iso, _ = self.make_iso("echo", cfg='echo "Starting installer, one moment"\nlinux /images/pxeboot/vmlinuz\n')
        dest = os.path.join(self.tmp, "copy.iso")
        with self.assertRaises(self.m.CopyError):
            self.m.make_serial_copy(iso, dest, self.tmp, NONCE)
        self.assertFalse(os.path.exists(dest))

    def test_any_other_difference_is_caught(self):
        a, _ = self.make_iso("a")
        b, _ = self.make_iso("b", kernel=b"other" * 60000)
        with self.assertRaises(self.m.CopyError) as cm:
            self.m.compare_isos(a, b, changed=set(), work=self.tmp)
        self.assertIn("vmlinuz", str(cm.exception))


class OutputSafetyTests(unittest.TestCase):
    """Guest text printed to the CI log can't become GitHub workflow commands."""

    def setUp(self):
        self.m = load()

    def test_untrusted_text_is_fenced_with_a_random_token(self):
        out = []
        for _ in range(2):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.m.print_untrusted("Serial:", "::error::pwned\r\n::add-mask::x\n\x1b[31mred\x07\n")
            out.append(buf.getvalue().splitlines())
        lines = out[0]
        self.assertEqual(lines[0], "Serial:")
        m = __import__("re").fullmatch(r"::stop-commands::([0-9a-f]{32,})", lines[1])
        self.assertTrue(m, lines)
        self.assertEqual(lines[-1], f"::{m.group(1)}::")
        self.assertIn("::error::pwned", lines)
        self.assertNotIn("\x1b", "".join(lines))
        self.assertNotEqual(out[0][1], out[1][1], "a new token every time")


class QemuCommandTests(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.iso = os.path.join(self.tmp, "forge-os.iso")
        open(self.iso, "wb").close()

    def argv(self, **kw):
        base = dict(qemu="qemu-system-x86_64", iso=self.iso, code="/fw/CODE.fd", nvram="/t/VARS.fd",
                    serial="/t/serial.log", qmp="/t/qmp.sock", secure_boot=False, kvm=True,
                    memory_mb=4096, cpus=2)
        base.update(kw)
        return self.m.qemu_argv(**base)

    def test_secure_boot_off(self):
        a = self.argv()
        self.assertIsInstance(a, list)
        self.assertEqual(a[0], "qemu-system-x86_64")
        joined = " ".join(a)
        self.assertIn("if=pflash,format=raw,unit=0,readonly=on,file=/fw/CODE.fd", joined)
        self.assertIn("if=pflash,format=raw,unit=1,file=/t/VARS.fd", joined)
        self.assertNotIn("smm=on", joined)
        self.assertNotIn("property=secure", joined)
        # The ISO is read-only raw data (no format guessing), and there's no network or reboot.
        self.assertIn(f"file={self.iso},format=raw,media=cdrom,readonly=on", joined)
        for flag in ("-no-reboot", "-nic", "-sandbox"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("-nic") + 1], "none")
        self.assertIn("accel=kvm", joined)
        self.assertEqual(a[a.index("-cpu") + 1], "host")

    def test_secure_boot_on_needs_smm_and_secure_flash(self):
        joined = " ".join(self.argv(secure_boot=True))
        self.assertIn("smm=on", joined)
        self.assertIn("driver=cfi.pflash01,property=secure,value=on", joined)

    def test_without_kvm(self):
        a = self.argv(kvm=False)
        self.assertIn("accel=tcg", " ".join(a))
        self.assertEqual(a[a.index("-cpu") + 1], "max")

    def test_firmware_files(self):
        self.assertEqual(self.m.firmware_files(False, "/o"), ("/o/OVMF_CODE_4M.fd", "/o/OVMF_VARS_4M.fd"))
        self.assertEqual(self.m.firmware_files(True, "/o"), ("/o/OVMF_CODE_4M.secboot.fd", "/o/OVMF_VARS_4M.ms.fd"))

    def test_commas_in_paths_are_refused(self):
        # A comma would let a file name add QEMU drive options.
        with self.assertRaises(ValueError):
            self.argv(iso="/x/a.iso,snapshot=off")
        with self.assertRaises(ValueError):
            self.argv(nvram="/t/v,x.fd")


class BootLoopTests(unittest.TestCase):
    """The whole run with a fake QEMU on PATH."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        q = os.path.join(self.bin, "qemu-system-x86_64")
        with open(q, "w") as f:
            f.write(FAKE_QEMU)
        os.chmod(q, 0o755)
        self.fw = os.path.join(self.tmp, "ovmf")
        os.makedirs(self.fw)
        for name in ("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd", "OVMF_CODE_4M.secboot.fd", "OVMF_VARS_4M.ms.fd"):
            with open(os.path.join(self.fw, name), "wb") as f:
                f.write(b"fake firmware")
        self.iso = os.path.join(self.tmp, "forge-os.iso")
        with open(self.iso, "wb") as f:
            f.write(b"fake iso")
        self.out = os.path.join(self.tmp, "out")

    def run_check(self, mode, *extra, timeout=8):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], FAKE_QEMU_MODE=mode,
                   FAKE_QEMU_ARGS=os.path.join(self.tmp, "args.json"))
        cmd = [sys.executable, SCRIPT, "--iso", self.iso, "--out", self.out, "--ovmf-dir", self.fw,
               "--accel", "tcg", "--interval", "0.1", "--timeout", str(timeout), *extra]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)

    def assert_screenshot(self):
        with open(os.path.join(self.out, "screen.png"), "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_reaches_installer(self):
        r = self.run_check("gui")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PASS", r.stdout)
        self.assert_screenshot()

    def test_installer_that_appears_later(self):
        r = self.run_check("gui-late")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_timeout_fails_and_keeps_the_screenshot(self):
        r = self.run_check("black", timeout=2)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("timed out", r.stdout)
        self.assert_screenshot()

    def test_flickering_screen_is_not_enough(self):
        # The installer must stay on screen for several checks in a row.
        r = self.run_check("flicker", timeout=2)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_firmware_failure_fails_fast(self):
        for mode in ("shell", "denied"):
            r = self.run_check(mode, timeout=30)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("firmware", r.stdout)
            self.assert_screenshot()

    def test_qemu_stopping_early_fails(self):
        r = self.run_check("exit")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("QEMU stopped", r.stdout)

    def test_secure_boot_uses_microsoft_keys_on_a_copy(self):
        r = self.run_check("gui", "--secure-boot")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.tmp, "args.json")) as f:
            args = " ".join(json.load(f))
        self.assertIn(os.path.join(self.fw, "OVMF_CODE_4M.secboot.fd"), args)
        self.assertNotIn(os.path.join(self.fw, "OVMF_VARS_4M.ms.fd"), args, "the template must not be written to")
        self.assertIn("property=secure,value=on", args)

    def test_missing_iso_is_a_usage_error(self):
        os.unlink(self.iso)
        r = self.run_check("gui")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_firmware_run_needs_the_boot_loader_to_start(self):
        # Colours alone are not enough: the firmware must also report that it started the boot loader.
        r = self.run_check("gui-no-start", timeout=2)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_firmware_pass_says_what_it_proves(self):
        r = self.run_check("gui")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("firmware stage only", r.stdout)

    def test_serial_output_cannot_run_workflow_commands(self):
        r = self.run_check("hostile", timeout=1)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        lines = r.stdout.splitlines()
        token = None
        for line in lines:
            stripped = line.strip()
            if token:
                if stripped == f"::{token}::":
                    token = None
                continue
            if stripped.startswith("::stop-commands::"):
                token = stripped[len("::stop-commands::"):]
                self.assertRegex(token, r"^[0-9a-f]{32,}$")
                continue
            self.assertFalse(stripped.startswith("::"), f"workflow command outside the fence: {line!r}")
        self.assertIsNone(token, "the fence is closed")
        self.assertIn("::error::pwned", r.stdout, "the text is still shown, inside the fence")

    # In-process runs, so the test copy (xorriso) can be replaced by a plain copy.
    def run_installer(self, mode, *extra, timeout=8, copy_error=None):
        m = load()

        def fake_copy(iso, dest, work, nonce):
            if copy_error:
                raise m.CopyError(copy_error)
            with open(dest, "w") as f:      # the fake QEMU reads the mark from the copy it boots
                f.write("fake copy\nlinux /images/pxeboot/vmlinuz forge.boottest=" + nonce + "\n")
            return ["changed: /EFI/BOOT/grub.cfg (fake copy for the test)"]
        m.make_serial_copy = fake_copy
        env = {"FAKE_QEMU_MODE": mode, "FAKE_QEMU_ARGS": os.path.join(self.tmp, "args.json")}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(buf):
            code = m.main(["--iso", self.iso, "--out", self.out, "--ovmf-dir", self.fw, "--accel", "tcg",
                           "--interval", "0.1", "--timeout", str(timeout), "--serial-copy",
                           "--qemu", os.path.join(self.bin, "qemu-system-x86_64"), *extra])
        return code, buf.getvalue()

    def test_colours_alone_do_not_pass_the_installer_run(self):
        # Like the reviewer's fake ISO: a colourful GRUB picture, installer words, no kernel.
        code, out = self.run_installer("gui", timeout=2)
        self.assertEqual(code, 1, out)
        self.assertIn("timed out", out)

    def test_installer_run_passes_on_serial_evidence(self):
        for mode in ("installer", "installer-systemd"):
            code, out = self.run_installer(mode)
            self.assertEqual(code, 0, out)
            self.assertIn("PASS", out)
            self.assertRegex(out, r"Command line: BOOT_IMAGE=\S+ forge\.boottest=[0-9a-f]{32}")
        with open(os.path.join(self.out, "result.txt")) as f:
            self.assertIn("installer started", f.read())
        with open(os.path.join(self.tmp, "args.json")) as f:
            args = json.load(f)
        self.assertNotIn(self.iso, " ".join(args), "the test copy is booted, not the original")
        self.assertTrue(os.path.exists(os.path.join(self.out, "test-copy.txt")))

    def test_grub_text_and_wrong_marks_fail_the_installer_run(self):
        # Review finding: GRUB echo lines and menu titles passed with no kernel. Also a guessed mark,
        # and installer words printed before the kernel's command line.
        for mode in ("grub-echo", "grub-menu", "wrong-nonce", "installer-first"):
            code, out = self.run_installer(mode, timeout=2)
            self.assertEqual(code, 1, mode + "\n" + out)
            self.assertIn("timed out", out, mode)

    def test_every_run_has_a_new_mark(self):
        marks = []
        for _ in range(2):
            code, out = self.run_installer("installer")
            self.assertEqual(code, 0, out)
            marks.append(re.search(r"forge\.boottest=([0-9a-f]{32})", out).group(1))
        self.assertNotEqual(marks[0], marks[1])

    def test_installer_run_secure_boot(self):
        code, out = self.run_installer("installer", "--secure-boot")
        self.assertEqual(code, 0, out)

    def test_kernel_without_installer_fails(self):
        code, out = self.run_installer("kernel-only", timeout=2)
        self.assertEqual(code, 1, out)

    def test_guest_access_denied_does_not_fail_a_good_iso(self):
        code, out = self.run_installer("guest-denied")
        self.assertEqual(code, 0, out)

    def test_kernel_panic_fails_fast(self):
        code, out = self.run_installer("panic", timeout=30)
        self.assertEqual(code, 1, out)
        self.assertIn("kernel panic", out)

    def test_firmware_failure_still_fails_the_installer_run(self):
        code, out = self.run_installer("denied", timeout=30)
        self.assertEqual(code, 1, out)
        self.assertIn("Access Denied", out)

    def test_copy_problem_fails(self):
        code, out = self.run_installer("installer", copy_error="no kernel line in any grub.cfg")
        self.assertEqual(code, 1, out)
        self.assertIn("no kernel line", out)

    def private_dirs(self, tmpdir):
        return [n for n in os.listdir(tmpdir) if n.startswith("forge-boot-")]

    def start_and_signal(self, sig, mode, wait_for, *extra):
        """Start a run with TMPDIR in the test folder, send sig once wait_for exists, return the exit code."""
        tmpdir = tempfile.mkdtemp(prefix="tmpdir-", dir=self.tmp)   # a fresh one per run, even after a failed run
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], FAKE_QEMU_MODE=mode,
                   FAKE_QEMU_ARGS=os.path.join(self.tmp, "args.json"), TMPDIR=tmpdir)
        cmd = [sys.executable, SCRIPT, "--iso", self.iso, "--out", self.out, "--ovmf-dir", self.fw,
               "--accel", "tcg", "--interval", "0.1", "--timeout", "60", *extra]
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while not os.path.exists(wait_for) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(wait_for), "the run did not get that far")
        self.assertEqual(len(self.private_dirs(tmpdir)), 1, "the private folder exists while running")
        proc.send_signal(sig)
        code = proc.wait(timeout=120)   # generous: the full suite shares the CPU with other work
        return code, tmpdir

    def test_interrupt_during_the_copy_removes_the_private_folder(self):
        # Review finding: Ctrl+C (or a cancelled job) during the copy left the private folder,
        # gigabytes for a real ISO. A fake xorriso stands in for the slow copy.
        started = os.path.join(self.tmp, "xorriso-started")
        with open(os.path.join(self.bin, "xorriso"), "w") as f:
            f.write("#!/bin/sh\necho $$ > %s\nexec sleep 60\n" % started)
        os.chmod(os.path.join(self.bin, "xorriso"), 0o755)
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(sig=sig):
                if os.path.exists(started):
                    os.unlink(started)
                code, tmpdir = self.start_and_signal(sig, "gui", started, "--serial-copy")
                self.addCleanup(shutil.rmtree, tmpdir, True)
                self.assertNotEqual(code, 0)
                self.assertEqual(self.private_dirs(tmpdir), [], "private folder left behind")
                child = None
                deadline = time.monotonic() + 10      # the pid is written right after the file appears
                while child is None and time.monotonic() < deadline:
                    with open(started) as f:
                        text = f.read().strip()
                    if text:
                        child = int(text)
                    else:
                        time.sleep(0.05)
                self.assertIsNotNone(child, "the copy tool never wrote its pid")
                deadline = time.monotonic() + 10      # a loaded machine can take a moment to end it
                while time.monotonic() < deadline:
                    try:
                        os.kill(child, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.1)
                else:
                    self.fail("the copy tool is still running")

    def test_terminate_during_the_boot_removes_the_private_folder_and_stops_qemu(self):
        pidfile = os.path.join(self.tmp, "args.json.pid")
        code, tmpdir = self.start_and_signal(signal.SIGTERM, "black", pidfile)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.private_dirs(tmpdir), [])
        with open(pidfile) as f:
            qemu = int(f.read())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(qemu, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("QEMU still runs")

    def test_checksum(self):
        digest = hashlib.sha256(b"fake iso").hexdigest()
        good = os.path.join(self.tmp, "forge-os.iso-CHECKSUM")
        for text in (f"{digest}  forge-os.iso\n", f"SHA256 (forge-os.iso) = {digest}\n"):
            with open(good, "w") as f:
                f.write(text)
            r = self.run_check("gui", "--checksum", good)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(good, "w") as f:
            f.write("0" * 64 + "  forge-os.iso\n")
        r = self.run_check("gui", "--checksum", good)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("checksum", r.stdout)
        r = self.run_check("gui", "--checksum", os.path.join(self.tmp, "missing"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
