@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title 雷达数据重采 v2 - 现场引导（静息 / 左移 / 右移）
mode con: cols=62 lines=24 >nul 2>nul

rem ======================================================================
rem  雷达数据采集需求单 v2  ·  现场引导（静息基线 / 向左移 / 向右移）
rem
rem  本文件不采集数据，只做三件事：
rem    1) 把需求单里的采集流程变成屏幕上的大字节拍提示；
rem    2) 每换一步响一声（蜂鸣），人在 1.5 米外也能跟上；
rem    3) 把每次动作的墙上时钟时间戳写进 actions-draft-*.csv 供事后对齐。
rem
rem  开始 / 停止录制仍然由你们在采集软件里点（官方工具，或
rem  radar-pointcloud\segment_capture.py）。
rem
rem  按用户要求：需求单 v2 的第 3 段「跳」与第 4 段「蹲」在本文件中
rem  完全不包含，已跳过。
rem
rem  真值口径（AGENTS.md 证据纪律）：本文件写出的 CSV 是人工墙上时钟
rem  时间戳草稿，不是帧号真值；正式标注以采集程序按帧号打标的结果为准。
rem
rem  编码：GBK(936) + chcp 936。中文只出现在 echo 与 rem 里，
rem  变量名 / 标签 / 比较全部保持 ASCII，避免 cmd 解析歧义。
rem ======================================================================

rem ---- 画面骨架（纯 ASCII） -------------------------------------------------
set "RULE=############################################################"
set "THIN=------------------------------------------------------------"
set "IND=                            "
set "BARF=########################################"
set "BARE=----------------------------------------"

rem ---- 0-9 大字字模：每个 25 字符 = 5 行 x 5 列 ------------------------------
set "F0= ### #   # #   # #   #  ### "
set "F1=  #   ##    #    #   ### "
set "F2= ### #   #   #   #  #####"
set "F3=####     #  ###     ##### "
set "F4=#  # #  # ####    #    # "
set "F5=##### #     ####     #### "
set "F6= ### #     #### #   # ### "
set "F7=#####    #   #   #    #   "
set "F8= ### #   # ### #   # ### "
set "F9= ### #   # ####     # ### "
set "FB=     "

rem ---- 运行时状态 -----------------------------------------------------------
set "PERSON="
set "DIST=1.50"
set "SEG=d1.50"
set "REPS=10"
set "MOVE_SEC=3"
set "HOLD_SEC=3"
set "BACK_SEC=3"
set "REST_SEC=3"
set "BASE_SEC=30"
set "CSV="
set "CUR=00"
set "HDR1="
set "T0=0"

if defined CG_DRYRUN set "REPS=2"
if defined CG_DRYRUN set "BASE_SEC=2"
if defined CG_DRYRUN set "MOVE_SEC=1"
if defined CG_DRYRUN set "HOLD_SEC=1"
if defined CG_DRYRUN set "BACK_SEC=1"
if defined CG_DRYRUN set "REST_SEC=1"

call :stamp
set "T0=!NOW_SEC!"

if not defined CG_DRYRUN call :setup_font
call :screen_cover
call :screen_precheck
call :input_params
call :init_csv
call :stage_rest
echo.
echo   ^>^>^> 按任意键继续 ^<^<^<
call :waitkey
call :stage_move LEFT
call :stage_move RIGHT
call :screen_wrapup

if defined CG_DRYRUN goto :finish
echo.
echo    ^>^>^> 按任意键结束 ^<^<^<
pause >nul

:finish
endlocal
exit /b 0

rem ====================== 通用小工具 =======================================

:cls
if defined CG_DRYRUN goto :eof
cls
goto :eof

:beep
if defined CG_DRYRUN goto :eof
echo 
goto :eof

:sleep
rem %1 = 秒数
if defined CG_DRYRUN goto :eof
if "%~1"=="" goto :eof
if "%~1"=="0" goto :eof
for /l %%i in (1,1,%~1) do ping -n 2 127.0.0.1 >nul
goto :eof

:waitkey
if defined CG_DRYRUN goto :eof
pause >nul
goto :eof

:stamp
set "T=%time: =0%"
set "NOW_HM=!T:~0,8!"
set /a "NOW_SEC=(1!T:~0,2!-100)*3600 + (1!T:~3,2!-100)*60 + (1!T:~6,2!-100)"
goto :eof

