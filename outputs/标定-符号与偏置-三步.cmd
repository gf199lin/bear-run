@echo off
chcp 65001 >nul
title CALIB 3-step - azimuth sign + px offset (1T2R / COM3)
cd /d "%~dp0.."

rem ==============================================================
rem  Three-point lateral calibration (about 5 minutes).
rem
rem  Answers two open questions at once:
rem    1) docs/ai-decision-log.md #57  -> is the azimuth sign correct?
rem    2) docs/ai-decision-log.md #59  -> how much px offset does the
rem       game chain carry (LEFT can never fire with |x| >= 0.36)?
rem
rem  Bench setup BEFORE running:
rem    - tape a center line 1.50 m in front of the board
rem    - tape a point 0.60 m to the LEFT and 0.60 m to the RIGHT
rem      of that center point, all on one straight line
rem    - close ONELAB / viewer / the 8099 server: COM3 is exclusive
rem
rem  Output: .deepworks\tmp\calib-1029\calib3-<hhmmss>.md
rem  Read-only: no radar config is written, no commands are sent.
rem ==============================================================

echo.
echo   Three-point calibration - read the Chinese prompts on screen.
echo   Close ONELAB / viewer / service on COM3 first.
echo.

python -u ".deepworks\tmp\calib3.py" --port COM3

echo.
echo [run finished]
pause >nul
