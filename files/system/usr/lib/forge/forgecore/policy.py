"""Permission system for AI agents.

Every agent action gets one of three decisions: allow, ask or deny.
"ask" puts a request in an approval queue that a human answers.
Every decision and answer is written to an audit log.

Decisions come from ACTIONS (the defaults), then the "actions" overrides
in the config file, then the per-agent overrides. Deny always wins, and
an action that is not in ACTIONS is always denied.
"""
# Policy has a method called list, so type hints are kept as text.
from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import re
import urllib.parse
import sqlite3
import tempfile
import threading
import time
import unicodedata
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query

ACTIONS = {
    # action id: (description, default decision)
    "metrics.read": ("Read system stats", "allow"),
    "models.list": ("List AI models", "allow"),
    "settings.read": ("Read Forge settings", "allow"),
    "notify": ("Show a desktop notification", "allow"),
    "settings.write": ("Change a Forge setting", "ask"),
    "models.pull": ("Download an AI model", "ask"),
    "models.delete": ("Delete an AI model", "ask"),
    "web.fetch": ("Read a web page", "ask"),
    "ai.cloud": ("Use a paid cloud AI model", "ask"),
    "command.run": ("Run a shell command in a sandbox", "deny"),
    "files.read": ("Read files in the agent workspace", "allow"),
    "files.write": ("Write files in the agent workspace", "ask"),
}
DECISIONS = ("allow", "ask", "deny")
STATUSES = ("pending", "approved", "denied", "expired")

MAX_STR = 2000                 # per string in the audit log
MAX_APPROVAL_STR = 200_000     # per string in an approval, so the approver sees what will happen
MAX_APPROVAL_TOTAL = 200_000   # all strings of one approval together
HIDDEN = "[hidden]"

# How secret names are found. A name is split into words: "aws_secret_access_key",
# "AWS-Secret-Access-Key" and "awsSecretAccessKey" all become aws, secret, access, key.
# It is hidden if any word is in SECRET_WORDS, or two words next to each other make
# one of SECRET_PAIRS (api+key, private+key, ...).
# A plain "key" stays visible (setting names use it), and so do token counts like
# max_tokens or token_count. "password_hint" is hidden: people often type the real
# password as their hint.
SECRET_WORDS = {"password", "passwd", "passphrase", "secret", "token", "cookie", "credential",
                "credentials", "apikey", "auth", "authorization", "privatekey",
                "pwd", "pass", "jwt", "bearer", "otp", "totp", "hotp", "mnemonic", "session", "sessionid",
                "jsessionid", "phpsessid", "sshkey", "connectionstring", "seedphrase",
                "cookies", "passcode", "privkey", "apitoken", "pgpassword", "psk"}
# Weaker names: "pin" and "pw" only as the whole name, and "password" in other languages (written
# without accents: names are compared after accents are removed, so contraseña matches contrasena).
# In free text their value is only hidden when it looks like a secret (see _weak_value_hidden), so
# "GPIO pin: 17", "PW: 120W" and "Пароль: изменён" stay readable. As dict keys they are always hidden.
SOFT_NAMES = {"pin", "pw"}
FOREIGN_WORDS = {"passwort", "kennwort", "contrasena", "senha", "wachtwoord", "losenord", "salasana", "haslo",
                 "heslo", "jelszo", "sifre", "passord", "adgangskode", "parool", "пароль", "密码", "密碼",
                 "パスワード", "비밀번호", "รหสผาน", "พาสเวรด"}   # Thai: รหัสผ่าน, พาสเวิร์ด without marks
FOREIGN_PAIRS = {"depasse", "katasandi"}
THAI_PASSWORD_WORDS = ("รหสผาน", "พาสเวรด")    # รหัสผ่าน, พาสเวิร์ด without marks, also inside a longer name
# Only these names count when a tab is the only separator ("password<TAB>x"): "Session<TAB>42" and
# "secret<TAB>not set" are table columns, not secrets.
TAB_WORDS = {"password", "passwd", "passphrase", "pwd", "pgpassword", "apikey", "privatekey", "apitoken"}
TAB_PAIRS = {"apikey", "privatekey", "secretkey", "accesskey", "clientsecret", "authtoken", "accesstoken",
             "refreshtoken"}
SECRET_PAIRS = {"apikey", "privatekey", "accesskey", "secretkey", "sshkey", "signingkey", "seedphrase",
                "connectionstring", "databaseurl", "recoverykey", "encryptionkey", "masterkey", "sessionid",
                "totpseed", "otpseed", "privkey", "databaseuri", "recoverycodes", "recoverycode", "authtoken",
                "refreshtoken", "accesstoken", "clientsecret"}