:bigrows
rem %1 = 两位数字串（如 09）；结果写进 BR0..BR4
set "ds=%~1"
set "d1=!ds:~0,1!"
set "d2=!ds:~1,1!"
for %%a in (!d1!) do set "p1=!F%%a!"
for %%b in (!d2!) do set "p2=!F%%b!"
for /l %%k in (0,1,4) do (
  set /a "o=%%k*5"
  call :slice p1 !o!
  set "s1=!SLICE!"
  call :slice p2 !o!
  set "BR%%k=!s1!  !SLICE!"
)
goto :eof

:slice
set "src=!%~1!"
set "SLICE=!src:~%~2,5!"
goto :eof

:cdbar
rem %1 = 总秒数，%2 = 剩余秒数
set /a "k=%~1-%~2"
set /a "k=k*40/(%~1)"
if !k! LSS 0 set "k=0"
if !k! GTR 40 set "k=40"
set "bb=!BARF:~0,%k%!"
set "bb=!bb!!BARE:~%k%!"
echo    [!bb!]   剩 %~2 秒
goto :eof

:cd_big
rem %1 = 总秒数  %2 = 大字标题  %3 = 下面一行小字
set /a "s=%~1"
set /a "tot=%~1"
:cdb_loop
if !s! LEQ 0 goto :eof
set "nn=0!s!"
set "nn=!nn:~-2!"
call :bigrows !nn!
call :cls
echo %RULE%
echo.
echo        !HDR1!   %~2
echo.
echo !IND!!BR0!
echo !IND!!BR1!
echo !IND!!BR2!
echo !IND!!BR3!
echo !IND!!BR4!
call :cdbar !tot! !s!
echo.
echo        %~3
echo.
echo %RULE%
if !s! EQU !tot! call :beep
if !s! LEQ 3 call :beep
call :sleep 1
set /a "s-=1"
goto :cdb_loop

rem ====================== 屏幕：封面 / 核对 / 登记 =========================

:screen_cover
call :cls
echo %RULE%
echo.
echo    毫米波雷达 - 数据重采 v2 - 现场引导
echo.
echo    本次只做 3 段：静息基线 / 向左移 / 向右移
echo %THIN%
echo    第 0 段   静息基线   站定不动 30 秒                1 次
echo    第 1 段   向左移     横移到位 保持 3 秒 回中 3 秒   10 次
echo    第 2 段   向右移     同上                          10 次
echo %THIN%
echo    这个文件不采集数据，只报节拍：
echo      用大字告诉你现在该做什么、还剩几秒
echo      每换一步响一声，人在 1.5 m 外也能跟上
echo    开始/停止录制，还是你在采集软件里点。
echo %THIN%
echo    字不够大？
echo      Windows Terminal：按住 Ctrl 滚动鼠标滚轮
echo      传统控制台：右键标题栏 - 属性 - 字体 - 大小 24
echo.
echo    ^>^>^> 按任意键继续 ^<^<^<
echo %RULE%
call :waitkey
goto :eof

:screen_precheck
call :cls
echo %RULE%
echo.
echo    采集前核对（不通过就别开录）
echo %THIN%
echo    1. 人站雷达正前方 1.50 m，正对雷达，双脚与肩同宽
echo    2. 雷达对准人体中心：静息时看采集软件的方位角
echo       要 ^|az^| ^<= 10 度，别让人落在 45 度视场边缘
echo       做不到就记下实际安装角度与人体横向偏移
echo    3. 这一批数据全程不要移动雷达
echo %THIN%
echo    地面贴标记：中 0 / 左 0.5~0.8 m / 右 0.5~0.8 m
echo    左右两侧空地要一样大，一侧被挡那侧必然白采
echo    本批至少 2 名被试（p1 / p2），换人要重跑一遍
echo.
echo    ^>^>^> 按任意键继续 ^<^<^<
echo %RULE%
call :waitkey
goto :eof

