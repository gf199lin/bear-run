@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
title Radar kit 6/6 - stop serve
cd /d "%~dp0"

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo   [FAIL] Python not found.
    echo.
    pause
    exit /b 1
)

%PY% "%~dp0radar_serve.py" stop
echo.
pause
