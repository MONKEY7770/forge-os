"""Forge OS doctor: a read-only report on how well this PC and Forge OS fit together.

Used by the forge-doctor command and by Forge Core (GET /api/doctor). It only reads
files and asks a few system tools; it never changes anything and needs no root.
Every section is ok, warn, info or unknown ("not sure"), never a guess.

The report also lists fixes (see FIXES): metadata only. Only forge-doctor --fix runs
them, after asking, through the fixed root helper /usr/libexec/forge-doctor-fix.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

from forgecore import storage as S

# Rough rule for quantized (Q4) models fully on the GPU: about 0.6 GB per billion
# parameters, plus about 1.5 GB for the context. An estimate, not a promise.
COMMON_SIZES_B = (1, 3, 4, 7, 8, 12, 14, 24, 27, 32, 70)


def _read(root, p):
    try:
        with open(root + p) as f:
            return f.read()
    except OSError:
        return None


def _cmd(run, *cmd, timeout=20):
    """(returncode, stdout) or None if the tool is missing or did not answer."""
    try:
        r = run(list(cmd), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.returncode, r.stdout or ""


def _section(name, items, warn=(), unknown=False, info=False):
    status = "warn" if warn else "unknown" if unknown else "info" if info else "ok"
    return {"name": name, "status": status, "items": list(warn) + list(items)}


def image(run):
    out = _cmd(run, "rpm-ostree", "status", "--json")
    if not out or out[0] != 0:
        return _section("Image", ["Not sure which image is running: rpm-ostree did not answer."], unknown=True)
    try:
        booted = [d for d in json.loads(out[1]).get("deployments", []) if d.get("booted")][0]
    except (ValueError, IndexError, AttributeError):
        return _section("Image", ["Not sure which image is running: could not read rpm-ostree's answer."],
                        unknown=True)
    ref = booted.get("container-image-reference") or "(not a container image)"
    items = [f"Running: {ref}", f"Version: {booted.get('version', '?')}"]
    if ref.startswith("ostree-image-signed:"):
        return _section("Image", items + ["Updates are checked against the image signature."])
    return _section("Image", items, warn=[
        "Updates are NOT checked against a signature. Switch to the signed image: "
        "rpm-ostree rebase ostree-image-signed:" + ref.split(":", 1)[-1]])


def hardware(hw):
    if hw is None:
        return _section("Hardware", ["Not sure: no hardware profile yet (forge-hwcheck runs at boot)."],
                        unknown=True)
    items = [f"Type: {hw.get('type', '?')}", f"CPU: {hw.get('cpu', '?')}", f"RAM: {hw.get('ram_gb', '?')} GB",
             "Graphics: " + (", ".join(hw.get("gpus") or []) or "none found"),
             "Disks: " + (", ".join(hw.get("disks") or []) or "none found")]
    det = hw.get("details") or {}
    net = det.get("network") or []
    items.append("Network: " + (", ".join(f"{n['name']} ({n['kind']})" for n in net) or "none found"))
    items.append("Bluetooth: " + ("yes" if det.get("bluetooth") else "none found"))
    return _section("Hardware", items, warn=hw.get("checks") or [])


def ai_readiness(hw, run):
    det = (hw or {}).get("details") or {}
    gpus = det.get("gpus") or []
    items, warn = [], []
    best = max((g.get("vram_mb") or 0 for g in gpus), default=0)
    for g in gpus:
        vram = f"{round(g['vram_mb'] / 1024)} GB VRAM" if g.get("vram_mb") else "VRAM unknown"
        items.append(f"{g.get('vendor', '?')} GPU, driver {g.get('driver') or 'none'}, {vram}")
        if g.get("vendor") == "NVIDIA" and g.get("driver") != "nvidia":
            warn.append("The NVIDIA GPU is not using the NVIDIA driver, so AI models run on the CPU.")
            warn.append("How to fix: make sure you run the NVIDIA image (forge-os), then restart. If Secure Boot "
                        "is on, the driver only loads once the Secure Boot key is enrolled (the MOK step, see "
                        "Security).")
    if best:
        fit = (best / 1024 - 1.5) / 0.6
        sizes = [s for s in COMMON_SIZES_B if s <= fit]
        items.append(f"Rough estimate: models up to about {sizes[-1]}B parameters (Q4) fit on the GPU."
                     if sizes else "The GPU memory is small: models mostly run on the CPU.")
    elif hw is not None:
        items.append("No GPU memory found: models run on the CPU (small models, up to about 3B, work best).")
    if det.get("cpu_ai"):
        items.append("CPU AI features: " + ", ".join(det["cpu_ai"]))
    if det.get("npu"):
        items.append("An NPU (AI accelerator) is present. Ollama does not use it yet.")
    state = _cmd(run, "systemctl", "is-active", "ollama.service")
    items.append("Ollama: " + (state[1].strip() if state else "not sure"))
    if hw is None:
        return _section("AI readiness", items + ["Not sure: no hardware profile yet."], unknown=True)
    return _section("AI readiness", items, warn=warn)


def _encryption(root):
    """True (crypttab has an entry), False (no crypttab, or only comments), None (can't read it)."""
    tab = _read(root, "/etc/crypttab")
    # Lines as systemd reads them: split at \n only, comments may start after spaces.
    if tab is not None and any(line.strip() and not line.strip().startswith("#") for line in tab.split("\n")):
        return True
    if tab is None and os.path.lexists(root + "/etc/crypttab"):
        return None
    return False


def security(root, run):
    items, warn, unknown = [], [], False
    sb = _cmd(run, "mokutil", "--sb-state")
    if sb and "enabled" in sb[1].lower():
        items.append("Secure Boot: on")
    elif sb and "disabled" in sb[1].lower():
        warn.append("Secure Boot is off. Turning it on protects the boot chain (needs the MOK key enrolled).")
        warn.append("How to fix: enroll the key first (MOK step): run ujust enroll-secure-boot-key, restart, "
                    "choose Enroll MOK on the blue screen and type the password it showed you. Then turn on "
                    "Secure Boot in the BIOS. Not sure the command name is the same on every Bazzite version.")
    else:
        unknown = True
        items.append("Secure Boot: not sure (mokutil did not answer)")
    enc = _encryption(root)
    if enc is True:
        items.append("Disk encryption: on")
    elif enc is None:
        unknown = True
        items.append("Disk encryption: not sure (/etc/crypttab can't be read as this user; "
                     "sudo forge-doctor shows it)")
    else:
        warn.append("The disk is not encrypted. Anyone with the PC can read your files.")
        warn.append("How to fix: encryption can only be turned on while installing. Back up your files, then "
                    "reinstall with encryption on.")
    fw = _cmd(run, "systemctl", "is-active", "firewalld.service")
    if fw and fw[1].strip() == "active":
        items.append("Firewall: on")
    elif fw:
        warn.append("The firewall is not running.")
        warn.append("How to fix: sudo systemctl enable --now firewalld.service")
    else:
        unknown = True
        items.append("Firewall: not sure")
    return _section("Security", items, warn=warn, unknown=unknown)


def virtualization(root):
    items, warn = [], []
    flags = ""
    for line in (_read(root, "/proc/cpuinfo") or "").splitlines():
        if line.startswith("flags"):
            flags = line
            break
    has_virt = " vmx" in flags or " svm" in flags
    items.append("CPU virtualization: " + ("yes" if has_virt else "not found (check the BIOS setting)"))
    if os.path.exists(root + "/dev/kvm"):
        items.append("KVM: ready (virt-manager and GNOME Boxes can run fast VMs)")
    else:
        warn.append("KVM is not available: VMs will be very slow. Turn on virtualization (VT-x / SVM) in the BIOS.")
    groups = root + "/sys/kernel/iommu_groups"
    if os.path.isdir(groups) and os.listdir(groups):
        items.append("IOMMU: on (GPU passthrough is possible)")
        items += passthrough(root)
    else:
        items.append("IOMMU: off (only needed for GPU passthrough)")
    return _section("Virtualization", items, warn=warn)


VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}
PCI_ADDR = re.compile(r"^[0-9a-f]{4,8}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")   # domains can be 5 digits (VMD)


