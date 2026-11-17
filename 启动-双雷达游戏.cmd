@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Keep this file ASCII-only: cmd.exe mis-parses UTF-8 Chinese in .cmd files.
rem All Chinese guidance is printed by tools\dual_radar_manager.py instead.

where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python 3 not found in PATH. Install it and check "Add to PATH".
    pause
    exit /b 1
  )
  set "PY=py"
) else (
  set "PY=python"
)

"%PY%" tools\dual_radar_manager.py %*

echo.
echo Service stopped. Press any key to close.
pause >nul
