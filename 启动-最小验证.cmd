@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   ============================================================
echo    Minimal validation: 5 rounds of
echo        [ jump once -^> stand 2s -^> duck once -^> stand 2s ]
echo.
echo    Radar: ~0.5 m high, keep the SAME tilt as last time.
echo    Person: stand ~1.2 m in front, facing the radar.
echo.
echo    Do each action ONCE, then stand STILL and wait.
echo    Do NOT walk around between actions (walking looks like duck).
echo   ============================================================
echo.
python tools\vguide_1t2r.py --alternate 5 %*
echo.
pause
