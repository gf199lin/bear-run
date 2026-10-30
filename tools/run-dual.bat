@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

REM ============================================================
REM  双雷达双开：左右一台、上下一台，判定都在后台完成
REM  组员只需要改下面 6 行，其它不用动
REM
REM  上游怎么填（先跟数据组确认雷达是什么接口）：
REM    UDP 直发：   set LR_FROM=udp    set LR_ARG=--udp-port 8888
REM    TCP 服务：   set LR_FROM=tcp    set LR_ARG=--tcp 192.168.1.50:9000
REM    主动推给本机：set LR_FROM=tcp-listen  set LR_ARG=--listen-port 9100
REM    HTTP 轮询：  set LR_FROM=http   set LR_ARG=--http http://192.168.1.50/api
REM    厂商 SDK：   set LR_FROM=exec   set LR_ARG=--exec "C:\vendor\radar_cli.exe --dev 1"
REM ============================================================

set PY=py -3

set LR_FROM=udp
set LR_ARG=--udp-port 8888

set UD_FROM=udp
set UD_ARG=--udp-port 8889

set LR_PORT=9001
set UD_PORT=9002
set HUB_PORT=8766

REM ============================================================

cd /d "%~dp0"

echo ============================================================
echo   [1/3] 左右雷达进程  (--role lr --judge)  输出 ws://127.0.0.1:%LR_PORT%
echo         只判 left / right，永远不产生 jump / slide
echo ============================================================
start "RADAR-LR" cmd /k %PY% radar_bridge.py --from %LR_FROM% %LR_ARG% --role lr --judge --port %LR_PORT% --print-sample

echo ============================================================
echo   [2/3] 上下雷达进程  (--role ud --judge)  输出 ws://127.0.0.1:%UD_PORT%
echo         只判 jump / slide，永远不产生 left / right
echo ============================================================
start "RADAR-UD" cmd /k %PY% radar_bridge.py --from %UD_FROM% %UD_ARG% --role ud --judge --port %UD_PORT% --print-sample

echo 等 3 秒让两个判定进程起来...
timeout /t 3 /nobreak >nul

echo ============================================================
echo   [3/3] 汇聚进程    输出 ws://127.0.0.1:%HUB_PORT%  (游戏连这个)
echo         只转发，不做判定
echo ============================================================
start "RADAR-HUB" cmd /k %PY% radar_hub.py --lr ws://127.0.0.1:%LR_PORT% --ud ws://127.0.0.1:%UD_PORT% --port %HUB_PORT%

echo.
echo ============================================================
echo   三个窗口都起来后，浏览器打开：
echo   http://127.0.0.1:8099/index.html?input=radar^&judge=backend^&radar=ws://127.0.0.1:%HUB_PORT%
echo.
echo   没起网页服务的话，先在 radar-runner 目录执行：
echo     %PY% -m http.server 8099 --bind 127.0.0.1
echo ============================================================
pause
