# Changelog

Every Forge OS image has a version number like `45.20261120.57`: the Fedora version, the build date (UTC) and the build number, plus the git commit it was built from. Images are built every day, so most versions only bring Bazzite and Fedora updates. This file lists the changes made in this repository.

Channels: **stable** (image tag `latest`, built from `main`) and **testing** (image tag `testing`, built from the `testing` branch). See "Updates: version number, channels and automatic rollback" in the README.

## Unreleased

### New: release basics

- Version number in every image (`/usr/share/forge/version.json` and the image label `org.forgeos.version`), shown in Forge Settings and Forge Dashboard.
- Update channels: stable and testing. Switching is done in Forge Settings (System, Updates and recovery) after a confirmation, always to the signed image, and starts after the next restart.
- Automatic rollback with greenboot: on the first start of a new version, a failed login screen or a missing NVIDIA driver makes the PC go back to the previous version after 3 failed starts (the NVIDIA check only when the driver worked on the last good start). Can be turned off.
- An SBOM (SPDX) for each image, kept with each build and attached to the image as a signed attestation.
- Screenshot test of the built image in a virtual machine (separate workflow, never blocks a release).

### What Forge OS already has (first summary)

- **Images:** `forge-os` (NVIDIA, on `bazzite-nvidia-open`) and `forge-os-universal` (AMD, Intel and virtual machines, on `bazzite`), Fedora 45, KDE Plasma, built with BlueBuild and signed with cosign. Every build runs the tests and security scanners first and checks the Bazzite base image's signature. Installer ISOs every week, boot tested in a virtual UEFI PC with Secure Boot off and on.
- **Apps:** Steam, Firefox, Discord, Claude Desktop, VS Code, GitHub Desktop, OBS, Kdenlive, VLC, Blender, Blockbench, Krita, Pixelorama, Audacity, Sober, Vinegar, Minecraft Bedrock launcher, Moonlight, RustDesk, LocalSend, Tailscale, Bitwarden, Flatseal, Mission Center, Ollama.
- **Forge Settings:** every option in one list with search and tags, also through `forge-config`, the settings file and the Forge Core API. Snapshots, history and undo. Appearance (background, dock styles, bootup) per user.
- **Forge Core API:** local API with metrics history, model library with a GPU fit check, an OpenAI-compatible AI gateway with keys and limits, agents with allow, ask and deny rules, approvals and an audit log.
- **Forge Dashboard:** home screen with live stats, approvals, activity timeline, updates and rollback, backups, storage, sync, virtual machines, the creative hub and diagnostics.
- **System tools:** `forge-doctor` (compatibility report and guided fixes), `forge-health` (self-monitoring), `forge-mode` (gaming, agents, creative, quiet), `forge-usbkey` (unlock the encrypted disk with a USB stick), `forge-backup` (encrypted restic backups), `forge-settings-sync` (settings on several PCs), `forge-shared-drive` (drive shared with Windows), `forge-hub` (Creative MCP hub on Linux), virtual machines on or off, hardware change detection.
- **Privacy defaults:** encrypted DNS, no connectivity pings, random Wi-Fi address while scanning, firewall that only trusts your Tailscale devices.
