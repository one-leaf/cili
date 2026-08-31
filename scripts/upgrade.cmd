@echo off
chcp 65001 >nul
title Cili Agent - 升级工具

echo ========================================
echo   Cili Agent 升级工具
echo ========================================
echo.

:: ==================== 检测 Git 路径 ====================

set GIT_CMD=

:: 1. 检查系统 PATH 中的 git
where git >nul 2>&1
if not errorlevel 1 (
    set GIT_CMD=git
    echo ✅ 检测到系统 git
    goto :git_found
)

:: 2. 检查 data/deps/git/ 下的 Git Bash
set "DEPS_GIT=%~dp0..\data\deps\git"
if exist "%DEPS_GIT%\cmd\git.exe" (
    set "GIT_CMD=%DEPS_GIT%\cmd\git.exe"
    echo ✅ 检测到 data/deps/git 中的 git
    goto :git_found
)

:: 3. 检查常见 Git 安装路径
for %%P in (
    "C:\Program Files\Git\cmd\git.exe"
    "C:\Program Files (x86)\Git\cmd\git.exe"
    "%LOCALAPPDATA%\Programs\Git\cmd\git.exe"
) do (
    if exist %%P (
        set "GIT_CMD=%%~P"
        echo ✅ 检测到 %%~P
        goto :git_found
    )
)

echo ❌ 未检测到 git
echo.
echo 💡 解决方案：
echo   1. 安装 Git: https://git-scm.com/download/win
echo   2. 或运行 start.cmd 自动下载 Git
echo.
pause
exit /b 1

:git_found
echo.

:: ==================== 初始化 Git 仓库 ====================

if not exist .git (
    echo 📦 初始化本地仓库...
    "%GIT_CMD%" init
    "%GIT_CMD%" remote add origin https://github.com/one-leaf/cili.git
    echo.
)

:: ==================== 测试镜像源 ====================

echo 🌐 测试镜像源...

:: 测试 GitHub 直连
echo   测试 GitHub 直连...
"%GIT_CMD%" ls-remote --heads https://github.com/one-leaf/cili.git >nul 2>&1
if not errorlevel 1 (
    echo     ✅ GitHub 直连可用
    set "REMOTE_URL=https://github.com/one-leaf/cili.git"
    goto :remote_set
)

:: 测试 ghproxy 镜像
echo   测试 ghproxy 镜像...
"%GIT_CMD%" ls-remote --heads https://ghproxy.com/https://github.com/one-leaf/cili.git >nul 2>&1
if not errorlevel 1 (
    echo     ✅ ghproxy 镜像可用
    set "REMOTE_URL=https://ghproxy.com/https://github.com/one-leaf/cili.git"
    goto :remote_set
)

echo ❌ 所有镜像源均不可用
echo.
echo 💡 请检查网络连接或使用 VPN
echo.
pause
exit /b 1

:remote_set
echo.

:: ==================== 保存本地修改 ====================

"%GIT_CMD%" diff --quiet >nul 2>&1
if errorlevel 1 (
    echo ️  检测到本地修改，正在暂存...
    "%GIT_CMD%" stash
    set HAS_STASH=1
)

:: ==================== 拉取最新代码 ====================

echo 🔄 正在拉取最新代码...
"%GIT_CMD%" fetch origin main

if errorlevel 1 (
    echo ❌ 拉取失败
    echo.
    if defined HAS_STASH (
        echo   恢复本地修改...
        "%GIT_CMD%" stash pop
    )
    echo.
    echo 💡 建议：
    echo   1. 检查网络连接
    echo   2. 使用 VPN 或代理
    echo   3. 手动下载: https://github.com/one-leaf/cili
    echo.
    pause
    exit /b 1
)

:: ==================== 更新到最新版本 ====================

"%GIT_CMD%" checkout -B main origin/main

if errorlevel 1 (
    echo ❌ 更新失败
    if defined HAS_STASH (
        echo   恢复本地修改...
        "%GIT_CMD%" stash pop
    )
    pause
    exit /b 1
)

:: ==================== 恢复本地修改 ====================

if defined HAS_STASH (
    echo  恢复本地修改...
    "%GIT_CMD%" stash pop

    if errorlevel 1 (
        echo ⚠️  合并冲突，请手动解决
        echo   冲突文件：
        "%GIT_CMD%" diff --name-only
        echo.
    )
)

:: ==================== 完成 ====================

echo.
echo ========================================
echo   ✅ 升级完成！
echo ========================================
echo.
echo  提示：
echo   - 重新启动服务以应用更新
echo   - 如有冲突，请手动解决后重新提交
echo.
pause
