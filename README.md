# Forge OS

A custom Bazzite-based OS for AI agents, creative work and gaming. Runs on desktops, laptops and virtual machines, and dual-boots next to Windows.

## Which image?

| Image | For |
|---|---|
| `forge-os` | Any PC or laptop with an NVIDIA GPU (GTX 16 series and newer, RTX 50 included) |
| `forge-os-universal` | AMD or Intel graphics, laptops without NVIDIA, and virtual machines (QEMU/KVM, VMware, VirtualBox, Hyper-V) |

Both have the same apps and Forge tools. Not sure: on AMD, local AI models run on the CPU unless ROCm support is added later.

## Hardware changes

Forge OS adapts when you change parts:

- Agent memory limits are a share of total RAM, so they grow or shrink with your RAM.
- At every boot, `forge-hwcheck` compares the CPU, RAM, graphics, disks, battery and VM status with the last boot. If anything changed, it re-applies all settings, logs it in `/var/lib/forge-hw/hardware-changes.log`, and shows you a notification.
- Model fit checks, stats and VRAM limits always read the hardware live.

## What's preinstalled

| Area | Apps |
|---|---|
| Essentials | Firefox, Discord, Claude Desktop, VS Code, GitHub Desktop, VLC |
| Gaming | Steam (from Bazzite), Sober (Roblox Player), Vinegar (Roblox Studio), Minecraft Bedrock launcher |
| Creative | Blender, Blockbench, Krita, Audacity, OBS, Kdenlive |
| AI | Ollama (CUDA), agent resource limits |
| Remote access | Tailscale, Moonlight, RustDesk, LocalSend, KDE Connect (built in), Sunshine (one click in Bazzite Portal) |
| Privacy | Bitwarden, Flatseal |
| System | Mission Center, disk health (smartd), temperature sensors |

## Forge Settings (everything is changeable)

Every option below lives in one settings list. Change it whichever way you like:

| Way | How |
|---|---|
| Settings app | Open **Forge Settings** from the app menu. Search, filter by tag, reset any option, save, undo. |
| Terminal | `forge-config list`, `forge-config list gaming`, `forge-config set privacy.dns_provider mullvad`, `forge-config reset`, `forge-config undo` |
| By hand | Edit `/etc/forge/settings.json` (only holds what you changed), then run `sudo /usr/libexec/forge-apply` |

Every option has tags (privacy, gaming, vram, remote and more) so you can find related settings together. A broken settings file never stops the PC from booting: it falls back to defaults and tells you which line is wrong. Every file the system writes from your settings is named `90-forge.conf`, so you can always see what changed.

The desktop look (themes, panels, widgets, shortcuts) is KDE Plasma's own System Settings, which is already one of the most customizable desktops on Linux.

## Forge Core API

A local API for everything in Forge OS. It only listens on this PC (`127.0.0.1:47900`) and needs the token in `/etc/forge/core.token` (admin users can read it).

| What | How |
|---|---|
| See every endpoint | Open http://127.0.0.1:47900/docs |
| Live stats (CPU, RAM, disk, GPU, VRAM, power) | `forge-api GET /api/metrics/now` |
| History, up to 90 days | `forge-api GET "/api/metrics/history?metric=vram_used_mb&since=86400&step=900"` |
| Your models, with a "fits on GPU?" check | `forge-api GET /api/models` |
| Download a model (Ollama or Hugging Face GGUF) | `forge-api POST /api/models/pull '{"name": "hf.co/user/repo:Q4_K_M"}'` |
| Free VRAM | `forge-api POST /api/models/qwen3:8b/unload` |
| Change any setting | `forge-api PUT /api/settings '{"values": {"ai.keep_alive_min": 10}}'` |

It runs as its own unprivileged `forge` user. The only root action it can take is applying settings, and those are validated first. Turn it off or change its port in Forge Settings, under Forge Core API.

## AI agents and gateway

**Gateway (one address for every AI model):** apps on this PC, or your other devices through Tailscale, talk to `http://127.0.0.1:47900/v1`, the same format as OpenAI. Model names pick where it runs:
- `qwen3:8b` or `hf.co/...` run locally on your GPU (Ollama)
- `openai/...`, `anthropic/...`, `openrouter/...` go to that cloud service once you add its API key

