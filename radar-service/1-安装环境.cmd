@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
title Radar kit 1/6 - setup
cd /d "%~dp0"

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY goto NOPY

%PY% "%~dp0setup_check.py"
goto END

:NOPY
echo.
echo ====================================================================
echo   [FAIL] Python not found
echo ====================================================================
echo.
echo   You need Python 3.8 or newer.
echo.
echo   1) Open  Python安装说明.md  in this folder and follow it.
echo   2) Install Python from https://www.python.org/downloads/windows/
echo   3) IMPORTANT: tick  "Add python.exe to PATH"  during install.
echo   4) Close this window, then double-click 1-安装环境.cmd again.
echo.
start "" notepad "%~dp0Python安装说明.md" 2>nul

:END
echo.
pause
