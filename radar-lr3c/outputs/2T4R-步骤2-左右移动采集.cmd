@echo off
chcp 65001 >nul
title 2T4R left/right capture - COM4 - pre-warm-up timing variant

rem ==============================================================
rem  STEP 2 of 2 - 2T4R LEFT/RIGHT CAPTURE (exploratory)
rem
rem  READ THIS FIRST - WHAT THIS RUN IS AND IS NOT
rem    The game layer (radar_actions.py) is still tuned for 1T2R:
rem    FRAME_PERIOD is 0.1295 s (7.7 frames/s) and the recognizer
rem    locks its range gate on FRAME COUNTS, not seconds:
rem        AUTO_BAND_MIN_FRAMES = 30   (1T2R: 3.9 s)
rem        AUTO_BAND_MAX_FRAMES = 120  (1T2R: 15.5 s)
rem        AUTO_BAND_MIN_POINTS = 20
rem    2T4R runs at about 1.92 frames/s. The same 30 frames are
rem    15.6 s, and the 120-frame fallback is 62.5 s. So:
rem      - data captured here CANNOT be merged with 1T2R numbers
rem        to re-tune the lane thresholds;
rem      - it is still worth recording, because it answers a
rem        question no 1T2R run can: does a left/right half-step
rem        show up at all, in the right gate direction, on the
rem        2T4R board, at 4x slower sampling?
rem    The REAL outside-sample lane dataset is the existing
rem    launcher in outputs\ whose title line reads
rem    "OUTSIDE-SAMPLE capture - left/right lane movement".
rem    That one runs on COM3 / 1T2R: use it when the CP2102
rem    board is plugged in. This file is the 2T4R companion.
rem
rem  WHY --lead 30 (and not 12 like the 1T2R launcher)
rem    --lead is SILENT STILLNESS in seconds at the start. The
rem    gate needs >=30 frames AND >=20 detected points before it
rem    can lock. 30 frames at 1.92 frames/s is 15.6 s, and
rem    detection here is sparse, so 12 s is NOT enough on 2T4R.
rem    With --lead 30 you get ~58 frames of standstill: enough
rem    for the frame count, with margin for the point count.
rem    Do not move during it - that is warm-up AND rest baseline.
rem
rem  THE COMPARE TABLE IS OFF ON PURPOSE (--no-compare)
rem    That table scores with the code DEFAULT criterion, which
rem    is a 1T2R range band. On 2T4R the band is different
rem    (0.150 m gates, not 0.681 m), so the default table would
rem    print 0 hits and false alarms and mean nothing. The
rem    assistant analyses these files afterwards instead.
rem
rem  PROTOCOL OTHERWISE IDENTICAL: --prep 3 --settle 3 --reps 5
rem  --actions left,right --rest 25, so the 2T4R run can at
rem  least be compared against itself.
rem
rem  BOARD 2T4R / DEV113P, FTDI, COM4, 1500000 8-N-1 (--baud
rem  stays EMPTY on purpose: the auto path picks the baudrate).
rem ==============================================================

cd /d "%~dp0.."

echo.
echo ============================================================
echo   2T4R left/right capture  (exploratory companion run)
echo ------------------------------------------------------------
echo   BEFORE YOU START
echo     1) Close the point-cloud viewer and ONELAB. COM4 is
echo        exclusive - two readers break the stream.
echo     2) Mark the standing spot on the floor and measure it
echo        from the radar face. 1.00 m is the default below.
echo     3) Quiet room, no metal right in front of the radar.
echo     4) Remember: this is a 2T4R probe, NOT the dataset that
echo        will re-tune the lane thresholds. Keep it clearly
echo        labelled so nobody merges it with 1T2R later.
echo ============================================================
echo.

set ST=
set /p ST=Standing distance in metres [default 1.00]: 
if "%ST%"=="" set ST=1.00

set TAG=
set /p TAG=Session tag (who/when, e.g. 2t4r-wang-1019) [default 2t4r]: 
if "%TAG%"=="" set TAG=2t4r