Make a key for each app or device: `forge-api POST /api/gateway/keys '{"name": "phone", "rate_per_min": 30}'`. The key is shown once. Keys can only use local models unless you create them with `"allow_cloud": true`. Add cloud API keys with `forge-api PUT /api/gateway/secrets/OPENAI_API_KEY '{"value": "..."}'`; they are never shown again. Usage per model or key: `forge-api GET /api/gateway/usage`.

**Agents:** an agent is a model plus a task, a set of tools and permissions. Every tool use is checked first:

| Allowed by default | Asks you first | Blocked by default |
|---|---|---|
| read stats, list models, read settings, desktop notices, read its own workspace files | change settings, download or delete models, read a web page, use paid cloud AI, write workspace files | running commands |

When an agent asks, you get a desktop notification. Approve with `forge-api POST /api/approvals/<id>/approve` (or deny). Every action is in the audit log: `forge-api GET /api/audit`. Change any rule with `forge-api PUT /api/policy`.

Try the built-in example: `forge-api POST /api/agents/system-checkup/run '{"task": "Is anything wrong with my PC?"}'`, then `forge-api GET /api/runs`. Create your own agents with `PUT /api/agents/<id>`. All endpoints are listed at http://127.0.0.1:47900/docs.

## USB key unlock (encrypted installs)

Install Forge OS with disk encryption on. Then a USB stick can unlock it at boot, so you don't have to type the password:

1. Name the USB drive `FORGEKEY` (in Windows: right-click the drive, Rename). FAT32 is the safest format.
2. Put a file called `forge_key` in its top folder, holding a long secret text (64 random characters is best).
3. On Forge OS, plug it in and run `sudo forge-usbkey setup`. Enter your disk password once.
4. Reboot with the USB plugged in. If the USB is missing, boot asks for your password as usual.

Other commands: `sudo forge-usbkey test` (check it works), `status`, `remove`. The drive name and wait time are in Forge Settings, under Security.

Keep the USB away from the PC when you're not using it: anyone holding it can unlock the disk. If you edit or re-save `forge_key`, run setup again.

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

Not included: Aseprite (paid, not on Flathub). Install your own copy after setup.

## Hub ideas already in this phase

- **On-demand models:** Ollama unloads a model after 5 idle minutes, so VRAM is free for games.
- **Agent limits:** everything in `agents.slice` gets lower CPU priority and a 48 GB memory cap, so games and the desktop stay smooth.
- **Rollback:** Bazzite keeps the previous system version. Pick it in the boot menu if an update breaks something.

The permission system, activity timeline and dashboard home screen come in later phases.

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

Every push builds both images (`forge-os` and `forge-os-universal`). On GitHub, open the **Actions** tab, pick **bluebuild**, click **Run workflow**. It takes about 20 to 40 minutes. After that it rebuilds every day on its own, so you always get the latest Bazzite fixes.

## Step 4: Prepare Windows (do this first)

1. **Back up** anything important.
2. **BitLocker:** if it's on, save your recovery key somewhere off this PC.
3. **Turn off Fast Startup:** Control Panel, Power Options, "Choose what the power buttons do", uncheck Fast Startup.
4. **Make space:** best option is a separate SSD for Linux. Otherwise shrink a partition in Disk Management (at least 200 GB free, more for games and models).

## Step 5: Install

### Option A: Forge OS installer ISO (simplest)

1. On GitHub: **Actions** tab, **build-iso**, **Run workflow**. It takes about 30 to 60 minutes.
2. Open the finished run and download `forge-os-iso` (NVIDIA) or `forge-os-universal-iso` from the bottom of the page. Unzip it to get the `.iso`.
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
- [ ] `sudo forge-usbkey setup`, then reboot with the USB: the disk unlocks without typing

## Known risks (not sure until the first build)

- **Random Wi-Fi address:** Bazzite uses the iwd Wi-Fi backend, which may ignore this NetworkManager setting (iwd has its own scan randomization). Not sure until tested.
- **ISO workflow:** first run untested. The Fedora version input must match the image (45 at the time of writing).
- **VM guest tools:** if one of the five guest packages is missing for this Fedora version, the build fails on it and that line can be removed.
- **Claude Desktop:** if the build fails on a `/opt` error, the package needs BlueBuild's `optfix` option. Fallback: the AppImage from the same project.
- **Ollama models** live in `/var/lib/ollama/models` by default. Change the folder in Forge Settings.
- **Sober and Vinegar** are unofficial and can break after Roblox updates. Windows stays as your fallback.
