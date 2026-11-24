# Forge OS security process and risk register

No process can promise zero bugs. What this file promises is that every security-relevant change goes through the same checks, every finding is written down, and nothing is called "working" without evidence.

## The process (every change)

1. **Threat notes first.** Before writing code: who could attack this (an AI agent, a web page, another local user, a hostile USB stick, someone on the network), and what must never happen.
2. **Tests for every rule.** Each security rule gets an automated test, and each fixed finding gets a regression test so it can't come back.
3. **Independent review.** A separate reviewer that did not write the code tries to break it, and reports CONFIRMED (reproduced) or PLAUSIBLE (reasoned) findings.
4. **Fixes are re-reviewed.** Fixes are checked again by a reviewer that did not make them. (Until 2026-11-19 this step was missing: Phase 2 and 3 fixes were only checked by their own tests.)
5. **Every finding is logged below**, including false alarms and anything deliberately not fixed.
6. **Honest status labels:**
   - **Verified on hardware:** tested on a real Forge OS install.
   - **Tested (real tool):** tested against the real program here (for example real `cryptsetup` on a disk image).
   - **Tested (simulated):** tested with fakes standing in for hardware or services.
   - **Untested.**
7. **Automated checks on GitHub** (written 11-19, first run still to come): unit tests, Python security linting (bandit), shell checks (shellcheck) and secret scanning (detect-secrets) run before every build, and the Bazzite base images' signatures are checked. Images are only built and published when all of that passes; afterwards the published images' signatures are checked again. Not added yet: gitleaks, CodeQL.

## Findings log

| Date | Area | Finding | Severity | Status |
|---|---|---|---|---|
| 11-19 | forge-apply | A setting value with a line break could inject a root command into a systemd file | Critical | Fixed, regression test |
| 11-19 | forge-apply | Model folder could be `/` or `/etc`, then chowned by root | Critical | Fixed (allowlisted folders), regression test |
| 11-19 | Forge Core | Decimal or infinite numbers crashed the API | Medium | Fixed, regression test |
| 11-19 | Forge Core | Self-restart could cut off its own settings change | Medium | Fixed (delayed restart outside the service) |
| 11-19 | forge-api | Token could be sent to another program squatting the port | Medium | Fixed (checks the port owner) |
| 11-19 | Forge Core | Settings apply had no lock and no timeout handling | Low | Fixed |
| 11-19 | sudo rule | Extra arguments accepted | Low | Fixed (exact arguments only) |
| 11-19 | Policy | Approval requests hid which setting would change | High | Fixed, re-verified by independent review 11-19 |
| 11-19 | Agents | Web fetch could be tricked into reaching local services (DNS rebinding), no size cap | High | Fixed: address pinning and size cap re-verified; time cap fixed later (R3) |
| 11-19 | Agents | No cap on tool calls per step, notices file unbounded | Medium | Fixed, re-verified by independent review 11-19 |
| 11-19 | Storage | Databases never cleaned up | Medium | Fixed (daily cleanup), re-verified by independent review 11-19 |
| 11-19 | Forge Core | Agent data readable by other local users | Medium | Fixed (private folder), re-verified by independent review 11-19 |
| 11-19 | Agents | Extra cloud providers skipped the cloud approval | Medium | Fixed, re-verified by independent review 11-19 |
| 11-19 | Policy | Approvals expired early or stayed pending after a run ended | Low | Fixed, re-verified by independent review 11-19 |
| 11-19 | forge-health | Agent text could add links to notifications | Low | Fixed (escaped), re-verified by independent review 11-19 |
| 11-19 | Gateway | Ollama "-cloud" models bypassed the cloud permission | Medium | Fixed, re-verified by independent review 11-19 |
| 11-19 | Policy, Gateway | Broken config files failed open or crashed | Medium | Fixed, re-verified by independent review 11-19 |
| 11-19 | Agents | Model folder setting path (reported again) | n/a | False alarm: already fixed by the allowlist |
| 11-19 | forge-settings | Settings page handed its token to anyone on the PC, DNS rebinding possible | Medium | DNS rebinding fixed and re-verified. Local token theft fixed later (R4) |
| 11-19 | forge-hwcheck | USB sticks counted as disks, so a plugged key stick looked like new hardware | Low | Fixed (R10) |

### R1 to R15: second independent review (11-19), fixed and re-reviewed

Every fix followed: reproduce with a test that fails, fix, test passes, then a reviewer who did not write the fix tries to reproduce the original problem. Reviews ran in rounds; when a reviewer found a gap, it was fixed and reviewed again.

