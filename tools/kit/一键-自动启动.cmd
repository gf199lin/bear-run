@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ASCII-only on purpose: cmd.exe mis-parses UTF-8 Chinese inside .cmd files.
rem All Chinese guidance is printed by tools\dual_radar_manager.py.

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [ERROR] Python 3 not found in PATH.
  echo         Install Python 3 and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

%PY% -c "import serial, numpy" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] pyserial / numpy missing.
  echo         Double-click "install-deps.cmd" once, then run this again.
  pause
  exit /b 1
)

echo ============================================================
echo   ONE-CLICK START  (no clicking on the web page needed)
echo ------------------------------------------------------------
echo   Plug order:
echo     1) LEFT/RIGHT board first, wait about 2 seconds
echo     2) UP/DOWN board second
echo.
echo   This window will:
echo     - auto-detect the COM ports
echo     - assign roles by plug order (first=left/right)
echo     - start both bridges on ws://127.0.0.1:8766 and :8767
echo     - open the game page in your browser
echo.
echo   If a board cannot be assigned, the assignment page opens
echo   instead and you can pick the port by hand.
echo   Stop: Ctrl+C in this window.
echo ============================================================
echo.

%PY% tools\dual_radar_manager.py --autostart %*

echo.
echo Service stopped. Press any key to close.
pause >nul
