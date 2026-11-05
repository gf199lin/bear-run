@echo off
chcp 65001 >nul
cd /d %~dp0
title Radar Fusion - keep this window open
node radar_fusion.js %*
echo.
echo Fusion exited. Double-click this file again to restart.
pause
