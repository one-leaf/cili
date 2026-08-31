# Cili Agent 升级工具

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Cili Agent 升级工具" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ==================== 检测 Git 路径 ====================

$gitCmd = $null

# 1. 检查系统 PATH 中的 git
try {
    $null = Get-Command git -ErrorAction Stop
    $gitCmd = "git"
    Write-Host "✅ 检测到系统 git" -ForegroundColor Green
} catch {
    # 2. 检查 data/deps/git/ 下的 Git Bash
    $depsGit = Join-Path $PSScriptRoot "..\data\deps\git\cmd\git.exe"
    if (Test-Path $depsGit) {
        $gitCmd = $depsGit
        Write-Host "✅ 检测到 data/deps/git 中的 git" -ForegroundColor Green
    } else {
        # 3. 检查常见 Git 安装路径
        $commonPaths = @(
            "C:\Program Files\Git\cmd\git.exe",
            "C:\Program Files (x86)\Git\cmd\git.exe",
            "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"
        )
        foreach ($path in $commonPaths) {
            if (Test-Path $path) {
                $gitCmd = $path
                Write-Host "✅ 检测到 $path" -ForegroundColor Green
                break
            }
        }
    }
}

if (-not $gitCmd) {
    Write-Host "❌ 未检测到 git" -ForegroundColor Red
    Write-Host ""
    Write-Host " 解决方案：" -ForegroundColor Yellow
    Write-Host "  1. 安装 Git: https://git-scm.com/download/win"
    Write-Host "  2. 或运行 start.cmd 自动下载 Git"
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

Write-Host ""

# ==================== 初始化 Git 仓库 ====================

if (-not (Test-Path ".git")) {
    Write-Host "📦 初始化本地仓库..." -ForegroundColor Yellow
    & $gitCmd init
    & $gitCmd remote add origin https://github.com/one-leaf/cili.git
    Write-Host ""
}

# ==================== 测试镜像源 ====================

Write-Host "🌐 测试镜像源..." -ForegroundColor Cyan

# 测试 GitHub 直连
Write-Host "  测试 GitHub 直连..." -ForegroundColor Gray
$testResult = & $gitCmd ls-remote --heads https://github.com/one-leaf/cili.git 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "    ✅ GitHub 直连可用" -ForegroundColor Green
    $remoteUrl = "https://github.com/one-leaf/cili.git"
} else {
    # 测试 ghproxy 镜像
    Write-Host "  测试 ghproxy 镜像..." -ForegroundColor Gray
    $testResult = & $gitCmd ls-remote --heads https://ghproxy.com/https://github.com/one-leaf/cili.git 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "    ✅ ghproxy 镜像可用" -ForegroundColor Green
        $remoteUrl = "https://ghproxy.com/https://github.com/one-leaf/cili.git"
    } else {
        Write-Host "❌ 所有镜像源均不可用" -ForegroundColor Red
        Write-Host ""
        Write-Host "💡 请检查网络连接或使用 VPN" -ForegroundColor Yellow
        Write-Host ""
        Read-Host "按回车键退出"
        exit 1
    }
}

Write-Host ""

# ==================== 保存本地修改 ====================

$hasStash = $false
& $gitCmd diff --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "⚠️  检测到本地修改，正在暂存..." -ForegroundColor Yellow
    & $gitCmd stash
    $hasStash = $true
}

# ==================== 拉取最新代码 ====================

Write-Host "🔄 正在拉取最新代码..." -ForegroundColor Cyan
& $gitCmd fetch origin main

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ 拉取失败" -ForegroundColor Red
    Write-Host ""
    if ($hasStash) {
        Write-Host "  恢复本地修改..." -ForegroundColor Yellow
        & $gitCmd stash pop
    }
    Write-Host ""
    Write-Host "💡 建议：" -ForegroundColor Yellow
    Write-Host "  1. 检查网络连接"
    Write-Host "  2. 使用 VPN 或代理"
    Write-Host "  3. 手动下载：https://github.com/one-leaf/cili"
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

# ==================== 更新到最新版本 ====================

& $gitCmd checkout -B main origin/main

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ 更新失败" -ForegroundColor Red
    if ($hasStash) {
        Write-Host "  恢复本地修改..." -ForegroundColor Yellow
        & $gitCmd stash pop
    }
    Read-Host "按回车键退出"
    exit 1
}

# ==================== 恢复本地修改 ====================

if ($hasStash) {
    Write-Host "📝 恢复本地修改..." -ForegroundColor Yellow
    & $gitCmd stash pop

    if ($LASTEXITCODE -ne 0) {
        Write-Host "⚠️  合并冲突，请手动解决" -ForegroundColor Yellow
        Write-Host "  冲突文件："
        & $gitCmd diff --name-only
        Write-Host ""
    }
}

# ==================== 完成 ====================

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  ✅ 升级完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "提示：" -ForegroundColor Cyan
Write-Host "  - 重新启动服务以应用更新"
Write-Host "  - 如有冲突，请手动解决后重新提交"
Write-Host ""
Read-Host "按回车键退出"
