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
  /usr/bin/forge-game /usr/libexec/forge-mode-apply
# sudo ignores rule files unless they are owner-only
chmod 0440 /etc/sudoers.d/forge-core
