@echo off
chcp 65001 >nul
cd /d "%~dp0..\..\.."
echo ==========================================
echo  A/B capture: raw first, then patched p110
echo  Board must STAND UP, antenna facing you!
echo ==========================================
node .deepworks\tmp\capture_ab.js raw
if errorlevel 1 goto err
echo.
echo ##########################################
echo  Phase 2: patched config (p110)
echo ##########################################
node .deepworks\tmp\capture_ab.js p110
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
