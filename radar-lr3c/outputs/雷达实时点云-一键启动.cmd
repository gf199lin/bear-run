@echo off
rem ---------------------------------------------------------------------------
rem  Radar live point-cloud viewer - one-click launcher (deliverable copy).
rem  Double-click this file to start the viewer and open it in the browser.
rem  It simply calls the launcher that lives next to the code:
rem      ..\radar-pointcloud\start_viewer.cmd
rem  Keep this file ASCII-only (see start_viewer.cmd for the reason).
rem ---------------------------------------------------------------------------
setlocal
if exist "%~dp0..\radar-pointcloud\start_viewer.cmd" goto found
echo [ERROR] Cannot find ..\radar-pointcloud\start_viewer.cmd
echo.
echo This launcher must stay inside the project (outputs folder).
echo Please use this one instead:
echo     ^<project^>\radar-pointcloud\start_viewer.cmd
echo.
pause
exit /b 1

:found
call "%~dp0..\radar-pointcloud\start_viewer.cmd"
