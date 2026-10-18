@echo off
chcp 65001 >nul
title Station self-check - LEFT/RIGHT prerequisite - stand 0.70 1.00 1.35 1.70 m

rem ==============================================================
rem  LEFT/RIGHT accuracy work - STEP 1 of 2 : STATION SELF-CHECK
rem
rem  Goal : find which RANGE GATE actually holds the person's echo.
rem  Why : the current band (0.70, 1.35) only catches ~9.6% of the
rem        echo power; ~90% sits in gate j=2 (1.70 m). Until the
rem        band is fixed, LEFT/RIGHT staying at 0 hits is expected.
rem  Tool : radar-pointcloud\position_capture.py
rem  Output : .deepworks\tmp\station-v2\
rem
rem  NO config is written to the radar. Nothing is changed on the
rem  device. This is a pure measurement.
rem
rem  DO NOT point --outdir at .deepworks\tmp\seg : that is the
rem  default outdir and it has already destroyed one dataset.
rem  Always name an output folder explicitly.
rem
rem  Chinese prompts come from the Python script (UTF-8); this
rem  launcher stays ASCII-only so cmd.exe never garbles it.
rem ==============================================================

cd /d "%~dp0.."

set OUTDIR=.deepworks\tmp\station-v2

if exist "%OUTDIR%" (
  echo.
  echo   NOTE: %OUTDIR% already exists - new files will replace
  echo   same-named files inside it. Press Ctrl+C to abort, or
  echo   any key to continue.
  echo.
  pause
)

echo.
echo ============================================================
echo   STATION SELF-CHECK  -  about 4-5 minutes
echo ------------------------------------------------------------
echo   You will STAND STILL at four distances, in this order:
echo        1)  0.70 m      2)  1.00 m
echo        3)  1.35 m      4)  1.70 m
echo.
echo   For each station the program says: "walk to mark N",
echo   then it WAITS FOR YOU TO PRESS ENTER, then beeps.
echo.
echo   From the moment you press ENTER: STAND STILL. There is a
echo   SILENT 3 s warm-up + 3 s countdown before the high beep -
echo   do not adjust your position during it.
echo.
echo   BEEP(high) = recording has started  -> STAND STILL
echo   BEEP(low)  = recording finished     -> walk to next mark
echo.
echo   Each station records 25 seconds of pure stillness
echo   (progress is announced every 5 s).
echo.
echo   *** DO NOT MOVE while recording. ***
echo   Any LANE event during these still periods counts as a
echo   FALSE ALARM and is the whole point of this measurement.
echo   Breathe normally, keep your feet planted.
echo.
echo   Put DOWN four floor marks BEFORE you start, e.g. with
echo   tape, and measure them with a tape measure from the
echo   front face of the radar. Both feet on the mark, body
echo   facing the radar.
echo.
echo   Close the viewer / ONELAB first: COM3 is exclusive.
echo   Do NOT run this together with anything else on COM3.
echo ============================================================
echo.

python -u "radar-pointcloud\position_capture.py" ^
  --source serial ^
  --port COM3 ^
  --stations 0.70,1.00,1.35,1.70 ^
  --duration 25 ^
  --warmup 3 ^
  --outdir ".deepworks\tmp\station-v2" ^
  --start-delay 45

echo.
echo [STEP 1 finished] the report is above:
echo   .deepworks\tmp\station-v2\station-report.md
echo Tell the assistant it is done - it will read the report.
pause >nul
