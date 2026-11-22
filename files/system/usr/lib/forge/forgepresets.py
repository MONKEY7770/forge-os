"""Appearance presets for Forge OS: your whole look under one name. Per user, never root.

A preset holds the values of every appearance section and refers to the pictures and videos it uses in your
media library. Presets live in $XDG_DATA_HOME/forge/appearance/presets/<id>.json (0600, folder 0700).

  save_current(name)        the look you have saved now (Forge Settings, Appearance) as a new preset
  apply(id)                 saves the preset's values and runs every section's program (like "Save and apply")
  rename(id, name), delete(id)
  export(id, file)          one .forgelook file (a zip: forge-look.json and media/<n>.<ext>) to share
  import_file(file)         strictly checked; pictures and videos go through the normal media library checks.
                            Importing only stores the preset: it never runs anything.
  sync()                    copies presets to and from <settings sync folder>/forge-looks-<user>/

The boot splash and login screen change the whole PC and need the admin password, so a preset only changes
them when you ask for it (include_system). Values that belong to one PC (a screen's connector name, an
installed theme or Wallpaper Engine item) are only used when this PC lists them.
"""
import copy
import hashlib
import json
import os
import pwd
import re
import secrets
import stat
import struct
import tempfile
import time
import unicodedata
import zipfile

import forgeappearance as fa
import forgecfg

ID_RE = re.compile(r"[0-9a-f]{32}")
NAME_MAX = 60
PRESETS_MAX = 200                   # your own presets (saved or imported here)
SYNCED_MAX = 50                     # presets brought in by sync, counted on their own
DOC_MAX = 256 * 1024
MEDIA_MAX = 16
IMPORT_MAX = 8 * 1024 ** 3          # one .forgelook file
CD_MAX = 64 * 1024                  # its zip directory
SYNC_FILES_MAX = 200
FORMAT = "forge-look"
DOC_NAME = "forge-look.json"
SUFFIX = ".forgelook"
TIME_MAX = 1e11
CHUNK = 1024 * 1024
DOC_KEYS = {"format", "version", "id", "name", "created", "updated", "values", "media"}
MEDIA_KEYS = {"file", "name", "kind", "format", "size", "sha256"}
MEMBER_RE = re.compile(r"media/(0|[1-9][0-9]?)\.([a-z0-9]{2,4})")
# These change the whole PC (pkexec, admin password): only changed when you ask for it.
SYSTEM_FIELDS = {"boot": ("splash", "splash_media", "splash_play", "splash_length", "splash_size",
                          "splash_background", "splash_logo", "login_screen", "login_media")}


class PresetError(Exception):
    """A preset could not be saved, read, imported or synced. The text is for the user."""


# Names, ids and files

def _check_id(pid):
    if not isinstance(pid, str) or not ID_RE.fullmatch(pid):
        raise PresetError("no such preset")
    return pid


def clean_name(name):
    if not isinstance(name, str):
        raise PresetError("give the preset a name")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") for c in name):
        raise PresetError("the name can't have line breaks, control or invisible characters")
    name = " ".join(name.split())
    if not name:
        raise PresetError("give the preset a name")
    if len(name) > NAME_MAX:
        raise PresetError(f"the name is too long (at most {NAME_MAX} characters)")
    return name


def presets_dir():
    return os.path.join(fa.data_dir(), "presets")


def preset_path(pid):
    return os.path.join(presets_dir(), _check_id(pid) + ".json")


def _private_presets_dir():
    os.makedirs(os.path.dirname(fa.data_dir()), mode=0o700, exist_ok=True)
    fa._private_dir(fa.data_dir())
    return fa._private_dir(presets_dir())


def _valid_doc(doc, pid=None):
    return (isinstance(doc, dict) and isinstance(doc.get("id"), str) and ID_RE.fullmatch(doc["id"])
            and (pid is None or doc["id"] == pid) and isinstance(doc.get("name"), str)
            and isinstance(doc.get("values"), dict) and isinstance(doc.get("media"), dict)
            and all(isinstance(v, dict) for v in doc["values"].values())
            and fa._is_num(doc.get("created")) and fa._is_num(doc.get("updated")))


def _read(pid):
    doc = fa._read_json(preset_path(pid), DOC_MAX)
    return doc if _valid_doc(doc, pid) else None


def get(pid):
    doc = _read(_check_id(pid))
    if doc is None:
        raise PresetError("no such preset")
    return doc


