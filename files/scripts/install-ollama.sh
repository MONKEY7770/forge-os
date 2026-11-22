#!/usr/bin/env bash
# Installs the official Ollama Linux build into /usr during the image build.
# The version is pinned and the download must match a known sha256, or the build stops.
#
# To update Ollama:
#   1. Pick a release at https://github.com/ollama/ollama/releases
#   2. Set OLLAMA_VERSION to it (without the "v").
#   3. Open that release's sha256sum.txt and copy the hash on the
#      ollama-linux-amd64.tar.zst line into OLLAMA_SHA256.
set -euo pipefail

OLLAMA_VERSION="0.40.16"
OLLAMA_SHA256="87bfafb2acbc89b2e6a1be0a9043de6aa75fb450696f86bc426e65fc448f788d"
OLLAMA_FILE="ollama-linux-amd64.tar.zst"
OLLAMA_URL="https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/${OLLAMA_FILE}"

# Check a file against the pinned hash. Fails (and stops the build) on any mismatch.
verify() {
  echo "${OLLAMA_SHA256}  $1" | sha256sum -c -
}

# Test mode: VERIFY_ONLY=/some/file only checks that file, with no download or install.
if [[ -n "${VERIFY_ONLY:-}" ]]; then
  verify "${VERIFY_ONLY}"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

curl -fsSL --retry 3 --proto '=https' -o "${tmp}/${OLLAMA_FILE}" "${OLLAMA_URL}"
verify "${tmp}/${OLLAMA_FILE}"
tar --zstd -xf "${tmp}/${OLLAMA_FILE}" -C /usr

/usr/bin/ollama --version || true
