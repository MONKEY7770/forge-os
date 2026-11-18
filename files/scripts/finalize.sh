#!/usr/bin/env bash
# Make sure the Forge OS tools are executable in the final image.
set -euo pipefail
chmod 0755 /usr/bin/forge-mode /usr/bin/forge-health /usr/bin/forge-config \
  /usr/bin/forge-settings /usr/libexec/forge-apply