| ID | Sev. | Area | Original problem | What changed | Independent review | Test level |
|---|---|---|---|---|---|---|
| R1 | High | forge-usbkey | An interrupted add could leave a working key slot with no record, invisible to list and revoke | Rebuilt over 8 review rounds: the password is read and checked by the tool first and passed on stdin; a pending record (with the header change counter) is written before the key is added and swapped for the final record after; interrupted adds are finished before any other write; unclear records are never revoked automatically; revoke refuses any slot the typed password opens; key checks only read cryptsetup's "no match" answer as no; the password slot can't be adopted; one name can't cover two sticks; a lock stops two runs at once; unreadable, unplugged or detached-header disks block adds and stop revoke/remove from reporting success; LUKS1 and swap lines are ignored | Rounds 1 to 7 each found new gaps (one of them High: an unreadable disk treated as not encrypted), all fixed with tests shown failing first. Round 7: every goal held under single faults and kills at every cryptsetup call. Round 8: those last fixes VERIFIED, no regressions in 400+ fault runs, two new Low items (below). Status: FIXED and independently verified, with the Low residuals listed below | Tested (real tool): real cryptsetup on LUKS2 images, faults, kills and read errors injected |
| R2 | Med | forge-apply | Model folder could be swapped for a symlink so root chowns any file | Folder made and chowned through file descriptors, no symlinks followed, existing folders never chowned | VERIFIED: 20,000 to 30,000 race attempts per mode, nothing outside the folder chowned | Tested (real tool), users simulated |
| R3 | Med | Agent web fetch | Slow server could stall an agent for hours | One deadline for every step; round 2 (reviewer found DNS outside it): lookup runs under the same deadline | VERIFIED twice: every stage stops on time, 400 stuck lookups did not crash | Tested (simulated servers, no real DNS here) |
| R4 | Med | forge-settings | Another local user could get the page and token first | Owner check, one-time launch key, refuses root; round 2 (reviewer bypassed the owner check with the same port from another address): exact address match, loopback only, exactly one socket | Round 1 PARTIAL (bypass confirmed), round 2 VERIFIED live with two users | Tested (real tool), IPv6 not tested |
| R5 | Med | Forge Core | One admin token readable by every program you run | Two tokens: admin (root and forge only), viewer (read-only, no secrets, audit or policy) | VERIFIED: 16 path spellings, method overrides, file modes | Tested (real tool) |
| R6 | Med | forge-usbkey | Stick found by name only; unsafe mounts | Stick must be a real USB disk (lsblk), exactly one match, private nosuid/nodev/noexec mount; round 2: mount inside a root-only folder with root-only file modes | VERIFIED (hostile lsblk data); mount modes checked by reading code only | Tested (simulated lsblk), no real USB |
| R7 | Low | forge-apply | A FIFO, symlink or huge file in the import folder could hang or mislead root | File descriptor open, no symlinks, no FIFOs, regular files only, 1 MB cap, lock timeout | VERIFIED: symlink, FIFO, device, 2 MB, path tricks | Tested (real tool) |
| R8 | Low | Agent web fetch | IPv6 forms with a private IPv4 inside were allowed | NAT64 and fec0 refused; round 2 (reviewer found SIIT ::ffff:0:x): refused too | VERIFIED twice, every encoding listed in the review | Tested (unit) |
| R9 | Low | Forge Core | Cleanup only after 24 hours of uptime | Runs 60 s after start, then daily, errors can't stop it | VERIFIED | Tested (unit) |
| R10 | Low | forge-hwcheck, forge-health | USB sticks counted as hardware; names not escaped | USB and removable disks skipped, names escaped; round 2 (reviewer found SD cards and Thunderbolt drives): skipped too, internal eMMC still counted | VERIFIED twice | Tested (simulated sysfs) |
| R11 | Low | forge-health | Text starting with "-" read as an option | "--" before the text | VERIFIED | Tested (fake notify-send) |
| R12 | Low | Policy | Some secret names not hidden; long writes cut off in approvals | More names and URL secrets hidden; approvals keep 200 KB with a checksum | VERIFIED | Tested (unit) |
| R13 | Low | Gateway | Local alias of a cloud model counted as local | Ollama asked about every model, unknown means cloud | VERIFIED | Tested (mocked Ollama) |
| R14 | Low | Supply chain | Ollama unverified; actions on tags | Ollama v0.40.16 pinned with its sha256; every action pinned to a commit (round 2: the signing job's blue-build action too) | VERIFIED: checksum and commits checked independently | Tested (real files) |
| R15 | Low | forge-usbkey | Failed record removal ignored | Reported, plus a cleanup command | VERIFIED | Tested (real tool) |

### Residual risks left by these fixes

- **R1, interrupted add plus a manual password (Low):** if forge-usbkey is killed, or the power fails, while it is adding a key (a few seconds), and the very next change to the disk is a password you add by hand that lands in the same slot, the tool later treats that slot as the USB key. Revoking it with a different password then removes that hand-added password. It can never be the last way in. A fix exists (give our key slot a recognisable setting) but has trade-offs on low-memory machines: your decision.
- **R1, two passwords (Low):** with two password slots, a stick whose forge_key is password #2 can be adopted as a USB key, so revoking it removes password #2. Password #1 keeps working.
- **R1, renamed devices (Low, plausible):** a crypttab line using a kernel name (like /dev/sdb1) that now points at a different readable non-LUKS disk is skipped. Lines using UUID= (the default) are not affected.
- **R1, detached headers and LUKS1:** volumes with header= block adds and stop revoke from reporting success (safe, but those volumes can't use USB keys). LUKS1 volumes can't use USB keys.
- **R3:** a stuck name lookup leaves one background thread until the system resolver gives up.
- **R8:** an ISP's own NAT64 prefix can't be recognised by address alone.
- **R10:** NVMe drives seen through the multipath subsystem, "SDcombo" cards and SD Express cards may still count as hardware.
- **R12:** some secret names are still shown (pwd, session, jwt, bearer, ssh_key, mnemonic, connection_string, free text like password=...); agent results in the audit log are not redacted.
- **R13:** an alias re-pointed to a cloud model during a run is noticed only on the next run (5 minute cache).
- **R14, supply chain (mostly done 11-19):** the claude-desktop and tailscale repo files are now part of the image, and their signing keys are checked against pinned fingerprints before use (the tailscale fingerprint is not fully confirmed yet: the first CI run shows it). Base images stay on a moving tag but are only used when Universal Blue's signature checks out. The BlueBuild tool is one exact release with its signature checked. The Secure Boot key URL points at one commit (its sha256 is noted, but the ISO action does not check it). Still open: Flatpaks are unpinned, and the claude-desktop repo is a third party you chose to keep.

### New findings from the reviews (open, not fixed: need approval)

| Sev. | Area | Finding | Status |
|---|---|---|---|
| Low | forge-apply | Deleting the new model folder at the right moment aborts the rest of the apply | Fixed, regression test |
| Low | forge-core-init | Token written before its permissions are tightened if an empty file already exists | Fixed (new private temp file, then moved), regression test |
| Low | forge-apply | /etc/crypttab rewritten in place, not atomically; trailing comments dropped | Fixed (atomic, keeps owner, labels, comments and odd bytes), regression tests |
| Low | forge-api | Forge Core restarting between the port check and connect could hand the admin token to a squatter | Fixed (owner of the connected socket checked before sending), regression test |
| Low | forge-apply | Model folder on a user FUSE mount could hang applies (needs user_allow_other) | Fixed (FUSE refused; the step runs in a child with a 20 s limit), regression tests |
| Low | forge-settings | Rejected requests from other users keep the page alive | Fixed (other users' connections are now closed on arrival), regression tests |
| Low | forge-settings | No anti-framing headers | Fixed, on every response, regression tests |
| Info | forge-settings | Launch URL printed to stdout (may reach the journal) | Fixed (only printed when you ask to open it yourself) |
| Info | forge-usbkey | Test-only environment variables still read in production (sudo clears them) | Open |
| Low | forge-usbkey | A disk changed from LUKS2 to LUKS1 by restoring a LUKS1 header backup is skipped even though a LUKS2 backup header survives (needs an admin mistake plus manual header repair; cryptsetup can't open it as is) | Fixed (backup headers are checked too), regression test |
| Low | forge-usbkey | A crypttab line with header= (detached header) permanently blocks adds and makes revoke always exit 1, with a wrong "connect it" message | Message fixed and adds work, regression test; detached-header volumes still can't use USB keys (fails safe) |
| Info | forge-usbkey | A stick key added by hand to a LUKS1 disk still works after "every USB key is revoked" (LUKS1 is not managed, and the tool says so) | By design, noted |
| Info | Forge Core | Viewer token can read agent run transcripts | By design, noted |
| Info | Tests | test_core fails if test_core_hardening is imported first (settings read at import) | Open, test hygiene |

### Latest review rounds (11-19): fixed

All found by the independent reviewer in the newest code; each has a test shown failing on the old version.

- forge-apply and forge-config: undo order depended on the clock (snapshots now numbered); odd `--undo` and `--import` arguments; broken, linked or FIFO snapshot files; very long text values; `forge-config set` sent every setting instead of only the changed one; crypttab with non-UTF-8 bytes; a hanging model folder step.
- forge-settings: another local user could open many idle connections and use up the page's threads. Their connections are now closed as they arrive, and the total is capped.

The independent re-review of those fixes confirmed they hold (other users' connections: 0 of 60 kept; six concurrent `forge-config set` runs all saved; the round 3 code fails the new tests). It found these, all fixed with a test shown failing first unless marked:

| Sev. | Area | Finding | Status |
|---|---|---|---|
| Medium | forge-apply | `forge-config set` with one bad value in the settings file wiped every other setting, and the snapshot could not bring them back | Fixed: the save is refused and the file left as it is; snapshots keep the old values as they were |
| Medium | forge-core-init, forge-apply | Under Forge Core's strict umask, /etc/forge (and any config file forge-apply creates) could become owner-only, so users and services silently got defaults | Fixed: the folder is set to 0755 at every start, forge-apply uses the normal umask, settings.json stays readable |
| Low | forge-apply | A crypttab comment containing an unusual line break (U+2028 and others) was split, and its tail got the USB key | Fixed: split only where systemd splits |
| Low | forge-hwcheck, metrics, power switch | A wireless mouse or controller battery made a desktop count as a laptop (and "hardware changed" on every plug) | Fixed: device batteries ignored |
| Low | doctor | An unreadable /etc/crypttab was reported as "not encrypted" | Fixed: "not sure" |
| Low | forge-doctor | Two `--save` runs in the same second crashed | Fixed |
| Low | metrics | Two collectors upgrading one old database at once: one failed to start | Fixed: one write transaction |
| Low | forge-settings | Another user can still flood the connection queue and slow the owner's page | Mitigated (larger queue); a slowdown, never access |
| Low | forge-apply | Snapshot counter overflow after ten billion saves | Fixed: renumbered |
| Low | forge-apply | A malformed history line crashed `--history` | Fixed |
| Low | forge-health | A new check alone still said "settings were re-applied" | Fixed. Its other half (sysfs text not escaped) was a false alarm: the notify helper already escapes |
| Not sure | forge-power-switch | With Ollama stopped, another user could listen on its port and feed many model names to the root switch | Hardened anyway: only asks a running Ollama service, at most 8 models, 5 minute limit |

Also found while fixing these: two test files used a fixed folder path from the development machine, so they would have failed on GitHub (and the reviewer's copy of those two tests checked the live code instead of the frozen copy). Fixed, with a test that stops it coming back.

The next independent re-review confirmed all of those fixes hold, and reviewed the new features below. It found:

| Sev. | Area | Finding | Status |
|---|---|---|---|
| Medium | forge-apply | The normal umask (from the fix above) made the apply lock file readable, so any user could hold it and block every settings apply | Fixed: owner-only lock, repaired if an old one is found |
| Medium | forge-virt | An administrator removed from wheel kept the VM rights Forge gave them | Fixed: taken back on the next run; people Forge didn't add are never touched |
| Low | forge-virt | libvirtd enabled but failed to start was not recorded, so turning VMs off left it enabled | Fixed |
| Low | forge-hwcheck | The notice said settings were re-applied even when the re-apply failed | Fixed: written after the re-apply, from its result |
| Low | doctor | A crypttab with only comments showed "Disk encryption: on" | Fixed |
| Low | forge-apply | Config files an older version made owner-only stayed that way | Fixed: every generated file is set to 0644 |
| Low | forge-apply | The crypttab backup was briefly readable by everyone while being copied | Fixed: created owner-only from the start |
| Low | forge-apply | A deeply nested history line still crashed `--history` | Fixed |
| Info | forge-doctor | `--save` into an unwritable home crashed | Fixed |
| Info | doctor | Devices in 5-digit PCI domains (Intel VMD) were missed in IOMMU groups, so a GPU could be called "ready" wrongly | Fixed |
| Info | forge-virt | A file where its swtpm folder goes crashed it | Fixed |
| Info | forge-virt | With a broken settings file, forge-virt changes nothing while forge-apply falls back to defaults (VMs off) | By design: never tear down on a broken file |
| Info | welcome page | Save sent every setting, so a change made meanwhile elsewhere was reverted | Fixed: sends only its three settings, as a merge. The main Settings page still saves the whole list (it shows them all) |
| Info | welcome page | If the browser failed to open at first login, the page never showed again | Fixed: recorded only once the page is served |
| Not sure | forge-virt | Turning VMs off during a long install could be lost (systemd joins the second start into the running one) | Fixed anyway: the run re-reads the setting and follows it; it also has its own lock |

Still "not sure", needs a real Bazzite PC: whether `NoNewPrivileges=yes` lets usermod, groupadd and restorecon change SELinux domains in forge-virt; whether `usermod -aG libvirt` works when the libvirt group only exists in /usr/lib/group (Bazzite's own recipe has the same question). (The power switch's worst case, 12 calls with a 60 s limit each, was longer than its unit limit: fixed with a 15 s limit per model and an 8 minute unit limit.)

The re-review of that round confirmed every fix (no High or Medium findings) and found these, all fixed with a test shown failing first:

| Sev. | Area | Finding | Status |
|---|---|---|---|
| Low | forge-apply, forge-virt | settings.json was saved in place; a reader catching it half-written sees "all defaults", which could turn the VM setup off | Fixed: written to a new file and swapped in |
| Low | forge-virt | If the wheel group couldn't be read, everyone Forge added lost VM rights | Fixed: nobody is removed then |
| Low | forge-hwcheck | If the re-apply could not start, or hwcheck was stopped during it, no notice was written at all | Fixed: a notice is written first, then updated if the re-apply worked |
| Info | forge-apply | Unchanged drop-ins made owner-only by an older version were not repaired | Fixed |
| Info | forge-doctor | A write error during `--save` crashed and left a partial file | Fixed |

Noted, by design: a user Forge added keeps VM rights only while in wheel, even if an admin later wanted them to keep it; a setting changed more than three times during one long forge-virt run is followed on the next apply; two logins at the same moment could both show the welcome page.

The re-review of those five confirmed all of them, and found only Low and Info items, all fixed with a test shown failing first: the hardware notice now says "pending" during its re-apply (forge-health waits up to 15 minutes for it) and is written in one step; temp files from a killed settings save are cleaned up at the next save; a full disk now gives "Not saved" with the old settings untouched and no extra undo point (old undo points are only removed after a save works); settings.json.bak is readable like the settings.

Not sure, needs a real Bazzite PC: the new settings file gets the folder's SELinux label instead of keeping the old file's (expected to be the same, etc_t).

The re-review of those confirmed them, and found one Medium that was already there before: on a completely full disk the snapshot step failed with a traceback, and a size-limited write could leave a cut-off snapshot that broke undo. Fixed with a test shown failing first: snapshots are written whole before they get their name, every save step fails with a clean "Not saved" and leaves the settings, the one-step undo copy and the undo points as they were; a history that can't be written after a save is a note, not a crash; the undo copy is never made through a link (a linked settings file is now refused); a pending notice from a clock that was ahead is shown at once. One Info item stays: a pending notice that only finishes after 15 minutes is shown twice. (The check for leftover temp file names now needs an exact match.)

The re-review of those confirmed every fix with a real full disk and size limits (no High or Medium findings). Of its four Low or Info items, two were fixed with a test shown failing first: the "takes effect after a restart" note is no longer lost when the history can't be written, and snapshot temp files left by a power loss are cleaned up. These two small fixes have not had their own independent re-review. Accepted: a full disk can leave an empty seq.tmp (reused next time), and a failed save uses up a snapshot number (harmless gaps). Not sure: the settings undo copy is not fsynced before its rename; a symlinked settings file can still be read but no longer saved (by design).

### New features (11-19): threat notes

- **forge-virt** (root, started by forge-apply): only acts on the "Virtual machines" setting, which only an administrator can change. Every name it passes to a command comes from a fixed list or a strict pattern, its record is root-only and never followed through a link, and turning it off undoes only what it recorded. It gives the libvirt group to administrators (wheel), which is close to root access: they already have sudo.
- **GET /api/doctor** (Forge Core): admin token only (it describes the PC's security setup), read-only, one report at a time, reused for 30 seconds, errors without details.
- **Welcome page**: served by forge-settings with the same owner check, one-time key and token as the settings page. The report shown in it comes from /sys and other system files, so the page only inserts it as plain text.
- **forge-power-switch** (root, from a udev rule): reads only validated settings and the charger state; model names are checked against a strict pattern before `ollama stop`.

### Second build round (11-19, evening): new features and their reviews

Built by eight parallel builders, then the Forge Dashboard, each with failing-first tests: shared drive with Windows, encrypted home backups (restic), activity timeline and update status, `forge-doctor --fix`, settings sync between PCs, the ISO boot test, a read-only Windows pre-install checker, the creative MCP hub connection (the hub itself is never changed) and the Forge Dashboard. Four independent reviews followed (privileged parts, user tools with untrusted data, CI and supply chain, the dashboard), then re-reviews of every fix. No High findings in any round.

| Sev. | Area | Finding | Status |
|---|---|---|---|
| Medium | forge-backup | Another user could make a restore write into your home (a folder they made, or a link planted while restic starts) | Fixed: the target must be a new 0700 folder; every parent is checked without following links; FUSE filesystems are refused |
| Medium | forge-hub | The hub dashboard's token was on xdg-open's command line, readable by every user | Fixed: a 0600 redirect page in a private runtime folder |
| Medium | forge-shared-drive | A drive with no UUID skipped the "remember my drive" check | Fixed: UUID and PARTUUID are remembered; a missing id counts as a different drive |
| Medium | ISO boot test | Any colourful screen passed, even an ISO with no kernel | Fixed: a test copy that differs only in its kernel console lines (every other file checked by sha256) must show the kernel and the installer starting on serial |
| Low (my merge slip) | finalize.sh | A lost line continuation would have run forge-apply as root during the image build | Fixed, with a test that every tool is in one chmod command |
| Low | timeline | Decoy file names and flooding could hide other users' activity; text was cut before redaction | Fixed: exact uid names owned by that uid, caps per user and source, future times clamped, redaction before cutting |
| Low | policy | An early cut for speed could split a secret so part of it showed | Fixed: the partial word at the early cut is dropped |
| Low | forge-doctor-fix | Fixes were recorded as done by "root"; a test seam worked in production | Fixed: the real user is passed on; the seam needs a test marker |
| Low | updates API | One slow refresh made every caller wait | Fixed: callers get the last result while one refresh runs |
| Low | polkit rule | Lookalike labels (for example with a long s) could match the shared drive rule | Fixed: ASCII labels only |
| Low | settings sync | Decoy files could hide a PC; a new export date alone notified again; odd characters in the folder setting | Fixed (decoys: partly, anyone who can write the folder can still delete a PC's file; a "too many files" notice warns) |
| Low | dashboard | Huge tool output used lots of memory; rollback could run twice; any web page could keep it alive; sync showed only a count before applying; bidi characters in dialogs; doctor runs not limited; launch key on the command line; long VM names | Fixed |
| Low | CI | Free-text Fedora version reached a privileged shell; guest output could fake workflow messages; workflow tests matched text | Fixed: a fixed choice, stop-commands wrapping, YAML-based tests (17 of 17 mutations caught) |
| Low | Windows checker | Hardware names with control characters could fake lines in the terminal | Fixed: every probe string cleaned |

Accepted and open:
- One administrator can still put up to 100 of their own entries at the top of the timeline (only wheel members can write the activity folder).
- The drive "pin" stops mistakes and casual swaps, not someone who copies your drive's ids on purpose.
- Settings sync: a file swapped in the milliseconds between the dashboard's check and the apply is not caught (the apply still validates every value and asks for the password).
- `forge-hub setup` runs the clone's own build scripts (it is your own repo).
- Not sure until real hardware: polkit and udisks prompts, the Flatpak browser opening the redirect pages in the runtime folder, anaconda's exact serial lines in Fedora 45, whether the ISO action writes a CHECKSUM file.

Two more re-review rounds checked those fixes. They found:

| Sev. | Area | Finding | Status |
|---|---|---|---|
| High | ISO boot test | A crafted ISO with no kernel could still pass: its GRUB config printed itself (with the secret test mark) using GRUB's own "cat" command | Fixed: a GRUB config that uses commands able to print files or build commands (cat, hexdump, source, configfile, eval and others) is refused, also when split with quotes; shown failing against the crafted ISOs in real QEMU, while a good ISO still passes with Secure Boot off and on |
| Medium | policy | The text kept back near the early cut (1 KB) was smaller than the longest secret pattern (4 KB), so part of a long quoted secret could show | Fixed: 8 KB kept back, the scan grows until that holds; tested with the reviewer's cases |
| Medium (my regression) | policy | My first fix for the split-secret edge cut 512 characters early and could split a token there | Replaced by the approach above |
| Medium | ISO boot test | GRUB text (menu titles, echo) could print the installer's start lines | Fixed: a fresh random mark goes only on the kernel lines, and only the kernel's own "Command line:" line with that mark counts, before any installer line |
| Low | dashboard | A tool started through pkexec that could not be stopped crashed the request, and could allow a second rollback | Fixed |
| Low | dashboard | Rollback dialog wording could be wrong when the running version is the default but not the newest | Fixed: it names a version only when certain |
| Low | CI tests | Workflow tests could be fooled (job or step "if: false", "\|\| true", extra flags, a second download after the hash check, shell overrides) | Fixed: workflows are parsed as YAML and each gate's exact command is required; 40 of 40 boot-test mutations and 8 of 8 gate mutations are caught |
| Low | ISO boot test | File names with line breaks were skipped by the file comparison; odd line endings; Ctrl+C left gigabytes behind | Fixed |
| Low | Windows checker | Some invisible characters outside the basic plane survived cleaning | Fixed |
| Low | CI | The secret scan baseline had old line numbers, so the first push would have failed the gate | Fixed; note: any edit that moves a flagged line needs the baseline refreshed (run the same command CI runs, then commit the file) |

Limit of the ISO boot test, by design: it catches broken ISOs. It can never prove that an ISO is honest, because a deliberately crafted ISO could boot a tiny real kernel that prints the installer's start lines. What protects against a tampered ISO is the signed image and the locked-down build pipeline, not this test.

### Third build round (11-20 to 11-22): customization, release safety, gaming, Thai

Built by parallel builders with failing-first tests, each part reviewed independently, every fix re-reviewed (up to four rounds), then merged and tested together: Settings cleanup (6 sections, search) with a per-user Appearance framework; background (picture, slideshow, video, Wallpaper Engine, colours, crash guard); dock styles (stock panel, floating clean, animated with Krema); custom bootup (boot splash, login screen, desktop splash, lock screen); Theme section; appearance presets with export, import and sync; Thai for Settings and the Dashboard; automatic rollback checks (greenboot); stable and testing update channels; version numbers, changelog and SBOM; a CI screenshot test; MangoHud overlay; automatic game mode; a monthly backup restore test; a Dashboard log viewer.

| Sev. | Area | Finding | Status |
|---|---|---|---|
| High (from a fix) | Appearance media library | A damaged or missing index made the new cleanup delete every picture and video, even when only opening Settings | Fixed: nothing is ever deleted; unknown files go to a quarantine folder; a damaged index makes the library read-only until it is repaired or rebuilt from the files |
| High | Automatic rollback | The NVIDIA check failed on every update where the driver never loads (for example Secure Boot without the key enrolled), so every update rolled back and the PC rebooted while in use | Fixed: it only fails when the driver loaded on the last good start with the same card and Secure Boot state |
| Medium | Game mode | A password-free polkit rule to switch modes also let any program of the user cancel queued system jobs (polkit never sees the job mode) | Fixed: rule removed; a small hardened root service takes one of five words from the admin at the PC (SO_PEERCRED plus the active seat0 session) |
| Medium | Automatic rollback | Leftover greenboot marks counted as a new update, turning rollback off left a GRUB countdown running, and Windows boots during a countdown could end with GRUB starting the failed entry | Fixed and checked with a simulation of greenboot 0.16.4 and 0.16.6, GRUB and Windows boots |
| Medium | Update channels | Going back from the boot menu left updates on testing; the version label could be empty, and a bad one reached latest before the check | Fixed: decisions use the default deployment; Forge has its own label; latest and testing move only after the signature and label are checked by digest |
| Medium | Presets | Applying a preset could ask for the admin password without the tick box; a sync folder writer could replace an existing preset | Fixed: without the box boot runs in a user-only mode; synced changes arrive as copies; owner and mode checks (reduced on drives without permissions) |
| Medium | Background | Changing the wallpaper during crash recovery could leave the desktop stopped; packed Wallpaper Engine scenes were never listed | Fixed |
| Medium | Dock | Pins made in Krema were lost; old backups re-applied to panels; one non-ASCII pin dropped the list; a timeout could leave the panel stopped | Fixed |
| Medium | Bootup | "Off" could silently fail after a slow admin step; an error part way left root changes unrecorded | Fixed: off follows the system state; every part is recorded as soon as it starts |
| Medium | Appearance framework | The 2 minute backend limit was not enforced; helpers could be killed or block apply; upload errors were invisible | Fixed: deadline on the process, output through a bounded pipe |
| Medium (existing code) | USB key unlock | A second change to its settings never reached the boot image until the next update (rpm-ostree answers "No changes." for an already tracked file) | Fixed: --force-sync |
| Low | many | Redaction gaps (password="x", pw=, psk=, curl -u, mysql -p, sshpass -p, full-width separators, other languages), restore test leftovers and a missing time limit, slow-upload and lock issues, i18n slowdown on long lines, Thai marks lost in file names, GTK apps not told about cursor and font changes, clock-skewed pins, CI runs of the same commit mixing images, and others | Fixed, each with a regression test |

Accepted and open:
- Any program running as the administrator at the PC can switch between the five configured modes (the same power `forge-mode` has). The mode service needs CAP_SYS_ADMIN to manage units; its only input is one word.
- A program of the same user named like a game can start game mode.
- Redaction cannot catch secrets written as free text ("the password was x").
- Test hooks (FORGE_* environment variables) are honoured in user tools; only your own environment can set them.
- Not seen on a real screen yet: wallpapers, dock styles, boot splash and unlock box, login and lock screens, themes, game mode, greenboot on a real update, and the new CI jobs.

### CI round (11-22 to 11-23): first real runs on GitHub

- Bazzite's images are signed keylessly: the base check now verifies the signer identity pinned to Bazzite's own build workflow on main (wrong identities shown refused).
- The archived ISO builder action pulled an image tag that never exists for a commit pin: its v1.5.0 image now runs pinned by digest, with the Secure Boot key URL pinned to a commit.
- bluez was dropped (Bazzite ships terra-bluez); checked repo keys are imported into rpm.
- Python 3.15 parses deeper JSON than before: redaction now stops at 64 levels instead of hitting the recursion limit.
- The ISO boot test now handles Fedora's UEFI image in an appended partition, compares hidden or system FAT files, and allows only the BIOS image's own position bytes (8 to 23, 2548 to 2555) to differ. A reviewer found one Medium in each version; both fixed.

### Fourth round (11-23): a real install, then an after-install bug hunt

The first VM install had no user account. Three read-only reviews of first boot, Settings and system services followed, then a review of all fixes.

| Sev. | Area | Finding | Status |
|---|---|---|---|
| High | First boot | No account could be created: the installer (Fedora Kinoite's) leaves it to Plasma Setup, which Bazzite removes | Fixed: Plasma Setup installed and enabled |
| High | First boot | Plasma Setup only checks a "done" file, so PCs with accounts (rebased from Bazzite) would get the wizard, a passwordless session allowed to create administrators | Fixed: it runs only while no account with UID 1000 to 60513 exists (read with getent; a failed lookup skips it), and comes back if account creation failed |
| High | First boot | Offline or Wi-Fi-only first boot: the one-time app install (Firefox included) never retried, so there was no browser | Fixed: retried every 3 minutes for about an hour |
| High | Virtual machines | Turning VMs on always failed: libvirt is only in /usr/lib/group, which usermod does not edit | Fixed: the group's line is copied to /etc/group (and /etc/gshadow), as Bazzite's docs say |
| Medium (security) | Ollama | "Allow my other devices to use Ollama" opened it to the whole home network: Bazzite's default firewall zone accepts ports 1025 to 65535 | Fixed: every zone except trusted rejects 11434 unless the source is Tailscale (100.64.0.0/10, fd7a:115c:a1e0::/48); checked with firewalld's own parser |
| Medium | Ollama | ReadWritePaths without "-" stopped the service while the models folder was missing | Fixed |
| Medium | Services | forge and ollama users exist only through sysusers.d; my first fix ("+" ExecStartPre) could not work, as systemd resolves User= first (review) | Fixed: a small service without User= creates them, required by both |
| Medium | Health alerts | A critical alert came back every 10 minutes for as long as a problem lasted | Fixed: each problem shown once until the PC is healthy again |
| Medium | Session | Forge user units and the welcome page also ran for system accounts (Plasma Setup's user, the login screen) | Fixed: ConditionUser=!@system, welcome only from UID 1000 |
| Medium | Background | The screen list was always empty: sysfs files report 4096 bytes | Fixed |
| Medium | Thai | Background, Dock and Bootup results were English only; the Thai password word was not redacted (split at its vowel mark, and missed inside longer names) | Fixed: 183 message templates; Thai marks kept inside words, the word found inside names |
| Medium | Redaction | A weak name's value was judged on its first 64 characters, so a secret after them stayed | Fixed: a value that goes on is hidden |
| Medium | Power | This base has tuned-ppd and no powerprofilesctl, so modes never changed the power profile | Fixed: a small stand-in using the same D-Bus API, linked only when the real one is missing |
| Medium | Wallpaper Engine | Once pinned, a COPR outage would stop every build; then a bad pin would have been dropped quietly (review) | Fixed: only "COPR unreachable" skips it; a bad pin, signature or anything else stops the build |
| Low | Branding | The installer said Fedora Kinoite and the system said Bazzite | Fixed: display names only, IDs kept |
| Low | Repos | The Claude Desktop repo failed its first metadata check on installed systems too | Fixed: turned off once installed (it updates with the image); checks stay on |
| Low | Docs | Rollback was described as after 4 failed starts; greenboot does it after 3 | Fixed |
| Low | First login | Bazzite Portal opened next to Forge Welcome | Fixed: Portal stays in the menu only |

Krema 0.13.0-1.fc45 and Wallpaper Engine 1.5^20261108.192.g8173e09-1.fc45 are now pinned with their COPR keys (18B2C521BF9E618E9F9D950C124E1771EDE61218, 44E8D456D5C4D6C6F9672E5E4BEA2DC31EEAD2C7). The keys come from COPR over HTTPS (no second source exists); the pins stop a later key change. Both were shown to install into the image in a dry run.

A new CI job boots an install with no account and checks that the Plasma Setup wizard session starts. First run (11-23, this branch's image, real QEMU): passed after 75 s; Plasma Setup's user is UID 967; its automatic login works, which also shows that Plasma Login Manager reads /etc/plasmalogin.conf.d.

Accepted and open (this round):
- A network that itself hands out 100.64.0.0/10 (some ISPs, hotspots) counts as Tailscale for Ollama's port. Virtual machines and containers can no longer reach Ollama.
- Not sure: NoNewPrivileges on forge-virt.service under SELinux (domain changes for groupadd, usermod, restorecon); BlueBuild's "Flatpaks installed" notice may go to the login screen's session.

### Fifth round (11-24): Tempered design, AI storage reserve, polish

New parts and their threat notes:
- **AI storage reserve.** Model downloads stop (HTTP 507) when they would eat into the space kept for normal use (by default 30% of the disk, 128 to 600 GB). forge-model-guard runs as root every 30 s, writes only to /run/forge (never following links), and restarts Ollama at most once per 5 minutes. It fails open: if it cannot read the disk, downloads are not blocked.
- **Setup page for the reserve.** Plasma Setup's user (UID 967) writes an INI file to /var/lib/forge-setup (0700, owned by that user); a root service reads it once, deletes it, and accepts only a [storage] section with a mode and one number in range (plain text, size-capped, never followed as a link). Anything else is refused. The choice is saved through forge-apply's normal import, like a change made in Settings.
- **Notifications.** Every Forge notice now names "Forge OS" and has an icon. Agent approval requests are no longer critical and are shown once when new ones arrive, not every 10 minutes. Notice bodies are HTML-escaped.
- **Sound.** In virtual machines only, PipeWire uses larger buffers (min quantum 2048) to stop crackling notification sounds. Real PCs keep their defaults except a cap of 2048.
- **Fonts and colours.** IBM Plex Sans, Plex Sans Thai and Plex Mono (Fedora packages, checked in this image with a dry-run install), Forge's own KDE colour schemes. Added key by key to Bazzite's /etc/xdg files, never replacing them.
- **Prism Launcher** (Flatpak, Flathub) is preinstalled.
- **AI while you play** (Settings, Modes; default Wait). The root mode service (forge-mode-switch) now also stops Ollama during a game and starts it after (Wait), or restarts it with the graphics card hidden (CPU). Its sandbox gains write access to one root-owned folder, /run/forge/gaming, and nothing else; it still only acts for an administrator at this PC's own screen. While paused, the gateway answers 503 with a plain reason, and health checks and the doctor do not report the stop as a problem.

- **Optional apps (forge-apps, root).** Installs the Flathub apps whose switch is on and deletes an app only when its switch goes from on to off. An app installed by hand while its switch is off is never touched; once its switch is on, it is treated like the others (so switching it off deletes it). After an update from an older Forge OS, apps you had deleted stay deleted (their switches are turned off). If many apps seem to vanish at once, the list is not trusted and nothing is switched off. The wizard's file is accepted only until about 2 hours after the first choice. The wizard's Apps page writes /var/lib/forge-setup/apps.ini as Plasma Setup's user; a root service reads it once without following links, accepts only "[apps]" and known names set to true or false (ASCII, size-capped), deletes it, and saves the choice through forge-apply's normal import. Android: turning it on downloads Waydroid's Android images from ota.waydro.id (the same source as Bazzite's own setup), or runs Bazzite's waydroid-nvidia-setup on NVIDIA; turning it off deletes /var/lib/waydroid only after Waydroid stopped and nothing is mounted there (a link there is removed, never followed). Its own record is in /var/lib/forge-apps (root only).
- **forge-vm (your user).** Edits only VMs your user may manage (Virtual Machine Manager's own virsh; your session first, then the system connection if one exists), only Windows VMs, refuses XML with declarations, saves a backup first (with display passwords, readable only by you), and never changes disks, networks, the TPM or the display.
- **BlueBuild's boot-time installer** now lists only Firefox, Flatseal and Mission Center: it installs its whole list again at every boot, so optional apps could not be deleted while they were in it.

Independent review of optional apps and forge-vm (no High findings; the root-side file handling held up):

| Sev. | Finding | Status |
|---|---|---|
| Medium | After an update from an older Forge OS, apps you had deleted would be installed again | Fixed: on the first run after such an update, absent apps get their switch turned off |
| Medium | The wizard's choice could wait up to 30 minutes (the installer started before the choice was marked) | Fixed: marked first |
| Medium | An empty or partial app list could turn many switches off | Fixed: more than 2 apps gone at once is not trusted; switch changes are saved together |
| Medium | A failed NVIDIA Android re-setup left Android stopped and retried every 30 minutes | Fixed: Android is started again and the re-setup waits 6 hours; a re-setup is not reported as new |
| Medium (suspected) | Android could be deleted while its files were still mounted | Fixed: deleted only after Waydroid stopped and nothing is mounted there |
| Medium (suspected) | forge-vm handed the sandboxed virsh a file it can't see | Fixed: the XML goes through standard input |
| Medium (suspected) | forge-vm looked only at your own VMs | Fixed: then the system connection, if one exists |
| Low | A wizard choice was lost if saving failed | Fixed: kept and saved at the next run |
| Low | SECURITY.md said apps not installed by forge-apps are never deleted | Fixed wording (an app is handled once its switch is on) |
| Low | forge-vm dropped display passwords | Fixed: read with --security-info; backups are readable only by you |
| Low | The wizard's user could change switches long after setup | Fixed: its file is accepted only for about 2 hours after the first choice |

Independent review of this round (no High findings; the root-run parts held up):

| Sev. | Area | Finding | Status |
|---|---|---|---|
| Medium | AI storage | An unfinished download counted at its full planned size, even after it was refused or stopped: the PC then stayed "over budget", every later download was refused, and Ollama was restarted | Fixed: only bytes written so far count; the rest is kept free only while the download is still going (changed in the last 2 minutes) |
| Medium | Setup page | It could not have worked: in Plasma Setup 6.8.3 nextRequested is a signal, not a method (the page did not even load: "Duplicate method name"), contentItem is required, and the page is not a visual item | Fixed against the module's real type description from this image. It now loads and saves in a real Qt 6.12 QML engine with stand-ins for the wizard, and its output passes the strict parser. Its metadata names the wizard as parent app, like the built-in pages |
| Low | AI storage | Ollama was restarted after a download had already finished | Fixed: only a download still going counts |
| Low | Doctor | "0 GB is kept for normal use" for users who can't read the model folder | Fixed |
| Low (security) | AI storage | Root followed a "blobs" link, which could publish the size of a root-only folder | Fixed: never followed; a link means "not sure" (downloads are not blocked) |
| Low | Setup | A folder in place of the wizard's file crashed the service and made it start again and again | Fixed: refused and removed (without following links) |
| Low | Setup | The tmpfiles line for /var/lib/forge-setup could fail if Plasma Setup's user is missing | Not a problem here: Plasma Setup's own sysusers file creates that user at every boot, before tmpfiles runs (checked in this image) |
| Low | Logs | The 30 s timer adds start and finish lines to the journal | Accepted: hiding them would also hide the guard's own messages |

bandit (11-24): no High findings; 15 Medium, all from earlier rounds and checked by hand (the patterns listed under Scanners, plus 0755 on public folders and launchers).

Independent review of "AI while you play" (no High findings; nothing an unprivileged user could use):

| Sev. | Finding | Status |
|---|---|---|
| Medium | A mode picked by hand during a game (forge-mode) left Ollama paused after the game | Fixed: when the game ends, the root service is told the mode you picked and undoes the pause |
| Medium | Settings Apply during a game started Ollama again but left the pause in place (the gateway said "paused" while Ollama ran) | Fixed: Apply keeps a paused Ollama stopped until the game ends |
| Medium | Ollama turned off in Settings during a game was started again afterwards | Fixed: started again only while it is still on |
| Low | A slow Ollama stop could time out the request, and then the pause was never undone | Fixed: service changes are queued (no waiting), and the pause is always undone when its files exist |
| Low | Other local providers (added by hand) were reported as paused too | Fixed: only Ollama |

The first-boot test ran again with a step that prints any wizard message about our page: none (the wizard logged an error from KDE's own language page, so page errors do reach that log). Not yet clicked through by hand.

Accepted and open (this round):
- **Ollama on 127.0.0.1 has no password.** Any program on the PC can use it directly, around the gateway's limits and logs. Planned: make the gateway the only way in.
- **The setup page is not tested in the real wizard yet** (only in a QML engine with stand-ins). If it fails, the reserve keeps its default (30%) and can be changed in Forge Settings. The first-boot CI test will look for its errors.
- **The sound fix is not tested on real hardware.**

### Scanners (11-19)

- bandit: production code has no High findings. Medium patterns were checked by hand: SQL built only from fixed column names (not injectable), urlopen only on 127.0.0.1 or the system's OLLAMA_HOST, "0.0.0.0" is a comparison, not a bind. Accepted.
- shellcheck: every shell script clean at warning level.
- detect-secrets (all files): 26 hits, all false positives (public Universal Blue enrollment password, public Ollama checksum, environment variable names, a folder path, fake test values and ids, a key header with no key in it). An earlier run reported 0 because it scanned nothing outside git; that result was wrong and was replaced.
- Unit tests: 1933, all passing (11-24, full merged branch; 18 skipped need tools or root features not available here), with the real-tool tests run (restic 0.16.4, PowerShell 7, QEMU with OVMF for the boot test). Workflows pass actionlint (with its shellcheck checks).
- gitleaks: not available in this environment.

### Could not verify here (needs real hardware)

USB unlock at boot (initramfs drivers, udev, real lsblk output), the stick mount on a real FAT or exFAT stick, interactive password prompts, Ctrl-C at a real terminal, OOM kills under real argon2 memory, real TPM2 or FIDO2 enrollments next to USB keys, real DNS, IPv6, Thunderbolt and SD card readers, sudo and polkit prompts.

## Known open risks (not fixed yet)

- **Nothing has been verified on real hardware yet.** Everything above is tested with real tools or simulations only.
- **The first-boot CI job only runs on its own from main or testing** (GitHub starts workflows by hand only from the default branch); it was run once on the test branch through a temporary copy, and passed.
- **Cloud API keys are stored as plain text** in `/var/lib/forge/secrets.json` (owner-only). Their protection is the disk encryption and file permissions.
- **Viewer token readable by every program you run.** It can read status, runs and agents, but not secrets, the audit log, policy, approval details or the doctor report, and it can't change anything. Changes need `sudo forge-api`.
- **Your single USB stick (H:) holds both the disk unlock key and the cosign signing key.** Losing it exposes both. Better: keep the signing key somewhere else, and add a second unlock stick with `forge-usbkey`.
- **A policy rule can enable command running**, but no command tool exists yet, so it does nothing today.
- **Gateway rate limits reset when Forge Core restarts.**
- **Unverified on real systems:** the iwd Wi-Fi backend honouring random MAC addresses, KDE reading `/etc/xdg/kded6rc`, Claude Desktop surviving the atomic `/opt` layout, the ISO workflow, virtual machine setup from a background service, the welcome page autostart, charger mode switching, the shared drive and backup triggers (udisks, polkit, udev), the Forge Dashboard's password prompts, the hub inside Flatpak and Wine apps, the ISO boot test on GitHub.
- **Test noise:** one agent test thread writes to its database after the test removes it. Harmless, but it should be cleaned up.