def passthrough(root):
    """Which GPUs could be given to a VM. Information only: passthrough is optional.

    A GPU can be passed through when nothing else shares its IOMMU group, apart from the
    card's own functions (such as its HDMI audio) and PCI bridges. Bridges are left out
    because that is the usual guidance; not sure it holds on every board.
    """
    base = root + "/sys/bus/pci/devices"
    try:
        addrs = sorted(a for a in os.listdir(base) if PCI_ADDR.match(a))
    except OSError:
        return []

    def cls(a):
        return (_read(root, f"/sys/bus/pci/devices/{a}/class") or "").strip()

    gpus = [a for a in addrs if cls(a).startswith("0x03")]
    out = []
    for gpu in gpus:
        name = VENDORS.get((_read(root, f"/sys/bus/pci/devices/{gpu}/vendor") or "").strip(), "") + " GPU"
        try:
            group = os.path.basename(os.readlink(f"{base}/{gpu}/iommu_group"))
            others = [a for a in os.listdir(f"{groups_dir(root)}/{group}/devices")
                      if PCI_ADDR.match(a) and a[:-2] != gpu[:-2] and not cls(a).startswith("0x0604")]
        except OSError:
            out.append(f"{name.strip()} ({gpu}): not sure about its IOMMU group")
            continue
        if others:
            n = len(others)
            out.append(f"{name.strip()} ({gpu}): shares its IOMMU group with {n} other device{'s' if n > 1 else ''}, "
                       "so it can't be passed to a VM on its own")
        else:
            out.append(f"{name.strip()} ({gpu}): in its own IOMMU group, ready for passthrough")
    if len(gpus) == 1:
        out.append("Only one graphics device: giving it to a VM leaves this PC without a screen while the VM runs")
    return out


