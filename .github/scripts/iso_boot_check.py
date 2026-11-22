#!/usr/bin/env python3
"""UEFI boot test for a Forge OS installer ISO.

Boots the ISO in QEMU with OVMF (UEFI firmware). Two kinds of run:

Firmware run (default), on the unmodified ISO:
  * Proves the firmware path: OVMF loads and starts the ISO's boot loader (with Secure Boot on,
    that means it accepted the signed shim). OVMF copies its console to the serial port, so
    "BdsDxe: starting Boot..." shows the hand-over, and "Access Denied", "No bootable option" or
    the UEFI shell fail the test right away.
  * Passes when the boot loader was started and a graphical screen (not mostly black, many
    colours) stays up for several checks in a row. This is the firmware stage only: colours can
    come from a GRUB picture, so it is not proof that the installer started.

Installer run (--serial-copy), on a test copy of the ISO:
  * The copy differs only in the grub.cfg files GRUB reads (EFI/BOOT/grub.cfg, boot/grub2/grub.cfg,
    boot/grub/grub.cfg, and EFI/BOOT/grub.cfg inside the UEFI boot image). Their kernel lines get a
    new random mark "forge.boottest=<32 hex>" and "console=tty0 console=ttyS0,115200n8", and lose
    "quiet", "rhgb" and "rd.live.check" (the media check would fail on a changed ISO). Every other
    file, including the kernel, initrd, shim and GRUB binaries (also those inside the UEFI boot
    image), is checked to be byte-identical (sha256) before booting. Needs xorriso and mtools.
  * grub.cfg is read the way GRUB 2.12 reads it (lib/getline.c, script/yylex.l): lines end only at
    "\n", every "\r" is dropped, quotes and "\" continuations can join lines. Anything this check
    can't follow safely is refused: control characters or ";", "|", "{" on a kernel line, a kernel
    line that continues, a quote or "\" in a comment, a NUL. So is a grub.cfg that already has the
    mark or the text the check looks for ("Linux version", "Command line:", installer lines), or
    that defines its own "linux" function.
  * Passes only on serial evidence, in this order:
      1. the kernel started: its own "Command line: ..." line (printed early, replayed to the serial
         console when it starts) with this run's mark. GRUB can't print that: it never sees the
         mark until the copy is made, and the mark is only on kernel lines;
      2. after that line, the installer started: anaconda's own banner "anaconda <version> for
         <product> started", or "Starting installer, one moment", or systemd's status line
         "Started anaconda.service" / "Started anaconda-direct.service" / "Reached target
         anaconda.target".
  * Fails fast on "Kernel panic - not syncing", "Entering emergency mode" or dracut's "Could not
    boot" after that line. Firmware failure lines count only before it, so a guest line like
    "Access Denied" can't fail a good ISO.

Both runs fail on timeout or when QEMU stops. The last screen is always saved as screen.png in
--out. Ctrl+C or SIGTERM stops QEMU and removes the private folder (with the test copy). Serial output is untrusted: it is printed without control characters and fenced with
::stop-commands:: so it can't run GitHub workflow commands.

Local use (Ubuntu: apt install qemu-system-x86 ovmf xorriso mtools):
  .github/scripts/iso_boot_check.py --iso forge-os.iso --out /tmp/boot-test [--secure-boot] [--serial-copy]
Exit codes: 0 pass, 1 fail, 2 bad arguments or missing files.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import zlib

OVMF_DIR = "/usr/share/OVMF"   # Ubuntu's "ovmf" package
NEEDED_HITS = 3                # installer screens in a row before we call it a pass
MIN_SIZE = (320, 200)          # smaller screens are never the installer
ANSI = re.compile(r"\x1b\[[0-9;=?]*[A-Za-z]")

# Lines OVMF prints (to screen and serial) when it can't start the ISO's boot loader.
FIRMWARE_FAILURES = {
    "Access Denied": "Secure Boot refused the ISO's boot loader (Access Denied)",
    "No bootable option or device was found": "the firmware found nothing to boot",
    "UEFI Interactive Shell": "the firmware fell back to the UEFI shell (nothing bootable)",
}


# Screenshots

def read_ppm(data):
    """Parse a binary PPM (P6, 8 bit), as QEMU's screendump writes it. Returns (w, h, rgb)."""
    fields, i = [], 0
    while len(fields) < 4:
        while i < len(data) and data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b"#":
            while i < len(data) and data[i:i + 1] != b"\n":
                i += 1
            continue
        j = i
        while j < len(data) and not data[j:j + 1].isspace():
            j += 1
        if j == i:
            raise ValueError("truncated PPM header")
        fields.append(data[i:j])
        i = j
    if fields[0] != b"P6":
        raise ValueError("not a binary PPM")
    try:
        w, h, maxval = (int(x) for x in fields[1:])
    except ValueError:
        raise ValueError("bad PPM header") from None
    if w <= 0 or h <= 0 or w > 16384 or h > 16384 or maxval != 255:
        raise ValueError("unsupported PPM size or depth")
    rgb = data[i + 1:i + 1 + w * h * 3]
    if len(rgb) != w * h * 3:
        raise ValueError("truncated PPM data")
    return w, h, rgb


