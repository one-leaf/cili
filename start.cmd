@echo off
chcp 65001 >nul 2>&1
title Cili Agent

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
