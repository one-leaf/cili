# Cili Agent 升级工具

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Cili Agent 升级工具" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ==================== Switch to project root ====================

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot
Write-Host "[INFO] 工作目录: $(Get-Location)" -ForegroundColor Gray
Write-Host ""

# ==================== Download latest code ====================

Write-Host "[DOWNLOAD] 正在下载最新代码..." -ForegroundColor Cyan

$tempZip = Join-Path $env:TEMP "cili-main.zip"
$tempExtract = Join-Path $env:TEMP "cili-main-extract"

# Clean temp files
if (Test-Path $tempZip) { Remove-Item $tempZip -Force }
if (Test-Path $tempExtract) { Remove-Item $tempExtract -Recurse -Force }

# Download using BITS (has progress bar)
Import-Module BitsTransfer -ErrorAction SilentlyContinue

$urls = @(
    'https://github.com/one-leaf/cili/archive/refs/heads/main.zip',
    'https://ghproxy.net/https://github.com/one-leaf/cili/archive/refs/heads/main.zip',
    'https://ghfast.top/https://github.com/one-leaf/cili/archive/refs/heads/main.zip',
    'https://gh-proxy.com/https://github.com/one-leaf/cili/archive/refs/heads/main.zip'
)

$downloaded = $false
foreach ($url in $urls) {
    try {
        Write-Host "  Trying: $url" -ForegroundColor Cyan
        Start-BitsTransfer -Source $url -Destination $tempZip -DisplayName 'Downloading' -ErrorAction Stop
        if ((Test-Path $tempZip) -and ((Get-Item $tempZip).Length -gt 0)) {
            Write-Host "  OK! Size: $([math]::Round((Get-Item $tempZip).Length/1KB)) KB" -ForegroundColor Green
            $downloaded = $true
            break
        }
    } catch {
        Write-Host "  Failed: $_" -ForegroundColor Yellow
        if (Test-Path $tempZip) { Remove-Item $tempZip -Force }
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

# ==================== Extract ====================

Write-Host "[EXTRACT] 正在解压..." -ForegroundColor Cyan

try {
    Expand-Archive -Path $tempZip -DestinationPath $tempExtract -Force
} catch {
    Write-Host "[ERROR] 解压失败" -ForegroundColor Red
    Read-Host "按回车键退出"
    exit 1
}

# Find extracted folder
$extractedDir = Get-ChildItem -Path $tempExtract -Directory -Filter "cili-main*" | Select-Object -First 1

if (-not $extractedDir) {
    Write-Host "[ERROR] 未找到解压目录" -ForegroundColor Red
    Read-Host "按回车键退出"
    exit 1
}

Write-Host "[OK] 已解压到 $($extractedDir.FullName)" -ForegroundColor Green

# ==================== Copy files ====================

Write-Host "[UPDATE] 正在更新文件..." -ForegroundColor Cyan

# Copy files, excluding data/, workspace/, .git/, scripts/ (copy scripts/ last)
$excludeDirs = @('data', 'workspace', '.git', 'scripts')
Get-ChildItem -Path $extractedDir.FullName | Where-Object { $_.Name -notin $excludeDirs } | ForEach-Object {
    $target = Join-Path $projectRoot $_.Name
    if ($_.PSIsContainer) {
        Copy-Item $_.FullName $target -Recurse -Force
    } else {
        Copy-Item $_.FullName $target -Force
    }
}

# Copy scripts/ last (after all other files) to avoid overwriting running script
Copy-Item -Path (Join-Path $extractedDir.FullName 'scripts') -Destination (Join-Path $projectRoot 'scripts') -Recurse -Force

# ==================== Cleanup ====================

if (Test-Path $tempZip) { Remove-Item $tempZip -Force }
if (Test-Path $tempExtract) { Remove-Item $tempExtract -Recurse -Force }

# ==================== Done ====================

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  [OK] 升级完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "提示：" -ForegroundColor Cyan
Write-Host "  - 重新启动服务以应用更新"
Write-Host "  - data/ 和 workspace/ 已保留"
Write-Host ""
Read-Host "按回车键退出"
