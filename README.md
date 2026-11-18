# Forge OS (Phase 1)

A custom Bazzite image for this PC (RTX 5070, 64 GB RAM), built to dual-boot next to Windows.

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

On GitHub, open the **Actions** tab, pick **bluebuild**, click **Run workflow**. It takes about 20 to 40 minutes. After that it rebuilds every day on its own, so you always get the latest Bazzite fixes.

## Step 4: Prepare Windows (do this first)

1. **Back up** anything important.
2. **BitLocker:** if it's on, save your recovery key somewhere off this PC.
3. **Turn off Fast Startup:** Control Panel, Power Options, "Choose what the power buttons do", uncheck Fast Startup.
4. **Make space:** best option is a separate SSD for Linux. Otherwise shrink a partition in Disk Management (at least 200 GB free, more for games and models).

## Step 5: Install Bazzite, then switch to Forge OS

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

You'll get a boot menu to pick Forge OS or Windows on each startup. Optional: if you later want your own installer USB instead of steps 1 to 4, see https://blue-build.org/how-to/generate-iso/

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

## Known risks (not sure until the first build)

- **Claude Desktop:** if the build fails on a `/opt` error, the package needs BlueBuild's `optfix` option. Fallback: the AppImage from the same project.
- **Ollama models** live in `/var/lib/ollama/models` by default. Change the folder in Forge Settings.
- **Sober and Vinegar** are unofficial and can break after Roblox updates. Windows stays as your fallback.
