# Full set (win-ops, optional — not run by default)

Run only when the user asks for depth or the fast report flags "dig deeper"; state each item's cost:

1. **Online update search** (1-3 min) — the authoritative source of pending patches:
```powershell
$ErrorActionPreference = 'Stop'
try {
  $s = New-Object -ComObject Microsoft.Update.Session
  $r = $s.CreateUpdateSearcher().Search("IsInstalled=0 and IsHidden=0")
  "PENDING_UPDATES=$($r.Updates.Count)"
  $r.Updates | ForEach-Object { "PENDING | $($_.Title) | $([math]::Round($_.MaxDownloadSize/1MB,0))MB" }
} catch { "PENDING_UPDATES=error: $($_.Exception.Message)" }
```
2. **DISM component store analysis** (admin required, 30s-2min) — the **only trustworthy source** for the WinSxS reclaimable ceiling:
```powershell
dism /online /cleanup-image /analyzecomponentstore
```
Parse "Component Store Cleanup Recommended: Yes" (or 建议清理). The reclaimable amount is ONLY "Backups and Disabled Features / 备份和已禁用的功能"; hard links "shared with Windows" are NOT reclaimable.
3. **Full-disk largest files Top 20** (5-15 min, `run_in_background=true`):
```powershell
Get-ChildItem C:\ -Recurse -File -Force -ErrorAction SilentlyContinue |
  Sort-Object Length -Descending | Select-Object -First 20 |
  ForEach-Object { "BIGFILE | $([math]::Round($_.Length/1GB,2))GB | $($_.FullName)" }
```
Caution: on PS 5.1 `-Recurse` follows junctions and hard links double-count — label results "logical size (estimated)".