def groups_dir(root):
    return root + "/sys/kernel/iommu_groups"


def services(run):
    items = []
    unknown = False
    for unit in ("forge-core.service", "ollama.service", "tailscaled.service", "firewalld.service"):
        state = _cmd(run, "systemctl", "is-active", unit)
        if state is None:
            unknown = True
        items.append(f"{unit}: {state[1].strip() if state else 'not sure'}")
    return _section("Services", items, unknown=unknown, info=not unknown)


def storage(root, run=subprocess.run):
    items, warn = [], []
    for mount in ("/", "/var"):
        try:
            u = shutil.disk_usage(root + mount if root else mount)
        except OSError:
            continue
        free_pct = round(u.free * 100 / u.total) if u.total else 0
        line = f"{mount}: {round(u.free / 1e9)} GB free of {round(u.total / 1e9)} GB ({free_pct}%)"
        (warn if free_pct < 10 else items).append(("Low space. " if free_pct < 10 else "") + line)
        if free_pct < 10:
            warn.append("How to fix: delete big files you don't need, empty the trash, or remove AI models "
                        "you no longer use (ollama rm NAME).")
    # AI model storage limit (Forge Settings, AI and local models). Never fails the report.
    try:
        ai = S.current(root)
    except Exception:   # noqa: BLE001 (the report must always be built)
        ai = None
    if ai is None:
        items.append("AI model storage: not sure (the model folder or the settings could not be read)")
    else:
        g = S.gb_int
        line = (f"AI models: {g(ai['models'])} GB used, {g(max(0, ai['room']))} GB left of {g(ai['budget'])} GB "
                f"(keeps {g(ai['reserve'])} GB for games, apps and files and {g(ai['min_free'])} GB free for "
                f"updates; limit: {ai['limit']})")
        problems = S.problem_lines(ai)
        if problems and ai["limit"] != "off":
            warn.extend(problems)
            warn.append("How to fix: delete AI models you no longer use (ollama rm NAME), or change the limit "
                        "in Forge Settings, AI and local models.")
        items.append(line)
    # Shared drive with Windows: one line, from the tool's own read-only status.
    out = _cmd(run, "forge-shared-drive", "status", "--json")
    try:
        sd = json.loads(out[1]) if out and out[0] == 0 else None
    except ValueError:
        sd = None
    if isinstance(sd, dict) and sd.get("state") != "off":
        msg = "".join(c for c in str(sd.get("message", "")) if c.isprintable())[:300]
        if sd.get("state") == "mounted":
            items.append(f"Shared drive {sd.get('label')}: {sd.get('fstype')}, open at {sd.get('mountpoint')}")
        elif sd.get("state") in ("not_found", "not_mounted"):
            items.append(f"Shared drive: {msg}")
        else:
            warn.append(f"Shared drive: {msg}")
    return _section("Storage", items, warn=warn)


def battery(hw):
    bats = ((hw or {}).get("details") or {}).get("battery") or []
    if not bats:
        return None
    items, warn = [], []
    for b in bats:
        health = b.get("health_pct")
        line = (f"{b.get('name')}: {b.get('charge_pct', '?')}% charged, health "
                f"{str(health) + '%' if health is not None else 'unknown'}, {b.get('cycles') or '?'} cycles")
        (warn if health is not None and health < 70 else items).append(
            ("Worn battery (holds under 70% of its design charge). " if health is not None and health < 70
             else "") + line)
    return _section("Battery", items, warn=warn)


