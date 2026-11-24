# Forge OS

A custom Bazzite-based OS for AI agents, creative work and gaming. Runs on desktops, laptops and virtual machines, and dual-boots next to Windows.

## Which image?

| Image | For |
|---|---|
| `forge-os` | Any PC or laptop with an NVIDIA GPU (GTX 16 series and newer, RTX 50 included) |
| `forge-os-universal` | AMD or Intel graphics, laptops without NVIDIA, and virtual machines (QEMU/KVM, VMware, VirtualBox, Hyper-V) |

Both have the same apps and Forge tools. Not sure: on AMD, local AI models run on the CPU unless ROCm support is added later.

## Installing (VM or real PC)

1. **Get the ISO:** the latest `build-iso` run on GitHub, Artifacts, `forge-os-universal-iso` for VMs and AMD or Intel PCs, `forge-os-iso` for NVIDIA PCs. Unzip it to get the `.iso`.
2. **VM settings (VirtualBox):** Type Linux, Version Fedora (64-bit), tick "Skip Unattended Installation", turn on **EFI** (System), at least 8 GB memory, 4 CPUs and a 64 GB disk, Graphics Controller VMSVGA (try VBoxSVGA if the screen is black). Attach the ISO in Storage.
3. **Real PC:** write the ISO to a USB stick (for example with Fedora Media Writer), boot it in UEFI mode.
4. **Install:** choose Install (skip the media check if you like), pick the disk, Erase and install. Check that the plan shows `/boot/efi`: if it shows `biosboot`, the PC or VM started in BIOS mode.
5. **First start:** if a blue "MOK management" screen appears, choose Enroll MOK and type `universalblue`. Then the **setup wizard** creates your account (username and password), asks how much space to keep free for normal use next to AI models, and lets you tick the apps you want (see below).
6. **Sound in a VM:** if notification sounds crackle, Forge already uses a larger audio buffer inside VMs; switching the VM's audio controller (Intel HD Audio or ICH AC97) can also help. Real PCs are not affected.

## Hardware changes

Forge OS adapts when you change parts:

- Agent memory limits are a share of total RAM, so they grow or shrink with your RAM.
- At every boot, `forge-hwcheck` compares the CPU, RAM, graphics, disks, battery and VM status with the last boot. If anything changed, it re-applies all settings, logs it in `/var/lib/forge-hw/hardware-changes.log`, and shows you a notification.
- Model fit checks, stats and VRAM limits always read the hardware live.
- It also warns once if something doesn't fit, for example an NVIDIA card on the universal image.

## Compatibility check

`forge-doctor` gives a read-only report on how well this PC fits Forge OS: image and signature, graphics and VRAM, AI readiness, security (Secure Boot, disk encryption), virtual machines (KVM, IOMMU, GPU passthrough), services, storage and battery. Each part says OK, look at this, or not sure. `forge-doctor --save` writes it to a file you can share, `forge-doctor --json` gives the raw data, and `sudo forge-api GET /api/doctor` (add `?format=text` for a download) gives the same report over the API.

On first login, a **Forge Welcome** page shows this report and helps with location, USB key unlock and Tailscale. Open it again any time from the app menu.

## Apps (always on, and optional)

Every app is a choice. The setup wizard's **Apps** page lists them with a checkbox each, and Forge Settings, **Apps** has the same switches for later. Ticked apps download in the background after setup (nothing is downloaded before you choose; if the PC is offline, or Wi-Fi is joined later, it tries again every few minutes). Turning a switch off **deletes** that app; your own files and the app's saved data in your home folder stay. An app you install yourself (for example from Bazaar, the app store) is never deleted unless you turn its switch from on to off, and an app you delete yourself is not put back (its switch turns off).

Apps that are **part of the system image** (Steam, Lutris, Claude, Kate and the other "built in" ones) can't be deleted, because the system is read-only: unticking one **hides** it from menus and search instead, and ticking it shows it again. System tools (Dolphin, Konsole, System Settings, Bazaar) always stay. Also built in and always on: Ollama, Tailscale, KDE Connect, disk health and temperature sensors.

| Area | Optional apps (ticked at first unless noted) |
|---|---|
| Essentials | Firefox, Flatseal, Mission Center |
| Gaming | Discord, Sober (Roblox), Vinegar (Roblox Studio), Prism Launcher (Minecraft Java), Minecraft Bedrock launcher, Moonlight, GPU Screen Recorder; built in: Steam, Lutris, Input Remapper; **unticked at first:** Retro gaming (RetroDECK), Android apps (Waydroid), Virtual machines |
| Creating | Blender, Blockbench, Krita, Audacity, Pixelorama, Kdenlive, OBS Studio |
| Work and tools | VS Code, GitHub Desktop, VLC, LocalSend, Bitwarden, RustDesk; built in: Claude, Kate, KWrite, Spectacle, Filelight, KFind, KRDC, Krfb, Web Apps |

