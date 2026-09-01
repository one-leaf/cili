@echo off
chcp 65001 >nul 2>&1
title Cili Agent - Upgrade Tool

:: Check PowerShell
where powershell >nul 2>&1
if %errorlevel% neq 0 (
    echo [error] PowerShell not found.
    pause
    exit /b 1
)

:: Pass control to PowerShell script, passing project root as argument
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0upgrade.ps1" -ProjectRoot "%~dp0.." %*

:: Pause on error
if %errorlevel% neq 0 (
    echo.
    echo [error] Upgrade exited with error code %errorlevel%
    pause
)

exit /b %errorlevel%
