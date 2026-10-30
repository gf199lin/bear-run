@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "NODE="
if exist "%~dp0radar-agent\runtime\node.exe" set "NODE=%~dp0radar-agent\runtime\node.exe"
if not defined NODE (
  for /f "delims=" %%I in ('where node 2^>nul') do if not defined NODE set "NODE=%%I"
)
if not defined NODE if exist "%ProgramFiles%\nodejs\node.exe" set "NODE=%ProgramFiles%\nodejs\node.exe"

if not defined NODE (
  echo.
  echo [x] Node runtime not found ^(radar-agent\runtime\node.exe^).
  echo     You can also run: python -m http.server 8801
  echo.
  pause
  exit /b 1
)

start "" "http://127.0.0.1:8801/radar-connect.html"
"%NODE%" "%~dp0serve.js" 8801
echo.
echo Static server stopped.
pause
