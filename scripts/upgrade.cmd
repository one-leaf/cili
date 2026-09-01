@echo off
chcp 65001 >nul 2>&1
title Cili Agent - Upgrade Tool

echo ========================================
echo   Cili Agent - Upgrade Tool
echo ========================================
echo.

:: ==================== Check Git in deps ====================

set "DEPS_GIT=%~dp0..\data\deps\git\cmd\git.exe"

if exist "%DEPS_GIT%" (
    set "GIT_CMD=%DEPS_GIT%"
    echo [OK] Found git in deps: %DEPS_GIT%
    goto :git_found
)

echo [ERROR] Git not found in deps directory!
echo.
echo Please run start.cmd first to download Git.
echo.
pause
exit /b 1

:git_found
echo.

:: ==================== Initialize Git repo ====================

if not exist .git (
    echo [INIT] Initializing local repository...
    "%GIT_CMD%" init
    "%GIT_CMD%" remote add origin https://github.com/one-leaf/cili.git
    echo.
)

:: ==================== Test mirrors ====================

echo [NET] Testing mirror sources...

:: Test GitHub direct
echo   Testing GitHub direct...
"%GIT_CMD%" ls-remote --heads https://github.com/one-leaf/cili.git >nul 2>&1
if not errorlevel 1 (
    echo     [OK] GitHub direct available
    set "REMOTE_URL=https://github.com/one-leaf/cili.git"
    goto :remote_set
)

:: Test ghproxy mirror
echo   Testing ghproxy mirror...
"%GIT_CMD%" ls-remote --heads https://ghproxy.com/https://github.com/one-leaf/cili.git >nul 2>&1
if not errorlevel 1 (
    echo     [OK] ghproxy mirror available
    set "REMOTE_URL=https://ghproxy.com/https://github.com/one-leaf/cili.git"
    goto :remote_set
)

echo [ERROR] All mirrors unavailable
echo.
echo Please check network connection or use VPN.
echo.
pause
exit /b 1

:remote_set
echo.

:: ==================== Save local changes ====================

"%GIT_CMD%" diff --quiet >nul 2>&1
if errorlevel 1 (
    echo [SAVE] Saving local changes...
    "%GIT_CMD%" stash
    set HAS_STASH=1
)

:: ==================== Fetch latest ====================

echo [FETCH] Fetching latest code...
"%GIT_CMD%" fetch origin main

if errorlevel 1 (
    echo [ERROR] Fetch failed
    echo.
    if defined HAS_STASH (
        echo   Restoring local changes...
        "%GIT_CMD%" stash pop
    )
    echo.
    echo Suggestions:
    echo   1. Check network connection
    echo   2. Use VPN or proxy
    echo   3. Manual download: https://github.com/one-leaf/cili
    echo.
    pause
    exit /b 1
)

:: ==================== Update to latest ====================

"%GIT_CMD%" checkout -B main origin/main

if errorlevel 1 (
    echo [ERROR] Update failed
    if defined HAS_STASH (
        echo   Restoring local changes...
        "%GIT_CMD%" stash pop
    )
    pause
    exit /b 1
)

:: ==================== Restore local changes ====================

if defined HAS_STASH (
    echo [RESTORE] Restoring local changes...
    "%GIT_CMD%" stash pop

    if errorlevel 1 (
        echo [WARN] Merge conflict, please resolve manually
        echo   Conflicted files:
        "%GIT_CMD%" diff --name-only
        echo.
    )
)

:: ==================== Done ====================

echo.
echo ========================================
echo   [OK] Upgrade completed!
echo ========================================
echo.
echo Tips:
echo   - Restart the service to apply updates
echo   - Resolve conflicts manually if any
echo.
pause
