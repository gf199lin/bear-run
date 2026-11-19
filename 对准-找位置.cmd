@echo off
chcp 936 >nul
title 对准 - 找位置（左右板 COM5）
cd /d "%~dp0"

echo ================================================================
echo   对准页面：把身体站到 +30 度（斜向强区）
echo   水平板 = COM5
echo ================================================================
echo.

echo [1/3] 停止推流，释放串口...
py -3 bear-run-new\radar-service\radar_serve.py stop >nul 2>&1
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8766,8767 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }" >nul 2>&1
timeout /t 3 /nobreak >nul
echo        完成
echo.

echo [2/3] 启动页面（后台），端口 8774...
start "" /min cmd /c "py -3 tools\aim_web.py --port COM3 --web-port 8774 --target 30 >> .deepworks\tmp\aim_web.log 2>&1"
timeout /t 4 /nobreak >nul

echo [3/3] 打开浏览器...
start "" http://127.0.0.1:8774/
echo.
echo 页面已打开。看页面上的大字：
echo   橙色 = 还要挪，页面会直接写「往左挪」或「往右挪」
echo   绿色 = 站位正确，贴一条胶带记住这个位置
echo.
echo 页面打不开就看 .deepworks\tmp\aim_web.log 里的报错。
echo 关掉本窗口不影响页面。
echo.
timeout /t 5 >nul
