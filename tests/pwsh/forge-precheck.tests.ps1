# Tests for tools/windows/forge-precheck.ps1, run with fake probe data (no Windows needed).
# Plain PowerShell, no Pester: run as   pwsh -NoProfile -File forge-precheck.tests.ps1 <path to script>
# The Python wrapper tests/test_windows_precheck.py runs this when pwsh is available.
param([Parameter(Mandatory = $true)][string]$ScriptPath)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$script:Failures = New-Object System.Collections.Generic.List[string]
$script:Count = 0

function Check([string]$What, [bool]$Condition) {
    $script:Count++
    if (-not $Condition) { $script:Failures.Add($What); Write-Host "FAIL: $What" }
}
function Eq([string]$What, $Expected, $Actual) {
    Check "$What (expected '$Expected', got '$Actual')" ($Expected -eq $Actual)
}
function Fake([string]$Name, [scriptblock]$Body) { Set-Item -Path "function:script:$Name" -Value $Body }
function Has([string]$What, [string]$Needle, $Haystack) {
    $text = ($Haystack | Out-String)
    Check "$What (missing '$Needle')" ($text -like "*$Needle*")
}

# ---------------------------------------------------------------- static checks on the parsed script
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($ScriptPath, [ref]$tokens, [ref]$errors)
Eq 'script parses without errors' 0 $errors.Count

# Syntax that Windows PowerShell 5.1 does not understand.
$newSyntax = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.TernaryExpressionAst] -or
        $n -is [System.Management.Automation.Language.PipelineChainAst] -or
        ($n -is [System.Management.Automation.Language.BinaryExpressionAst] -and $n.Operator -eq 'QuestionQuestion') -or
        ($n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Operator -eq 'QuestionQuestionEquals') -or
        ($n -is [System.Management.Automation.Language.MemberExpressionAst] -and $n.NullConditional)
    }, $true)
Eq 'no PowerShell 7 only syntax (?:, ??, ?., &&, ||)' 0 @($newSyntax).Count

# Every command the script runs must be read-only: its own functions, or this allowlist.
$own = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) | ForEach-Object { $_.Name })
$allowed = @(
    'Get-CimInstance', 'Get-ItemProperty', 'Get-Disk', 'Get-Partition', 'Get-Volume', 'Get-BitLockerVolume',
    'Confirm-SecureBootUEFI', 'Get-Date', 'Get-Command',
    'Select-Object', 'Where-Object', 'ForEach-Object', 'Sort-Object', 'Measure-Object', 'Group-Object',
    'Write-Host', 'Write-Output', 'Out-String', 'ConvertTo-Json', 'Join-Path', 'Split-Path', 'Test-Path')
$cmds = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)
$bad = @()
foreach ($c in $cmds) {
    $name = $c.GetCommandName()
    if ($null -eq $name) {
        # Dynamic calls: only  & $exe -status  (manage-bde, read-only) and  & $Block  (the script's own
        # error wrapper, which runs a probe or check written in this file).
        $text1 = $c.Extent.Text
        $args1 = @($c.CommandElements | Select-Object -Skip 1 | ForEach-Object { $_.Extent.Text })
        $ok1 = (($text1 -match '^&\s*\$exe\s') -and (($args1 -join ' ') -eq '-status')) -or ($text1 -match '^&\s*\$Block$')
        if (-not $ok1) { $bad += $text1 }
    }
    elseif ($own -notcontains $name -and $allowed -notcontains $name) { $bad += $name }
}
Eq ('only read-only commands are run: ' + ($bad -join ', ')) 0 $bad.Count

# ---------------------------------------------------------------- load the script without running it
. $ScriptPath

