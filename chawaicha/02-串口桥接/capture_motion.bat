@echo off
chcp 65001 >nul
cd /d "%~dp0..\..\.."
echo ==========================================
echo  Motion capture: 5 segments x 12s
echo  Board must STAND UP, antenna facing you!
echo ==========================================
node .deepworks\tmp\capture_motion.js
if errorlevel 1 goto err
echo.
echo ALL DONE. Tell the assistant you finished.
pause
exit /b 0
:err
echo.
echo FAILED - check: close vendor app / Edge radar page, replug USB.
pause
exit /b 1