def _all():
    try:
        names = sorted(os.listdir(presets_dir()))
    except OSError:
        return []
    out = []
    for n in names[:PRESETS_MAX * 4]:
        if n.endswith(".json") and ID_RE.fullmatch(n[:-5]):
            doc = _read(n[:-5])
            if doc is not None:
                out.append(doc)
    return out


def _count(origin):
    return sum(1 for d in _all() if d.get("origin", "local") == origin)


def _unique_name(name, skip=None):
    taken = {d["name"].casefold() for d in _all() if d["id"] != skip}
    if name.casefold() not in taken:
        return name
    for n in range(2, 1000):
        tail = f" ({n})"
        cand = name[:NAME_MAX - len(tail)].rstrip() + tail
        if cand.casefold() not in taken:
            return cand
    raise PresetError("too many presets with this name")


def _write(doc):
    _private_presets_dir()
    fa._write_json_atomic(preset_path(doc["id"]), doc)


def system_differs(doc, current):
    """True when the preset's boot splash or login screen differ from the values you have now."""
    for sid, keys in SYSTEM_FIELDS.items():
        given = doc["values"].get(sid, {})
        for k in keys:
            if k in given and k in current.get(sid, {}) and given[k] != current[sid][k]:
                return True
    return False


def _look(values):
    """A few values for the small picture of a preset in Forge Settings (only checked, plain values)."""
    def get(sid, key, pattern):
        v = values.get(sid, {}).get(key)
        return v if isinstance(v, str) and re.fullmatch(pattern, v) else None
    bg_type = get("background", "type", r"[a-z_]{1,20}")
    mode = get("theme", "scheme_mode", r"[a-z]{1,10}")
    scheme = get("theme", "scheme", r"[^\x00-\x1f]{1,100}") or ""
    dark = mode == "dark" or (mode == "custom" and re.search(r"dark|night|black|vgui", scheme, re.I) is not None)
    return {"picture": get("background", "picture", ID_RE.pattern) if bg_type == "picture" else None,
            "color": get("background", "color", r"#[0-9a-f]{6}") if bg_type in ("color", "gradient") else None,
            "scheme": "dark" if dark else "light" if mode in ("light", "custom") else None,
            "accent": get("theme", "accent_color", r"#[0-9a-f]{6}") if get("theme", "accent", r"[a-z]+") == "custom" else None,
            "dock": get("dock", "style", r"[a-z]{1,20}")}


def summary(doc, current=None):
    return {"id": doc["id"], "name": doc["name"], "created": doc["created"], "updated": doc["updated"],
            "sections": sorted(doc["values"]), "media_count": len(doc["media"]),
            "system": bool(current is not None and system_differs(doc, current)), "look": _look(doc["values"]),
            "origin": "sync" if doc.get("origin") == "sync" else "local"}


def list_presets(schema=None):
    """Every preset, newest change first."""
    current = fa.load(schema) if schema is not None else None
    docs = sorted(_all(), key=lambda d: (-d["updated"], d["name"].casefold()))
    return [summary(d, current) for d in docs]


def _sha256_of(path):
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _media_fields(schema):
    for s in schema["sections"]:
        for f in s["fields"]:
            if f["type"] == "media":
                yield s["id"], f


def save_current(name, schema=None):
    """Your saved appearance values now, with the media they use, as a new preset."""
    schema = fa.load_schema() if schema is None else schema
    name = clean_name(name)
    if _count("local") >= PRESETS_MAX:
        raise PresetError(f"you have {PRESETS_MAX} presets, delete one first")
    values = fa.load(schema)
    index = fa.media_index()
    media = {}
    for sid, f in _media_fields(schema):
        mid = values[sid].get(f["key"])
        path = fa.media_path(mid) if mid else None
        if path and mid not in media:
            it = index[mid]
            media[mid] = {"name": it["name"], "kind": it["kind"], "format": it["format"], "size": it["size"],
                          "sha256": _sha256_of(path)}
    now = time.time()
    doc = {"version": 1, "id": secrets.token_hex(16), "name": _unique_name(name), "created": now, "updated": now,
           "values": values, "media": media, "origin": "local"}
    _write(doc)
    return doc


def rename(pid, name):
    doc = get(pid)
    doc["name"] = _unique_name(clean_name(name), skip=pid)
    doc["updated"] = max(time.time(), doc["updated"] + 0.001)
    _write(doc)
    return doc


