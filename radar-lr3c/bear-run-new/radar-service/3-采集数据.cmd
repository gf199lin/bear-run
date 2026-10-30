@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
title Radar kit 3/6 - capture
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
echo   Raw radar bytes will be recorded.
echo   The tool finds the streaming boards automatically -- do NOT type a COM number.
echo.
set "SECS=60"
set /p SECS=Seconds to record [60]:
if "%SECS%"=="" set "SECS=60"

echo.
%PY% "%~dp0radar_autoconnect.py" capture --seconds %SECS% --outdir captures
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo   Files are in: %~dp0captures\
    echo   Send the whole captures folder back to the team lead.
) else (
    echo   Nothing recorded. Run 2-检查雷达.cmd first to see which step failed.
)
echo.
pause
