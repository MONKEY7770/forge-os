"""Thai and English for Forge Settings and the Forge Dashboard. Per user, never root.

String tables: /usr/share/forge/i18n/<lang>.json, {"lang": "th", "name": "...", "strings": {"<id>": "<text>"}}.
The language is your choice in Forge Settings ($XDG_CONFIG_HOME/forge/ui.json), or else the system locale.
The pages get the table as JSON (page_block) and translate with t(), tn() and tr() (see I18N_JS).
"""
import json
import os
import re
import stat
import tempfile

# /usr/lib/forge/forgei18n.py -> /usr/share/forge/i18n (the same layout in the repo's files/system).
I18N_DIR = os.environ.get("FORGE_I18N_DIR") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "share", "forge", "i18n"))
LANGS = ("en", "th")
NAMES = {"en": "English", "th": "ไทย"}
CHOICES = ("auto",) + LANGS
ID_RE = re.compile(r"[a-z0-9_.-]{1,120}")
TABLE_MAX = 2 * 1024 * 1024
PREF_MAX = 4096
TEXT_MAX = 4000


def _xdg_config():
    base = os.environ.get("XDG_CONFIG_HOME", "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".config")
    return base


def pref_path():
    return os.path.join(_xdg_config(), "forge", "ui.json")


def _read_small(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        data = f.read(limit + 1)
    return data if len(data) <= limit else None


def system_language(env=None):
    """"th" when the system locale is Thai, else "en". GNU order: LANGUAGE (its first entry) is used unless the
    locale is C or POSIX, then LC_ALL, LC_MESSAGES and LANG."""
    env = os.environ if env is None else env
    locale = env.get("LC_ALL") or env.get("LC_MESSAGES") or env.get("LANG") or ""
    first = locale
    if env.get("LANGUAGE") and locale.split(".")[0] not in ("C", "POSIX"):
        first = env["LANGUAGE"].split(":")[0]
    code = re.split(r"[_.@]", first, maxsplit=1)[0].lower()
    return "th" if code == "th" else "en"


def preference():
    """"auto", "en" or "th". Anything odd in the file means auto."""
    data = _read_small(pref_path(), PREF_MAX)
    try:
        v = json.loads(data.decode("utf-8")).get("language") if data else None
    except (ValueError, AttributeError, UnicodeDecodeError):
        return "auto"
    return v if v in CHOICES else "auto"


def set_preference(choice):
    """Save your choice ("auto", "en" or "th"), 0600 in a 0700 folder, atomically, never through a link."""
    if not isinstance(choice, str) or choice not in CHOICES:
        raise ValueError("language must be auto, en or th")
    path = pref_path()
    folder = os.path.dirname(path)
    os.makedirs(os.path.dirname(folder), mode=0o700, exist_ok=True)
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(folder)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"{folder} is not a private folder of yours")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(folder, 0o700)
    if os.path.islink(path):
        raise OSError(f"{path} is a link, so it was not changed")
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".ui.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump({"language": choice}, f)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def language():
    choice = preference()
    return system_language() if choice == "auto" else choice


def table(lang):
    """id -> text for a language. A missing or broken file gives {} (the page then shows English)."""
    if lang not in LANGS:
        raise ValueError("unknown language")
    data = _read_small(os.path.join(I18N_DIR, lang + ".json"), TABLE_MAX)
    try:
        raw = json.loads(data.decode("utf-8")) if data else None
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}
    strings = raw.get("strings") if isinstance(raw, dict) else None
    if not isinstance(strings, dict):
        return {}
    if not all(isinstance(k, str) and ID_RE.fullmatch(k) and isinstance(v, str) and len(v) <= TEXT_MAX
               for k, v in strings.items()):
        return {}
    return strings


def page_data(lang, strings=None):
    return {"lang": lang, "pref": preference(), "names": NAMES,
            "strings": table(lang) if strings is None else strings, "fallback": table("en")}


def _safe_json(obj):
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    for a, b in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), (" ", "\\u2028"),
                 (" ", "\\u2029")):
        text = text.replace(a, b)
    return text


def page_json(lang, strings=None):
    """The tables as JSON that is safe inside <script type="application/json">."""
    return _safe_json(page_data(lang, strings))


def page_block(lang, nonce):
    """What forge-settings puts at <!--__I18N__--> in a page: the tables and the small runtime."""
    return (f'<script type="application/json" id="forge-i18n" nonce="{nonce}">{page_json(lang)}</script>\n'
            f'<script nonce="{nonce}">{I18N_JS}</script>')


