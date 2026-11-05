@echo off
chcp 65001 >nul
cd /d "%~dp0..\..\.."
echo ==== Radar labeled capture: 6 segments x 15s, about 2.5 min ====
echo ==== Follow the countdown prompts in this window ====
node .deepworks\tmp\capture_labeled.js
echo.
pause
