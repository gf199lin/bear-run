@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [ERROR] Python 3 not found in PATH.
  pause
  exit /b 1
)

echo ============================================================
echo   Radar link self-test
echo ------------------------------------------------------------
echo   Checks ports 8766 / 8767 / 8778 and whether frames arrive.
echo   IMPORTANT: start the service first (one-click start .cmd),
echo              then run this file.
echo ============================================================
echo.

%PY% tools\radar_selftest.py %*

echo.
pause >nul
