# Cili Agent 升级工具

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
    'https://ghfast.top/https://github.com/one-leaf/cili/archive/refs/heads/main.zip',
    'https://ghproxy.net/https://github.com/one-leaf/cili/archive/refs/heads/main.zip',
    'https://gh-proxy.com/https://github.com/one-leaf/cili/archive/refs/heads/main.zip'
)

$downloaded = $false
foreach ($url in $urls) {
    try {
        Write-Host "  Trying: $url" -ForegroundColor Cyan
        # Clean up any leftover from previous attempt
        if (Test-Path $tempZip) { Remove-Item $tempZip -Force }
        Start-BitsTransfer -Source $url -Destination $tempZip -DisplayName 'Downloading' -ErrorAction Stop
        if ((Test-Path $tempZip) -and ((Get-Item $tempZip).Length -gt 1024)) {
            # Validate it's actually a zip file (PK header magic bytes)
            $bytes = [System.IO.File]::ReadAllBytes($tempZip)[0..3]
            if ($bytes[0] -eq 0x50 -and $bytes[1] -eq 0x4B) {
                Write-Host "  OK! Size: $([math]::Round((Get-Item $tempZip).Length/1KB)) KB" -ForegroundColor Green
                $downloaded = $true
                break
            } else {
                Write-Host "  Invalid file (not a zip)" -ForegroundColor Yellow
                Remove-Item $tempZip -Force
            }
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
$newCount = 0
$updateCount = 0
$skipCount = 0

function Copy-WithLog($srcFile, $ProjectRoot, $relativePath) {
    $target = Join-Path $ProjectRoot $relativePath
    $targetDir = Split-Path $target -Parent
    if (-not (Test-Path $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }

    $isNew = -not (Test-Path $target)
    $isChanged = $true
    if (-not $isNew) {
        $srcHash = (Get-FileHash $srcFile.FullName -Algorithm MD5).Hash
        $dstHash = (Get-FileHash $target -Algorithm MD5).Hash
        $isChanged = $srcHash -ne $dstHash
    }

    if ($isNew) {
        Copy-Item $srcFile.FullName $target -Force
        Write-Host "  [+] $relativePath" -ForegroundColor Green
        return 'new'
    } elseif ($isChanged) {
        Copy-Item $srcFile.FullName $target -Force
        Write-Host "  [~] $relativePath" -ForegroundColor Yellow
        return 'updated'
    } else {
        return 'skipped'
    }
}

Get-ChildItem -Path $extractedDir.FullName -Recurse -File |
    Where-Object {
        $relativePath = $_.FullName.Substring($extractedDir.FullName.Length + 1)
        $topDir = $relativePath.Split('\')[0]
        $topDir -notin $excludeDirs -and $topDir -ne 'scripts'
    } | ForEach-Object {
        $relativePath = $_.FullName.Substring($extractedDir.FullName.Length + 1)
        $result = Copy-WithLog $_ $ProjectRoot $relativePath
        switch ($result) { 'new' { $newCount++ } 'updated' { $updateCount++ } default { $skipCount++ } }
    }

# Copy scripts/ last
$scriptsDir = Join-Path $extractedDir.FullName 'scripts'
if (Test-Path $scriptsDir) {
    Get-ChildItem -Path $scriptsDir -Recurse -File | ForEach-Object {
        $subPath = $_.FullName.Substring($scriptsDir.Length + 1)
        $relativePath = "scripts\$subPath"
        $result = Copy-WithLog $_ $ProjectRoot $relativePath
        switch ($result) { 'new' { $newCount++ } 'updated' { $updateCount++ } default { $skipCount++ } }
    }
}

Write-Host ""
Write-Host "[INFO] 文件统计：新增 $newCount 个，修改 $updateCount 个，未变 $skipCount 个" -ForegroundColor Gray
Write-Host "  [+] 新增  [~] 已更新" -ForegroundColor Gray

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
