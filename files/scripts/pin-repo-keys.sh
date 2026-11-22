#!/usr/bin/env bash
# Runs at image build time, before any package is installed from the extra repos.
# Downloads each extra repo's signing key and checks it against the fingerprint pinned
# below. If a key changes (or extra keys appear), the build stops until a person has
# checked the new key and updated the pin. The repo files in /etc/yum.repos.d point dnf
# at the checked key files, so dnf never fetches a key on its own.
#
#   pin-repo-keys.sh [name...]   check only these repos (default: all)
#
# FORGE_KEY_DIR and FORGE_KEY_TEST_DIR are only for tests.
set -euo pipefail

KEY_DIR=${FORGE_KEY_DIR:-/etc/pki/rpm-gpg}

# name | key URL | pinned primary key fingerprint(s), space separated
PINS=(
  # Claude Desktop (claude-desktop-debian project, repackages Anthropic's official Linux app).
  # Fingerprint read 2026-11-19 from the key served at this URL (armor checksum verified).
  "claude-desktop|https://pkg.claude-desktop-debian.dev/KEY.gpg|87494CF73ACC0F23AA9557B86E29E413B912E0F1"
  # Tailscale. Key ID 458CA832957F5868 is confirmed by an independent source; the full
  # fingerprint is not yet confirmed from the key itself: the first CI build checks it.
  "tailscale|https://pkgs.tailscale.com/stable/fedora/repo.gpg|2596A99EAAB33821893C0A79458CA832957F5868"
)

check_one() {
  local name=$1 url=$2 want=$3 tmp got
  tmp=$(mktemp -d)
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN
  if [ -n "${FORGE_KEY_TEST_DIR:-}" ]; then
    cp "$FORGE_KEY_TEST_DIR/$name.key" "$tmp/key"
  else
    curl -fsSL --proto '=https' --tlsv1.2 --retry 3 "$url" -o "$tmp/key"
  fi
  mkdir -m 0700 "$tmp/gnupg"
  got=$(GNUPGHOME="$tmp/gnupg" gpg --batch --with-colons --show-keys "$tmp/key" 2>/dev/null \
        | awk -F: '$1=="pub"{p=1; next} p && $1=="fpr"{print $10; p=0}' | sort | tr '\n' ' ')
  want=$(printf '%s\n' $want | sort | tr '\n' ' ')
  if [ -z "$got" ] || [ "$got" != "$want" ]; then
    echo "pin-repo-keys: the signing key for $name does not match the pinned fingerprint." >&2
    echo "  expected: $want" >&2
    echo "  got:      ${got:-(no key)}" >&2
    echo "Check the new key with the project, then update the pin in files/scripts/pin-repo-keys.sh." >&2
    return 1
  fi
  install -d -m 0755 "$KEY_DIR"
  install -m 0644 "$tmp/key" "$KEY_DIR/forge-$name.gpg"
  echo "pin-repo-keys: $name key OK ($want)"
}

want_names=("$@")
found=0
for entry in "${PINS[@]}"; do
  IFS='|' read -r name url fprs <<<"$entry"
  if [ ${#want_names[@]} -gt 0 ] && [[ ! " ${want_names[*]} " == *" $name "* ]]; then
    continue
  fi
  found=$((found + 1))
  check_one "$name" "$url" "$fprs"
done
if [ "$found" -eq 0 ]; then
  echo "pin-repo-keys: no repo called: ${want_names[*]}" >&2
  exit 1
fi
