"""Shared settings logic for Forge OS.

Defaults live in /usr/share/forge/settings.json (part of the image).
Your changes live in /etc/forge/settings.json and only hold the keys
you changed, so you can edit that file by hand at any time.
"""
import json
import math
import os
import re
import time
import unicodedata

SCHEMA_PATH = os.environ.get("FORGE_SCHEMA", "/usr/share/forge/settings.json")
OVERRIDES_PATH = os.environ.get("FORGE_CONF", "/etc/forge/settings.json")


# Where model folders may live. Anything else (like / or /etc) is refused.
# The first entry is the default folder itself; the others need a subfolder.
TEXT_MAX = 1024   # longest text setting; values end up in system files and the change history
MODEL_DIR_PREFIXES = ["/var/lib/ollama", "/var/mnt", "/mnt", "/var/home", "/home", "/run/media"]


def load_schema():
    with open(SCHEMA_PATH) as f:
        return json.load(f)


def settings_by_key(schema=None):
    schema = schema or load_schema()
    return {s["key"]: s for s in schema["settings"]}


def load_overrides():
    try:
        with open(OVERRIDES_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # A broken file should never stop the PC from booting: fall back to defaults.
        return {}


def values(schema=None, overrides=None):
    """Every setting with its current value (your change, or the default)."""
    defs = settings_by_key(schema)
    overrides = load_overrides() if overrides is None else overrides
    out = {k: s["default"] for k, s in defs.items()}
    for k, v in overrides.items():
        if k in defs:
            out[k] = v
    return out


def coerce(setting, raw):
    """Turn input (string from the CLI, or JSON value) into the right type."""
    t = setting["type"]
    if t == "toggle":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "on", "yes"):
            return True
        if s in ("0", "false", "off", "no"):
            return False
        raise ValueError(f"{setting['key']}: use on or off")
    if t == "number":
        try:
            n = float(raw)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{setting['key']}: must be a number")
        if not math.isfinite(n):
            raise ValueError(f"{setting['key']}: must be a number")
        # Every number setting is a whole number unless the schema says "float": true.
        if not setting.get("float"):
            if n != int(n):
                raise ValueError(f"{setting['key']}: must be a whole number")
            n = int(n)
        lo, hi = setting.get("min"), setting.get("max")
        if lo is not None and n < lo or hi is not None and n > hi:
            raise ValueError(f"{setting['key']}: must be between {lo} and {hi}")
        return n
    if t == "select":
        s = str(raw)
        if s not in setting["options"]:
            raise ValueError(f"{setting['key']}: choose one of {', '.join(setting['options'])}")
        return s
    s = str(raw).strip()
    # Values end up in system files, so line breaks and control characters are never allowed.
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        raise ValueError(f"{setting['key']}: line breaks and control characters are not allowed")
    if not s:
        if setting.get("allow_empty"):
            return ""
        raise ValueError(f"{setting['key']}: cannot be empty")
    if len(s) > TEXT_MAX:
        raise ValueError(f"{setting['key']}: too long (at most {TEXT_MAX} characters)")
    if setting.get("pattern") and not re.fullmatch(setting["pattern"], s):
        raise ValueError(f"{setting['key']}: {setting.get('help') or 'invalid value'}")
    return s


def validate(new_values, schema=None):
    """Check a full set of values. Returns (clean_values, errors)."""
    defs = settings_by_key(schema)
    clean, errors = {}, []
    for k, s in defs.items():
        try:
            clean[k] = coerce(s, new_values.get(k, s["default"]))
        except ValueError as e:
            errors.append(str(e))
    if not errors and clean["agents.memory_high_pct"] > clean["agents.memory_max_pct"]:
        errors.append("agents.memory_high_pct: soft limit must not be above the hard cap")
    if not errors:
        d = clean["ai.models_dir"].rstrip("/")
        if (not re.fullmatch(r"/[A-Za-z0-9._/-]+", d) or "/../" in d + "/" or "/./" in d + "/" or "//" in d
                or not any(d == p or d.startswith(p + "/") for p in MODEL_DIR_PREFIXES)
                or d in MODEL_DIR_PREFIXES[1:]):
            errors.append("ai.models_dir: must be a folder inside /var/lib/ollama, /var/mnt, /mnt, "
                          "/var/home, /home or /run/media (letters, numbers, . _ - / only)")
        clean["ai.models_dir"] = d
    if not errors and "sync.folder" in clean:
        folder, problem = check_sync_folder(clean["sync.folder"])
        if problem:
            errors.append("sync.folder: " + problem)
        elif clean["sync.enabled"] and not folder:
            errors.append("sync.folder: choose a folder before turning on settings sync")
        clean["sync.folder"] = folder
    return clean, errors


