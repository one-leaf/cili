# Cili Agent upgrade tool (downloads the package; applied automatically on restart)

param(
    [string]$ProjectRoot = ""
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Cili Agent Upgrade Tool" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ==================== Switch to project root ====================

if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
} else {
    $ProjectRoot = (Resolve-Path $ProjectRoot).Path
}
Set-Location $ProjectRoot
Write-Host "[INFO] Working directory: $(Get-Location)" -ForegroundColor Gray
Write-Host ""

# ==================== Download upgrade zip ====================
# The package is stored in the project root, matching get_upgrade_zip_path()
# in core/updater.py. Extraction is not done here; the next startup applies it.

Write-Host "[DOWNLOAD] Downloading the latest upgrade package..." -ForegroundColor Cyan

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
    Write-Host "[ERROR] Download failed" -ForegroundColor Red
    Write-Host ""
    Write-Host "Check your network connection or use a VPN" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

# ==================== Done ====================

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  [OK] Upgrade package downloaded!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Notes:" -ForegroundColor Cyan
Write-Host "  - Package location: $destZip"
Write-Host "  - Restart the service (start.cmd / python main.py); the version is compared and the package is applied"
Write-Host "  - Same-version packages are also applied to repair files; a package older than the current version is deleted at startup to prevent downgrades"
Write-Host ""
Read-Host "Press Enter to exit"
