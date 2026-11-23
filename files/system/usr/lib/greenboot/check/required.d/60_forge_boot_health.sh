#!/bin/bash
# Forge OS start-up check for automatic rollback (greenboot runs it at every start).
# What it checks, and when: /usr/libexec/forge-boot-health (read its header).
# Only exit code 3 from it means "this start is broken". Anything else (a missing tool,
# an error inside the check) passes, so it can never roll back a good update by mistake.
[ -x /usr/libexec/forge-boot-health ] || exit 0
/usr/libexec/forge-boot-health check
[ "$?" -eq 3 ] && exit 1
exit 0
