@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
title Radar kit 2/6 - check
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

%PY% "%~dp0radar_autoconnect.py" scan
echo.
%PY% "%~dp0radar_autoconnect.py" check
set "RC=%ERRORLEVEL%"

echo.
echo --------------------------------------------------------------------
if "%RC%"=="0" (
    echo   Result: at least one board is streaming. Read the
    echo           "可用（出帧正常）" lines above to see how many.
) else (
    echo   Result: nothing connected. Match the [FAIL] reason above:
    echo.
    echo     port missing  -^> board not plugged / charge-only cable / no driver
    echo     port busy     -^> close ONELAB, the pointcloud server, or an old window
    echo     no data       -^> board not powered or not streaming yet
    echo     unrecognized  -^> wrong baud / wrong firmware mode
)
echo --------------------------------------------------------------------
echo.
echo   Save this report to send back:
echo     %PY% radar_autoconnect.py check --json ^> check-result.txt
echo.
pause
