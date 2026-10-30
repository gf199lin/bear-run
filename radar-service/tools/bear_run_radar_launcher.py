# -*- coding: utf-8 -*-
"""双击即用：雷达 → 线上 Bear Run 跑酷页面。

做三件事，不需要懂命令行：
  1. 自动识别雷达串口（1T2R 的 CP2102 或 2T4R 的 FTDI，插哪块用哪块），
     自动匹配波特率与上行协议；
  2. 在本机 8766 端口起 WebSocket，把雷达点云翻译成游戏帧
     {ts, x, height, jump, crouch, confidence}；
  3. 等端口就绪后打开线上页面：
     https://gf199lin.github.io/bear-run/?input=radar&radar=ws://127.0.0.1:8766&debug=1

用法（双击 exe 或 `python bear_run_radar_launcher.py`）：
  --port COM5     手动指定串口（默认 auto = 自动识别）
  --role lr|both  默认 lr：只驱动左右换道，jump/crouch 恒 0（稳）；
                  both：同时透出跳/蹲（项目里仍是未验证候选项，现场慎用）
  --ws-port 8766  WebSocket 端口
  --no-open       不自动打开浏览器
"""
import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

GAME_URL = 'https://gf199lin.github.io/bear-run/'
DEFAULT_WS_PORT = 8766

# ---------------------------------------------------------------- 打包路径
if getattr(sys, 'frozen', False):
    # PyInstaller：模块已打进包，radar_pointcloud 的源码目录不存在也没关系；
    # 但 radar_live_ws 用 PIPELINE_DIR 找 calib.json，这里补一份可用的目录结构。
    BUNDLE = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    for cand in (os.path.join(BUNDLE, 'radar-pointcloud'), BUNDLE):
        if os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))     # radar-service/tools
    SERVICE = os.path.abspath(os.path.join(HERE, '..'))   # radar-service
    PROJECT = os.path.abspath(os.path.join(SERVICE, '..'))
    # HERE 提供 radar_live_ws；SERVICE 提供 radar_source / radar_actions 等
    for cand in (HERE, SERVICE,
                 os.path.join(SERVICE, 'radar-pointcloud'),
                 os.path.join(PROJECT, 'radar-pointcloud')):
        if os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)


def setup_console():
    """Windows 控制台切 UTF-8，避免中文日志变乱码。"""
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def game_url(ws_port):
    return '%s?input=radar&radar=%s&debug=1' % (
        GAME_URL, 'ws://127.0.0.1:%d' % ws_port)


def wait_port_then_open(port, url, timeout=30.0):
    """等 WS 真正在监听再开浏览器 —— 避免页面先加载、连了个空端口就放弃。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(('127.0.0.1', port), 0.5):
                break
        except OSError:
            time.sleep(0.25)
    else:
        print('!! 等了 %.0f 秒，%d 端口仍未就绪，浏览器没有自动打开。' % (timeout, port),
              flush=True)
        return False
    print('\n游戏页面: %s' % url, flush=True)
    print('（页面打开后点「游客」进入；输入源应是「雷达」）\n', flush=True)
    try:
        webbrowser.open(url)
        return True
    except Exception as e:
        print('自动打开浏览器失败（%s）。请手动打开上面的链接。' % e, flush=True)
        return False


def main():
    setup_console()
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument('--port', default='auto')
    ap.add_argument('--role', default='lr', choices=['lr', 'ud', 'both'])
    ap.add_argument('--ws-port', type=int, default=DEFAULT_WS_PORT)
    ap.add_argument('--no-open', action='store_true')
    ap.add_argument('-h', '--help', action='help')
    mine, rest = ap.parse_known_args()

    url = game_url(mine.ws_port)
    print('=' * 62)
    print(' 雷达 → Bear Run 桥接器')
    print(' 串口: %s   角色: %s   WS: 127.0.0.1:%d'
          % (mine.port, mine.role, mine.ws_port))
    print('=' * 62)
    print('插好雷达后无需重启：程序每秒重新枚举串口，认到就自动开始。')
    print('没插雷达时，页面会显示「未连接」，插上后自动恢复。\n')

    if not mine.no_open:
        threading.Thread(target=wait_port_then_open,
                         args=(mine.ws_port, url), daemon=True).start()

    # 交给项目里已验证的桥（判据、板型识别、零点标定都在那边）
    sys.argv = [
        'radar_live_ws.py',
        '--port', mine.port,
        '--role', mine.role,
        '--ws-port', str(mine.ws_port),
    ] + rest

    import radar_live_ws
    return radar_live_ws.main()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