def delete(pid):
    """Removes the preset (its pictures and videos stay in your media library). Also takes it out of the sync
    folder, and remembers it, so a sync never brings it back to this PC."""
    _check_id(pid)
    doc = _read(pid)
    try:
        os.unlink(preset_path(pid))
    except FileNotFoundError:
        return False
    if doc is not None and doc.get("origin") == "sync":
        _remove_own_media(doc)
    st = _sync_state()
    if pid not in st["deleted"]:
        st["deleted"] = (st["deleted"] + [pid])[-1000:]
    st["written"].pop(pid, None)
    _save_sync_state(st)
    ufd, _ = _sync_dir(create=False)
    if ufd is not None:
        try:
            os.unlink(pid + SUFFIX, dir_fd=ufd)
        except OSError:
            pass
        finally:
            os.close(ufd)
    return True


def _remove_own_media(doc):
    """After a synced preset is deleted: remove the pictures and videos its import added to the library, unless
    another preset or your current settings use them. Never media you added yourself."""
    added = doc.get("added_media") if isinstance(doc.get("added_media"), list) else []
    used = set()
    for other in _all():
        used |= set(other["media"])
    try:
        schema = fa.load_schema()
        current = fa.load(schema)
        for sid, f in _media_fields(schema):
            used.add(current[sid].get(f["key"]))
    except (OSError, fa.MediaError):
        return                              # can't be sure what is in use: keep everything
    for mid in added:
        if isinstance(mid, str) and ID_RE.fullmatch(mid) and mid in doc["media"] and mid not in used:
            fa.media_remove(mid)


# Applying

def _choices(sid, schema):
    try:
        return fa.section_choices(sid, schema)
    except (ValueError, PermissionError, OSError):
        return "failed", {}, ""


def plan(doc, include_system=False, schema=None, choices=None):
    """(the values to save, notes for you). Fields the preset leaves out, media no longer in the library and
    values this PC doesn't list keep what you have now."""
    schema = fa.load_schema() if schema is None else schema
    choices = choices or _choices
    current = fa.load(schema)
    index = fa.media_index()
    new = copy.deepcopy(current)
    notes, kept_system = [], False
    for sec in schema["sections"]:
        sid = sec["id"]
        given = doc["values"].get(sid, {})
        for f in sec["fields"]:
            key = f["key"]
            if key not in given:
                continue
            v = given[key]
            if key in SYSTEM_FIELDS.get(sid, ()) and not include_system:
                kept_system |= v != current[sid][key]
                continue
            if f["type"] == "media" and v and not fa.media_path(v):
                was = doc["media"].get(v, {}).get("name") if isinstance(v, str) else None
                notes.append(f"{sec['label']}, {f['label']}: {was or 'the file'} is no longer in your media library, "
                             "so it stays as it is.")
                continue
            try:
                new[sid][key] = fa.coerce(f, v, media=index)
            except ValueError:
                notes.append(f"{sec['label']}, {f['label']}: the saved value can't be used, so it stays as it is.")
    for sec in schema["sections"]:
        sid = sec["id"]
        check = [f for f in sec["fields"] if f.get("choices") == "backend"
                 and new[sid][f["key"]] not in ("", current[sid][f["key"]])]
        if not check:
            continue
        state, lists, _ = choices(sid, schema)
        for f in check:
            listed = {c["value"] for c in (lists.get(f["key"]) or {}).get("choices", [])} if state == "ok" else set()
            v = new[sid][f["key"]]
            if v in listed:
                continue
            new[sid][f["key"]] = current[sid][f["key"]]
            parent = (f.get("show_if") or {}).get("key")
            if parent:
                new[sid][parent] = current[sid][parent]
            notes.append(f"{sec['label']}, {f['label']}: {v} is not on this PC, so it stays as it is.")
    if kept_system:
        notes.append("The boot splash and login screen stay as they are: changing them needs the admin password.")
    return new, notes


def apply(pid, include_system=False, schema=None, choices=None):
    """Save the preset's values and run every section's program. Returns {"ok", "results", "notes", "values"}."""
    schema = fa.load_schema() if schema is None else schema
    doc = get(pid)
    new, notes = plan(doc, include_system, schema, choices)
    try:
        values, errors = fa.save_checked(new, schema)
    except (OSError, fa.MediaError) as e:
        raise PresetError(f"not saved ({e})")
    if errors:
        raise PresetError("not saved: " + errors[0])
    results = []
    for sec in schema["sections"]:
        # Without the tick box the boot program runs in its user-only mode: it never asks for a password,
        # even when the system state differs from your saved values (for example after a cancelled prompt).
        state, text = fa.apply_section(sec["id"], schema, user_only=not include_system)
        results.append({"section": sec["id"], "state": state, "output": text})
    return {"ok": all(r["state"] != "failed" for r in results), "results": results, "notes": notes,
            "values": values}


