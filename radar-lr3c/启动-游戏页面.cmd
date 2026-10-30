@echo off
cd /d "%~dp0"
echo Starting radar bridge on ws://127.0.0.1:8766 ...
start "radar-bridge" python tools\bear_run_radar_launcher.py --no-open
echo Starting local game page on http://127.0.0.1:8770 ...
start "game-page" python tools\serve_bear_run.py
echo Waiting 8s for radar boot + calibration ...
timeout /t 8 /nobreak >nul
start "" "http://127.0.0.1:8770/?input=radar&radar=ws://127.0.0.1:8766&debug=1"
echo Done. Keep the two black windows open while playing.