# The runtime in the pages. Text only ever goes into the page with textContent or attributes.
I18N_JS = r"""
"use strict";
const I18N = {lang: "en", pref: "auto", names: {}, strings: {}, fallback: {}, exact: null, patterns: null};
function setI18n(data) {
  I18N.lang = data.lang === "th" ? "th" : "en";
  I18N.pref = data.pref || "auto";
  I18N.names = data.names || {};
  I18N.strings = data.strings || {};
  I18N.fallback = data.fallback || {};
  I18N.exact = new Map(); I18N.patterns = [];
  Object.keys(I18N.fallback).forEach(id => {
    if (!id.startsWith("msg.")) return;
    const en = I18N.fallback[id];
    if (!/\{[a-z_][a-z0-9_]*\}/.test(en)) { I18N.exact.set(en, id); return; }
    // A template as literal parts with the placeholder names between them: lits.length === names.length + 1.
    const lits = [], names = [];
    let rest = en, m;
    while ((m = /\{([a-z_][a-z0-9_]*)\}/.exec(rest))) { lits.push(rest.slice(0, m.index)); names.push(m[1]); rest = rest.slice(m.index + m[0].length); }
    lits.push(rest);
    I18N.patterns.push({lits, id, names, len: en.length});
  });
  I18N.patterns.sort((a, b) => b.len - a.len);
  document.documentElement.lang = I18N.lang;
}
function fillVars(text, vars) {
  return vars ? text.replace(/\{([a-z_][a-z0-9_]*)\}/g, (m, n) => (n in vars ? String(vars[n]) : m)) : text;
}
// The text for an id in the chosen language, else English, else the id itself.
function t(id, vars) {
  const s = Object.prototype.hasOwnProperty.call(I18N.strings, id) ? I18N.strings[id]
          : Object.prototype.hasOwnProperty.call(I18N.fallback, id) ? I18N.fallback[id] : id;
  return fillVars(s, vars);
}
// Counted text: <id>.one when n is 1, otherwise <id>.other. {n} is filled in.
function tn(id, n, vars) { return t(id + (n === 1 ? ".one" : ".other"), Object.assign({n}, vars || {})); }
// A label from a schema: its table entry if there is one, else the English text the schema gives.
function tl(id, english) {
  return Object.prototype.hasOwnProperty.call(I18N.strings, id) ? I18N.strings[id] : english;
}
// Matches a template without regular expressions (no backtracking): the text must start and end with the
// first and last literal parts; each middle literal is the first one found after the previous value, and every
// value is at least one character, like a lazy "(.+?)". Linear in the length of the line.
function matchTemplate(p, body) {
  const L = p.lits, first = L[0], last = L[L.length - 1];
  if (body.length < first.length + last.length + p.names.length || !body.startsWith(first) || !body.endsWith(last)) return null;
  const end = body.length - last.length, vals = [];
  let pos = first.length;
  for (let i = 1; i < L.length - 1; i++) {
    const at = L[i] ? body.indexOf(L[i], pos + 1) : pos + 1;
    if (at < 0 || at > end) return null;
    vals.push(body.slice(pos, at));
    pos = at + L[i].length;
  }
  if (end <= pos) return null;
  vals.push(body.slice(pos, end));
  return vals;
}
const TR_MAX = 500;      // longer lines are data (a log or a report line): left as they are
function trLine(line, depth) {
  if (I18N.lang === "en" || !line || depth > 3 || line.length > TR_MAX) return line;
  const lead = line.match(/^\s*/)[0], body = line.slice(lead.length);
  const id = I18N.exact.get(body);
  if (id) return lead + t(id);
  for (const p of I18N.patterns) {
    const vals = matchTemplate(p, body);
    if (!vals) continue;
    const vars = {};
    p.names.forEach((n, i) => { vars[n] = trLine(vals[i], depth + 1); });
    return lead + t(p.id, vars);
  }
  return line;
}
// Text from the server (English): each line that matches a msg.* entry is translated, other lines stay.
function tr(text) { return text == null ? text : String(text).split("\n").map(l => trLine(l, 0)).join("\n"); }
// Fills elements marked with data-i18n (text), data-i18n-title, -placeholder and -aria-label.
// (Without a table entry the English already in the page stays.)
function known(id) { return id in I18N.strings || id in I18N.fallback; }
function applyStatic(root) {
  const r = root || document;
  r.querySelectorAll("[data-i18n]").forEach(e => { if (known(e.dataset.i18n)) e.textContent = t(e.dataset.i18n); });
  [["data-i18n-title", "title"], ["data-i18n-placeholder", "placeholder"], ["data-i18n-aria-label", "aria-label"]]
    .forEach(([a, attr]) => r.querySelectorAll("[" + a + "]").forEach(e => {
      if (known(e.getAttribute(a))) e.setAttribute(attr, t(e.getAttribute(a)));
    }));
  if (r === document) { const tt = document.querySelector("title[data-i18n]"); if (tt && known(tt.dataset.i18n)) document.title = t(tt.dataset.i18n); }
}
(() => {
  const box = document.getElementById("forge-i18n");
  let data = {};
  try { data = JSON.parse(box ? box.textContent : "{}"); } catch (e) { data = {}; }
  setI18n(data);
})();
"""
