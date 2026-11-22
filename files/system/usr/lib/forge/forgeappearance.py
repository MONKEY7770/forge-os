"""Appearance settings for Forge OS: background, dock and bootup. Per user, never root.

The fields come from /usr/share/forge/appearance.json (part of the image). Your values live in
$XDG_CONFIG_HOME/forge/appearance.json (default ~/.config/forge/appearance.json), and the pictures
and videos you add live in $XDG_DATA_HOME/forge/appearance/media/ (default ~/.local/share/...).
Nothing here asks for a password or goes through forge-apply.

Each section can have a small program, /usr/libexec/forge-appearance-SECTION, that puts the values
into effect. "apply" runs it as you, with argv ["apply"] only, and gives it the values on stdin.
"""
import fcntl
import json
import math
import os
import re
import secrets
import select
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata

SCHEMA_PATH = os.environ.get("FORGE_APPEARANCE_SCHEMA", "/usr/share/forge/appearance.json")
# Where the per-section programs are. Only tests point this somewhere else.
LIBEXEC = os.environ.get("FORGE_APPEARANCE_LIBEXEC", "/usr/libexec")

FIELD_TYPES = ("toggle", "number", "slider", "select", "color", "text", "media")
KINDS = ("image", "animated-image", "video")
ACCEPT = {"image": ("image", "animated-image"), "video": ("video",), "any": KINDS}
SECTION_RE = r"[a-z][a-z0-9_]{0,31}"
KEY_RE = r"[a-z][a-z0-9_]{0,39}"
MEDIA_ID_RE = r"[0-9a-f]{32}"
TEXT_MAX = 1024             # the longest a text field may allow
TEXT_DEFAULT_MAX = 256
VALUES_MAX = 256 * 1024     # the values file is a few KB; anything bigger is not ours
SCHEMA_MAX = 1024 * 1024
INDEX_MAX = 8 * 1024 * 1024
NAME_MAX = 120              # longest original file name kept for display

FILE_CAP_DEFAULT = 4 * 1024 ** 3     # one file
TOTAL_CAP_DEFAULT = 20 * 1024 ** 3   # the whole media library


def _cap(name, default):
    """A cap from the environment may only be lower than the default (tests use small ones)."""
    try:
        v = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return v if 0 < v < default else default


FILE_CAP = _cap("FORGE_APPEARANCE_FILE_CAP", FILE_CAP_DEFAULT)
TOTAL_CAP = _cap("FORGE_APPEARANCE_TOTAL_CAP", TOTAL_CAP_DEFAULT)
CHUNK = 1024 * 1024
FFPROBE_TIMEOUT_S = 20
BACKEND_TIMEOUT_S = 120
OUTPUT_MAX = 16 * 1024
STALE_UPLOAD_S = 600        # an unlocked temp upload file untouched this long is left over from a killed upload
QUARANTINE = ".quarantine"  # unknown files in the media folder are moved here, never deleted
QUARANTINE_MAX = 1000
ADDED_MAX = 1e11            # "added" times in the index must be a real date (seconds since 1970)
# Text fields with "choices": "backend" get a list to pick from: the backend's "options" command.
CHOICES_TIMEOUT_S = 20
CHOICES_OUTPUT_MAX = 1024 * 1024
CHOICES_MAX = 1000          # per field
CHOICE_LABEL_MAX = 120
CHOICE_NOTE_MAX = 300

# Extensions a media file may keep. The kind always comes from the file's bytes, never from this.
IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "avif", "heic", "heif", "gif", "apng", "svg", "bmp", "tif", "tiff",
              "jxl", "ico")
VIDEO_EXTS = ("mp4", "m4v", "mkv", "webm", "mov", "avi", "wmv", "flv", "mpg", "mpeg", "ogv", "3gp", "ts")
MEDIA_EXTS = frozenset(IMAGE_EXTS + VIDEO_EXTS)
# Format found in the bytes: (extensions that fit it, the first one is used otherwise; MIME type).
FORMATS = {
    "png": (("png",), "image/png"), "apng": (("apng", "png"), "image/apng"),
    "jpeg": (("jpg", "jpeg"), "image/jpeg"), "gif": (("gif",), "image/gif"), "webp": (("webp",), "image/webp"),
    "avif": (("avif",), "image/avif"), "heic": (("heic", "heif"), "image/heic"),
    "heif": (("heif", "heic"), "image/heif"), "bmp": (("bmp",), "image/bmp"), "tiff": (("tif", "tiff"), "image/tiff"),
    "jxl": (("jxl",), "image/jxl"), "ico": (("ico",), "image/x-icon"), "svg": (("svg",), "image/svg+xml"),
    "mp4": (("mp4", "m4v"), "video/mp4"), "m4v": (("m4v", "mp4"), "video/x-m4v"), "mov": (("mov",), "video/quicktime"),
    "mkv": (("mkv",), "video/x-matroska"), "webm": (("webm",), "video/webm"), "avi": (("avi",), "video/x-msvideo"),
    "wmv": (("wmv",), "video/x-ms-wmv"), "flv": (("flv",), "video/x-flv"), "mpeg": (("mpg", "mpeg"), "video/mpeg"),
    "ogv": (("ogv",), "video/ogg"), "3gp": (("3gp",), "video/3gpp"), "ts": (("ts",), "video/mp2t"),
}

# Environment passed on to a section's program: what a desktop tool needs, nothing else.
BACKEND_ENV = ("HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
               "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "XDG_SESSION_DESKTOP",
               "XDG_CURRENT_DESKTOP", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
               "KDE_FULL_SESSION", "KDE_SESSION_VERSION")


class MediaError(Exception):
    """A file could not be added to (or read from) the media library. The text is for the user."""


class MediaTooLarge(MediaError):
    """Over the size cap for one file or for the whole library."""


