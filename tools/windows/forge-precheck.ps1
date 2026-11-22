#Requires -Version 5.1
<#
.SYNOPSIS
Forge OS pre-install check for Windows. Read-only: it changes nothing on this PC.

.DESCRIPTION
Run this on Windows BEFORE installing Forge OS. It looks at BitLocker, Fast Startup,
hibernation, free disk space, UEFI and GPT, Secure Boot, graphics, RAM, CPU, virtualization,
the SHARED, FORGEBACKUP and FORGEKEY drives, and the Windows version. Each check says OK,
Look at this, or Not sure, with a plain "what to do" line.

It never writes to the registry, never changes services or settings, installs nothing and
uses no network. The only file it can write is the report on your Desktop (with -SaveReport).

Works without admin. Run it as admin for a sure answer on BitLocker and Secure Boot.

.PARAMETER Json
Print machine-readable JSON instead of the text report.

.PARAMETER SaveReport
Also save the text report to your Desktop (a new file, never overwrites).

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\forge-precheck.ps1
#>
[CmdletBinding()]
param(
    [switch]$Json,
    [switch]$SaveReport
)

$script:ForgeVersion = '1.0'
$script:GiB = 1073741824.0
$script:MinGB = 80
$script:RecGB = 200
$script:Inv = [System.Globalization.CultureInfo]::InvariantCulture

# ============================================================================ small helpers

function Build-ForgeResult {
    param([string]$Id, [string]$Name, [string]$Status, [string[]]$Detail, [string]$Todo, [bool]$NeedsAdmin = $false)
    $d = @()
    if ($null -ne $Detail) { $d = @($Detail | Where-Object { $null -ne $_ } | ForEach-Object { ConvertTo-ForgeCleanText $_ }) }
    [pscustomobject]@{ Id = $Id; Name = (ConvertTo-ForgeCleanText $Name); Status = $Status; Detail = [string[]]$d
        Todo = (ConvertTo-ForgeCleanText $Todo); NeedsAdmin = $NeedsAdmin }
}

# Names from drivers, firmware and Windows are untrusted text. Drop control and format characters
# (terminal escape codes, bell, bidi overrides, zero-width and line separators) before anything
# is matched, shown, saved or put in JSON. Strings are UTF-16, so a character outside the basic
# plane is two code units (a surrogate pair): those are judged as one real character (tag
# characters and other format or unassigned characters are dropped), and a surrogate without its
# partner is dropped too.
function ConvertTo-ForgeCleanText($Value) {
    if ($null -eq $Value) { return $null }
    $s = [regex]::Replace([string]$Value, '[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]', '')
    if (-not [regex]::IsMatch($s, '[\uD800-\uDFFF]')) { return $s }
    $out = [System.Text.StringBuilder]::new($s.Length)
    $i = 0
    while ($i -lt $s.Length) {
        if ([char]::IsSurrogatePair($s, $i)) {
            $cat = [System.Globalization.CharUnicodeInfo]::GetUnicodeCategory($s, $i)
            if ($cat -ne [System.Globalization.UnicodeCategory]::Format -and
                $cat -ne [System.Globalization.UnicodeCategory]::Control -and
                $cat -ne [System.Globalization.UnicodeCategory]::OtherNotAssigned) {
                [void]$out.Append($s, $i, 2)
            }
            $i += 2
            continue
        }
        if (-not [char]::IsSurrogate($s[$i])) { [void]$out.Append($s[$i]) }
        $i += 1
    }
    return $out.ToString()
}

# Every string in a probe result, including nested tables and lists.
function ConvertTo-ForgeCleanData($Value) {
    if ($null -eq $Value) { return $null }
    if ($Value -is [string]) { return (ConvertTo-ForgeCleanText $Value) }
    if ($Value -is [System.Collections.IDictionary]) {
        $out = @{}
        foreach ($k in @($Value.Keys)) { $out[$k] = ConvertTo-ForgeCleanData $Value[$k] }
        return $out
    }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $obj = [ordered]@{}
        foreach ($p in @($Value.PSObject.Properties)) { $obj[$p.Name] = ConvertTo-ForgeCleanData $p.Value }
        return [pscustomobject]$obj
    }
    if ($Value -is [System.Collections.IEnumerable]) { return , @(foreach ($i in $Value) { ConvertTo-ForgeCleanData $i }) }
    return $Value
}

function Get-ForgeLabel([string]$Status) {
    if ($Status -eq 'ok') { return 'OK' }
    if ($Status -eq 'look') { return 'Look at this' }
    return 'Not sure'
}

function Format-ForgeSize($Bytes) {
    if ($null -eq $Bytes) { return '? GB' }
    return [string]::Format($script:Inv, '{0:0} GB', ([double]$Bytes / $script:GiB))
}

function Test-ForgeSameText([string]$A, [string]$B) {
    return [string]::Equals($A, $B, [System.StringComparison]::OrdinalIgnoreCase)
}

function ConvertTo-ForgeBusType($Value) {
    if ($null -eq $Value) { return 'unknown' }
    $map = @{ 1 = 'SCSI'; 2 = 'ATAPI'; 3 = 'ATA'; 4 = '1394'; 6 = 'Fibre Channel'; 7 = 'USB'; 8 = 'RAID'; 9 = 'iSCSI';
        10 = 'SAS'; 11 = 'SATA'; 12 = 'SD'; 13 = 'MMC'; 14 = 'Virtual'; 15 = 'File Backed Virtual'; 16 = 'Storage Spaces'; 17 = 'NVMe' }
    $s = [string]$Value
    $n = 0
    if ([int]::TryParse($s, [ref]$n)) { if ($map.ContainsKey($n)) { return $map[$n] } return 'unknown' }
    return $s
}

function ConvertTo-ForgePartitionStyle($Value) {
    $s = [string]$Value
    if ($s -eq '1' -or $s -eq 'MBR') { return 'MBR' }
    if ($s -eq '2' -or $s -eq 'GPT') { return 'GPT' }
    if ($s -eq '0' -or $s -eq 'RAW' -or $s -eq 'Unknown') { return 'RAW' }
    return 'unknown'
}

function Get-ForgeRegValue([string]$Path, [string]$Name) {
    $item = Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction Stop
    return $item.$Name
}

# Runs a probe or a check. A probe that throws gives @{ Error = ... } so its checks say "not sure".
function Invoke-ForgeSafe([scriptblock]$Block) {
    try { return (& $Block) }
    catch { return @{ Error = 'probe failed' } }
}

# ============================================================================ probes (the only parts that read Windows)
# Each probe returns plain data. Tests replace these functions with fake data.

function Get-ForgeProbeAdmin {
    try {
        $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $p = [System.Security.Principal.WindowsPrincipal]::new($id)
        return [bool]$p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
    }
    catch { return $false }
}

