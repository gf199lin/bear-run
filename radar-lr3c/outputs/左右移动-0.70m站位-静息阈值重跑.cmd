@echo off
chcp 65001 >nul
title 1T2R COM3 - rest threshold re-run at the 0.70 m station

rem ==============================================================
rem  1T2R (DEV202H / COM3) - REST THRESHOLD RE-RUN, 0.70 m STATION
rem
rem  WHY THIS FILE EXISTS
rem    The 0.70 m station is the only candidate that improves the
rem    left/right signal WITHOUT changing hardware and WITHOUT
rem    changing the interaction model. Measured on 2026-10-18
rem    (.deepworks\tmp\station-v2\station-report.md):
rem      0.70 m -> strongest echo gate j=1 (1.022 m), 83.9% of the
rem                window power in ONE gate
rem      1.00 m -> 97.0%   1.35 m -> 69.5%   1.70 m -> 27.3%
rem    So 0.70 m concentrates the person's echo the most.
rem    Before re-capturing left/right there, re-measure the STILL
rem    thresholds AT THAT STATION: v_on / v_off / min_dx /
rem    max_step are only valid for the geometry they were taken at.
rem
rem  BAND - ALREADY CORRECT, DO NOT CHANGE
rem    --band 0.70 1.35 is both the tool default (ra.BAND_DEFAULT)
rem    and the band that CONTAINS gate 1 (span 0.681-1.363 m,
rem    centre 1.022 m) where the 0.70 m station actually shows up.
rem    Historical note: this band was called "completely useless"
rem    on 10/15 - that was true for the 1.25 m data (person sat in
rem    gate j=2, i.e. 1.70 m), not for the 0.70 m station.
rem
rem  BOARD  1T2R / DEV202H, CP2102 bridge, COM3, 921600 8-N-1
rem         (--baud is left EMPTY ON PURPOSE: the tool fingerprints
rem          the board and picks 921600 / 1500000 automatically)
rem  TOOL   radar-pointcloud\radar_calibrate.py  --mode rest
rem  IN     radar-pointcloud\calib.json  (used as the base draft)
rem  OUT    .deepworks\tmp\calib-070-rest.json
rem         *** calib.json IS NOT OVERWRITTEN *** - a separate
rem         output file is written so the current demo config stays
rem         intact until the numbers have been compared.
rem
rem  WRITES NOTHING TO THE RADAR. Pure measurement.
rem
rem  Chinese prompts come from the Python script (UTF-8); this
rem  launcher stays ASCII-only so cmd.exe never garbles them.
rem ==============================================================

cd /d "%~dp0.."

set OUT=.deepworks\tmp\calib-070-rest.json

echo.
echo ============================================================
echo   0.70 m STATION - REST THRESHOLD RE-RUN  -  about 1 minute
echo ------------------------------------------------------------
echo   WHAT YOU DO
echo     1) Stand at 0.70 m from the FRONT FACE OF THE RADAR,
echo        measured with a tape measure, both feet on the mark,
echo        body facing the radar.
echo     2) Run this file. The script prints a 3 s countdown.
echo     3) From "go" onward STAND STILL for 20 s. Breathe
echo        normally - do not step, sway, wave or shift weight.
echo        Any lane event in this window is counted as a FALSE
echo        ALARM, and that count is half the value of this run.
echo
echo   EXPECTED BANNER (ASCII tokens, must be present):
echo        mode=rest   serial COM3   band (0.7, 1.35)
echo     and NO "1t2r/2t4r" surprise. If it reports 2t4r, COM3 is
echo     not the 1T2R board any more.
echo
echo   RESULT FILE: %OUT%
echo     It contains v_on / v_off / min_dx / max_step /
echo     noise / threshold_confidence / rest_false_positives plus
echo     the DSP params that were actually in force.
echo     radar-pointcloud\calib.json is left untouched on purpose.
echo
echo   Do NOT run this while the point-cloud viewer or ONELAB is
echo   holding COM3 - the guard below will offer to close OUR
echo   viewer only, never a foreign program. The guard is called
echo   with --baud 921600 on purpose: free_com.py defaults to
echo   1500000 (the 2T4R rate), and a baud the CP2102 rejects
echo   would be misread as "port busy".
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
echo [capture] starting... if the port is wrong this fails fast.
echo.
python -u "radar-pointcloud\radar_calibrate.py" ^
  --mode rest ^
  --port COM3 ^
  --band 0.70 1.35 ^
  --seconds 20 ^
  --calib "radar-pointcloud\calib.json" ^
  --out "%OUT%"
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" goto capture_failed

echo ============================================================
echo   DONE. Result written to:
echo     %OUT%
echo.
echo   Tell the assistant it is finished - it will read that file
echo   and compare the fresh 0.70 m thresholds against the current
echo   calib.json values before anything is merged.
echo.
echo   NEXT STEP (only after this one is read):
echo     the other new 0.70 m launcher in outputs\ (the left/right
echo     ground-truth re-capture, not this rest-threshold one)
echo ============================================================
goto end

:capture_failed
echo ============================================================
echo   RE-RUN FAILED (exit code %RC%)
echo ------------------------------------------------------------
echo   Most common causes, in order:
echo     1) COM3 held by another program (viewer service 8099,
echo        ONELAB, another terminal). Close it.
echo     2) COM3 is no longer the CP2102 board - Windows can
echo        renumber it. Check Device Manager:
echo        Silicon Labs CP210x USB to UART Bridge.
echo     3) "no thresholds produced (not enough samples)" - the 20 s
echo        window held too
echo        few usable points - retry closer to the radar and let
echo        the window run without moving.
echo     4) Board not powered, or a charge-only USB cable.
echo   Reminder: the 1T2R bridge is the CP2102 one and needs the
echo   CP210x driver. The 2T4R bridge is FTDI (no driver needed).
echo ============================================================

:port_stuck
echo.
echo ============================================================
echo   COM3 is still not openable - NOTHING was measured.
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
