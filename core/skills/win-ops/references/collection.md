# Collection batches (win-ops)

Read this before collecting. Run everything through the `pwsh` tool (**never** invoke powershell from bash — Git Bash rewrites `/xxx` flags into local paths). One pwsh call per block; batches 1/3 can be sent in parallel. Fast set budget: ~1-2 minutes total.

Output line protocol: `KEY=value` or `TAG | field | ...`; parse into report tables after collection.

## Batch 0: preflight (inside batch 1)

The admin check is in batch 1 (`IsAdmin=`). Without admin, keep going and mark admin-dependent items (DISM, service ops, BitLocker, ...) as "requires admin" in the report appendix.

## Batch 1: system info + disks + pending reboot

```powershell
$ErrorActionPreference = 'SilentlyContinue'
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
"OS=$($os.Caption.Trim()) Version=$($os.Version) Build=$($os.BuildNumber) Arch=$($os.OSArchitecture)"
"HW=$($cs.Manufacturer) $($cs.Model)"
"CPU=$($cpu.Name.Trim()) LogicalCores=$($cpu.NumberOfLogicalProcessors) LoadNow=$($cpu.LoadPercentage)"
"RAM_TotalGB=$([math]::Round($cs.TotalPhysicalMemory/1GB,1)) RAM_FreeGB=$([math]::Round($os.FreePhysicalMemory*1KB/1GB,1))"
$up = (Get-Date) - $os.LastBootUpTime
"BootTime=$($os.LastBootUpTime.ToString('yyyy-MM-dd HH:mm')) UptimeDays=$([math]::Round($up.TotalDays,1))"
"InstalledOn=$($os.InstallDate.ToString('yyyy-MM-dd'))"
$pr = @()
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $pr += 'CBS' }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') { $pr += 'WU' }
$pfro = (Get-Item 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager').GetValue('PendingFileRenameOperations')
if ($pfro) { $pr += 'FileRename' }
"PendingReboot=$(if ($pr.Count) { $pr -join ',' } else { 'none' })"
"IsAdmin=$(([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
Get-Volume | Where-Object { $_.DriveLetter -and $_.DriveType -eq 'Fixed' } | Sort-Object DriveLetter | ForEach-Object {
  "VOL $($_.DriveLetter): Label=$($_.FileSystemLabel) TotalGB=$([math]::Round($_.Size/1GB,1)) FreeGB=$([math]::Round($_.SizeRemaining/1GB,1)) FreePct=$([math]::Round(100*$_.SizeRemaining/$_.Size,1))"
}
```

## Batch 2: cleanup candidates (sizes only, no deletion)

