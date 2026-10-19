@echo off
chcp 65001 >nul
title ONE-SHOT CAPTURE - 1T2R / COM3 - stations + rest thresholds + left/right truth

rem ==============================================================
rem  ONE-SHOT CAPTURE  (1T2R / DEV202H / COM3)  - 2026-10-19
rem
rem  Goal: capture EVERYTHING the offline analysis needs in ONE
rem  sitting, so the radar only has to be set up once. Analysis
rem  happens afterwards, offline.
rem
rem  The 2T4R board (COM4 / DEV113P) is NOT present, so this
rem  covers the 1T2R path only: the "0.70 m station" question and
rem  a clean re-measure of the still thresholds. Jump/duck is NOT
rem  captured here - its implementation path is still undecided
rem  (AGENTS.md gate: 2T4R link -> T0 -> T1 offline separability ->
rem  T2 decision), and a 1T2R jump/duck set already exists in
rem  .deepworks\tmp\seg-jd-v2. Never claim jump/duck as working
rem  from this run.
rem
rem  WHY THE 0.70 m STATION
rem    Measured 2026-10-18 (.deepworks\tmp\station-v2\station-report.md):
rem      0.70 m -> strongest echo gate j=1 (1.022 m): 83.9% of the
rem                window power sits in that ONE gate
rem      1.00 m -> 97.0%   1.35 m -> 69.5%   1.70 m -> 27.3%
rem    At 1.25-1.70 m the measured lateral displacement was only
rem    0.291 m against a noise p95 of 1.144 m (~0.25x) - the left/
rem    right signal is buried in range/angle noise. 0.70 m is the
rem    one station that concentrates the person's echo, and it is
rem    the only candidate that needs neither new hardware nor a
rem    change of interaction model. That is what this run tests.
rem
rem  WHAT RUNS, IN ORDER
rem    P0  preflight   : port list, COM3 fingerprint, free the port
rem    P1  station scan: position_capture.py, 6 stations, 25 s
rem                      still-rest each  -> gate profile + power
rem                      share per station + recommended band
rem    P2  rest thresh : radar_calibrate.py --mode rest at 0.70 m
rem                      -> v_on / v_off / min_dx / max_step +
rem                      confidence + false-alarm count
rem    P3  left/right  : segment_capture.py, 5 x LEFT + 5 x RIGHT
rem                      at 0.70 / 0.85 / 1.00 m + a 25 s silent
rem                      window at the end (false-alarm check)
rem    P4  scoring     : eval_segments.py, 1000-permutation test
rem                      -> outputs\eval-oncehot-<TAG>.md
rem
rem  OUTPUT ROOT: .deepworks\tmp\oncehot-<TAG>\
rem    station\     pos_*.bin + station-report.md
rem    calib-070-rest.json   (calib.json itself is NOT touched)
rem    lr\          seg_00.70.bin / seg_00.85.bin / seg_01.00.bin
rem                 + matching .labels.jsonl ground truth
rem
rem  BAND - WHY NOTHING NEEDS CHANGING
rem    --band 0.70 1.35 is ra.BAND_DEFAULT and it CONTAINS gate 1
rem    (span 0.681-1.363 m, centre 1.022 m) where the 0.70 m
rem    station shows up. For 1.00/1.25/1.35 m the person sits in
rem    gate 2 (1.703 m) and segment_capture's DIST_BAND already
rem    maps those stations to (1.36, 2.04), which contains it.
rem    Historical note: this band was called "useless" on 10/15 -
rem    that was the 1.25 m data sitting in gate 2, not this case.
rem
rem  YOU WILL NEED
rem    tape measure, floor marks for 0.70 / 0.85 / 1.00 m,
rem    ~12 minutes, and no phone/keys/metal in front of the radar.
rem
rem  GEOMETRY WARNING for P3
rem    Horizontal FOV is +-45 deg, so at 0.70 m forward distance
rem    the beam edge is only ~+-0.70 m sideways; at 0.85 m it is
rem    ~+-0.85 m. Keep every lateral move to about HALF A STEP,
rem    natural speed, no sprinting, no forward/backward travel.
rem    (Propagation to ~+-0.70 m is a proposal - this run is the
rem     check. If the echo disappears mid-move, that trial is
rem     invalid and must be repeated.)
rem
rem  Chinese prompts come from the Python tools (UTF-8); this
rem  launcher stays ASCII-only so cmd.exe never garbles them.
rem ==============================================================

cd /d "%~dp0.."

set TAG=
set /p TAG=Session tag (who/when, e.g. jiang-1019c) [default oncehot]: 
if "%TAG%"=="" set TAG=oncehot

set ROOT=.deepworks\tmp\oncehot-%TAG%
set ST=%ROOT%\station
set LR=%ROOT%\lr
set CALIB=%ROOT%\calib-070-rest.json
set REPORT=outputs\eval-oncehot-%TAG%.md

