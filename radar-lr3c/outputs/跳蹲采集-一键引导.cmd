@echo off
chcp 65001 >nul
title Radar capture - BOTH stations in one run (advanced) - COM3

rem ==============================================================
rem  Jump/Duck dataset, SESSION 2 (T1 re-measurement)
rem  ONE-SHOT variant: both stations (1.00 m then 1.50 m) in a
rem  single 8-minute run. Prefer the two STEP launchers instead
rem  if you want to check the 1.00 m result before walking back.
rem
rem  Output : .deepworks\tmp\seg-jd-v2\both\
rem
rem  DO NOT point --outdir at .deepworks\tmp\seg-jd : that folder is
rem  the frozen evidence cited in docs/progress-radar-pointcloud.md 7.23.
rem  It has been moved here on purpose. Do not move it back.
rem
rem  Chinese prompts are printed by the Python script (UTF-8);
rem  this launcher stays ASCII-only so cmd.exe never garbles it.
rem ==============================================================

cd /d "%~dp0.."

set OUTDIR=.deepworks\tmp\seg-jd-v2\both

if exist "%OUTDIR%\seg_01.00.bin" (
  echo.
  echo   WARNING: %OUTDIR%\seg_01.00.bin already exists.
  echo   Re-running will REPLACE it. If it is a good capture, press
  echo   Ctrl+C NOW to keep it, or rename / move it first.
  echo.
  pause
)

echo.
echo ============================================================
echo   ONE-SHOT run: DUCK x12 then JUMP x12, at 1.00 m AND 1.50 m
echo ------------------------------------------------------------
echo   Stand 1.00 m first. After a beep sequence the voice tells
echo   you when to walk back to 1.50 m, then it repeats.
echo.
echo   DUCK : squat down in place and hold
echo   JUMP : jump in place, land, stay put - do NOT step
echo   At the end of EACH station: 25 s of pure stillness.
echo   DO NOT MOVE during it: every false alarm counts.
echo.
echo   MOST IMPORTANT: put both feet on the SAME floor mark
echo   every single time. Tape down marks for BOTH distances.
echo   Close the viewer / ONELAB first: COM3 is exclusive.
echo.
echo   Total about 8 minutes, unattended.
echo ============================================================
echo.

python -u "radar-pointcloud\segment_capture.py" ^
  --port COM3 ^
  --auto ^
  --outdir ".deepworks\tmp\seg-jd-v2\both" ^
  --distances 1.0,1.5 ^
  --actions duck,jump ^
  --reps 12 ^
  --walk-time 12 ^
  --start-delay 45

echo.
echo [run finished] both stations captured in this folder.
pause >nul
