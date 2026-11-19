@echo off
chcp 936 >nul
title 个人校准 · 引导采集

set URL=http://127.0.0.1:8778/calib-guide.html

netstat -an | findstr ":8778" | findstr "LISTENING" >nul
if errorlevel 1 (
  echo.
  echo   [x] 校准页打不开：分配台没有在跑。
  echo.
  echo   请先双击  启动-双雷达游戏.cmd
  echo   等窗口里出现两块板都「已监听」之后，再运行本文件。
  echo.
  pause
  exit /b 1
)

echo.
echo   正在打开校准页…
echo   %URL%
echo.
echo   注意：一定要用这个 8778 地址采集，
echo   换成 8080 会存到另一份浏览器存储里，游戏页读不到。
echo.
start "" "%URL%"
timeout /t 2 >nul
exit /b 0