def to_png(w, h, rgb):
    """Encode 8 bit RGB as a PNG (standard library only)."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    stride = w * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def screen_stats(w, h, rgb, step=4):
    """Share of (almost) black pixels and number of distinct colours, on every step-th pixel."""
    total = black = 0
    colours = set()
    for y in range(0, h, step):
        row = y * w * 3
        for x in range(0, w, step):
            o = row + x * 3
            r, g, b = rgb[o], rgb[o + 1], rgb[o + 2]
            total += 1
            if r < 16 and g < 16 and b < 16:
                black += 1
            colours.add((r >> 3, g >> 3, b >> 3))
    return {"width": w, "height": h, "black": black / max(total, 1), "colours": len(colours)}


def looks_like_installer(stats):
    """True for a graphical desktop-like screen, False for black, text or firmware screens.

    Measured on QEMU 8.2 + OVMF 2024.02: text and firmware screens have at most 42 colours
    (with the TianoCore logo) and are 98% black, or (firmware menu) 3 colours. A graphical
    screen had over 3000 colours and almost no black.
    """
    if stats["width"] < MIN_SIZE[0] or stats["height"] < MIN_SIZE[1]:
        return False
    return stats["black"] <= 0.7 and stats["colours"] >= 64


# Serial evidence. Everything before the kernel's first line is firmware and boot loader output.
# In the installer run, the kernel's first line is its command line with this run's random mark.
KERNEL_STARTED = re.compile(r"Linux version \d+\.\d+")
MARK = "forge.boottest"
MARK_VALUE = re.compile(r"^[0-9a-f]{32}$")
KERNEL_CMDLINE = re.compile(r"^(?:\[\s*\d+\.\d+\]\s*)?(?:Kernel command line|Command line): (.*)$")
INSTALLER_STARTED = (
    re.compile(r"\banaconda \d[\w.+~-]* for .{1,100} started"),     # anaconda's own banner
    re.compile(r"\bStarting installer, one moment"),                  # printed by anaconda
    re.compile(r"\b(?:Started|Reached target) anaconda(?:-direct)?\.(?:service|target)\b"),   # systemd status
)
GUEST_FAILURES = {
    "Kernel panic - not syncing": "the kernel stopped (kernel panic)",
    "Entering emergency mode": "the installer's early boot dropped to an emergency shell",
    "Could not boot.": "the installer's early boot (dracut) could not boot",
}
BOOT_LOADER_STARTED = "BdsDxe: starting Boot"


def kernel_cmdline_start(serial_text, mark):
    """Offset of the kernel's "Command line: ..." line that has forge.boottest=<mark>, or None."""
    want = re.compile(r"(?:^|\s)" + re.escape(f"{MARK}={mark}") + r"(?:\s|$)")
    pos = 0
    for piece in re.split(r"(\r|\n)", serial_text):
        if piece not in ("\r", "\n"):
            m = KERNEL_CMDLINE.match(ANSI.sub("", piece).strip())
            if m and want.search(m.group(1)):
                return pos
        pos += len(piece)
    return None


def split_at_kernel(serial_text, mark=None):
    """(firmware and boot loader part, guest part): split at the kernel's first line. With a mark
    (installer run), that is the kernel's command line with the mark, not "Linux version"."""
    if mark is not None:
        start = kernel_cmdline_start(serial_text, mark)
        if start is None:
            return serial_text, ""
        return serial_text[:start], serial_text[start:]
    m = KERNEL_STARTED.search(serial_text)
    if not m:
        return serial_text, ""
    start = serial_text.rfind("\n", 0, m.start()) + 1
    return serial_text[:start], serial_text[start:]


def firmware_failure(serial_text, mark=None):
    """A reason string if OVMF could not boot the ISO, else None. Only firmware-stage lines count."""
    firmware, _ = split_at_kernel(serial_text, mark)
    for needle, reason in FIRMWARE_FAILURES.items():
        if needle in firmware:
            return reason
    return None


def guest_failure(serial_text, mark=None):
    """A reason string if the booted kernel or early userspace failed, else None."""
    _, guest = split_at_kernel(serial_text, mark)
    for needle, reason in GUEST_FAILURES.items():
        if needle in guest:
            return reason
    return None


def boot_loader_started(serial_text):
    firmware, _ = split_at_kernel(serial_text)
    return BOOT_LOADER_STARTED in firmware


def installer_evidence(serial_text, mark):
    """(kernel command line, installer line) when the kernel printed its command line with this
    run's mark and the installer started after that, else None. GRUB text never counts."""
    _, guest = split_at_kernel(serial_text, mark)
    if not guest:
        return None
    lines = [ANSI.sub("", line).strip() for line in guest.replace("\r", "\n").split("\n")]
    for line in lines[1:]:
        if any(p.search(line) for p in INSTALLER_STARTED):
            return lines[0], line
    return None


# The test copy for the installer run