# Fake data for a typical owner PC: RTX 5070, Windows on disk 0, a second SSD with free space.
$GB = [double]1GB
function Disks-Typical {
    @{
        Error = $null; SystemDrive = 'C'
        Disks = @(
            @{ Number = 0; Model = 'Samsung SSD 980 1TB'; BusType = 'NVMe'; SizeBytes = 931 * $GB; PartitionStyle = 'GPT'; FreeExtentBytes = 0 },
            @{ Number = 1; Model = 'WD Blue SN580 2TB'; BusType = 'NVMe'; SizeBytes = 1863 * $GB; PartitionStyle = 'GPT'; FreeExtentBytes = 1500 * $GB },
            @{ Number = 2; Model = 'SanDisk Cruzer'; BusType = 'USB'; SizeBytes = 14 * $GB; PartitionStyle = 'MBR'; FreeExtentBytes = 0 }
        )
        Volumes = @(
            @{ DiskNumber = 0; DriveLetter = 'C'; Label = 'Windows of Somchai'; FileSystem = 'NTFS'; SizeBytes = 900 * $GB; FreeBytes = 300 * $GB; DriveType = 'Fixed' },
            @{ DiskNumber = 1; DriveLetter = 'S'; Label = 'SHARED'; FileSystem = 'exFAT'; SizeBytes = 300 * $GB; FreeBytes = 250 * $GB; DriveType = 'Fixed' },
            @{ DiskNumber = 1; DriveLetter = 'B'; Label = 'ForgeBackup'; FileSystem = 'NTFS'; SizeBytes = 63 * $GB; FreeBytes = 60 * $GB; DriveType = 'Fixed' },
            @{ DiskNumber = 2; DriveLetter = 'H'; Label = 'FORGEKEY'; FileSystem = 'FAT32'; SizeBytes = 14 * $GB; FreeBytes = 14 * $GB; DriveType = 'Removable' }
        )
    }
}
function Set-Typical {
    Fake 'Get-ForgeProbeAdmin' { $false }
    Fake 'Get-ForgeProbeBitLocker' { param($IsAdmin) @{ Source = 'manage-bde'; Error = $null; Volumes = @(@{ MountPoint = 'C:'; Encrypted = $false; ProtectionOn = $false }) } }
    Fake 'Get-ForgeProbeFastStartup' { @{ Error = $null; HiberbootEnabled = 0 } }
    Fake 'Get-ForgeProbeHibernation' { @{ Error = $null; HibernateEnabled = 0 } }
    Fake 'Get-ForgeProbeDisks' { Disks-Typical }
    Fake 'Get-ForgeProbeFirmware' { @{ Error = $null; FirmwareType = 'UEFI' } }
    Fake 'Get-ForgeProbeSecureBoot' { param($IsAdmin) @{ Error = $null; Enabled = $true; Supported = $true; Source = 'registry' } }
    Fake 'Get-ForgeProbeGpu' { @{ Error = $null; Adapters = @(@{ Name = 'NVIDIA GeForce RTX 5070'; VendorId = '10DE'; PnpClass = 'PCI' }) } }
    Fake 'Get-ForgeProbeSystem' { @{ Error = $null; RamBytes = 64 * $GB; CpuName = 'AMD Ryzen 7 7700 8-Core Processor'; Cores = 8; Threads = 16; Architecture = 9; VirtFirmware = $false; VmExtensions = $true; HypervisorPresent = $true } }
    Fake 'Get-ForgeProbeWindows' { @{ Error = $null; Caption = 'Microsoft Windows 11 Pro'; Version = '10.0.26100'; Build = '26100'; DisplayVersion = '24H2'; Ubr = 4349; Arch = '64-bit' } }
    Fake 'Get-ForgeProbeDesktop' { $script:FakeDesktop }
}
function Run { Invoke-ForgeChecks }
function Find($run, $id) { @($run.Results | Where-Object { $_.Id -eq $id })[0] }
function St($run, $id) { (Find $run $id).Status }

$script:FakeDesktop = Join-Path ([System.IO.Path]::GetTempPath()) ('forge-precheck-test-' + [guid]::NewGuid())
[void][System.IO.Directory]::CreateDirectory($script:FakeDesktop)

# ---------------------------------------------------------------- the typical PC
Set-Typical
$run = Run
$ids = @($run.Results | ForEach-Object { $_.Id })
foreach ($id in 'bitlocker', 'fast_startup', 'hibernation', 'free_space', 'windows_disk', 'boot_mode', 'secure_boot',
    'gpu', 'ram', 'cpu', 'virtualization', 'shared_drive', 'backup_drive', 'key_drive', 'windows_version') {
    Check "check '$id' is present" ($ids -contains $id)
}
foreach ($r in $run.Results) {
    Check "status of $($r.Id) is one of ok/look/unsure" (@('ok', 'look', 'unsure') -contains $r.Status)
    Check "$($r.Id) has a what to do line" ([string]::IsNullOrWhiteSpace($r.Todo) -eq $false)
}
Eq 'typical: bitlocker off is OK' 'ok' (St $run 'bitlocker')
Eq 'typical: fast startup off is OK' 'ok' (St $run 'fast_startup')
Eq 'typical: 1500 GB free is OK' 'ok' (St $run 'free_space')
Has 'free space names the disk' 'disk 1' ((Find $run 'free_space').Detail).ToLower()
Eq 'typical: windows disk inform' 'ok' (St $run 'windows_disk')
Has 'windows disk says separate disk' 'separate' (Find $run 'windows_disk').Detail
Eq 'typical: UEFI and GPT' 'ok' (St $run 'boot_mode')
Eq 'typical: RTX 5070 OK' 'ok' (St $run 'gpu')
Eq 'typical: image suggestion' 'forge-os' $run.Image
Has 'RTX 50 mentions open driver' 'open NVIDIA driver' (Find $run 'gpu').Detail
Eq 'typical: 64 GB RAM' 'ok' (St $run 'ram')
Eq 'typical: hypervisor present means virtualization on' 'ok' (St $run 'virtualization')
Eq 'typical: SHARED exFAT' 'ok' (St $run 'shared_drive')
Eq 'typical: backup found (label match ignores case)' 'ok' (St $run 'backup_drive')
Has 'backup drive letter shown' 'B:' (Find $run 'backup_drive').Detail
Eq 'typical: FORGEKEY FAT32 USB' 'ok' (St $run 'key_drive')
Has 'windows version shown' '24H2' (Find $run 'windows_version').Detail
Check 'secure boot marked as needing admin' ((Find $run 'secure_boot').NeedsAdmin)
Check 'bitlocker marked as needing admin' ((Find $run 'bitlocker').NeedsAdmin)
Check 'not admin is reported' (-not $run.IsAdmin)

