@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
title Radar kit 4/6 - watch
cd /d "%~dp0"

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo   [FAIL] Python not found. Run 1-安装环境.cmd first.
    echo.
    pause
    exit /b 1
)

echo.
echo   Watching plug / unplug / streaming state. Press Ctrl+C to stop.
echo.
%PY% "%~dp0radar_autoconnect.py" watch --interval 3
echo.
pause
