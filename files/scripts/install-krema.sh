#!/usr/bin/env bash
# Optional: installs Krema (the Animated dock style) from the COPR repo isac322/krema during the image
# build. Krema is optional, so this script never stops the build. If a pin is empty, the key does not
# match, or dnf can't install it (for example when the COPR package wants a different Qt version than
# the image has), it prints one clear line and the image is built without Krema. The Animated style in
# Forge Settings then says that Krema is not installed.
#
# Two pins, both empty until the owner fills them in (see README, "Dock styles"):
#   KREMA_VERSION  the exact version-release of the krema package for this Fedora, for example
#                  0.13.0-1.fc45 (dnf --repofrompath=k,https://download.copr.fedorainfracloud.org/results/isac322/krema/fedora-45-x86_64/ list --showduplicates krema)
#   KREMA_KEY_FPR  the full fingerprint (40 hex characters) of the COPR project's signing key, checked
#                  by hand from https://download.copr.fedorainfracloud.org/results/isac322/krema/pubkey.gpg
# The values below are fixed in this file on purpose: the environment can't change them.
#
# FORGE_KREMA_ROOT and FORGE_KEY_TEST_DIR are only for tests.
set -uo pipefail

KREMA_VERSION=""
KREMA_KEY_FPR=""

KEY_URL="https://download.copr.fedorainfracloud.org/results/isac322/krema/pubkey.gpg"
ROOT=${FORGE_KREMA_ROOT:-}
KEY_FILE="$ROOT/etc/pki/rpm-gpg/forge-krema.gpg"
REPO_FILE="$ROOT/etc/yum.repos.d/forge-krema.repo"
GLOBAL_AUTOSTART="$ROOT/etc/xdg/autostart/com.bhyoo.krema.autostart.desktop"

tmp=$(mktemp -d)
cleanup() {
  rm -rf "$tmp"
  # The repo is only for this one install: the image does not keep it.
  rm -f "$REPO_FILE" "$KEY_FILE"
}
trap cleanup EXIT

not_installed() {
  echo "install-krema: Krema NOT installed: $*"
  echo "install-krema: the image is built without Krema; the Animated dock style will say so."
  exit 0
}

if [ -z "$KREMA_VERSION" ] || [ -z "$KREMA_KEY_FPR" ]; then
  echo "install-krema: skipped: KREMA_VERSION and KREMA_KEY_FPR in files/scripts/install-krema.sh are empty, so Krema is not in this image."
  exit 0
fi
if ! [[ $KREMA_VERSION =~ ^[0-9][0-9A-Za-z.+~^_]*-[0-9A-Za-z.+~^_]+$ ]]; then
  not_installed "KREMA_VERSION is not a version-release like 0.13.0-1.fc45"
fi
if ! [[ $KREMA_KEY_FPR =~ ^[0-9A-F]{40}$ ]]; then
  not_installed "KREMA_KEY_FPR is not 40 upper case hex characters"
fi

# The signing key must match the pinned fingerprint exactly (no other keys next to it).
if [ -n "${FORGE_KEY_TEST_DIR:-}" ]; then
  cp "$FORGE_KEY_TEST_DIR/krema.key" "$tmp/key" || not_installed "no test key"
else
  curl -fsSL --proto '=https' --tlsv1.2 --retry 3 "$KEY_URL" -o "$tmp/key" || not_installed "the signing key could not be downloaded"
fi
mkdir -m 0700 "$tmp/gnupg"
got=$(GNUPGHOME="$tmp/gnupg" gpg --batch --with-colons --show-keys "$tmp/key" 2>/dev/null \
      | awk -F: '$1=="pub"{p=1; next} p && $1=="fpr"{print $10; p=0}' | tr '\n' ' ')
if [ "$got" != "$KREMA_KEY_FPR " ]; then
  not_installed "the COPR signing key does not match the pinned fingerprint (expected $KREMA_KEY_FPR, got ${got:-no key})"
fi
install -d -m 0755 "$(dirname "$KEY_FILE")" "$(dirname "$REPO_FILE")"
install -m 0644 "$tmp/key" "$KEY_FILE"
# Packages are signed by the COPR key (gpgcheck=1). COPR does not sign its repo metadata, so
# repo_gpgcheck stays off here; the exact version below is what pins the package.
cat > "$REPO_FILE" <<'EOF'
[forge-krema]
name=Krema dock (COPR isac322/krema), used only while building the Forge OS image
baseurl=https://download.copr.fedorainfracloud.org/results/isac322/krema/fedora-$releasever-$basearch/
enabled=0
gpgcheck=1
repo_gpgcheck=0
gpgkey=file:///etc/pki/rpm-gpg/forge-krema.gpg
EOF

if ! dnf -y --enablerepo=forge-krema install "krema-$KREMA_VERSION"; then
  not_installed "dnf could not install krema-$KREMA_VERSION (the COPR build may need a different Qt version than this image has)"
fi
# Krema's package starts it for every user. Forge starts it per user instead (Settings, Appearance, Dock).
rm -f "$GLOBAL_AUTOSTART"
echo "install-krema: installed krema-$KREMA_VERSION (no global autostart; Forge starts it per user)"
