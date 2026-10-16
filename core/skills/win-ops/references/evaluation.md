# Evaluation rules (win-ops)

Read this after collection, before writing the report.

## Health score

`100 − P0×25 − P1×10 − P2×3` (floor 0). Verdicts (in Chinese in the report): ≥85 优秀 / 70-84 良好 / 50-69 需维护 / <50 需立即处理. A heuristic, not a benchmark.

## Threshold table

| Category | Check | Condition | Level | Suggestion |
|---|---|---|---|---|
| Disk | System drive FreePct | <10% | P1 | Clean now (🟢 items first) |
| Disk | Any fixed drive FreePct | <15% | P2 | Clean large directories |
| Disk | 🟢 items total | >2GB | P2 | Suggest cleanup with an itemized list |
| Disk | PendingReboot | ≠none | P2 | Reboot when convenient |
| Disk | Windows.old / update residue | exists | P2 | Clean after confirmation |
| Startup | Total startup items (STARTUP+RUN+TASK) | >15 | P2 | Disable non-essential ones |
| Startup | Unknown-origin items (suspicious path / no publisher) | present | P2 | Identify the source first |
| Software | Known bloat Appx | hit | P2 | List them; the user decides |
| Software | Functional duplicates (two AVs / two archivers / duplicate editor installs) | present | P1 | Keep one |
| Drivers | Problem devices DRV_ERR_COUNT | >0 | P1 | Reinstall the corresponding driver |
| Services | Auto+Stopped, non-Delayed, non-critical | >0 | P2 | Investigate or set to Manual |
| Network | PING_223 / PING_BAIDU | Timeout | P1 | Network diagnosis (NIC first, then DNS) |
| Network | Proxy enabled without the user's knowledge | Enable=1 | P2 | Verify the proxy's origin |
| Network | Speedtest DOWNLOAD_CN | <30 Mbps (retest once to confirm) | P2 | Check NIC link rate / Wi-Fi / broadband |
| Network | Speedtest PING_CN | avg >80ms or lost>0 | P2 | NIC → DNS → ISP; cross-read batch-4 PING_223 |
| Security | Defender RT=False and no other AV in charge | yes | P0 | Restore protection immediately. **SecurityCenter2 misses Chinese AVs** (Huorong/360 often don't register) — cross-check batch 3/4 startup items and services for Sysdiag (Huorong) / 360 / QQPCMgr traces; if found, protection exists and the report just notes "Defender handed over" — do NOT raise P0 |
| Security | AV signature age SigAgeDays | >7 (only while Defender is in charge) | P1 | Update signatures and run a full scan |
| Security | Any firewall profile off | Enabled=False | P1 | Enable it |
| Security | UAC EnableLUA | =0 | P1 | Restore UAC |
| Security | SMB1 | =1 (empty = disabled by default) | P1 | Disable (LanmanServer Parameters) |
| Security | RDP open (fDenyTSConnections=0) | user unaware | P2 | Verify and close |
| Performance | Free memory | <15% (instantaneous, indicative only) | P2 | Identify hogs / add RAM |
| Performance | Top-10 process memory sum | >60% of total | P2 | Report the hogs |
| Performance | UptimeDays | >14 | P2 | Reboot when convenient |
| Updates | Age of newest hotfix | >90 days | P1 | Check Windows Update |
| Updates | wuauserv | Disabled | P1 | Restore auto-update; Manual+Stopped → P2 (check the latest patch date for a possible update-blocker tool) |

Classify out-of-table anomalies by nearest match, and state the reasoning in the report.
