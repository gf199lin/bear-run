# 一键启动"DEV202H 左右移动 → 游戏"链路（4 个窗口）
# 用法: 双击 start_lane_link.bat，或右键本文件 → 使用 PowerShell 运行
# 需要改串口号 / 路径时，只改下面这几行
$COM      = "COM7"
$BAUD     = "921600"
$NODE_WS  = "8768"                       # 我们的点云网页 → 桥接
$EVENT_WS = "8899"                       # 网页事件 → 融合器
$GAME_WEB = "8801"                       # 本地游戏网页 (http)
$RUNNER   = "C:\Users\叉歪叉\Documents\xwechat_files\wxid_3onj4akypcjx22_d38f\msg\file\2026-10\bear-run-radar-connect-20261022-share(1)\radar-runner"
$HERE     = Split-Path -Parent $MyInvocation.MyCommand.Path
$BRIDGE   = Join-Path (Split-Path -Parent $HERE) "radar-bridge\bridge.js"

$node = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $node) { $node = "C:\Users\叉歪叉\AppData\Local\Author Software\nvm\installs\v22.23.2\node.exe" }
if (-not (Test-Path $RUNNER)) { Write-Host "找不到游戏目录: $RUNNER" -ForegroundColor Red; pause; exit 1 }

Write-Host "[1/4] 串口桥接 $COM @ $BAUD -> ws://127.0.0.1:$NODE_WS" -ForegroundColor Cyan
Start-Process -FilePath $node -ArgumentList $BRIDGE,"--com=$COM","--baud=$BAUD","--ws=$NODE_WS"

Write-Host "[2/4] 融合器 网页事件 -> UDP 8888 @20Hz" -ForegroundColor Cyan
Start-Process -FilePath $node -ArgumentList (Join-Path $HERE "radar_fusion.js") -WorkingDirectory $HERE

Write-Host "[3/4] 游戏桥 UDP 8888 -> ws://127.0.0.1:8766" -ForegroundColor Cyan
Start-Process -FilePath "py" -ArgumentList "-u","tools\radar_bridge.py","--from","udp","--udp-port","8888","--parse","json","--print-sample" -WorkingDirectory $RUNNER

Write-Host "[4/4] 本地游戏网页 http://127.0.0.1:$GAME_WEB/" -ForegroundColor Cyan
Start-Process -FilePath "py" -ArgumentList "-m","http.server","$GAME_WEB" -WorkingDirectory $RUNNER

Write-Host ""
Write-Host "四个窗口都启动后，按下面顺序操作：" -ForegroundColor Yellow
Write-Host "  A. 打开点云网页(带参数) -> 数据源选'桥接助手' -> 连接串口 -> 事件输出点'连接'"
Write-Host "  B. 浏览器开 http://127.0.0.1:$GAME_WEB/ -> 登录 -> 输入源'雷达' -> 雷达测试"
Write-Host ""
Write-Host "点云网页地址（复制到浏览器）:" -ForegroundColor Green
Write-Host "file:///C:/Users/叉歪叉/DeepWorks/outputs/radar-pointcloud/radar-pointcloud.html?board=icl1112&bridge=ws://127.0.0.1:$NODE_WS&out=ws://127.0.0.1:$EVENT_WS/lr"
Write-Host ""
