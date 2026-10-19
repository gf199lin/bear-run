@echo off
chcp 65001 >nul
title 2T4R station tape-measure check - COM4 - stand 0.70 1.00 1.35 1.70 m

rem ==============================================================
rem  STEP 1 of 2 - 2T4R (DEV113P) STATION TAPE-MEASURE CHECK
rem
rem  WHY THIS FILE EXISTS
rem    Every 2T4R range number so far is DERIVED, not measured.
rem    range_res = c / (2*B) with B = 999 MHz read out of the
rem    vendor Radar_Config.ini gives 0.150 m. It was cross-checked
rem    by feeding the same formula back into the 1T2R config
rem    (220 MHz -> 0.6813 m, which matches radar_dsp.py and the
rem    1T2R station log to the digit). The 2T4R number itself has
rem    NEVER been checked against a tape measure. This run is
rem    that check, and it is the last open item in the 2T4R
rem    bring-up.
rem
rem  HOW THE RESULT IS READ
rem    If range_res = 0.150 m is right, the strongest echo gate
rem    must step by ONE gate every 0.15 m of real walking:
rem      0.70 m -> gate ~4      1.00 m -> gate ~6
rem      1.35 m -> gate ~9      1.70 m -> gate ~11
rem    (gate centre = (j + 0.5) * 0.150 m, and 1T2R showed a
rem    NON-CONSTANT +0.3..0.7 m offset, so judge the STEP SIZE
rem    between stations, not the absolute gate number.)
rem    If the gate steps by ~4.5 gates per 0.15 m instead, the
rem    tape measure disagrees with the config and 0.150 m is
rem    WRONG - that is the useful negative result here.
rem
rem  BOARD  2T4R / DEV113P, FTDI bridge, COM4, 1500000 8-N-1
rem  TOOL   radar-pointcloud\position_capture.py
rem         The board is auto-detected from the port fingerprint
rem         and --baud is left EMPTY ON PURPOSE: 2T4R needs
rem         1500000, 1T2R needs 921600, and forcing 921600 while
rem         the auto path is off receives NOTHING AT ALL.
rem  OUT    .deepworks\tmp\station-2t4r\
rem
rem  NO config is written to the radar. Nothing changes on the
rem  device. This is a pure measurement.
rem
rem  Chinese prompts come from the Python script (UTF-8); this
rem  launcher stays ASCII-only so cmd.exe never garbles them.
rem ==============================================================

cd /d "%~dp0.."

set OUTDIR=.deepworks\tmp\station-2t4r

if exist "%OUTDIR%\pos_0p70.bin" (
  echo.
  echo   NOTE: %OUTDIR% already holds a previous run. New files
  echo   will REPLACE same-named files inside it. Press Ctrl+C to
  echo   abort, or any key to continue.
  echo.
  pause
)

echo.
echo ============================================================
echo   2T4R STATION TAPE-MEASURE CHECK  -  about 4-5 minutes
echo ------------------------------------------------------------
echo   STEP 0 - GET THE MARKS RIGHT (this is the whole point)
echo     Put DOWN four floor marks BEFORE you start and measure
echo     each one with a tape measure FROM THE FRONT FACE OF
echo     THE RADAR - not from the table edge, not from the
echo     tripod screw, not from your best guess:
echo          1)  0.70 m      2)  1.00 m
echo          3)  1.35 m      4)  1.70 m
echo     Both feet on the mark, body facing the radar.
echo     Write down what you actually measured.
echo.
echo   STEP 1 - WHAT HAPPENS
echo     For each station the program names the mark, then WAITS
echo     for you to press ENTER, then beeps. From ENTER onward
echo     STAND STILL - there is a silent 3 s warm-up plus a 3 s
echo     countdown before the high beep.
echo         BEEP(high) = recording started   -^> STAND STILL
echo         BEEP(low)  = recording finished  -^> next mark
echo     Each station records 25 s of pure stillness.
echo.
echo   *** DO NOT MOVE while recording. ***
echo   Any LANE event inside a still window is a FALSE ALARM and
echo   counting those is half the point of this run.
echo   Breathe normally, keep both feet planted.
echo.
echo   TIMING: 2T4R runs at about 1.9 frames/s, 4x slower than
echo   1T2R, so a 25 s window holds only ~48 frames. That is
echo   normal here - a short frame count is NOT an error.
echo.
echo   THE BANNER LINE THAT MUST APPEAR (Chinese words, ASCII
echo   numbers). If you cannot find these tokens, press Ctrl+C:
echo        2t4r      0.5208 s/frame      0.1500 m/gate
echo        range band (0.3, 2.1)
echo   If it says 1t2r instead, COM4 is NOT the 2T4R board.
echo.
echo   Close the point-cloud viewer and ONELAB first. COM4 is
echo   exclusive and the viewer service keeps holding it.
echo ============================================================
echo.

