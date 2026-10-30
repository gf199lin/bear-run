@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
title [2/4] Radar point cloud viewer
cd /d "%~dp0"

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY goto NOPY

%PY% start_viewer.py
set "RC=%ERRORLEVEL%"
goto END

:NOPY
set "RC=1"
echo.
echo ============================================================
echo   [FAIL] Python was not found in PATH.
echo ============================================================
echo.
echo   Install Python 3.8 or newer:
echo     https://www.python.org/downloads/windows/
echo   Tick "Add python.exe to PATH" during setup, then run again.
echo.

:END
echo.
if not "%RC%"=="0" echo [NOTE] Exit code %RC% - see the messages above.
echo Press any key to close this window.
pause >nul
exit /b %RC%