:input_params
call :cls
echo %RULE%
echo.
echo    先登记 2 个值（直接回车 = 用默认值）
echo %THIN%
echo.
set "PERSON="
if defined CG_DRYRUN set "PERSON=p1"
if not defined PERSON set /p "PERSON=    被试编号（p1 / p2）： "
if not defined PERSON set "PERSON=p1"
set "DIST="
if defined CG_DRYRUN set "DIST=1.50"
if not defined DIST set /p "DIST=    站位距离 m（两位小数）： "
if not defined DIST set "DIST=1.50"
if "!DIST:~3,1!"=="" set "DIST=!DIST!0"
set "SEG=d!DIST!"
echo.
echo %THIN%
echo    被试 !PERSON!    站位 !DIST! m    段名 !SEG!
echo %RULE%
echo.
echo    ^>^>^> 按任意键继续 ^<^<^<
call :waitkey
goto :eof

:init_csv
set "hms=!T:~0,2!!T:~3,2!!T:~6,2!"
if defined CG_DRYRUN (
  set "CSV=.deepworks\tmp\draft-actions-dryrun.csv"
) else (
  set "CSV=%~dp0actions-draft-!PERSON!-!hms!.csv"
)
>"%CSV%" echo action_id,segment,distance_m,direction_label,t_start_s,t_end_s,frame_start,frame_end,frames_with_target,person_id,px_start_m,px_end_m,px_delta_m,px_range_m,mean_deg_span,mean_range_m,rel_start_s,rel_end_s,wall_start,wall_end,suggested_file,note
goto :eof

:logact
rem %1=action_id %2=direction %3=rel_start %4=rel_end %5=wall_start %6=wall_end %7=file
>>"%CSV%" echo %1,!SEG!,!DIST!,%2,,,,,,!PERSON!,,,,,,,%3,%4,%5,%6,%7,wallclock-draft-not-frame-truth
goto :eof

rem ====================== 第 0 段：静息基线 ================================

:stage_rest
call :cls
echo %RULE%
echo.
echo    第 0 段 - 静息基线（1 次 - 30 秒）
echo %THIN%
echo    1) 站到中间标记，正对雷达，站好别动
echo    2) 在采集软件里点「开始录制」
echo       文件名建议： move2-!SEG!-!PERSON!-rest
echo    3) 按任意键 - 先 3 秒准备，再 30 秒倒计时
echo    4) 倒计时结束后点「停止录制」并保存
echo %THIN%
echo    这段是基线：期间任何动作、挪脚、看手机都会让它作废
echo.
echo    ^>^>^> 按任意键开始 ^<^<^<
echo %RULE%
call :waitkey
set "HDR1=第 0 段 静息基线"
call :cd_big 3 "站 好 别 动" "准备：站到中间，站直，别动"
call :stamp
set /a "r1=NOW_SEC-T0"
set "w1=!NOW_HM!"
call :cd_big !BASE_SEC! "保 持 不 动" "这 30 秒里不要动、不要说话、不要挪脚"
call :stamp
set /a "r2=NOW_SEC-T0"
set "w2=!NOW_HM!"
call :logact REST01 REST !r1! !r2! !w1! !w2! move2-!SEG!-!PERSON!-rest
call :cls
echo %RULE%
echo.
echo    基线倒计时结束
echo %THIN%
echo    现在：在采集软件里点「停止录制」并保存
echo    文件名建议： move2-!SEG!-!PERSON!-rest
echo %THIN%
echo    时间戳已记： !r1!s - !r2!s（相对本节拍起点）
echo %RULE%
goto :eof

rem ====================== 第 1 / 2 段：左移 / 右移 =========================

:stage_move
if /i "%~1"=="LEFT" (
  set "DIRZH=向 左 移"
  set "DIRE=LEFT"
  set "DIRF=left"
  set "SNUM=1"
  set "MVH=横移一大步到左边标记（0.5~0.8 m），到位立刻停住"
) else (
  set "DIRZH=向 右 移"
  set "DIRE=RIGHT"
  set "DIRF=right"
  set "SNUM=2"
  set "MVH=横移一大步到右边标记（0.5~0.8 m），到位立刻停住"
)
call :cls
echo %RULE%
echo.
echo    第 !SNUM! 段 - !DIRZH!（!REPS! 次）
echo %THIN%
echo    每一次的节拍（全程不用碰键盘，跟着屏幕和蜂鸣走）：
echo      1) 大字给方向 - 横移到那一侧的标记，停住
echo      2) 大字倒计时 3 秒 - 保持不动
echo      3) 大字提示回中 - 走回中间标记
echo      4) 大字倒计时 3 秒 - 静息
echo %THIN%
echo    开始前：
echo      1) 站到中间，在采集软件里点「开始录制」
echo         文件名建议： move2-!SEG!-!PERSON!-!DIRF!
echo      2) 按任意键 - 节拍立刻开始，之后约 2 分钟不用碰键盘
echo    结束后：
echo      3) 按提示点「停止录制」并保存
echo %THIN%
echo    注意：横向平移，不要转身；每一步都要踩到地面标记
echo.
echo    ^>^>^> 按任意键进入自动节拍 ^<^<^<
echo %RULE%
call :waitkey