# Export

def export(pid, out):
    """Write the preset as a .forgelook zip into the writable binary file out."""
    doc = get(pid)
    schema = fa.load_schema()
    index = fa.media_index()
    mapping, meta = {}, []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for sid, f in _media_fields(schema):
            mid = doc["values"].get(sid, {}).get(f["key"])
            if not mid or mid in mapping:
                continue
            path = fa.media_path(mid)
            if not path:
                continue
            item = index[mid]
            ext = item["file"].rsplit(".", 1)[1]
            name = f"media/{len(meta)}.{ext}"
            h, size = hashlib.sha256(), 0
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as src, z.open(info, "w", force_zip64=True) as dst:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    size += len(chunk)
                    dst.write(chunk)
            mapping[mid] = f"media:{len(meta)}"
            meta.append({"file": name, "name": item["name"], "kind": item["kind"], "format": item["format"],
                         "size": size, "sha256": h.hexdigest()})
        values = {}
        for sec in schema["sections"]:
            given = doc["values"].get(sec["id"])
            if not isinstance(given, dict):
                continue
            values[sec["id"]] = {}
            for f in sec["fields"]:
                if f["key"] in given:
                    v = given[f["key"]]
                    values[sec["id"]][f["key"]] = (mapping.get(v, "") if f["type"] == "media" else v)
        body = {"format": FORMAT, "version": 1, "id": doc["id"], "name": doc["name"], "created": doc["created"],
                "updated": doc["updated"], "values": values, "media": meta}
        info = zipfile.ZipInfo(DOC_NAME, (1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (stat.S_IFREG | 0o600) << 16
        z.writestr(info, json.dumps(body, ensure_ascii=False, indent=1))


def export_name(pid):
    """A file name for the download: the preset's name with only safe characters."""
    # Letters, marks (Thai vowels and tone marks) and digits of any script, and a few safe signs.
    name = "".join(c for c in get(pid)["name"]
                   if unicodedata.category(c)[0] in "LMN" or c in " .()-_").strip(" .") or "preset"
    return name[:NAME_MAX] + SUFFIX


# Import

def _zip_directory(f):
    """Checks the end of the zip before Python reads its directory: few entries, a small directory."""
    f.seek(0, os.SEEK_END)
    size = f.tell()
    if size > IMPORT_MAX:
        raise PresetError(f"the file is too large (at most {IMPORT_MAX // 1024 ** 3} GiB)")
    if size < 22:
        raise PresetError("this is not a Forge preset file")
    f.seek(0)
    if f.read(4) != b"PK\x03\x04":
        raise PresetError("this is not a Forge preset file")
    tail_len = min(size, 65536 + 22)
    f.seek(size - tail_len)
    tail = f.read(tail_len)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        raise PresetError("this is not a Forge preset file")
    _, _, _, _, entries, cd_size, cd_off, _ = struct.unpack("<4s4H2LH", tail[at:at + 22])
    if entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
        loc = at - 20
        if loc < 0 or tail[loc:loc + 4] != b"PK\x06\x07":
            raise PresetError("this is not a Forge preset file")
        _, _, z64_off, _ = struct.unpack("<4sLQL", tail[loc:loc + 20])
        if z64_off > size - 56:
            raise PresetError("this is not a Forge preset file")
        f.seek(z64_off)
        rec = f.read(56)
        if rec[:4] != b"PK\x06\x06":
            raise PresetError("this is not a Forge preset file")
        entries, cd_size = struct.unpack("<Q", rec[32:40])[0], struct.unpack("<Q", rec[40:48])[0]
    if entries > MEDIA_MAX + 1 or cd_size > CD_MAX:
        raise PresetError("the file has too many parts to be a Forge preset")
    f.seek(0)
    return size


def _members(z, size):
    """name -> ZipInfo, after checking every entry: only forge-look.json and media/<n>.<ext>, each once, plain
    files, never encrypted, media stored as they are (no compression, so no zip bomb), sizes capped."""
    out, total = {}, 0
    for info in z.infolist():
        name = info.filename
        kind = (info.external_attr >> 16) & 0o170000
        if name in out:
            raise PresetError("the file has a part twice")
        if kind not in (0, stat.S_IFREG) or name.endswith("/") or info.flag_bits & 0x1:
            raise PresetError(f"the file has a part that is not a plain file ({name[:60]})")
        if name == DOC_NAME:
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or info.file_size > DOC_MAX:
                raise PresetError("the preset description is too large")
        else:
            m = MEMBER_RE.fullmatch(name)
            if not m or int(m.group(1)) >= MEDIA_MAX or m.group(2) not in fa.MEDIA_EXTS:
                raise PresetError(f"the file has a part Forge does not know ({name[:60]})")
            if info.compress_type != zipfile.ZIP_STORED or info.file_size != info.compress_size:
                raise PresetError("pictures and videos in a preset file must not be compressed")
            if info.file_size > fa.FILE_CAP:
                raise PresetError("a picture or video in it is too large")
        total += info.compress_size
        out[name] = info
    if total > size or DOC_NAME not in out:
        raise PresetError("this is not a Forge preset file")
    return out


def _parse_doc(z, members, schema):
    with z.open(members[DOC_NAME]) as f:
        data = f.read(DOC_MAX + 1)
    if len(data) > DOC_MAX:
        raise PresetError("the preset description is too large")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise PresetError("the preset description is not readable")
    if not isinstance(doc, dict) or set(doc) != DOC_KEYS:
        raise PresetError("the preset description has unknown or missing parts")
    if doc["format"] != FORMAT or doc["version"] != 1 or type(doc["version"]) is not int:
        raise PresetError("this preset is from a different Forge version")
    if not isinstance(doc["id"], str) or not ID_RE.fullmatch(doc["id"]):
        raise PresetError("the preset has a bad id")
    name = clean_name(doc["name"])
    for k in ("created", "updated"):
        if not fa._is_num(doc[k]) or not 0 <= doc[k] < TIME_MAX:
            raise PresetError("the preset has a bad date")
    media = doc["media"]
    if not isinstance(media, list) or len(media) > MEDIA_MAX:
        raise PresetError("the preset lists too many pictures and videos")
    for i, m in enumerate(media):
        if not isinstance(m, dict) or set(m) != MEDIA_KEYS:
            raise PresetError("a picture or video in the preset has unknown or missing details")
        fmt = m["format"]
        ext = m["file"].rsplit(".", 1)[-1] if isinstance(m["file"], str) else ""
        if (fmt not in fa.FORMATS or m["file"] != f"media/{i}.{ext}" or ext not in fa.FORMATS[fmt][0]
                or m["kind"] not in fa.KINDS or not isinstance(m["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", m["sha256"]) or type(m["size"]) is not int
                or m["file"] not in members or members[m["file"]].file_size != m["size"]
                or not isinstance(m["name"], str) or not fa._clean_name(m["name"])):
            raise PresetError(f"picture or video {i + 1} in the preset does not match the file")
    listed = {m["file"] for m in media}
    if set(members) - listed - {DOC_NAME}:
        raise PresetError("the file has a part the preset does not list")
    values, refs = {}, {}
    if not isinstance(doc["values"], dict):
        raise PresetError("the preset's settings are not readable")
    secs = fa.sections(schema)
    for sid, given in doc["values"].items():
        if sid not in secs or not isinstance(given, dict):
            raise PresetError(f"the preset has a part this Forge does not know ({str(sid)[:40]})")
        fields = {f["key"]: f for f in secs[sid]["fields"]}
        values[sid] = {}
        for key, v in given.items():
            f = fields.get(key)
            if f is None:
                raise PresetError(f"the preset has a setting this Forge does not know ({sid}.{str(key)[:40]})")
            if f["type"] == "media":
                if v == "":
                    values[sid][key] = ""
                    continue
                m = re.fullmatch(r"media:(0|[1-9][0-9]?)", v) if isinstance(v, str) else None
                if not m or int(m.group(1)) >= len(media):
                    raise PresetError(f"{sid}.{key}: refers to a picture or video that is not in the file")
                refs.setdefault(int(m.group(1)), []).append(f)
                values[sid][key] = v
                continue
            try:
                values[sid][key] = fa.coerce(f, v, media={})
            except ValueError as e:
                raise PresetError(f"{sid}.{e}")
    return doc, name, values, refs


class _Hashing:
    """A reader that hashes what passes through it."""

    def __init__(self, f):
        self.f, self.h = f, hashlib.sha256()

    def read(self, n=-1):
        data = self.f.read(n)
        self.h.update(data)
        return data


def _library_match(size, sha):
    for mid, it in fa.media_index().items():
        if it["size"] == size:
            path = fa.media_path(mid)
            if path and _sha256_of(path) == sha:
                return mid
    return None


def import_file(f, keep_id=False, schema=None, origin="local"):
    """Check a .forgelook file and store it as a preset. Its pictures and videos are added to your media
    library through the normal checks (or matched to files you already have). Nothing is run."""
    schema = fa.load_schema() if schema is None else schema
    size = _zip_directory(f)
    try:
        z = zipfile.ZipFile(f)
    except (zipfile.BadZipFile, ValueError, OSError, EOFError, NotImplementedError):
        raise PresetError("this is not a Forge preset file")
    added = []
    try:
        with z:
            members = _members(z, size)
            doc, name, values, refs = _parse_doc(z, members, schema)
            ids = {}
            for i, fields in sorted(refs.items()):
                m = doc["media"][i]
                info = members[m["file"]]
                with z.open(info) as src:              # first: the bytes must be the ones the preset lists
                    r = _Hashing(src)
                    while r.read(CHUNK):
                        pass
                if r.h.hexdigest() != m["sha256"]:
                    raise PresetError(f"{m['name']} was changed after the preset was saved")
                mid = _library_match(m["size"], m["sha256"])
                if mid is None:
                    with z.open(info) as src:
                        try:
                            item = fa.media_add_stream(src, info.file_size, m["name"])
                        except fa.MediaError as e:
                            raise PresetError(f"{m['name']}: {e}")
                    added.append(item["id"])
                    if item["format"] != m["format"] or item["kind"] != m["kind"]:
                        raise PresetError(f"{m['name']} is not the kind of file the preset says")
                    mid = item["id"]
                ids[i] = mid
            index = fa.media_index()
            secs = fa.sections(schema)
            for sid, given in values.items():
                fields = {f["key"]: f for f in secs[sid]["fields"]}
                for key, v in given.items():
                    if fields[key]["type"] == "media" and v:
                        given[key] = ids[int(v.split(":")[1])]
                        try:
                            fa.coerce(fields[key], given[key], media=index)
                        except ValueError as e:
                            raise PresetError(f"{sid}.{e}")
            media = {}
            for i, mid in ids.items():
                m = doc["media"][i]
                media[mid] = {"name": index[mid]["name"], "kind": index[mid]["kind"], "format": index[mid]["format"],
                              "size": m["size"], "sha256": m["sha256"]}
            pid = doc["id"] if keep_id else secrets.token_hex(16)
            if origin == "sync" and _count("sync") >= SYNCED_MAX:
                raise PresetError(f"{SYNCED_MAX} presets came in by sync already, delete some first")
            if origin != "sync" and _count("local") >= PRESETS_MAX:
                raise PresetError(f"you have {PRESETS_MAX} presets, delete one first")
            now = time.time()
            out = {"version": 1, "id": pid, "name": _unique_name(name, skip=pid if keep_id else None),
                   "created": doc["created"] if keep_id else now, "updated": doc["updated"] if keep_id else now,
                   "values": values, "media": media, "origin": "sync" if origin == "sync" else "local"}
            if origin == "sync":
                out["added_media"] = list(added)
            _write(out)
            return out
    except (zipfile.BadZipFile, EOFError, OSError, ValueError, KeyError, NotImplementedError) as e:
        for mid in added:
            fa.media_remove(mid)
        if isinstance(e, OSError) and not isinstance(e, zipfile.BadZipFile):
            raise PresetError(f"the file could not be read ({e.strerror or e})")
        raise PresetError("this is not a Forge preset file (it is damaged)")
    except PresetError:
        for mid in added:
            fa.media_remove(mid)
        raise


# Sync through the settings sync folder (Forge Settings, Data, Settings sync)

def user_name():
    try:
        name = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        name = ""
    name = re.sub(r"[^a-z0-9_-]", "_", name.lower())[:32]
    return name or str(os.getuid())


def _sync_state_path():
    base = os.environ.get("XDG_STATE_HOME", "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "forge", "appearance-presets-sync.json")


def _sync_state():
    raw = fa._read_json(_sync_state_path(), 1024 * 1024)
    st = {"written": {}, "deleted": [], "seen": {}}
    if isinstance(raw, dict):
        for key in ("written", "seen"):
            if isinstance(raw.get(key), dict):
                st[key] = {k: v for k, v in list(raw[key].items())[-5000:] if ID_RE.fullmatch(str(k)) and fa._is_num(v)}
        if isinstance(raw.get("deleted"), list):
            st["deleted"] = [x for x in raw["deleted"] if isinstance(x, str) and ID_RE.fullmatch(x)][-1000:]
    return st


def _save_sync_state(st):
    p = _sync_state_path()
    try:
        os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
        fa._write_json_atomic(p, st)
    except OSError:
        pass


# Filesystems without real Unix permissions: every file shows the owner and mode of the mount options
# (a shared NTFS or FAT drive mounted with umask=000 shows 0777). There the owner and mode checks can't mean
# anything, so they are skipped; a synced change still never overwrites a preset, and the cap of 50 stays.
LOOSE_FS = ("vfat", "msdos", "exfat", "ntfs", "ntfs3", "fuseblk", "fuse.ntfs-3g")


def _unescape_mount(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def _fs_type(path):
    """The filesystem type of the mount that holds path (the longest matching mount point in mountinfo)."""
    data = fa._read_small(os.environ.get("FORGE_MOUNTINFO") or "/proc/self/mountinfo", 4 * 1024 * 1024)
    best, kind = "", None
    for line in (data or b"").decode("utf-8", "replace").splitlines():
        left, sep, right = line.partition(" - ")
        parts = left.split()
        if not sep or len(parts) < 5 or not right.split():
            continue
        point = _unescape_mount(parts[4])
        if (path == point or path.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
            best, kind = point, right.split()[0]
    return kind


def _sync_folder():
    """(real path of the sync folder, None) or (None, why not). Touches nothing."""
    try:
        vals = forgecfg.values(forgecfg.load_schema())
    except Exception:
        return None, "the settings could not be read"
    if vals.get("sync.enabled") is not True:
        return None, "Settings sync is off. Turn it on in Forge Settings, under Data, Settings sync."
    folder, problem = forgecfg.check_sync_folder(vals.get("sync.folder", ""))
    if problem or not folder:
        return None, "No sync folder is set. Choose one in Forge Settings, under Data, Settings sync."
    return os.path.realpath(os.environ.get("FORGE_ROOT", "") + folder), None


def sync_reduced():
    """True when the sync folder is on a drive without real permissions (see LOOSE_FS)."""
    real, _ = _sync_folder()
    return bool(real) and _fs_type(real) in LOOSE_FS


def _sync_dir(create=True):
    """(fd of <sync folder>/forge-looks-<user>, None) or (None, why not)."""
    try:
        vals = forgecfg.values(forgecfg.load_schema())
    except Exception:
        return None, "the settings could not be read"
    if vals.get("sync.enabled") is not True:
        return None, "Settings sync is off. Turn it on in Forge Settings, under Data, Settings sync."
    folder, problem = forgecfg.check_sync_folder(vals.get("sync.folder", ""))
    if problem or not folder:
        return None, "No sync folder is set. Choose one in Forge Settings, under Data, Settings sync."
    root = os.environ.get("FORGE_ROOT", "")
    real = os.path.realpath(root + folder)
    try:
        me = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        me = None
    roots = [os.path.realpath(root + os.path.expanduser("~")), os.path.realpath(root + "/mnt"),
             os.path.realpath(root + "/var/mnt")] + ([os.path.realpath(root + "/run/media/" + me)] if me else [])
    if not any(real.startswith(r + "/") for r in roots):
        return None, "The sync folder must be inside your home folder, /run/media/<you> or /mnt."
    try:
        dfd = os.open(real, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None, "The sync folder can't be reached right now (not plugged in or not mounted?)."
    sub = "forge-looks-" + user_name()
    try:
        if create:
            try:
                os.mkdir(sub, 0o700, dir_fd=dfd)
            except FileExistsError:
                pass
        ufd = os.open(sub, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
    except OSError:
        return None, f"{sub} in the sync folder is not a folder (a link?), so nothing was synced."
    finally:
        os.close(dfd)
    st = os.fstat(ufd)
    if _fs_type(real) not in LOOSE_FS and (st.st_uid != os.getuid() or st.st_mode & 0o022):
        os.close(ufd)
        return None, f"{sub} in the sync folder is not yours, or others can change it, so nothing was synced."
    return ufd, None


def sync_configured():
    """(True, None) when settings sync is on with a folder, else (False, why not). Touches no folder."""
    try:
        vals = forgecfg.values(forgecfg.load_schema())
    except Exception:
        return False, "the settings could not be read"
    if vals.get("sync.enabled") is not True:
        return False, "Settings sync is off. Turn it on in Forge Settings, under Data, Settings sync."
    folder, problem = forgecfg.check_sync_folder(vals.get("sync.folder", ""))
    if problem or not folder:
        return False, "No sync folder is set. Choose one in Forge Settings, under Data, Settings sync."
    return True, None


def scratch_file():
    """An unnamed temporary file in your private appearance folder (for exports and imports)."""
    os.makedirs(os.path.dirname(fa.data_dir()), mode=0o700, exist_ok=True)
    return tempfile.TemporaryFile(dir=fa._private_dir(fa.data_dir()), prefix=".preset-")


def _open_entry(ufd, name, loose=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=ufd)
    f = os.fdopen(fd, "rb")
    st = os.fstat(f.fileno())
    if not stat.S_ISREG(st.st_mode) or st.st_size > IMPORT_MAX:
        f.close()
        raise PresetError("not a regular file, or too large")
    if not loose and (st.st_uid != os.getuid() or st.st_mode & 0o022):
        f.close()
        raise PresetError("not yours, or others can change it")
    return f


def _private_copy(ufd, name, loose=False):
    """The file copied once into a private temporary file: it is checked and imported from this copy only,
    so nobody can change it between the check and the import."""
    copy = scratch_file()
    try:
        with _open_entry(ufd, name, loose) as src:
            left = IMPORT_MAX + 1
            while left > 0:
                chunk = src.read(min(CHUNK, left))
                if not chunk:
                    break
                copy.write(chunk)
                left -= len(chunk)
        if left <= 0:
            raise PresetError("too large")
        copy.seek(0)
        return copy
    except BaseException:
        copy.close()
        raise


def _peek(f):
    """The id, name and date in a .forgelook file, after the same checks as an import (nothing is added)."""
    size = _zip_directory(f)
    try:
        with zipfile.ZipFile(f) as z:
            members = _members(z, size)
            doc, _, _, _ = _parse_doc(z, members, fa.load_schema())
    except (zipfile.BadZipFile, EOFError, ValueError, KeyError, NotImplementedError):
        raise PresetError("damaged")
    return doc


def sync():
    """Send this PC's presets to the sync folder and bring in the ones from your other PCs.
    Returns {"ok", "message", "sent", "received", "skipped"}."""
    ufd, why = _sync_dir()
    loose = ufd is not None and sync_reduced()
    if ufd is None:
        return {"ok": False, "message": why, "sent": 0, "received": 0, "skipped": [], "reduced": False}
    st = _sync_state()
    sent, received, skipped = 0, 0, []
    try:
        local = {d["id"]: d for d in _all()}
        names = [n for n in sorted(os.listdir(ufd)) if not n.startswith(".")][:SYNC_FILES_MAX * 2]
        there = set()
        for n in names:
            pid = n[:-len(SUFFIX)] if n.endswith(SUFFIX) else ""
            if not ID_RE.fullmatch(pid):
                continue
            there.add(pid)
        # Bring in what is new or changed on another PC. A change to a preset you already have never replaces
        # it: it arrives as a separate copy (once per change).
        for pid in sorted(there)[:SYNC_FILES_MAX]:
            if pid in st["deleted"]:
                continue
            try:
                with _private_copy(ufd, pid + SUFFIX, loose) as f:
                    head = _peek(f)
                    if head["id"] != pid:
                        raise PresetError("its name and its content don't match")
                    seen = st["seen"].get(pid)
                    if seen is not None and head["updated"] <= seen:
                        continue
                    mine = local.get(pid)
                    if mine is not None and (head["updated"] <= mine["updated"]
                                             or st["written"].get(pid) == head["updated"]):
                        st["seen"][pid] = head["updated"]
                        continue
                    f.seek(0)
                    doc = import_file(f, keep_id=mine is None, origin="sync")
                st["seen"][pid] = head["updated"]
                local[doc["id"]] = doc
                received += 1
            except (PresetError, OSError) as e:
                skipped.append(f"{pid + SUFFIX}: {getattr(e, 'strerror', None) or e}")
        # Send what is new or changed here (only your own presets: what came in by sync is there already).
        for pid, doc in sorted(local.items()):
            if doc.get("origin") == "sync":
                continue
            if pid in there and st["written"].get(pid) == doc["updated"]:
                continue
            tmp = f".{pid}.{secrets.token_hex(4)}.tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                             dir_fd=ufd)
                with os.fdopen(fd, "wb") as out:
                    export(pid, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, pid + SUFFIX, src_dir_fd=ufd, dst_dir_fd=ufd)
                st["written"][pid] = doc["updated"]
                sent += 1
            except (PresetError, OSError, zipfile.LargeZipFile) as e:
                try:
                    os.unlink(tmp, dir_fd=ufd)
                except OSError:
                    pass
                skipped.append(f"{doc['name']}: not sent ({getattr(e, 'strerror', None) or e})")
    finally:
        os.close(ufd)
    _save_sync_state(st)
    return {"ok": True, "message": None, "sent": sent, "received": received, "skipped": skipped, "reduced": loose}