echo.
echo ============================================================
echo   ONE-SHOT CAPTURE  -  tag = %TAG%
echo ------------------------------------------------------------
echo   P0  preflight        (about 10 s)
echo   P1  station scan     (about 4 min, 6 stations x 25 s still)
echo   P2  rest thresholds  (about 1 min, at 0.70 m)
echo   P3  left/right truth (about 6 min, 3 stations x 5+5)
echo   P4  scoring          (about 1 min, automatic)
echo ------------------------------------------------------------
echo   Output root: %ROOT%
echo   Scoring    : %REPORT%
echo ============================================================
echo.

rem ---------------------------------------------------------------- P0
echo ############ P0  PREFLIGHT #################################
echo [P0] serial ports seen right now:
python -u -c "import serial.tools.list_ports as lp; [print('   ', p.device, '-', p.description) for p in lp.comports()]"

echo.
echo [P0] board fingerprint on COM3 (expected: 1t2r):
python -u -c "import sys; sys.path.insert(0, 'radar-pointcloud'); import radar_source as rs; print('   ', rs.probe_board('COM3'))"

echo.
echo [P0] can COM3 be opened right now?
python -u "radar-pointcloud\free_com.py" COM3 --baud 921600
if not errorlevel 1 goto p0_free

echo ============================================================
echo   COM3 IS BUSY - something else holds the port.
echo   Most likely our own point-cloud viewer (service on 8099),
echo   or ONELAB, or a terminal still reading COM3.
echo ------------------------------------------------------------
echo   Y = close THIS PROJECT's viewer and continue
echo   N = stop here
echo   No answer within 20 s is treated as Y.
echo ============================================================
choice /c YN /n /t 20 /d Y /m "  Close the viewer and continue? [Y/N] "
if errorlevel 2 goto p0_stuck

echo [P0] closing this project's viewer...
python -u "radar-pointcloud\free_com.py" COM3 --kill --baud 921600
python -u "radar-pointcloud\free_com.py" COM3 --baud 921600
if errorlevel 1 goto p0_stuck

:p0_free
echo [P0] COM3 is free - going ahead.
echo.

rem ---------------------------------------------------------------- P1
echo ############ P1  STATION SCAN (STILL-REST) #################
echo ============================================================
echo   WHAT YOU DO
echo     Lay down floor marks at 0.55 / 0.70 / 0.85 / 1.00 /
echo     1.25 / 1.50 m from the FRONT FACE of the radar, measured
echo     with the tape - tell the assistant the REAL measured
echo     distances afterwards.
echo     Then stand at the 0.55 m mark, feet on the mark, facing
echo     the radar, press a key, and from there just follow the
echo     spoken/printed cues: it walks you 0.55 -^> 1.50 m, 25 s of
echo     complete stillness per station.
echo     STAND STILL during each window: no stepping, swaying,
echo     waving, weight shifting. Every detection in a still
echo     window is a FALSE ALARM and is counted.
echo.
echo   EXPECTED BANNER: 1t2r / 0.1295 s/frame / 0.6813 m/gate
echo   RESULT: %ST%\station-report.md
echo ============================================================
echo.
pause
python -u "radar-pointcloud\position_capture.py" ^
  --source serial ^
  --port COM3 ^
  --board 1t2r ^
  --stations 0.55,0.70,0.85,1.00,1.25,1.50 ^
  --duration 25 ^
  --warmup 2 ^
  --start-delay 15 ^
  --band "0.70,1.35" ^
  --outdir "%ST%"
if errorlevel 1 goto p1_failed

rem ---------------------------------------------------------------- P2
echo.
echo ############ P2  REST THRESHOLDS AT 0.70 m #################
echo ============================================================
echo   WHAT YOU DO
echo     Walk back to the 0.70 m mark, stand still, face the
echo     radar, then press a key. There is a 3 s countdown; after
echo     "go" hold still for 20 s - breathe normally, do not step
echo     or wave.
echo     This re-measures v_on / v_off / min_dx / max_step AT THAT
echo     STATION: the current values were taken at another
echo     geometry and are only valid for the geometry they were
echo     taken at.
echo.
echo   NOTE: written to a SEPARATE file, so the working
echo   radar-pointcloud\calib.json stays untouched until the
echo   numbers have been compared.
echo   RESULT: %CALIB%
echo ============================================================
echo.
pause
python -u "radar-pointcloud\radar_calibrate.py" ^
  --mode rest ^
  --port COM3 ^
  --board 1t2r ^
  --band 0.70 1.35 ^
  --seconds 20 ^
  --calib "radar-pointcloud\calib.json" ^
  --out "%CALIB%"
if errorlevel 1 echo [P2] WARNING: rest thresholds failed - the rest of the run continues.

