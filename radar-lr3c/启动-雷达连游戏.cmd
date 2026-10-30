@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Radar  ->  Bear Run
echo ------------------------------------------------------------
echo   1. Plug the radar board into USB (1T2R or 2T4R, any port)
echo   2. This window finds it and starts streaming
echo   3. The browser opens the game page automatically
echo.
echo   Stop: press Ctrl+C in this window
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] python not found in PATH.
  echo         Install Python 3 and make sure "python" works.
  pause
  exit /b 1
)

python tools\bear_run_radar_launcher.py %*

echo.
echo Bridge stopped.
pause