# Privacy: own drive labels and anything personal never appear in output.
$lines = Format-ForgeReport -Run $run
$text = $lines -join "`n"
Check 'report hides other drive labels' ($text -notlike '*Somchai*')
Has 'report has a summary line' 'Suggested image: forge-os' $text
Has 'report says which checks need admin' 'need admin' $text
Has 'report shows the labels' 'Look at this' ((Format-ForgeReport -Run @{ IsAdmin = $false; Image = $null; Results = @((Build-ForgeResult 'x' 'X' 'look' @('d') 't')) }) -join "`n")
Check 'report is ASCII' (-not ($text -match '[^\x00-\x7F]'))
Check 'no em dash or double hyphen in report' (($text -notmatch [char]0x2014) -and ($text -notmatch '--'))

$jsonText = ConvertTo-ForgeJson -Run $run
$obj = $jsonText | ConvertFrom-Json
Eq 'json: tool name' 'forge-precheck' $obj.tool
Eq 'json: one entry per check' $run.Results.Count @($obj.checks).Count
Eq 'json: status label' 'OK' (@($obj.checks | Where-Object { $_.id -eq 'gpu' })[0].label)
Eq 'json: image' 'forge-os' $obj.suggested_image
Check 'json: detail is an array' (@($obj.checks)[0].detail -is [array] -or @($obj.checks)[0].detail -is [string])
Check 'json hides other drive labels' ($jsonText -notlike '*Somchai*')

# ---------------------------------------------------------------- one thing wrong at a time
Set-Typical
Fake 'Get-ForgeProbeBitLocker' { param($IsAdmin) @{ Source = 'Get-BitLockerVolume'; Error = $null; Volumes = @(@{ MountPoint = 'C:'; Encrypted = $true; ProtectionOn = $true }, @{ MountPoint = 'S:'; Encrypted = $false; ProtectionOn = $false }) } }
$run = Run
Eq 'bitlocker on: look' 'look' (St $run 'bitlocker')
Has 'bitlocker on: recovery key reminder' 'recovery key' (Find $run 'bitlocker').Todo
Has 'bitlocker on: off this PC' 'off this PC' (Find $run 'bitlocker').Todo

Fake 'Get-ForgeProbeBitLocker' { param($IsAdmin) @{ Source = 'manage-bde'; Error = 'Access denied'; Volumes = @() } }
$run = Run
Eq 'bitlocker unreadable: not sure' 'unsure' (St $run 'bitlocker')
Has 'bitlocker unreadable: run as admin' 'admin' (Find $run 'bitlocker').Todo

Set-Typical
Fake 'Get-ForgeProbeFastStartup' { @{ Error = $null; HiberbootEnabled = 1 } }
Eq 'fast startup on: look' 'look' (St (Run) 'fast_startup')
Fake 'Get-ForgeProbeFastStartup' { @{ Error = 'not found'; HiberbootEnabled = $null } }
Eq 'fast startup unknown: not sure' 'unsure' (St (Run) 'fast_startup')

Set-Typical
Fake 'Get-ForgeProbeHibernation' { @{ Error = $null; HibernateEnabled = 1 } }
Eq 'hibernation on: look' 'look' (St (Run) 'hibernation')
Fake 'Get-ForgeProbeHibernation' { @{ Error = 'x'; HibernateEnabled = $null } }
Eq 'hibernation unknown: not sure' 'unsure' (St (Run) 'hibernation')

