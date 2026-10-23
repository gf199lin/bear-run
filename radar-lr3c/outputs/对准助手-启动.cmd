@echo off
chcp 65001 >nul
title Alignment helper - 1T2R / COM3 - target ^|az^| ^<= 10 deg
cd /d "%~dp0.."

echo ============================================================
echo   ALIGNMENT HELPER   -   1T2R / COM3   (read-only)
echo ------------------------------------------------------------
echo   GOAL: with one person standing STILL at the 0.70 m mark,
echo           ^|az^| ^<= 10 deg      and      ^|px^| ^< 0.20 m
echo   TODAY:  az = +24.3 deg, px = +0.46 m  -^> off by 2.4x
echo.
echo   HOW TO TURN IT
echo     1) One person stands on the 0.70 m mark and holds still.
echo        Do not let them watch the screen - they must not move.
echo     2) Turn the RADAR about its vertical axis, a few degrees
echo        at a time, and watch az move toward 0.
echo        If you are unsure which way: turn 5 deg, look at the
echo        number. Smaller = correct, bigger = turn the other way.
echo     3) Moving the PERSON sideways by ^|px^| metres works too,
echo        but only change ONE of the two, never both at once.
echo     4) It is aligned when az and px both show [ OK ].
echo        Roughly 20-26 deg of radar yaw is what is missing.
echo.
echo   The helper only reads the serial port. It writes nothing,
echo   sends no commands, and does not touch the recording files.
echo   Ctrl+C quits.
echo ============================================================
echo.

python -u "radar-pointcloud\align_assist.py" --port COM3

echo.
echo [exited] press any key to close.
pause >nul
