@echo off
chcp 65001 >nul
title 1T2R COM3 - LEFT/RIGHT re-capture at 0.70 m and 1.00 m (10 reps each)

rem ==============================================================
rem  1T2R (DEV202H / COM3) - LEFT/RIGHT RE-CAPTURE, 0.70 m + 1.00 m
rem
rem  WHY THIS FILE EXISTS
rem    The game will be played with the person 0.7-1.0 m in front
rem    of the radar, so that is the only range worth tuning. The
rem    requirement sheet's 1.50 m main station is NOT reachable in
rem    this room and is not what the demo uses. Two stations here:
rem    0.70 m and 1.00 m.
rem
rem    Measured 2026-10-18 (.deepworks\tmp\station-v2\station-report.md)
rem    - how concentrated the person's echo is in ONE gate:
rem        0.70 m -> gate j=1 (1.022 m) holds 83.9% of window power
rem        1.00 m -> 97.0%   1.35 m -> 69.5%   1.70 m -> 27.3%
rem      So the near stations are the good ones for LEFT/RIGHT.
rem
rem  PROTOCOL - deliberately close to the 2026-10-15 baseline so the
rem  numbers stay comparable, with ONE change: --lead raised to 30 s.
rem      --actions left,right   --reps 10   --prep 3   --settle 3
rem      --lead 30   requirement sheet v2 asks for a full 30 s
rem                  stand-still baseline. Auto-band also needs
rem                  >=30 frames AND >=20 points AND the strongest
rem                  gate to beat the runner-up by 1.5x; the silent
rem                  fallback is at frame 120 (= 15.5 s), and a
rem                  segment shorter than that can end up never
rem                  locking at all. 30 s covers both safely.
rem      --rest 25   tail standstill. Every lane event inside it
rem                  counts as a FALSE ALARM - that is half the
rem                  value of this run.
rem      --no-compare   the tool's own compare table uses the code
rem                  DEFAULT criterion, which on this data gives 0
rem                  hits plus false alarms (devlog 7.28). Scoring
rem                  is done by eval_segments.py below instead.
rem
rem  OUT  .deepworks\tmp\seg-lr2\<tag>\d0.70\
rem       .deepworks\tmp\seg-lr2\<tag>\d1.00\
rem       Explicit outdir on purpose: the default outdir
rem       (.deepworks\tmp\seg) has already destroyed one dataset.
rem
rem  BOARD  1T2R / DEV202H, CP2102 bridge, COM3, 921600 8-N-1.
rem         --baud is left EMPTY on purpose: the tool fingerprints
rem         the board and picks 921600 / 1500000 itself.
rem
rem  Chinese prompts come from the Python side (UTF-8). This
rem  launcher stays ASCII-only so cmd.exe never garbles them.
rem ==============================================================

cd /d "%~dp0.."

echo.
echo ============================================================
echo   LEFT/RIGHT RE-CAPTURE  -  0.70 m and 1.00 m  -  about 8 min
echo ------------------------------------------------------------
echo   BEFORE YOU START
echo     1) Close ONELAB and our point-cloud viewer. The serial
echo        port is exclusive - two readers break it.
echo     2) Tape TWO floor marks, measured with a tape measure
echo        from the FRONT FACE of the radar: 0.70 m and 1.00 m.
echo        Both feet on the mark, body facing the radar.
echo     3) Quiet room, no metal in front of the radar.
echo ------------------------------------------------------------
echo   WHAT YOU WILL HEAR, PER STATION
echo     - "please stand still": STAND STILL, do not shift your
echo       weight. 30 s. This is the baseline AND the warm-up
echo       that lets the tool lock the right range gate.
echo     - then 10 cues. On each cue: ONE lateral step to the
echo       side and HOLD it. 5 LEFT, then 5 RIGHT. Move at a
echo       natural speed, about half a step to a step, no lunge.
echo     - between cues, walk back to the middle and stand still.
echo     - after the last cue there is a 25 s standstill window.
echo       Any lane event there is a FALSE ALARM. Do not move.
echo   Total about 4 minutes per station.
echo ============================================================
echo.

set "TAG="
set /p "TAG=Person / session tag (e.g. p1) [default p1]: "
if "%TAG%"=="" set "TAG=p1"

set "PORT="
set /p "PORT=Serial port [default COM3]: "
if "%PORT%"=="" set "PORT=COM3"

set "OUTBASE=.deepworks\tmp\seg-lr2\%TAG%"

echo.
echo ============================================================
echo   tag    = %TAG%
echo   port   = %PORT%
echo   output = %OUTBASE%\d0.70   and   %OUTBASE%\d1.00
echo ============================================================
echo.