# Guided fixes. Everything here is a constant: the report only decides WHICH ids apply,
# and nothing read from the system (names from /sys, rpm-ostree output, settings values)
# ever becomes part of a command. "automatic" fixes run as root through
# `pkexec /usr/libexec/forge-doctor-fix ID`, whose own table must match these commands.
HELPER = "/usr/libexec/forge-doctor-fix"
IMAGE_NVIDIA = "ostree-image-signed:docker://ghcr.io/monkey7770/forge-os:latest"
IMAGE_UNIVERSAL = "ostree-image-signed:docker://ghcr.io/monkey7770/forge-os-universal:latest"
FIXES = {
    "restart-ollama": {
        "title": "Start Ollama again",
        "problem": "Ollama (local AI models) is on in Forge Settings, but its service is not running.",
        "explanation": "Restarts the Ollama service. Your models are not touched.",
        "commands": [["systemctl", "restart", "ollama.service"]],
        "needs_root": True, "reversible": True, "risk": "low", "automatic": True},
    "restart-forge-core": {
        "title": "Start Forge Core again",
        "problem": "The Forge Core API is on in Forge Settings, but its service is not running.",
        "explanation": "Restarts the Forge Core service (the local API, metrics and agents). Nothing is deleted.",
        "commands": [["systemctl", "restart", "forge-core.service"]],
        "needs_root": True, "reversible": True, "risk": "low", "automatic": True},
    "usb-unlock-off": {
        "title": "Turn off USB key unlock",
        "problem": "USB key unlock is on, but this system has no encrypted disk, so it does nothing.",
        "explanation": "Sets security.usb_unlock to off with forge-config, the same as switching it off in "
                       "Forge Settings. Undo it with: forge-config undo",
        "commands": [["forge-config", "set", "security.usb_unlock", "off"]],
        "needs_root": True, "reversible": True, "risk": "low", "automatic": True},
    "run-hwcheck": {
        "title": "Record the hardware profile",
        "problem": "There is no hardware profile yet, so parts of this report are not sure.",
        "explanation": "Runs the hardware check (forge-hwcheck) that normally runs at boot. It only records "
                       "what hardware this PC has.",
        "commands": [["systemctl", "start", "forge-hwcheck.service"]],
        "needs_root": True, "reversible": True, "risk": "low", "automatic": True},
    "start-forge-virt": {
        "title": "Finish the virtual machine setup",
        "problem": "Virtual machines are on in Forge Settings, but libvirtd is not set up (or the last "
                   "setup failed).",
        "explanation": "Starts the Forge VM setup again (forge-virt), as saving the setting does. It runs in the "
                       "background and may download virt-manager, so it can take a few minutes. Turning the "
                       "setting off undoes it.",
        "commands": [["systemctl", "--no-block", "start", "forge-virt.service"]],
        "needs_root": True, "reversible": True, "risk": "low", "automatic": True},
    "switch-to-nvidia-image": {
        "title": "Switch to the NVIDIA image",
        "problem": "This PC has an NVIDIA GPU, but runs the universal image, so the NVIDIA driver is missing.",
        "explanation": "Switching images downloads a few GB and needs a restart. The old image stays in the "
                       "boot menu, so you can go back. Forge doctor does not run this for you: copy the command "
                       "into a terminal when you are ready.",
        "commands": [["rpm-ostree", "rebase", IMAGE_NVIDIA]],
        "needs_root": True, "reversible": True, "risk": "medium", "automatic": False},
    "switch-to-universal-image": {
        "title": "Switch to the universal image",
        "problem": "This PC has no NVIDIA GPU, but runs the NVIDIA image. The universal image fits better.",
        "explanation": "Switching images downloads a few GB and needs a restart. The old image stays in the "
                       "boot menu, so you can go back. Forge doctor does not run this for you: copy the command "
                       "into a terminal when you are ready.",
        "commands": [["rpm-ostree", "rebase", IMAGE_UNIVERSAL]],
        "needs_root": True, "reversible": True, "risk": "medium", "automatic": False},
}
# Our two images, as the end of the booted image reference (signed or not, any tag).
FORGE_IMAGE = re.compile(r"(?:^|[:/])ghcr\.io/monkey7770/forge-os(-universal)?"
                         r"(?::[A-Za-z0-9_.-]{1,128}|@sha256:[0-9a-f]{64})?$")


def _toggles(root):
    """The current value of every toggle setting, or None if not sure (schema or settings file unreadable)."""
    try:
        schema = json.loads(_read(root, "/usr/share/forge/settings.json") or "")
        out = {s["key"]: s["default"] for s in schema["settings"] if s.get("type") == "toggle"}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    text = _read(root, "/etc/forge/settings.json")
    if text is None:
        if os.path.lexists(root + "/etc/forge/settings.json"):
            return None
        return out
    try:
        changed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(changed, dict):
        return None
    for k in out:
        if k in changed:
            out[k] = changed[k]
    return out