SERIAL_ARGS = ["console=tty0", "console=ttyS0,115200n8"]   # the last console= gets systemd and anaconda
DROP_ARG = re.compile(r"^(?:quiet|rhgb|rd\.live\.check|console=[\w.,:/=-]*)$")   # unquoted words only
KERNEL_CMD = re.compile(r"^([ \t]*)(linux|linuxefi)(?=[ \t]|$)")
MTOOLS_ENV = {"MTOOLS_SKIP_CHECK": "1"}
SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./+@~ -]+$")
REPORT_LBA = re.compile(r"^File data lba:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.*)'$")
# The grub.cfg files GRUB reads. On the ISO (Rock Ridge names, so exact case): UEFI, Fedora's BIOS
# GRUB and upstream GRUB's default prefix (also Ubuntu's signed CD GRUB, used for the test ISOs).
# Inside the UEFI boot image (FAT, any case): the one next to the boot loader.
ISO_GRUB_CFGS = ("/EFI/BOOT/grub.cfg", "/boot/grub2/grub.cfg", "/boot/grub/grub.cfg")
FAT_GRUB_CFG = "/efi/boot/grub.cfg"
# Text a grub.cfg must not have before it is changed: the mark, the lines this check looks for, and
# a "linux" function that could print its arguments. The random mark already stops GRUB text from
# counting; this refuses the plain attempts early, with a clear reason.
GRUB_CFG_REFUSED = (
    (re.compile(re.escape(MARK), re.I), "the boot-test mark (" + MARK + ")"),
    (re.compile(r"linux version", re.I), "the kernel's first words (Linux version)"),
    (re.compile(r"command line:", re.I), "the kernel's command line words (Command line:)"),
    (re.compile(r"(?m)^[ \t]*function[ \t]+(?:linux|linuxefi)\b"), "its own linux function"),
) + tuple((p, "the installer's start message") for p in INSTALLER_STARTED)


# GRUB commands that can print a file (the changed grub.cfg holds the mark), run another config,
# or build a command from pieces. A boot ISO's grub.cfg has no use for them. Matched after quotes
# and backslashes are removed, so split spellings like "c"at or c\at are caught too. A command
# taken from a variable ($x) is refused for the same reason.
GRUB_CMD_REFUSED = re.compile(
    r"\b(cat|hexdump|cmp|source|configfile|eval|loadenv|load_env|smbios|read_file|"
    r"extract_entries_\w+|extract_legacy_entries_\w+|extract_syslinux_entries_\w+|"
    r"syslinux_source|syslinux_configfile|normal|normal_exit)\b")
GRUB_VAR_COMMAND = re.compile(r"(?m)(?:^|[;{}&|]|\bthen\b|\bdo\b|\belse\b)[ \t]*\$")


class CopyError(Exception):
    pass


def grub_var_end(line, i):
    """Index after a "$" variable starting at line[i] (GRUB: $name, ${name}, $1, $?, $#, $*, $@)."""
    if line[i + 1:i + 2] == "{":
        m = re.match(r"\{[A-Za-z0-9_?#*@]+\}", line[i + 1:])
        if not m:
            raise CopyError(f"grub.cfg has a variable this check can't read: {printable(line[i:i + 20])!r}")
        return i + 1 + m.end()
    if line[i + 1:i + 2] in ("?", "#", "*", "@"):
        return i + 2
    return i + 1


def grub_scan(line, quote, continued):
    """Follow GRUB 2.12's lexer (script/yylex.l) over one line (no "\r", no "\n").
    Returns (quote still open: None, "'" or '"', ends with a "\" continuation, words, comment),
    where words are (start, end) of each unquoted-level word on the line and comment is the start
    of a "#" comment or None. Quoted text and escapes stay inside their word."""
    words, comment, i, n = [], None, 0, len(line)
    word = 0 if (quote or continued) else None
    while i < n:
        c = line[i]
        if quote == "'":
            if c == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if c == "\\":
                if i + 1 == n:
                    return quote, True, words, comment
                i += 2
                continue
            if c == '"':
                quote = None
            i = grub_var_end(line, i) if c == "$" else i + 1
            continue
        if c in " \t" or c in "{}|&;<>":
            if word is not None:
                words.append((word, i))
                word = None
            if c not in " \t":
                words.append((i, i + 1))
            i += 1
            continue
        if c == "#" and word is None:
            comment = line[i:]
            if any(q in comment for q in "'\"\\"):
                raise CopyError(f"grub.cfg has a quote or \\ in a comment: {printable(comment, 80)!r}")
            return None, False, words, i
        if word is None:
            word = i
        if c == "\\":
            if i + 1 == n:
                words.append((word, n))
                return None, True, words, comment
            i += 2
            continue
        if c in "'\"":
            quote = c
        elif c == "$" and line[i + 1:i + 2] == '"':
            quote = '"'
            i += 1
        elif c == "$":
            i = grub_var_end(line, i)
            continue
        i += 1
    if word is not None and quote is None:
        words.append((word, n))
    return quote, False, words, comment


