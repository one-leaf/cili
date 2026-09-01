# Cili Agent 升级工具

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Cili Agent 升级工具" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ==================== Check Git in deps ====================

$depsGit = Join-Path $PSScriptRoot "..\data\deps\git\cmd\git.exe"

if (Test-Path $depsGit) {
    $gitCmd = $depsGit
    Write-Host "[OK] 找到 Git: $depsGit" -ForegroundColor Green
} else {
    Write-Host "[ERROR] deps 目录中未找到 git" -ForegroundColor Red
    Write-Host ""
    Write-Host "请先运行 start.cmd 下载 Git" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

Write-Host ""

# ==================== Initialize Git repo ====================

if (-not (Test-Path ".git")) {
    Write-Host "[INIT] 初始化本地仓库..." -ForegroundColor Yellow
    & $gitCmd init
    & $gitCmd remote add origin https://github.com/one-leaf/cili.git
    Write-Host ""
}

# ==================== Test mirrors ====================

Write-Host "[NET] 测试镜像源..." -ForegroundColor Cyan

# Test GitHub direct
Write-Host "  测试 GitHub 直连..." -ForegroundColor Gray
$testResult = & $gitCmd ls-remote --heads https://github.com/one-leaf/cili.git 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "    [OK] GitHub 直连可用" -ForegroundColor Green
    $remoteUrl = "https://github.com/one-leaf/cili.git"
} else {
    # Test ghproxy mirror
    Write-Host "  测试 ghproxy 镜像..." -ForegroundColor Gray
    $testResult = & $gitCmd ls-remote --heads https://ghproxy.com/https://github.com/one-leaf/cili.git 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "    [OK] ghproxy 镜像可用" -ForegroundColor Green
        $remoteUrl = "https://ghproxy.com/https://github.com/one-leaf/cili.git"
    } else {
        Write-Host "[ERROR] 所有镜像源均不可用" -ForegroundColor Red
        Write-Host ""
        Write-Host "请检查网络连接或使用 VPN" -ForegroundColor Yellow
        Write-Host ""
        Read-Host "按回车键退出"
        exit 1
    }
}

Write-Host ""

# ==================== Save local changes ====================

$hasStash = $false
& $gitCmd diff --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "[SAVE] 检测到本地修改，正在暂存..." -ForegroundColor Yellow
    & $gitCmd stash
    $hasStash = $true
}

# ==================== Fetch latest ====================

Write-Host "[FETCH] 正在拉取最新代码..." -ForegroundColor Cyan
& $gitCmd fetch origin main

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] 拉取失败" -ForegroundColor Red
    Write-Host ""
    if ($hasStash) {
        Write-Host "  恢复本地修改..." -ForegroundColor Yellow
        & $gitCmd stash pop
    }
    Write-Host ""
    Write-Host "建议：" -ForegroundColor Yellow
    Write-Host "  1. 检查网络连接"
    Write-Host "  2. 使用 VPN 或代理"
    Write-Host "  3. 手动下载：https://github.com/one-leaf/cili"
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

# ==================== Update to latest ====================

& $gitCmd checkout -B main origin/main

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] 更新失败" -ForegroundColor Red
    if ($hasStash) {
        Write-Host "  恢复本地修改..." -ForegroundColor Yellow
        & $gitCmd stash pop
    }
    Read-Host "按回车键退出"
    exit 1
}

# ==================== Restore local changes ====================

if ($hasStash) {
    Write-Host "[RESTORE] 恢复本地修改..." -ForegroundColor Yellow
    & $gitCmd stash pop

    if ($LASTEXITCODE -ne 0) {
        Write-Host "[WARN] 合并冲突，请手动解决" -ForegroundColor Yellow
        Write-Host "  冲突文件："
        & $gitCmd diff --name-only
        Write-Host ""
    }
}

# ==================== Done ====================

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  [OK] 升级完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "提示：" -ForegroundColor Cyan
Write-Host "  - 重新启动服务以应用更新"
Write-Host "  - 如有冲突，请手动解决后重新提交"
Write-Host ""
Read-Host "按回车键退出"