# Space: 100 GB unallocated is enough to install but under the 200 GB recommendation.
Set-Typical
Fake 'Get-ForgeProbeDisks' { $d = Disks-Typical; $d.Disks[1].FreeExtentBytes = 100 * $GB; $d }
$run = Run
Eq '100 GB free: look' 'look' (St $run 'free_space')
Has '100 GB mentions 200 GB' '200 GB' (Find $run 'free_space').Todo
# No unallocated space at all, but C: has plenty of free room: suggest shrinking or an SSD.
Fake 'Get-ForgeProbeDisks' { $d = Disks-Typical; $d.Disks[1].FreeExtentBytes = 0; $d }
$run = Run
Eq 'no free space: look' 'look' (St $run 'free_space')
Has 'no free space: shrink' 'Shrink' (Find $run 'free_space').Todo
Has 'no free space: names a drive to shrink' 'C:' (Find $run 'free_space').Todo
Has 'no free space: separate SSD' 'SSD' (Find $run 'free_space').Todo
# Tiny free extents (alignment leftovers) do not count.
Fake 'Get-ForgeProbeDisks' { $d = Disks-Typical; $d.Disks[1].FreeExtentBytes = 1MB; $d.Volumes[0].FreeBytes = 10 * $GB; $d }
$run = Run
Eq 'nothing free anywhere: look' 'look' (St $run 'free_space')
Has 'nothing free: SSD' 'SSD' (Find $run 'free_space').Todo
# A brand new empty disk (RAW) counts as all free.
Fake 'Get-ForgeProbeDisks' { $d = Disks-Typical; $d.Disks[1].FreeExtentBytes = 0; $d.Disks[1].PartitionStyle = 'RAW'; $d.Volumes = @($d.Volumes[0], $d.Volumes[3]); $d }
$run = Run
Eq 'empty RAW disk: OK' 'ok' (St $run 'free_space')
Eq 'empty RAW disk: boot mode still OK' 'ok' (St $run 'boot_mode')
# Free space on the Windows disk: OK, just inform.
Fake 'Get-ForgeProbeDisks' { $d = Disks-Typical; $d.Disks[0].FreeExtentBytes = 250 * $GB; $d.Disks[1].FreeExtentBytes = 0; $d }
$run = Run
Eq 'free space on windows disk: still OK' 'ok' (St $run 'windows_disk')
Has 'same disk: says same disk' 'same disk' (Find $run 'windows_disk').Detail
Fake 'Get-ForgeProbeDisks' { @{ Error = 'Access denied'; SystemDrive = 'C'; Disks = @(); Volumes = @() } }
$run = Run
Eq 'disks unreadable: space not sure' 'unsure' (St $run 'free_space')
Eq 'disks unreadable: shared not sure' 'unsure' (St $run 'shared_drive')
Eq 'disks unreadable: key not sure' 'unsure' (St $run 'key_drive')

# Boot mode.
Set-Typical
Fake 'Get-ForgeProbeFirmware' { @{ Error = $null; FirmwareType = 'BIOS' } }
Eq 'legacy BIOS: look' 'look' (St (Run) 'boot_mode')
Fake 'Get-ForgeProbeFirmware' { @{ Error = 'x'; FirmwareType = $null } }
Eq 'firmware unknown: not sure' 'unsure' (St (Run) 'boot_mode')
Set-Typical
Fake 'Get-ForgeProbeDisks' { $d = Disks-Typical; $d.Disks[0].PartitionStyle = 'MBR'; $d }
Eq 'windows disk MBR: look' 'look' (St (Run) 'boot_mode')

# Secure Boot.
Set-Typical
Fake 'Get-ForgeProbeSecureBoot' { param($IsAdmin) @{ Error = 'denied'; Enabled = $null; Supported = $null; Source = 'none' } }
$run = Run
Eq 'secure boot unknown: not sure' 'unsure' (St $run 'secure_boot')
Has 'secure boot unknown: admin hint' 'admin' (Find $run 'secure_boot').Todo
Fake 'Get-ForgeProbeSecureBoot' { param($IsAdmin) @{ Error = $null; Enabled = $false; Supported = $true; Source = 'Confirm-SecureBootUEFI' } }
Eq 'secure boot off: OK (inform)' 'ok' (St (Run) 'secure_boot')
Fake 'Get-ForgeProbeSecureBoot' { param($IsAdmin) @{ Error = $null; Enabled = $null; Supported = $false; Source = 'Confirm-SecureBootUEFI' } }
Eq 'secure boot unsupported: look' 'look' (St (Run) 'secure_boot')
Fake 'Get-ForgeProbeAdmin' { $true }
Fake 'Get-ForgeProbeSecureBoot' { param($IsAdmin) @{ Error = $null; Enabled = $true; Supported = $true; Source = 'Confirm-SecureBootUEFI' } }
$run = Run
Check 'admin is reported' $run.IsAdmin
Has 'secure boot on: MOK hint' 'MOK' (Find $run 'secure_boot').Todo

