@echo off
chcp 936 >nul 2>&1
setlocal
cd /d "%~dp0.."

set "PY=python"
set "TMPD=%~dp0..\.deepworks\tmp"
if not exist "%TMPD%" mkdir "%TMPD%"

where %PY% >nul 2>&1
if errorlevel 1 (
    echo.
    echo [错误] 找不到 python，请先确认 Python 已安装并加入 PATH。
    echo.
    pause
    exit /b 1
)

:WELCOME
cls
echo ================================================================
echo.
echo        毫米波雷达  正前方测试  引导程序
echo.
echo ================================================================
echo.
echo   带你做 4 件事，全程约 12 分钟。
echo.
echo     1. 确认设备已连接
echo     2. 站正前方，看识别有没有反应   （同时自动录像）
echo     3. 同样动作，再试 --clutter-keep 0.5 （同时自动录像）
echo     4. 采一份带真值的左右移动数据
echo.
echo ----------------------------------------------------------------
echo   最重要的一句话：
echo.
echo     不要用「点数」判断好坏，
echo     要看「左右移动时，方向/事件有没有反应」。
echo.
echo     昨天已查明：正前方点数少，主要是 3x3 邻域抑制造成的
echo     显示问题；正前方超门限像素其实比侧面还多 21%。点少
echo     不等于人消失了。
echo.
echo ----------------------------------------------------------------
echo.
pause


:STEP1
cls
echo ================================================================
echo   [步骤 1 / 4]  连接设备
echo ================================================================
echo.
echo   1) 雷达 USB 插到电脑（芯片 CP2102）
echo   2) 雷达上电，等 3 秒
echo   3) 设备管理器 - 端口(COM 和 LPT)，应能看到 COM 口
echo.
echo   看不到 COM 口：
echo     - 装 CP210x 驱动（不是 FTDI）
echo     - 换一根能传数据的 USB 线
echo.
echo   看到 COM 口后，按任意键继续。
echo.
pause

set "COMPORT=COM3"
set /p COMPORT=请输入串口号（直接回车用 COM3）：
if "%COMPORT%"=="" set "COMPORT=COM3"


:STEP2
cls
echo ================================================================
echo   [步骤 2 / 4]  站正前方，看识别
echo ================================================================
echo.
echo   接下来打开实时点云窗口，并且会自动录像。
echo.
echo   窗口打开后你要做：
echo.
echo     1. 站到雷达【正前方 1.0 米】—— 就是你说看不见的位置
echo     2. 慢慢左右移动 5 次（每次约 1 秒）
echo     3. 盯两件事：
echo          a) 点云有没有（有几个点就行，别数）
echo          b) 左右移动时，方向/事件有没有反应  ^<== 这个才算数
echo.
echo   看完：【直接点右上角 X 关掉那个黑窗口】（别按 Ctrl+C，它问 Y/N 会卡住）。
echo.
echo   准备好后按任意键启动。
echo.
pause

echo.
echo 正在启动（串口 %COMPORT%，并录制到 front_default.bin）...
echo.
start "雷达实时点云" /wait cmd /c "python radar-pointcloud\server.py --port %COMPORT% --record .deepworks\tmp\front_default.bin"

cls
echo ================================================================
echo   [步骤 2 / 4]  记录你刚看到的结果
echo ================================================================
echo.
echo     A = 有反应（方向/事件会变）
echo     B = 没反应，或很弱
echo.
set /p A2=请输入 A 或 B 然后回车： 
echo.
if /i "%A2%"=="A" (
    echo   记下了：正前方【有】反应。
    echo   这意味着「正对不能用」可能根本不成立，
    echo   昨天关于点数少的推断要重新看。
) else (
    echo   记下了：正前方【弱或没反应】。继续往下试。
)
echo.
pause


:STEP3
cls
echo ================================================================
echo   [步骤 3 / 4]  再试一次：只减掉一半静止分量
echo ================================================================
echo.
echo   昨天查到：人正对站着不动时，回波几乎全在零多普勒上，
echo   而算法默认会把它整个减掉（实测正对被减掉 52%，
echo   斜着站只减掉 18%）。
echo.
echo   这次加 --clutter-keep 0.5，只减掉一半。
echo.
echo   动作和刚才一样：站正前方 1.0 米，左右移动 5 次。
echo   这次也会自动录像。
echo.
pause

echo.
echo 正在启动（--clutter-keep 0.5，录制到 front_keep05.bin）...
echo.
start "雷达实时点云 0.5" /wait cmd /c "python radar-pointcloud\server.py --port %COMPORT% --clutter-keep 0.5 --record .deepworks\tmp\front_keep05.bin"

cls
echo ================================================================
echo   [步骤 3 / 4]  对比结果
echo ================================================================
echo.
echo     A = 明显更好（点更多 / 识别更顺）
echo     B = 差不多
echo     C = 更差（杂点变多，画面变脏）
echo.
set /p A3=请输入 A / B / C 然后回车： 
echo.
if /i "%A3%"=="A" (
    echo   记下了：保留一半静止分量【有效】。
) else if /i "%A3%"=="C" (
    echo   记下了：【反而更差】，说明默认的完全去静止是对的。
) else (
    echo   记下了：【差不多】，说明 MTI 不是主要矛盾。
)
echo.
pause


:STEP4
cls
echo ================================================================
echo   [步骤 4 / 4]  采一份带真值的左右移动数据
echo ================================================================
echo.
echo   这一步会记录「你实际做了什么」+「雷达看到了什么」，
echo   有了真值才能算命中率。
echo.
echo   操作要求（别快）：
echo.
echo     1. 站在雷达正前方 1.0 米，站稳别晃
echo     2. 按程序提示：左移 10 次，右移 10 次
echo     3. 每次移动后回到中间，停一下再动下一次
echo     4. 全程别说废话
echo.
echo   这一项约 5 分钟。如果不方便，可以跳过（按 N）。
echo.
set /p A4=开始采集？(Y/N)：
if /i "%A4%"=="N" goto DONE

echo.
echo 正在启动采集程序...
echo.
start "左右动作采集" /wait cmd /c "python radar-pointcloud\segment_capture.py --port %COMPORT% --board 1t2r --distances 1.0 --actions left,right --reps 10 --stride 0"

:DONE
cls
echo ================================================================
echo   全部完成
echo ================================================================
echo.
echo   录像文件（在 .deepworks\tmp 里）：
echo     front_default.bin   步骤2 的原始数据（默认参数）
echo     front_keep05.bin    步骤3 的原始数据（clutter-keep 0.5）
echo.
echo   请把这些告诉我，我来做离线对比和命中率：
echo.
echo     - 步骤 2 答案（A/B）
echo     - 步骤 3 答案（A/B/C）
echo     - 两步里你大致看到的点数
echo     - 有没有生成 front_default.bin / front_keep05.bin
echo.
echo   任何一步报错，把报错内容复制给我。
echo.
pause
exit /b 0