```powershell
$ErrorActionPreference = 'SilentlyContinue'
function Get-DirMB([string]$p) {
  if (-not (Test-Path -LiteralPath $p)) { return -1 }
  $sum = 0
  Get-ChildItem -LiteralPath $p -Recurse -Force -File -ErrorAction SilentlyContinue | ForEach-Object { $sum += $_.Length }
  [math]::Round($sum/1MB, 0)
}
$targets = @(
  @('UserTemp', $env:TEMP, 'G'),
  @('SysTemp', "$env:SystemRoot\Temp", 'G'),
  @('WUDownloadCache', "$env:SystemRoot\SoftwareDistribution\Download", 'G'),
  @('Prefetch', "$env:SystemRoot\Prefetch", 'G'),
  @('CrashDumps', "$env:LOCALAPPDATA\CrashDumps", 'G'),
  @('WER', "$env:LOCALAPPDATA\Microsoft\Windows\WER", 'G'),
  @('DeliveryOptimizationCache', 'C:\Windows\ServiceProfiles\NetworkService\AppData\Local\Microsoft\DeliveryOptimization\Cache', 'G'),
  @('PipCache', "$env:LOCALAPPDATA\pip\cache", 'G'),
  @('NpmCache', "$env:LOCALAPPDATA\npm-cache", 'G'),
  @('UvCache', "$env:USERPROFILE\.cache\uv", 'G'),
  @('RecycleBin_C', 'C:\$Recycle.Bin', 'Y'),
  @('WhisperModels', "$env:USERPROFILE\.cache\whisper", 'R'),
  @('HuggingFaceModels', "$env:USERPROFILE\.cache\huggingface\hub", 'R'),
  @('OllamaModels', "$env:USERPROFILE\.ollama\models", 'R')
)
foreach ($t in $targets) {
  $mb = Get-DirMB $t[1]
  if ($mb -ge 0) { "CLEAN $($t[2]) | $($t[0]) | $($t[1]) | ${mb}MB" }
}
if (Test-Path 'C:\Windows.old') { "CLEAN Y | WindowsOld | C:\Windows.old" }
if (Test-Path 'C:\$WINDOWS.~BT') { "CLEAN Y | WindowsUpdateResidue | C:\`$WINDOWS.~BT" }
foreach ($f in @('C:\hiberfil.sys', 'C:\pagefile.sys', 'C:\swapfile.sys')) {
  $i = Get-Item $f -Force -ErrorAction SilentlyContinue
  if ($i) { "SYSFILE | $f | $([math]::Round($i.Length/1GB,2))GB" }
}
foreach ($browser in @('Microsoft\Edge', 'Google\Chrome')) {
  $ud = "$env:LOCALAPPDATA\$browser\User Data"
  if (Test-Path $ud) {
    Get-ChildItem $ud -Directory -ErrorAction SilentlyContinue | ForEach-Object {
      foreach ($sub in @('Cache', 'Code Cache', 'GPUCache')) {
        $mb = Get-DirMB (Join-Path $_.FullName $sub)
        if ($mb -gt 50) { "CLEAN Y | BrowserCache | $(Join-Path $_.FullName $sub) | ${mb}MB" }
      }
    }
  }
}
```

Interpretation: `CLEAN G` = pure regenerable cache; `CLEAN Y` = side effects, needs confirmation (Recycle Bin may hold personal files; the number is a lower bound due to permissions); `CLEAN R` = model data — report by rebuild cost (pip/npm: seconds; small models: minutes; GB-scale models: hours plus bandwidth), report only, never proactively suggest deletion. Identify AI data **by owning directory, never by extension** (extension scans have a 97%+ false-positive rate).

## Batch 3: startup items + installed software

```powershell
$ErrorActionPreference = 'SilentlyContinue'
"== StartupCommands"
Get-CimInstance Win32_StartupCommand | ForEach-Object { "STARTUP | $($_.Location) | $($_.Name) | $($_.Command)" }
"== Run keys"
$runKeys = @(
  'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
  'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce',
  'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run',
  'HKLM:\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Run'
)
foreach ($k in $runKeys) {
  $p = Get-Item $k -ErrorAction SilentlyContinue
  if ($p) { foreach ($v in $p.GetValueNames()) { "RUN | $k | $v = $($p.GetValue($v))" } }
}
"== StartupApproved (byte0 odd = disabled)"
foreach ($k in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run',
                 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run')) {
  $p = Get-Item $k -ErrorAction SilentlyContinue
  if ($p) { foreach ($v in $p.GetValueNames()) { "SA | $v | byte0=$($p.GetValue($v)[0])" } }
}
"== Non-Microsoft scheduled tasks"
Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskPath -notlike '\Microsoft\*' } | ForEach-Object {
  $tg = (($_.Triggers | ForEach-Object { $_.CimClass.CimClassName -replace 'MSFT_Task', '' -replace 'Trigger', '' }) -join '+')
  "TASK | $($_.State) | trigger=$tg | $($_.TaskPath)$($_.TaskName)"
}
"== Installed software (ARP, EstimatedSize unit=KB)"
$apps = Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
  'HKLM:\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
  'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
  Where-Object { $_.DisplayName }
"APP_COUNT=$(@($apps).Count) APP_TotalSizeMB=$([math]::Round((@($apps) | Measure-Object EstimatedSize -Sum).Sum/1024,0))"
@($apps) | Sort-Object EstimatedSize -Descending | Select-Object -First 15 | ForEach-Object {
  "APP | $($_.DisplayName) | v$($_.DisplayVersion) | $([math]::Round($_.EstimatedSize/1024,0))MB | $($_.InstallDate) | $($_.Publisher)"
}
"== Appx"
$appx = Get-AppxPackage -ErrorAction SilentlyContinue
if ($appx) {
  "APPX_COUNT=$(@($appx).Count)"
  $bloat = @('Microsoft.Copilot','Microsoft.BingNews','Microsoft.BingWeather','Microsoft.BingSearch',
    'Microsoft.GetHelp','Microsoft.Getstarted','Microsoft.MicrosoftSolitaireCollection','Microsoft.MicrosoftOfficeHub',
    'Microsoft.News','Microsoft.People','Microsoft.Todos','Microsoft.WindowsFeedbackHub','Microsoft.WindowsMaps',
    'Microsoft.YourPhone','Microsoft.ZuneMusic','Microsoft.ZuneVideo','Clipchamp.Clipchamp','Microsoft.549981C3F5F10',
    'Microsoft.MicrosoftStickyNotes','Microsoft.WindowsSoundRecorder','Microsoft.WindowsAlarms')
  @($appx) | Where-Object { $n = $_.Name; @($bloat | Where-Object { $n -like $_ }).Count -gt 0 } |
    ForEach-Object { "APPX_BLOAT | $($_.Name)" }
} else { "APPX_COUNT=unavailable_in_this_shell" }
```

