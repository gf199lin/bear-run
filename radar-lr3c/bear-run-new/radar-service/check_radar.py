# -*- coding: utf-8 -*-
"""雷达链路自检 —— 不启动网页，只回答「能不能连上、有没有出帧、有没有点」。

它做四件事：
  1. 列出系统当前串口（设备名 / 描述 / USB VID:PID），这是「驱动装没装好」的直接证据；
  2. 自动挑出雷达串口（也可 --port COMx 手动指定）；
  3. 打开串口读几秒，统计原始字节、解析帧数、实际帧率；
  4. 用同一套 DSP 处理这些帧，报告单帧最多几个点 —— 有点才说明「能看到点云」。

它不写雷达寄存器、不改配置、不占网页端口。但**会占用串口**，
所以运行前请先关掉正在跑的点云网页服务（否则会提示「串口被占用」）。

用法：
  python check_radar.py                # 自动识别 + 读 4 秒
  python check_radar.py --port COM5    # 手动指定串口
  python check_radar.py --seconds 8    # 读更久（人走动时更容易看到点）
  python check_radar.py --list-only    # 只列串口，不打开

退出码：0 = 正常出帧；1 = 连上了但没出帧；2 = 环境或串口问题。
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

REASON_CN = {
    'vidpid': 'USB VID:PID 命中已知雷达板（最可靠）',
    'hint': '串口描述含 CP210x/FTDI 等线索',
    'prefer': '命中历史串口名',
    'single': '系统只有一个串口',
    'fixed': '命令行手动指定',
    'ambiguous': '有多个串口，但认不出哪一个是雷达',
    'none': '系统里一个串口都没有',
}

DRIVER_HINT = ('驱动没装好时，设备管理器里应能看到：\n'
               '  1T2R（DEV202H / ICL1122）→ "Silicon Labs CP210x USB to UART Bridge"\n'
               '  2T4R（DEV113P）         → "USB Serial Port"（FTDI FT232）\n'
               'CP210x 驱动下载：https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers')


def check_deps():
    """返回缺失的 pip 包名列表。"""
    missing = []
    for mod, pkg in (('serial', 'pyserial'), ('numpy', 'numpy')):
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)
    return missing


def print_ports(ports):
    if not ports:
        print('  系统串口: 0 个 —— 这通常不是雷达的问题，而是：')
        print('    a) USB 线只供电不传数据（换一根数据线）；')
        print('    b) 板子没插好 / USB 口没供电；')
        print('    c) 驱动没装。%s' % DRIVER_HINT.replace('\n', '\n       '))
        return
    print('  系统串口: %d 个' % len(ports))
    for dev, desc, hwid in ports:
        print('    %-6s %s' % (dev, desc or '(无描述)'))
        if hwid:
            print('           %s' % hwid)


def main():
    ap = argparse.ArgumentParser(description='雷达链路自检（不启动网页）')
    ap.add_argument('--port', default='auto', help='串口号；默认 auto = 自动识别')
    ap.add_argument('--seconds', type=float, default=4.0, help='采样秒数（默认 4）')
    ap.add_argument('--list-only', action='store_true', help='只列串口，不打开')
    a = ap.parse_args()

    print('=' * 64)
    print('   毫米波雷达 · 链路自检')
    print('=' * 64)

    missing = check_deps()
    if missing:
        print('缺少 Python 包: %s' % ', '.join(missing))
        print('请先双击 1-setup.cmd 安装，或执行:')
        print('  %s -m pip install %s' % (os.path.basename(sys.executable), ' '.join(missing)))
        return 2

    import radar_dsp as dsp
    import radar_source as rs

    ports = rs.list_serial_ports()
    print('')
    print_ports(ports)
    if a.list_only:
        return 0

    manual = str(a.port).strip().lower() not in ('auto', '')
    if manual:
        dev, reason = a.port, 'fixed'
    else:
        dev, reason, ports = rs.find_radar_port(prefer=(rs.DEFAULT_PORT,))

    print('')
    print('选择串口: %s（%s）' % (dev if dev else '无', REASON_CN.get(reason, reason)))
    if dev is None:
        if ports:
            print('')
            print('请挑一个上面的串口手动指定，例如：')
            print('  python check_radar.py --port %s' % ports[0][0])
        return 2

    prof = rs.profile_for(rs.find_board_for(dev, ports))
    print('板型判断: %s，预计波特率 %d' % (prof['label'], prof['baud']))
    print('提示: 本自检会独占串口，请先关掉正在运行的点云网页窗口。')
    print('-' * 64)

    src = rs.SerialSource(dev, None, retry_delay=0.5, on_event=print)
    src.open()
    if not src.connected:
        print('')
        print('打开串口失败: %s' % (src.last_error_hint or src.last_error))
        print('')
        print('常见原因与处置：')
        print('  1) 串口被占用 —— 关掉点云网页、官方上位机（ICL_mmWaveSensorDevTool /')
        print('     ONELAB）、串口助手、别的雷达桥接程序后重试；')
        print('  2) 板子没被系统识别 —— 重插 USB，看设备管理器是否有黄色感叹号；')
        print('  3) 选错了口 —— 用 --port COMx 指定上面列表里的雷达口。')
        return 2

    board = src.board or rs.DEFAULT_BOARD
    prof = rs.profile_for(board)
    print('已连接: %s，板型 %s，波特率 %s' % (src.port, prof['label'], src.baud))

    parser, asm = rs.make_protocol(board)
    try:
        proc = dsp.PointCloudProcessor(
            bins=prof['bins'], chirps=prof['chirps'],
            range_res=prof['range_res'], v_res=prof['v_res'],
            ant_spacing=prof['ant_spacing'],
            lam=299792458.0 / float(prof['f0']),
            min_range=prof['range_res'],
        )
    except Exception as e:
        print('DSP 初始化失败（不影响出帧判断）: %s: %s' % (type(e).__name__, e))
        proc = None

    t0 = time.time()
    nbytes = 0
    frames = 0
    pt_frames = 0
    pt_total = 0
    pt_max = 0
    try:
        while time.time() - t0 < max(0.5, a.seconds):
            data = src.read(0.05)
            if not data:
                continue
            nbytes += len(data)
            for rf in parser.feed(data):
                z = asm.push(rf)
                if z is None:
                    continue
                frames += 1
                if proc is None:
                    continue
                try:
                    pts = proc.process(*z)
                except Exception:
                    pts = []
                n = len(pts)
                pt_total += n
                pt_max = max(pt_max, n)
                if n:
                    pt_frames += 1
    finally:
        src.close()

    elapsed = time.time() - t0
    fps = frames / elapsed if elapsed > 0 else 0.0
    expect = 1.0 / rs.frame_period(board)

    print('')
    print('-' * 64)
    print('采样 %.1f 秒：' % elapsed)
    print('  原始字节   : %d' % nbytes)
    print('  解析帧数   : %d（%.2f 帧/秒，该板型理论约 %.2f 帧/秒）'
          % (frames, fps, expect))
    print('  协议错误   : %d 字节被跳过' % parser.skipped_bytes)
    print('  点云       : %d 帧里 %d 帧有点，单帧最多 %d 点'
          % (frames, pt_frames, pt_max))
    print('-' * 64)

    if frames == 0:
        tool = ('ICL_mmWaveSensorDevTool' if board == '1t2r'
                else 'ONELAB 上位机')
        print('结论: 串口能打开，但没有解析出任何一帧。')
        print('请依次排查：')
        print('  1) 板子是否真的在输出 —— 用 %s 连一下，' % tool)
        print('     看工具里有没有实时数据；')
        print('  2) 波特率/板型对不对 —— 换 --port 指定另一块板的口再试；')
        print('  3) USB 线材 —— 换一根短一点的数据线、换一个 USB 口；')
        print('  4) 若用 USB 扩展坞/hub，先直插电脑。')
        if board == '2t4r':
            print('')
            print('  补充（2T4R）: DEV113P 需要按官方流程完成上电/配置后才会上行 55AA 帧，')
            print('  且其链路验证本身尚未收尾。若官方上位机里也看不到数据，先解决硬件侧问题，')
            print('  不要据此认为点云程序坏了。')
        return 1

    if fps < expect * 0.5:
        print('结论: 有数据，但帧率只有理论值的一半左右，可能是 USB 带宽不足/线材质量差。')
        print('      先用官方工具确认设备本身是否正常，再换线/换口对比。')
        return 0

    if pt_frames == 0:
        print('结论: 链路正常（帧率达标），但这几秒里没有检测到目标点。')
        print('      人体静止正对雷达时会被「去静止杂波」减掉，属正常现象。')
        print('      请站在雷达前方 0.7~1.5 m，挥手或左右走动，再跑一次。')
        return 0

    print('结论: 链路正常，能出帧也能出点 —— 直接双击 2-start.cmd 就能看到点云画面。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