def serial_grub_cfg(text, mark):
    """(new text, number of kernel lines changed). Only "linux"/"linuxefi" commands change: they get
    forge.boottest=<mark> right after the kernel, then their other words (quoting kept, "quiet",
    "rhgb", "rd.live.check" and console= dropped), then the serial console. Raises CopyError for
    a grub.cfg this check can't follow safely."""
    if not MARK_VALUE.match(mark):
        raise ValueError("the mark must be 32 hex digits")
    if "\0" in text:
        raise CopyError("grub.cfg has a NUL character (GRUB would cut that line short)")
    for pattern, what in GRUB_CFG_REFUSED:
        if pattern.search(text):
            raise CopyError(f"grub.cfg already contains {what}: refused before changing it")
    plain = re.sub(r"[\"'\\\\]", "", text)
    bad = GRUB_CMD_REFUSED.search(plain)
    if bad:
        raise CopyError(f"grub.cfg uses the GRUB command {bad.group(1)!r}, which could print the changed "
                        "grub.cfg or run other text: refused")
    if GRUB_VAR_COMMAND.search(plain):
        raise CopyError("grub.cfg runs a command taken from a variable: refused")
    out, changed = [], 0
    quote, continued = None, False
    for raw in text.split("\n"):            # GRUB ends lines only at "\n" (lib/getline.c) ...
        line = raw.replace("\r", "")        # ... and drops every "\r"
        fresh = quote is None and not continued
        quote, continued, words, comment = grub_scan(line, quote, continued)
        m = KERNEL_CMD.match(line) if fresh else None
        if not m:
            out.append(raw)
            continue
        body = raw[:-1] if raw.endswith("\r") else raw
        bad = [c for c in body if c != "\t" and unicodedata.category(c)[0] in "CZ" and c != " "]
        if bad:
            raise CopyError(f"a kernel line in grub.cfg has control characters: {printable(body, 200)!r}")
        if quote or continued:
            raise CopyError(f"a kernel line in grub.cfg goes on to the next line: {printable(body, 200)!r}")
        args = [body[a:b] for a, b in words[1:]]
        if not args:
            raise CopyError("a kernel line in grub.cfg names no kernel")
        if any(w in ("{", "}", "|", "&", ";", "<", ">") for w in args):
            raise CopyError(f"a kernel line in grub.cfg has more than one command: {printable(body, 200)!r}")
        kept = [w for w in args[1:] if not DROP_ARG.match(w)]
        new = " ".join([m.group(1) + m.group(2), args[0], f"{MARK}={mark}"] + kept + SERIAL_ARGS)
        if comment is not None:
            new += " " + body[comment:]
        out.append(new + raw[len(body):])
        changed += 1
    if quote or continued:
        raise CopyError("grub.cfg ends inside a quote or a \\ continuation")
    return "\n".join(out), changed


def run_tool(argv, binary=False, env=None):
    exe = shutil.which(argv[0])
    if not exe:
        raise CopyError(f"{argv[0]} not found (install xorriso and mtools)")
    r = subprocess.run([exe] + argv[1:], capture_output=True, env=dict(os.environ, **(env or {})), timeout=3600)
    if r.returncode != 0:
        raise CopyError(f"{argv[0]} failed: {printable(r.stderr.decode('utf-8', 'replace'), 300).strip()}")
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def iso_files(iso):
    """{path: [(start block, size), ...]} for every file in the ISO, read with xorriso.
    xorriso writes control characters in names as backslash codes ("\\n", "\\034"), so every file
    is on one line, and such names are refused: no file can be left out of the comparison."""
    files = {}
    out = run_tool(["xorriso", "-backslash_codes", "on", "-indev", iso,
                    "-find", "/", "-type", "f", "-exec", "report_lba", "--"])
    for line in out.split("\n"):
        if not line.strip() or line.startswith("Report layout:"):
            continue
        m = REPORT_LBA.match(line.strip())
        if not m:
            raise CopyError(f"could not read this line of the ISO's file list: {printable(line, 200)!r}")
        path = m.group(5)
        if "\\" in path:
            raise CopyError(f"a file name in the ISO has control characters (or a backslash): {printable(path, 200)!r}")
        if not SAFE_PATH.match(path) or "/../" in path:
            raise CopyError(f"unusual file name in the ISO: {printable(path, 200)!r}")
        files.setdefault(path, []).append((int(m.group(1)), int(m.group(2)), int(m.group(4))))
    if not files:
        raise CopyError("could not list the files in the ISO")
    return {p: [(lba, size) for _, lba, size in sorted(ext)] for p, ext in files.items()}


def read_extents(f, extents, chunk=1 << 20):
    for lba, size in extents:
        f.seek(lba * 2048)
        left = size
        while left > 0:
            data = f.read(min(chunk, left))
            if not data:
                raise CopyError("the ISO is shorter than its file table says")
            left -= len(data)
            yield data


def file_bytes(iso, extents):
    with open(iso, "rb") as f:
        return b"".join(read_extents(f, extents))


def files_sha256(iso, files):
    sums = {}
    with open(iso, "rb") as f:
        for path, extents in files.items():
            h = hashlib.sha256()
            for data in read_extents(f, extents):
                h.update(data)
            sums[path] = h.hexdigest()
    return sums


def volume_id(iso):
    for line in run_tool(["xorriso", "-indev", iso, "-pvd_info"]).splitlines():
        if line.startswith("Volume Id"):
            return line.split(":", 1)[1].strip()
    return ""


def efi_boot_images(iso):
    """Paths of the El Torito UEFI boot images (FAT images with the UEFI boot loader)."""
    out = run_tool(["xorriso", "-indev", iso, "-report_el_torito", "plain"])
    uefi = set(re.findall(r"^El Torito boot img :\s+(\d+)\s+UEFI\b", out, re.M))
    paths = re.findall(r"^El Torito img path :\s+(\d+)\s+(\S.*)$", out, re.M)
    return [p.strip() for n, p in paths if n in uefi]


# Bytes xorriso writes into an El Torito boot image file: its own position on the disc.
# boot-info-table: bytes 8 to 23 (disc, own block, length, checksum; 24 to 63 are reserved zeros).
SELF_POSITION = {"boot-info-table": (8, 24), "grub2-boot-info": (2548, 2556)}


def self_position_ranges(iso):
    """{boot image path: [(start, end)]} of the bytes xorriso writes into it (SELF_POSITION)."""
    out = run_tool(["xorriso", "-indev", iso, "-report_el_torito", "plain"])
    paths = dict(re.findall(r"^El Torito img path :\s+(\d+)\s+(\S.*?)\s*$", out, re.M))
    ranges = {}
    for n, opts in re.findall(r"^El Torito img opts :\s+(\d+)\s+(.*?)\s*$", out, re.M):
        found = [SELF_POSITION[o] for o in opts.split() if o in SELF_POSITION]
        if found and n in paths:
            ranges[paths[n]] = sorted(found)
    return ranges


