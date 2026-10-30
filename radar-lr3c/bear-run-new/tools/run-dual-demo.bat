@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  无雷达自测：用假数据把「双开 + 后台判定 + 汇聚」整条链路跑起来
REM  用途：组员拿到代码、手上还没雷达时，先确认流程和游戏侧没问题
REM  不需要任何硬件，也不需要装第三方库
REM ============================================================

set PY=py -3
set LR_PORT=9001
set UD_PORT=9002
set HUB_PORT=8766
set FAKE_LR_PORT=8866
set FAKE_UD_PORT=8867

cd /d "%~dp0"

echo [0/3] 启动两路假雷达（左右 / 上下各一路，UDP 20Hz）
start "FAKE-LR" cmd /k %PY% fake_radar_ws.py --port %FAKE_LR_PORT% --profile good --udp-out 127.0.0.1:8888 --udp-channels lr
start "FAKE-UD" cmd /k %PY% fake_radar_ws.py --port %FAKE_UD_PORT% --profile good --udp-out 127.0.0.1:8889 --udp-channels ud

timeout /t 2 /nobreak >nul

echo [1/3] 左右判定进程
start "RADAR-LR" cmd /k %PY% radar_bridge.py --from udp --udp-port 8888 --role lr --judge --port %LR_PORT%

echo [2/3] 上下判定进程
start "RADAR-UD" cmd /k %PY% radar_bridge.py --from udp --udp-port 8889 --role ud --judge --port %UD_PORT%

timeout /t 3 /nobreak >nul

echo [3/3] 汇聚进程
start "RADAR-HUB" cmd /k %PY% radar_hub.py --lr ws://127.0.0.1:%LR_PORT% --ud ws://127.0.0.1:%UD_PORT% --port %HUB_PORT%

echo.
echo ============================================================
echo   自测链路已启动（纯假数据）
echo   游戏地址：
echo   http://127.0.0.1:8099/index.html?input=radar^&judge=backend^&radar=ws://127.0.0.1:%HUB_PORT%
echo.
echo   预期：RADAR-UD 窗口每 10 秒打印一次 动作: jump / slide
echo         RADAR-LR 窗口随着横向摆动打印 动作: left / right
echo         游戏里能正常换道、跳、滑铲
echo ============================================================
pause
