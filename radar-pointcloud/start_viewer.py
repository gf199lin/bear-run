# -*- coding: utf-8 -*-
"""一键启动：雷达实时点云网站。

双击 `start_viewer.cmd`（或桌面快捷方式）即走这里。它负责：

  1. 环境自检（pyserial / numpy 是否装了）；
  2. **串口体检**：列出系统当前串口，判断雷达是否已被系统识别（不打开端口，只枚举）；
  3. **复用已在跑的服务**，否则新起一个（端口从 8099 起自动找空闲口），
     避免双击两次变成两个实例抢同一个串口；
  4. 自动打开浏览器。

设计原则：任何一步失败都给**可操作的中文提示**，而不是留下一个「无数据」的黑箱页面。

边界（诚实说明）：本脚本不做「雷达是否真的在出数据」的判断，那由服务端与页面显示；
本脚本也不改雷达配置、不写寄存器。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
PORT_BASE = 8099
PORT_TRIES = 5
WAIT_SECONDS = 15.0


def say(msg=''):
    print(msg, flush=True)


def check_deps():
    """返回缺失的 pip 包名列表。"""
    missing = []
    for mod, pkg in (('serial', 'pyserial'), ('numpy', 'numpy')):
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)
    return missing


def probe_service(port, timeout=0.8):
    """该端口上是否已有本项目的服务在跑；是则返回 /status 内容，否则 None。"""
    url = 'http://127.0.0.1:%d/status' % port
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'replace') or '{}'
        data = json.loads(raw)
        if isinstance(data, dict) and ('active' in data or 'control' in data or 'cfg' in data):
            return data
    except Exception:
        return None
    return None


def port_free(port):
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def diagnose_ports():
    """只读串口体检。返回 (可用设备或 None, 中文说明)。"""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        from radar_source import find_radar_port, list_serial_ports
    except Exception as e:
        return None, '串口模块导入失败: %s: %s' % (type(e).__name__, e)
    try:
        ports = list_serial_ports()
    except Exception as e:
        return None, '串口枚举失败: %s: %s' % (type(e).__name__, e)
    if not ports:
        return None, ('未检测到任何串口设备 —— 雷达没插好 USB，'
                      '或 CP210x 驱动未安装（设备管理器里应出现 '
                      '"Silicon Labs CP210x USB to UART Bridge"）')
    txt = '; '.join(('%s [%s]' % (d, desc)) if desc else d for d, desc, _ in ports)
    dev, reason, _ = find_radar_port()
    if dev is None:
        if reason == 'ambiguous':
            return None, ('系统串口: %s —— 有多个串口但认不出雷达，'
                          '请手动指定：python server.py --port COMx' % txt)
        return None, '系统串口: %s —— 未识别到雷达' % txt
    return dev, '系统串口: %s —— 将使用 %s' % (txt, dev)


def main():
    say('=' * 64)
    say('   毫米波雷达 · 实时点云   —   一键启动')
    say('=' * 64)

    missing = check_deps()
    if missing:
        say('缺少 Python 包: %s' % ', '.join(missing))
        say('请先执行:  %s -m pip install %s'
            % (os.path.basename(sys.executable), ' '.join(missing)))
        return 2

    # 1) 已有服务在跑 → 直接复用并打开页面（绝不重复起实例抢串口）
    for p in range(PORT_BASE, PORT_BASE + PORT_TRIES):
        st = probe_service(p)
        if st is not None:
            url = 'http://127.0.0.1:%d' % p
            state = '采集中' if st.get('active') else '已停止'
            say('检测到已在运行的点云服务: %s（当前状态: %s）' % (url, state))
            say('将直接打开该页面，不重复启动（避免两个进程抢同一个串口）。')
            if not st.get('active'):
                say('注意：服务当前是「已停止」状态，页面里点「开始」即恢复采集。')
            try:
                webbrowser.open(url)
            except Exception as e:
                say('未能自动打开浏览器: %s —— 请手动访问 %s' % (e, url))
            return 0

    # 2) 选一个空闲端口
    port = None
    for p in range(PORT_BASE, PORT_BASE + PORT_TRIES):
        if port_free(p):
            port = p
            break
    if port is None:
        say('端口 %d~%d 都被占用，无法启动新的点云服务。'
            % (PORT_BASE, PORT_BASE + PORT_TRIES - 1))
        say('可能上一次的服务窗口还开着但已卡死：请先关掉那个窗口再试。')
        return 3

    # 3) 串口体检（只读枚举，不占用端口）
    dev, msg = diagnose_ports()
    say('串口体检: %s' % msg)
    if dev is None:
        say('提示: 服务照样会启动，并每秒重新识别一次 —— 把雷达插上就会自动连上，'
            '不需要重启本窗口。')

    # 4) 启动服务（串口自动识别）
    url = 'http://127.0.0.1:%d' % port
    cmd = [sys.executable, os.path.join(HERE, 'server.py'),
           '--source', 'serial', '--port', 'auto', '--http-port', str(port)]
    say('')
    say('启动服务: %s' % ' '.join(cmd))
    say('-' * 64)
    proc = subprocess.Popen(cmd, cwd=HERE)
    t0 = time.time()
    ready = False
    while time.time() - t0 < WAIT_SECONDS:
        if proc.poll() is not None:
            break
        if probe_service(port, timeout=0.4) is not None:
            ready = True
            break
        time.sleep(0.25)
    if proc.poll() is not None:
        say('-' * 64)
        say('服务启动失败（进程已退出）—— 请看上面的报错信息。')
        return 4
    if not ready:
        say('服务已启动，但 %d 秒内没等到 HTTP 就绪，仍尝试打开页面。' % int(WAIT_SECONDS))

    say('-' * 64)
    say('点云页面: %s' % url)
    say('页面自动打开；若没有弹出，请手动在浏览器访问上面的地址。')
    say('停止服务: 在本窗口按 Ctrl+C，或直接关闭本窗口。')
    say('- ' * 32)
    try:
        webbrowser.open(url)
    except Exception as e:
        say('未能自动打开浏览器: %s' % e)
    try:
        proc.wait()
    except KeyboardInterrupt:
        say('')
        say('正在停止服务...')
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    return 0


if __name__ == '__main__':
    sys.exit(main())