def masked_sha256(data, ranges):
    data = bytearray(data)
    for start, end in ranges:
        data[start:end] = bytes(len(data[start:end]))
    return hashlib.sha256(data).hexdigest()


EFI_SYSTEM_GUID = "28732ac11ff8d211ba4b00a0c93ec93b"   # as xorriso prints the EFI System type GUID


def hidden_efi_images(iso):
    """UEFI boot images that are not files in the ISO but a partition appended after it, which is
    how lorax builds Fedora's boot.iso: {"appended partition N": [(block, size in bytes)]}.
    Each must start exactly where El Torito points and be an EFI System partition in the GPT."""
    out = run_tool(["xorriso", "-indev", iso, "-report_el_torito", "plain"])
    entries = re.findall(r"^El Torito boot img :\s+(\d+)\s+UEFI\s+\S+\s+\S+\s+\S+\s+\S+\s+\d+\s+(\d+)\s*$", out, re.M)
    with_path = set(re.findall(r"^El Torito img path :\s+(\d+)\s", out, re.M))
    hidden = [int(lba) for n, lba in entries if n not in with_path]
    if not hidden:
        return {}
    area = run_tool(["xorriso", "-indev", iso, "-report_system_area", "plain"])
    types = dict(re.findall(r"^GPT type GUID\s+:\s+(\d+)\s+([0-9a-f]{32})\s*$", area, re.M))
    parts = {int(start): (n, int(size)) for n, start, size
             in re.findall(r"^GPT start and size :\s+(\d+)\s+(\d+)\s+(\d+)\s*$", area, re.M)}
    images = {}
    for lba in hidden:
        n, size = parts.get(lba * 4, (None, 0))
        if n is None or types.get(n) != EFI_SYSTEM_GUID or not size:
            raise CopyError(f"El Torito points to a UEFI boot image at block {lba} that is neither a file "
                            "nor an appended EFI System partition")
        images[f"appended partition {n}"] = [(lba, size * 512)]
    return images


def fat_files(img):
    # -a: also files with the hidden or system flag, which firmware still loads.
    out = run_tool(["mdir", "-/", "-a", "-b", "-i", img, "::"], env=MTOOLS_ENV)
    files = []
    for line in out.split("\n"):
        line = line.strip()
        if not line or line.endswith("/"):
            continue
        if not line.startswith("::/") or not SAFE_PATH.match(line[2:]):
            raise CopyError(f"unusual file name in the UEFI boot image: {printable(line, 200)!r}")
        files.append(line[2:])
    if not any(f.lower().endswith(".efi") for f in files):
        raise CopyError("the UEFI boot image lists no .efi file")
    return files


def fat_read(img, path):
    return run_tool(["mcopy", "-n", "-i", img, "::" + path, "-"], binary=True, env=MTOOLS_ENV)


def fat_sha256(img):
    return {p: hashlib.sha256(fat_read(img, p)).hexdigest() for p in fat_files(img)}


def compare_isos(iso, copy, changed, work):
    """Raise CopyError unless every file outside `changed` is byte-identical. Returns sha256 sums.
    `changed` holds the changed grub.cfg files and the UEFI boot images, where only FAT_GRUB_CFG
    may differ inside."""
    old, new = iso_files(iso), iso_files(copy)
    if set(old) != set(new):
        raise CopyError("the test copy has different files: " + ", ".join(sorted(set(old) ^ set(new)))[:300])
    # Appended UEFI partitions are not files in the ISO, so their insides are always compared.
    old_hidden, new_hidden = hidden_efi_images(iso), hidden_efi_images(copy)
    if set(old_hidden) != set(new_hidden) or any(old_hidden[p][0][1] != new_hidden[p][0][1] for p in old_hidden):
        raise CopyError("the test copy has a different appended UEFI partition")
    old, new = {**old, **old_hidden}, {**new, **new_hidden}
    keep = {p: e for p, e in old.items() if p not in changed and p not in old_hidden}
    # Boot images may differ only in the bytes holding their own position (same options in both).
    masked = self_position_ranges(iso)
    if masked != self_position_ranges(copy):
        raise CopyError("the test copy has different El Torito boot image options")
    plain = {p: e for p, e in keep.items() if p not in masked}
    a, b = files_sha256(iso, plain), files_sha256(copy, {p: new[p] for p in plain})
    for p in (p for p in masked if p in keep):
        a[p] = masked_sha256(file_bytes(iso, old[p]), masked[p])
        b[p] = masked_sha256(file_bytes(copy, new[p]), masked[p])
    diff = sorted(p for p in keep if a[p] != b[p])
    if diff:
        raise CopyError("files changed in the test copy: " + ", ".join(diff)[:300])
    inner = {}
    for path in sorted((changed - set(ISO_GRUB_CFGS)) | set(old_hidden)):
        imgs = []
        for n, iso_path in enumerate((iso, copy)):
            img = os.path.join(work, f"compare-{n}.img")
            with open(img, "wb") as f:
                f.write(file_bytes(iso_path, (old if n == 0 else new)[path]))
            imgs.append(fat_sha256(img))
        if set(imgs[0]) != set(imgs[1]):
            raise CopyError(f"{path}: the test copy has different files inside")
        diff = sorted(p for p in imgs[0] if p.lower() != FAT_GRUB_CFG and imgs[0][p] != imgs[1][p])
        if diff:
            raise CopyError(f"{path}: files changed inside: " + ", ".join(diff))
        inner.update({f"{path}:{p}": h for p, h in imgs[0].items() if p.lower() != FAT_GRUB_CFG})
    return a, inner


