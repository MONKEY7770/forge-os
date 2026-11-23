#!/usr/bin/env bash
# Show "Forge OS" as the system's name: About this System, the boot menu and an installer's list of
# installed systems. Only display names change. ID=bazzite, VARIANT_ID and the other fields that
# Bazzite's own tools read stay as Bazzite wrote them.
set -euo pipefail
OS_RELEASE="$(readlink -f "${FORGE_OS_RELEASE:-/usr/lib/os-release}")"
SYSTEM_RELEASE="${FORGE_SYSTEM_RELEASE:-/etc/system-release}"

VERSION_ID="$(sed -n 's/^VERSION_ID="\{0,1\}\([0-9]*\)"\{0,1\}$/\1/p' "$OS_RELEASE")"
if [[ ! "$VERSION_ID" =~ ^[0-9]+$ ]]; then
  echo "brand-os-release: $OS_RELEASE has no numeric VERSION_ID" >&2
  exit 1
fi

set_field() {   # replace the field, or add it when the base has none
  if grep -q "^$1=" "$OS_RELEASE"; then
    sed -i "s/^$1=.*/$1=\"$2\"/" "$OS_RELEASE"
  else
    printf '%s="%s"\n' "$1" "$2" >> "$OS_RELEASE"
  fi
}
set_field NAME "Forge OS"
set_field PRETTY_NAME "Forge OS $VERSION_ID"
set_field BOOTLOADER_NAME "Forge OS $VERSION_ID"

# GRUB takes its distributor name from the text before " release ".
sed -i 's/^.* release /Forge OS release /' "$SYSTEM_RELEASE"
grep -q '^Forge OS release ' "$SYSTEM_RELEASE"
echo "brand-os-release: $(grep '^PRETTY_NAME=' "$OS_RELEASE")"