set /a "i=0"
:mv_loop
set /a "i+=1"
if !i! GTR !REPS! goto :mv_done
set "nn=0!i!"
set "nn=!nn:~-2!"
set "HDR1=第 !nn! / !REPS! 次"
set "ACTID=!DIRF!-!nn!"
set "FILE=move2-!SEG!-!PERSON!-!DIRF!-!nn!"

rem ---- 1) 给方向：横移过去 ----
call :cls
echo %RULE%
echo.
echo        !HDR1!
echo.
echo                     !DIRZH!
echo.
if /i "!DIRE!"=="LEFT" echo     ^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<
if /i "!DIRE!"=="RIGHT" echo     ^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>
echo.
echo        !MVH!
echo.
echo %RULE%
call :beep
call :stamp
set /a "r1=NOW_SEC-T0"
set "w1=!NOW_HM!"
call :sleep !MOVE_SEC!

rem ---- 2) 保持 3 秒 ----
call :cd_big !HOLD_SEC! "保 持 不 动" "站直，别晃，别回头"

rem ---- 3) 回中 ----
call :cls
echo %RULE%
echo.
echo        !HDR1!
echo.
echo                   回 到 中 间
echo.
if /i "!DIRE!"=="LEFT" echo     ^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>^>
if /i "!DIRE!"=="RIGHT" echo     ^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<^<
echo.
echo        走回中间标记，停住
echo.
echo %RULE%
call :beep
call :sleep !BACK_SEC!

rem ---- 4) 静息 3 秒 ----
call :cd_big !REST_SEC! "静 息" "站在中间别动，等下一次方向提示"
call :stamp
set /a "r2=NOW_SEC-T0"
set "w2=!NOW_HM!"
call :logact !ACTID! !DIRE! !r1! !r2! !w1! !w2! !FILE!
goto :mv_loop

:mv_done
call :cls
echo %RULE%
echo.
echo    第 !SNUM! 段（!DIRZH!）节拍跑完
echo %THIN%
echo    现在：在采集软件里点「停止录制」并保存
echo    文件名建议： move2-!SEG!-!PERSON!-!DIRF!
echo %THIN%
echo    动作时间戳已写入：
echo      !CSV!
echo %THIN%
echo    ^>^>^> 按任意键继续 ^<^<^<
echo %RULE%
call :waitkey
goto :eof

rem ====================== 收尾 ============================================

:screen_wrapup
call :cls
echo %RULE%
echo.
echo    左右两段都跑完了
echo %THIN%
echo    收尾 3 件事：
echo      1) 确认 3 个录制文件都保存了（rest / left / right）
echo      2) 换第二名被试就整批重跑一遍（p2）
echo      3) 数据 + 本脚本导出的 CSV 一起交给游戏组
echo %THIN%
echo    交给别人前的自查：
echo      右移动作的 px_delta_m 中位数应该 大于 0
echo      左移动作的 px_delta_m 中位数应该 小于 0
echo      静息时目标方位角 ^|az^| 应该 小于等于 10 度
echo      有一段完整的 30 秒静息基线
echo %THIN%
echo    本脚本记录（草稿，需要人工核对）：
echo      !CSV!
echo %THIN%
echo    提醒：这份 CSV 只是墙上时钟时间戳，不是帧号真值；
echo    正式标注以采集程序按帧号打标的结果为准。
echo.
echo    ^>^>^> 按任意键结束 ^<^<^<
echo %RULE%
goto :eof

rem ====================== 让窗口的字变大（尽力而为） =======================