# Paths

def _xdg(name, fallback):
    base = os.environ.get(name, "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), fallback)
    return base


def config_path():
    return os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "forge", "appearance.json")


def data_dir():
    return os.path.join(_xdg("XDG_DATA_HOME", os.path.join(".local", "share")), "forge", "appearance")


def media_dir():
    return os.path.join(data_dir(), "media")


def index_path():
    return os.path.join(data_dir(), "media.json")


def _private_dir(path):
    """Make path (and missing parents) and check it is a real folder of ours, then set it to 0700."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise MediaError(f"{path} is not a private folder of yours (a link or someone else's), so nothing was saved")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)
    return path


def _read_small(path, limit):
    """Bytes of a regular file of at most limit bytes, without following a link. None otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        with os.fdopen(fd, "rb") as f:
            fd = None
            data = f.read(limit + 1)
        return data if len(data) <= limit else None
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _read_json(path, limit):
    data = _read_small(path, limit)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return None


def _write_json_atomic(path, obj):
    """Write obj to path through a 0600 temporary file in the same folder, then rename it over path."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(obj, f, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Schema

def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _is_scalar(x):
    return x is None or isinstance(x, (str, bool)) or _is_num(x)


def _clean_text(s, limit):
    """Text without control, format or invisible characters, trimmed, or None if it has any."""
    s = s.strip()
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") for c in s):
        return None
    return s if len(s) <= limit else None


def _field_problem(f):
    """Why a field definition can't be used, or None."""
    if not isinstance(f, dict):
        return "a field is not an object"
    key = f.get("key")
    if not isinstance(key, str) or not re.fullmatch(KEY_RE, key):
        return f"field {key!r}: key must be lower case letters, digits and _"
    t = f.get("type")
    if t not in FIELD_TYPES:
        return f"{key}: unknown type {t!r}"
    if not isinstance(f.get("label"), str) or not f["label"].strip():
        return f"{key}: needs a label"
    if not isinstance(f.get("help", ""), str):
        return f"{key}: help must be text"
    if "advanced" in f and not isinstance(f["advanced"], bool):
        return f"{key}: advanced must be true or false"
    if "tags" in f and not (isinstance(f["tags"], list) and all(isinstance(x, str) for x in f["tags"])):
        return f"{key}: tags must be a list of words"
    if t in ("number", "slider"):
        lo, hi, step = f.get("min"), f.get("max"), f.get("step", 1)
        if not (_is_num(lo) and _is_num(hi) and _is_num(step) and lo < hi and step > 0):
            return f"{key}: needs min below max and a step above 0"
        if "unit" in f and not (isinstance(f["unit"], str) and len(f["unit"]) <= 12):
            return f"{key}: unit must be short text"
    if t == "select":
        opts = f.get("options")
        if (not isinstance(opts, list) or not opts
                or not all(isinstance(o, dict) and isinstance(o.get("value"), str) and isinstance(o.get("label"), str)
                           for o in opts)
                or len({o["value"] for o in opts}) != len(opts)):
            return f"{key}: options must be a list of {{value, label}} with different values"
    if t == "text":
        m = f.get("max_length", TEXT_DEFAULT_MAX)
        if type(m) is not int or not 1 <= m <= TEXT_MAX:
            return f"{key}: max_length must be 1 to {TEXT_MAX}"
    if "choices" in f and (t != "text" or f["choices"] != "backend"):
        return f"{key}: choices is only for text fields, and only as \"backend\""
    if t == "media":
        if f.get("accept") not in ACCEPT:
            return f"{key}: accept must be image, video or any"
        if f.get("default") != "":
            return f"{key}: a media field's default must be empty"
        return None
    if "default" not in f:
        return f"{key}: needs a default"
    try:
        coerce(f, f["default"], media={})
    except ValueError as e:
        return f"{key}: default is not allowed ({e})"
    return None


def _show_if_problem(f, keys):
    cond = f.get("show_if")
    if cond is None:
        return None
    if (not isinstance(cond, dict) or cond.get("key") not in keys or cond.get("key") == f["key"]
            or len(cond) != 2 or not (("equals" in cond and _is_scalar(cond["equals"])) or
                                      ("in" in cond and isinstance(cond["in"], list)
                                       and all(_is_scalar(x) for x in cond["in"])))):
        return f"{f['key']}: show_if must be {{key: another field, equals: value}} or {{key, in: [values]}}"
    return None


FIELD_KEYS = ("key", "type", "label", "help", "default", "advanced", "show_if", "tags", "min", "max", "step", "unit",
              "options", "max_length", "accept", "choices")


def _check_schema(raw):
    """Returns (usable schema, problems). Bad sections and fields are left out, never fatal."""
    clean, problems = {"version": 1, "sections": []}, []
    if not isinstance(raw, dict) or not isinstance(raw.get("sections"), list):
        return clean, ["the file has no list of sections"]
    seen = set()
    for s in raw["sections"]:
        if not isinstance(s, dict):
            problems.append("a section is not an object")
            continue
        sid = s.get("id")
        if not isinstance(sid, str) or not re.fullmatch(SECTION_RE, sid) or sid in seen:
            problems.append(f"section {sid!r}: id must be new, lower case letters, digits and _")
            continue
        if not isinstance(s.get("label"), str) or not s["label"].strip() or not isinstance(s.get("help", ""), str):
            problems.append(f"{sid}: needs a label (and help must be text)")
            continue
        if not isinstance(s.get("fields"), list):
            problems.append(f"{sid}: fields must be a list")
            continue
        seen.add(sid)
        good, keys = [], set()
        for f in s["fields"]:
            p = _field_problem(f)
            if p is None and f["key"] in keys:
                p = f"{f['key']}: used twice"
            if p:
                problems.append(f"{sid}.{p}")
                continue
            keys.add(f["key"])
            good.append({k: f[k] for k in FIELD_KEYS if k in f})
        fields = []
        for f in good:
            p = _show_if_problem(f, keys)
            if p:
                problems.append(f"{sid}.{p}")
            else:
                fields.append(f)
        clean["sections"].append({"id": sid, "label": s["label"], "help": s.get("help", ""), "fields": fields})
    return clean, problems