# GPU and image choice.
function Gpu($name, $ven) { Fake 'Get-ForgeProbeGpu' ([scriptblock]::Create("@{ Error = `$null; Adapters = @(@{ Name = '$name'; VendorId = '$ven'; PnpClass = 'PCI' }) }")); Run }
Set-Typical
$r = Gpu 'NVIDIA GeForce RTX 3060' '10DE'; Eq 'RTX 3060 ok' 'ok' (St $r 'gpu'); Eq 'RTX 3060 image' 'forge-os' $r.Image
$r = Gpu 'NVIDIA GeForce GTX 1660 SUPER' '10DE'; Eq 'GTX 1660 ok' 'ok' (St $r 'gpu')
$r = Gpu 'NVIDIA GeForce RTX 5090' '10DE'; Eq 'RTX 5090 ok' 'ok' (St $r 'gpu')
$r = Gpu 'NVIDIA GeForce RTX 5070 Laptop GPU' '10DE'; Eq 'RTX 5070 laptop ok' 'ok' (St $r 'gpu')
$r = Gpu 'NVIDIA RTX 6000 Ada Generation' '10DE'; Eq 'RTX 6000 Ada ok' 'ok' (St $r 'gpu')
$r = Gpu 'NVIDIA GeForce RTX 6080' '10DE'; Eq 'unknown new card: not sure' 'unsure' (St $r 'gpu'); Eq 'new card still NVIDIA image' 'forge-os' $r.Image
$r = Gpu 'NVIDIA GeForce GTX 1080 Ti' '10DE'; Eq 'GTX 1080 too old: look' 'look' (St $r 'gpu'); Eq 'GTX 1080: universal image' 'forge-os-universal' $r.Image
$r = Gpu 'AMD Radeon RX 7800 XT' '1002'; Eq 'AMD ok' 'ok' (St $r 'gpu'); Eq 'AMD universal' 'forge-os-universal' $r.Image
$r = Gpu 'Intel(R) Arc(TM) A770 Graphics' '8086'; Eq 'Intel universal' 'forge-os-universal' $r.Image
$r = Gpu 'Microsoft Basic Display Adapter' '1414'; Eq 'basic display only: not sure' 'unsure' (St $r 'gpu')
Fake 'Get-ForgeProbeGpu' { @{ Error = $null; Adapters = @(@{ Name = 'Intel(R) UHD Graphics 770'; VendorId = '8086'; PnpClass = 'PCI' }, @{ Name = 'NVIDIA GeForce RTX 4060 Laptop GPU'; VendorId = '10DE'; PnpClass = 'PCI' }, @{ Name = 'Parsec Virtual Display Adapter'; VendorId = ''; PnpClass = 'ROOT' }) } }
$r = Run; Eq 'hybrid laptop: NVIDIA image' 'forge-os' $r.Image; Eq 'hybrid laptop ok' 'ok' (St $r 'gpu')
Fake 'Get-ForgeProbeGpu' { @{ Error = 'no CIM'; Adapters = @() } }
$r = Run; Eq 'gpu unknown: not sure' 'unsure' (St $r 'gpu'); Eq 'gpu unknown: no image' $null $r.Image

# RAM, CPU, virtualization.
Set-Typical
Fake 'Get-ForgeProbeSystem' { @{ Error = $null; RamBytes = 8 * $GB; CpuName = 'Intel Core i5'; Cores = 4; Threads = 8; Architecture = 9; VirtFirmware = $false; VmExtensions = $true; HypervisorPresent = $false } }
$r = Run
Eq '8 GB RAM: look' 'look' (St $r 'ram')
Eq 'virtualization off in firmware: look' 'look' (St $r 'virtualization')
Has 'virtualization off: BIOS hint' 'SVM' (Find $r 'virtualization').Todo
Eq 'x64 cpu ok' 'ok' (St $r 'cpu')
Fake 'Get-ForgeProbeSystem' { @{ Error = $null; RamBytes = 16 * $GB; CpuName = 'Snapdragon X Elite'; Cores = 12; Threads = 12; Architecture = 12; VirtFirmware = $null; VmExtensions = $null; HypervisorPresent = $false } }
$r = Run
Eq 'ARM cpu: look' 'look' (St $r 'cpu')
Eq 'virtualization unknown: not sure' 'unsure' (St $r 'virtualization')
Fake 'Get-ForgeProbeSystem' { @{ Error = $null; RamBytes = 32 * $GB; CpuName = 'Intel Core i7'; Cores = 8; Threads = 16; Architecture = 9; VirtFirmware = $true; VmExtensions = $true; HypervisorPresent = $false } }
Eq 'virtualization on: ok' 'ok' (St (Run) 'virtualization')

