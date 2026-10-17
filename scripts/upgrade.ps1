﻿# Cili Agent 升级工具（只下载升级包，重启服务时自动应用）

param(
    [string]$ProjectRoot = ""
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Cili Agent 升级工具" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ==================== Switch to project root ====================

if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
} else {
    $ProjectRoot = (Resolve-Path $ProjectRoot).Path
}
Set-Location $ProjectRoot
Write-Host "[INFO] 工作目录: $(Get-Location)" -ForegroundColor Gray
Write-Host ""

# ==================== Download upgrade zip ====================
# 升级包落到脚本目录（项目根），与 core/updater.py 的 get_upgrade_zip_path() 一致；
# 解压覆盖不在本脚本执行——下次启动时由 core/updater.py apply_pending_upgrade() 统一应用。

Write-Host "[DOWNLOAD] 正在下载最新升级包..." -ForegroundColor Cyan

$destZip = Join-Path $ProjectRoot "cili-update.zip"

# Download using BITS (has progress bar)
Import-Module BitsTransfer -ErrorAction SilentlyContinue

$urls = @(
    'https://github.com/one-leaf/cili/archive/refs/heads/main.zip',
    'https://ghfast.top/https://github.com/one-leaf/cili/archive/refs/heads/main.zip',
    'https://ghproxy.net/https://github.com/one-leaf/cili/archive/refs/heads/main.zip',
    'https://gh-proxy.com/https://github.com/one-leaf/cili/archive/refs/heads/main.zip'
)

$downloaded = $false
foreach ($url in $urls) {
    try {
        Write-Host "  Trying: $url" -ForegroundColor Cyan
        # Clean up any leftover from previous attempt
        if (Test-Path $destZip) { Remove-Item $destZip -Force }
        Start-BitsTransfer -Source $url -Destination $destZip -DisplayName 'Downloading' -ErrorAction Stop
        if ((Test-Path $destZip) -and ((Get-Item $destZip).Length -gt 1024)) {
            # Validate it's actually a zip file (PK header magic bytes)
            $bytes = [System.IO.File]::ReadAllBytes($destZip)[0..3]
            if ($bytes[0] -eq 0x50 -and $bytes[1] -eq 0x4B) {
                Write-Host "  OK! Size: $([math]::Round((Get-Item $destZip).Length/1KB)) KB" -ForegroundColor Green
                $downloaded = $true
                break
            } else {
                Write-Host "  Invalid file (not a zip)" -ForegroundColor Yellow
                Remove-Item $destZip -Force
            }
        }
    } catch {
        Write-Host "  Failed: $_" -ForegroundColor Yellow
        if (Test-Path $destZip) { Remove-Item $destZip -Force }
    }
}

if (-not $downloaded) {
    Write-Host "[ERROR] 下载失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "请检查网络连接或使用 VPN" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

# ==================== Done ====================

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  [OK] 升级包已下载！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "提示：" -ForegroundColor Cyan
Write-Host "  - 升级包位置: $destZip"
Write-Host "  - 重新启动服务（start.cmd / python main.py）后自动比对版本并解压覆盖"
Write-Host "  - 升级包版本不高于当前版本时，启动时会自动删除该升级包"
Write-Host ""
Read-Host "按回车键退出"