# "token" next to one of these words is a count, not a secret.
COUNT_WORDS = {"count", "counts", "max", "min", "limit", "limits", "used", "usage", "total", "num",
               "number", "remaining", "budget", "prompt", "completion"}
# Extra words that mean a secret in a URL query, like ?key= or &sig=.
URL_SECRET_WORDS = {"key", "sig", "signature"}

_CAMEL_1 = re.compile(r"([a-z0-9])([A-Z])")       # accessToken -> access_Token
_CAMEL_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")    # APIKey -> API_Key
# scheme://user:password@host -> scheme://[hidden]@host
# (The scheme is bounded and must start a word, so long runs of letters can't make it slow.)
_URL_USERINFO = re.compile(r"(?<![A-Za-z0-9+.\-])([A-Za-z][A-Za-z0-9+.\-]{0,31}://)[^/?#\s@\"'<>]+@")
# ?name=value or &name=value
_URL_PARAM = re.compile(r"([?&;])([^=&#\s?;\"'<>]{1,128})=([^&#\s;\"'<>]*)")
# name=value or name: value in free text (commands, logs, file text): the value is hidden if the
# name looks secret. Quoted values may contain spaces.
_TEXT_ASSIGN = re.compile(r"(?<![A-Za-z0-9_.\-])([\"']?)([A-Za-z_][A-Za-z0-9_.\-]{0,63})\1(\s*[:=]>?\s*)"
                          r"(\"[^\"\n]*\"?|'[^'\n]*'?|\[[^\]\n]*\]?|[^\s,;&\"'<>]+)")
# (A quote that never closes, as in text that was cut off, runs to the end of the line.)
# --password=x, --password x, --auth-token x on command lines.
_CLI_FLAG = re.compile(r"(?<![A-Za-z0-9_\-])(--?[A-Za-z][A-Za-z0-9_\-]{0,63})(=|\s+)(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)")
# Private key blocks and well-known token formats, wherever they appear.
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----")
PEM_MAX = 20_000   # a private key block is never longer than this
_KNOWN_TOKENS = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|sk-ant-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}"
                           r"|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16})")
# JSON web tokens (eyJ...) are found by _hide_jwts, in one pass: as a regex with no length limit they
# took quadratic time on text like "eyJ-eyJ-eyJ-...".
_JWT_RUN = re.compile(r"[A-Za-z0-9_\-.]{21,}")
# (Values have no length limit, so a long secret is hidden to its end. The early cut in
# _hide_then_cut still never shows text near the cut.)
# "Bearer abc", "Basic abc", "Token abc" in free text.
_AUTH_SCHEME = re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=\-]{4,}", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    run_id TEXT,
    status TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    decided_by TEXT,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status, created);
CREATE INDEX IF NOT EXISTS approvals_expires ON approvals(status, expires);
CREATE INDEX IF NOT EXISTS approvals_created ON approvals(created);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    decision TEXT NOT NULL,
    details TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