# Labeled drives.
function WithVolumes([scriptblock]$Edit) {
    Fake 'Get-ForgeProbeDisks' ([scriptblock]::Create("`$d = Disks-Typical; & { $Edit } `$d; `$d"))
    Run
}
Set-Typical
$r = WithVolumes { param($d) $d.Volumes[1].FileSystem = 'NTFS' }; Eq 'SHARED NTFS ok' 'ok' (St $r 'shared_drive')
$r = WithVolumes { param($d) $d.Volumes[1].FileSystem = 'FAT32' }; Eq 'SHARED FAT32: look' 'look' (St $r 'shared_drive')
$r = WithVolumes { param($d) $d.Volumes[1].FileSystem = 'ReFS' }; Eq 'SHARED ReFS: look' 'look' (St $r 'shared_drive')
$r = WithVolumes { param($d) $d.Volumes[1].Label = 'Games' }; Eq 'no SHARED: ok (optional)' 'ok' (St $r 'shared_drive'); Has 'no SHARED: optional' 'Optional' (Find $r 'shared_drive').Todo
$r = WithVolumes { param($d) $d.Volumes[0].Label = 'shared' }; Eq 'two SHARED: look' 'look' (St $r 'shared_drive')
$r = WithVolumes { param($d) $d.Volumes[3].FileSystem = 'exFAT' }; Eq 'FORGEKEY exFAT ok' 'ok' (St $r 'key_drive')
$r = WithVolumes { param($d) $d.Volumes[3].FileSystem = 'NTFS' }; Eq 'FORGEKEY NTFS: look' 'look' (St $r 'key_drive'); Has 'FORGEKEY NTFS: FAT32' 'FAT32' (Find $r 'key_drive').Todo
$r = WithVolumes { param($d) $d.Volumes[3].DiskNumber = 1 }; Eq 'FORGEKEY not on USB: look' 'look' (St $r 'key_drive')
$r = WithVolumes { param($d) $d.Volumes[3].Label = 'KINGSTON' }; Eq 'no FORGEKEY: ok (optional)' 'ok' (St $r 'key_drive')
$r = WithVolumes { param($d) $d.Volumes[2].DiskNumber = 0 }; Eq 'backup on the Windows disk: look' 'look' (St $r 'backup_drive')
$r = WithVolumes { param($d) $d.Volumes[2].Label = 'Data' }; Eq 'no backup drive: ok (optional)' 'ok' (St $r 'backup_drive')

# Windows version unknown.
Set-Typical
Fake 'Get-ForgeProbeWindows' { @{ Error = 'x' } }
Eq 'windows unknown: not sure' 'unsure' (St (Run) 'windows_version')

# A check that crashes becomes "not sure", and the other checks still run.
Set-Typical
Fake 'Get-ForgeProbeSystem' { throw 'boom' }
$r = Run
Eq 'crash: ram not sure' 'unsure' (St $r 'ram')
Eq 'crash: gpu still ok' 'ok' (St $r 'gpu')

# manage-bde parsing (English output). Volume labels are personal: never kept.
$bde = @'
BitLocker Drive Encryption: Configuration Tool version 10.0.26100
Copyright (C) 2013 Microsoft Corporation. All rights reserved.

Disk volumes that can be protected with
BitLocker Drive Encryption:
Volume C: [Windows of Somchai]
[OS Volume]

    Size:                 931.00 GB
    BitLocker Version:    2.0
    Conversion Status:    Fully Encrypted
    Percentage Encrypted: 100.0%
    Encryption Method:    XTS-AES 128
    Protection Status:    Protection On
    Lock Status:          Unlocked

Volume S: [SHARED]
[Data Volume]

    Size:                 300.00 GB
    BitLocker Version:    None
    Conversion Status:    Fully Decrypted
    Percentage Encrypted: 0.0%
    Protection Status:    Protection Off
'@
$p = ConvertFrom-ManageBdeStatus -Text $bde
Eq 'manage-bde: two volumes' 2 @($p).Count
Eq 'manage-bde: C encrypted' $true (@($p)[0].Encrypted)
Eq 'manage-bde: C protection on' $true (@($p)[0].ProtectionOn)
Eq 'manage-bde: S not encrypted' $false (@($p)[1].Encrypted)
Eq 'manage-bde: mount point' 'S:' (@($p)[1].MountPoint)
Check 'manage-bde: label not kept' ((($p | Out-String) -notlike '*Somchai*'))
$p = ConvertFrom-ManageBdeStatus -Text "Volume C: [x]`n    Conversion Status:    Vollstaendig verschluesselt`n"
Eq 'manage-bde: other language gives unknown' $null (@($p)[0].Encrypted)
$p = ConvertFrom-ManageBdeStatus -Text 'ERROR: An attempt to access a required resource was denied.'
Eq 'manage-bde: denied gives no volumes' 0 @($p).Count

# Shared SHARED drive with BitLocker on: Forge OS may not open it.
Set-Typical
Fake 'Get-ForgeProbeBitLocker' { param($IsAdmin) @{ Source = 'manage-bde'; Error = $null; Volumes = @(@{ MountPoint = 'S:'; Encrypted = $true; ProtectionOn = $true }) } }
Eq 'SHARED with BitLocker: look' 'look' (St (Run) 'shared_drive')

