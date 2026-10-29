@echo off
REM 双雷达一键启动: 1T2R(COM3) 管左右 :8766 / 2T4R(COM4) 管上下 :8767
REM 前置: 两个雷达串口必须空闲(关掉 calib_collect / 厂商工具)
setlocal
cd /d "%~dp0.."
set LOGDIR=..\.deepworks\tmp
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo [1/2] 1T2R COM3 -^> ws://127.0.0.1:8766  (role=lr)
start "radar-lr" /min pythonw tools\radar_live_ws.py --port COM3 --role lr --ws-port 8766 --log "%LOGDIR%\radar-lr.log"
echo [2/2] 2T4R COM4 -^> ws://127.0.0.1:8767  (role=ud)
start "radar-ud" /min pythonw tools\radar_live_ws.py --port COM4 --role ud --ws-port 8767 --log "%LOGDIR%\radar-ud.log"

echo.
echo 等 6 秒后自检...
timeout /t 6 /nobreak >nul
python tools\check_radar.py ws://127.0.0.1:8766 --seconds 6
python tools\check_radar.py ws://127.0.0.1:8767 --seconds 6
echo.
echo 两个都 OK 就打开联调台 / 游戏:
echo   联调台  http://127.0.0.1:8801/radar-connect.html
echo   游戏    http://127.0.0.1:8801/index.html?input=radar^&radar=ws://127.0.0.1:8766^&radar2=ws://127.0.0.1:8767
endlocal