## Batch 4: drivers + services + network

```powershell
$ErrorActionPreference = 'SilentlyContinue'
"== Problem devices"
$bad = Get-CimInstance Win32_PnPEntity | Where-Object { $_.ConfigManagerErrorCode -ne 0 }
"DRV_ERR_COUNT=$(@($bad).Count)"
@($bad) | ForEach-Object { "DRV_ERR code=$($_.ConfigManagerErrorCode) | $($_.Name)" }
"== Auto services currently stopped"
$autoStopped = Get-CimInstance Win32_Service | Where-Object { $_.StartMode -eq 'Auto' -and $_.State -eq 'Stopped' }
"SVC_AUTOSTOP_COUNT=$(@($autoStopped).Count)"
@($autoStopped) | ForEach-Object { "SVC_AUTOSTOP | $($_.Name) | $($_.DisplayName) | Delayed=$($_.DelayedAutoStart)" }
"== Third-party services (not under C:\Windows)"
$tp = Get-CimInstance Win32_Service | Where-Object { $_.PathName -and $_.PathName -notmatch '(?i)^[a-z]:\\Windows\\' }
"SVC_3RD_COUNT=$(@($tp).Count)"
@($tp) | Sort-Object State | Select-Object -First 25 | ForEach-Object { "SVC_3RD | $($_.State)/$($_.StartMode) | $($_.Name) | $($_.PathName)" }
"== Network"
Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { "NIC | $($_.Name) | $($_.InterfaceDescription) | $($_.LinkSpeed)" }
Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | ForEach-Object {
  "IP | if=$($_.InterfaceAlias) IP=$($_.IPv4Address.IPAddress) GW=$($_.IPv4DefaultGateway.NextHop) DNS=$($_.DNSServer.ServerAddresses -join ',')"
}
$ping = New-Object System.Net.NetworkInformation.Ping
"PING_223=$($ping.Send('223.5.5.5', 2000).Status) PING_BAIDU=$($ping.Send('www.baidu.com', 2000).Status)"
$ipp = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
"PROXY Enable=$($ipp.ProxyEnable) Server=$($ipp.ProxyServer)"
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
  (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName
} | Group-Object | Sort-Object Count -Descending | Select-Object -First 12 | ForEach-Object { "LISTEN | $($_.Name) | ports=$($_.Count)" }
$hosts = Get-Content "$env:SystemRoot\System32\drivers\etc\hosts" -ErrorAction SilentlyContinue | Where-Object { $_ -match '\S' -and $_ -notmatch '^\s*#' }
"HOSTS_ACTIVE_LINES=$(@($hosts).Count)"
@($hosts) | Select-Object -First 10 | ForEach-Object { "HOSTS | $_" }
```

