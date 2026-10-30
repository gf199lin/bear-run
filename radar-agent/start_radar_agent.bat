@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title radar-agent - keep this window open

rem Prefer the bundled Node (no install needed), then fall back to system Node.
set "NODE="
if exist "%~dp0runtime\node.exe" set "NODE=%~dp0runtime\node.exe"
if not defined NODE (
  for /f "delims=" %%I in ('where node 2^>nul') do if not defined NODE set "NODE=%%I"
)
if not defined NODE if exist "%ProgramFiles%\nodejs\node.exe" set "NODE=%ProgramFiles%\nodejs\node.exe"
if not defined NODE if exist "%LOCALAPPDATA%\Programs\nodejs\node.exe" set "NODE=%LOCALAPPDATA%\Programs\nodejs\node.exe"

if not defined NODE (
  echo.
  echo [x] Node runtime not found.
  echo     Keep radar-agent\runtime\node.exe next to this file,
  echo     or install Node.js and run again.
  echo.
  pause
  exit /b 1
)

echo Using Node: %NODE%
echo.
"%NODE%" "%~dp0agent.js" %*
echo.
echo radar-agent exited. Double-click this file to start it again.
pause
