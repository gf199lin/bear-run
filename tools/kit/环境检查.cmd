@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Environment check
echo ------------------------------------------------------------
echo   Required: Python 3.9+  /  pyserial  /  numpy
echo ============================================================
echo.

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [FAIL] Python 3 not found in PATH.
  echo        Install Python 3 from python.org and tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

echo [1/4] Python version
%PY% --version
if errorlevel 1 (
  echo [FAIL] Python cannot run.
  pause
  exit /b 1
)
echo.

echo [2/4] Packages
%PY% -c "import serial, numpy; print('  pyserial', serial.__version__); print('  numpy   ', numpy.__version__)"
if errorlevel 1 (
  echo.
  echo [FAIL] Missing packages. Double-click install-deps.cmd, or run:
  echo        %PY% -m pip install pyserial numpy
  echo.
  pause
  exit /b 1
)
echo.

echo [3/4] Radar modules import
%PY% -c "import sys; sys.path.insert(0,'radar-pointcloud'); import radar_source, radar_dsp, radar_actions; print('  radar modules OK')"
if errorlevel 1 (
  echo [FAIL] Radar modules failed to import. Folder layout may be broken.
  pause
  exit /b 1
)
echo.

echo [4/4] Key files
if not exist "radar-bridge\tools\radar_live_ws.py" (
  echo [FAIL] radar-bridge\tools\radar_live_ws.py missing.
  pause
  exit /b 1
)
if not exist "bear-run-new\index.html" (
  echo [FAIL] bear-run-new\index.html missing.
  pause
  exit /b 1
)
if not exist "tools\dual_radar_manager.py" (
  echo [FAIL] tools\dual_radar_manager.py missing.
  pause
  exit /b 1
)
echo   radar-bridge OK / game OK / manager OK
echo.

echo ------------------------------------------------------------
echo   All good.
echo   Next:
echo     1) plug the LEFT/RIGHT board first, wait 2 s
echo     2) plug the UP/DOWN board second
echo     3) double-click the ONE-CLICK START .cmd in this folder
echo ------------------------------------------------------------
echo.
pause