function ConvertFrom-ManageBdeStatus {
    param([string]$Text)
    # English output only. Other languages give "unknown" values, never a guess.
    # Volume labels (the text in [brackets]) are personal and are not kept.
    $result = @()
    $cur = $null
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match '^\s*Volume\s+([A-Za-z]:)') {
            if ($null -ne $cur) { $result += $cur }
            $cur = @{ MountPoint = $Matches[1].ToUpper(); Encrypted = $null; ProtectionOn = $null }
            continue
        }
        if ($line -match '^\s*Volume\s+\\\\\?\\Volume\{') {
            if ($null -ne $cur) { $result += $cur }
            $cur = @{ MountPoint = '(no drive letter)'; Encrypted = $null; ProtectionOn = $null }
            continue
        }
        if ($null -eq $cur) { continue }
        if ($line -match '^\s*Conversion Status:\s*(.+)$') {
            $v = $Matches[1].Trim()
            if ($v -match '^Fully Decrypted') { $cur.Encrypted = $false }
            elseif ($v -match 'Encrypt|Decryption in Progress|Paused') { $cur.Encrypted = $true }
        }
        elseif ($line -match '^\s*Protection Status:\s*(.+)$') {
            $v = $Matches[1]
            if ($v -match 'Protection On') { $cur.ProtectionOn = $true }
            elseif ($v -match 'Protection Off') { $cur.ProtectionOn = $false }
        }
    }
    if ($null -ne $cur) { $result += $cur }
    return $result
}

function Get-ForgeProbeBitLocker {
    param([bool]$IsAdmin)
    if ($IsAdmin -and (Get-Command -Name Get-BitLockerVolume -ErrorAction SilentlyContinue)) {
        try {
            $vols = @(Get-BitLockerVolume -ErrorAction Stop | ForEach-Object {
                    @{ MountPoint = [string]$_.MountPoint
                        Encrypted = ([string]$_.VolumeStatus -ne 'FullyDecrypted')
                        ProtectionOn = ([string]$_.ProtectionStatus -eq 'On') }
                })
            return @{ Source = 'Get-BitLockerVolume'; Error = $null; Volumes = $vols }
        }
        catch { }
    }
    if ([string]::IsNullOrEmpty($env:SystemRoot)) { return @{ Source = 'none'; Error = 'not Windows'; Volumes = @() } }
    $exe = Join-Path $env:SystemRoot 'System32\manage-bde.exe'
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { return @{ Source = 'none'; Error = 'manage-bde not found'; Volumes = @() } }
    try {
        $text = (& $exe -status 2>&1 | Out-String)
        $code = $LASTEXITCODE
        $vols = @(ConvertFrom-ManageBdeStatus -Text $text)
        if ($code -ne 0 -or $vols.Count -eq 0) { return @{ Source = 'manage-bde'; Error = 'manage-bde could not read the drives'; Volumes = @() } }
        return @{ Source = 'manage-bde'; Error = $null; Volumes = $vols }
    }
    catch { return @{ Source = 'manage-bde'; Error = 'manage-bde failed'; Volumes = @() } }
}

function Get-ForgeProbeFastStartup {
    try {
        $v = Get-ForgeRegValue 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' 'HiberbootEnabled'
        return @{ Error = $null; HiberbootEnabled = [int]$v }
    }
    catch { return @{ Error = 'not readable'; HiberbootEnabled = $null } }
}

function Get-ForgeProbeHibernation {
    foreach ($name in 'HibernateEnabled', 'HibernateEnabledDefault') {
        try {
            $v = Get-ForgeRegValue 'HKLM:\SYSTEM\CurrentControlSet\Control\Power' $name
            if ($null -ne $v) { return @{ Error = $null; HibernateEnabled = [int]$v } }
        }
        catch { }
    }
    return @{ Error = 'not readable'; HibernateEnabled = $null }
}

function Get-ForgeProbeDisks {
    $sys = $null
    if (-not [string]::IsNullOrEmpty($env:SystemDrive)) { $sys = $env:SystemDrive.Substring(0, 1).ToUpper() }
    try {
        $disks = @(Get-Disk -ErrorAction Stop)
        $parts = @(Get-Partition -ErrorAction Stop)
        $vols = @(Get-Volume -ErrorAction Stop)
    }
    catch { return @{ Error = 'not readable'; SystemDrive = $sys; Disks = @(); Volumes = @() } }

    $outDisks = @(foreach ($d in $disks) {
            @{ Number = [int]$d.Number; Model = ([string]$d.FriendlyName).Trim(); BusType = (ConvertTo-ForgeBusType $d.BusType)
                SizeBytes = [double]$d.Size; PartitionStyle = (ConvertTo-ForgePartitionStyle $d.PartitionStyle)
                FreeExtentBytes = [double]$d.LargestFreeExtent }
        })
    $outVols = @(foreach ($p in $parts) {
            $letter = [string]$p.DriveLetter
            if ($letter -notmatch '^[A-Za-z]$') { $letter = '' }
            $vol = $null
            foreach ($v in $vols) {
                $vpath = [string]$v.Path
                if ($vpath -and (@($p.AccessPaths) -contains $vpath)) { $vol = $v; break }
                if ($letter -and (Test-ForgeSameText ([string]$v.DriveLetter) $letter)) { $vol = $v; break }
            }
            if ($null -eq $vol) { continue }
            $dt = [string]$vol.DriveType
            if ($dt -eq '2') { $dt = 'Removable' } elseif ($dt -eq '3') { $dt = 'Fixed' }
            @{ DiskNumber = [int]$p.DiskNumber; DriveLetter = $letter.ToUpper(); Label = [string]$vol.FileSystemLabel
                FileSystem = [string]$vol.FileSystem; SizeBytes = [double]$p.Size; FreeBytes = [double]$vol.SizeRemaining; DriveType = $dt }
        })
    return @{ Error = $null; SystemDrive = $sys; Disks = $outDisks; Volumes = $outVols }
}

function Get-ForgeProbeFirmware {
    try {
        $v = [int](Get-ForgeRegValue 'HKLM:\SYSTEM\CurrentControlSet\Control' 'PEFirmwareType')
        if ($v -eq 2) { return @{ Error = $null; FirmwareType = 'UEFI' } }
        if ($v -eq 1) { return @{ Error = $null; FirmwareType = 'BIOS' } }
    }
    catch { }
    $t = [string]$env:firmware_type
    if ($t -eq 'UEFI') { return @{ Error = $null; FirmwareType = 'UEFI' } }
    if ($t -eq 'Legacy') { return @{ Error = $null; FirmwareType = 'BIOS' } }
    return @{ Error = 'not readable'; FirmwareType = $null }
}

function Get-ForgeProbeSecureBoot {
    param([bool]$IsAdmin)
    if ($IsAdmin -and (Get-Command -Name Confirm-SecureBootUEFI -ErrorAction SilentlyContinue)) {
        try {
            $on = Confirm-SecureBootUEFI -ErrorAction Stop
            return @{ Error = $null; Enabled = [bool]$on; Supported = $true; Source = 'Confirm-SecureBootUEFI' }
        }
        catch {
            if ($_.Exception -is [System.PlatformNotSupportedException] -or $_.Exception.Message -match 'not supported') {
                return @{ Error = $null; Enabled = $null; Supported = $false; Source = 'Confirm-SecureBootUEFI' }
            }
        }
    }
    try {
        $v = Get-ForgeRegValue 'HKLM:\SYSTEM\CurrentControlSet\Control\SecureBoot\State' 'UEFISecureBootEnabled'
        return @{ Error = $null; Enabled = ([int]$v -eq 1); Supported = $true; Source = 'registry' }
    }
    catch { return @{ Error = 'not readable'; Enabled = $null; Supported = $null; Source = 'none' } }
}