"""


_WORD_SPLIT = re.compile(r"[\W_]+")
# Thai vowel and tone marks: part of a word, not a break (รหัสผ่าน is one word). Removed before
# words are compared, like accents.
THAI_MARKS = "\u0e31\u0e34-\u0e3a\u0e47-\u0e4e"
_THAI_MARK = re.compile(f"[{THAI_MARKS}]")


def _split_words(name):
    name = _CAMEL_2.sub(r"\1_\2", _CAMEL_1.sub(r"\1_\2", name))
    if not name.isascii():
        name = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
        name = _THAI_MARK.sub("", name)
    return tuple(w for w in _WORD_SPLIT.split(name.lower()) if w)


_split_words_cached = functools.lru_cache(maxsize=8192)(_split_words)


def _words(name):
    """Split a name into lowercase words: "awsSecret-Access_KEY" -> aws, secret, access, key.
    Accents are removed and full-width letters made plain first (pässword, ｐａｓｓｗｏｒｄ -> password).
    Short names are cached: the same names come back again and again in long text."""
    name = str(name)
    return _split_words_cached(name) if len(name) <= 128 else _split_words(name)


def _is_secret(name, extra=()):
    if not extra and isinstance(name, str) and len(name) <= 128:
        return _is_secret_cached(name)
    return _is_secret_uncached(name, extra)


def _is_secret_uncached(name, extra=()):
    words = _words(name)
    pairs = {a + b for a, b in zip(words, words[1:])}
    found = (set(words) & (SECRET_WORDS | set(extra))) | (pairs & SECRET_PAIRS)
    if found and found <= {"token", "session", "otp", "totp", "pass", "auth"} and set(words) & (
            COUNT_WORDS | {"enabled", "name", "mode", "type", "method"}):
        return False   # max_tokens, session_count, auth_mode and the like are not secrets
    return bool(found)


_is_secret_cached = functools.lru_cache(maxsize=8192)(_is_secret_uncached)


def _hide_url_secrets(text):
    """Hide passwords in URLs (user:pw@host) and secret query values (?token=, &key=, &sig=)."""
    if "://" in text:
        text = _URL_USERINFO.sub(r"\1" + HIDDEN + "@", text)
    if "=" in text:
        text = _URL_PARAM.sub(
            lambda m: m.group(0) if not _is_secret(urllib.parse.unquote(urllib.parse.unquote(m.group(2))),
                                                   URL_SECRET_WORDS)
            else f"{m.group(1)}{m.group(2)}={HIDDEN}", text)
    return text


def _assign_sub(m, depth=0):
    quote, name, sep, value = m.group(1), m.group(2), m.group(3), m.group(4)
    if _is_secret(name) and value != HIDDEN:
        return f"{quote}{name}{quote}{sep}{HIDDEN}"
    # A non-secret name with a quoted value: secrets can still be inside the value
    # ({"message": "password=x"}), so look inside it too.
    if depth < 3 and len(value) >= 2 and value[0] in "\"'[" and value[-1] in "\"']":
        inner = _TEXT_ASSIGN.sub(lambda mm: _assign_sub(mm, depth + 1), value[1:-1])
        inner = _CLI_FLAG.sub(_flag_sub, inner) if "-" in inner else inner
        return f"{quote}{name}{quote}{sep}{value[0]}{inner}{value[-1]}"
    return m.group(0)


def _flag_sub(m):
    if not _is_secret(m.group(1).lstrip("-")) or m.group(3) == HIDDEN:
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}{HIDDEN}"


def _hide_pem(text):
    """Hide private key blocks. A plain scan (no backtracking), so it stays fast on any input."""
    if "-----BEGIN" not in text:
        return text
    out, pos = [], 0
    while True:
        m = _PEM_BEGIN.search(text, pos)
        if not m:
            out.append(text[pos:])
            return "".join(out)
        end = text.find("-----END", m.end(), m.end() + PEM_MAX)
        if end == -1:
            stop = min(len(text), m.end() + PEM_MAX)     # unterminated: hide what could be the key
        else:
            close = text.find("-----", end + 8, end + 8 + 80)
            stop = close + 5 if close != -1 else end + 8
        out.append(text[pos:m.start()] + HIDDEN)
        pos = stop


def _plain(text):
    """Text that is always safe to store and send: broken surrogates become escapes."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


# Secret pairs anywhere, also inside another pair's value ("login failed: password=x", where the
# pattern above reads "failed" = "password=x"). Only name and separator are matched here, so no value
# hides the next name; the value is then matched at that spot. Tabs and full-width : and = separate too.
_NAME_SEP = re.compile(rf"(?<![\w{THAI_MARKS}.\-])([^\W\d][\w{THAI_MARKS}.\-]{{0,63}})([ \t]*[:=\uff1a\uff1d]>?[ \t]*|\t+)")
_PAIR_VALUE = re.compile(r"\"[^\"\n]*\"?|'[^'\n]*'?|\[[^\]\n]*\]?|<[^>\n]*>?"
                         r"|\([^)\n]*\)?|`[^`\n]*`?|[^\s,;&\"'<>]+")
PAIR_HITS_MAX = 2000   # past this many secret names in one text, the rest of the text is hidden (speed)
# Passwords in well-known command lines: curl -u user:pw, mysql -pPW, sshpass -p PW.
# (curl's -u is found first, then "curl" is looked for in the 400 characters before it on the same
# line: a lazy "curl.{0,400}?-u" pattern was slow on text with many "curl" words.)
_CURL_USER = re.compile(r"(\s(?:-u|--user|-U|--proxy-user)(?:[ \t]+|=)?[^\s:\"']{1,256}+:)([^\s\"']+)")
_CURL_WORD = re.compile(r"\bcurl\b")


