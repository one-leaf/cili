# Repair phase (win-ops — entered only on explicit request)

Read this before any repair. Repair is a separate turn: only when the user says "clean it up" or "run suggestions 2 and 5". If the user changes their mind mid-way, stop immediately.

## Authorization tiers

- 🟢: show the operation list → one confirmation → execute
- 🟡: confirm **item by item**; explain impact, reversibility, and reboot requirements; any item the user didn't nod through is skipped
- 🔴: refuse and explain; may provide manual instructions
- The pwsh tool prompts user approval for `Remove-Item -Recurse -Force` etc. — expected behavior; explain it to the user, never bypass it

## Repair command reference (pick per suggestion)

| Item | Tier | Command notes |
|---|---|---|
| User/system Temp | 🟢 | `Get-ChildItem $env:TEMP -Force -EA SilentlyContinue \| Remove-Item -Recurse -Force -EA SilentlyContinue` (skips locked files; **exclude `*\.cili\*`** — deleting it loses this turn's command output) |
| WU download cache | 🟢 admin | stop `wuauserv` + `bits` → clear `SoftwareDistribution\Download` → restart the services |
| DNS cache | 🟢 | `Clear-DnsClientCache` |
| Recycle Bin | 🟡 | `Clear-RecycleBin -Force` (warn: may contain personal files) |
| Browser caches | 🟡 | close the browser first; delete only each profile's `Cache`/`Code Cache`/`GPUCache`; **never touch Cookies/Login Data** |
| Startup items | 🟡 | prefer setting the disabled byte in `StartupApproved` (export original values first); deleting Run values is irreversible — only on the user's explicit naming |
| Scheduled tasks | 🟡 | `Disable-ScheduledTask` (disable beats delete) |
| Services | 🟡 admin | `Stop-Service` + `Set-Service -StartupType Manual/Disabled`; **exclude** wuauserv, WinDefend, AVs, licensing services |
| Appx bloat | 🟡 | `Get-AppxPackage <name> \| Remove-AppxPackage`; keep DesktopAppInstaller (winget) / WindowsStore / VCLibs / IME packages |
| Desktop app uninstall | 🟡 | ① `winget uninstall --id <id> --silent` (clear the 4 proxy vars `http_proxy/https_proxy/HTTP_PROXY/HTTPS_PROXY` first, or winget crashes) ② ARP QuietUninstallString ③ `msiexec /x <GUID> /qn /norestart /L*v <log>` |
| Disable hibernation | 🟡 admin | `powercfg /h off` (frees 3-8GB, disables fast startup, reversible) |
| WinSxS | 🟡 admin | `AnalyzeComponentStore` first, read "备份和已禁用的功能" → `/StartComponentCleanup`; `/ResetBase` is irreversible (installed updates can no longer be uninstalled) — separate confirmation |
| Drivers | advise only | Device Manager / vendor tools / winget — never run automatically |
| Network stack reset | 🟡 admin | `netsh winsock reset` (needs a reboot); only on the user's explicit request |

For deletions, prefer the Recycle Bin or a quarantine dir (`$env:TEMP\_winops_quarantine_<timestamp>\` + a manifest); Temp-class content may be deleted directly. **"Freed X GB" only counts measured deltas** (`Get-Volume` before and after) — space is truly freed only after the Recycle Bin is emptied; state this in the delivery notes.

## Elevation (admin-required repairs)

The elevated process's stdout is unreachable — write results to disk and read them back:

```powershell
# 1) write the commands into a temp ps1 (the script Out-Files its results to $log)
# 2) launch elevated — tell the user IMMEDIATELY to click "Yes" on UAC; ~120s without confirmation = treated as refusal
Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$tmp`""
# 3) read the log back
Get-Content $log
```

The `Start-Process powershell` call trips the pwsh tool's re-invocation approval (ask tier) — expected behavior, the user approves and it proceeds. Never route around it via other launch paths.

## Re-measure

After each repair batch, re-run the matching collection block (references/collection.md), then push the Markdown report again with a "修复结果" (repair results) section added — each repaired item shows before → after. Skipped items (locked / access denied) are recorded honestly — no sugar-coating.