function Get-ForgeProbeGpu {
    try {
        $list = @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop | ForEach-Object {
                $pnp = [string]$_.PNPDeviceID
                $ven = ''
                if ($pnp -match 'VEN_([0-9A-Fa-f]{4})') { $ven = $Matches[1].ToUpper() }
                $cls = ''
                if ($pnp -match '^([A-Za-z]+)\\') { $cls = $Matches[1].ToUpper() }
                @{ Name = ([string]$_.Name).Trim(); VendorId = $ven; PnpClass = $cls }
            })
        return @{ Error = $null; Adapters = $list }
    }
    catch { return @{ Error = 'not readable'; Adapters = @() } }
}

function Get-ForgeProbeSystem {
    try {
        $cs = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
        $cpus = @(Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop)
    }
    catch { return @{ Error = 'not readable' } }
    $ram = $null
    try {
        $ram = (@(Get-CimInstance -ClassName Win32_PhysicalMemory -ErrorAction Stop) | Measure-Object -Property Capacity -Sum).Sum
    }
    catch { }
    if (-not $ram) { $ram = $cs.TotalPhysicalMemory }
    $c = $cpus[0]
    return @{
        Error = $null
        RamBytes = [double]$ram
        CpuName = ([string]$c.Name).Trim()
        Cores = [int](($cpus | Measure-Object -Property NumberOfCores -Sum).Sum)
        Threads = [int](($cpus | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum)
        Architecture = $c.Architecture
        VirtFirmware = $c.VirtualizationFirmwareEnabled
        VmExtensions = $c.VMMonitorModeExtensions
        HypervisorPresent = $cs.HypervisorPresent
    }
}

function Get-ForgeProbeWindows {
    try { $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop }
    catch { return @{ Error = 'not readable' } }
    $disp = $null; $ubr = $null
    try { $disp = Get-ForgeRegValue 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' 'DisplayVersion' } catch { }
    try { $ubr = Get-ForgeRegValue 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' 'UBR' } catch { }
    return @{ Error = $null; Caption = ([string]$os.Caption).Trim(); Version = [string]$os.Version; Build = [string]$os.BuildNumber
        DisplayVersion = $disp; Ubr = $ubr; Arch = [string]$os.OSArchitecture }
}

function Get-ForgeProbeDesktop {
    return [Environment]::GetFolderPath('Desktop')
}

# ============================================================================ checks (pure: data in, result out)

function Get-ForgeFreeDisks($Disk) {
    # Usable unallocated space per disk. An empty, uninitialized disk counts as all free.
    $out = @()
    foreach ($d in @($Disk.Disks)) {
        $hasVol = @($Disk.Volumes | Where-Object { $_.DiskNumber -eq $d.Number }).Count -gt 0
        $free = [double]$d.FreeExtentBytes
        if ($d.PartitionStyle -eq 'RAW' -and -not $hasVol) { $free = [double]$d.SizeBytes }
        if ($free -lt $script:GiB) { $free = 0 }
        $out += @{ Number = $d.Number; Free = $free; Disk = $d }
    }
    return $out
}

function Get-ForgeBestFree($Disk) {
    $best = $null
    foreach ($f in @(Get-ForgeFreeDisks $Disk)) {
        if ($f.Free -gt 0 -and ($null -eq $best -or $f.Free -gt $best.Free)) { $best = $f }
    }
    return $best
}

function Get-ForgeSystemDiskNumber($Disk) {
    if (-not $Disk.SystemDrive) { return $null }
    foreach ($v in @($Disk.Volumes)) {
        if (Test-ForgeSameText $v.DriveLetter $Disk.SystemDrive) { return $v.DiskNumber }
    }
    return $null
}

function Test-ForgeDiskReadable($Disk) {
    return ($null -ne $Disk -and -not $Disk.Error -and @($Disk.Disks).Count -gt 0)
}

function Test-ForgeBitLocker($Data, [bool]$IsAdmin) {
    $name = 'BitLocker (drive encryption)'
    $vols = @()
    if ($null -ne $Data -and -not $Data.Error) { $vols = @($Data.Volumes) }
    if ($vols.Count -eq 0) {
        return Build-ForgeResult 'bitlocker' $name 'unsure' @('Could not read the BitLocker state (this usually needs admin).') `
            'Run this checker again as admin. If BitLocker or Device encryption is on, save your recovery key somewhere off this PC before installing.' $true
    }
    $enc = @($vols | Where-Object { $_.Encrypted -eq $true -or $_.ProtectionOn -eq $true })
    $unknown = @($vols | Where-Object { $null -eq $_.Encrypted -and $null -eq $_.ProtectionOn })
    $how = 'Read with ' + $Data.Source + '.'
    if ($enc.Count -gt 0) {
        $list = ($enc | ForEach-Object { $_.MountPoint }) -join ', '
        return Build-ForgeResult 'bitlocker' $name 'look' @("Encrypted with BitLocker: $list", $how) `
            'Save the BitLocker recovery key for each encrypted drive somewhere off this PC (print it, photograph it, or check it is in your Microsoft account at aka.ms/myrecoverykey). Changing boot settings for Forge OS can make Windows ask for it.' $true
    }
    if ($unknown.Count -gt 0) {
        return Build-ForgeResult 'bitlocker' $name 'unsure' @('Could not tell for some drives (non-English Windows or no admin).', $how) `
            'Check by hand: Control Panel, BitLocker Drive Encryption. If any drive is on, save its recovery key somewhere off this PC.' $true
    }
    return Build-ForgeResult 'bitlocker' $name 'ok' @('No drive is encrypted with BitLocker.', $how) 'Nothing to do.' $true
}

function Test-ForgeFastStartup($Data) {
    $name = 'Fast Startup'
    $v = $null
    if ($null -ne $Data -and -not $Data.Error) { $v = $Data.HiberbootEnabled }
    if ($v -eq 1) {
        return Build-ForgeResult 'fast_startup' $name 'look' @('Fast Startup is on.') `
            'Turn it off: Control Panel, Power Options, Choose what the power buttons do, Change settings that are currently unavailable, untick Turn on fast startup, Save changes. With it on, Windows leaves its drives half closed and Forge OS cannot safely write to them.'
    }
    if ($v -eq 0) { return Build-ForgeResult 'fast_startup' $name 'ok' @('Fast Startup is off.') 'Nothing to do.' }
    return Build-ForgeResult 'fast_startup' $name 'unsure' @('Could not read the Fast Startup setting.') `
        'Check by hand: Control Panel, Power Options, Choose what the power buttons do. Turn on fast startup must be unticked.'
}

function Test-ForgeHibernation($Data) {
    $name = 'Hibernation'
    $v = $null
    if ($null -ne $Data -and -not $Data.Error) { $v = $Data.HibernateEnabled }
    if ($v -eq 1) {
        return Build-ForgeResult 'hibernation' $name 'look' @('Hibernation is on.') `
            'Before switching to Forge OS, always use Shut down or Restart, never Hibernate. To turn hibernation off completely, run powercfg /h off in an admin terminal (this also turns off Fast Startup).'
    }
    if ($v -eq 0) { return Build-ForgeResult 'hibernation' $name 'ok' @('Hibernation is off.') 'Nothing to do.' }
    return Build-ForgeResult 'hibernation' $name 'unsure' @('Could not read the hibernation setting.') `
        'Before switching to Forge OS, always use Shut down or Restart, never Hibernate.'
}

function Get-ForgeDiskLines($Disk) {
    $lines = @()
    foreach ($f in @(Get-ForgeFreeDisks $Disk)) {
        $d = $f.Disk
        $lines += ('Disk {0}: {1} ({2}, {3}, {4}), unallocated: {5}' -f $d.Number, $d.Model, $d.BusType, $d.PartitionStyle,
            (Format-ForgeSize $d.SizeBytes), (Format-ForgeSize $f.Free))
        foreach ($v in @($Disk.Volumes | Where-Object { $_.DiskNumber -eq $d.Number -and $_.DriveLetter })) {
            $lines += ('  {0}: {1}, {2}, {3} free' -f $v.DriveLetter, $v.FileSystem, (Format-ForgeSize $v.SizeBytes), (Format-ForgeSize $v.FreeBytes))
        }
    }
    return $lines
}

function Test-ForgeSpace($Disk) {
    $name = 'Free space for Forge OS'
    if (-not (Test-ForgeDiskReadable $Disk)) {
        return Build-ForgeResult 'free_space' $name 'unsure' @('Could not read the disks.') `
            'Run this again as admin, or open Disk Management (right-click Start, Disk Management) and look for Unallocated space of at least 200 GB.'
    }
    $lines = @(Get-ForgeDiskLines $Disk)
    $best = Get-ForgeBestFree $Disk
    $min = $script:MinGB * $script:GiB
    $rec = $script:RecGB * $script:GiB
    if ($null -ne $best -and $best.Free -ge $rec) {
        return Build-ForgeResult 'free_space' $name 'ok' (@("Disk $($best.Number) has $(Format-ForgeSize $best.Free) of unallocated space.") + $lines) `
            "In the installer, pick only this free space on disk $($best.Number)."
    }
    if ($null -ne $best -and $best.Free -ge $min) {
        return Build-ForgeResult 'free_space' $name 'look' (@("Disk $($best.Number) has $(Format-ForgeSize $best.Free) unallocated: enough to install (80 GB minimum), but under the 200 GB recommended.") + $lines) `
            'It works, but 200 GB or more is better for games and AI models. Shrink a drive in Disk Management to add more, or use a separate SSD (best option).'
    }
    # No usable unallocated space: find an NTFS drive that could be shrunk (keep 20 GB for Windows).
    $usb = @($Disk.Disks | Where-Object { $_.BusType -eq 'USB' } | ForEach-Object { $_.Number })
    $cand = $null
    foreach ($v in @($Disk.Volumes)) {
        if (-not $v.DriveLetter -or $v.FileSystem -ne 'NTFS' -or $usb -contains $v.DiskNumber) { continue }
        $room = [double]$v.FreeBytes - 20 * $script:GiB
        if ($room -ge $min -and ($null -eq $cand -or $room -gt $cand.Room)) { $cand = @{ Vol = $v; Room = $room } }
    }
    if ($null -ne $cand) {
        $l = $cand.Vol.DriveLetter
        return Build-ForgeResult 'free_space' $name 'look' (@('No unallocated space big enough for Forge OS yet.') + $lines) `
            ("Best option: a separate SSD for Forge OS. Otherwise: Shrink ${l}: in Disk Management (right-click Start, Disk Management, right-click ${l}:, Shrink Volume) to make at least 200 GB of Unallocated space. It has about $(Format-ForgeSize $cand.Room) to spare; Windows may allow less.")
    }
    return Build-ForgeResult 'free_space' $name 'look' (@('No unallocated space, and no drive with enough free room to shrink.') + $lines) `
        'Add a separate SSD for Forge OS (best option), or free up at least 200 GB on a drive and then shrink it in Disk Management.'
}

function Test-ForgeWindowsDisk($Disk) {
    $name = 'Windows disk and free space'
    $sysDisk = $null
    if (Test-ForgeDiskReadable $Disk) { $sysDisk = Get-ForgeSystemDiskNumber $Disk }
    if ($null -eq $sysDisk) {
        return Build-ForgeResult 'windows_disk' $name 'unsure' @('Could not tell which disk Windows is on.') `
            'In the installer, never pick the Windows partitions (look for NTFS partitions and the EFI partition).'
    }
    $best = Get-ForgeBestFree $Disk
    if ($null -eq $best) {
        return Build-ForgeResult 'windows_disk' $name 'ok' @("Windows is on disk $sysDisk. There is no unallocated space yet.") `
            'Nothing to do here: see the free space check.'
    }
    if ($best.Number -eq $sysDisk) {
        return Build-ForgeResult 'windows_disk' $name 'ok' @("Windows is on disk $sysDisk, the same disk as the free space.") `
            'That is fine. In the installer, pick only the free space, never the Windows partitions.'
    }
    return Build-ForgeResult 'windows_disk' $name 'ok' @("Windows is on disk $sysDisk. The free space is on disk $($best.Number), a separate disk.") `
        "Good. In the installer, pick only disk $($best.Number) so Windows is never touched."
}

function Test-ForgeBootMode($Disk, $Firmware) {
    $name = 'UEFI and GPT'
    $fw = $null
    if ($null -ne $Firmware -and -not $Firmware.Error) { $fw = $Firmware.FirmwareType }
    if ($fw -eq 'BIOS') {
        return Build-ForgeResult 'boot_mode' $name 'look' @('Windows starts in legacy BIOS mode, not UEFI.') `
            'Forge OS needs UEFI. Switching means converting the Windows disk to GPT (Microsoft mbr2gpt tool) and then turning on UEFI in the firmware settings. Ask for help first: a mistake can stop Windows from starting.'
    }
    if ($fw -ne 'UEFI') {
        return Build-ForgeResult 'boot_mode' $name 'unsure' @('Could not tell if the PC starts in UEFI or legacy BIOS mode.') `
            'Check by hand: press Win+R, type msinfo32, and look at BIOS Mode. It should say UEFI.'
    }
    if (-not (Test-ForgeDiskReadable $Disk)) {
        return Build-ForgeResult 'boot_mode' $name 'unsure' @('The PC starts in UEFI mode.', 'Could not read the disk layout (GPT or MBR).') `
            'Check by hand in Disk Management: right-click a disk, Properties, Volumes, Partition style. It should say GPT.'
    }
    $sysDisk = Get-ForgeSystemDiskNumber $Disk
    $best = Get-ForgeBestFree $Disk
    $styles = @($Disk.Disks | Where-Object { $_.BusType -ne 'USB' } | ForEach-Object { "Disk $($_.Number): $($_.PartitionStyle)" })
    $detail = @('The PC starts in UEFI mode.') + $styles
    foreach ($d in @($Disk.Disks)) {
        if ($d.PartitionStyle -ne 'MBR') { continue }
        if ($d.Number -eq $sysDisk) {
            return Build-ForgeResult 'boot_mode' $name 'look' $detail `
                "The Windows disk (disk $sysDisk) uses MBR. Not sure how this PC boots; ask for help before installing."
        }
        if ($null -ne $best -and $d.Number -eq $best.Number) {
            return Build-ForgeResult 'boot_mode' $name 'look' $detail `
                "Disk $($d.Number), where the free space is, uses MBR. Not sure the installer accepts that on a UEFI PC; GPT is safest. An empty disk can be switched to GPT in Disk Management (only if it holds nothing you need)."
        }
    }
    return Build-ForgeResult 'boot_mode' $name 'ok' $detail 'Nothing to do.'
}

function Test-ForgeSecureBoot($Data, [bool]$IsAdmin) {
    $name = 'Secure Boot'
    $how = @()
    if ($null -ne $Data -and $Data.Source -eq 'registry') { $how = @('Read from the registry. Run as admin to confirm.') }
    if ($null -ne $Data -and -not $Data.Error -and $Data.Supported -eq $false) {
        return Build-ForgeResult 'secure_boot' $name 'look' @('This PC has no Secure Boot (it starts in legacy BIOS mode).') `
            'See the UEFI and GPT check.' $true
    }
    if ($null -ne $Data -and -not $Data.Error -and $Data.Enabled -eq $true) {
        return Build-ForgeResult 'secure_boot' $name 'ok' (@('Secure Boot is on.') + $how) `
            'Fine. On the first reboot after installing, a blue MOK management screen may appear: choose Enroll MOK and type the password universalblue.' $true
    }
    if ($null -ne $Data -and -not $Data.Error -and $Data.Enabled -eq $false) {
        return Build-ForgeResult 'secure_boot' $name 'ok' (@('Secure Boot is off.') + $how) `
            'Forge OS works either way. If you turn it on later, enroll the key on the blue MOK screen (password universalblue).' $true
    }
    return Build-ForgeResult 'secure_boot' $name 'unsure' @('Could not read the Secure Boot state.') `
        'Run this checker again as admin, or press Win+R, type msinfo32 and look at Secure Boot State.' $true
}

function Get-ForgeGpuKind($Adapter) {
    $n = [string]$Adapter.Name
    $v = [string]$Adapter.VendorId
    if ($Adapter.PnpClass -eq 'ROOT' -or $Adapter.PnpClass -eq 'SWD') { return 'virtual-display' }
    if ($v -eq '10DE' -or $n -match 'NVIDIA|GeForce') { return 'nvidia' }
    if ($v -eq '1002' -or $v -eq '1022' -or $n -match '\bAMD\b|Radeon') { return 'amd' }
    if ($v -eq '8086' -or $n -match '\bIntel\b') { return 'intel' }
    if (@('15AD', '80EE', '1AF4', '1B36', '1234') -contains $v -or $n -match 'VMware|VirtualBox|QXL|VirtIO|Hyper-V') { return 'vm' }
    if ($v -eq '1414' -or $n -match 'Microsoft Basic|Microsoft Remote') { return 'basic' }
    return 'other'
}

function Get-ForgeNvidiaSupport([string]$Name) {
    # The NVIDIA image uses the open kernel driver: GTX 16 series (Turing) and newer.
    $n = $Name
    if ($n -match 'TITAN\s*RTX' -or $n -match 'Quadro\s*RTX' -or $n -match 'RTX\s*A\d{3,4}' -or $n -match 'RTX\s*\d{4}\s*Ada' -or
        $n -match 'RTX\s*PRO\s*\d{4}') {
        return 'ok'
    }
    if ($n -match 'RTX\s*[6-9]0\d0') { return 'unknown' }
    if ($n -match 'RTX\s*[2-5]0[5-9]0' -or $n -match 'GTX\s*16[3-6]0' -or $n -match 'MX\s*[45][5-9]0') { return 'ok' }
    if ($n -match 'GTX\s*(TITAN|10[5-8]0|9[5-8]0|7[5-8]0)' -or $n -match '\bGT\s*\d{3,4}\b' -or $n -match 'Quadro\s*[KMP]\d' -or
        $n -match 'Tesla' -or $n -match 'MX\s*[1-3][0-9]0') {
        return 'old'
    }
    return 'unknown'
}

function Get-ForgeImageSuggestion($Gpu) {
    if ($null -eq $Gpu -or $Gpu.Error) { return $null }
    $real = @(@($Gpu.Adapters) | Where-Object { (Get-ForgeGpuKind $_) -ne 'virtual-display' })
    $nv = @($real | Where-Object { (Get-ForgeGpuKind $_) -eq 'nvidia' })
    if ($nv.Count -gt 0) {
        $usable = @($nv | Where-Object { (Get-ForgeNvidiaSupport $_.Name) -ne 'old' })
        if ($usable.Count -gt 0) { return 'forge-os' }
        return 'forge-os-universal'
    }
    if (@($real | Where-Object { @('amd', 'intel', 'vm') -contains (Get-ForgeGpuKind $_) }).Count -gt 0) { return 'forge-os-universal' }
    return $null
}

function Test-ForgeGpu($Data) {
    $name = 'Graphics card and image'
    $adapters = @()
    if ($null -ne $Data -and -not $Data.Error) { $adapters = @($Data.Adapters) }
    $real = @($adapters | Where-Object { (Get-ForgeGpuKind $_) -ne 'virtual-display' })
    if ($real.Count -eq 0) {
        return Build-ForgeResult 'gpu' $name 'unsure' @('Could not read the graphics card.') `
            'Check its name in Device Manager, Display adapters. NVIDIA: use the forge-os image. Anything else: forge-os-universal.'
    }
    $detail = @($real | ForEach-Object { $_.Name })
    $image = Get-ForgeImageSuggestion $Data
    $nv = @($real | Where-Object { (Get-ForgeGpuKind $_) -eq 'nvidia' })
    if ($nv.Count -gt 0) {
        $support = @($nv | ForEach-Object { Get-ForgeNvidiaSupport $_.Name })
        if ($support -contains 'ok') {
            $msg = 'Supported by the open NVIDIA driver in the forge-os image.'
            if (@($nv | Where-Object { $_.Name -match 'RTX\s*50\d0' }).Count -gt 0) { $msg = 'RTX 50 series: supported by the open NVIDIA driver in the forge-os image.' }
            return Build-ForgeResult 'gpu' $name 'ok' ($detail + $msg) 'Use the forge-os image (NVIDIA). After installing, nvidia-smi should show the card.'
        }
        if ($support -contains 'unknown') {
            return Build-ForgeResult 'gpu' $name 'unsure' ($detail + 'Not sure this card works with the open NVIDIA driver yet (very new or unusual model).') `
                'Use the forge-os image and check nvidia-smi after installing. If it does not show the card, ask for help.'
        }
        return Build-ForgeResult 'gpu' $name 'look' ($detail + 'This card is older than the GTX 16 series. The open NVIDIA driver in the forge-os image does not support it.') `
            'Use forge-os-universal. Graphics work with the basic open driver, but slower and without CUDA (not sure how well for games).'
    }
    $kinds = @($real | ForEach-Object { Get-ForgeGpuKind $_ })
    if ($kinds -contains 'amd') {
        return Build-ForgeResult 'gpu' $name 'ok' ($detail + 'AMD graphics: use the forge-os-universal image.') `
            'Use forge-os-universal. Local AI models run on the CPU on AMD for now.'
    }
    if ($kinds -contains 'intel') {
        return Build-ForgeResult 'gpu' $name 'ok' ($detail + 'Intel graphics: use the forge-os-universal image.') 'Use forge-os-universal.'
    }
    if ($kinds -contains 'vm') {
        return Build-ForgeResult 'gpu' $name 'ok' ($detail + 'This looks like a virtual machine: use the forge-os-universal image.') 'Use forge-os-universal.'
    }
    return Build-ForgeResult 'gpu' $name 'unsure' ($detail + 'Only a basic display driver is active, so the real card is unknown.') `
        'Install the graphics driver in Windows, or check the card name in Device Manager, Display adapters.'
}

function Test-ForgeRam($Sys) {
    $name = 'Memory (RAM)'
    if ($null -eq $Sys -or $Sys.Error -or -not $Sys.RamBytes) {
        return Build-ForgeResult 'ram' $name 'unsure' @('Could not read the amount of RAM.') 'Check by hand: Settings, System, About, Installed RAM.'
    }
    $gb = [math]::Round([double]$Sys.RamBytes / $script:GiB)
    if ($gb -ge 16) { return Build-ForgeResult 'ram' $name 'ok' @("$gb GB of RAM.") 'Nothing to do.' }
    if ($gb -ge 8) {
        return Build-ForgeResult 'ram' $name 'look' @("$gb GB of RAM.") 'Forge OS works, but local AI models, games and virtual machines will be tight. 16 GB or more is better.'
    }
    return Build-ForgeResult 'ram' $name 'look' @("$gb GB of RAM.") 'Below the 8 GB minimum. Forge OS will be very slow.'
}

function Test-ForgeCpu($Sys) {
    $name = 'Processor (CPU)'
    if ($null -eq $Sys -or $Sys.Error -or -not $Sys.CpuName) {
        return Build-ForgeResult 'cpu' $name 'unsure' @('Could not read the processor.') 'Check by hand: Settings, System, About, Processor.'
    }
    $line = "$($Sys.CpuName), $($Sys.Cores) cores, $($Sys.Threads) threads"
    $arch = [string]$Sys.Architecture
    if ($arch -eq '9') { return Build-ForgeResult 'cpu' $name 'ok' @("$line, 64-bit (x64).") 'Nothing to do.' }
    if ($arch -eq '12') { return Build-ForgeResult 'cpu' $name 'look' @("$line, ARM64.") 'Forge OS images are for x64 PCs (Intel or AMD) only. It will not run on this PC.' }
    if ($arch -eq '0') { return Build-ForgeResult 'cpu' $name 'look' @("$line, 32-bit.") 'Forge OS needs a 64-bit (x64) processor.' }
    return Build-ForgeResult 'cpu' $name 'unsure' @($line) 'Not sure what kind of processor this is. Forge OS needs a 64-bit Intel or AMD processor.'
}

function Test-ForgeVirtualization($Sys) {
    $name = 'Virtualization (for virtual machines)'
    if ($null -eq $Sys -or $Sys.Error) {
        return Build-ForgeResult 'virtualization' $name 'unsure' @('Could not read the processor.') `
            'Check by hand: Task Manager, Performance, CPU, Virtualization. Only needed for virtual machines in Forge OS.'
    }
    if ($Sys.HypervisorPresent -eq $true) {
        return Build-ForgeResult 'virtualization' $name 'ok' @('A hypervisor already runs in Windows (Hyper-V or Windows security features), so virtualization is on in the firmware.') 'Nothing to do.'
    }
    if ($Sys.VmExtensions -eq $false) {
        return Build-ForgeResult 'virtualization' $name 'look' @('This processor has no virtualization support.') 'Virtual machines in Forge OS will not work. Everything else does.'
    }
    if ($Sys.VirtFirmware -eq $true) { return Build-ForgeResult 'virtualization' $name 'ok' @('Virtualization is on in the firmware.') 'Nothing to do.' }
    if ($Sys.VirtFirmware -eq $false) {
        return Build-ForgeResult 'virtualization' $name 'look' @('Virtualization is off in the firmware (BIOS).') `
            'Only needed for virtual machines in Forge OS: turn on SVM Mode (AMD) or Intel Virtualization Technology (VT-x) in the firmware settings.'
    }
    return Build-ForgeResult 'virtualization' $name 'unsure' @('Could not tell if virtualization is on.') `
        'Check by hand: Task Manager, Performance, CPU, Virtualization. Only needed for virtual machines in Forge OS.'
}

function Get-ForgeLabeled($Disk, [string]$Label) {
    return @(@($Disk.Volumes) | Where-Object { Test-ForgeSameText ([string]$_.Label) $Label })
}

function Get-ForgeVolumeLine($Vol, [string]$Label) {
    return ('{0}: is named {1} ({2}, {3}, {4} free).' -f $Vol.DriveLetter, $Label, $Vol.FileSystem, (Format-ForgeSize $Vol.SizeBytes), (Format-ForgeSize $Vol.FreeBytes))
}

function Test-ForgeSharedDrive($Disk, $BitLocker) {
    $name = 'SHARED drive (optional)'
    if (-not (Test-ForgeDiskReadable $Disk)) {
        return Build-ForgeResult 'shared_drive' $name 'unsure' @('Could not read the drives.') 'Run this again as admin.'
    }
    $found = @(Get-ForgeLabeled $Disk 'SHARED')
    if ($found.Count -eq 0) {
        return Build-ForgeResult 'shared_drive' $name 'ok' @('No drive is named SHARED.') `
            'Optional: to share files between Windows and Forge OS, make a partition or drive formatted as exFAT or NTFS and name it SHARED.'
    }
    if ($found.Count -gt 1) {
        return Build-ForgeResult 'shared_drive' $name 'look' @('More than one drive is named SHARED: ' + (($found | ForEach-Object { "$($_.DriveLetter):" }) -join ', ')) `
            'Keep the name SHARED on one drive only.'
    }
    $v = $found[0]
    $detail = @(Get-ForgeVolumeLine $v 'SHARED')
    if ($null -ne $BitLocker -and -not $BitLocker.Error -and $v.DriveLetter) {
        $enc = @(@($BitLocker.Volumes) | Where-Object { (Test-ForgeSameText $_.MountPoint "$($v.DriveLetter):") -and ($_.Encrypted -eq $true -or $_.ProtectionOn -eq $true) })
        if ($enc.Count -gt 0) {
            return Build-ForgeResult 'shared_drive' $name 'look' ($detail + 'It is encrypted with BitLocker.') `
                'Not sure Forge OS can open a BitLocker drive. Turn BitLocker off for this drive if you want to share it.'
        }
    }
    if ($v.FileSystem -eq 'NTFS' -or $v.FileSystem -eq 'exFAT') {
        return Build-ForgeResult 'shared_drive' $name 'ok' $detail 'Nothing to do. Keep Fast Startup off so Forge OS can write to it.'
    }
    if ($v.FileSystem -eq 'FAT32') {
        return Build-ForgeResult 'shared_drive' $name 'look' ($detail + 'FAT32 cannot hold files over 4 GB.') `
            'Reformat it as exFAT or NTFS (this erases it: copy your files off first).'
    }
    return Build-ForgeResult 'shared_drive' $name 'look' ($detail + "Forge OS may not be able to use $($v.FileSystem).") `
        'Reformat it as exFAT or NTFS (this erases it: copy your files off first).'
}

function Test-ForgeBackupDrive($Disk) {
    $name = 'FORGEBACKUP drive (optional)'
    if (-not (Test-ForgeDiskReadable $Disk)) {
        return Build-ForgeResult 'backup_drive' $name 'unsure' @('Could not read the drives.') 'Run this again as admin.'
    }
    $found = @(Get-ForgeLabeled $Disk 'FORGEBACKUP')
    if ($found.Count -eq 0) {
        return Build-ForgeResult 'backup_drive' $name 'ok' @('No drive is named FORGEBACKUP.') `
            'Optional: for Forge OS backups, name a drive FORGEBACKUP, ideally a separate disk or USB drive.'
    }
    if ($found.Count -gt 1) {
        return Build-ForgeResult 'backup_drive' $name 'look' @('More than one drive is named FORGEBACKUP: ' + (($found | ForEach-Object { "$($_.DriveLetter):" }) -join ', ')) `
            'Keep the name FORGEBACKUP on one drive only.'
    }
    $v = $found[0]
    $detail = @(Get-ForgeVolumeLine $v 'FORGEBACKUP')
    $sysDisk = Get-ForgeSystemDiskNumber $Disk
    if ($null -ne $sysDisk -and $v.DiskNumber -eq $sysDisk) {
        return Build-ForgeResult 'backup_drive' $name 'look' ($detail + 'It is on the same disk as Windows. If that disk fails, the backups are lost too.') `
            'Better: use a separate disk or a USB drive for FORGEBACKUP.'
    }
    if ($v.FileSystem -eq 'ReFS') {
        return Build-ForgeResult 'backup_drive' $name 'look' ($detail + 'Forge OS cannot use ReFS.') 'Reformat it as NTFS or exFAT (this erases it).'
    }
    return Build-ForgeResult 'backup_drive' $name 'ok' $detail 'Nothing to do. Forge OS backups work on NTFS or exFAT drives.'
}

function Test-ForgeKeyDrive($Disk) {
    $name = 'FORGEKEY USB stick (optional)'
    if (-not (Test-ForgeDiskReadable $Disk)) {
        return Build-ForgeResult 'key_drive' $name 'unsure' @('Could not read the drives.') 'Run this again as admin.'
    }
    $found = @(Get-ForgeLabeled $Disk 'FORGEKEY')
    if ($found.Count -eq 0) {
        return Build-ForgeResult 'key_drive' $name 'ok' @('No drive named FORGEKEY is plugged in.') `
            'Optional, for USB key unlock on an encrypted install: name a USB stick FORGEKEY, formatted FAT32 (safest) or exFAT.'
    }
    if ($found.Count -gt 1) {
        return Build-ForgeResult 'key_drive' $name 'look' @('More than one FORGEKEY drive is plugged in: ' + (($found | ForEach-Object { "$($_.DriveLetter):" }) -join ', ')) `
            'Fine for now, but when you add or test a key in Forge OS, plug in only one stick at a time.'
    }
    $v = $found[0]
    $detail = @(Get-ForgeVolumeLine $v 'FORGEKEY')
    $fs = [string]$v.FileSystem
    if (@('FAT32', 'FAT', 'exFAT') -notcontains $fs) {
        return Build-ForgeResult 'key_drive' $name 'look' ($detail + "Forge OS only reads key sticks formatted FAT32 or exFAT, not $fs.") `
            'Reformat the stick as FAT32 (right-click it in File Explorer, Format; this erases it), then name it FORGEKEY again.'
    }
    $disk = @(@($Disk.Disks) | Where-Object { $_.Number -eq $v.DiskNumber })
    if ($disk.Count -gt 0 -and $disk[0].BusType -ne 'USB' -and $disk[0].BusType -ne 'unknown') {
        return Build-ForgeResult 'key_drive' $name 'look' ($detail + "It is on a $($disk[0].BusType) disk, not a USB stick.") `
            'Forge OS only accepts a USB stick as the key. Name a USB stick FORGEKEY instead, and rename this drive.'
    }
    return Build-ForgeResult 'key_drive' $name 'ok' $detail 'Nothing to do. Keep the stick away from the PC when you are not using it.'
}

function Test-ForgeWindowsVersion($Data) {
    $name = 'Windows version'
    if ($null -eq $Data -or $Data.Error -or -not $Data.Caption) {
        return Build-ForgeResult 'windows_version' $name 'unsure' @('Could not read the Windows version.') 'Nothing to do. Just for your information.'
    }
    $build = [string]$Data.Build
    if ($null -ne $Data.Ubr) { $build = "$build.$($Data.Ubr)" }
    $line = $Data.Caption
    if ($Data.DisplayVersion) { $line = "$line, version $($Data.DisplayVersion)" }
    $line = "$line (build $build), $($Data.Arch)."
    return Build-ForgeResult 'windows_version' $name 'ok' @($line) 'Nothing to do. Just for your information.'
}

# ============================================================================ run everything

function Invoke-ForgeChecks {
    $isAdmin = [bool](Invoke-ForgeSafe { Get-ForgeProbeAdmin })
    $bl = ConvertTo-ForgeCleanData (Invoke-ForgeSafe { Get-ForgeProbeBitLocker -IsAdmin $isAdmin })
    $fs = ConvertTo-ForgeCleanData (Invoke-ForgeSafe { Get-ForgeProbeFastStartup })
    $hib = ConvertTo-ForgeCleanData (Invoke-ForgeSafe { Get-ForgeProbeHibernation })
    $disk = ConvertTo-ForgeCleanData (Invoke-ForgeSafe { Get-ForgeProbeDisks })
    $fw = ConvertTo-ForgeCleanData (Invoke-ForgeSafe { Get-ForgeProbeFirmware })
    $sb = ConvertTo-ForgeCleanData (Invoke-ForgeSafe { Get-ForgeProbeSecureBoot -IsAdmin $isAdmin })
    $gpu = ConvertTo-ForgeCleanData (Invoke-ForgeSafe { Get-ForgeProbeGpu })
    $sys = ConvertTo-ForgeCleanData (Invoke-ForgeSafe { Get-ForgeProbeSystem })
    $win = ConvertTo-ForgeCleanData (Invoke-ForgeSafe { Get-ForgeProbeWindows })

    $plan = @(
        @('bitlocker', 'BitLocker (drive encryption)', { Test-ForgeBitLocker $bl $isAdmin }),
        @('fast_startup', 'Fast Startup', { Test-ForgeFastStartup $fs }),
        @('hibernation', 'Hibernation', { Test-ForgeHibernation $hib }),
        @('free_space', 'Free space for Forge OS', { Test-ForgeSpace $disk }),
        @('windows_disk', 'Windows disk and free space', { Test-ForgeWindowsDisk $disk }),
        @('boot_mode', 'UEFI and GPT', { Test-ForgeBootMode $disk $fw }),
        @('secure_boot', 'Secure Boot', { Test-ForgeSecureBoot $sb $isAdmin }),
        @('gpu', 'Graphics card and image', { Test-ForgeGpu $gpu }),
        @('ram', 'Memory (RAM)', { Test-ForgeRam $sys }),
        @('cpu', 'Processor (CPU)', { Test-ForgeCpu $sys }),
        @('virtualization', 'Virtualization (for virtual machines)', { Test-ForgeVirtualization $sys }),
        @('shared_drive', 'SHARED drive (optional)', { Test-ForgeSharedDrive $disk $bl }),
        @('backup_drive', 'FORGEBACKUP drive (optional)', { Test-ForgeBackupDrive $disk }),
        @('key_drive', 'FORGEKEY USB stick (optional)', { Test-ForgeKeyDrive $disk }),
        @('windows_version', 'Windows version', { Test-ForgeWindowsVersion $win })
    )
    $results = @()
    foreach ($p in $plan) {
        $r = $null
        $r = Invoke-ForgeSafe $p[2]
        if ($null -eq $r -or -not ($r.PSObject.Properties.Name -contains 'Status')) {
            $r = Build-ForgeResult $p[0] $p[1] 'unsure' @('This check could not run.') 'Check this by hand, and please report it.'
        }
        $results += $r
    }
    $image = $null
    try { $image = Get-ForgeImageSuggestion $gpu } catch { $image = $null }
    return @{ IsAdmin = $isAdmin; Results = $results; Image = $image }
}

function Format-ForgeReport($Run) {
    $lines = @()
    $lines += 'Forge OS pre-install check for Windows (read-only: it changes nothing)'
    $lines += ('Checker version {0}, made {1} UTC. Running as: {2}.' -f $script:ForgeVersion,
        (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm', $script:Inv), $(if ($Run.IsAdmin) { 'admin' } else { 'normal user' }))
    $lines += ''
    $counts = @{ ok = 0; look = 0; unsure = 0 }
    foreach ($r in @($Run.Results)) {
        $counts[$r.Status] = $counts[$r.Status] + 1
        $title = $r.Name
        if ($r.NeedsAdmin -and -not $Run.IsAdmin) { $title = "$title (needs admin for a sure answer)" }
        $lines += ('[{0}] {1}' -f (Get-ForgeLabel $r.Status), $title)
        foreach ($d in @($r.Detail)) { $lines += "    $d" }
        $lines += "    What to do: $($r.Todo)"
        $lines += ''
    }
    $lines += ('Summary: {0} OK, {1} to look at, {2} not sure.' -f $counts['ok'], $counts['look'], $counts['unsure'])
    if ($Run.Image -eq 'forge-os') { $lines += 'Suggested image: forge-os (NVIDIA).' }
    elseif ($Run.Image) { $lines += "Suggested image: $($Run.Image) (AMD, Intel or virtual machine)." }
    else { $lines += 'Suggested image: not sure (see the graphics check).' }
    $admin = @(@($Run.Results) | Where-Object { $_.NeedsAdmin } | ForEach-Object { $_.Name })
    if (-not $Run.IsAdmin -and $admin.Count -gt 0) {
        $lines += ('These checks need admin for a sure answer: {0}. To run as admin: right-click Windows PowerShell, Run as administrator, then run the same command again.' -f ($admin -join ', '))
    }
    $lines += 'Nothing on this PC was changed.'
    return $lines
}

function ConvertTo-ForgeJson($Run, [string]$ReportFile = '') {
    $checks = @()
    $counts = [ordered]@{ ok = 0; look = 0; unsure = 0 }
    foreach ($r in @($Run.Results)) {
        $counts[$r.Status] = $counts[$r.Status] + 1
        $checks += [ordered]@{ id = $r.Id; name = $r.Name; status = $r.Status; label = (Get-ForgeLabel $r.Status)
            detail = [string[]]@($r.Detail); todo = $r.Todo; needs_admin = [bool]$r.NeedsAdmin }
    }
    $o = [ordered]@{
        tool = 'forge-precheck'
        version = $script:ForgeVersion
        generated_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', $script:Inv)
        powershell = $PSVersionTable.PSVersion.ToString()
        read_only = $true
        is_admin = [bool]$Run.IsAdmin
        suggested_image = $Run.Image
        counts = $counts
        report_file = $ReportFile
        checks = [object[]]$checks
    }
    return (ConvertTo-Json -InputObject $o -Depth 6)
}

# The ONLY function that writes anything: a new text file on the Desktop, never an existing one.
function Save-ForgeReport {
    param([string[]]$Lines)
    $desk = Get-ForgeProbeDesktop
    if ([string]::IsNullOrEmpty($desk) -or -not (Test-Path -LiteralPath $desk -PathType Container)) {
        throw 'Could not find your Desktop folder.'
    }
    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss', $script:Inv)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes((($Lines -join "`r`n") + "`r`n"))
    for ($i = 0; $i -lt 20; $i++) {
        $file = "forge-precheck-$stamp.txt"
        if ($i -gt 0) { $file = "forge-precheck-$stamp-$i.txt" }
        $path = Join-Path $desk $file
        if (Test-Path -LiteralPath $path) { continue }
        $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try { $fs.Write($bytes, 0, $bytes.Length) }
        finally { $fs.Dispose() }
        return $path
    }
    throw 'Could not pick a new file name for the report.'
}

function Invoke-ForgePrecheck {
    param([switch]$Json, [switch]$SaveReport)
    $run = Invoke-ForgeChecks
    $lines = Format-ForgeReport -Run $run
    $saved = $null
    $saveError = $null
    if ($SaveReport) {
        try { $saved = Save-ForgeReport -Lines $lines }
        catch { $saveError = $_.Exception.Message }
    }
    if ($Json) {
        $name = ''
        if ($saved) { $name = Split-Path -Leaf $saved }
        Write-Output (ConvertTo-ForgeJson -Run $run -ReportFile $name)
        return
    }
    foreach ($l in $lines) {
        if ($l.StartsWith('[OK]')) { Write-Host $l -ForegroundColor Green }
        elseif ($l.StartsWith('[Look at this]')) { Write-Host $l -ForegroundColor Yellow }
        elseif ($l.StartsWith('[Not sure]')) { Write-Host $l -ForegroundColor Cyan }
        else { Write-Host $l }
    }
    if ($saved) { Write-Host ('Report saved on your Desktop: ' + (Split-Path -Leaf $saved)) }
    if ($saveError) { Write-Host ('Could not save the report: ' + (ConvertTo-ForgeCleanText $saveError)) -ForegroundColor Yellow }
}

# Run only when started as a script, not when loaded with "." by the tests.
if ($MyInvocation.InvocationName -ne '.') {
    Invoke-ForgePrecheck -Json:$Json -SaveReport:$SaveReport
}
