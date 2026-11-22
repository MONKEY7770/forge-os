"""Forge OS releases: version number, update channels and the image this PC follows.

Shared by forge-apply (root, switches the channel), forge-settings and the Forge Dashboard (show
the version). Standard library only. Everything read here (rpm-ostree output, the version file)
is checked before it is used.
"""
import json
import os
import re
import stat

REGISTRY = "ghcr.io/monkey7770"
IMAGES = ("forge-os", "forge-os-universal")
# Channel -> image tag. CI pushes "latest" only from main and "testing" only from the testing branch.
CHANNEL_TAGS = {"stable": "latest", "testing": "testing"}
VERSION_PATH = "/usr/share/forge/version.json"   # FORGE_VERSION_FILE overrides it (tests)
VERSION_MAX = 4096

# How rpm-ostree writes the image a deployment follows. Signed or not, only our two images,
# a plain tag (never a digest).
_REF = re.compile(r"(ostree-image-signed:docker://|ostree-unverified-registry:|ostree-unverified-image:docker://|"
                  r"ostree-remote-image:[A-Za-z0-9_.-]{1,64}:docker://)"
                  + re.escape(REGISTRY) + r"/(forge-os|forge-os-universal):([A-Za-z0-9_][A-Za-z0-9_.-]{0,127})")
_VERSION = re.compile(r"[0-9]{2,3}\.[0-9]{8}\.[0-9]{1,9}(?:\.[0-9]{1,3})?")   # .A: a CI re-run
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
_BUILT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def parse_ref(ref):
    """{"image", "tag", "signed"} for one of our image references, else None."""
    if not isinstance(ref, str):
        return None
    m = _REF.fullmatch(ref)
    if not m:
        return None
    return {"image": m.group(2), "tag": m.group(3), "signed": m.group(1) == "ostree-image-signed:docker://"}


def channel_of_tag(tag):
    for channel, t in CHANNEL_TAGS.items():
        if tag == t:
            return channel
    return None


def target_ref(image, channel):
    """The signed reference to switch to. Never an unverified one."""
    if image not in IMAGES or channel not in CHANNEL_TAGS:
        raise ValueError("unknown image or channel")
    return f"ostree-image-signed:docker://{REGISTRY}/{image}:{CHANNEL_TAGS[channel]}"


def followed(data):
    """What this PC follows from `rpm-ostree status --json`: the first deployment (a downloaded one
    waiting for a restart, otherwise the default boot entry). rpm-ostree takes updates from that one
    (rpmostree_syscore_get_origin_merge_deployment), even when an older entry was started from the
    boot menu. None when it is not one of our images or unreadable."""
    if not isinstance(data, dict) or not isinstance(data.get("deployments"), list):
        return None
    deps = [d for d in data["deployments"] if isinstance(d, dict)]
    if not deps or len(deps) != len(data["deployments"]):
        return None
    if not any(d.get("booted") is True for d in deps):
        return None
    pick = deps[0]
    staged = pick.get("staged") is True
    ref = parse_ref(pick.get("container-image-reference"))
    if ref is None:
        return None
    return dict(ref, channel=channel_of_tag(ref["tag"]), staged=staged)


def switch_plan(data, channel):
    """(target reference or None when already there, follow info). Raises ValueError with a
    message for the user when the switch is not possible."""
    if channel not in CHANNEL_TAGS:
        raise ValueError("choose stable or testing")
    f = followed(data)
    if f is None:
        raise ValueError(f"this PC does not run a Forge OS image from {REGISTRY}, so it has no update channel")
    if f["channel"] == channel and f["signed"]:
        return None, f
    return target_ref(f["image"], channel), f


def read_version(path=None):
    """The version file written at build time, checked, or None (a local build, or a bad file)."""
    path = path or os.environ.get("FORGE_VERSION_FILE") or VERSION_PATH
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, VERSION_MAX + 1)
    finally:
        os.close(fd)
    if len(data) > VERSION_MAX:
        return None
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or obj.get("format") != 1:
        return None
    version, commit, channel = obj.get("version"), obj.get("commit"), obj.get("channel")
    if not (isinstance(version, str) and _VERSION.fullmatch(version)):
        return None
    if not (isinstance(commit, str) and _COMMIT.fullmatch(commit)):
        return None
    if channel not in ("stable", "testing", "dev"):
        return None
    built = obj.get("built")
    return {"version": version, "commit": commit, "channel": channel,
            "built": built if isinstance(built, str) and _BUILT.fullmatch(built) else None}


def version_text(info):
    """One line for people: "45.20261120.57 (stable, abc1234)", or a note when unknown."""
    if not info:
        return "unknown (built without the release pipeline)"
    return f"{info['version']} ({info['channel']}, {info['commit'][:7]})"


def go_back_text(channel):
    other = "stable" if channel == "testing" else "testing"
    return (f"To go back: choose {other.capitalize()} in Forge Settings (System, Updates and recovery), or run "
            f"sudo /usr/libexec/forge-apply --switch-channel {other} --confirm, then restart. Before you restart, "
            f"rpm-ostree cleanup -p cancels the switch. Starting the previous entry from the boot menu only starts "
            f"it once: updates keep coming from the channel you chose.")
