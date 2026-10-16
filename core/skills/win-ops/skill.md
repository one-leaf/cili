---
name: Windows 优化
description: Windows local daily health check: read-only collection + threshold evaluation across 10 categories (system info, disk cleanup assessment, startup items, installed-software bloat, drivers, services, network, security, performance, updates), producing a Markdown report in Chinese; tiered repairs run only after user confirmation. Use when the user asks for 优化、体检、系统体检、健康检查、网络测速、系统信息; complains in everyday terms (电脑太卡了/电脑变慢/反应慢/卡顿/开机慢/开机太慢/后台太多/内存不足/风扇狂转/网速慢/下载慢); reports disk pressure (C盘满了/磁盘满了/磁盘空间不足) or asks for cleanup (清理电脑/清理缓存/清理垃圾/系统垃圾/电脑提速/优化系统); or asks about 启动项、垃圾评估、驱动检查、服务管理、网络诊断、测速、网速测试、宽带测速、安全审计、性能优化、更新管理。
roles: [master, worker]
---

# Windows Health Check (win-ops)

Run a **read-only health check** on the current Windows machine and produce a Markdown report. Enter the repair phase only after the user has seen the report and explicitly asks for it.

Workflow: **preflight → batched collection (read-only) → threshold evaluation → report → (optional) approved repair → re-measure**.

## Phase map

Read the sub-file for the current phase:

## Non-negotiable rules

1. **Zero mutation during collection**: every probe is read-only (`Get-CimInstance`, registry reads, directory sizing). Even obvious junk is only recorded — report first.
2. **Three-tier classification**, with different repair authorization:
   - 🟢 **Safe**: purely regenerable caches (Temp, update cache, crash dumps, DNS cache) — list the items, execute after one confirmation
   - 🟡 **Confirm**: side effects or costly rebuilds (browser caches, Recycle Bin, startup items, services, Appx/app uninstall, WinSxS) — confirm item by item, explain impact and reversibility
   - 🔴 **Forbidden**: never execute, report only — `C:\Windows` system dirs, `Program Files`, drivers, Defender/third-party AV, user data (Documents/Downloads/Desktop/chat history), pagefile/hiberfil, other users' profiles
3. **Report before repair.** Repair is a separate turn: only when the user says "clean it up" or "run suggestions 2 and 5". If the user changes their mind mid-way, stop immediately.
4. **Every pwsh call is a fresh process** (stateless): each code block in the reference files must run whole in a single call — never split and reassemble across calls.
5. Probe output keys are always English (values may be Chinese); the report body is in Chinese.
6. Long tasks (>2 min) use the pwsh tool's `run_in_background=true`; timeout cap is 600s.

## Phase map

| Phase | Sub-file | When to read |
|---|---|---|
| Collection (fast set: batches 1-5) | `references/collection.md` | always, before the first probe |
| Full set (update search / DISM / full-disk scan) | `references/full-set.md` | only on request or "dig deeper" flags |
| Network speed test | `references/speedtest.md` | only on 测速/网速慢/下载慢 or network findings |
| Threshold evaluation | `references/evaluation.md` | always, before grading |
| Markdown report | `references/report.md` | always, before rendering |
| Repair + elevation + re-measure | `references/repair.md` | only on the repair turn, never preemptively |

If the user only asked about one category (e.g. "看看启动项"), run only the matching batch(es), output only that category as chat text, and skip the full report.

## Hard-won gotchas

| Gotcha | Solution |
|---|---|
| pwsh calls are fresh processes; no state carries over | self-contained blocks, executed whole in one call |
| PS 5.1 compatibility (the pwsh tool may fall back to 5.1) | no PS7-only syntax (ternary `? :`, ...); `Test-Connection -ComputerName` doesn't exist on PS7 — always ping via `System.Net.NetworkInformation.Ping` |
| Chinese output keys may garble | English keys (values may be Chinese); the pwsh tool already forces UTF-8 |
| PS 5.1 `-Recurse` follows junctions | fixed-path probing; check the `ReparsePoint` attribute before deleting |
| WinSxS logical size ≠ reclaimable | reclaimable is ONLY DISM AnalyzeComponentStore's "Backups and Disabled Features" |
| `$env:TEMP` and `LOCALAPPDATA\Temp` often point to the same dir | dedupe before reporting |
| EstimatedSize unit is KB | `/1024` to MB |
| Recycle Bin sizing misses other users' SIDs | label it a "lower bound" |
| Cleaning Temp may delete cili's own output files | exclude `*\.cili\*` |
| pwsh approval prompts | `Remove-Item -Recurse -Force` etc. triggering user approval is expected — explain it, don't bypass |
| winget crashes with duplicate proxy vars in different cases | clear the 4 proxy vars before winget |
| Slow probes block the turn | update search / DISM / full-disk scan via `run_in_background` |
| Speedtest numbers include the system proxy path | script prints `PROXY_INUSE`; if yes, cross-read batch-4 `PROXY Enable=` and note it in the report |
