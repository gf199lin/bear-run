@echo off
rem ---------------------------------------------------------------------------
rem  M2 field session - one guided line, no menu navigation needed
rem
rem  Steps run in order (each one asks for its own parameters):
rem    0. environment + serial link check
rem    2. station / range-band calibration  (live, 20 s)
rem    3. rest capture at the new station   (30 s per station)
rem    4. left/right action capture          (--auto, voice + beeps)
rem    5. real displacement entry            (dx_cm, from floor tape marks)
rem    7. decisive regression: dx vs measured dev
rem
rem  Data goes to  ..\.deepworks\tmp\m2-recap  (a NEW folder on purpose:
rem  do not mix 0.7 m and 1.5/2.0 m captures in one directory).
rem
rem  Keep this file ASCII-only: cmd.exe reads it in the local ANSI codepage,
rem  so Chinese text saved as UTF-8 would show up as garbage. All Chinese
rem  messages come from m2_checklist.py instead.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
title M2 field session - radar point cloud

set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py"
)
if not defined PY (
  echo [ERROR] Python was not found in PATH.
  echo Please install Python 3 from https://www.python.org and tick
  echo "Add python.exe to PATH" during setup, then run this file again.
  echo.
  pause
  exit /b 1
)

set "OUTDIR=..\.deepworks\tmp\m2-recap"

echo ===========================================================================
echo  M2 field session
echo  Output folder: %OUTDIR%
echo  Before step 4: floor tape ready, 0 point = rest position, 10 cm marks
echo ===========================================================================
echo.

"%PY%" m2_checklist.py --step session --dir "%OUTDIR%"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo The session stopped with code %RC%. Read the message above.
if "%RC%"=="0" echo Session finished. Capture and labels are in %OUTDIR%
echo Press any key to close this window.
pause >nul
exit /b %RC%
