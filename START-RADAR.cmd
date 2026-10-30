@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title radar-runner launcher

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY ( where python >nul 2>&1 && set "PY=python" )

if not defined PY goto NOPY

:menu
cls
echo.
echo  ==================================================
echo    radar-runner  -  all-in-one launcher
echo  ==================================================
echo.
echo    [1] Play the game      push 8766/8767 + open game page
echo    [2] Point cloud        viewer on http://127.0.0.1:8099
echo    [3] Check radar        list COM ports and frame rate
echo    [4] Install deps       pip install numpy pyserial
echo    [5] Stop streams       kill background radar streams
echo    [0] Exit
echo.
echo    Note: the viewer and the stream both lock the COM ports.
echo          Stop one before starting the other.
echo.
set "CH="
set /p "CH=  Choose [1]: "
if not defined CH set "CH=1"
if "%CH%"=="0" exit /b 0
if "%CH%"=="1" goto play
if "%CH%"=="2" goto cloud
if "%CH%"=="3" goto check
if "%CH%"=="4" goto deps
if "%CH%"=="5" goto stop
goto menu

:play
cls
echo.
echo  [1/2] starting game page server on port 8801 ...
netstat -ano | findstr ":8801 " | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
  start "game-8801" /MIN cmd /k %PY% -m http.server 8801 --directory .
  timeout /t 2 >nul
) else (
  echo        port 8801 already in use - assuming the page server is running.
)
echo.
echo  [2/2] detecting radar and starting streams ...
echo        this takes about 10 seconds
echo.
%PY% "radar-service\radar_serve.py"
if errorlevel 1 (
  echo.
  echo  [WARN] streams did not start. The game page still opens,
  echo         but there will be no radar data. Use [3] to diagnose.
  echo.
  timeout /t 3 >nul
)
echo.
echo  opening the game page ...
start "" "http://127.0.0.1:8801/index.html?input=radar&radar=ws://127.0.0.1:8766&radar2=ws://127.0.0.1:8767"
echo.
echo  Done. Close this window when finished.
echo  To stop the radar streams later use option [5].
echo.
pause
exit /b 0

:cloud
cls
echo.
echo  Starting the point-cloud viewer ...
echo  It locks the COM ports, so stop any running stream first (option 5).
echo.
start "radar-viewer" /MIN cmd /k %PY% "radar-service\start_viewer.py"
echo  Viewer window opened. The browser should come up at:
echo    http://127.0.0.1:8099
echo.
pause
exit /b 0

:check
cls
echo.
%PY% "radar-service\radar_autoconnect.py" scan
echo.
%PY% "radar-service\radar_autoconnect.py" check
echo.
echo  Full report: open the radar-service folder and run the check .cmd there.
echo.
pause
goto menu

:deps
cls
echo.
%PY% "radar-service\setup_check.py"
echo.
pause
goto menu

:stop
cls
echo.
%PY% "radar-service\radar_serve.py" stop
echo.
pause
goto menu

:NOPY
cls
echo.
echo  [FAIL] Python not found.
echo.
echo  1) Install Python 3.8+ from https://www.python.org/downloads/windows/
echo     IMPORTANT: tick "Add python.exe to PATH" during install.
echo  2) Reopen this window and try option [4] Install deps.
echo.
pause
exit /b 1