What hiding also does: a hidden app no longer opens files or links for you (hide both Kate and KWrite and text files have no default editor; hide Steam and steam:// links stop working), the Print Screen key no longer opens a hidden Spectacle, and a hidden app's launcher pinned to the dock or panel may disappear (not sure). Hiding Steam does not stop it from starting by itself if you set it to.

- **Retro gaming (RetroDECK):** emulators for older consoles in one app with a controller-friendly menu. Add your own games (ROMs) and, for some consoles, BIOS files you own.
- **Android apps (Waydroid):** turning it on downloads Android (about 1 GB) and sets it up (on NVIDIA, with Bazzite's `waydroid-nvidia` in its default mode, set up again by itself after kernel or driver updates). Some apps and games won't run. Turning it off deletes the Android system (`/var/lib/waydroid`); each user's Android data in their home folder stays.
- **Virtual machines:** see [Virtual machines](#virtual-machines).

How it works: the list is in `/usr/share/forge/apps.json`; `forge-apps` (root, after boot, every 30 minutes and after each settings change) installs and deletes. Its record of what it installed is in `/var/lib/forge-apps` (root only). `journalctl -u forge-apps` shows what it did.

## Forge Settings (everything is changeable)

Every option below lives in one settings list. Change it whichever way you like:

| Way | How |
|---|---|
| Settings app | Open **Forge Settings** from the app menu. Search, filter by tag, reset any option, save, undo. |
| Terminal | `forge-config list`, `forge-config list gaming`, `forge-config set privacy.dns_provider mullvad`, `forge-config reset`, `forge-config undo`, `forge-config undo 3`, `forge-config history` |
| By hand | Edit `/etc/forge/settings.json` (only holds what you changed), then run `sudo /usr/libexec/forge-apply` |

Before every change, the old settings are saved (the last 10 are kept), and `forge-config history` shows who changed what and when. `forge-config undo 3` goes back three changes. A few options only take effect after a restart or a new login; saving tells you which.

Every option has tags (privacy, gaming, vram, remote and more) so you can find related settings together. A broken settings file never stops the PC from booting: it falls back to defaults and tells you which line is wrong. Every file the system writes from your settings is named `90-forge.conf`, so you can always see what changed.

The desktop look (themes, panels, widgets, shortcuts) is KDE Plasma's own System Settings, which is already one of the most customizable desktops on Linux.

## Forge Core API

A local API for everything in Forge OS. It only listens on this PC (`127.0.0.1:47900`). There are two tokens: `forge-api GET ...` works for admin users with the read-only viewer token (`/etc/forge/viewer.token`: no secrets, audit log, policy, approval details or doctor report). Anything that changes something, or reads gateway keys, the audit log or policy, needs the admin token: run it as `sudo forge-api ...`.

| What | How |
|---|---|
| See every endpoint | Open http://127.0.0.1:47900/docs |
| Live stats (CPU, RAM, disk, GPU, VRAM, power) | `forge-api GET /api/metrics/now` |
| History, up to 90 days | `forge-api GET "/api/metrics/history?metric=vram_used_mb&since=86400&step=900"` |
| Your models, with a "fits on GPU?" check | `forge-api GET /api/models` |
| Download a model (Ollama or Hugging Face GGUF) | `sudo forge-api POST /api/models/pull '{"name": "hf.co/user/repo:Q4_K_M"}'` |
| Free VRAM | `sudo forge-api POST /api/models/qwen3:8b/unload` |
| Change any setting | `sudo forge-api PUT /api/settings '{"values": {"ai.keep_alive_min": 10}}'` |
| Compatibility report | `sudo forge-api GET /api/doctor` |

It runs as its own unprivileged `forge` user. The only root action it can take is applying settings, and those are validated first. Turn it off or change its port in Forge Settings, under Forge Core API.

## AI agents and gateway

**Gateway (one address for every AI model):** apps on this PC, or your other devices through Tailscale, talk to `http://127.0.0.1:47900/v1`, the same format as OpenAI. Model names pick where it runs:
- `qwen3:8b` or `hf.co/...` run locally on your GPU (Ollama)
- `openai/...`, `anthropic/...`, `openrouter/...` go to that cloud service once you add its API key

Make a key for each app or device: `sudo forge-api POST /api/gateway/keys '{"name": "phone", "rate_per_min": 30}'`. The key is shown once. Keys can only use local models unless you create them with `"allow_cloud": true`. Add cloud API keys with `sudo forge-api PUT /api/gateway/secrets/OPENAI_API_KEY '{"value": "..."}'`; they are never shown again. Usage per model or key: `sudo forge-api GET /api/gateway/usage`.

**Agents:** an agent is a model plus a task, a set of tools and permissions. Every tool use is checked first:

| Allowed by default | Asks you first | Blocked by default |
|---|---|---|
| read stats, list models, read settings, desktop notices, read its own workspace files | change settings, download or delete models, read a web page, use paid cloud AI, write workspace files | running commands |

When an agent asks, you get a desktop notification. Approve with `sudo forge-api POST /api/approvals/<id>/approve` (or deny). Every action is in the audit log: `sudo forge-api GET /api/audit`. Change any rule with `sudo forge-api PUT /api/policy`.

Try the built-in example: `sudo forge-api POST /api/agents/system-checkup/run '{"task": "Is anything wrong with my PC?"}'`, then `forge-api GET /api/runs`. Create your own agents with `PUT /api/agents/<id>`. All endpoints are listed at http://127.0.0.1:47900/docs.

## USB key unlock (encrypted installs)

Install Forge OS with disk encryption on. Then one or more USB sticks can unlock it at boot, so you don't have to type the password. Your password always keeps working.

1. Name each key stick's drive `FORGEKEY` (in Windows: right-click the drive, Rename). Every key stick uses this same drive name. FAT32 is the safest format.
2. Plug in one stick and run `sudo forge-usbkey create home` (writes a new random key to the stick and adds it). For a stick that already has a `forge_key` file, use `sudo forge-usbkey add home` instead.
3. Repeat with more sticks and different names: `sudo forge-usbkey create backup`. Each stick gets its own key, so one can be revoked alone.
4. Reboot with any one key stick plugged in.

| Command | What it does |
|---|---|
| `sudo forge-usbkey list` | Show every USB key (works without the sticks) |
| `sudo forge-usbkey test` | Check which key the plugged-in stick is |
| `sudo forge-usbkey revoke backup` | Disable one stick, for example a lost one (asks for your password) |
| `sudo forge-usbkey remove` | Disable every USB key and turn USB unlock off |
| `sudo forge-usbkey audit` | Show every key slot and flag anything unfinished or unknown |
| `sudo forge-usbkey forget NAME` | Drop the record of an interrupted add that may not be a USB key (never removes a key) |

Keep key sticks away from the PC when you're not using them: anyone holding one can unlock the disk. If you edit or re-save a stick's `forge_key` file, revoke it and add it again.

## Wi-Fi, Bluetooth, location and other essentials

Wi-Fi, Bluetooth (including file transfer and phone tethering), mobile data modems, printing, scanners, firmware updates, fingerprint readers and laptop rotation sensors are all included. In Forge Settings, under **Wi-Fi, Bluetooth and location**, you can switch off:

- Bluetooth, printing and mobile data
- **Location services** (on by default, and apps still ask first)
- **Set time zone automatically** (on by default; uses KDE's location-by-IP service, so it follows you when you travel)
- **Precise location from nearby Wi-Fi** (off by default; street-level accuracy through beaconDB)

The clock always syncs over the internet.

## Privacy (defaults)

- **Encrypted DNS** through Quad9, falling back to normal DNS if a network blocks it.
- **No connectivity pings** to outside servers, and random MAC addresses while scanning for Wi-Fi.
- **Firewall:** only your own Tailscale devices are trusted. Remote access goes through Tailscale, not the open network.
- **Local AI:** Ollama only listens on this PC.
- **App permissions:** use Flatseal to see and limit what each app can access.

## Self-monitoring (defaults)

`forge-health` runs every 10 minutes. It checks failed services, disk space, GPU temperature and VRAM, free memory and Ollama. It only notifies you when something is wrong. Log: `~/.local/state/forge/health.log`.

## Modes for every occasion (defaults)

| Command | What it does |
|---|---|
| `forge-mode gaming` | Unloads AI models, limits agents, max performance |
| `forge-mode agents` | Gives agents more CPU and RAM |
| `forge-mode creative` | Balanced for Blender, Studio, OBS |
| `forge-mode quiet` | Low power and low noise |
| `forge-mode status` | Shows the current mode |

What each mode does is set in Forge Settings, under Performance modes.

Mode changes are logged to `~/.local/state/forge/activity.log` (the start of the activity timeline).

On a laptop, Forge can switch modes by itself when you plug the charger in or out: turn on "Laptop: switch mode on charger" in Forge Settings and pick a mode for each. Battery level, charger state and CPU temperature are recorded with the other stats.

## Virtual machines

Off by default (tick it in the setup wizard's Apps page, or turn on "Virtual machines (KVM)" in Forge Settings, under Virtual machines). Forge then does what Bazzite's `ujust setup-virtualization` does: Virtual Machine Manager from Flathub (with its own QEMU and a software TPM, so Windows 11 installs) and two kernel options. Restart when it says so, then open Virtual Machine Manager. Your VMs run as your own user (this image has no system-wide VM service). Turning it off removes only what Forge set up. The same section has a memory cap and CPU priority for all VMs together.

**Windows, tuned for games:** after creating a Windows VM in Virtual Machine Manager (choose Windows 11 or 10 as the OS), shut it down and run `forge-vm windows-tune NAME`. It sets the host's own CPU with a matching core and thread layout, all the Hyper-V enlightenments Windows uses, Windows-friendly clocks and no memory balloon. It saves a backup first (`forge-vm undo NAME` puts it back) and never changes disks, networks, the TPM or the display. `--dry-run` only shows the changes.

Honest limits: without a second graphics card given to the VM (GPU passthrough), Windows in a VM has no real 3D acceleration, so modern games run poorly. Many online games with anti-cheat refuse to run in a VM at all. Most Windows games run better directly on Forge OS through Steam (Proton). GPU passthrough needs two GPUs (for example the processor's built-in graphics plus a graphics card) and a system-wide VM service, which is not in this image. `forge-doctor` shows whether your GPU could be passed through.

Not included: Aseprite (paid, not on Flathub). Install your own copy after setup.

## Hub ideas already in this phase

- **On-demand models:** Ollama unloads a model after 5 idle minutes, so VRAM is free for games.
- **Agent limits:** everything in `agents.slice` gets lower CPU priority and a 48 GB memory cap, so games and the desktop stay smooth.
- **Rollback:** Bazzite keeps the previous system version. Pick it in the boot menu if an update breaks something.

See **Forge Dashboard** below for the home screen.

## Guided fixes (forge-doctor --fix)

`forge-doctor --fix` goes through the problems the report found that have a safe fix, one at a time. For each it shows the problem, what the fix does, the exact command, whether it needs your password, whether it can be undone and its risk. Nothing runs until you answer `y` for that one fix (there is no "yes to all"). Afterwards it checks again and tells you what is fixed. `forge-doctor --fix --dry-run` only shows what would run.

| Problem | Fix |
|---|---|
| Ollama is on in settings but not running | `systemctl restart ollama.service` |
| Forge Core is on in settings but not running | `systemctl restart forge-core.service` |
| USB key unlock is on, but the disk is not encrypted | `forge-config set security.usb_unlock off` |
| No hardware profile yet | `systemctl start forge-hwcheck.service` |
| Virtual machines are on, but libvirtd is not set up | `systemctl --no-block start forge-virt.service` |
| The image does not match the GPU | Only shows the `rpm-ostree rebase` command (medium risk, you run it yourself) |

Root steps ask for your password and go through a small fixed helper, `/usr/libexec/forge-doctor-fix`, that only knows these fixes. Other problems get a "How to fix" line in the report instead (for example enrolling the Secure Boot key). `forge-doctor --json` and `GET /api/doctor` list the fixes too, but the API can't run them.
## Shared drive with Windows

One drive or partition that both Windows and Forge OS use for projects and files. It shows up in Forge OS as **Shared** in your home folder.

**1. Make the partition in Windows** (skip if you already have a drive for this):

1. Right-click Start, **Disk Management**.
2. To use free space on a disk: right-click a partition with spare room, **Shrink Volume**, then right-click the new unallocated space, **New Simple Volume**. Or use a whole second drive. Never use the Windows (C:) drive itself.
3. Format it as **exFAT** or **NTFS** (not FAT32), and set the volume label (the drive name) to `SHARED`. NTFS is safest for big projects; exFAT also works on a Mac.
4. Don't turn on BitLocker for this drive: Forge OS can't open BitLocker drives.

**2. Turn off Fast Startup in Windows:** Control Panel, Power Options, "Choose what the power buttons do", "Change settings that are currently unavailable", untick **Turn on fast startup**. With Fast Startup (or hibernation) Windows keeps the drive in use when it "shuts down", and opening it from Linux then could lose files. Forge OS checks for this and won't open such a drive; it tells you instead.

**3. Turn it on in Forge OS:** Forge Settings, **Storage and shared drive**, turn on **Shared drive with Windows** and save. If you used another drive name, set it there too. Log out and in once. After that the drive opens when you log in or plug it in, without a password.

| Command | What it does |
|---|---|
| `forge-shared-drive status` | Shows whether the drive was found and opened |
| `forge-shared-drive` | Opens it now and fixes the Shared link |

Forge OS never replaces a folder or file you already have called Shared. It refuses FAT32, Linux formats, BitLocker drives and the Windows system drive, and says why. It does not need root: the mount is done by udisks, like opening a drive in Dolphin. Saving the setting adds one permission rule so an administrator can open this one drive name without typing a password.
## Settings on several PCs

Use the same Forge settings on a desktop and a laptop, without any cloud account. It works through any folder both PCs can reach: a shared drive, a USB stick or a synced folder.

**By hand, with a file:**

| Command | What it does |
|---|---|
| `forge-config export my-settings.json` | Saves every setting to a file |
| `forge-config export my-settings.json --portable` | The same, without the settings that belong to this PC only |
| `forge-config import my-settings.json --dry-run` | Shows what would change, changes nothing |
| `forge-config import my-settings.json --merge` | Changes only the settings in the file |
| `forge-config import my-settings.json` | The file's settings replace yours; shareable settings not in the file go back to default |

Importing asks for your password and keeps a snapshot, so `forge-config undo` takes it back. Forge Settings also has **Export settings** and **Import settings** buttons; an imported file shows up as unsaved changes, so you see it before you save.

**Automatically, with a sync folder:** in Forge Settings, under **Settings sync**, pick the folder (inside your home folder, `/run/media` or `/mnt`) and turn on "Share settings with my other PCs". Do this on each PC, then log out and in once.

- Each PC keeps its shareable settings in `forge-settings-<pc name>.json` in that folder, updated when they change.
- When another PC's settings are newer and different, you get one notification. Nothing changes by itself.
- `forge-settings-sync diff laptop` shows what would change. `forge-settings-sync apply laptop` uses them (asks for your password, `forge-config undo` takes it back). `forge-settings-sync status` lists the PCs.
- Give each PC its own name (System Settings, About this System, or `hostnamectl hostname NAME`). Two PCs with the same name overwrite each other's file.

**Never shared** (they belong to one PC): USB key unlock, drive name and wait time, the model folder, models loaded at once, power profiles, laptop charger modes, mobile data, the GPU temperature and free RAM warnings, virtual machine settings and the sync settings themselves.

Files in the sync folder are treated as untrusted: odd names, links, special files, files over 256 KB, nested data, unknown settings and values that are not allowed are refused or ignored, and forge-apply checks everything again before saving.
## Backups

`forge-backup` saves your home folder to a USB stick, USB disk or second drive. Backups are encrypted (restic), need no account and no cloud, and only keep what changed since last time.

**Set up (once):**

1. Name the drive `FORGEBACKUP` (in Windows: right-click the drive, Rename; in Linux: KDE Partition Manager). exFAT or ext4 is best. Use a different drive from your `FORGEKEY` stick. Another name works too: change "Backup drive name" in Forge Settings, under Backups.
2. Plug it in and run `forge-backup init`. Type a backup password twice.
3. **Write the password down and keep it away from this PC.** Without it the backup can't be restored on another PC or after a reinstall. Nobody can open the backup without it.
4. In Forge Settings, under Backups, turn on **Automatic backups**.

Forge checks every hour, and when you plug the drive in, whether the last backup is older than "Back up every (hours)" (24 by default). If the drive isn't there, it waits quietly. You get a notification only when a backup fails, or when no backup has worked for 3 times that interval. Old backups are thinned out (7 daily, 4 weekly, 6 monthly by default), but only after a new backup worked.

| Command | What it does |
|---|---|
| `forge-backup run` | Back up now |
| `forge-backup status` | Last backup, size, how many are kept (`--json` for the raw data) |
| `forge-backup list` | Every backup, with its ID |
| `forge-backup restore latest --target ~/Restored` | Copy the newest backup into a new folder |
| `forge-backup restore ID Documents/thesis.odt --target ~/Restored` | Copy one file or folder from a backup |
| `forge-backup check` | Check the backup on the drive for damage |

**Restoring:** a restore always goes into a new folder that Forge makes (only you can open it), never over your home folder, so nothing you have now gets overwritten. The folder it goes in must be one only you can change, so pick a place in your home folder (like `~/Restored`), not `/tmp` or a shared folder. Copy back what you need from there. On a new PC or after a reinstall, run `forge-backup init` with the drive plugged in and type your written-down password: it connects to the existing backup.

**What is skipped** (when "Skip big files you can download again" is on, the default): Steam games (`steamapps/common` and downloads; Proton game saves in `steamapps/compatdata` are kept), Ollama models (and your model folder if it's inside home), caches (`~/.cache` and Flatpak app caches), the Trash, and virtual machine disks (`.qcow2`, `.vdi`, `.vmdk`, `.vhd`, `.vhdx`). Other drives mounted inside your home folder are skipped too. The saved backup password itself is never put in the backup.

The password is saved in `~/.local/share/forge-backup/password` (only you can read it) so automatic backups work. The last result is in `~/.local/state/forge-backup/status.json`.
## Activity and updates

**Timeline:** `forge-api GET "/api/timeline?limit=50"` lists what happened on this PC, newest first: mode switches, settings changes (which settings and who, never the values), hardware changes, health problems, agent runs, backups and system updates. Add `&since=<unix time>` to get only newer entries. The read-only viewer token may read it; secrets are hidden.

**Who can see the activity:** every administrator (anyone with the viewer token, including the Forge Dashboard) sees all users' entries, with user names and the text of problems, backups and agent results, which can include file paths. Secrets are hidden, but nothing else is.

Forge Core can't read your home folder, so `forge-mode` and `forge-health` share a short summary of your own mode switches, problems and backup results in `/var/lib/forge-activity` (only administrators can write there, and only Forge Core and administrators can read it).

**Updates:** `forge-api GET /api/system/updates` shows the running system version, whether an update is downloaded ("restart to use it"), whether an older version is kept for rollback, whether you are running that older version now, and how the last automatic update went (Bazzite updates with uupd). `forge-health` tells you once when an update is ready, when one failed, and when the PC started an older version. Turn that off in Forge Settings, under Self-monitoring.

**If an update breaks something:** restart and pick the previous entry in the boot menu. To keep that older version, run `rpm-ostree rollback` and restart. Your files and settings are not touched.
## Creative MCP hub

Your Creative MCP hub (one Claude Desktop connection to Blender, Roblox Studio, Aseprite and Blockbench) was written for Windows. `forge-hub` connects it on Forge OS without changing the hub. Clone the hub anywhere in your home folder, then:

| Command | What it does |
|---|---|
| `forge-hub setup ~/creative-mcp-hub` | Builds the hub (its own `npm ci` and `npm run build`), writes its Linux config to `~/.local/share/creative-mcp-hub`, adds it to Claude Desktop (backup first), and shows how to install each app's plugin |
| `forge-hub status` (or `--json`) | Built or not, git commit, config, Claude Desktop entry, and, while the hub runs, each app server's state |
| `forge-hub dashboard` | Opens the hub's dashboard in your browser, through a private page in your runtime folder that is deleted after a minute (the dashboard's key never goes on a command line, where other users could see it) |
| `forge-hub remove` | Takes the hub out of Claude Desktop and undoes the Flatpak permissions Forge added. Your hub folder and its data stay |

- **Approvals happen in the dashboard.** The hub's approval pop-ups are Windows-only. On Forge OS, run `forge-hub dashboard` and keep the page open while Claude works. Without it, anything that needs your OK is refused.
- **Setup runs the clone's own code.** `npm ci` and `npm run build` run the build scripts in your hub clone, as you. Only set up a clone you trust.
- **Flatpak permissions:** Blender needs to know where the pairing token is, and Blockbench needs read-only access to the token folder. Setup shows the exact `flatpak override --user` commands and runs them only if you say yes.
- **Roblox Studio** (through Vinegar): copy the plugin into Studio's plugin folder inside Vinegar's Wine prefix (setup shows the path) and paste the token once.
- **Aseprite** is paid and not preinstalled. Until you install it, the hub runs and Aseprite just shows as not connected.
- Fully quit and reopen Claude Desktop after setup or remove. Not sure yet: the plugins working inside Flatpak and Wine is untested on a real PC.

## Forge Dashboard

The home screen of Forge OS. Open **Forge Dashboard** from the app menu, or with the button on the Forge Settings and Forge Welcome pages (`forge-settings --dashboard` in a terminal). It only works on this PC, for you, like Forge Settings, and closes itself a couple of minutes after you close the tab.

| Page | What it shows |
|---|---|
| Home | Live CPU, GPU, VRAM, RAM and temperature (paused while the tab is hidden), the mode, problems, waiting approvals, the last backup and the update state |
| Agents and approvals | Agents, recent runs, and waiting approvals to approve or deny |
| Activity | The timeline, filtered by kind |
| Updates and recovery | The running, downloaded and previous system versions, and how to roll back |
| Backups, Storage, Sync, Virtual machines, Creative hub | The status of each, from its own tool |
| Diagnostics | The forge-doctor report, its fixes, and a download of the report |

Reading needs no password. Every button that does something shows what it will run and asks you to confirm first: back up now, open the shared drive, use another PC's settings, run one doctor fix, open the hub's dashboard, approve or deny an agent request, and roll back. The last four ask for your password. Rolling back also asks you to type `rollback`, is only offered when no update is waiting for a restart, and needs a restart to take effect. Seeing the details of waiting approvals asks for your password too, because they can hold private data.

## Appearance: background, dock and bootup

Forge Settings has a sidebar with six parts: **Appearance**, **System**, **Network and devices**, **Privacy and security**, **AI** and **Data**. The search box at the top finds any setting by its name, help text or tag (type `#gaming` for one tag exactly). Press `/` to jump to it. A few settings only experts need are folded away under "Show advanced settings" in their card; search always finds them, and one you changed always shows.

**Appearance** is yours alone: it is saved in your home folder and needs no password, except the boot splash and login screen (Bootup), which change the whole PC and ask for the admin password when applied. Each part (Background, Dock, Bootup) has a live preview. Its options arrive with later Forge OS updates; until a part is installed, its card says so.

**Media library:** pictures and videos you add are copied (never moved) into `~/.local/share/forge/appearance/media/`, which only you can open. Up to 4 GB per file and 20 GB together. Forge checks each file's contents, not its name, and refuses anything that is not a known picture or video format. SVG pictures are kept but never previewed, because they can carry scripts.

| Command | What it does |
|---|---|
| `forge-appearance show` | Every appearance setting and its value (`--json` for the raw data) |
| `forge-appearance set dock size 48` | Change one value |
| `forge-appearance apply` | Put the values into effect (or `apply dock` for one part); a part that is not installed yet says so |
| `forge-appearance media add ~/Pictures/beach.jpg` | Copy a picture or video into the library |
| `forge-appearance media list` | What is in the library (`--json` for the raw data) |
| `forge-appearance media remove ID` | Remove one item; settings that used it go back to their default |

Your values are in `~/.config/forge/appearance.json` (only you can read it). A broken or edited file never stops anything: wrong values fall back to their default.

## Dock styles (Appearance, Dock)

Forge Settings, Appearance, Dock has three styles. All of it is per user and needs no password.

| Style | What it is |
|---|---|
| **Default** | Your normal Plasma panel. Choosing it again puts the panel (and Krema's settings) back exactly as they were before Forge first changed the dock. |
| **Floating clean** | The same Plasma panel, floating, centered, only as long as its icons, see-through. Position, alignment, width, thickness, visibility and background can be changed. Corner shape, color and blur come from your Plasma theme. |
| **Animated** | [Krema](https://github.com/isac322/krema), a separate dock whose icons grow under the mouse. Zoom, zoom style and speed, icon size, spacing, corners, gap, background, opacity, tint, shadow, visibility, delays, badges, previews and screens can be changed. You choose what happens to the Plasma panel meanwhile (slim bar on the other edge, auto hide, or leave it). |

- Before the first change away from Default, Forge backs up your panel layout to `~/.local/share/forge/appearance/dock-backup/first/` (only you can open it). That backup is never replaced. Its `README.txt` explains a manual restore.
- Krema starts from `~/.config/autostart/forge-krema.desktop` for you only, and its settings are in `~/.config/kremarc`. Other Krema settings you change in Krema itself are kept, also when you go to Default and back: pinned apps, and per-screen choices (those win on their screen). If `kremarc` is a link (a dotfile manager), Forge writes to the file it points to and keeps the link.
- Floating clean and Animated only change the Plasma panel that holds your app list. Your other panels are never touched; Default puts every panel back.
- Not possible: zoom inside the Plasma panel, a bounce when an app starts, and turning running-app dots on or off (Krema has no setting for them).
- **Krema 0.13.0 is in the image**, pinned to its exact version and its COPR signing key (`files/scripts/install-krema.sh`). If the COPR is ever unreachable or the package does not fit, the build leaves Krema out and says why; it never fails because of Krema.

## Backgrounds: pictures, videos and Wallpaper Engine

Forge Settings, Appearance, Background picks what fills your desktop, per user and without a password: a **picture**, a **slideshow** (every picture in your media library), a **video**, a **Wallpaper Engine** wallpaper, a **solid color** or a **gradient**. Only the options that fit the chosen type show. "Save and apply" changes the wallpaper right away through Plasma's own desktop scripting; `forge-appearance apply background` does the same.

- **Videos** use the Smart Video Wallpaper Reborn plugin (built into the image, version pinned). They loop, pause while a window is maximized or fullscreen (a fullscreen game counts), can pause on battery, and are muted unless you raise the volume. Optional: a second video for the night, switched at set hours or with the light or dark Plasma style.
- **Wallpaper Engine** needs Wallpaper Engine bought in Steam; Forge lists the wallpapers you subscribed to (from `~/.local/share/Steam` and your other Steam library folders). Video and web wallpapers are offered by default; scene wallpapers only after you turn on "Allow scene wallpapers (experimental)", because they can crash the desktop. The plugin is in the image, pinned to its exact version and COPR signing key (`files/scripts/install-wallpaper-engine.sh`). If COPR cannot be reached the build leaves it out and the page says "Wallpaper Engine support is not included in this build"; a wrong pin or signature stops the build instead.
- **Crash guard:** after a video or Wallpaper Engine background is applied, Forge watches the desktop for a minute. If plasmashell crashes again and again, it switches back to the last picture that worked (or Plasma's default wallpaper) once, and tells you the next time you apply.
- **One screen:** under advanced settings, use a background on one screen only (by its connector name, like DP-1).
- Not offered, because no plugin can do it: dimming the background, and a separate "pause while a game runs" (games pause it as fullscreen windows). The lock screen and login screen are set in the Bootup part.

## Bootup: boot splash, login screen, desktop splash and lock screen

In Forge Settings, Appearance, **Bootup**. Pick any picture or video from your media library.

| Part | Who it affects | Password | When you see it |
|---|---|---|---|
| Boot splash (start, shutdown and the disk password box) | Whole PC | Admin password | After the next restart |
| Login screen background (Plasma Login Manager only, not SDDM) | Whole PC | Admin password | Next time the login screen shows |
| Desktop splash after login | Only you | None | Next login |
| Lock screen (its own picture or video, or the same as your desktop) | Only you | None | Next lock |

The password is asked only when the boot splash or login screen actually changed. Videos on the login and lock screen need the Smart Video Wallpaper plugin.

**Boot splash limits:** at most 90 frames (60 for a loop), at most 480 x 480 pixels, at most 8 MB together. The boot splash is drawn by Plymouth before the disk is unlocked, so every frame sits in memory, and the frames ride along in every boot image in `/boot`. Your file is converted as you (never as root), with a time limit and no network (in a sandbox when `bwrap` works). While a custom boot splash is on, system updates take a little longer, because the boot image gets the theme each time.

What the boot splash changes, and nothing else: the theme in `/etc/forge-boot/plymouth/forge-boot/`, two lines in `/etc/plymouth/plymouthd.conf` (the original is kept), and `rpm-ostree initramfs-etc --track` of exactly those two paths. It never removes other tracked files (like `/etc/crypttab` for USB key unlock), never rebuilds the boot image with dracut, and never restarts the PC. If the theme ever fails to load, Plymouth falls back to Bazzite's own splash, and the disk password prompt still works.

**Undo:** choose "Bazzite's own" or "Default" and Save and apply. By hand, if Settings won't open:

1. `sudo /usr/libexec/forge-boot-apply off` (turns the boot splash and login screen back to default).
2. If that fails: `sudo rpm-ostree initramfs-etc --untrack=/etc/plymouth/plymouthd.conf --untrack=/etc/forge-boot/plymouth/forge-boot` first, and only then remove `/etc/forge-boot` and the `Theme=forge-boot`, `ThemeDir=` and Forge comment lines from `/etc/plymouth/plymouthd.conf` (lines starting with `#forge-boot-disabled# ` were yours: remove that prefix). Never delete a tracked path before untracking it. Check `rpm-ostree initramfs-etc` still lists `/etc/crypttab` if you use USB key unlock.
3. Login screen: `sudo rm /etc/plasmalogin.conf.d/90-forge-login.conf` and `sudo rm -r /var/lib/forge-boot`.
4. Your own parts: choose "Plasma's own" in Settings. By hand: remove the `Theme=` line under `[KSplash]` in `~/.config/ksplashrc`, and the `[Greeter]` lines in `~/.config/kscreenlockerrc`.
5. Missed it at boot? Old deployments keep their own boot image: pick the previous entry in the boot menu.

## Updates: version number, channels and automatic rollback

**Version number:** every image has one, like `45.20261120.57`: the Fedora version, the build date and the build number, plus the commit it was built from. It is at the bottom of Forge Settings, in Forge Dashboard (Updates and recovery, "Forge OS build"), in `/usr/share/forge/version.json` and in the image's own label `org.forgeos.version` (Bazzite's version label, which `rpm-ostree status` shows, stays as it is). What changed between versions: `CHANGELOG.md`.

**Update channels:**

| Channel | Image tag | Built from |
|---|---|---|
| Stable (default) | `latest` | the `main` branch only (every push, and every day) |
| Testing | `testing` | the `testing` branch only (every push to it, or by hand) |

To switch: Forge Settings, System, **Updates and recovery**, pick the channel, **Save and apply**, say yes to the question and type your password. Forge downloads that version right away (its signature is checked) and the PC starts it after the next restart. Later updates come from the new channel. In a terminal: `sudo /usr/libexec/forge-apply --switch-channel testing --confirm` (without `--confirm` it only shows what it would do).

To go back: choose Stable the same way (or `--switch-channel stable --confirm`) and restart. Before you restart, `rpm-ostree cleanup -p` cancels the switch. Starting an older entry from the boot menu does not change the channel: updates come from the default entry's channel. Changing the setting any other way (`forge-config`, the API, undo, settings sync) never switches by itself: forge-apply only reminds you. Every switch uses the signed image (`ostree-image-signed:docker://...`), also when the PC was installed unsigned.

For the owner: to publish a testing image, push to a `testing` branch (`git switch -c testing`, then push). BlueBuild itself only pushes `candidate` tags. After each build, the workflow checks that image's signature and version label by its digest, and only then points `latest` (runs of `main` only) or `testing` (runs of `testing` only) at that exact digest. A failed check leaves `latest` and `testing` where they were.

**Automatic rollback (greenboot, part of Bazzite):** only on the first start(s) of a new system version, Forge checks two things (`/usr/libexec/forge-boot-health`):

1. The login screen (`display-manager.service`) did not fail or keep crashing within 4 minutes (skipped when the PC does not start to the desktop on purpose).
2. If the `nvidia` driver was loaded on the last good start, it loads again within 2 minutes (skipped for a card given to a virtual machine, a driver blocked on purpose on the kernel command line or in modprobe.d, and a driver that never loaded on this PC, for example with Secure Boot and the key not enrolled).

If one fails, the PC restarts and tries again; after 3 failed starts of the new version it goes back to the previous one, once, and stays there. Turning it off also clears greenboot's countdown from the boot menu settings. It never counts: optional Forge services (Ollama, Forge Core and the rest), no internet (greenboot's DNS check is switched off), a slow disk unlock or a missing USB key (the disk is unlocked before the checks run), or any normal day without a new version. An error inside the check passes. Turn it off in Forge Settings: "Go back automatically if an update fails to start". What happened: `journalctl -b -u greenboot-healthcheck`.

**SBOM:** every build makes a list of everything in each image (SPDX), downloadable as `forge-os-sbom` and `forge-os-universal-sbom` from the run, and attached to the image as an attestation signed with the workflow's own GitHub identity (no key). Check it: `cosign verify-attestation --type spdxjson --certificate-identity-regexp '^https://github.com/MONKEY7770/forge-os/' --certificate-oidc-issuer https://token.actions.githubusercontent.com ghcr.io/monkey7770/forge-os:latest`.

**Screenshot test:** the `screenshot-test` workflow installs `forge-os-universal` to a virtual disk after each successful build (or by hand), starts it, logs a test account in by itself and saves screenshots of the boot splash, the desktop and Forge Settings (artifact `screenshots`). It never blocks a release.

Not sure until it runs for real: the testing tag job, the SBOM attestation (a large SBOM might be too big for the public transparency log; the artifact is kept either way), the screenshot test (first run untested), and greenboot on a real update.

## Theme, presets and Thai (Appearance)

**Theme** (Forge Settings, Appearance, Theme): light or dark colors or any installed color scheme, the accent color (from the wallpaper or your own), icons, mouse pointer and its size, window title bars, Plasma style, and the text and fixed width fonts. "Leave as it is" and "None chosen" change nothing. Only KDE's own tools and keys are used (`plasma-apply-colorscheme`, `plasma-apply-cursortheme`, `plasma-apply-desktoptheme`, `/usr/libexec/plasma-changeicons`, `kwriteconfig6` on `kdeglobals`, `kcminputrc` and `kwinrc`, then KDE's reload signals). The lists come from what is installed on the PC. Untested on Plasma so far.

**Presets** (Appearance, Presets): save your whole look (background, dock, bootup, theme, with the pictures and videos they use) under a name, apply it in one click, rename, delete, export it as one `.forgelook` file and import one. An imported file is checked strictly (size caps, only known parts, pictures and videos checked like an upload) and only stored: nothing runs until you click Apply. The boot splash and login screen change the whole PC, so a preset changes them only when you tick "Also change the boot splash and login screen" (it then asks for the admin password). A screen connector, theme or Wallpaper Engine item that this PC doesn't have is left as it is. With Settings sync on, "Sync now" copies presets through `<sync folder>/forge-looks-<you>/`. Also from a terminal: `forge-appearance preset list|save|apply|rename|delete|export|import|sync`.

**Thai** (ภาษาไทย): Forge Settings, the welcome page and the Forge Dashboard follow the system language, or the Language switch at the bottom of the Settings sidebar (Auto, English, ไทย; per user, no password). Messages that come from other tools (forge-doctor, the update and backup tools, the dock, background and bootup programs) stay in English. The image adds `google-noto-sans-thai-vf-fonts` for Thai text.

## Gaming, backup health and logs

**Game overlay (MangoHud):** Forge Settings, Gaming, "Game overlay": off, minimal (an FPS counter) or full (FPS, frame times, CPU and GPU load and temperature, VRAM and RAM). Forge writes it per user to `~/.config/MangoHud/MangoHud.conf` (MangoHud's own file). It only shows in games you start with MangoHud: in Steam, a game's Properties, Launch options: `mangohud %command%`. It is never turned on for other apps. **Right Shift+F12** shows or hides it in a game. A `MangoHud.conf` you wrote yourself is never changed (Forge only replaces a file that starts with its own marker line). Not covered: Roblox through Sober (a Flatpak has its own config folder and needs the MangoHud Flatpak extension).

**Gaming mode while a game runs** (on by default; Forge Settings, Gaming): when a Steam game or Roblox (Sober) starts, Forge switches to gaming mode (what `forge-mode gaming` does: AI models unloaded, agents slowed down, performance power profile) and back to your mode and power profile about 15 seconds after the game ends. Bazzite removes Feral GameMode, so Forge finds games itself: every 5 seconds it looks at your own processes for Steam's `reaper SteamLaunch AppId=` process (it runs for the whole game) or `sober`. Roblox Studio is not a game here, so agents keep helping while you build. If you pick another mode during a game, Forge keeps it. If anything crashes or you log out, Forge switches back when it stops, at its next start, or at the next login. No password prompt: the one root step (the agents' limits) goes to a small root service, `forge-mode-switch` (socket `/run/forge-mode.sock`, only root and administrators can open it). It accepts one mode word, and only from an administrator who is the user of the active local desktop session, at most 10 times a minute. If that step fails, Forge keeps saying gaming mode (the limits are still the gaming ones) and tries again every 15 seconds and at login; Forge Dashboard shows it on Home.

| Command | What it does |
|---|---|
| `forge-game status` | Overlay, mode, and the games found right now (`--json` for the raw data) |
| `forge-game restore --force` | Switch back now from a gaming mode a game started |

Video and Wallpaper Engine backgrounds already pause by themselves while a window is fullscreen or maximized (Appearance, Background, "Pause the video", default), so a game gets the GPU.

**Backup restore test:** about once a month, while the backup drive is plugged in, `forge-backup restore-test` restores up to 12 random files (up to 64 MB) from the newest backup into a new private temporary folder (your runtime folder, never your home folder), checks their size, compares their contents with your originals that have not changed since the backup, runs `restic check --read-data-subset=256M`, deletes the folder and records the result in `~/.local/state/forge-backup/restore-test.json`. The password goes to restic only as a file, as for every backup. A missing or unplugged drive is not a failure: it waits. You get a notification when a test fails, and once a day when no test has worked for 40 days. Forge Dashboard shows it under Backups (with **Test now**) and on Home when it needs a look. It never runs during a backup or while you are in gaming mode.

**Logs in Forge Dashboard:** the Logs page shows recent errors and warnings (or everything, info level) from the system journal and your own user journal, filtered by Forge service, level, time (1 hour, 24 hours, 7 days) and search words. It only reads; passwords, keys and tokens are hidden in every line before it reaches the page (the same redaction as the audit log, plus a second pass for `name: password=...` lines). At most 1000 lines per journal are read, for at most 15 seconds, and 500 are shown. **Who can read what:** on Fedora your own user journal is always yours to read; the whole system journal needs the `wheel` (administrators), `adm` or `systemd-journal` group. Without one of them the page says so and shows only your own lines. To let another account read it: `sudo usermod -aG systemd-journal NAME`, then log out and in.

## Step 1: Put this folder on GitHub (free)

1. Install **GitHub Desktop** on Windows and sign in.
2. File, Add local repository, pick this folder. When it says it isn't a repo yet, click **create a repository**.
3. Click **Publish repository**, name it `forge-os`, and **untick "Keep this code private"** (free image hosting needs a public repo; only the recipe is public, no personal data).

## Step 2: Add your signing key

This proves updates really come from you.

1. Download `cosign-windows-amd64.exe` from https://github.com/sigstore/cosign/releases and rename it to `cosign.exe`.
2. In this folder, run `cosign.exe generate-key-pair` and press Enter twice (no password).
3. It makes `cosign.key` and `cosign.pub`. The new `cosign.pub` replaces the old one, so commit and push it.
4. On GitHub: repo Settings, Secrets and variables, Actions, **New repository secret**. Name: `SIGNING_SECRET`. Value: paste everything inside `cosign.key`.
5. **Never upload `cosign.key` itself.** It's already blocked by `.gitignore`. Keep a copy somewhere safe.

## Step 3: Build

Every push builds both images (`forge-os` and `forge-os-universal`). On GitHub, open the **Actions** tab, pick **bluebuild**, click **Run workflow**. It takes about 20 to 40 minutes. After that it rebuilds every day on its own, so you always get the latest Bazzite fixes. Every build first runs the tests and security scanners and checks the Bazzite base image's signature; if any of that fails, no image is published. After publishing, the signature of the new images is checked again.

## Step 4: Prepare Windows (do this first)

**Run the checker first.** It is read-only and changes nothing. Copy `tools/windows/forge-precheck.ps1` to the PC, open PowerShell in that folder and run `powershell -ExecutionPolicy Bypass -File .\forge-precheck.ps1` (this allows it for this one run only; add `-SaveReport` to keep a copy on your Desktop). It tells you which steps below still need doing. Details: `tools/windows/README.md`.

1. **Back up** anything important.
2. **BitLocker:** if it's on, save your recovery key somewhere off this PC.
3. **Turn off Fast Startup:** Control Panel, Power Options, "Choose what the power buttons do", uncheck Fast Startup.
4. **Make space:** best option is a separate SSD for Linux. Otherwise shrink a partition in Disk Management (at least 200 GB free, more for games and models).

## Step 5: Install

### Option A: Forge OS installer ISO (simplest)

1. On GitHub: **Actions** tab, **build-iso**. A new ISO is built every Sunday on its own (each is kept for 14 days), so there is usually one ready. To make one now, click **Run workflow**. It takes about 30 to 60 minutes.
2. Open the finished run and download `forge-os-iso` (NVIDIA) or `forge-os-universal-iso` from the bottom of the page. Unzip it to get the `.iso`.
   To build an ISO from a test branch instead: in **Run workflow**, type the branch's image tag in **Image tag**, `br-<branch>-candidate-45` (for a branch named `vm-test`: `br-vm-test-candidate-45`; one commit: `<first 7 characters of the commit>-candidate-45`). Its signature is checked and the ISO holds exactly that build. Tags longer than 19 characters don't fit the ISO's name, so such an install follows `latest` for later updates; to keep following the branch, run `rpm-ostree rebase ostree-image-signed:docker://ghcr.io/monkey7770/forge-os:br-<branch>-candidate-45` after installing.
   Each ISO is also boot tested in a virtual UEFI PC, with Secure Boot off and then on (Microsoft keys). First the unmodified ISO must get past the firmware (its boot loader must start). Then a test copy, where only the boot menu files (grub.cfg) change (they add a serial console and a new random test mark to the kernel line), must show the kernel printing its command line with that mark, and after that the installer. Text from the boot menu can't pass this. The run fails if either check fails. To see what happened, download `forge-os-boot-test` or `forge-os-universal-boot-test` from the same page (`screen.png`, `serial.log` and `result.txt` for each boot).
3. Flash it to a USB stick (16 GB or more) with Rufus or Fedora Media Writer.
4. Boot from the USB (F8, F11 or F12 at startup). Pick the **empty disk or free space only**, never the Windows partitions. Turn on disk encryption if you want USB key unlock.
5. If a blue "MOK management" screen appears on the first reboot, choose Enroll MOK and type the password `universalblue`. This lets Secure Boot load the drivers.
6. After the first login, switch updates to the signed image once (replace `YOUR-NAME`, and use `forge-os-universal` if that's what you installed):

```
rpm-ostree rebase ostree-image-signed:docker://ghcr.io/YOUR-NAME/forge-os:latest
systemctl reboot
```

For a virtual machine, attach the `forge-os-universal` ISO to a new VM (at least 4 CPU cores, 8 GB RAM, 80 GB disk, UEFI firmware) and install the same way.

### Option B: Install Bazzite, then switch to Forge OS

1. Download the official Bazzite ISO from https://bazzite.gg (pick: Desktop, NVIDIA RTX series, KDE Plasma).
2. Flash it to a USB stick (8 GB or more) with Rufus or Fedora Media Writer.
3. Boot from the USB (F8, F11 or F12 at startup, depends on your motherboard).
4. In the installer, pick the **empty disk or free space only**. Do not select the Windows partitions.
5. After it boots into Bazzite, open a terminal and switch to your image (replace `YOUR-NAME`):

```
rpm-ostree rebase ostree-unverified-registry:ghcr.io/YOUR-NAME/forge-os:latest
systemctl reboot
```

6. After the reboot, switch to the signed version so future updates are verified:

```
rpm-ostree rebase ostree-image-signed:docker://ghcr.io/YOUR-NAME/forge-os:latest
systemctl reboot
```

You'll get a boot menu to pick Forge OS or Windows on each startup.

## Step 6: First boot checklist

- [ ] Flatpak apps finish installing (a notification shows when done)
- [ ] `nvidia-smi` shows the RTX 5070
- [ ] `systemctl status ollama` is running, then `ollama run qwen3` works
- [ ] `sudo tailscale up` and sign in
- [ ] Sunshine: Bazzite Portal, enable Sunshine, pair Moonlight on your phone
- [ ] Steam opens and a game launches
- [ ] Sober logs into Roblox
- [ ] Vinegar installs and opens Roblox Studio
- [ ] Claude Desktop opens and signs in
- [ ] Forge Settings opens, and saving a change asks for your password
- [ ] `forge-api GET /api/metrics/now` shows GPU temperature and VRAM
- [ ] Wi-Fi connects, Bluetooth pairs a device, and the time zone is right
- [ ] The system-checkup agent runs and finishes (`forge-api GET /api/runs`)
- [ ] `cat /var/lib/forge-hw/hardware.json` shows your CPU, RAM, GPU and disks
- [ ] `sudo forge-usbkey create home`, then reboot with the stick: the disk unlocks without typing
- [ ] `sudo forge-usbkey list` shows the key, and `revoke` works
- [ ] The Forge Welcome page opened on first login, and `forge-doctor` shows nothing unexpected
- [ ] Optional: turn on Virtual machines, restart, and Virtual Machine Manager can start a VM
- [ ] `forge-doctor --fix --dry-run` lists no fixes, or only ones you expect
- [ ] Optional: make a `SHARED` drive in Windows, turn on Shared drive, log out and in: `~/Shared` opens it
- [ ] Optional: set a sync folder on two PCs, change a setting on one, and the other shows "Settings from ... are available"; `forge-settings-sync apply` asks for your password
- [ ] Plug in a drive named FORGEBACKUP, run `forge-backup init` and `forge-backup run`, then `forge-backup status` shows a backup
- [ ] Turn on Automatic backups, unplug and replug the drive: a backup starts by itself once it is due (`journalctl --user -u forge-backup`)
- [ ] `forge-backup restore latest --target ~/Restored` brings back your files
- [ ] `forge-mode quiet`, then `forge-api GET /api/timeline` shows the switch, and `forge-api GET /api/system/updates` shows your version
- [ ] Optional: `forge-hub setup` with your hub clone, then Claude Desktop lists creative-mcp-hub and `forge-hub status` shows the hub running
- [ ] Forge Dashboard opens from the app menu, Home shows live CPU, GPU and VRAM lines, and Back up now asks before it starts
- [ ] Forge Settings shows the sidebar; Appearance can add a picture to the media library without a password, and `forge-appearance show` lists Background, Dock and Bootup
- [ ] Appearance, Dock: Floating clean turns the panel into a centered floating bar, and Default puts it back as it was
- [ ] Optional (Krema pinned): Animated starts Krema with zoom, it starts again after logging out and in, and Default stops it
- [ ] Appearance, Background: pick a picture and "Save and apply": the desktop changes without a password; then a video: it plays, pauses when you maximize a window, and `systemctl --user status forge-background-guard` shows the guard ran
- [ ] Optional (only if Wallpaper Engine was pinned in the build): a subscribed Wallpaper Engine video or web wallpaper shows in the list and applies
- [ ] Bootup: turn on a boot splash with a short video, Save and apply asks for the admin password once, `rpm-ostree initramfs-etc` still lists `/etc/crypttab`, and after a restart the video shows and the disk password box works (type the password, and try Caps Lock)
- [ ] Bootup: choose "Bazzite's own" again, restart: Bazzite's splash is back and `/etc/forge-boot` is gone
- [ ] Bootup: login screen picture shows on the login screen; desktop splash and lock screen pictures show without a password
- [ ] Forge Settings shows "Forge OS 45.YYYYMMDD.N (stable, ...)" at the bottom, and Forge Dashboard, Updates and recovery, shows the same build
- [ ] `systemctl is-enabled greenboot-healthcheck.service` says enabled; after the next update, `journalctl -b -u greenboot-healthcheck` shows `forge-boot-health: login screen: ok` (and `NVIDIA driver: ok` on the NVIDIA image)
- [ ] Optional: switch to Testing in Forge Settings, confirm, restart: `rpm-ostree status` shows `forge-os:testing`; switch back to Stable the same way
- [ ] Appearance, Theme: Dark, then Light, then an accent color: Plasma changes at once without a password; a pointer size and a font change too
- [ ] Appearance, Presets: save the current look, change something, apply the preset: it comes back; export it, import the file on another PC
- [ ] Language switch: ไทย shows Forge Settings and the Dashboard in Thai with readable Thai letters (`fc-list :lang=th` lists a font)
- [ ] Gaming: set Game overlay to minimal, start a Steam game with launch options `mangohud %command%`: an FPS counter shows, and Right Shift+F12 hides it; Firefox and Discord show no overlay
- [ ] Gaming: start a Steam game: `forge-game status` says gaming mode, `ollama ps` is empty, and no password prompt appears; quit the game: after about 15 seconds the mode and power profile are back
- [ ] Gaming: start Roblox in Sober: gaming mode turns on; open Roblox Studio: it does not
- [ ] With the backup drive plugged in, Forge Dashboard, Backups, Test now: the restore test shows OK, and `ls /run/user/$UID` shows no forge-restore-test folder left
- [ ] Forge Dashboard, Logs: lines from forge-core and your own Forge services show; a line with a password shows [hidden]

## Known risks (not sure until the first build)

See also SECURITY.md for the security process, every finding so far and the open risks.


- **Random Wi-Fi address:** Bazzite uses the iwd Wi-Fi backend, which may ignore this NetworkManager setting (iwd has its own scan randomization). Not sure until tested.
- **ISO workflow:** first run untested. The Fedora version input must match the image (45 at the time of writing).
- **VM guest tools:** if one of the five guest packages is missing for this Fedora version, the build fails on it and that line can be removed.
- **Claude Desktop:** if the build fails on a `/opt` error, the package needs BlueBuild's `optfix` option. Fallback: the AppImage from the same project.
- **Ollama models** live in `/var/lib/ollama/models` by default. Change the folder in Forge Settings.
- **Virtual machines and the welcome page** are new and untested on a real PC. Not sure yet that the Flathub download and kernel options work from a background service on the first try.
- **Shared drive, backups, settings sync, the hub connection and the Forge Dashboard** are new and tested only with simulated drives and tools (backups also with a real restic). Password prompts, drive mounting and the Flatpak apps need a real PC first.
- **ISO boot test:** it waits for the installer's start messages on the serial console. Not sure Fedora 45's installer prints exactly those; if the first run fails there, `serial.log` in the run's files shows what it printed.
- **Sober and Vinegar** are unofficial and can break after Roblox updates. Windows stays as your fallback.
