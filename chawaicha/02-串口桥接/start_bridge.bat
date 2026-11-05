@echo off
chcp 65001 >nul
cd /d %~dp0
title Radar Bridge - keep this window open
node bridge.js %1
echo.
echo Bridge exited. If this was an accident, just double-click this file again.
pause
