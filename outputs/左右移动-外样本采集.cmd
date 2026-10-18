@echo off
chcp 65001 >nul
title OUTSIDE-SAMPLE capture - left/right lane movement

rem ==============================================================
rem  OUTSIDE-SAMPLE CAPTURE  -  the highest-priority unverified risk
rem
rem  WHY THIS FILE EXISTS
rem    Every existing left/right number comes from 2026-10-15:
rem    2 stations, 1 operator, 1 day, 20 truth windows. The code
rem    path works on that data. Whether it still works for
rem    ANOTHER person, ANOTHER day, ANOTHER station is NOT tested
rem    yet. That is the point of this file.
rem
rem  WHAT "OUTSIDE SAMPLE" MEANS (be strict about this)
rem    Change at least two of: the person, the day, the station.
rem    Re-recording the same person at the same spot on the same
rem    afternoon is NOT an outside sample - it just re-measures
rem    the sample you already tuned on.
rem
rem  PROTOCOL IS DELIBERATELY IDENTICAL TO 2026-10-15
rem    --prep 3 --settle 3 --reps 5 --actions left,right
rem    So the only new variables are the person/day/station.
rem
rem  WHY --lead 12
rem    The recognizer picks its range gate automatically during
rem    warm-up. Warm-up needs >=30 frames AND >=20 detected points
rem    AND the strongest gate to beat the runner-up by 1.5x.
rem    If that never happens it falls back at frame 120 (= 15.5 s).
rem    A segment SHORTER than 120 frames can end up never locking
rem    at all (measured: an 89-frame synthetic file never locked).
rem    So the recording opens with 12 s of pure standstill, plus
rem    3 s of "get into position" => about 16 s before your first
rem    movement. Do not skip it, and do not move during it.
rem ==============================================================

cd /d "%~dp0.."

echo.
echo ============================================================
echo   OUTSIDE-SAMPLE capture: left/right lane movement
echo ------------------------------------------------------------
echo   BEFORE YOU START
echo     1) Close ONELAB and any other program using the serial
echo        port. The port is exclusive - two readers break it.
echo     2) Plug in the DEV202H board (CP2102 driver).
echo     3) Mark the standing spot on the floor. Measure from the
echo        radar face. 0.70 m is the documented minimum.
echo     4) Pick a day / a person / a station that is DIFFERENT
echo        from 2026-10-15. Otherwise you are re-measuring the
echo        sample the thresholds were tuned on.
echo     5) Quiet room, no metal in front of the radar.
echo ============================================================
echo.

set ST=
set /p ST=Standing distance in metres [default 1.25]: 
if "%ST%"=="" set ST=1.25

set TAG=
set /p TAG=Session tag (who/when, e.g. wang-1019) [default ext]: 
if "%TAG%"=="" set TAG=ext

set PORT=
set /p PORT=Serial port [default COM3]: 
if "%PORT%"=="" set PORT=COM3

set OUTDIR=.deepworks\tmp\seg-ext\%TAG%\d%ST%

echo.
echo ============================================================
echo   station = %ST% m     tag = %TAG%     port = %PORT%
echo   output  = %OUTDIR%
echo ------------------------------------------------------------
echo   WHAT WILL HAPPEN
echo     - You will hear "please stand still for 12 seconds".
echo       STAND STILL. Do not shift your weight, do not wave.
echo       This is the warm-up AND the rest baseline.
echo     - Then, 10 times: a cue, then a beep. On the beep, do
echo       ONE movement and HOLD it for about 2.5 s.
echo       5 LEFT, then 5 RIGHT. Move a half step to a step,
echo       natural speed, do not lunge.
echo     - Between movements, return to the middle and stand
echo       still. - After the last movement there is 25 s of
echo       pure standstill. Every event in that window counts
echo       as a FALSE ALARM. Do not move.
echo     - Total about 2.5 minutes. One station per run.
echo.
echo   Then run again for a second station if you can.
echo ============================================================
echo.

echo [capture] starting... if the port is wrong this will fail fast.
echo.
python -u "radar-pointcloud\segment_capture.py" ^
  --source serial ^
  --port %PORT% ^
  --distances %ST% ^
  --actions left,right ^
  --reps 5 ^
  --prep 3 ^
  --lead 12 ^
  --settle 3 ^
  --rest 25 ^
  --outdir "%OUTDIR%" ^
  --no-compare
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" goto capture_failed

echo ============================================================
echo   Capture finished. Now scoring this folder with the
echo   recommended configuration (auto-band + drift + top_k 1).
echo.
echo   NOTE: the capture tool's own compare table is DISABLED on
echo   purpose (--no-compare). That table uses the code DEFAULT
echo   criterion, and on this data the default gives 0 hits plus
echo   false alarms (devlog section 7.28). Showing it would just
echo   mislead. The evaluator below uses the configuration that
echo   actually works.
echo ============================================================
echo.
python -u ".deepworks\tmp\eval_segments.py" --dir ".deepworks\tmp\seg-ext\%TAG%" --perm 1000 --out "outputs\eval-ext-%TAG%.md"

echo.
echo ============================================================
echo   NEXT STEP (the part that actually decides something)
echo ------------------------------------------------------------
echo   Look at the report. Two things to check, in this order:
echo.
echo     1) Did auto-band lock the RIGHT gate?
echo        Push it: station 1.00-1.35 m should lock gate j=2
echo        (there is a constant +0.5 m range offset). A wrong
echo        gate shows up as a burst of false alarms.
echo     2) Recall per side, and false alarms together.
echo        A hit rate without the false-alarm count means
echo        nothing.
echo.
echo   If LEFT is 0/N again, check the DETECTION COUNT in that
echo   station's LEFT windows before touching any threshold -
echo   historically LEFT 0/5 at 1.50 m was "cannot see it", not
echo   "judged the wrong way".
echo.
echo   All thresholds in this project are PROPOSALS pending group
echo   confirmation. Do not quote a hit rate as "accuracy".
echo ============================================================
goto end

:capture_failed
echo ============================================================
echo   CAPTURE FAILED (exit code %RC%)
echo ------------------------------------------------------------
echo   Most common causes, in order:
echo     1) The serial port is held by another program (ONELAB).
echo     2) Wrong port name. Check Windows Device Manager for the
echo        CP2102 entry - it is not always COM3.
echo     3) Board not powered / not plugged in.
echo   Remember: the CP2102 needs the CP210x driver. The old plan
echo   document saying "FTDI driver" is wrong.
echo ============================================================

:end
echo.
echo [finished] press any key to close.
pause >nul