def _curl_sub(m, text):
    start = m.start()
    line_start = text.rfind("\n", max(0, start - 400), start) + 1
    window = text[max(line_start, start - 400):start + 1]
    return m.group(1) + HIDDEN if _CURL_WORD.search(window) else m.group(0)


_MYSQL_PW = re.compile(r"(\b(?:mysql|mysqldump|mysqladmin|mysqlimport|mysqlshow|mysqlcheck|mariadb|mariadb-dump)\b"
                       r"[^\n]{0,400}?\s-p)([^\s\"']+)")
_SSHPASS_PW = re.compile(r"(\bsshpass\b[^\n]{0,200}?\s-p[ \t]*)([^\s\"']+)")


_NUMERIC_LOOKING = re.compile(r"[+-]?[\d.,]+\s*[A-Za-z%]{0,3}")


@functools.lru_cache(maxsize=8192)
def _weak_kind_cached(name):
    return _weak_kind_uncached(name)


def _weak_kind(name):
    name = str(name)
    return _weak_kind_cached(name) if len(name) <= 128 else _weak_kind_uncached(name)


def _weak_kind_uncached(name):
    """"soft" for pin or pw as the whole name, "foreign" for "password" in another language, else None."""
    words = _words(name)
    if len(words) == 1 and words[0] in SOFT_NAMES:
        return "soft"
    pairs = {a + b for a, b in zip(words, words[1:])}
    if set(words) & FOREIGN_WORDS or pairs & FOREIGN_PAIRS:
        return "foreign"
    # Thai writes no spaces between words: "new password" is one name that contains the word.
    if not str(name).isascii() and any(t in w for w in words for t in THAI_PASSWORD_WORDS):
        return "foreign"
    return None


def _looks_like_a_secret(value):
    """Letters and digits mixed (hunter2, Zq8v...), at least 6 characters."""
    return len(value) >= 6 and re.search(r"\d", value) is not None and re.search(r"[^\W\d_]", value) is not None


def _weak_value_hidden(name, value):
    v = value.strip("\"'[]<>()` ")
    if _weak_kind(name) == "soft":
        if _words(name)[0] == "pin":
            return re.fullmatch(r"\d{4,8}", v) is not None or _looks_like_a_secret(v)
        return bool(v) and not _NUMERIC_LOOKING.fullmatch(v) and v.lower() not in ("on", "off", "yes", "no", "none")
    return _looks_like_a_secret(v) or len(v) >= 16


def _tab_name(name):
    words = _words(name)
    pairs = {a + b for a, b in zip(words, words[1:])}
    return bool(set(words) & TAB_WORDS or pairs & TAB_PAIRS)


WEAK_LOOK = 64
_WEAK_VALUE = re.compile(r"\"[^\"\n]{0,64}\"?|'[^'\n]{0,64}'?|\[[^\]\n]{0,64}\]?|<[^>\n]{0,64}>?"
                         r"|\([^)\n]{0,64}\)?|`[^`\n]{0,64}`?|[^\s,;&\"'<>]{1,64}")


_CLOSING = {'"': '"', "'": "'", "[": "]", "<": ">", "(": ")", "`": "`"}
_BARE_VALUE_CHAR = re.compile(r"[^\s,;&\"'<>]")


def _weak_cut(text, w):
    """True when the value goes on past the WEAK_LOOK characters that were looked at. Such a value is
    hidden: whatever follows the first 64 characters was never judged."""
    s, end = w.group(0), w.end()
    if end >= len(text):
        return False
    if s[0] in _CLOSING:
        closed = len(s) > 1 and s.endswith(_CLOSING[s[0]])
        return not closed and text[end] != "\n"
    return len(s) >= WEAK_LOOK and _BARE_VALUE_CHAR.match(text, end) is not None


def _hide_jwts(text):
    """Hide eyJ<8+>.<8+>.<any> (the same tokens the old pattern found) in linear time: each run of
    token characters is split at its dots once, and every part is looked at a fixed number of times."""
    if "eyJ" not in text:
        return text
    spans = []
    for run in _JWT_RUN.finditer(text):
        token = run.group(0)
        if "eyJ" not in token:
            continue
        parts = token.split(".")
        starts, at = [], run.start()
        for part in parts:
            starts.append(at)
            at += len(part) + 1
        i = 0
        while i + 2 < len(parts):
            part, k = parts[i], parts[i].find("eyJ")
            while k > 0 and part[k - 1] != "-":    # must start a word, as \b did
                k = part.find("eyJ", k + 1)
            if k >= 0 and len(part) - k - 3 >= 8 and len(parts[i + 1]) >= 8:
                spans.append((starts[i] + k, starts[i + 2] + len(parts[i + 2])))
                i += 3
            else:
                i += 1
    if not spans:
        return text
    out, pos = [], 0
    for a, b in spans:
        out.append(text[pos:a] + HIDDEN)
        pos = b
    out.append(text[pos:])
    return "".join(out)