echo [preflight] serial ports seen right now:
python -u -c "import serial.tools.list_ports as lp; [print('   ', p.device, '|', p.description) for p in lp.comports()]"

echo.
echo [preflight] board fingerprint on COM4 (expected: 2t4r):
python -u -c "import sys; sys.path.insert(0, 'radar-pointcloud'); import radar_source as rs; print('   ', rs.probe_board('COM4'))"
echo   (reminder: this reads the USB descriptor, so it still
echo    answers 2t4r while another program is holding the port)

rem --- port guard, added 2026-10-19 after a real COM4 clash ---
rem  The point-cloud viewer (server.py --port auto) claims COM4 the
rem  moment it starts, and then the capture dies with
rem  PermissionError(13) 'access denied'. free_com.py checks that
rem  COM4 can really be opened, and can close OUR OWN viewer only.
rem  It never touches ONELAB or any foreign process.
echo.
echo [guard] can COM4 actually be opened right now?
python -u "radar-pointcloud\free_com.py" COM4
if not errorlevel 1 goto port_free

echo ============================================================
echo   COM4 IS BUSY - another program is holding the port.
echo   Most likely our own point-cloud viewer (service on 8099).
echo   ONELAB open, or another terminal, would do it too.
echo ------------------------------------------------------------
echo   Y = close this project's viewer and continue
echo   N = stop here
echo   No answer within 20 seconds is treated as Y and continues.
echo ============================================================
choice /c YN /n /t 20 /d Y /m "  Close the viewer and continue? [Y/N] "
if errorlevel 2 goto port_stuck

echo.
echo [guard] closing this project's viewer...
python -u "radar-pointcloud\free_com.py" COM4 --kill
python -u "radar-pointcloud\free_com.py" COM4
if errorlevel 1 goto port_stuck

:port_free
echo [guard] COM4 is free - going ahead.

echo.
echo [capture] starting... if the port is wrong this fails fast.
echo.
python -u "radar-pointcloud\position_capture.py" ^
  --source serial ^
  --port COM4 ^
  --stations 0.70,1.00,1.35,1.70 ^
  --duration 25 ^
  --warmup 3 ^
  --outdir ".deepworks\tmp\station-2t4r" ^
  --start-delay 45
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" goto capture_failed

echo ============================================================
echo   STEP 1 finished. Report:
echo     .deepworks\tmp\station-2t4r\station-report.md
echo.
echo   Tell the assistant it is done - it will read that report
echo   and answer the ONE question that decides this step:
echo     does the strongest echo gate step by ONE gate per
echo     0.15 m of real walking?
echo       YES -^> 2T4R range_res 0.150 m becomes MEASURED, and
echo              the 2T4R bring-up is closed.
echo       NO  -^> the config-derived 0.150 m is wrong and every
echo              2T4R range number has to be rescaled.
echo   Either way, go on to STEP 2 (the left/right capture).
echo ============================================================
goto end

:capture_failed
echo ============================================================
echo   CAPTURE FAILED (exit code %RC%)
echo ------------------------------------------------------------
echo   Most common causes, in order:
echo     1) COM4 is held by another program - the point-cloud
echo        viewer service on port 8099, or ONELAB. Close it.
echo     2) COM4 is no longer the 2T4R board. Check Device
echo        Manager: 2T4R is the FTDI entry and Windows can
echo        renumber it.
echo     3) The board is not powered, or the cable is a
echo        charge-only cable with no data lines.
echo   Reminder: the 2T4R bridge is FTDI (no driver install
echo   needed on Windows 10+). The 1T2R board is the CP2102 one
echo   and needs the CP210x driver - the old plan document that
echo   said "FTDI driver" for THAT board is wrong.
echo ============================================================

:port_stuck
echo.
echo ============================================================
echo   COM4 is still not openable - nothing was captured.
echo ------------------------------------------------------------
echo   Close whatever holds COM4 and run this file again:
echo     - our point-cloud viewer window (or its service on 8099)
echo     - ONELAB / the vendor tool
echo     - another terminal that is still reading COM4
echo ============================================================
goto end

:end
echo.
echo [finished] press any key to close.
pause >nul
