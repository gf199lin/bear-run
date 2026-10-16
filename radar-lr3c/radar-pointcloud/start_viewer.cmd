@echo off
rem ---------------------------------------------------------------------------
rem  Radar live point-cloud viewer - one-click launcher
rem  Double-click this file (or the desktop shortcut that points to it).
rem  All user-facing messages are printed in Chinese by start_viewer.py.
rem  Keep this file ASCII-only: cmd.exe reads it in the local ANSI codepage,
rem  so Chinese text written as UTF-8 would show up as garbage.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
title Radar Live Viewer

set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py"
)
if not defined PY (
  echo [ERROR] Python was not found in PATH.
  echo Please install Python 3 from https://www.python.org and tick
  echo "Add python.exe to PATH" during setup, then run this file again.
  echo.
  pause
  exit /b 1
)

"%PY%" start_viewer.py

echo.
echo The viewer has stopped. Press any key to close this window.
pause >nul
