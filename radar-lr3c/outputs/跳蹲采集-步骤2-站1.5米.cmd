@echo off
chcp 65001 >nul
title Radar capture - STEP 2/2 - stand 1.50 m - DUCK x12 then JUMP x12

rem ==============================================================
rem  Jump/Duck dataset, SESSION 2 (T1 re-measurement) - STEP 2 of 2
rem  Station : 1.50 m in front of the radar
rem  Output  : .deepworks\tmp\seg-jd-v2\m1.5\
rem
rem  Run STEP 1 (1.00 m) FIRST, in a separate window.
rem  Chinese prompts are printed by the Python script (UTF-8);
rem  this launcher stays ASCII-only so cmd.exe never garbles it.
rem
rem  DO NOT point --outdir at .deepworks\tmp\seg-jd : that folder is
rem  the frozen evidence cited in docs/progress-radar-pointcloud.md 7.23.
rem ==============================================================

cd /d "%~dp0.."

set OUTDIR=.deepworks\tmp\seg-jd-v2\m1.5

if exist "%OUTDIR%\seg_01.50.bin" (
  echo.
  echo   WARNING: %OUTDIR%\seg_01.50.bin already exists.
  echo   Re-running will REPLACE it. If it is a good capture, press
  echo   Ctrl+C NOW to keep it, or rename / move it first.
  echo.
  pause
)

echo.
echo ============================================================
echo   STEP 2/2  -  stand 1.50 m in front of the radar
echo ------------------------------------------------------------
echo   Each repetition (about 8.5 s):
echo     "prepare"      3.0 s  - body centred, completely still
echo     BEEP(high)     2.5 s  - do the action and HOLD it
echo     BEEP(low)      3.0 s  - back to centre, stay still
echo   A voice announces which action comes next.
echo.
echo   DUCK : squat down in place and hold
echo   JUMP : jump in place, land, stay put - do NOT step
echo.
echo   At the very end there is 25 s of pure stillness.
echo   DO NOT MOVE during it: every false alarm counts.
echo.
echo   MOST IMPORTANT: put both feet on the SAME floor mark
echo   every single time. Tape it down if you can.
echo   Close the viewer / ONELAB first: COM3 is exclusive.
echo.
echo   Total about 4 minutes. Doing 12 + 12 repetitions.
echo ============================================================
echo.

python -u "radar-pointcloud\segment_capture.py" ^
  --port COM3 ^
  --auto ^
  --outdir ".deepworks\tmp\seg-jd-v2\m1.5" ^
  --distances 1.5 ^
  --actions duck,jump ^
  --reps 12 ^
  --walk-time 12 ^
  --start-delay 45

echo.
echo [STEP 2 finished] both stations are done.
echo Tell the assistant: the data is ready.
pause >nul
