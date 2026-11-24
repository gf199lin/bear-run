@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ASCII-only on purpose: cmd.exe mis-parses UTF-8 Chinese inside .cmd files.

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [ERROR] Python 3 not found in PATH.
  echo         Install Python 3 from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

echo ============================================================
echo   Install Python packages for this kit
echo ------------------------------------------------------------
echo   Required: pyserial (USB serial) + numpy (signal math)
echo ============================================================
echo.

%PY% --version

echo.
echo Trying Tsinghua mirror first (faster in mainland China)...
%PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
  echo.
  echo Mirror failed, falling back to the default index...
  %PY% -m pip install -r requirements.txt
)

echo.
%PY% -c "import serial, numpy; print('OK  pyserial', serial.__version__, ' numpy', numpy.__version__)"
if errorlevel 1 (
  echo.
  echo [FAIL] Still missing. Check your network, or install manually:
  echo        %PY% -m pip install pyserial numpy
  pause
  exit /b 1
)

echo.
echo Done. Next: double-click the ONE-CLICK START .cmd in this folder.
pause