def _hide_secret_pairs(text):
    spans, hits, pos = [], 0, 0
    while True:
        m = _NAME_SEP.search(text, pos)
        if not m:
            break
        pos = m.end()
        name, sep = m.group(1), m.group(2)
        strong = _is_secret(name)
        if not strong and not _weak_kind(name):
            continue
        if not sep.strip(" \t") and not (strong and _tab_name(name)):
            continue    # a tab alone separates only for real password names
        if not strong:
            # Decided on at most WEAK_LOOK characters, so text like "pin:(pin:(pin:(..." stays linear.
            # A value that is kept is not skipped: a real secret pair can be inside it.
            w = _WEAK_VALUE.match(text, m.end())
            if not w or w.group(0).startswith(HIDDEN):
                continue
            if not _weak_cut(text, w) and not _weak_value_hidden(name, w.group(0)):
                continue
        v = _PAIR_VALUE.match(text, m.end())      # hidden to its end, however long
        if not v or v.group(0).startswith(HIDDEN):
            continue
        hits += 1
        if hits > PAIR_HITS_MAX:
            spans.append((m.end(), len(text)))
            break
        spans.append((v.start(), v.end()))
        pos = max(pos, v.end())     # what is inside a hidden value is hidden anyway (keeps this linear)
    if not spans:
        return text
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    out, pos = [], 0
    for a, b in merged:
        out.append(text[pos:a] + HIDDEN)
        pos = b
    out.append(text[pos:])
    return "".join(out)


def _hide_text_secrets(text):
    """Hide secrets in free text: URL secrets, name=value / name: value pairs, auth headers."""
    text = _hide_pem(_plain(text))
    text = _hide_jwts(_KNOWN_TOKENS.sub(HIDDEN, text))
    text = _hide_url_secrets(text)
    text = _AUTH_SCHEME.sub(lambda m: f"{m.group(1)} {HIDDEN}", text)
    if "=" in text or ":" in text:
        text = _TEXT_ASSIGN.sub(_assign_sub, text)
    if "-" in text:
        text = _CLI_FLAG.sub(_flag_sub, text)
        if "curl" in text:
            text = _CURL_USER.sub(lambda m, t=text: _curl_sub(m, t), text)
        if "-p" in text:
            text = _MYSQL_PW.sub(lambda m: m.group(1) + HIDDEN, text)
            text = _SSHPASS_PW.sub(lambda m: m.group(1) + HIDDEN, text)
    return _hide_secret_pairs(text)


MAX_DEPTH = 64                    # deeper parts are replaced, never walked (no recursion limit)
TOO_DEEP = "[too deeply nested]"


