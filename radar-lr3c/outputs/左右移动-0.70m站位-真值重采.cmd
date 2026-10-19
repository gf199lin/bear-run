@echo off
chcp 65001 >nul
title 1T2R COM3 - left/right ground truth re-capture at the 0.70 m station

rem ==============================================================
rem  1T2R (DEV202H / COM3) - LEFT/RIGHT GROUND-TRUTH RE-CAPTURE
rem  AT THE 0.70 m STATION
rem
rem  WHY THIS FILE EXISTS
rem    The left/right decision is still the weakest accepted link.
rem    At 1.25-1.70 m the measured lateral displacement was 0.291 m
rem    against a noise p95 of 1.144 m (about 0.25x SNR) - the real
rem    signal is buried in the range/angle noise. The one station
rem    that concentrates the person's echo far better is 0.70 m:
rem    strongest echo gate j=1 (1.022 m) carries 83.9% of the
rem    window power, versus 27.3% at 1.70 m
rem    (.deepworks\tmp\station-v2\station-report.md, 2026-10-18).
rem    This run repeats the SAME protocol at 0.70 m so the numbers
rem    can be compared apples-to-apples with the 1.00/1.25/1.35 m
rem    outside samples.
rem
rem  PROTOCOL - IDENTICAL TO the existing 1T2R left/right
rem    outside-sample launcher in outputs\ (same timings, same 5x
rem    LEFT + 5x RIGHT ground truth) so the two runs compare.
rem    --reps 5 --prep 3 --lead 12 --settle 3 --rest 25
rem    --actions left,right   (5 x LEFT + 5 x RIGHT ground truth)
rem    --band is NOT a flag here: segment_capture.py derives it
rem    from the station, and 0.70 m falls back to
rem    ra.BAND_DEFAULT = (0.70, 1.35) - which CONTAINS gate 1
rem    (span 0.681-1.363 m), the gate where the 0.70 m station
rem    actually shows up. No band change needed or wanted.
rem
rem  GEOMETRY WARNING - READ THIS BEFORE YOU STAND THERE
rem    Horizontal FOV is +-45 deg, so at 0.70 m forward distance
rem    the edge of the beam is only about +-0.70 m sideways.
rem    A full step sideways (0.75 m) puts you AT or OUTSIDE the
rem    beam edge; "half a step to a step" - the prompt text used
rem    at every other station - is therefore borderline here.
rem    Keep the lateral move to about HALF A STEP, natural speed,
rem    no sprinting, and do not add forward/backward travel.
rem    (Propagation to ~+-0.70 m at 0.70 m is a proposal to be
rem     confirmed by this very run - if the echo is lost mid-move
rem     the trial is invalid and must be repeated closer in.)
rem    No phone / keys / metal in front of the radar.
rem
rem  BOARD  1T2R / DEV202H, CP2102 bridge, COM3, 921600 8-N-1
rem         (--baud is left EMPTY ON PURPOSE: auto-fingerprint)
rem  TOOL   radar-pointcloud\segment_capture.py
rem  OUT    .deepworks\tmp\seg-070\%TAG%\d0.70\seg_01.00.bin
rem         .deepworks\tmp\seg-070\%TAG%\d0.70\seg_01.00.labels.jsonl
rem         outputs\eval-070-%TAG%.md   (scoring report, written
rem         after the capture by eval_segments.py)
rem
rem  Chinese prompts come from the Python script (UTF-8); this
rem  launcher stays ASCII-only so cmd.exe never garbles them.
rem ==============================================================

cd /d "%~dp0.."

set TAG=
set /p TAG=Session tag (who/when, e.g. jiang-1019b) [default w070]: 
if "%TAG%"=="" set TAG=w070

set OUTDIR=.deepworks\tmp\seg-070\%TAG%\d0.70

echo.
echo ============================================================
echo   0.70 m STATION - LEFT/RIGHT GROUND TRUTH  -  about 3 min
echo ------------------------------------------------------------
echo   BEFORE YOU START
echo     Tape-measure 0.70 m FROM THE FRONT FACE OF THE RADAR and
echo     put a mark down. Both feet on the mark, facing the radar.
echo     Tell the assistant your real measured distance - it goes
echo     into the record. Do not trust a guessed distance.
echo
echo   WHAT HAPPENS
echo     The script prompts you, then WAITS for ENTER.
echo       PREP cue -^> step back to centre, stand still
echo       BEEP    -^> move ONCE in the announced direction and
echo                  HOLD that position for about 2.5 s
echo       STOP cue -^> back to centre, wait for the next prompt
echo     Order is 5 x LEFT, then 5 x RIGHT, then a 25 s silent
echo     rest window at the end. Any event in that silent window
echo     is a FALSE ALARM and is counted.
echo
echo   MOVE SIZE: about HALF A STEP sideways, natural speed.
echo   At 0.70 m the beam edge is only ~0.70 m sideways - a full
echo   step goes outside it.
echo
echo   EXPECTED BANNER (ASCII tokens, must be present):
echo       1t2r     0.1295 s/frame     0.6813 m/gate
echo       band (0.7, 1.35)
echo     If it reports 2t4r, COM3 is not the 1T2R board any more.
echo
echo   OUTPUT: %OUTDIR%
echo   Close the point-cloud viewer and ONELAB first - the guard
echo   below offers to close OUR viewer only, never a foreign one.
echo ============================================================
echo.

