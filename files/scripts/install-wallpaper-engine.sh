#!/usr/bin/env bash
# Optional: the Wallpaper Engine plugin for Plasma (CaptSilver's fork, package wallpaper-engine-kde-plugin-qt6).
# Its COPR builds every new commit, so nothing is installed unless a person has pinned BOTH:
#   WE_VERSION   the exact version-release to install, for example "1.5-1.fc45"
#   WE_KEY_FPR   the full fingerprint of the COPR project's signing key (40 hex characters)
# With both empty (the default) the build skips it and Forge Settings says
# "Wallpaper Engine support is not included in this build". The plugin also needs Wallpaper Engine
# bought on Steam; only its workshop files are used.
#
# To turn it on:
#   1. Look at https://copr.fedorainfracloud.org/coprs/captsilver666/wallpaper-engine-kde-plugin/
#      and pick a successful Fedora 45 build of a tagged release (not just any commit).
#   2. Put its version-release in WE_VERSION.
#   3. Download the key from WE_KEY_URL below, check it with the project, and put its fingerprint
#      (gpg --show-keys --with-colons KEY | grep fpr) in WE_KEY_FPR.
# The package is installed from that one build with the signature checked against the pinned key,
# and no repo file is left in the image, so updates never pull an unpinned build.
set -euo pipefail

WE_VERSION=""
WE_KEY_FPR=""
WE_PACKAGE="wallpaper-engine-kde-plugin-qt6"
WE_REPO="https://download.copr.fedorainfracloud.org/results/captsilver666/wallpaper-engine-kde-plugin/fedora-\$releasever-\$basearch/"
WE_KEY_URL="https://download.copr.fedorainfracloud.org/results/captsilver666/wallpaper-engine-kde-plugin/pubkey.gpg"

if [[ -z "${WE_VERSION}" && -z "${WE_KEY_FPR}" ]]; then
  echo "install-wallpaper-engine: skipped, no pinned version. Wallpaper Engine support is not included in this build."
  exit 0
fi
if [[ ! "${WE_VERSION}" =~ ^[0-9][0-9A-Za-z.+~_]*-[0-9A-Za-z.+~_]+$ ]]; then
  echo "install-wallpaper-engine: WE_VERSION must be an exact version-release like 1.5-1.fc45 (got '${WE_VERSION}')." >&2
  exit 1
fi
if [[ ! "${WE_KEY_FPR}" =~ ^[0-9A-F]{40}$ ]]; then
  echo "install-wallpaper-engine: WE_KEY_FPR must be the key's full fingerprint (40 hex characters, upper case)." >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "${tmp}/key" "${WE_KEY_URL}"
mkdir -m 0700 "${tmp}/gnupg"
got=$(GNUPGHOME="${tmp}/gnupg" gpg --batch --with-colons --show-keys "${tmp}/key" 2>/dev/null \
      | awk -F: '$1=="pub"{p=1; next} p && $1=="fpr"{print $10; p=0}' | tr '\n' ' ')
if [[ "${got}" != "${WE_KEY_FPR} " ]]; then
  echo "install-wallpaper-engine: the COPR signing key does not match the pinned fingerprint." >&2
  echo "  expected: ${WE_KEY_FPR}" >&2
  echo "  got:      ${got:-(no key)}" >&2
  exit 1
fi
install -m 0644 "${tmp}/key" "${tmp}/forge-wallpaper-engine.gpg"
dnf -y install \
  --repofrompath="forge-wallpaper-engine,${WE_REPO}" \
  --setopt="forge-wallpaper-engine.gpgcheck=1" \
  --setopt="forge-wallpaper-engine.gpgkey=file://${tmp}/forge-wallpaper-engine.gpg" \
  "${WE_PACKAGE}-${WE_VERSION}"
test -f /usr/share/plasma/wallpapers/com.github.captsilver.wallpaperEngineKde/metadata.json
echo "install-wallpaper-engine: ${WE_PACKAGE}-${WE_VERSION} installed."