# ---------------------------------------------------------------- hostile names from the PC
# Disk, GPU, CPU and Windows names come from drivers and firmware. Control and format characters
# (terminal escape codes, bell, bidi overrides, zero-width) must not reach the screen, JSON or report.
$esc = [string][char]27
$bad = $esc + '[2J' + $esc + ']0;owned' + [char]7 + [char]0x202E + [char]0x200B + [char]0x2028 + [char]0 + [char]::ConvertFromUtf32(0xE0049) + [char]::ConvertFromUtf32(0x1D173) + [char]0xD800
Set-Typical
Fake 'Get-ForgeProbeDisks' {
    $d = Disks-Typical
    $d.Disks[1].Model = 'WD Blue' + $bad + ' SN580'
    $d.Volumes[1].FileSystem = 'exFAT' + $bad
    $d
}
Fake 'Get-ForgeProbeGpu' { @{ Error = $null; Adapters = @(@{ Name = 'NVIDIA GeForce RTX' + $bad + ' 5070'; VendorId = '10DE'; PnpClass = 'PCI' }) } }
Fake 'Get-ForgeProbeSystem' { @{ Error = $null; RamBytes = 64 * $GB; CpuName = 'AMD Ryzen' + $bad + ' 7 7700'; Cores = 8; Threads = 16; Architecture = 9; VirtFirmware = $false; VmExtensions = $true; HypervisorPresent = $true } }
Fake 'Get-ForgeProbeWindows' { @{ Error = $null; Caption = 'Microsoft Windows 11' + $bad + ' Pro'; Version = '10.0.26100'; Build = '26100' + $bad; DisplayVersion = '24H2' + $bad; Ubr = 4349; Arch = '64-bit' } }
function Test-ForgeClean([string]$What, [string]$Text) {
    # \p{Cs}: any UTF-16 surrogate left over (tag characters, U+1D173, a lone surrogate).
    $hits = @([regex]::Matches($Text, '[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Cs}]') | Where-Object { $_.Value -ne "`n" -and $_.Value -ne "`r" })
    Eq "$What has no control or format characters" 0 $hits.Count
}
$hostileRun = Run
$hostileText = (Format-ForgeReport -Run $hostileRun) -join "`n"
Test-ForgeClean 'report with hostile names' $hostileText
Check 'cleaned GPU name still shown' ($hostileText.Contains('NVIDIA GeForce RTX[2J]0;owned 5070'))
Check 'cleaned CPU name still shown' ($hostileText.Contains('AMD Ryzen[2J]0;owned 7 7700'))
Fake 'Get-ForgeProbeGpu' { @{ Error = $null; Adapters = @(@{ Name = 'NVIDIA GeForce RTX' + [char]0x200B + ' 5070' + [char]0x202E; VendorId = '10DE'; PnpClass = 'PCI' }) } }
Eq 'a GPU name with hidden characters still matches RTX 50' 'ok' (St (Run) 'gpu')
$hostileHost = Invoke-ForgePrecheck 6>&1 | Out-String
Test-ForgeClean 'screen output with hostile names' $hostileHost
$hostileJson = Invoke-ForgePrecheck -Json:$true 6>&1 | Out-String
Check 'json with hostile names has no escaped control characters' ($hostileJson -notmatch '\\u00(0|1)[0-9a-fA-F]|\\u20(0b|2e|28)|\\u0007')
Test-ForgeClean 'json with hostile names' ($hostileJson -replace '\r?\n', ' ')
$o = $hostileJson | ConvertFrom-Json
Test-ForgeClean 'json strings after parsing' (($o.checks | ForEach-Object { @($_.detail) + $_.todo + $_.name }) -join ' ')
Eq 'clean text helper: null stays null' $null (ConvertTo-ForgeCleanText $null)
Eq 'clean text helper' 'ab' (ConvertTo-ForgeCleanText ('a' + $esc + [char]0x202E + 'b'))
# Review finding: the cleaning worked on UTF-16 code units, so characters outside the basic plane
# (two code units each) and lone surrogates got through. Now it works on real code points.
function Cp([int]$N) { [char]::ConvertFromUtf32($N) }
# Ordinal: -eq compares with the culture, which ignores tag characters and similar.
function Same([string]$What, [string]$Expected, [string]$Actual) {
    $shown = (($Actual.ToCharArray() | ForEach-Object { 'U+{0:X4}' -f [int]$_ }) -join ' ')
    Check "$What (expected '$Expected', got $shown)" ([string]::Equals($Expected, $Actual, [System.StringComparison]::Ordinal))
}
Same 'clean text: tag characters (hidden text) dropped' 'RTX 5090' (ConvertTo-ForgeCleanText ('RTX 5090' + (Cp 0xE0001) + (Cp 0xE0049) + (Cp 0xE0067) + (Cp 0xE007F)))
Same 'clean text: format characters outside the BMP dropped' 'xy' (ConvertTo-ForgeCleanText ('x' + (Cp 0x1D173) + (Cp 0x1D17A) + 'y'))
Same 'clean text: unassigned characters outside the BMP dropped' 'xy' (ConvertTo-ForgeCleanText ('x' + (Cp 0xE0080) + 'y'))
Same 'clean text: lone surrogates dropped' 'xyz' (ConvertTo-ForgeCleanText ('x' + [char]0xD800 + 'y' + [char]0xDC00 + 'z'))
Same 'clean text: a reversed pair is two lone surrogates' 'xy' (ConvertTo-ForgeCleanText ('x' + [char]0xDC00 + [char]0xD800 + 'y'))
Same 'clean text: a high surrogate at the end dropped' 'x' (ConvertTo-ForgeCleanText ('x' + [char]0xD83D))
$keep = 'GPU ' + (Cp 0x1F600) + ' ' + (Cp 0x20000) + ' ' + [char]0xE9
Check 'clean text: real characters outside the BMP are kept' ([string]::Equals($keep, (ConvertTo-ForgeCleanText $keep), [System.StringComparison]::Ordinal))
Same 'clean text: plain text unchanged' 'NVIDIA GeForce RTX 5070' (ConvertTo-ForgeCleanText 'NVIDIA GeForce RTX 5070')
# Review finding: pscustomobject properties were not cleaned.
$pc = ConvertTo-ForgeCleanData ([pscustomobject]@{ s = "z$esc[2J"; n = 5; inner = [pscustomobject]@{ t = 'a' + [char]0x202E + (Cp 0xE0041) + 'b' }; list = @("q$esc[1m"); h = @{ u = 'c' + [char]7 } })
Same 'clean data: pscustomobject property cleaned' 'z[2J' $pc.s
Same 'clean data: nested pscustomobject cleaned' 'ab' $pc.inner.t
Eq 'clean data: numbers kept' 5 $pc.n
Same 'clean data: list inside a pscustomobject cleaned' 'q[1m' @($pc.list)[0]
Same 'clean data: table inside a pscustomobject cleaned' 'c' $pc.h.u
Check 'clean data: still a pscustomobject' ($pc -is [System.Management.Automation.PSCustomObject])
Eq 'clean data: same property order' 's,n,inner,list,h' (@($pc.PSObject.Properties | ForEach-Object { $_.Name }) -join ',')

