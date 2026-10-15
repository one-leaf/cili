@echo off
chcp 65001 >nul 2>&1
title Cili TUI

:: Cili terminal client launcher.
:: Runs the TUI from ANY directory - the current directory becomes the workspace.
:: The server must be running separately (start.cmd / python main.py).

set "CILI_ROOT=%~dp0"
if "%CILI_ROOT:~-1%"=="\" set "CILI_ROOT=%CILI_ROOT:~0,-1%"

:: Python discovery (same priority as start.ps1, but no auto-download):
:: 1. portable python in data\deps\python (project root is baked into ._pth,
::    so interfaces.* is importable from any cwd)
:: 2. python from PATH (project root passed via PYTHONPATH)
set "PYTHON=%CILI_ROOT%\data\deps\python\python.exe"
if exist "%PYTHON%" goto :run

where python >nul 2>&1
if errorlevel 1 (
    echo [error] Python not found. Run start.cmd first to install the environment.
    exit /b 1
)
set "PYTHON=python"
set "PYTHONPATH=%CILI_ROOT%"

:run
:: mintty (Git Bash): python's stdin/stdout is not a console - wrap with winpty.
if /i "%TERM_PROGRAM%"=="mintty" (
    where winpty >nul 2>&1
    if not errorlevel 1 (
        winpty "%PYTHON%" -m interfaces.tui %*
        exit /b %errorlevel%
    )
)

:: Never cd - the current directory IS the workspace.
"%PYTHON%" -m interfaces.tui %*
exit /b %errorlevel%