rem ---------------------------------------------------------------- P3
echo.
echo ############ P3  LEFT/RIGHT GROUND TRUTH ###################
echo ============================================================
echo   WHAT YOU DO
echo     Stand at the 0.70 m mark, face the radar, press a key.
echo     About 15 s later the cues start. Per repetition:
echo       PREP cue  -^> step back to centre, stand still
echo       BEEP      -^> move ONCE in the announced direction,
echo                    about HALF A STEP sideways, then HOLD it
echo                    for ~2.5 s
echo       STOP cue  -^> return to centre and wait
echo     Order: 5 x LEFT, then 5 x RIGHT at 0.70 m, then the same
echo     at 0.85 m, then at 1.00 m (the tool walks you there).
echo     After the last station there is a 25 s silent window:
echo     stand still, any event there is a FALSE ALARM.
echo.
echo   MOVE SIZE: about HALF A STEP, natural speed. At 0.70 m the
echo   beam edge is only ~0.70 m sideways, so a full step goes
echo   outside it.
echo   Probe stations -^> band (0.70, 1.35) for 0.70 m and
echo   (1.36, 2.04) for 0.85/1.00 m, both containing the echo.
echo   RESULT: %LR%\seg_00.70.bin  seg_00.85.bin  seg_01.00.bin
echo           + matching .labels.jsonl
echo ============================================================
echo.
pause
python -u "radar-pointcloud\segment_capture.py" ^
  --source serial ^
  --port COM3 ^
  --board 1t2r ^
  --distances 0.70,0.85,1.00 ^
  --actions left,right ^
  --reps 5 ^
  --prep 3 ^
  --settle 3 ^
  --lead 12 ^
  --rest 25 ^
  --auto ^
  --walk-time 12 ^
  --start-delay 15 ^
  --outdir "%LR%" ^
  --no-compare
if errorlevel 1 goto p3_failed

rem ---------------------------------------------------------------- P4
echo.
echo ############ P4  SCORING ###################################
python -u ".deepworks\tmp\eval_segments.py" --dir "%LR%" --board 1t2r --perm 1000 --out "%REPORT%"
if errorlevel 1 echo [P4] WARNING: scoring failed - the raw data is still on disk.

echo.
echo ============================================================
echo   CAPTURE FINISHED.
echo ------------------------------------------------------------
echo   raw station data : %ST%\station-report.md
echo   raw left/right   : %LR%\
echo   rest thresholds  : %CALIB%
echo   scoring report   : %REPORT%
echo ------------------------------------------------------------
echo   Tell the assistant it is done. It will then:
echo     1) read station-report.md - does the echo really sit in
echo        gate 1 at 0.70 m, and how does power share fall off
echo        with distance;
echo     2) compare %CALIB% against radar-pointcloud\calib.json
echo        and decide whether to merge the 0.70 m thresholds;
echo     3) read %REPORT% - is the 0.70/0.85 m lateral SNR
echo        clearly better than the 1.25-1.70 m baseline.
echo   DECISION RULE (agreed in advance): better SNR and real
echo   events win on independent samples -^> 0.70 m becomes the
echo   demo station. Not clearly better, or false alarms rise
echo   -^> 0.70 m is dropped. Both outcomes are results.
echo.
echo   OPTIONAL, if a SECOND PERSON is available: run this same
echo   file type once more with another tag and have THE OTHER
echo   person do 0.85 m. Person-to-person generalisation is still
echo   the top unverified risk in AGENTS.md.
echo ============================================================
goto end

:p1_failed
echo.
echo ============================================================
echo   P1 STATION SCAN FAILED - stopping here.
echo ------------------------------------------------------------
echo   Nothing after this point is trustworthy, so the run stops.
echo   Check in this order:
echo     1) COM3 held by another program (viewer 8099 / ONELAB).
echo     2) COM3 is no longer the CP210x board - check Device
echo        Manager: Silicon Labs CP210x USB to UART Bridge.
echo        The 1T2R bridge needs the CP210x driver; the 2T4R
echo        bridge is FTDI and needs none.
echo     3) Board not powered, or a charge-only USB cable.
echo     4) A station was skipped / the program was closed early.
echo   Raw data already written stays on disk.
echo ============================================================
goto end

:p3_failed
echo.
echo ============================================================
echo   P3 LEFT/RIGHT CAPTURE FAILED (exit code 1) - stopping.
echo ------------------------------------------------------------
echo   The station report and rest thresholds above are still
echo   valid; only the left/right ground truth is missing.
echo   Most likely: ENTER was never pressed, the port got taken,
echo   or a station cue was missed. Re-run this file with the SAME
echo   tag bookkeeping and tell the assistant which part is new.
echo ============================================================
goto end

:p0_stuck
echo.
echo ============================================================
echo   COM3 IS STILL NOT OPENABLE - NOTHING WAS CAPTURED.
echo ------------------------------------------------------------
echo   Close whatever holds COM3 and run this file again:
echo     - our point-cloud viewer window (or its service on 8099)
echo     - ONELAB / the vendor tool
echo     - another terminal still reading COM3
echo   Then check the Device Manager for the CP210x bridge.
echo ============================================================

:end
echo.
echo [finished] press any key to close.
pause >nul
