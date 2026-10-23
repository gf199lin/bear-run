@echo off
rem ---------------------------------------------------------------------------
rem  Re-capture for the "standing still but judged LEFT" investigation.
rem  Design notes: the diagnosis report added to docs/ on 2026-10-21
rem  ("standing still, judged as moving LEFT - left gate vs rest noise"),
rem  section 6.2 (re-capture spec). Keep this file ASCII-only.
rem
rem  Preset: station 0.70 m / rest 300 s (5 min) / 10 reps per direction
rem  Output : ..\.deepworks\tmp\m2-recap2
rem           (a NEW folder on purpose: do NOT mix with the earlier
rem            m2-recap captures, their station/band values differ)
rem
rem  Why these numbers
rem    - rest >= 5 min : the 366 s rest captured at 0.70 m was the only
rem      usable baseline; the 1.50 / 2.00 m rests were only ~35 s, which
rem      makes any false-positive-per-minute number meaningless.
rem    - 10 reps       : the 0.70 m LEFT side had only 2 measurable windows,
rem      far too few to re-calibrate a threshold from.
rem    - station 0.70 m: keeps the new data comparable with the existing
rem      cap-long-1020 set AND is the station used for the demo.
rem
rem  The tool still asks for: serial port, board type, minimum stride.
rem  Press Enter to accept each default.
rem
rem  Keep this file ASCII-only: cmd.exe reads it in the local ANSI codepage,
rem  so Chinese text saved as UTF-8 shows up as garbage. All Chinese messages
rem  come from m2_checklist.py instead.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0..\radar-pointcloud"
title Re-capture - 0.70 m / rest 5 min / 10 reps per side

set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py"
)
if not defined PY (
  echo [ERROR] Python was not found in PATH.
  echo Install Python 3 from https://www.python.org and tick
  echo "Add python.exe to PATH" during setup, then run this file again.
  echo.
  pause
  exit /b 1
)

set "OUTDIR=..\.deepworks\tmp\m2-recap2"

echo ===========================================================================
echo  Re-capture: standing-still-judged-LEFT investigation
echo  Station 0.70 m / rest 300 s per station / 10 reps per direction
echo  Output folder: %OUTDIR%
echo.
echo  BEFORE STARTING
echo    1. Floor tape: 0 point = rest position, marks every 10 cm,
echo       out to +/- 0.6 m on both sides.
echo    2. Nobody else walks around the room during the whole session.
echo    3. Do not move the radar for the whole session.
echo.
echo  STEPS (the tool asks before each one)
echo    0. environment + serial link check
echo    2. station / range-band calibration
echo    3. REST 300 s per station        ^<- the important new part
echo    4. left / right actions, 10 reps each (voice + beeps)
echo    5. real displacement entry (dx_cm, read from the tape)
echo    7. decisive regression
echo.
echo  NOTE  the station is preset to 0.7 m. To capture a second station
echo        (e.g. 1.25 m) instead, run m2_checklist.py directly:
echo          python m2_checklist.py --step session --dir %OUTDIR%
echo ===========================================================================
echo Press any key to start (Ctrl+C to abort).
pause >nul

"%PY%" m2_checklist.py --step session --dir "%OUTDIR%" --preset-stations 0.7 --preset-rest-sec 300 --preset-reps 10
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo Session stopped with code %RC%. Read the message above.
if "%RC%"=="0" echo Session finished. Capture and labels are in %OUTDIR%
echo Press any key to close this window.
pause >nul
exit /b %RC%
