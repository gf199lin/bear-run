@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   跳 / 蹲 采集电池（多人 · 多条件 · 带负样本）
echo ------------------------------------------------------------
echo   现场纪律（不满足就别开始，数据会废）：
echo     1. 除受试者外，所有人退到雷达视场外（±45 度 / 2 m 外）
echo     2. 板子沿用上次能分开的那套摆法：约 0.5 m 高、仰视约 20 度
echo     3. 受试者站 1.0 m 处，一次只做一件事，做完立刻回站姿
echo     4. 风扇 / 走动的门 / 晃动的线 全部清掉
echo     5. 每块采完看控制台 nz_p90：静息段应小于 0.20
echo.
echo   页面会自动打开，提示大字和语音和以前一样。
echo   停止：Ctrl+C（已采的块会保存）
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 找不到 python
  pause
  exit /b 1
)

python tools\jumpduck_battery.py --port COM3 --tier p0 --tier p1 %*
echo.
pause
