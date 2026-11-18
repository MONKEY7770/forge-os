"""Shared settings logic for Forge OS.

Defaults live in /usr/share/forge/settings.json (part of the image).
Your changes live in /etc/forge/settings.json and only hold the keys
you changed, so you can edit that file by hand at any time.
"""
import json
import math
import os
import re

SCHEMA_PATH = os.environ.get("FORGE_SCHEMA", "/usr/share/forge/settings.json")
OVERRIDES_PATH = os.environ.get("FORGE_CONF", "/etc/forge/settings.json")


# Where model folders may live. Anything else (like / or /etc) is refused.
# The first entry is the default folder itself; the others need a subfolder.
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
        raise ValueError(f"{setting['key']}: cannot be empty")
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
        if (not re.fullmatch(r"/[A-Za-z0-9._/-]+", d) or "/../" in d + "/" or "/./" in d + "/"
                or not any(d == p or d.startswith(p + "/") for p in MODEL_DIR_PREFIXES)
                or d in MODEL_DIR_PREFIXES[1:]):
            errors.append("ai.models_dir: must be a folder inside /var/lib/ollama, /var/mnt, /mnt, "
                          "/var/home, /home or /run/media (letters, numbers, . _ - / only)")
        clean["ai.models_dir"] = d
    return clean, errors


def to_overrides(clean, schema=None):
    """Keep only the values that differ from the defaults."""
    defs = settings_by_key(schema)
    return {k: v for k, v in clean.items() if v != defs[k]["default"]}