def to_overrides(clean, schema=None):
    """Keep only the values that differ from the defaults."""
    defs = settings_by_key(schema)
    return {k: v for k, v in clean.items() if v != defs[k]["default"]}


# Settings on several PCs: export files and the sync folder (forge-config export/import,
# forge-settings-sync). Files from other PCs are untrusted, so parse_export checks everything.
EXPORT_FORMAT = "forge-settings"
EXPORT_VERSION = 1
EXPORT_MAX = 256 * 1024     # a full export is about 4 KB; text values are capped at TEXT_MAX
EXPORT_DEPTH = 2            # the file object, then its "settings" object; values are never lists or objects
EXPORT_HEADER = ("format", "version", "exported_at", "from_host", "image")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Where a sync folder may be, and how many path parts the shortest allowed folder has
# (/home/alex/x, /var/home/alex/x, /run/media/alex/LABEL, /mnt/x, /var/mnt/x).
SYNC_ROOTS = (("home",), ("var", "home"), ("run", "media"), ("mnt",), ("var", "mnt"))
SYNC_MIN_PARTS = {("home",): 3, ("var", "home"): 4, ("run", "media"): 4, ("mnt",): 2, ("var", "mnt"): 3}


# Hangul fillers, braille blank, grapheme joiner, Khmer and Mongolian invisible vowels and separator.
INVISIBLE = set("\u115f\u1160\u3164\uffa0\u2800\u034f\u17b4\u17b5\u180b\u180c\u180d\u180e\u180f")


def check_sync_folder(p):
    """Returns (cleaned folder, None) or (None, problem). An empty value means "not set"."""
    if p == "":
        return "", None
    # Control (C0, DEL, C1), invisible format and bidi characters (like U+202E), line and
    # paragraph separators, surrogates, private-use and unassigned characters can hide what a path is.
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") for c in p):
        return None, "line breaks, control and invisible characters are not allowed"
    # Characters that show as nothing but are not in those categories, and every space but " ".
    if any(c in INVISIBLE or (unicodedata.category(c) == "Zs" and c != " ") or
           0xFE00 <= ord(c) <= 0xFE0F or 0xE0100 <= ord(c) <= 0xE01EF for c in p):
        return None, "invisible characters and unusual spaces are not allowed"
    if not p.startswith("/"):
        return None, "must be a full path starting with /"
    if ".." in p:
        return None, 'must not contain ".."'
    parts = p.rstrip("/").split("/")[1:]
    if any(x in ("", ".") for x in parts):
        return None, 'must not contain "//" or "/./"'
    for root in SYNC_ROOTS:
        if tuple(parts[:len(root)]) == root:
            if len(parts) < SYNC_MIN_PARTS[root]:
                break
            return "/" + "/".join(parts), None
    return None, ("must be a folder inside your home folder, /run/media or /mnt "
                  "(for example /run/media/alex/FORGESYNC)")


def machine_keys(schema=None):
    """Settings that belong to one PC (marked "machine": true), left out of portable exports."""
    return {k for k, s in settings_by_key(schema).items() if s.get("machine") is True}


def clean_host(name):
    """A host name safe for file names, terminals and notifications: a-z, 0-9 and "-", at most 63."""
    out = "".join(c if c in "abcdefghijklmnopqrstuvwxyz0123456789" else "-" for c in str(name).lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:63].rstrip("-")