## Batch 5: security + performance + updates

```powershell
$ErrorActionPreference = 'SilentlyContinue'
"== Defender"
$mp = $null
try { $mp = Get-MpComputerStatus -ErrorAction Stop } catch {}
if ($mp) {
  $sigAge = if ($mp.AntivirusSignatureLastUpdated) { ((Get-Date) - $mp.AntivirusSignatureLastUpdated).Days } else { -1 }
  "DEFENDER RT=$($mp.RealTimeProtectionEnabled) SigAgeDays=$sigAge LastQuickScan=$($mp.QuickScanEndTime)"
  if (-not $mp.RealTimeProtectionEnabled) {
    Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction SilentlyContinue |
      ForEach-Object { "AV | $($_.displayName) | state=$('0x{0:X}' -f $_.productState) | ts=$($_.timestamp)" }
  }
} else {
  Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction SilentlyContinue |
    ForEach-Object { "AV | $($_.displayName) | state=$('0x{0:X}' -f $_.productState) | ts=$($_.timestamp)" }
}
"== Firewall"
Get-NetFirewallProfile -ErrorAction SilentlyContinue | ForEach-Object { "FW | $($_.Name) Enabled=$($_.Enabled)" }
"== Hardening"
"UAC_EnableLUA=" + (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -ErrorAction SilentlyContinue).EnableLUA
"SMB1=" + (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\LanmanServer\Parameters' -Name SMB1 -ErrorAction SilentlyContinue).SMB1
"RDP_fDenyTSConnections=" + (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' -ErrorAction SilentlyContinue).fDenyTSConnections
try { Get-LocalUser -ErrorAction Stop | Where-Object Enabled | ForEach-Object { "USER | $($_.Name) | LastLogon=$($_.LastLogon)" } } catch {}
"== Performance"
Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 10 | ForEach-Object {
  "PROCMEM | $($_.ProcessName) pid=$($_.Id) memMB=$([math]::Round($_.WorkingSet64/1MB,0)) cpuSec=$([math]::Round($_.CPU,0))"
}
Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue | ForEach-Object {
  "PAGEFILE | $($_.Name) AllocatedMB=$($_.AllocatedBaseSize) UsedMB=$($_.CurrentUsage)"
}
$pp = (powercfg /getactivescheme) -join ' '
if ($pp -match '([0-9a-fA-F-]{36})') { "POWERPLAN_GUID=$($Matches[1])" }
"== Updates"
Get-HotFix -ErrorAction SilentlyContinue | Sort-Object InstalledOn -Descending | Select-Object -First 3 |
  ForEach-Object { "HOTFIX | $($_.HotFixID) | $($_.Description) | $($_.InstalledOn)" }
$wu = Get-Service wuauserv -ErrorAction SilentlyContinue
if ($wu) { "WU_Service=$($wu.Status)/$($wu.StartType)" }
$au = Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' -ErrorAction SilentlyContinue
if ($au) { "WU_Policy NoAutoUpdate=$($au.NoAutoUpdate) AUOptions=$($au.AUOptions)" } else { "WU_Policy=default" }
```

Power plan GUID mapping: `381b4222-…` = Balanced, `8c5e7fda-…` = High performance, `a1841308-…` = Power saver.

AV productState decoding: bit `0x1000` = real-time protection on, bit `0x10` = signatures up to date (so `0x61100` = enabled and updated). When unsure, list the raw value and let the report combine it with `DEFENDER RT=` — with a third-party AV installed (Huorong/360/...), Defender RT=False is a normal handover, not a P0. Note that Huorong/360 may **not register** with SecurityCenter2 (on the reference machine only Windows Defender was listed): to judge "is anything protecting this machine", cross-check batch 3/4 startup items and services for Sysdiag (Huorong), HipsDaemon, 360, QQPCMgr traces.
