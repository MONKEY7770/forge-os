#!/usr/bin/env bash
# Make sure the Forge OS tools are executable in the final image.
set -euo pipefail
chmod 0755 /usr/bin/forge-mode /usr/bin/forge-health /usr/bin/forge-config \
  /usr/bin/forge-settings /usr/bin/forge-usbkey /usr/bin/forge-api \
  /usr/bin/forge-doctor /usr/bin/forge-backup /usr/bin/forge-hub \
  /usr/libexec/forge-apply /usr/libexec/forge-core-init /usr/libexec/forge-hwcheck \
  /usr/libexec/forge-power-switch /usr/libexec/forge-virt /usr/libexec/forge-doctor-fix \
  /usr/bin/forge-shared-drive /usr/bin/forge-settings-sync /usr/libexec/forge-activity-publish \
  /usr/bin/forge-appearance /usr/libexec/forge-appearance-dock \
  /usr/libexec/forge-appearance-background \
  /usr/libexec/forge-appearance-boot /usr/libexec/forge-boot-apply \
  /usr/libexec/forge-boot-health \
  /usr/libexec/forge-appearance-theme \
  /usr/bin/forge-game /usr/libexec/forge-mode-apply /usr/libexec/forge-first-user \
  /usr/lib/greenboot/check/required.d/60_forge_boot_health.sh /usr/lib/greenboot/green.d/60_forge_boot_health.sh \
  /usr/libexec/forge/powerprofilesctl /usr/libexec/forge-model-guard \
  /usr/libexec/forge-apps /usr/bin/forge-vm
# sudo ignores rule files unless they are owner-only
chmod 0440 /etc/sudoers.d/forge-core
# Claude Desktop is in the image now and updates with it, so its repo is turned off (as Bazzite does
# with its own extra repos). Its signature checks stay on if someone turns it back on.
sed -i 's/^enabled=1$/enabled=0/' /etc/yum.repos.d/forge-claude-desktop.repo
grep -qx 'enabled=0' /etc/yum.repos.d/forge-claude-desktop.repo
# One welcome at the first login: Forge Welcome. Bazzite Portal no longer starts by itself for new
# accounts (it stays in the app menu).
rm -f /etc/skel/.config/autostart/bazzite-portal.desktop
# tuned-ppd has power-profiles-daemon's D-Bus API but no powerprofilesctl: Forge's stand-in fills in.
[ -e /usr/bin/powerprofilesctl ] || ln -s /usr/libexec/forge/powerprofilesctl /usr/bin/powerprofilesctl
