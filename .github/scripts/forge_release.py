#!/usr/bin/env python3
"""Release helpers for the bluebuild workflow (standard library only).

  plan (--inspect FILE | --fedora N) --github-output FILE
      FILE is `skopeo inspect` of the Bazzite base image (or N its Fedora major). Works out the Forge OS version
      45.YYYYMMDD.N (Fedora major from the base image's org.opencontainers.image.version label, the
      UTC date, N = the workflow run number, plus ".A" for a re-run attempt A) and the channel
      (refs/heads/main: stable, refs/heads/testing: testing, anything else: dev). Reads GITHUB_REF,
      GITHUB_EVENT_NAME, GITHUB_RUN_NUMBER, GITHUB_RUN_ATTEMPT, GITHUB_SHA.
      Writes the step outputs fedora, version, channel, built_tag and version_json. The build job runs
      it itself (with --fedora), so every run attempt of that job gets its own version.
  write --out PATH --github-env FILE
      Checks FORGE_VERSION_JSON again (it went through a job output) and writes it to PATH (the
      image's /usr/share/forge/version.json), and FORGE_VERSION=<version> to FILE for the image's
      own label org.forgeos.version (Bazzite's org.opencontainers.image.version is left alone).
  check-label --inspect FILE --expect VERSION
      After the push: FILE is `skopeo inspect` of the pushed image; fails unless its
      org.forgeos.version label is exactly VERSION.
  digest
      Reads `cosign verify` JSON on stdin and prints the one image digest it verified.
  read-digest FILE
      Prints the one image digest in FILE (the digest artifact this run's build job wrote), checked.
  iso-tag TAG
      build-iso: prints TAG if it is a tag our build makes (latest, testing,
      br-<branch>-candidate-<fedora>, <sha7>-candidate-<fedora>), "latest" when empty; fails otherwise.
  iso-follow-tag TAG
      build-iso: the tag the installed system follows for updates. TAG itself when the ISO volume id
      "forge-x86_64-<tag>" fits lorax's 32 character limit, otherwise "latest".

Every value is checked against a strict pattern before it is written, so nothing from the
environment or a registry can add lines to the GitHub output or environment files.
"""
import argparse
import datetime
import json
import os
import re
import sys

VERSION_LABEL = "org.opencontainers.image.version"
# The same reading BlueBuild uses for its own tags: an optional "word-" prefix, then the major number.
_LABEL = re.compile(r"(?:[A-Za-z0-9_-]+-)?v?([0-9]+)(?:\.[0-9]+){0,2}(?:[-_].*)?")
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
CHANNELS = ("stable", "testing", "dev")
FIELDS = ("format", "version", "fedora", "date", "build", "attempt", "commit", "channel", "built")
FORGE_LABEL = "org.forgeos.version"
_VERSION = re.compile(r"[0-9]{2}\.[0-9]{8}\.[1-9][0-9]{0,8}(?:\.[2-9]|\.[1-9][0-9]{1,2})?")


def fedora_from_inspect(data):
    if not isinstance(data, dict) or not isinstance(data.get("Labels"), dict):
        raise ValueError("no labels in the base image")
    label = data["Labels"].get(VERSION_LABEL)
    m = _LABEL.fullmatch(label.strip()) if isinstance(label, str) else None
    if not m:
        raise ValueError(f"the base image has no usable {VERSION_LABEL} label")
    major = int(m.group(1))
    if not 30 <= major <= 99:
        raise ValueError(f"unexpected Fedora version {major}")
    return major


def channel_for(ref, event):
    """From the full ref, so a tag named "main" is never the main branch."""
    if event == "pull_request":
        return "dev"
    return {"refs/heads/main": "stable", "refs/heads/testing": "testing"}.get(ref, "dev")


def make_version(fedora, when, run_number, sha, channel, attempt="1"):
    if not (isinstance(fedora, int) and 30 <= fedora <= 99):
        raise ValueError("bad Fedora version")
    if not (isinstance(run_number, str) and re.fullmatch(r"[1-9][0-9]{0,8}", run_number)):
        raise ValueError("bad run number")
    if not (isinstance(sha, str) and _SHA.fullmatch(sha)):
        raise ValueError("bad commit")
    if channel not in CHANNELS:
        raise ValueError("bad channel")
    if not (isinstance(attempt, str) and re.fullmatch(r"[1-9][0-9]{0,2}", attempt)):
        raise ValueError("bad run attempt")
    when = when.astimezone(datetime.timezone.utc)
    date = when.strftime("%Y%m%d")
    version = f"{fedora}.{date}.{run_number}" + (f".{attempt}" if attempt != "1" else "")
    return {"format": 1, "version": version, "fedora": fedora, "date": date, "build": int(run_number),
            "attempt": int(attempt), "commit": sha, "channel": channel, "built": when.strftime("%Y-%m-%dT%H:%M:%SZ")}


