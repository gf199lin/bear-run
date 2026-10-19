@echo off
chcp 65001 >nul
title DEMO launcher - auto-band + drift + strongest point (recommended)

rem ==============================================================
rem  DEMO LAUNCHER  -  recommended recognition configuration
rem
rem  THIS FILE ONLY SETS COMMAND-LINE SWITCHES.
rem  No code default is changed: the shipping default is still
rem  crit='delta' + band (0.70, 1.35). Keeping "default" and
rem  "demo" separate is deliberate - while the thresholds are
rem  still only validated on the 10/15 samples, "default" must
rem  keep meaning "what the code does out of the box".
rem
rem  WHY THESE SWITCHES
rem    --auto-band    pick the range gate automatically during the
rem                   first ~4 s (30 frames @ 7.7 Hz). WITHOUT it
rem                   the default gate (0.70, 1.35) is EMPTY at the
rem                   1.25 / 1.50 m stations, and the recognizer
rem                   emits about 5 FALSE lane changes per minute.
rem    --crit drift   relative criterion (px - base) instead of the
rem                   absolute in-window displacement
rem    --top-k 1      keep only the strongest point in the band
rem    --min-pts 1    with top-k 1 and min_pts 2 every frame would
rem                   be judged "target lost"
rem    --hotkey       HOT SWAP while running (AGENTS.md hard rule 2:
rem                   one hotkey switches source within 1 second).
rem                   Press F2 in the page, or use the "switch data
rem                   source" button, to cycle
rem                     serial -> replay -> loopback -> serial
rem                   (on branch 1 the replay target is the VERIFIED
rem                   1.25 m recording, so F2 lands on the 9/10 case).
rem                   Measured: server-side swap 1.4-3.6 ms, HTTP
rem                   round trip 18-20 ms (2026-10-19, 37 checks).
rem                   Without --hotkey nothing changes at all.
rem
rem  STATUS (be honest, do not overclaim)
rem    Measured on 2026-10-15 truth data: 2 stations, 1 operator,
rem    1 day, 20 truth windows. Left/right hits 9/10 at 1.25 m with
rem    0 false alarms; 1.50 m is R 4/5 (1 false alarm) and LEFT is
rem    still 0/5 there. These are post hold-fix numbers
rem    (2026-10-19, radar_actions.py, devlog 7.34).
rem    External sample 2026-10-18 (same person, same station): 6/10.
rem    Rest check (2026-10-18, 4 stations x 25 s each): this
rem    launcher gives 0 false alarms at all four stations; the
rem    shipping default gate gives 2 false alarms at 1.00 m
rem    (4.8/min) without anyone moving.
rem    NOT yet validated on outside samples. These thresholds are
rem    a PROPOSAL pending group confirmation, not a product spec.
rem ==============================================================

cd /d "%~dp0.."

echo.
echo ============================================================
echo   DEMO LAUNCHER - auto-band + drift + strongest point
echo ------------------------------------------------------------
echo   BEFORE YOU START
echo     1) Close ONELAB and anything else holding COM3.
echo        COM3 is exclusive: two readers = broken stream.
echo     2) Plug in the DEV202H board (CP2102) if not already.
echo     3) Tape down the floor mark for your demo station
echo        BEFORE starting; STAND AT 1.00-1.35 m (1.25 m recommended).
echo        AVOID 1.70 m or beyond: auto-band locks the WRONG gate there
echo        (measured 2026-10-18). Avoid 0.70 m too: that is the
echo        near-field leakage gate j=1.
echo.
echo   WHEN THE PAGE OPENS
echo     1) STAND STILL in front of the radar and WAIT.
echo        The band is chosen during the first ~30 frames.
echo        No event can be emitted while warming up, so any move
echo        you make in the first ~4 seconds is simply lost.
echo        Stay still for about 10 s so the rest baseline
echo        (median px) can settle, then start waving.
echo     2) One movement = one lane change. Do not hold a pose.
echo     3) Check the startup banner says:
echo          band=auto (warm-up 30-120 frames) crit=drift top_k=1 min_pts=1
echo        and that /status reports band_source = auto.
echo        At 1.25 m the gate should be j=2 (band 1.36-2.04 m).
echo        If band_gate is NOT the gate a person is in, fix the
echo        standing spot BEFORE talking to the audience.
echo        BAND_READY in the page is an INFO event. It is NOT a
echo        lane change - ignore it.
echo.
echo   IF THE RADAR FAILS MID-DEMO
echo     Press F2 in the page (or the "switch data source" button).
echo     The source is swapped WITHOUT restarting the server: the
echo     simulator speaks the SAME event interface, so the game
echo     keeps running. F2 cycles serial - replay - loopback.
echo     On branch 1 F2 first lands on the verified 1.25 m
echo     recording, so the 9/10 case can be replayed on the spot.
echo     If the page does not have focus, click the page once or
echo     use the button. If that fails, press Ctrl+C and choose
echo     option 2 or 3. See the note at the bottom of this file
echo     about what has and has not been verified on those paths.
echo ============================================================
echo.

