#!/usr/bin/env bash
# Make sure the Forge OS tools are executable in the final image.
set -euo pipefail
chmod 0755 /usr/bin/forge-mode /usr/bin/forge-health /usr/bin/forge-config \
  /usr/bin/forge-settings /usr/bin/forge-usbkey /usr/bin/forge-api \
  /usr/libexec/forge-apply /usr/libexec/forge-core-init /usr/libexec/forge-hwcheck
# sudo ignores rule files unless they are owner-only
chmod 0440 /etc/sudoers.d/forge-core