# ---------------------------------------------------------------- the whole program
Set-Typical
$out = Invoke-ForgePrecheck -Json:$true 6>&1 | Out-String
$o = $out | ConvertFrom-Json
Eq 'main -Json: only JSON on output' 'forge-precheck' $o.tool
$before = @(Get-ChildItem -LiteralPath $script:FakeDesktop).Count
$null = Invoke-ForgePrecheck -SaveReport:$true 6>&1 | Out-String
$files = @(Get-ChildItem -LiteralPath $script:FakeDesktop -Filter 'forge-precheck-*.txt')
Eq 'save report: one new file' ($before + 1) $files.Count
$saved = Get-Content -LiteralPath $files[0].FullName -Raw
Has 'saved report has checks' 'Fast Startup' $saved
Check 'saved report has no personal labels' ($saved -notlike '*Somchai*')
Check 'saved report name uses a western year' ($files[0].Name -match '^forge-precheck-20\d{6}-\d{6}')
# Thai Buddhist calendar must not change the file name or the dates.
$old = [System.Threading.Thread]::CurrentThread.CurrentCulture
try {
    [System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::new('th-TH')
    $raw = Invoke-ForgePrecheck -Json:$true 6>&1 | Out-String
    Check 'json time uses a western year in th-TH' ($raw -match '"generated_utc":\s*"20\d\d-')
    Check 'report time uses a western year in th-TH' (((Format-ForgeReport -Run (Run)) -join ' ') -match 'made 20\d\d-')
}
finally { [System.Threading.Thread]::CurrentThread.CurrentCulture = $old }

# Real probes on a non-Windows box: nothing crashes, everything unknown becomes "not sure".
if (-not $IsWindows) {
    . $ScriptPath
    Fake 'Get-ForgeProbeDesktop' { $script:FakeDesktop }
    $real = Invoke-ForgeChecks
    Check 'real probes on Linux: every check returns' ($real.Results.Count -ge 15)
    Eq 'real probes on Linux: bitlocker not sure' 'unsure' (St $real 'bitlocker')
    Eq 'real probes on Linux: gpu not sure' 'unsure' (St $real 'gpu')
}

[System.IO.Directory]::Delete($script:FakeDesktop, $true)

Write-Host "$($script:Count) checks, $($script:Failures.Count) failed"
if ($script:Failures.Count -gt 0) { exit 1 }
Write-Host 'ALL PASSED'
exit 0
