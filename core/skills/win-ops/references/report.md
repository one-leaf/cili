# Report (win-ops)

Read this before rendering the report.

Render the report as **one Markdown message pushed straight to the conversation** — no LaTeX, no PDF, no file written to disk. The report itself is the deliverable; no extra summary message after it.

Markdown template (fill every `<...>` from collected data; the report body stays in Chinese):

```markdown
# Windows 系统体检报告

生成时间：YYYY-MM-DD HH:mm ｜ 主机：<HW> ｜ 系统：<OS>

**健康分：<n>/100（<档位>）**

## 总览

| 级别 | 数量 | 摘要 |
|---|---|---|
| **P0** | n | <摘要> |
| **P1** | n | <摘要> |
| **P2** | n | <摘要> |

## 系统信息
<!-- OS/型号/CPU/内存/开机时长/待重启/各盘容量 数据表 -->

## 磁盘与清理评估
<!-- 各盘剩余率表；清理项表（路径/体积/分级/说明），数据来源标注"实测" -->

## 启动项
## 已安装软件
## 驱动
## 服务
## 网络
<!-- NIC/IP/代理/监听/hosts；跑过测速时附 SPEEDTEST 行（下载CN/INTL、上传、延迟、公网IP与运营商、代理标记） -->
## 安全
## 性能
## 更新

## 建议清单

| # | 级别 | 问题 | 建议操作 | 命令/路径 | 风险与可逆性 |
|---|---|---|---|---|---|
<!-- 每条建议一行；级别用 **P0**/**P1**/**P2** -->

## 附录

- 本次为快速集；完整集（更新在线搜索/DISM/全盘大文件）未跑
- 非管理员降级项：…
```

Markdown rules:

- **Escape pipe characters inside table cells**: probe output uses ` | ` as field separator and shell commands contain `|` — write `\|` in cells
- Severity uses the **P0/P1/P2** labels; the 🟢🟡🔴 cleanup tiers stay as 安全 / 需确认 / 禁区 (or keep the emoji — Markdown has no PDF font constraint)
- Each category section = a data table + a findings paragraph referencing threshold-table entries. **Measured values only; estimates must be labeled 估算**. WinSxS: logical size and reclaimable ceiling are two separate numbers, never merged. A clean category still gets its data table — the measured state is the evidence; drop a section only when its batch didn't run
- Only write the report to disk (`workspace/系统体检报告-YYYY-MM-DD.md`) when the user explicitly asks for a copy — never unprompted

If the user only asked about one category (e.g. "看看启动项"), run only the matching batch(es), output only that category as chat text, and skip the full report.