set OUTDIR=.deepworks\tmp\seg-2t4r\%TAG%\d%ST%

echo.
echo ============================================================
echo   station = %ST% m     tag = %TAG%     port = COM4 (2T4R)
echo   output  = %OUTDIR%
echo ------------------------------------------------------------
echo   WHAT WILL HAPPEN
echo     - You hear "please stand still for 30 seconds".
echo       STAND STILL. No weight shifting, no waving. This is
echo       warm-up and rest baseline at once.
echo     - Then, 10 times: a cue, then a beep. On the beep do
echo       ONE movement and HOLD it for about 2.5 s.
echo       5 LEFT, then 5 RIGHT. Move a half step to a step at
echo       a natural speed. Do not lunge.
echo     - Between movements return to the middle and stand
echo       still for 3 s.
echo     - After the last movement there is 25 s of pure
echo       standstill. Every event in that window is a FALSE
echo       ALARM. Do not move.
echo     - Total about 3 minutes.
echo.
echo   TIMING NOTE: 2T4R is ~1.92 frames/s, so each second you
echo   hold a pose is only ~2 frames. Hold the FULL 2.5 s and
echo   keep the movement crisp - do not drift back early.
echo.
echo   THE BANNER LINE THAT MUST APPEAR (Chinese words, ASCII
echo   numbers). If you cannot find these tokens, press Ctrl+C:
echo        2t4r      0.5208 s/frame      0.1500 m/gate
echo ============================================================
echo.

echo [preflight] serial ports seen right now:
python -u -c "import serial.tools.list_ports as lp; [print('   ', p.device, '|', p.description) for p in lp.comports()]"

echo.
echo [preflight] board fingerprint on COM4 (expected: 2t4r):
python -u -c "import sys; sys.path.insert(0, 'radar-pointcloud'); import radar_source as rs; print('   ', rs.probe_board('COM4'))"

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
python -u "radar-pointcloud\segment_capture.py" ^
  --source serial ^
  --port COM4 ^
  --distances %ST% ^
  --actions left,right ^
  --reps 5 ^
  --prep 3 ^
  --lead 30 ^
  --settle 3 ^
  --rest 25 ^
  --outdir "%OUTDIR%" ^
  --no-compare
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" goto capture_failed

echo ============================================================
echo   Capture finished. Files for a 1.00 m station:
echo     %OUTDIR%\seg_01.00.bin            raw 2T4R frames
echo     %OUTDIR%\seg_01.00.labels.jsonl   truth windows + meta
echo   The name is the distance zero-padded to 5 characters, so
echo   1.00 m gives seg_01.00.* and 1.35 m gives seg_01.35.*
echo   The board / timing / range band header lives INSIDE the
echo   labels.jsonl file - there is no separate meta file.
echo.
echo   NO automatic scoring here (see the header). Tell the
echo   assistant it is done and it will analyse these files with
echo   the board-aware path: frame period 0.5208 s, gate 0.150 m,
echo   band centred on the station instead of the 1T2R band.
echo ------------------------------------------------------------
echo   WHAT IS BEING ASKED OF THIS DATA (be honest later)
echo     Is there enough signal, at 1.92 frames/s, for a half
echo     step to left or right to show up as a repeatable shift
echo     in the horizontal angle and gate power? If the answer
echo     is "not really", that is a real result: it says the
echo     lane-triggered MVP should stay on 1T2R, and 2T4R stays
echo     reserved for whatever needs its extra antennas.
echo ============================================================
goto end

:capture_failed
echo ============================================================
echo   CAPTURE FAILED (exit code %RC%)
echo ------------------------------------------------------------
echo   Most common causes, in order:
echo     1) COM4 is held by another program - the point-cloud
echo        viewer service on port 8099, or ONELAB.
echo     2) COM4 is not the 2T4R board anymore (Device Manager:
echo        FTDI entry). Windows can renumber ports.
echo     3) Board not powered / charge-only cable.
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