echo [preflight] serial ports seen right now:
python -u -c "import serial.tools.list_ports as lp; [print('   ', p.device, '-', p.description) for p in lp.comports()]"

echo.
echo [preflight] board fingerprint (expected: 1t2r):
python -u -c "import sys; sys.path.insert(0, 'radar-pointcloud'); import radar_source as rs; print('   ', rs.probe_board('%PORT%'))"

echo.
echo [guard] is %PORT% openable right now?
python -u "radar-pointcloud\free_com.py" %PORT% --baud 921600
if not errorlevel 1 goto port_ok

echo ============================================================
echo   %PORT% IS BUSY - another program is holding the port.
echo   Most likely our own point-cloud viewer (service on 8099),
echo   or ONELAB, or another terminal still reading it.
echo ------------------------------------------------------------
echo   Y = close this project's viewer and continue
echo   N = stop here
echo   No answer within 20 seconds is treated as Y and continues.
echo ============================================================
choice /c YN /n /t 20 /d Y /m "  Close the viewer and continue? [Y/N] "
if errorlevel 2 goto port_stuck

echo.
echo [guard] closing this project's viewer...
python -u "radar-pointcloud\free_com.py" %PORT% --baud 921600 --kill
python -u "radar-pointcloud\free_com.py" %PORT% --baud 921600
if errorlevel 1 goto port_stuck

:port_ok
echo [guard] %PORT% is free - going ahead.
echo.

echo ============================================================
echo   STATION 1 of 2 :  0.70 m
echo ------------------------------------------------------------
echo   Stand on the 0.70 m mark NOW, then press any key.
echo   The recording opens with 30 s of pure standstill.
echo   DO NOT move during it.
echo ============================================================
pause >nul
echo.
echo [capture] station 0.70 m starting...
echo.

python -u "radar-pointcloud\segment_capture.py" ^
  --source serial ^
  --port %PORT% ^
  --distances 0.70 ^
  --actions left,right ^
  --reps 10 ^
  --prep 3 ^
  --lead 30 ^
  --settle 3 ^
  --rest 25 ^
  --outdir "%OUTBASE%\d0.70" ^
  --no-compare
set RC=%ERRORLEVEL%
if not "%RC%"=="0" goto capture_failed

echo.
echo ============================================================
echo   STATION 1 (0.70 m) DONE.
echo   Move to the 1.00 m mark, stand still, then press a key.
echo ============================================================
pause >nul
echo.
echo [capture] station 1.00 m starting...
echo.

python -u "radar-pointcloud\segment_capture.py" ^
  --source serial ^
  --port %PORT% ^
  --distances 1.00 ^
  --actions left,right ^
  --reps 10 ^
  --prep 3 ^
  --lead 30 ^
  --settle 3 ^
  --rest 25 ^
  --outdir "%OUTBASE%\d1.00" ^
  --no-compare
set RC=%ERRORLEVEL%
if not "%RC%"=="0" goto capture_failed

echo.
echo ============================================================
echo   BOTH STATIONS CAPTURED.
echo   Now scoring both folders (auto-band + drift + top_k 1).
echo ============================================================
echo.
python -u ".deepworks\tmp\eval_segments.py" --dir "%OUTBASE%" --perm 1000 --out "outputs\eval-lr2-%TAG%.md"

echo.
echo ============================================================
echo   DONE.
echo     report : outputs\eval-lr2-%TAG%.md
echo     data   : %OUTBASE%
echo ------------------------------------------------------------
echo   Tell the assistant it is finished. It will read the report
echo   and the raw segments in %OUTBASE%, then work on the two
echo   goals for this dataset:
echo     (a) hit rate up at 0.70-1.00 m
echo     (b) decision latency down - today a lane change needs
echo         2-3 frames of confirmation = 260-390 ms at 7.75 Hz
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
echo     3) Board not powered, or a charge-only USB cable.
echo   Reminder: the 1T2R bridge is the CP2102 one and needs the
echo   CP210x driver. The old plan document saying "FTDI driver"
echo   is wrong.
echo ============================================================
goto end

:port_stuck
echo.
echo ============================================================
echo   %PORT% is still not openable - NOTHING was captured.
echo ------------------------------------------------------------
echo   Close whatever holds %PORT% and run this file again:
echo     - our point-cloud viewer window (or its service on 8099)
echo     - ONELAB / the vendor tool
echo     - another terminal that is still reading %PORT%
echo ============================================================

:end
echo.
echo [finished] press any key to close.
pause >nul