def make_serial_copy(iso, dest, work, mark):
    """Write a copy of the ISO whose kernel lines print to the serial port and carry
    forge.boottest=<mark>. Returns report lines."""
    files = iso_files(iso)
    images = {p: files[p] for p in efi_boot_images(iso) if p in files}
    hidden = hidden_efi_images(iso)
    if not images and not hidden:
        raise CopyError("no UEFI boot image found in the ISO (El Torito)")
    maps, patches, changed, report, kernel_lines = [], [], set(), [], 0
    for n, path in enumerate(p for p in ISO_GRUB_CFGS if p in files):
        new, count = serial_grub_cfg(file_bytes(iso, files[path]).decode("utf-8", "surrogateescape"), mark)
        if count:
            local = os.path.join(work, f"grub-{n}.cfg")
            with open(local, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
                f.write(new)
            maps.append((local, path))
            changed.add(path)
            kernel_lines += count
            report.append(f"changed: {path} ({count} kernel lines)")
    for n, (path, extents) in enumerate(list(images.items()) + list(hidden.items())):
        img = os.path.join(work, f"efiboot-{n}.img")
        with open(img, "wb") as f:
            f.write(file_bytes(iso, extents))
        touched = False
        for inner in (p for p in fat_files(img) if p.lower() == FAT_GRUB_CFG):
            new, count = serial_grub_cfg(fat_read(img, inner).decode("utf-8", "surrogateescape"), mark)
            if count:
                local = os.path.join(work, f"efi-grub-{n}.cfg")
                with open(local, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
                    f.write(new)
                run_tool(["mcopy", "-o", "-i", img, local, "::" + inner], env=MTOOLS_ENV)
                touched = True
                kernel_lines += count
                report.append(f"changed: {path}:{inner} ({count} kernel lines)")
        if touched:
            (patches if path in hidden else maps).append((img, path))
            changed.add(path)
    if not kernel_lines:
        raise CopyError("no kernel line (linux or linuxefi) in any grub.cfg: nothing that boots a kernel")
    argv = ["xorriso", "-indev", iso, "-outdev", dest, "-boot_image", "any", "replay"]
    for local, path in maps:
        argv += ["-map", local, path]
    run_tool(argv)
    if patches:
        # xorriso copies appended partitions unchanged, so the edited image is written over the
        # copy's partition: same size, at the place the copy's own El Torito and GPT point to.
        placed = hidden_efi_images(dest)
        with open(dest, "r+b") as f:
            for img, path in patches:
                with open(img, "rb") as g:
                    data = g.read()
                if path not in placed or placed[path][0][1] != len(data):
                    raise CopyError(f"the test copy has no {path} of the same size")
                f.seek(placed[path][0][0] * 2048)
                f.write(data)
    sums, inner = compare_isos(iso, dest, changed, work)
    if volume_id(dest) != volume_id(iso):
        raise CopyError("the test copy has a different volume label")
    if efi_boot_images(dest) != efi_boot_images(iso) or set(hidden_efi_images(dest)) != set(hidden):
        raise CopyError("the test copy has a different UEFI boot image")
    report.append(f"byte-identical (sha256): {len(sums)} other files, {len(inner)} files inside the UEFI boot image")
    for path, ranges in sorted(self_position_ranges(iso).items()):
        spans = " and ".join(f"{start}-{end - 1}" for start, end in ranges)
        report.append(f"{path}: bytes {spans} not compared (its own position, written by xorriso)")
    boot = re.compile(r"(\.efi$|/vmlinuz|/initrd)", re.I)
    masked = self_position_ranges(iso)
    for path, digest in sorted(list(sums.items()) + list(inner.items())):
        if boot.search(path) and path not in masked:
            report.append(f"unchanged {digest}  {path}")
    return report


def iso_checksum_problem(iso, checksum_file):
    """None if the ISO's sha256 is in the CHECKSUM file, else the problem."""
    try:
        with open(checksum_file, encoding="utf-8", errors="replace") as f:
            listed = set(re.findall(r"\b[0-9a-fA-F]{64}\b", f.read(65536)))
    except OSError:
        return f"checksum file not found: {os.path.basename(checksum_file)}"
    if not listed:
        return "no sha256 in the checksum file"
    h = hashlib.sha256()
    with open(iso, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    if h.hexdigest() not in {x.lower() for x in listed}:
        return "the ISO does not match its checksum file (damaged download?)"
    return None


# QEMU

def firmware_files(secure_boot, ovmf_dir=OVMF_DIR):
    """OVMF code and variable-store template. Secure Boot: Microsoft keys enrolled, SB enabled."""
    if secure_boot:
        return os.path.join(ovmf_dir, "OVMF_CODE_4M.secboot.fd"), os.path.join(ovmf_dir, "OVMF_VARS_4M.ms.fd")
    return os.path.join(ovmf_dir, "OVMF_CODE_4M.fd"), os.path.join(ovmf_dir, "OVMF_VARS_4M.fd")


def qemu_argv(qemu, iso, code, nvram, serial, qmp, secure_boot, kvm, memory_mb, cpus):
    for p in (iso, code, nvram, serial, qmp):
        if "," in p:
            raise ValueError(f"commas are not allowed in paths: {p!r}")
    argv = [qemu, "-name", "forge-iso-boot-test",
            "-machine", f"q35,accel={'kvm' if kvm else 'tcg'}" + (",smm=on" if secure_boot else ""),
            "-cpu", "host" if kvm else "max", "-smp", str(cpus), "-m", str(memory_mb),
            "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
            "-drive", f"if=pflash,format=raw,unit=1,file={nvram}"]
    if secure_boot:
        argv += ["-global", "driver=cfi.pflash01,property=secure,value=on"]
    argv += ["-drive", f"file={iso},format=raw,media=cdrom,readonly=on,if=none,id=cd0",
             "-device", "ide-cd,drive=cd0,bootindex=0",
             "-vga", "std", "-display", "none", "-nic", "none", "-no-reboot",
             "-serial", f"file:{serial}", "-qmp", f"unix:{qmp},server=on,wait=off",
             "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny"]
    return argv


class Qmp:
    def __init__(self, path, proc, wait=30):
        deadline = time.monotonic() + wait
        while True:
            try:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(path)
                break
            except OSError:
                self.sock.close()
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        self.sock.settimeout(60)
        self.file = self.sock.makefile("rw")
        self.file.readline()   # greeting
        self.cmd("qmp_capabilities")

    def cmd(self, name, **arguments):
        msg = {"execute": name}
        if arguments:
            msg["arguments"] = arguments
        self.file.write(json.dumps(msg) + "\n")
        self.file.flush()
        while True:
            line = self.file.readline()
            if not line:
                raise OSError("QMP connection closed")
            reply = json.loads(line)
            if "return" in reply:
                return reply["return"]
            if "error" in reply:
                raise OSError(f"QMP {name}: {reply['error'].get('desc', 'error')}")
            # anything else is an event: skip it

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def read_text(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def printable(text, limit=1500):
    """Last part of an untrusted log, with control characters removed, for the CI log."""
    text = ANSI.sub("", text[-limit:]).replace("\r", "")
    return "".join(c if c == "\n" or c.isprintable() else " " for c in text)


def print_untrusted(title, text, limit=1500):
    """Print guest text between ::stop-commands:: lines with a random token, so lines like
    "::error::" or "::add-mask::" from the guest are shown but not run by GitHub Actions."""
    token = secrets.token_hex(16)
    print(title)
    print(f"::stop-commands::{token}")
    print(printable(text, limit))
    print(f"::{token}::", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iso", required=True)
    ap.add_argument("--out", required=True, help="folder for screen.png, serial.log and result.txt")
    ap.add_argument("--secure-boot", action="store_true", help="Secure Boot on, Microsoft keys enrolled")
    ap.add_argument("--serial-copy", action="store_true",
                    help="boot a test copy with a serial console and wait for the kernel and installer")
    ap.add_argument("--checksum", help="CHECKSUM file to check the ISO against before booting")
    ap.add_argument("--accel", choices=("auto", "kvm", "tcg"), default="auto")
    ap.add_argument("--timeout", type=float, help="seconds (default 1200 with KVM, 3600 without)")
    ap.add_argument("--interval", type=float, default=10.0, help="seconds between screenshots")
    ap.add_argument("--memory", type=int, default=4096, help="MB of guest RAM")
    ap.add_argument("--cpus", type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument("--ovmf-dir", default=OVMF_DIR)
    ap.add_argument("--qemu", default="qemu-system-x86_64")
    a = ap.parse_args(argv)

    def usage(msg):
        print(f"ERROR: {msg}")
        return 2

    if not os.path.isfile(a.iso):
        return usage(f"ISO not found: {a.iso}")
    if not (512 <= a.memory <= 65536 and 1 <= a.cpus <= 64 and 0 < a.interval <= 600):
        return usage("memory, cpus or interval out of range")
    code, vars_template = firmware_files(a.secure_boot, a.ovmf_dir)
    for p in (code, vars_template):
        if not os.path.isfile(p):
            return usage(f"OVMF file not found: {p} (install the ovmf package)")
    qemu = shutil.which(a.qemu)
    if not qemu:
        return usage(f"{a.qemu} not found (install qemu-system-x86)")
    kvm = a.accel == "kvm" or (a.accel == "auto" and os.access("/dev/kvm", os.R_OK | os.W_OK))
    timeout = a.timeout if a.timeout else (1200 if kvm else 3600)

    os.makedirs(a.out, exist_ok=True)
    out = os.path.abspath(a.out)
    serial = os.path.join(out, "serial.log")
    shot_png = os.path.join(out, "screen.png")
    kind = "installer run (test copy, serial console)" if a.serial_copy else "firmware run (unmodified ISO)"
    label = f"{kind}, Secure Boot {'on' if a.secure_boot else 'off'}, {'KVM' if kvm else 'no KVM (TCG)'}"
    start = time.monotonic()

    def finish(ok, text, shots=0):
        result = (f"{'PASS' if ok else 'FAIL'}: {text}\n"
                  f"{label}, {shots} screenshots in {int(time.monotonic() - start)} s\n")
        with open(os.path.join(out, "result.txt"), "w") as f:
            f.write(result)
        print(result, end="", flush=True)
        return 0 if ok else 1

    if a.checksum:
        problem = iso_checksum_problem(a.iso, a.checksum)
        if problem:
            return finish(False, problem)
        print("The ISO matches its checksum file (sha256).")
    # Ctrl+C raises KeyboardInterrupt, and SIGTERM (a cancelled job, a stopped container) does the
    # same here, so the finally below always stops QEMU and removes the private folder (gigabytes
    # with the test copy). Both signals are held back while the folder is made, so it can't be left
    # behind between being made and being known.
    def on_sigterm(signum, frame):
        raise KeyboardInterrupt(f"stopped by signal {signum}")

    old_sigterm = signal.signal(signal.SIGTERM, on_sigterm)
    private, proc, log = None, None, None
    try:
        held = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            private = tempfile.mkdtemp(prefix="forge-boot-")   # QMP socket, screendumps, NVRAM copy, test copy
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, held)
        boot_iso = os.path.abspath(a.iso)
        mark = None
        if a.serial_copy:
            mark = secrets.token_hex(16)   # new for every run: GRUB text can't know it
            boot_iso = os.path.join(private, "serial-test-copy.iso")
            print("Making the test copy with a serial console (only grub.cfg changes)...", flush=True)
            try:
                report = make_serial_copy(os.path.abspath(a.iso), boot_iso, private, mark)
            except (CopyError, OSError, subprocess.TimeoutExpired) as e:
                return finish(False, f"could not make the serial test copy: {e}")
            with open(os.path.join(out, "test-copy.txt"), "w") as f:
                f.write("\n".join(report) + "\n")
            print_untrusted("Test copy (file names come from the ISO):", "\n".join(report), limit=20000)
        nvram = os.path.join(private, "OVMF_VARS.fd")
        shutil.copyfile(vars_template, nvram)   # never write to the system template
        sock = os.path.join(private, "qmp.sock")
        dump = os.path.join(private, "screen.ppm")
        try:
            cmd = qemu_argv(qemu, boot_iso, code, nvram, serial, sock, a.secure_boot, kvm, a.memory, a.cpus)
        except ValueError as e:
            return usage(str(e))

        print(f"Booting {os.path.basename(a.iso)}: {label}, timeout {int(timeout)} s")
        log = open(os.path.join(out, "qemu.log"), "wb")
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        qmp, hits, shots, verdict, stats, evidence = None, 0, 0, None, None, None
        try:
            try:
                qmp = Qmp(sock, proc)
            except OSError:
                verdict = f"QEMU stopped (exit code {proc.poll()}) before it could be controlled"
            while verdict is None:
                time.sleep(a.interval)
                if proc.poll() is not None:
                    verdict = f"QEMU stopped (exit code {proc.returncode}) before the installer appeared"
                    break
                try:
                    qmp.cmd("screendump", filename=dump)
                    with open(dump, "rb") as f:
                        w, h, rgb = read_ppm(f.read())
                except (OSError, ValueError) as e:
                    if proc.poll() is not None:
                        continue   # reported on the next pass
                    verdict = f"could not take a screenshot: {e}"
                    break
                shots += 1
                with open(shot_png, "wb") as f:
                    f.write(to_png(w, h, rgb))
                stats = screen_stats(w, h, rgb)
                hits = hits + 1 if looks_like_installer(stats) else 0
                elapsed = time.monotonic() - start
                text = read_text(serial)
                if a.serial_copy:
                    evidence = installer_evidence(text, mark)
                    kernel = bool(split_at_kernel(text, mark)[1])
                    print(f"[{elapsed:6.0f} s] screen {w}x{h}, serial: kernel command line with the mark "
                          f"{'yes' if kernel else 'no'}, installer {'yes' if evidence else 'no'}", flush=True)
                    if evidence:
                        verdict = "PASS"
                        break
                else:
                    started = boot_loader_started(text)
                    print(f"[{elapsed:6.0f} s] screen {w}x{h}, black {stats['black']:.0%}, "
                          f"colours {stats['colours']}, graphical {hits}/{NEEDED_HITS}, "
                          f"boot loader started {'yes' if started else 'no'}", flush=True)
                    if hits >= NEEDED_HITS and started:
                        verdict = "PASS"
                        break
                reason = firmware_failure(text, mark)
                guest = guest_failure(text, mark) if a.serial_copy else None
                if reason:
                    verdict = f"firmware: {reason}"
                elif guest:
                    verdict = f"after the kernel started: {guest}"
                elif elapsed > timeout:
                    verdict = (f"timed out after {int(elapsed)} s without serial proof that the kernel and the installer started"
                               if a.serial_copy else
                               f"timed out after {int(elapsed)} s (boot loader started: {'yes' if started else 'no'}, "
                               f"graphical screen {hits}/{NEEDED_HITS})")
        finally:
            if qmp:
                try:
                    qmp.cmd("quit")
                except OSError:
                    pass
                qmp.close()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        ok = verdict == "PASS"
        if not ok:
            text = verdict
        elif a.serial_copy:
            text = "the kernel booted and the installer started (seen on the serial console)"
        else:
            text = ("the firmware started the ISO's boot loader and a graphical screen stayed up "
                    "(firmware stage only: the --serial-copy run checks the kernel and the installer)")
        result = finish(ok, text, shots)
        if evidence:
            print_untrusted("Serial evidence (kernel command line, installer line):", "\n".join(evidence))
        if not ok:
            print_untrusted("Last serial output:", read_text(serial))
        if not os.path.exists(shot_png):
            print("No screenshot was taken.")
        return result
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)
        if proc is not None and proc.poll() is None:   # stopped by a signal before QEMU was asked to quit
            proc.kill()
            proc.wait()
        if log:
            log.close()
        if private:
            shutil.rmtree(private, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Stopped (Ctrl+C or SIGTERM): QEMU was stopped and the private folder removed.", flush=True)
        sys.exit(130)
