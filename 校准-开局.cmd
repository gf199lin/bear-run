@echo off
chcp 936 >nul
title 开局校准 - 让任何人都能用
cd /d "%~dp0"

echo ================================================================
echo   开局校准（约 13 秒）
echo   1. 站定别动 3 秒
echo   2. 往左手边走一大步，站住
echo   3. 往右手边走一大步，站住
echo ================================================================
echo.

echo [1/3] 停止推流，释放串口...
py -3 bear-run-new\radar-service\radar_serve.py stop >nul 2>&1
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8766,8767 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }" >nul 2>&1
timeout /t 3 /nobreak >nul
echo        完成
echo.

echo [2/3] 启动校准页面（后台），端口 8779...
start "" /min cmd /c "py -3 tools\calibrate_person.py --port COM3 --web-port 8779 >> .deepworks\tmp\calibrate.log 2>&1"
timeout /t 4 /nobreak >nul

echo [3/3] 打开浏览器...
start "" http://127.0.0.1:8779/
echo.
echo 按页面大字提示做：站定 - 往左走 - 往右走。
echo 完成后会显示「个人满量程」，并写入 radar-pointcloud\personal-cal.json
echo.
echo 如果显示红色警告（某一侧空间不足），按提示整体往另一侧站半步再跑一次。
echo 打不开就看 .deepworks\tmp\calibrate.log
echo.
timeout /t 5 >nul