def check_version(obj):
    """The same checks again, for a value that came back through a job output."""
    if not isinstance(obj, dict) or set(obj) != set(FIELDS):
        raise ValueError("unexpected fields")
    if obj["format"] != 1 or any(type(obj[k]) is not int for k in ("build", "fedora", "attempt")):
        raise ValueError("unexpected values")
    when = datetime.datetime.strptime(obj["built"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    again = make_version(obj["fedora"], when, str(obj["build"]), obj["commit"], obj["channel"], str(obj["attempt"]))
    if again != obj:
        raise ValueError("the version does not match its parts")
    return obj


_ISO_TAG = re.compile(r"latest|testing|[0-9a-f]{7}-candidate-[0-9]{2}|br-[A-Za-z0-9][A-Za-z0-9._-]{0,99}-candidate-[0-9]{2}")
VOLID_PREFIX = "forge-x86_64-"   # build-container-installer: <first word of image name>-<arch>-<tag>


def iso_tag(value):
    """The image tag build-iso may use: only the forms our build produces. Empty: latest."""
    if value == "":
        return "latest"
    if not (isinstance(value, str) and _ISO_TAG.fullmatch(value)):
        raise ValueError("not an image tag this repository's build makes (latest, testing, "
                         "br-<branch>-candidate-<fedora> or <sha7>-candidate-<fedora>)")
    return value


def iso_follow_tag(tag):
    if not (isinstance(tag, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag)):
        raise ValueError("bad tag")
    return tag if len(VOLID_PREFIX + tag) <= 32 else "latest"


def built_tag(sha, fedora):
    """The tag BlueBuild gives every image it pushes, with the recipes' alt-tags: [candidate]:
    <short sha>-candidate-<Fedora major>, on main, testing and other branches alike."""
    return f"{sha[:7]}-candidate-{fedora}"


def digest_from_cosign(text):
    try:
        items = json.loads(text)
    except ValueError:
        raise ValueError("cosign output is not JSON") from None
    if not isinstance(items, list) or not items:
        raise ValueError("cosign verified nothing")
    found = set()
    for it in items:
        try:
            found.add(it["critical"]["image"]["docker-manifest-digest"])
        except (KeyError, TypeError):
            raise ValueError("unexpected cosign output") from None
    if len(found) != 1:
        raise ValueError("cosign verified more than one image")
    digest = found.pop()
    if not (isinstance(digest, str) and _DIGEST.fullmatch(digest)):
        raise ValueError("unexpected digest")
    return digest


def label_problem(inspect, expected):
    """None when the pushed image's Forge version label is exactly the expected version."""
    if not (isinstance(expected, str) and _VERSION.fullmatch(expected)):
        return "bad expected version"
    labels = inspect.get("Labels") if isinstance(inspect, dict) else None
    got = labels.get(FORGE_LABEL) if isinstance(labels, dict) else None
    if got != expected:
        shown = got if isinstance(got, str) and got.isprintable() and len(got) < 100 else "(missing)"
        return f"the pushed image's {FORGE_LABEL} label is {shown!r}, expected {expected!r}"
    return None


def _append(path, lines):
    for line in lines:
        if "\n" in line or "\r" in line:
            raise ValueError("a value has a line break")
    with open(path, "a") as f:
        f.write("".join(line + "\n" for line in lines))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--inspect")
    src.add_argument("--fedora")
    p.add_argument("--github-output", required=True)
    w = sub.add_parser("write")
    w.add_argument("--out", required=True)
    w.add_argument("--github-env", required=True)
    c = sub.add_parser("check-label")
    c.add_argument("--inspect", required=True)
    c.add_argument("--expect", required=True)
    sub.add_parser("digest")
    d = sub.add_parser("read-digest")
    d.add_argument("file")
    for name in ("iso-tag", "iso-follow-tag"):
        sub.add_parser(name).add_argument("tag")
    a = ap.parse_args(argv)
    env = os.environ
    try:
        if a.cmd == "plan":
            if a.inspect:
                with open(a.inspect) as f:
                    fedora = fedora_from_inspect(json.load(f))
            elif re.fullmatch(r"[0-9]{2}", a.fedora) and 30 <= int(a.fedora) <= 99:
                fedora = int(a.fedora)
            else:
                raise ValueError("bad Fedora version")
            channel = channel_for(env.get("GITHUB_REF", ""), env.get("GITHUB_EVENT_NAME", ""))
            v = make_version(fedora, datetime.datetime.now(datetime.timezone.utc), env.get("GITHUB_RUN_NUMBER", ""),
                             env.get("GITHUB_SHA", ""), channel, env.get("GITHUB_RUN_ATTEMPT") or "1")
            _append(a.github_output, [f"fedora={fedora}", f"version={v['version']}", f"channel={channel}",
                                      f"built_tag={built_tag(v['commit'], fedora)}",
                                      "version_json=" + json.dumps(v, sort_keys=True, separators=(",", ":"))])
            print(f"Forge OS {v['version']} ({channel}, {v['commit'][:7]})")
        elif a.cmd == "write":
            v = check_version(json.loads(env.get("FORGE_VERSION_JSON", "")))
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            with open(a.out, "w") as f:
                json.dump(v, f, indent=2, sort_keys=True)
                f.write("\n")
            _append(a.github_env, [f"FORGE_VERSION={v['version']}"])
            print(f"Wrote {a.out}: Forge OS {v['version']} ({v['channel']}, {v['commit'][:7]})")
        elif a.cmd == "check-label":
            with open(a.inspect) as f:
                problem = label_problem(json.load(f), a.expect)
            if problem:
                raise ValueError(problem)
            print(f"{FORGE_LABEL} = {a.expect}: ok")
        elif a.cmd == "digest":
            print(digest_from_cosign(sys.stdin.read()))
        elif a.cmd == "iso-tag":
            print(iso_tag(a.tag))
        elif a.cmd == "iso-follow-tag":
            print(iso_follow_tag(a.tag))
        else:
            with open(a.file) as f:
                text = f.read(200)
            if not _DIGEST.fullmatch(text.rstrip("\n")) or text.count("\n") > 1:
                raise ValueError("the digest file does not hold one image digest")
            print(text.strip())
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
