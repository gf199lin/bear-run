@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   1T2R VERTICAL CAPTURE GUIDE
echo ------------------------------------------------------------
echo   Before starting:
echo     1. Plug in the NEW 1T2R board (unplug the old one if both
echo        are connected - they share the same USB VID:PID).
echo     2. Rotate the board 90 degrees around its optical axis so
echo        the two antennas are one above the other.
echo     3. Stand about 1.2 m in front of the radar.
echo.
echo   The page opens automatically and runs a 15 s install check
echo   first. It tells you whether this board can see vertical.
echo.
echo   Stop: Ctrl+C in this window
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] python not found in PATH.
  pause
  exit /b 1
)

python tools\vguide_1t2r.py %*
echo.
pause