echo   [1] REAL radar  (serial, port auto-detect)      - normal demo
echo   [2] SIMULATOR   (synthetic replay, no hardware) - fallback
echo   [3] REPLAY      (real recording, station 1.25 m) - fallback
echo.
set CHOICE=
set /p CHOICE=Enter 1, 2 or 3 [default 1]: 
if "%CHOICE%"=="" set CHOICE=1

if "%CHOICE%"=="3" goto replay_seg
if "%CHOICE%"=="2" goto simulator
goto real

:real
echo.
echo [real radar] serial + auto-band + drift + strongest point
echo.
python -u "radar-pointcloud\server.py" ^
  --source serial ^
  --port auto ^
  --auto-band ^
  --crit drift ^
  --top-k 1 ^
  --min-pts 1 ^
  --replay ".deepworks\tmp\seg\seg_01.25.bin" ^
  --hotkey ^
  --open
goto done

:simulator
echo.
echo [simulator] synthetic replay - no radar needed
echo.
python -u "radar-pointcloud\server.py" ^
  --source replay ^
  --replay "radar-pointcloud\sample-synthetic.bin" ^
  --auto-band ^
  --crit drift ^
  --top-k 1 ^
  --min-pts 1 ^
  --hotkey ^
  --open
goto done

:replay_seg
echo.
echo [replay] real recording, station 1.25 m - repeats the 9/10 case
echo.
python -u "radar-pointcloud\server.py" ^
  --source replay ^
  --replay ".deepworks\tmp\seg\seg_01.25.bin" ^
  --auto-band ^
  --crit drift ^
  --top-k 1 ^
  --min-pts 1 ^
  --hotkey ^
  --open
goto done

:done
echo.
echo ============================================================
echo   NOTE ON THE FALLBACK PATHS (measured 2026-10-18)
echo ------------------------------------------------------------
echo   Option 2 (synthetic replay) was probed with auto-band on:
echo   it works, but it locks gate j=4 - the gate holding the
echo   synthetic target at about 3 m. That is correct behaviour
echo   (pick the gate that the DATA shows), but it means the
echo   synthetic target sits in a different gate than a real
echo   person. Whether option 2 actually produces lane-change
echo   events in the game was NOT established by that probe.
echo   Option 1 and 3 are the verified paths.
echo ------------------------------------------------------------
echo   HOTKEY (--hotkey, on all three branches)
echo     F2 in the page, or the "switch data source" button,
echo     swaps the live source WITHOUT restarting the server.
echo     Measured server-side swap 1.4-3.6 ms, HTTP round trip
echo     18-20 ms, so the "within 1 second" hard rule is met.
echo     Verified 2026-10-19 by an offline end-to-end smoke run
echo     (37 checks, A/B/C/D all pass) - NOT yet rehearsed on
echo     real hardware. Try it once during T-5:00.
echo     Note: "swap OK" only means the source was restarted on
echo     the new mode. Whether the NEW source really produces
echo     frames still shows in active / frame rate / src_diag.
echo ============================================================
echo.

echo [launcher finished] press any key to close.
pause >nul