:setup_font
powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand CgAkAEUAcgByAG8AcgBBAGMAdABpAG8AbgBQAHIAZQBmAGUAcgBlAG4AYwBlACAAPQAgACcAUwBpAGwAZQBuAHQAbAB5AEMAbwBuAHQAaQBuAHUAZQAnAAoAJABzAHIAYwAgAD0AIABAACIACgB1AHMAaQBuAGcAIABTAHkAcwB0AGUAbQA7AAoAdQBzAGkAbgBnACAAUwB5AHMAdABlAG0ALgBSAHUAbgB0AGkAbQBlAC4ASQBuAHQAZQByAG8AcABTAGUAcgB2AGkAYwBlAHMAOwAKAHAAdQBiAGwAaQBjACAAYwBsAGEAcwBzACAAQwBvAG4ARgBvAG4AdAAgAHsACgAgACAAWwBTAHQAcgB1AGMAdABMAGEAeQBvAHUAdAAoAEwAYQB5AG8AdQB0AEsAaQBuAGQALgBTAGUAcQB1AGUAbgB0AGkAYQBsACkAXQAKACAAIABwAHUAYgBsAGkAYwAgAHMAdAByAHUAYwB0ACAAQwBPAE8AUgBEACAAewAgAHAAdQBiAGwAaQBjACAAcwBoAG8AcgB0ACAAWAA7ACAAcAB1AGIAbABpAGMAIABzAGgAbwByAHQAIABZADsAIAB9AAoAIAAgAFsAUwB0AHIAdQBjAHQATABhAHkAbwB1AHQAKABMAGEAeQBvAHUAdABLAGkAbgBkAC4AUwBlAHEAdQBlAG4AdABpAGEAbAAsACAAQwBoAGEAcgBTAGUAdAAgAD0AIABDAGgAYQByAFMAZQB0AC4AVQBuAGkAYwBvAGQAZQApAF0ACgAgACAAcAB1AGIAbABpAGMAIABzAHQAcgB1AGMAdAAgAEYATwBOAFQARQBYACAAewAKACAAIAAgACAAcAB1AGIAbABpAGMAIAB1AGkAbgB0ACAAYwBiAFMAaQB6AGUAOwAgAHAAdQBiAGwAaQBjACAAdQBpAG4AdAAgAG4ARgBvAG4AdAA7ACAAcAB1AGIAbABpAGMAIABDAE8ATwBSAEQAIABkAHcARgBvAG4AdABTAGkAegBlADsACgAgACAAIAAgAHAAdQBiAGwAaQBjACAAdQBpAG4AdAAgAEYAbwBuAHQARgBhAG0AaQBsAHkAOwAgAHAAdQBiAGwAaQBjACAAdQBpAG4AdAAgAEYAbwBuAHQAVwBlAGkAZwBoAHQAOwAKACAAIAAgACAAWwBNAGEAcgBzAGgAYQBsAEEAcwAoAFUAbgBtAGEAbgBhAGcAZQBkAFQAeQBwAGUALgBCAHkAVgBhAGwAVABTAHQAcgAsACAAUwBpAHoAZQBDAG8AbgBzAHQAIAA9ACAAMwAyACkAXQAgAHAAdQBiAGwAaQBjACAAcwB0AHIAaQBuAGcAIABGAGEAYwBlAE4AYQBtAGUAOwAKACAAIAB9AAoAIAAgAFsARABsAGwASQBtAHAAbwByAHQAKAAiAGsAZQByAG4AZQBsADMAMgAuAGQAbABsACIALAAgAFMAZQB0AEwAYQBzAHQARQByAHIAbwByACAAPQAgAHQAcgB1AGUAKQBdAAoAIAAgAHAAdQBiAGwAaQBjACAAcwB0AGEAdABpAGMAIABlAHgAdABlAHIAbgAgAEkAbgB0AFAAdAByACAARwBlAHQAUwB0AGQASABhAG4AZABsAGUAKABpAG4AdAAgAG4AKQA7AAoAIAAgAFsARABsAGwASQBtAHAAbwByAHQAKAAiAGsAZQByAG4AZQBsADMAMgAuAGQAbABsACIALAAgAFMAZQB0AEwAYQBzAHQARQByAHIAbwByACAAPQAgAHQAcgB1AGUALAAgAEMAaABhAHIAUwBlAHQAIAA9ACAAQwBoAGEAcgBTAGUAdAAuAFUAbgBpAGMAbwBkAGUAKQBdAAoAIAAgAHAAdQBiAGwAaQBjACAAcwB0AGEAdABpAGMAIABlAHgAdABlAHIAbgAgAGIAbwBvAGwAIABHAGUAdABDAHUAcgByAGUAbgB0AEMAbwBuAHMAbwBsAGUARgBvAG4AdABFAHgAKABJAG4AdABQAHQAcgAgAGgALAAgAGIAbwBvAGwAIABiACwAIAByAGUAZgAgAEYATwBOAFQARQBYACAAZgApADsACgAgACAAWwBEAGwAbABJAG0AcABvAHIAdAAoACIAawBlAHIAbgBlAGwAMwAyAC4AZABsAGwAIgAsACAAUwBlAHQATABhAHMAdABFAHIAcgBvAHIAIAA9ACAAdAByAHUAZQAsACAAQwBoAGEAcgBTAGUAdAAgAD0AIABDAGgAYQByAFMAZQB0AC4AVQBuAGkAYwBvAGQAZQApAF0ACgAgACAAcAB1AGIAbABpAGMAIABzAHQAYQB0AGkAYwAgAGUAeAB0AGUAcgBuACAAYgBvAG8AbAAgAFMAZQB0AEMAdQByAHIAZQBuAHQAQwBvAG4AcwBvAGwAZQBGAG8AbgB0AEUAeAAoAEkAbgB0AFAAdAByACAAaAAsACAAYgBvAG8AbAAgAGIALAAgAHIAZQBmACAARgBPAE4AVABFAFgAIABmACkAOwAKAH0ACgAiAEAACgBBAGQAZAAtAFQAeQBwAGUAIAAtAFQAeQBwAGUARABlAGYAaQBuAGkAdABpAG8AbgAgACQAcwByAGMACgAkAGgAIAA9ACAAWwBDAG8AbgBGAG8AbgB0AF0AOgA6AEcAZQB0AFMAdABkAEgAYQBuAGQAbABlACgALQAxADEAKQAKAGkAZgAgACgAJABoACAALQBuAGUAIABbAEkAbgB0AFAAdAByAF0AOgA6AFoAZQByAG8AKQAgAHsACgAgACAAJABmACAAPQAgAE4AZQB3AC0ATwBiAGoAZQBjAHQAIABDAG8AbgBGAG8AbgB0ACsARgBPAE4AVABFAFgACgAgACAAJABmAC4AYwBiAFMAaQB6AGUAIAA9ACAAWwB1AGkAbgB0ADMAMgBdAFsAUwB5AHMAdABlAG0ALgBSAHUAbgB0AGkAbQBlAC4ASQBuAHQAZQByAG8AcABTAGUAcgB2AGkAYwBlAHMALgBNAGEAcgBzAGgAYQBsAF0AOgA6AFMAaQB6AGUATwBmACgAWwB0AHkAcABlAF0AWwBDAG8AbgBGAG8AbgB0ACsARgBPAE4AVABFAFgAXQApAAoAIAAgAGkAZgAgACgAWwBDAG8AbgBGAG8AbgB0AF0AOgA6AEcAZQB0AEMAdQByAHIAZQBuAHQAQwBvAG4AcwBvAGwAZQBGAG8AbgB0AEUAeAAoACQAaAAsACAAJABmAGEAbABzAGUALAAgAFsAcgBlAGYAXQAkAGYAKQApACAAewAKACAAIAAgACAAaQBmACAAKAAkAGYALgBkAHcARgBvAG4AdABTAGkAegBlAC4AWQAgAC0AbAB0ACAAMgA0ACkAIAB7AAoAIAAgACAAIAAgACAAJABmAC4AZAB3AEYAbwBuAHQAUwBpAHoAZQAuAFgAIAA9ACAAMAAKACAAIAAgACAAIAAgACQAZgAuAGQAdwBGAG8AbgB0AFMAaQB6AGUALgBZACAAPQAgADIANAAKACAAIAAgACAAIAAgAFsAdgBvAGkAZABdAFsAQwBvAG4ARgBvAG4AdABdADoAOgBTAGUAdABDAHUAcgByAGUAbgB0AEMAbwBuAHMAbwBsAGUARgBvAG4AdABFAHgAKAAkAGgALAAgACQAZgBhAGwAcwBlACwAIABbAHIAZQBmAF0AJABmACkACgAgACAAIAAgAH0ACgAgACAAfQAKAH0ACgA= >nul 2>nul
goto :eof
