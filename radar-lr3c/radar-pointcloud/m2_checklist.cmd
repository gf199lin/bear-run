@echo off
rem ---------------------------------------------------------------------------
rem  M2 acceptance checklist - one-click guided runner
rem  Double-click this file (or the desktop shortcut that points to it).
rem  All user-facing messages are printed in Chinese by m2_checklist.py.
rem  Keep this file ASCII-only: cmd.exe reads it in the local ANSI codepage,
rem  so Chinese text written as UTF-8 would show up as garbage.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
title M2 Checklist - Radar Point Cloud

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

"%PY%" m2_checklist.py
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo The checklist exited with code %RC%. See the messages above.
echo The checklist has stopped. Press any key to close this window.
pause >nul
exit /b %RC%