def _state(run, verb, unit):
    out = _cmd(run, "systemctl", verb, unit)
    return out[1].strip() if out else None


def _image_kind(run):
    """"nvidia", "universal", or None if not sure (or not one of our images)."""
    out = _cmd(run, "rpm-ostree", "status", "--json")
    if not out or out[0] != 0:
        return None
    try:
        booted = [d for d in json.loads(out[1]).get("deployments", []) if d.get("booted")][0]
        ref = booted.get("container-image-reference")
    except (ValueError, IndexError, AttributeError):
        return None
    m = FORGE_IMAGE.search(ref) if isinstance(ref, str) else None
    if not m:
        return None
    return "universal" if m.group(1) else "nvidia"


def find_fixes(root, run, hw):
    """Which fixes apply. Each answer is a constant entry from FIXES, never text from the system."""
    ids = []
    on = _toggles(root) or {}
    # Gaming mode's "Wait" choice stops Ollama on purpose while a game runs.
    paused = os.path.lexists(root + "/run/forge/gaming/ollama-paused")
    if (on.get("ai.ollama") is True and not paused
            and _state(run, "is-active", "ollama.service") in ("inactive", "failed")):
        ids.append("restart-ollama")
    if on.get("core.enabled") is True and _state(run, "is-active", "forge-core.service") in ("inactive", "failed"):
        ids.append("restart-forge-core")
    if on.get("security.usb_unlock") is True and _encryption(root) is False:
        ids.append("usb-unlock-off")
    if hw is None:
        ids.append("run-hwcheck")
    else:
        gpus = ((hw.get("details") or {}).get("gpus") or []) if isinstance(hw, dict) else []
        vendors = [g.get("vendor") for g in gpus if isinstance(g, dict)]
        kind = _image_kind(run) if vendors else None
        if kind == "universal" and "NVIDIA" in vendors:
            ids.append("switch-to-nvidia-image")
        elif kind == "nvidia" and "NVIDIA" not in vendors:
            ids.append("switch-to-universal-image")
    # libvirtd can be socket-activated and idle, so only "not enabled" or a failed setup counts.
    if on.get("virt.enabled") is True and (_state(run, "is-enabled", "libvirtd.service") == "disabled"
                                           or _state(run, "is-active", "forge-virt.service") == "failed"):
        ids.append("start-forge-virt")
    return [fix_entry(i) for i in ids]


def fix_entry(fix_id):
    f = FIXES[fix_id]
    return {"id": fix_id, "title": f["title"], "problem": f["problem"], "explanation": f["explanation"],
            "commands": [list(c) for c in f["commands"]], "needs_root": f["needs_root"],
            "reversible": f["reversible"], "risk": f["risk"], "automatic": f["automatic"],
            "run_with": ["pkexec", HELPER, fix_id] if f["automatic"] else None}


def build_report(root: str = "", run=subprocess.run) -> dict:
    hw_text = _read(root, "/var/lib/forge-hw/hardware.json")
    try:
        hw = json.loads(hw_text) if hw_text else None
    except ValueError:
        hw = None
    sections = [image(run), hardware(hw), ai_readiness(hw, run), security(root, run),
                virtualization(root), services(run), storage(root, run)]
    bat = battery(hw)
    if bat:
        sections.append(bat)
    summary = {k: sum(1 for s in sections if s["status"] == k) for k in ("ok", "warn", "info", "unknown")}
    fixes = find_fixes(root, run, hw)
    summary["fixable"] = len(fixes)   # problems found by the fix checks also count
    return {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "summary": summary, "sections": sections,
            "fixes": fixes}


MARK = {"ok": "OK  ", "warn": "WARN", "info": "INFO", "unknown": "??  "}


def to_text(report: dict) -> str:
    s = report["summary"]
    lines = [f"Forge OS doctor, {report['created']}",
             f"{s['ok']} ok, {s['warn']} to look at, {s['unknown']} not sure, "
             f"{s.get('fixable', 0)} with a guided fix", ""]
    for sec in report["sections"]:
        lines.append(f"[{MARK[sec['status']]}] {sec['name']}")
        lines += [f"       {item}" for item in sec["items"]]
        lines.append("")
    fixes = report.get("fixes") or []
    if fixes:
        lines.append("Fixes: run forge-doctor --fix to go through them one at a time (each asks first).")
        for f in fixes:
            how = "" if f.get("automatic") else " (shows the command, does not run it)"
            lines.append(f"       {f['title']}{how}: {f['problem']}")
        lines.append("")
    return "\n".join(lines)
