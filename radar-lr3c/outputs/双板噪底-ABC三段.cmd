@echo off
chcp 65001 >nul
title Dual-board noise floor A/B/C - COM3 + COM4
cd /d "%~dp0.."

rem ==============================================================
rem  Dual-board coexistence test, A/B/C stages, about 4 minutes.
rem  Answers docs/dual-board-interference.md section 6: how much does
rem  1T2R noise floor rise when 2T4R runs at the same time?
rem
rem    A : 1T2R only  (COM3) empty room 60 s
rem    B : 2T4R only  (COM4) empty room 60 s
rem    C : both on          empty room 60 s
rem
rem  Requirements:
rem    - both boards placed in their FINAL mounting position, then
rem      do not touch them for the whole run
rem    - nobody inside the radar field of view (empty room)
rem    - ONELAB / viewer / 8099 service closed
rem
rem  Output: .deepworks\tmp\dual-noise-<hhmmss>\ (*.bin + report .md)
rem  Read-only: no radar config is written, no commands are sent.
rem ==============================================================

echo.
echo   Dual-board noise test. Read the Chinese prompts on screen.
echo   Empty room + both boards already mounted in final position.
echo.

python -u ".deepworks\tmp\dual_noise.py"

echo.
echo [run finished]
pause >nul
