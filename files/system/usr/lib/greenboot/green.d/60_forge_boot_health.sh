#!/bin/bash
# Forge OS: after every good start, remember whether the NVIDIA driver was loaded (with the NVIDIA
# cards and Secure Boot state), so the start-up check only fails on a driver that worked before
# (/usr/libexec/forge-boot-health). Recorded again 10 minutes later, for a driver that loads late.
# Never fails.
if [ -x /usr/libexec/forge-boot-health ]; then
  /usr/libexec/forge-boot-health record
  systemd-run --no-block --collect --on-active=600 --description="Forge OS: record this good start again" \
    /usr/libexec/forge-boot-health record --late >/dev/null 2>&1
fi
exit 0