echo [preflight] serial ports seen right now:
python -u -c "import serial.tools.list_ports as lp; [print('   ', p.device, '-', p.description) for p in lp.comports()]"

echo.
echo [preflight] board fingerprint on COM3 (expected: 1t2r):
python -u -c "import sys; sys.path.insert(0, 'radar-pointcloud'); import radar_source as rs; print('   ', rs.probe_board('COM3'))"

echo.
echo [guard] can COM3 actually be opened right now?
python -u "radar-pointcloud\free_com.py" COM3 --baud 921600
if not errorlevel 1 goto port_free

echo ============================================================
echo   COM3 IS BUSY - another program is holding the port.
echo   Most likely our own point-cloud viewer (service on 8099),
echo   or ONELAB, or another terminal still reading COM3.
echo ------------------------------------------------------------
echo   Y = close this project's viewer and continue
echo   N = stop here
echo   No answer within 20 seconds is treated as Y and continues.
echo ============================================================
choice /c YN /n /t 20 /d Y /m "  Close the viewer and continue? [Y/N] "
if errorlevel 2 goto port_stuck

echo.
echo [guard] closing this project's viewer...
python -u "radar-pointcloud\free_com.py" COM3 --baud 921600 --kill
python -u "radar-pointcloud\free_com.py" COM3 --baud 921600
if errorlevel 1 goto port_stuck

:port_free
echo [guard] COM3 is free - going ahead.

echo.
echo [capture] starting... press ENTER when the script asks.
echo.
python -u "radar-pointcloud\segment_capture.py" ^
  --source serial ^
  --port COM3 ^
  --distances 0.70 ^
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
echo   CAPTURE DONE. Scoring it now (1000-permutation test)...
echo ============================================================
python -u ".deepworks\tmp\eval_segments.py" --dir ".deepworks\tmp\seg-070\%TAG%" --perm 1000 --out "outputs\eval-070-%TAG%.md"
set RC2=%ERRORLEVEL%

echo.
echo ============================================================
echo   FINISHED.
echo     raw data : %OUTDIR%
echo     scoring  : outputs\eval-070-%TAG%.md
echo.
echo   Tell the assistant it is done - it will read the scoring
echo   report and decide ONE thing:
echo     the 0.70 m lateral SNR is clearly better than the
echo     1.25-1.70 m baseline AND real events win on independent
echo     samples -^> 0.70 m becomes the demo station, and the
echo     0.70 m rest thresholds get merged into calib.json.
echo     SNR is not better, or false alarms rise -^> 0.70 m is
echo     dropped as a candidate, and that is a valid result too.
echo   This report is NOT an accuracy claim until the assistant
echo   has read it against calib.json and the sample count
echo   (extra samples only - never a re-run of the same samples).
echo ============================================================
goto end

:capture_failed
echo ============================================================
echo   CAPTURE FAILED (exit code %RC%)
echo ------------------------------------------------------------
echo   Most common causes, in order:
echo     1) COM3 held by another program (viewer service 8099,
echo        ONELAB, another terminal). Close it.
echo     2) ENTER was not pressed - the script waits for it.
echo     3) COM3 is no longer the CP2102 board. Check Device
echo        Manager: Silicon Labs CP210x USB to UART Bridge.
echo     4) Board not powered, or a charge-only USB cable.
echo   Reminder: the 1T2R bridge is the CP2102 one and needs the
echo   CP210x driver. The 2T4R bridge is FTDI (no driver needed).
echo ============================================================

:port_stuck
echo.
echo ============================================================
echo   COM3 is still not openable - NOTHING was captured.
echo ------------------------------------------------------------
echo   Close whatever holds COM3 and run this file again:
echo     - our point-cloud viewer window (or its service on 8099)
echo     - ONELAB / the vendor tool
echo     - another terminal that is still reading COM3
echo ============================================================
goto end

:end
echo.
echo [finished] press any key to close.
pause >nul
