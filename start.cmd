@echo off
title Cili Agent

:: Check Windows version first (before chcp 65001 - old systems may misbehave)
:: NOTE: This file must stay GBK-encoded. The Chinese text below is read at the
::       console's default codepage (cp936 on zh-CN Windows); saving as UTF-8
::       would garble it and break the cmd parser's line tracking.
for /f "tokens=4-6 delims=.[] " %%a in ('ver') do (set "_winver=%%a.%%b.%%c" & set "_winmajor=%%a")
if %_winmajor% lss 10 goto :unsupported
set "_winver=" & set "_winmajor="

chcp 65001 >nul 2>&1

:: Check PowerShell
where powershell >nul 2>&1
if %errorlevel% neq 0 (
    echo [error] PowerShell not found.
    pause
    exit /b 1
)

:: Pass all args to PowerShell script
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0start.ps1" %*

:: Pause on error
if %errorlevel% neq 0 (
    echo.
    echo [error] Cili Agent exited with error code %errorlevel%
    pause
)

exit /b %errorlevel%

:: Below Windows 10: not supported
:unsupported
chcp 936 >nul 2>&1
echo.
echo [error] 当前系统为 Windows %_winver%，不受支持。
echo         Cili Agent 需要 Windows 10 及以上版本。
echo.
pause
exit /b 1