def schema_problems(raw):
    return _check_schema(raw)[1]


def clean_schema(raw):
    return _check_schema(raw)[0]


def load_schema():
    """The usable part of the appearance schema. A missing or broken file gives no sections."""
    return clean_schema(_read_json(SCHEMA_PATH, SCHEMA_MAX))


def sections(schema):
    return {s["id"]: s for s in schema["sections"]}


# Values

def coerce(field, raw, cli=False, media=None):
    """The checked value for a field, or ValueError. cli=True also takes text like "on" or "42"
    (from the command line); JSON input must already have the right type."""
    t, key = field["type"], field["key"]
    if t == "toggle":
        if isinstance(raw, bool):
            return raw
        if cli and isinstance(raw, str):
            s = raw.strip().lower()
            if s in ("1", "true", "on", "yes"):
                return True
            if s in ("0", "false", "off", "no"):
                return False
        raise ValueError(f"{key}: use on or off")
    if t in ("number", "slider"):
        n = raw
        if cli and isinstance(raw, str):
            try:
                n = float(raw.strip())
            except ValueError:
                raise ValueError(f"{key}: must be a number")
        if not _is_num(n):
            raise ValueError(f"{key}: must be a number")
        lo, hi, step = field["min"], field["max"], field.get("step", 1)
        if not lo <= n <= hi:
            raise ValueError(f"{key}: must be between {lo} and {hi}")
        k = (n - lo) / step
        if abs(k - round(k)) > 1e-6:
            raise ValueError(f"{key}: must be a step of {step} from {lo}")
        whole = all(float(x).is_integer() for x in (lo, step))
        n = lo + round(k) * step
        return int(round(n)) if whole else round(n, 6)
    if t == "select":
        if isinstance(raw, str) and raw in [o["value"] for o in field["options"]]:
            return raw
        raise ValueError(f"{key}: choose one of {', '.join(o['value'] for o in field['options'])}")
    if t == "color":
        if isinstance(raw, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", raw.strip()):
            return raw.strip().lower()
        raise ValueError(f"{key}: must be a color like #1a2b3c")
    if t == "text":
        limit = field.get("max_length", TEXT_DEFAULT_MAX)
        s = _clean_text(raw, limit) if isinstance(raw, str) else None
        if s is None:
            raise ValueError(f"{key}: text up to {limit} characters, without line breaks or control characters")
        return s
    if t == "media":
        if raw == "":
            return ""
        if not isinstance(raw, str) or not re.fullmatch(MEDIA_ID_RE, raw):
            raise ValueError(f"{key}: not a file from your media library")
        item = (media_index() if media is None else media).get(raw)
        if item is None:
            raise ValueError(f"{key}: that file is not in your media library (any more)")
        if item["kind"] not in ACCEPT[field["accept"]]:
            raise ValueError(f"{key}: needs {'a video' if field['accept'] == 'video' else 'a picture'}")
        return raw
    raise ValueError(f"{key}: unknown type")


def visible(field, values, fields=None):
    """True if the field should be shown, given the other values in its section (show_if).
    With the section's fields, a field is also hidden when the field its show_if names is hidden."""
    def same(a, b):
        return a == b and isinstance(a, bool) == isinstance(b, bool)

    by_key = {f["key"]: f for f in fields or ()}
    seen = set()
    while field is not None and field["key"] not in seen:
        seen.add(field["key"])
        cond = field.get("show_if")
        if not cond:
            return True
        have = values.get(cond["key"])
        if not (same(have, cond["equals"]) if "equals" in cond else any(same(have, x) for x in cond["in"])):
            return False
        field = by_key.get(cond["key"])
    return True


def defaults(schema):
    return {s["id"]: {f["key"]: f["default"] for f in s["fields"]} for s in schema["sections"]}


def clean_values(raw, schema, media=None):
    """Every value checked against the schema: unknown names are dropped, bad values become the default."""
    media = media_index() if media is None else media
    out = defaults(schema)
    if not isinstance(raw, dict):
        return out
    for s in schema["sections"]:
        given = raw.get(s["id"])
        if not isinstance(given, dict):
            continue
        for f in s["fields"]:
            if f["key"] in given:
                try:
                    out[s["id"]][f["key"]] = coerce(f, given[f["key"]], media=media)
                except ValueError:
                    pass
    return out


def check_values(raw, schema, media=None, base=None):
    """Strict check of values from the page. Returns (all values, errors). Sections or fields that are
    not given keep their value from base (default: the defaults)."""
    media = media_index() if media is None else media
    out = clean_values(base, schema, media) if base is not None else defaults(schema)
    if not isinstance(raw, dict):
        return out, ["values must be an object of sections"]
    secs, errors = sections(schema), []
    for sid, given in raw.items():
        if sid not in secs:
            errors.append(f"{str(sid)[:40]!r}: unknown section")
            continue
        if not isinstance(given, dict):
            errors.append(f"{sid}: must be an object of values")
            continue
        fields = {f["key"]: f for f in secs[sid]["fields"]}
        for key, v in given.items():
            if key not in fields:
                errors.append(f"{sid}.{str(key)[:40]}: unknown setting")
                continue
            try:
                out[sid][key] = coerce(fields[key], v, media=media)
            except ValueError as e:
                errors.append(f"{sid}.{e}")
    return out, errors


class _FileLock:
    """An flock on a 0600 lock file in your private data folder: one change at a time, across threads
    and processes (the page, the tool and the media library)."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        os.makedirs(os.path.dirname(data_dir()), mode=0o700, exist_ok=True)
        _private_dir(data_dir())
        self.fd = os.open(os.path.join(data_dir(), self.name),
                          os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def _values_lock():
    return _FileLock(".values.lock")


def load(schema=None):
    """Your values, checked. Never fails: a missing, huge, odd or broken file gives the defaults."""
    schema = load_schema() if schema is None else schema
    return clean_values(_read_json(config_path(), VALUES_MAX), schema)


def save(values, schema=None):
    """Check values again and write them atomically, owner-only. Returns what was written."""
    schema = load_schema() if schema is None else schema
    clean = clean_values(values, schema)
    path = config_path()
    os.makedirs(os.path.dirname(os.path.dirname(path)), mode=0o700, exist_ok=True)
    _private_dir(os.path.dirname(path))
    _write_json_atomic(path, clean)
    return clean


def set_value(section, key, raw, schema=None):
    """Change one value from text (the command line). ValueError if the section, name or value is wrong."""
    schema = load_schema() if schema is None else schema
    sec = sections(schema).get(section)
    if sec is None:
        raise ValueError(f"unknown section {section!r} (use one of: {', '.join(sections(schema))})")
    field = {f["key"]: f for f in sec["fields"]}.get(key)
    if field is None:
        raise ValueError(f"{section} has no setting {key!r}")
    value = coerce(field, raw, cli=True)
    with _values_lock():          # read, change and write as one step, so a save at the same time is kept
        values = load(schema)
        values[section][key] = value
        return save(values, schema)


def save_checked(raw, schema=None):
    """Strictly check values from the page (only the given sections and fields change) and save them, as
    one locked step. Returns (values, errors); nothing is written when there are errors."""
    schema = load_schema() if schema is None else schema
    with _values_lock():
        clean, errors = check_values(raw, schema, base=load(schema))
        if errors:
            return clean, errors
        return save(clean, schema), []


# Media library

def _clean_name(name):
    name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))
    return name.strip()[:NAME_MAX]


def _read_index():
    """(state, raw items, raw text) of media.json. state: "ok", "missing" or "damaged" (a link, a folder,
    too big, not UTF-8 JSON, or not {"items": [...]}). Never fails."""
    p = index_path()
    if not os.path.lexists(p):
        return "missing", [], ""
    data = _read_small(p, INDEX_MAX)
    if data is None:
        return "damaged", [], ""
    try:
        text = data.decode("utf-8")
        obj = json.loads(text)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "damaged", [], ""
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        return "damaged", [], ""
    return "ok", obj["items"], text


def _media_files():
    """Names of the files and links in the media folder, without temp uploads and the quarantine."""
    try:
        names = os.listdir(media_dir())
    except OSError:
        return []
    out = []
    for name in names:
        if name.startswith(".upload-") or name == QUARANTINE:
            continue
        try:
            st = os.lstat(os.path.join(media_dir(), name))
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            out.append(name)
    return out


def index_problem():
    """None, or why the library is read-only right now (its index is damaged, or missing while files are there).
    Nothing in the library is changed or cleaned up while this is so."""
    state = _read_index()[0]
    if state == "damaged":
        return (f"the media library index ({index_path()}) is damaged, so nothing in the library is changed. "
                "Restore it from a backup, or move it away together with the files in " + media_dir())
    if state == "missing" and _media_files():
        return (f"the media library index ({index_path()}) is missing, so nothing in the library is changed. "
                "Restore it, or move the files in " + media_dir() + " somewhere else")
    return None


def _recover_index():
    """With the lock held: when media.json is missing but every file in the media folder is a regular file named
    <32 hex id>.<allowed extension> (what an add killed before it wrote the index leaves), write a new index
    from them, checking each file's bytes again. Files whose bytes don't fit go to the quarantine. Anything
    else in the folder (other names, links): do nothing, the library stays read-only. Never deletes."""
    if _read_index()[0] != "missing":
        return
    d = media_dir()
    names = _media_files()
    if not names:
        return
    for name in names:
        stem, _, ext = name.partition(".")
        if (not re.fullmatch(MEDIA_ID_RE, stem) or ext not in MEDIA_EXTS
                or not stat.S_ISREG(os.lstat(os.path.join(d, name)).st_mode)):
            return
    index = {}
    for name in names:
        stem, _, ext = name.partition(".")
        p = os.path.join(d, name)
        found = sniff(p)
        if found is None or ext not in FORMATS[found[1]][0] or (found[0] == "video" and not _ffprobe_ok(p)):
            _quarantine(d, name)   # the same checks as an upload, ffprobe included
            continue
        st = os.lstat(p)
        added = st.st_mtime if 0 <= st.st_mtime < ADDED_MAX else time.time()
        index[stem] = {"id": stem, "file": name, "kind": found[0], "format": found[1], "mime": FORMATS[found[1]][1],
                       "name": name, "size": st.st_size, "added": added}
    _write_index(index)


def media_index():
    """id -> item for every well-formed entry in the index. Never fails."""
    out = {}
    for it in _read_index()[1]:
        try:
            mid, fmt, ext = it["id"], it["format"], it["file"].rsplit(".", 1)[1]
            if (not re.fullmatch(MEDIA_ID_RE, mid) or fmt not in FORMATS or it["file"] != f"{mid}.{ext}"
                    or ext not in FORMATS[fmt][0] or it["kind"] not in KINDS
                    or type(it["size"]) is not int or it["size"] < 0
                    or not _is_num(it["added"]) or not 0 <= it["added"] < ADDED_MAX):
                continue
            out[mid] = {"id": mid, "file": it["file"], "kind": it["kind"], "format": fmt, "mime": FORMATS[fmt][1],
                        "name": _clean_name(it.get("name", "")) or it["file"], "size": it["size"],
                        "added": it["added"]}
        except (KeyError, TypeError, AttributeError, IndexError):
            continue
    return out


def _write_index(index, drop=()):
    """Write the index. Entries already in the file that don't pass the checks (not in index) are kept as they
    are, so their files stay known; only ids in drop are taken out. Refuses when the index is damaged
    (callers check index_problem() before they change any file)."""
    if _read_index()[0] == "damaged":
        raise MediaError(index_problem())
    def ours(it):
        mid = it.get("id") if isinstance(it, dict) else None
        return isinstance(mid, str) and (mid in index or mid in drop)
    keep = [it for it in _read_index()[1] if isinstance(it, dict) and not ours(it)]
    _write_json_atomic(index_path(), {"version": 1,
                                      "items": sorted(index.values(), key=lambda i: i["added"]) + keep})


def _Lock():
    """One change to the media library index at a time. Only held for short steps, never while copying."""
    return _FileLock(".lock")


def media_path(mid):
    """The path of a media file, only if it is still a regular file in the library. None otherwise."""
    if not isinstance(mid, str) or not re.fullmatch(MEDIA_ID_RE, mid):
        return None
    item = media_index().get(mid)
    if item is None:
        return None
    path = os.path.join(media_dir(), item["file"])
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return path if stat.S_ISREG(st.st_mode) else None


def media_list():
    """Every usable item, newest first."""
    out = [it for mid, it in media_index().items() if media_path(mid)]
    return sorted(out, key=lambda i: i["added"], reverse=True)


def _used_bytes():
    """Space used by the library: only files in the index count (not uploads in progress or leftovers)."""
    total, d = 0, media_dir()
    for item in media_index().values():
        try:
            st = os.lstat(os.path.join(d, item["file"]))
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
    return total


def _remove_if_stale_upload(p, st):
    """Remove a temp upload only if no upload holds its lock and it was not touched for STALE_UPLOAD_S."""
    if stat.S_ISLNK(st.st_mode):
        if time.time() - st.st_mtime > STALE_UPLOAD_S:
            os.unlink(p)
        return
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return                      # an upload is still writing it
        if stat.S_ISREG(os.fstat(fd).st_mode) and time.time() - os.fstat(fd).st_mtime > STALE_UPLOAD_S:
            os.unlink(p)
    finally:
        os.close(fd)


def _quarantine(d, name):
    """Move one unknown file (or link, never followed) into the quarantine folder. Never deletes anything."""
    q = os.path.join(d, QUARANTINE)
    try:
        os.mkdir(q, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(q)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        return                          # not a real folder of ours: leave the file where it is
    if len(os.listdir(q)) >= QUARANTINE_MAX:
        return
    target = name
    while os.path.lexists(os.path.join(q, target)):
        target = f"{name}.{secrets.token_hex(4)}"
    os.rename(os.path.join(d, name), os.path.join(q, target))


def media_cleanup():
    """Tidy the media folder: remove temp uploads that are provably left over (unlocked and stale), and move
    files that the index does not mention anywhere into media/.quarantine (never deleted). Does nothing else
    while the index is missing or damaged. Never follows a link; folders are left alone."""
    d = media_dir()
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        return
    with _Lock():
        try:
            _recover_index()
        except (OSError, MediaError):
            pass
        state, _, text = _read_index()
        known = {item["file"] for item in media_index().values()}
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                st = os.lstat(p)
                if name == QUARANTINE or not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                    continue
                if name.startswith(".upload-"):
                    _remove_if_stale_upload(p, st)
                    continue
                if state != "ok" or name in known:
                    continue
                stem = name.split(".", 1)[0]
                if name in text or (len(stem) >= 8 and stem in text):
                    continue                # mentioned in the index, even in an entry that fails the checks
                _quarantine(d, name)
            except OSError:
                pass


def _drain_forever(rfd):
    """Hand the read end of the output pipe to a tiny reader that throws everything away, in its own session.
    It ends by itself when the last helper closes the pipe. Helpers never get SIGPIPE and never fill a disk,
    even after this process exits."""
    cat = next((c for c in ("/usr/bin/cat", "/bin/cat") if os.access(c, os.X_OK)), None)
    if cat is None:
        return
    try:
        subprocess.Popen([cat], stdin=rfd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env={"PATH": "/usr/bin:/bin"}, cwd="/", start_new_session=True, close_fds=True)
    except OSError:
        pass


def _run(argv, stdin_data, timeout, env, cwd=None, merge=True, keep=None):
    """Run argv (never a shell) in its own process group. Returns (returncode or None on timeout, output).

    Output goes into a pipe that a thread reads while the program runs, keeping only the last `keep` bytes
    (default OUTPUT_MAX) in memory: nothing is written to disk. Waits for the program itself, not for its output
    to close; helpers are left alone when it exits, and killed with its process group on timeout. If a helper
    still holds the output after the program exited, the pipe is handed to a reader that discards it
    (_drain_forever), so the helper keeps running. merge=False drops what it writes to stderr (for programs
    that answer in JSON on stdout)."""
    keep = OUTPUT_MAX if keep is None else keep
    rfd, wfd = os.pipe()
    try:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=wfd,
                             stderr=subprocess.STDOUT if merge else subprocess.DEVNULL,
                             env=env, cwd=cwd, start_new_session=True, close_fds=True)
    finally:
        os.close(wfd)
    tail, exited, eof = bytearray(), threading.Event(), threading.Event()

    def feed():
        try:
            p.stdin.write(stdin_data)
        except OSError:
            pass
        finally:
            try:
                p.stdin.close()
            except OSError:
                pass

    def drain():
        stop_at = None
        while True:
            if exited.is_set() and stop_at is None:
                stop_at = time.monotonic() + 0.5     # what the program wrote just before it exited
            if stop_at is not None and time.monotonic() > stop_at:
                return
            ready, _, _ = select.select([rfd], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(rfd, 65536)
            if not chunk:
                eof.set()
                return
            tail.extend(chunk)
            if len(tail) > 2 * keep:
                del tail[:-keep]
    threading.Thread(target=feed, daemon=True).start()
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.wait()
        rc = None
    exited.set()
    reader.join(3)
    if not eof.is_set():
        _drain_forever(rfd)             # a helper still has the output open
    os.close(rfd)
    return rc, bytes(tail[-keep:])


def _ffprobe_ok(path):
    """Extra check for videos: ffprobe, only if it is installed and never as root, must find a video stream."""
    exe = shutil.which("ffprobe")
    if exe is None or os.geteuid() == 0:
        return True
    try:
        rc, out = _run([exe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", path], b"",
                       FFPROBE_TIMEOUT_S, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    except OSError:
        return True   # could not start it at all: the byte check alone decides
    if rc != 0:
        return False
    try:
        streams = json.loads(out.decode("utf-8", "replace")).get("streams", [])
        return any(isinstance(s, dict) and s.get("codec_type") == "video" for s in streams)
    except (ValueError, AttributeError):
        return False


def media_add_stream(src, length, name, progress=None):
    """Copy a picture or video from the readable src into the library. length: the exact number of bytes
    to read (an upload), or None to read to the end (a file). The original name is kept for display only.
    Returns the new item. MediaError with a plain reason if it is refused."""
    if length is not None and (type(length) is not int or length < 0):
        raise MediaError("the size of the file is unknown")
    if length is not None and length > FILE_CAP:
        raise MediaTooLarge(f"the file is too large (at most {FILE_CAP // 1024 ** 3} GiB)")
    # Copying and checking happen without the lock, so a slow upload never holds up anything else.
    # The lock is only taken at the end, to check the space again, link the file in and write the index.
    problem = index_problem()
    if problem and _read_index()[0] == "missing":
        with _Lock():
            _recover_index()          # an earlier add was stopped before it wrote the index
        problem = index_problem()
    if problem:
        raise MediaError(problem)
    os.makedirs(os.path.dirname(data_dir()), mode=0o700, exist_ok=True)
    _private_dir(data_dir())
    d = _private_dir(media_dir())
    used = _used_bytes()
    if length is not None and used + length > TOTAL_CAP:
        raise MediaTooLarge(f"the media library is full (at most {TOTAL_CAP // 1024 ** 3} GiB together). "
                            "Remove something first")
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".upload-")
    fcntl.flock(fd, fcntl.LOCK_EX)      # held while copying: cleanup never takes a file that is being written
    try:
        written = 0
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o600)
            while length is None or written < length:
                want = CHUNK if length is None else min(CHUNK, length - written)
                data = src.read(want)
                if not data:
                    if length is not None:
                        raise MediaError("the file arrived incomplete")
                    break
                written += len(data)
                if written > FILE_CAP:
                    raise MediaTooLarge(f"the file is too large (at most {FILE_CAP // 1024 ** 3} GiB)")
                if used + written > TOTAL_CAP:
                    raise MediaTooLarge(f"the media library is full (at most {TOTAL_CAP // 1024 ** 3} GiB "
                                     "together). Remove something first")
                out.write(data)
                if progress:
                    progress(written)
            out.flush()
            os.fsync(out.fileno())
        if written == 0:
            raise MediaError("the file is empty")
        found = sniff(tmp)
        if found is None:
            raise MediaError("this is not a picture or video Forge can use")
        kind, fmt = found
        if kind == "video" and not _ffprobe_ok(tmp):
            raise MediaError("this video could not be read (ffprobe found no video in it)")
        clean = _clean_name(name)
        ext = clean.rsplit(".", 1)[1].lower() if "." in clean else ""
        if ext not in MEDIA_EXTS or ext not in FORMATS[fmt][0]:
            ext = FORMATS[fmt][0][0]
        with _Lock():
            problem = index_problem()
            if problem:
                raise MediaError(problem)
            if _used_bytes() + written > TOTAL_CAP:
                raise MediaTooLarge(f"the media library is full (at most {TOTAL_CAP // 1024 ** 3} GiB "
                                    "together). Remove something first")
            mid = secrets.token_hex(16)
            final = os.path.join(d, f"{mid}.{ext}")
            os.link(tmp, final)          # never replaces anything
            item = {"id": mid, "file": f"{mid}.{ext}", "kind": kind, "format": fmt, "mime": FORMATS[fmt][1],
                    "name": clean or f"{mid}.{ext}", "size": written, "added": time.time()}
            index = media_index()
            index[mid] = item
            try:
                _write_index(index)
            except BaseException:
                os.unlink(final)
                raise
        return item
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def media_add(path):
    """Copy the regular file at path into the library (never moves it, never follows a link)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        if os.path.islink(path):
            raise MediaError("it is a link. Add the file it points to instead")
        raise MediaError(f"it could not be opened ({e.strerror or e})")
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise MediaError("only regular files can be added (not folders, devices or pipes)")
    with os.fdopen(fd, "rb") as f:
        if st.st_size > FILE_CAP:
            raise MediaTooLarge(f"the file is too large (at most {FILE_CAP // 1024 ** 3} GiB)")
        return media_add_stream(f, None, os.path.basename(path))


def media_remove(mid):
    """Remove one item. Settings that used it go back to their default. False if there was no such item."""
    if not isinstance(mid, str) or not re.fullmatch(MEDIA_ID_RE, mid):
        return False
    with _Lock():
        if index_problem():
            return False
        index = media_index()
        item = index.pop(mid, None)
        if item is None:
            return False
        _write_index(index, drop=(mid,))   # first the index, so a failure never leaves an entry without its file
        try:
            os.unlink(os.path.join(media_dir(), item["file"]))
        except FileNotFoundError:
            pass
    if os.path.lexists(config_path()):
        schema = load_schema()
        with _values_lock():
            save(load(schema), schema)
    return True


# Recognising formats from their bytes

def _ftyp(head):
    size = int.from_bytes(head[:4], "big")
    end = min(size, len(head)) if size >= 16 else 16
    brands = {head[8:12]} | {head[i:i + 4] for i in range(16, end - 3, 4)}
    major = head[8:12]
    mp4 = {b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash", b"mmp4",
           b"f4v ", b"MSNV", b"NDAS", b"XAVC"}
    if major == b"avif":
        return "image", "avif"
    if major == b"avis":
        return "animated-image", "avif"
    if major in (b"heic", b"heix", b"heim", b"heis"):
        return "image", "heic"
    if major in (b"hevc", b"hevx"):
        return "animated-image", "heic"
    if major in (b"mif1", b"heif"):
        return "image", "avif" if b"avif" in brands else "heic" if b"heic" in brands else "heif"
    if major == b"msf1":
        return "animated-image", "avif" if b"avis" in brands else "heif"
    if major == b"qt  ":
        return "video", "mov"
    if major[:3] in (b"3gp", b"3g2", b"3ge", b"3gg"):
        return "video", "3gp"
    if major[:3] == b"M4V":
        return "video", "m4v"
    if major in (b"M4A ", b"M4B ", b"M4P "):
        return None                                  # sound only
    if major in mp4 or brands & mp4:
        return "video", "mp4"
    return None


def _png(f):
    f.seek(8)
    for _ in range(100000):
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        n, kind = int.from_bytes(hdr[:4], "big"), hdr[4:]
        if kind == b"acTL":
            return "animated-image", "apng"
        if kind in (b"IDAT", b"IEND"):
            break
        f.seek(n + 4, os.SEEK_CUR)
    return "image", "png"


def _gif(f):
    head = f.read(13)
    if len(head) < 13:
        return None
    flags = head[10]
    if flags & 0x80:
        f.seek(3 * (2 << (flags & 7)), os.SEEK_CUR)
    frames = 0

    def skip_blocks():
        while True:
            b = f.read(1)
            if not b or b == b"\x00":
                return
            f.seek(b[0], os.SEEK_CUR)
    for _ in range(1000000):
        b = f.read(1)
        if b == b"\x2c":
            frames += 1
            if frames > 1:
                return "animated-image", "gif"
            desc = f.read(9)
            if len(desc) < 9:
                break
            if desc[8] & 0x80:
                f.seek(3 * (2 << (desc[8] & 7)), os.SEEK_CUR)
            f.read(1)
            skip_blocks()
        elif b == b"\x21":
            f.read(1)
            skip_blocks()
        else:
            break
    return ("image", "gif") if frames else None


def _svg(head):
    text = head[:16384]
    if text.startswith(b"\xef\xbb\xbf"):
        text = text[3:]
    try:
        s = text.decode("utf-8")
    except UnicodeDecodeError as e:
        s = text[:e.start].decode("utf-8")       # a character cut at the end of what was read
    s = s.lstrip()
    for _ in range(50):
        m = re.match(r"(<\?xml[^>]*\?>|<!--.*?-->|<!DOCTYPE[^>\[]*(\[.*?\])?\s*>)\s*", s, re.S | re.I)
        if not m:
            break
        s = s[m.end():]
    return ("image", "svg") if re.match(r"<svg[\s>/]", s) else None


def sniff(path):
    """(kind, format) from a file's first bytes, or None if it is no picture or video Forge knows."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    with os.fdopen(fd, "rb") as f:
        head = f.read(65536)
        if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
            return _png(f)
        if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
            f.seek(0)
            return _gif(f)
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        if head[12:16] == b"VP8X":
            return ("animated-image" if len(head) > 20 and head[20] & 0x02 else "image"), "webp"
        return ("image", "webp") if head[12:16] in (b"VP8 ", b"VP8L") else None
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video", "avi"
    if head[4:8] == b"ftyp":
        return _ftyp(head)
    if head[4:8] in (b"moov", b"mdat", b"wide", b"pnot"):
        return "video", "mov"
    if (head[:2] == b"BM" and len(head) >= 26 and head[6:10] == b"\x00\x00\x00\x00"
            and int.from_bytes(head[14:18], "little") in (12, 16, 40, 52, 56, 64, 108, 124)):
        return "image", "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image", "tiff"
    if head[:2] == b"\xff\x0a" or head[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n":
        return "image", "jxl"
    if head[:4] == b"\x00\x00\x01\x00" and len(head) >= 6 and 1 <= int.from_bytes(head[4:6], "little") <= 256:
        return "image", "ico"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        top = head[:4096]
        return ("video", "webm") if b"webm" in top else ("video", "mkv") if b"matroska" in top else None
    if head[:16] == bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c"):
        return "video", "wmv"
    if head[:4] == b"FLV\x01":
        return "video", "flv"
    if head[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):
        return "video", "mpeg"
    if head[:4] == b"OggS":
        return ("video", "ogv") if any(m in head for m in (b"\x80theora", b"\x01video", b"OVP80")) else None
    if len(head) >= 188 * 3 and head[0] == head[188] == head[376] == 0x47:
        return "video", "ts"
    return _svg(head)


# Putting values into effect

def backend_path(sid):
    return os.path.join(LIBEXEC, "forge-appearance-" + sid)


def backend_installed(sid):
    p = backend_path(sid)
    return os.path.isfile(p) and os.access(p, os.X_OK)


def _scrub(text):
    text = "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf"))
    return text[-OUTPUT_MAX:].strip()


def _backend_payload(sid, schema, secs):
    values = load(schema)
    index = media_index()
    used = {}
    for s in schema["sections"]:
        for f in s["fields"]:
            mid = values[s["id"]].get(f["key"]) if f["type"] == "media" else ""
            path = media_path(mid) if mid else None
            if path:
                used[mid] = {"path": path, "kind": index[mid]["kind"], "format": index[mid]["format"],
                             "mime": index[mid]["mime"], "name": index[mid]["name"]}
    return {"section": sid, "values": values[sid], "all": values, "media": used, "fields": secs[sid]["fields"]}


def _backend_env(sid):
    env = {k: v for k, v in os.environ.items() if k in BACKEND_ENV or k.startswith("LC_")}
    env.update(PATH="/usr/bin:/bin", FORGE_LIB=os.path.dirname(os.path.abspath(__file__)),
               FORGE_APPEARANCE_SECTION=sid, FORGE_APPEARANCE_FILE=config_path(),
               FORGE_APPEARANCE_MEDIA=media_dir(), FORGE_APPEARANCE_SCHEMA=SCHEMA_PATH)
    home = env.get("HOME")
    return env, home if home and os.path.isdir(home) else "/"


def _known_section(sid, schema):
    if os.geteuid() == 0:
        raise PermissionError("appearance is per user and never runs as root")
    secs = sections(schema)
    if not isinstance(sid, str) or not re.fullmatch(SECTION_RE, sid) or sid not in secs:
        raise ValueError(f"unknown section {str(sid)[:40]!r}")
    return secs


def apply_section(sid, schema=None, user_only=False):
    """Run /usr/libexec/forge-appearance-SID apply for a section from the schema.

    user_only=True (presets, only for the boot section): argv ["apply", "--user-only"], so the program changes
    only your own parts and never asks for the admin password.
    Returns (state, text): state is "applied", "failed" or "not-installed". ValueError for an unknown section.
    """
    schema = load_schema() if schema is None else schema
    secs = _known_section(sid, schema)
    if not backend_installed(sid):
        return "not-installed", "not installed yet"
    payload = _backend_payload(sid, schema, secs)
    env, cwd = _backend_env(sid)
    try:
        argv = [backend_path(sid), "apply"] + (["--user-only"] if user_only and sid == "boot" else [])
        rc, out = _run(argv, json.dumps(payload).encode(), BACKEND_TIMEOUT_S, env, cwd=cwd)
    except OSError as e:
        return "failed", f"could not start it ({e.strerror or e})"
    text = _scrub(out.decode("utf-8", "replace"))
    if rc is None:
        return "failed", (text + "\n" if text else "") + f"stopped: took longer than {BACKEND_TIMEOUT_S} seconds"
    return ("applied" if rc == 0 else "failed"), text


def section_choices(sid, schema=None):
    """Lists to pick from for the section's text fields that have "choices": "backend" (for example the
    Wallpaper Engine wallpapers in your Steam library). Runs /usr/libexec/forge-appearance-SID options,
    as you, with the same stdin and environment as apply. It answers on stdout with
    {"fields": {"<key>": {"choices": [{"value": "...", "label": "..."}], "note": "..."}}}.

    Returns (state, {key: {"choices": [...], "note": "..."}}, text). state: "ok", "none" (no such fields,
    nothing was run), "not-installed" or "failed". Every value is checked like a typed one, so a choice
    can never be a value the field would refuse. ValueError for an unknown section."""
    schema = load_schema() if schema is None else schema
    secs = _known_section(sid, schema)
    wanted = {f["key"]: f for f in secs[sid]["fields"] if f.get("choices") == "backend"}
    if not wanted:
        return "none", {}, ""
    if not backend_installed(sid):
        return "not-installed", {}, "not installed yet"
    payload = _backend_payload(sid, schema, secs)
    env, cwd = _backend_env(sid)
    try:
        rc, out = _run([backend_path(sid), "options"], json.dumps(payload).encode(), CHOICES_TIMEOUT_S, env,
                       cwd=cwd, merge=False, keep=CHOICES_OUTPUT_MAX + 1)
    except OSError as e:
        return "failed", {}, f"could not start it ({e.strerror or e})"
    if rc is None:
        return "failed", {}, f"stopped: took longer than {CHOICES_TIMEOUT_S} seconds"
    if rc != 0 or len(out) > CHOICES_OUTPUT_MAX:
        return "failed", {}, "no list (the program failed)"
    try:
        raw = json.loads(out.decode("utf-8"))
    except (ValueError, RecursionError):
        return "failed", {}, "no list (not JSON)"
    given = raw.get("fields") if isinstance(raw, dict) else None
    if not isinstance(given, dict):
        return "failed", {}, "no list (no fields in it)"
    data = {}
    for key, field in wanted.items():
        item = given.get(key)
        if not isinstance(item, dict):
            continue
        choices, seen = [], set()
        for c in item.get("choices") if isinstance(item.get("choices"), list) else []:
            if len(choices) >= CHOICES_MAX:
                break
            if not isinstance(c, dict) or not isinstance(c.get("label"), str):
                continue
            try:
                value = coerce(field, c.get("value"), media={})
            except ValueError:
                continue
            label = _clean_text(c["label"], 10 * CHOICE_LABEL_MAX)
            if value in seen or not label:
                continue
            seen.add(value)
            choices.append({"value": value, "label": label[:CHOICE_LABEL_MAX]})
        note = item.get("note") if isinstance(item.get("note"), str) else ""
        note = (_clean_text(note, 10 * CHOICE_NOTE_MAX) or "")[:CHOICE_NOTE_MAX]
        data[key] = {"choices": choices, "note": note}
    return "ok", data, ""
