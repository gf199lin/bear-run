@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
title Radar kit 5/6 - serve to game
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
echo   Auto-detect the boards and start WebSocket streams.
echo     left/right : ws://127.0.0.1:8766
echo     up/down    : ws://127.0.0.1:8767
echo.
%PY% "%~dp0radar_serve.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo   Streams are running in the background.
    echo   Stop them with 6-停止推流.cmd when you are done.
) else (
    echo   Serve failed. Read the [FAIL] lines above.
    echo   If it says a COM port is busy, close the other program first.
)
echo.
pause
