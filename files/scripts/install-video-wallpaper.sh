#!/usr/bin/env bash
# Installs the Smart Video Wallpaper Reborn plugin for Plasma (video backgrounds) during the image build.
# It is plain QML (no compiled code). The version is pinned and the download must match a known sha256,
# or the build stops. Fedora's own package is much older (2.9), so the GitHub release is used.
#
# To update it:
#   1. Pick a release at https://github.com/luisbocanegra/plasma-smart-video-wallpaper-reborn/releases
#   2. Set SVWR_VERSION to it (without the "v").
#   3. Download plasma-smart-video-wallpaper-reborn-v<version>.tar.gz from that release, run
#      sha256sum on it and put the hash in SVWR_SHA256. Read the release notes for config changes.
set -euo pipefail

SVWR_VERSION="2.17.0"
SVWR_SHA256="25c13564016b495bc20949df0b798e7afd897945b7956a739a9c926b6f9cb9c0"
SVWR_ID="luisbocanegra.smart.video.wallpaper.reborn"
SVWR_FILE="plasma-smart-video-wallpaper-reborn-v${SVWR_VERSION}.tar.gz"
SVWR_URL="https://github.com/luisbocanegra/plasma-smart-video-wallpaper-reborn/releases/download/v${SVWR_VERSION}/${SVWR_FILE}"
DEST="/usr/share/plasma/wallpapers/${SVWR_ID}"

# Check a file against the pinned hash. Fails (and stops the build) on any mismatch.
verify() {
  echo "${SVWR_SHA256}  $1" | sha256sum -c -
}

# Test mode: VERIFY_ONLY=/some/file only checks that file, with no download or install.
if [[ -n "${VERIFY_ONLY:-}" ]]; then
  verify "${VERIFY_ONLY}"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

curl -fsSL --retry 3 --proto '=https' -o "${tmp}/${SVWR_FILE}" "${SVWR_URL}"
verify "${tmp}/${SVWR_FILE}"
mkdir "${tmp}/pkg"
tar -xzf "${tmp}/${SVWR_FILE}" -C "${tmp}/pkg" --no-same-owner --no-same-permissions
# The archive is the plugin folder itself: metadata.json and contents/ at the top.
grep -q "\"Id\": \"${SVWR_ID}\"" "${tmp}/pkg/metadata.json"
rm -rf "${DEST}"
install -d -m 0755 "${DEST}"
cp -r "${tmp}/pkg/." "${DEST}/"
# Readable by everyone, writable only by root; its helper script (contents/ui/tools/*.sh) stays executable.
chmod -R u=rwX,go=rX "${DEST}"
echo "install-video-wallpaper: Smart Video Wallpaper Reborn ${SVWR_VERSION} installed in ${DEST}"
