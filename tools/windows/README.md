# Forge OS pre-install check (Windows)

`forge-precheck.ps1` checks this PC before you install Forge OS. The installer USB can't do this, so run it on Windows first.

It is read-only. It never writes to the registry, never changes settings or services, installs nothing and uses no network. The only file it can write is the report on your Desktop, and only when you ask for it.

## Run it

1. In File Explorer, open the folder with `forge-precheck.ps1`, click the address bar, type `powershell` and press Enter.
2. Run:

```
powershell -ExecutionPolicy Bypass -File .\forge-precheck.ps1
```

`-ExecutionPolicy Bypass` allows this one run only. It does not change your settings.

| Option | What it does |
|---|---|
| `-SaveReport` | Also saves the report as a new text file on your Desktop |
| `-Json` | Prints machine-readable JSON instead of the text report |

Works with Windows PowerShell 5.1 (built in) and PowerShell 7 (`pwsh` instead of `powershell`).

## Admin

It works without admin. Run it as admin (right-click Windows PowerShell, Run as administrator, then go to the folder) for a sure answer on:

- **BitLocker:** uses `Get-BitLockerVolume`. Without admin it tries `manage-bde -status`, which usually needs admin too, so you get "not sure".
- **Secure Boot:** uses `Confirm-SecureBootUEFI`. Without admin it reads the registry instead.

Not sure: on some PCs the disk checks may also need admin. If they say "not sure", run it as admin.

## What it checks

Each check says **OK**, **Look at this** or **Not sure**, with a "What to do" line.

| Check | OK when |
|---|---|
| BitLocker | No drive is encrypted. If one is, save the recovery key off this PC. |
| Fast Startup | Off |
| Hibernation | Off (if on: always shut down fully before switching) |
| Free space | At least 200 GB unallocated (80 GB minimum). Otherwise it suggests a drive to shrink, or a separate SSD. |
| Windows disk | Just says whether Windows is on the same disk as the free space |
| UEFI and GPT | UEFI mode, GPT disks |
| Secure Boot | On or off are both fine. Legacy BIOS is not. |
| Graphics | NVIDIA GTX 16 series or newer (RTX 50 included): `forge-os`. AMD, Intel or a VM: `forge-os-universal`. Very new cards: not sure. |
| RAM | 16 GB or more |
| CPU | 64-bit Intel or AMD |
| Virtualization | On in the firmware (only needed for virtual machines) |
| SHARED drive | Optional. exFAT or NTFS. |
| FORGEBACKUP drive | Optional. Best on a separate disk. |
| FORGEKEY stick | Optional. A USB stick, FAT32 or exFAT. |
| Windows version | Just for your information |

## Privacy

The report holds hardware facts and check results only. No PC name, user name, serial numbers or files. Drive labels are not shown, except SHARED, FORGEBACKUP and FORGEKEY.

## Status

- Check logic: Tested (simulated), with fake data in PowerShell 7 on Linux (`tests/pwsh/forge-precheck.tests.ps1`).
- Real Windows probes (registry, disks, BitLocker, Secure Boot, graphics): Untested.
- Windows PowerShell 5.1: Untested on a real PC. The tests check that the script uses no PowerShell 7 only syntax.
- `manage-bde` output is read in English only. On other Windows languages BitLocker says "not sure" without admin.