def clean_details(value, max_str=MAX_STR, long_text=None, path="", _depth=0):
    """Hide secret-looking keys and URL secrets, and cut long strings, at any depth.

    If long_text is a list, every string longer than MAX_STR is listed there with its
    path, length and sha256 (of the exact text sent), so an approver can check it.
    Parts nested deeper than MAX_DEPTH are replaced by TOO_DEEP: nothing inside is shown."""
    if _depth > MAX_DEPTH and isinstance(value, (dict, list, tuple)):
        return TOO_DEEP
    if isinstance(value, dict):
        # {"name": "DB_PASSWORD", "value": "x"} (env lists, HAR files): hide the value.
        label = value.get("name", value.get("key"))
        if isinstance(label, str) and "value" in value and _is_secret(label):
            value = dict(value, value=HIDDEN)
        return {str(k): (HIDDEN if _is_secret(k) or _weak_kind(k) else
                         clean_details(v, max_str, long_text, f"{path}.{k}" if path else str(k), _depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        # A [name, value] pair (like a header list) hides the value of a secret name.
        if (len(value) == 2 and isinstance(value[0], str) and re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", value[0])
                and _is_secret(value[0])):
            return [value[0], HIDDEN]
        return [clean_details(v, max_str, long_text, f"{path}[{i}]", _depth + 1) for i, v in enumerate(value)]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)   # NaN and infinity are not valid JSON: stored as text
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # Anything else (bytes, objects) is stored as text.
    text = value if isinstance(value, str) else str(value)
    if long_text is not None and len(text) > MAX_STR:
        long_text.append({"path": path, "length": len(text),
                          "sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
                          "cut": len(text) > max_str})
    return _hide_then_cut(text, max_str)


EDGE = 8192              # shown text always stops this far before an early cut, so a secret split
                         # by the cut is never shown (values have no length limit: one that runs past
                         # the cut is hidden up to it, and nothing past the cut is ever shown)
EARLY_CUT_MAX = 1 << 20  # most text ever scanned for secrets in one value (speed)


def _hide_then_cut(text, max_str):
    """Hide first, then cut, so a cut can never show part of a secret.

    For speed only the start of a huge string is scanned. That early cut can split a secret so it
    no longer looks like one, and hiding earlier secrets shortens the text, which could bring the
    split piece into view. So the scan grows until the hidden text is at least EDGE characters
    longer than what is shown: the text nearest the early cut is then never shown."""
    n = max_str + 2 * EDGE
    while True:
        hidden = _hide_text_secrets(text[:n])
        if n >= len(text) or len(hidden) >= max_str + EDGE:
            return hidden[:max_str]
        if n >= EARLY_CUT_MAX:
            # Huge and mostly secrets: drop the edge itself.
            return hidden[:max(0, len(hidden) - EDGE)][:max_str]
        n = min(n * 4, EARLY_CUT_MAX)


def _exact(value, long_text, path=""):
    """A JSON-safe copy that changes nothing you need to see. Long strings get a sha256 note."""
    if isinstance(value, dict):
        return {str(k): _exact(v, long_text, f"{path}.{k}" if path else str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_exact(v, long_text, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = _plain(value if isinstance(value, str) else str(value))
    if len(text) > MAX_STR:
        long_text.append({"path": path, "length": len(text),
                          "sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(), "cut": False})
    return text


def _text_size(value):
    if isinstance(value, dict):
        return sum(len(str(k)) + _text_size(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return sum(_text_size(v) for v in value)
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value.bit_length() * 3 // 10 + 1   # about its number of digits (str() refuses huge ones)
    if isinstance(value, float):
        return len(repr(value))
    return len(value if isinstance(value, str) else str(value))


def approval_too_big(details):
    """None if an approval can show these details in full, else the reason it can't."""
    try:
        size = _text_size(details or {})
    except RecursionError:
        return "Too deeply nested to show you for approval."
    if size > MAX_APPROVAL_TOTAL:
        return (f"Too large to show you for approval (over {MAX_APPROVAL_TOTAL // 1000} KB). "
                "Write it in smaller parts.")
    return None


def clean_approval_details(details):
    """The details exactly as the agent sent them: an approval must show exactly what will
    happen, so nothing is hidden or cut here. (Approvals are readable with the admin token
    only. The audit log keeps a redacted copy.) Long strings are listed in "_long_text"."""
    reason = approval_too_big(details)
    if reason:
        raise ValueError(reason)
    long_text = []
    out = _exact(details or {}, long_text)
    if not isinstance(out, dict):
        out = {"details": out}
    if long_text:
        out["_long_text"] = long_text
    return out


def validate_config(config):
    """Check a config dict and return a clean copy. Raises ValueError."""
    if not isinstance(config, dict):
        raise ValueError("Config must be an object")
    # "error" is what get_config() reports for a broken file, so it is ignored here.
    unknown = set(config) - {"actions", "agents", "error"}
    if unknown:
        raise ValueError(f"Unknown config sections: {', '.join(sorted(unknown))}")

    def check_map(m, where):
        if not isinstance(m, dict):
            raise ValueError(f"{where} must be an object")
        for action, decision in m.items():
            if action not in ACTIONS:
                raise ValueError(f"Unknown action in {where}: {action}")
            if decision not in DECISIONS:
                raise ValueError(f"Bad decision for {action} in {where}: {decision!r} (use allow, ask or deny)")
        return dict(m)

    actions = check_map(config.get("actions", {}), "actions")
    agents_in = config.get("agents", {})
    if not isinstance(agents_in, dict):
        raise ValueError("agents must be an object")
    agents = {}
    for agent_id, m in agents_in.items():
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("Agent ids must be non-empty text")
        agents[agent_id] = check_map(m, f"agents.{agent_id}")
    return {"actions": actions, "agents": agents}


class Policy:
    def __init__(self, db_path: str, config_path: str):
        self.db_path = db_path
        self.config_path = config_path
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
        self._error = None
        self._config = self._load_config()

    # Config

    def _load_config(self):
        try:
            with open(self.config_path) as f:
                return validate_config(json.load(f))
        except FileNotFoundError:
            return {"actions": {}, "agents": {}}
        except (OSError, ValueError) as e:
            # A broken file fails safe: decide() denies everything that is not a plain read.
            self._error = f"Could not load {self.config_path}: {e}"
            return {"actions": {}, "agents": {}}

    def get_config(self) -> dict:
        with self._lock:
            out = json.loads(json.dumps(self._config))
            if self._error:
                out["error"] = self._error
            return out

    def set_config(self, config: dict) -> None:
        clean = validate_config(config)
        parent = os.path.dirname(os.path.abspath(self.config_path))
        os.makedirs(parent, exist_ok=True)
        with self._lock:
            fd, tmp = tempfile.mkstemp(suffix=".json", dir=parent)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(clean, f, indent=2, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o644)
                os.replace(tmp, self.config_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._config = clean
            self._error = None

    def decide(self, agent_id: str, action: str) -> str:
        if action not in ACTIONS:
            return "deny"
        with self._lock:
            if self._error:
                return "allow" if ACTIONS[action][1] == "allow" else "deny"
            glob = self._config["actions"].get(action)
            agent = self._config["agents"].get(agent_id, {}).get(action) if agent_id else None
        if "deny" in (glob, agent):
            return "deny"
        return agent or glob or ACTIONS[action][1]

    # Approvals

    def _row(self, r):
        return {"id": r["id"], "agent": r["agent"], "action": r["action"],
                "details": json.loads(r["details"]), "run_id": r["run_id"], "status": r["status"],
                "created": r["created"], "expires": r["expires"],
                "decided_by": r["decided_by"], "decided_at": r["decided_at"]}

    def _expire(self):
        # Caller holds the lock.
        self._db.execute("UPDATE approvals SET status='expired' WHERE status='pending' AND expires < ?",
                         (time.time(),))

    def request(self, agent_id: str, action: str, details: dict, run_id: str | None = None,
                ttl_s: int = 900) -> str:
        aid = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO approvals (id, agent, action, details, run_id, status, created, expires)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (aid, agent_id, action, json.dumps(clean_approval_details(details)), run_id, now, now + ttl_s))
        return aid

    def get(self, approval_id: str) -> dict | None:
        with self._lock:
            self._expire()
            r = self._db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return self._row(r) if r else None

    def list(self, status: str | None = "pending", limit: int = 100) -> list[dict]:
        if status is not None and status not in STATUSES:
            raise ValueError(f"Unknown status: {status}")
        with self._lock:
            self._expire()
            if status is None:
                rows = self._db.execute("SELECT * FROM approvals ORDER BY created DESC LIMIT ?",
                                        (limit,)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM approvals WHERE status=? ORDER BY created DESC LIMIT ?",
                                        (status, limit)).fetchall()
        return [self._row(r) for r in rows]

    def answer(self, approval_id: str, approve: bool, by: str) -> dict:
        status = "approved" if approve else "denied"
        with self._lock:
            self._expire()
            cur = self._db.execute(
                "UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=? AND status='pending'",
                (status, by, time.time(), approval_id))
            if cur.rowcount != 1:
                r = self._db.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()
                if r is None:
                    raise ValueError(f"Unknown approval: {approval_id}")
                raise ValueError(f"Approval is already {r['status']}")
            r = self._db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        out = self._row(r)
        self.audit(by, out["action"], status,
                   {"approval_id": approval_id, "agent": out["agent"], "run_id": out["run_id"]})
        return out

    def expire(self, approval_id: str, reason: str) -> dict:
        """Give up on a pending approval, for example when the runner stops waiting."""
        with self._lock:
            self._expire()
            cur = self._db.execute(
                "UPDATE approvals SET status='expired', decided_at=? WHERE id=? AND status='pending'",
                (time.time(), approval_id))
            r = self._db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if r is None:
                raise ValueError(f"Unknown approval: {approval_id}")
            if cur.rowcount != 1:
                raise ValueError(f"Approval is already {r['status']}")
        out = self._row(r)
        self.audit(out["agent"], out["action"], "expired",
                   {"approval_id": approval_id, "reason": reason, "run_id": out["run_id"]})
        return out

    def wait(self, approval_id: str, timeout_s: float, poll_s: float = 0.5) -> str:
        end = time.monotonic() + timeout_s
        while True:
            a = self.get(approval_id)
            if a is None:
                raise ValueError(f"Unknown approval: {approval_id}")
            if a["status"] != "pending":
                return a["status"]
            left = end - time.monotonic()
            if left <= 0:
                return "pending"
            time.sleep(min(poll_s, left))

    # Audit

    def audit(self, actor: str, action: str, decision: str, details: dict | None = None,
              result: str | None = None) -> None:
        text = json.dumps(clean_details(details), allow_nan=False) if details is not None else None
        if result is not None:
            result = _hide_text_secrets(str(result))[:MAX_STR]
        with self._lock:
            self._db.execute(
                "INSERT INTO audit (ts, actor, action, decision, details, result) VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), actor, action, decision, text, result))

    def audit_log(self, limit: int = 200, since_s: int | None = None, actor: str | None = None,
                  action: str | None = None) -> list[dict]:
        where, args = [], []
        if since_s is not None:
            where.append("ts >= ?")
            args.append(time.time() - since_s)
        if actor is not None:
            where.append("actor = ?")
            args.append(actor)
        if action is not None:
            where.append("action = ?")
            args.append(action)
        sql = "SELECT * FROM audit"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
                 "decision": r["decision"],
                 "details": json.loads(r["details"]) if r["details"] is not None else None,
                 "result": r["result"]} for r in rows]

    def prune(self, keep_days: int = 90) -> None:
        """Delete old audit rows and finished approvals. Both deletes use an index, so daily runs are cheap."""
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            self._expire()
            self._db.execute("DELETE FROM audit WHERE ts < ?", (cutoff,))
            # Pending requests are kept; answered and expired ones go.
            self._db.execute("DELETE FROM approvals WHERE status != 'pending' AND created < ?", (cutoff,))

    # Used by the agent runner

    def check(self, agent_id: str, action: str, details: dict, run_id: str | None = None,
              ttl_s: int = 900) -> dict:
        d = self.decide(agent_id, action)
        reason = None
        if d == "ask":
            # Never ask you to approve text you can't see in full.
            reason = approval_too_big(details)
            if reason:
                d = "deny"
        approval_id = self.request(agent_id, action, details, run_id, ttl_s) if d == "ask" else None
        info = {"details": details, "run_id": run_id}
        if approval_id:
            info["approval_id"] = approval_id
        if reason:
            info["reason"] = reason
        self.audit(agent_id, action, d, info)
        out = {"decision": d, "approval_id": approval_id}
        if reason:
            out["reason"] = reason
        return out

    def close(self):
        with self._lock:
            self._db.close()


def make_router(policy: Policy, auth_dep) -> APIRouter:
    """Routes for the policy, approval queue and audit log. Mount under "/api"."""
    router = APIRouter(tags=["agents"], dependencies=[Depends(auth_dep)])

    @router.get("/policy")
    def get_policy():
        actions = [{"id": a, "description": desc, "default": default, "effective": policy.decide("", a)}
                   for a, (desc, default) in ACTIONS.items()]
        return {"actions": actions, "config": policy.get_config()}

    @router.put("/policy")
    def put_policy(config: dict = Body(..., embed=True)):
        """Replace the policy, for example {"config": {"actions": {"web.fetch": "allow"}}}."""
        try:
            policy.set_config(config)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "config": policy.get_config()}

    @router.get("/approval-count")
    def approval_count():
        """How many approvals are waiting. Safe for the read-only viewer token."""
        return {"pending": len(policy.list("pending", 1000))}

    @router.get("/approvals")
    def list_approvals(status: str | None = "pending", limit: int = Query(100, ge=1, le=1000)):
        try:
            return policy.list(status or None, limit)
        except ValueError as e:
            raise HTTPException(422, str(e))

    def answer(approval_id, approve, by):
        if policy.get(approval_id) is None:
            raise HTTPException(404, f"Unknown approval: {approval_id}")
        try:
            return policy.answer(approval_id, approve, by or "admin")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @router.post("/approvals/{approval_id}/approve")
    def approve(approval_id: str, by: str | None = Body(default=None, embed=True)):
        return answer(approval_id, True, by)

    @router.post("/approvals/{approval_id}/deny")
    def deny(approval_id: str, by: str | None = Body(default=None, embed=True)):
        return answer(approval_id, False, by)

    @router.get("/audit")
    def get_audit(limit: int = Query(200, ge=1, le=5000),
                  since: int | None = Query(None, ge=1, description="Seconds back"),
                  actor: str | None = None, action: str | None = None):
        return policy.audit_log(limit, since, actor, action)

    return router
