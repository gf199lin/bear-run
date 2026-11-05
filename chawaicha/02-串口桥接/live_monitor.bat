@echo off
chcp 65001 >nul
cd /d "%~dp0..\..\.."
echo ==========================================
echo  LIVE MONITOR - watch the bars react
echo  Board must STAND UP, antenna facing you!
echo  Ctrl+C to quit (board auto-sleeps)
echo ==========================================
node .deepworks\tmp\live_monitor.js
pause