def this_host():
    import socket
    return clean_host(os.environ.get("FORGE_HOSTNAME") or socket.gethostname()) or "pc"


def image_name():
    """The image this PC runs (forge-os or forge-os-universal), or "unknown"."""
    try:
        with open("/usr/share/ublue-os/image-info.json") as f:
            name = json.load(f).get("image-name", "")
    except (OSError, ValueError, AttributeError):
        name = ""
    return name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) else "unknown"


def export_doc(vals, schema, portable, host, image, when):
    """The export file: a small header, then the settings (all of them, or only the shareable ones)."""
    drop = machine_keys(schema) if portable else set()
    return {"format": EXPORT_FORMAT, "version": EXPORT_VERSION, "exported_at": when, "from_host": host,
            "image": image, "settings": {k: vals[k] for k in sorted(settings_by_key(schema)) if k not in drop}}


def _depth_ok(text, limit):
    """True if no list or object in this JSON text is nested deeper than limit (checked before parsing)."""
    depth, in_str, esc = 0, False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > limit:
                return False
        elif ch in "]}":
            depth -= 1
    return True


def _type_ok(setting, v):
    t = setting["type"]
    if t == "toggle":
        return isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, str)


def parse_export(data, schema=None, drop=(), allow_plain=False):
    """Read an export file (bytes) that may come from another PC or another person.

    Returns (settings, header, notes, error). On error the other values are None.
    Checks: size, nesting depth, header, known setting names only (others are dropped and
    counted, never shown), value types, and every value through coerce(). Settings in drop
    are left out. allow_plain also takes a bare {"key": value} object (like /etc/forge/settings.json).
    Cross-setting rules are checked later by validate(), together with this PC's other settings.
    """
    defs = settings_by_key(schema)
    if len(data) > EXPORT_MAX:
        return None, None, None, f"the file is too large (more than {EXPORT_MAX // 1024} KB)"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, None, "the file is not text (UTF-8)"
    if not _depth_ok(text, EXPORT_DEPTH):
        return None, None, None, "the file is nested too deeply to be a settings file"
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None, None, None, "the file is not valid JSON"
    if not isinstance(obj, dict):
        return None, None, None, "the file is not a settings export"
    if "format" not in obj and allow_plain:
        header, settings = {}, obj
    else:
        if set(obj) != set(EXPORT_HEADER) | {"settings"}:
            return None, None, None, "the file has missing or unknown fields, so it is not a settings export"
        if obj["format"] != EXPORT_FORMAT:
            return None, None, None, "the file is not a settings export"
        if type(obj["version"]) is not int or obj["version"] != EXPORT_VERSION:
            return None, None, None, "the file comes from a different version of Forge OS settings sharing"
        when = obj["exported_at"]
        try:
            if not isinstance(when, str) or len(when) != 20:
                raise ValueError
            time.strptime(when, TIME_FORMAT)
        except ValueError:
            return None, None, None, "the file has no valid export time"
        host = obj["from_host"]
        if not isinstance(host, str) or not host or clean_host(host) != host:
            return None, None, None, "the file has no valid computer name"
        image = obj["image"]
        if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", image):
            return None, None, None, "the file has no valid image name"
        header = {k: obj[k] for k in EXPORT_HEADER}
        settings = obj["settings"]
        if not isinstance(settings, dict):
            return None, None, None, "the file's settings are not a list of names and values"
    unknown = [k for k in settings if k not in defs]
    out, errors = {}, []
    for k, v in settings.items():
        if k not in defs or k in drop:
            continue
        if not _type_ok(defs[k], v):
            errors.append(f"{k}: wrong kind of value")
            continue
        try:
            out[k] = coerce(defs[k], v)
        except ValueError as e:
            errors.append(str(e).replace("\n", " "))
    if errors:
        return None, None, None, "the file has values that are not allowed:\n  " + "\n  ".join(errors[:20])
    notes = []
    if unknown:
        notes.append(f"{len(unknown)} unknown setting(s) in the file were ignored "
                     "(maybe from a newer Forge OS version)")
    return out, header, notes, None
