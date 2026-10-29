# -*- coding: utf-8 -*-
"""零依赖假雷达：用「格式完全正确的雷达数据」把 WebSocket 推出来，专门用来判断
「到底是游戏/联调台这条链路的问题，还是雷达服务本身的问题」。

什么时候用它：
    真实雷达连不上时，先在这台电脑上跑本脚本，然后在联调台里连 ws://127.0.0.1:8766
      - 能连上、数值在动  =>  浏览器 / 地址 / 防火墙 / 端口都没问题，问题在雷达服务那一侧
      - 仍然连不上        =>  问题在这台电脑（端口被占、防火墙、页面用 https 打开等）

用法（只需要 Python 3，不需要 pip 装任何东西）：
    python fake_radar_ws.py                     # ws://0.0.0.0:8766，默认档 good（数值跟着人来回走）
    python fake_radar_ws.py --profile still     # 静息档：不动，用来试校准向导第 1 步
    python fake_radar_ws.py --port 9000         # 换端口
    python fake_radar_ws.py --profile noisy     # 静息但误报跳/蹲，用来试向导的判噪分支

    地址后面加路径也能切档（不用重启）：
    ws://127.0.0.1:8766/good  ws://127.0.0.1:8766/still  ws://127.0.0.1:8766/noisy
    ws://127.0.0.1:8766/weak  ws://127.0.0.1:8766/inverted

档位说明：
    good      正常：横向来回走（±0.6）、每 10 秒一次跳和一次蹲 —— 5 步校准能一次过
    still     静息：x 几乎不动、没有跳/蹲 —— 用来验证判噪与阈值学习
    noisy     静息但上游误报频繁（约一半帧报跳/蹲）—— 向导会判「噪」并停用雷达跳/蹲
    weak      横向幅度只有 ±0.34（峰值 < 0.40）—— 向导会提示「太弱」
    inverted  左右装反（x 取负）—— 向导会学出 sign=-1 自动纠正，供验证
"""
import argparse
import base64
import hashlib
import json
import math
import random
import socket
import struct
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
DT = 1.0 / 20.0                       # 20Hz：游戏与校准向导都按这个节奏设计
PROFILES = ('good', 'still', 'noisy', 'weak', 'inverted')


# ---------------------------------------------------------------- 数据生成

def gestures(t, amp):
    """每 10 秒给一次跳（0~0.3s）、每 10 秒给一次蹲（5.0~5.3s），形成干净的上升沿。"""
    jump = amp if (t % 10.0) < 0.3 else 0.0
    crouch = amp if 5.0 <= (t % 10.0) < 5.3 else 0.0
    return jump, crouch


def frame_values(profile, t):
    """按档位算出这一帧的 (x, jump, crouch, height, confidence)。"""
    if profile == 'still':
        return 0.02 + random.gauss(0, 0.004), 0.0, 0.0, 0.0, 0.95

    if profile == 'noisy':
        x = 0.02 + random.gauss(0, 0.02)
        return x, float(random.random() < 0.5), float(random.random() < 0.5), 0.0, 0.85

    if profile == 'weak':
        x = 0.34 * math.sin(2 * math.pi * t / 8.0)
        jump, crouch = gestures(t, 0.22)
        return x, jump, crouch, 0.0, 0.9

    if profile == 'inverted':                     # 左右装反
        x = -(0.6 * math.sin(2 * math.pi * t / 8.0)) + 0.02
        jump, crouch = gestures(t, 0.9)
        return x, jump, crouch, 0.0, 0.9

    x = 0.6 * math.sin(2 * math.pi * t / 8.0) + 0.02   # good
    jump, crouch = gestures(t, 0.9)
    return x, jump, crouch, 0.0, 0.9


# ---------------------------------------------------------------- WebSocket

def read_request(conn):
    data = b''
    while b'\r\n\r\n' not in data and len(data) < 65536:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        data += chunk
    return data


def do_handshake(conn, req):
    key = None
    for line in req.decode('latin-1').split('\r\n'):
        if line.lower().startswith('sec-websocket-key:'):
            key = line.split(':', 1)[1].strip()
    if not key:
        return False
    accept = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode('ascii')
    conn.sendall(('HTTP/1.1 101 Switching Protocols\r\n'
                  'Upgrade: websocket\r\n'
                  'Connection: Upgrade\r\n'
                  'Sec-WebSocket-Accept: %s\r\n\r\n' % accept).encode('latin-1'))
    return True


def send_text(conn, text):
    payload = text.encode('utf-8')
    n = len(payload)
    if n < 126:
        head = struct.pack('!BB', 0x81, n)
    elif n < 65536:
        head = struct.pack('!BBH', 0x81, 126, n)
    else:
        head = struct.pack('!BBQ', 0x81, 127, n)
    conn.sendall(head + payload)


def drain(conn):
    """读并丢弃客户端发来的东西（我们只推不收）；客户端一断开就让发送端退出。"""
    try:
        while True:
            if not conn.recv(4096):
                break
    except Exception:
        pass
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass


def apply_channels(x, jump, crouch, height, conf, channels):
    """按分工裁剪这一路要发的维度。

    lr = 只发横向（模拟「管左右」的那台）：jump/crouch/height 一律 0
    ud = 只发上下（模拟「管上下」的那台）：x 固定 0
    both = 全字段（单雷达 / 想验证"两台都发全字段也不会串台"时用）

    游戏侧本来就会按通道裁剪，这里只是让假雷达更像真分工现场。
    """
    if channels == 'lr':
        return x, 0.0, 0.0, 0.0, conf
    if channels == 'ud':
        return 0.0, jump, crouch, height, conf
    return x, jump, crouch, height, conf


def serve_client(conn, addr, default_profile, live, channels='both'):
    profile = default_profile
    try:
        conn.settimeout(10)
        req = read_request(conn)
        if not req or not do_handshake(conn, req):
            return
        try:
            target = req.split(b' ')[1].decode('latin-1')
        except Exception:
            target = '/'
        segs = [s for s in target.split('/') if s]
        if segs and segs[0].lower() in PROFILES:
            profile = segs[0].lower()
        conn.settimeout(None)

        with live['lock']:
            live['n'] += 1
            n = live['n']
        print('  [%s] %s 接入 → 档位 %s（当前 %d 个客户端）'
              % (time.strftime('%H:%M:%S'), addr[0], profile, n), flush=True)
        threading.Thread(target=drain, args=(conn,), daemon=True).start()

        t0 = time.time()
        i = 0
        while True:                                   # 绝对时间对齐，稳定 20Hz
            due = t0 + i * DT
            gap = due - time.time()
            if gap > 0:
                time.sleep(gap)
            x, jump, crouch, height, conf = frame_values(profile, i * DT)
            x, jump, crouch, height, conf = apply_channels(
                x, jump, crouch, height, conf, channels)
            send_text(conn, json.dumps({
                'ts': int(time.time() * 1000),
                'x': round(x, 4),
                'height': height,
                'jump': jump,
                'crouch': crouch,
                'confidence': conf,
                'raw': {'src': 'fake_radar', 'profile': profile,
                        'channels': channels, 'frame': i},
            }, ensure_ascii=False))
            i += 1
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
        with live['lock']:
            live['n'] -= 1
        print('  [%s] %s 断开' % (time.strftime('%H:%M:%S'), addr[0]), flush=True)


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def start_server(host, port, profile, channels, live):
    """在一个后台线程里 accept 连接；返回监听 socket（失败返回 None）。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((host, port))
    except OSError as e:
        print('绑定 %s:%d 失败：%s' % (host, port, e), flush=True)
        print('端口被占用？换一个：python fake_radar_ws.py --port 8767', flush=True)
        return None
    srv.listen(8)

    def loop():
        while True:
            try:
                conn, addr = srv.accept()
            except Exception:
                return
            threading.Thread(target=serve_client,
                             args=(conn, addr, profile, live, channels),
                             daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser(description='零依赖假雷达（WebSocket 推 JSON 帧）')
    ap.add_argument('--host', default='0.0.0.0', help='监听地址，默认 0.0.0.0（局域网可访问）')
    ap.add_argument('--port', type=int, default=8766, help='监听端口，默认 8766')
    ap.add_argument('--profile', default='good', choices=PROFILES, help='数据档位，默认 good')
    ap.add_argument('--channels', default='both', choices=('both', 'lr', 'ud'),
                    help='分工：both=全字段（默认）；lr=只发横向；ud=只发上下')
    ap.add_argument('--port2', type=int, default=None,
                    help='再加一路：同一进程里用 --channels2 的分工监听第二个端口'
                         '（例：--port 8766 --channels lr --port2 8767）')
    ap.add_argument('--channels2', default='ud', choices=('both', 'lr', 'ud'),
                    help='第二路的分工，默认 ud（配合 --port2）')
    args = ap.parse_args()

    live = {'n': 0, 'lock': threading.Lock()}
    srv = start_server(args.host, args.port, args.profile, args.channels, live)
    if srv is None:
        return 1

    ip = local_ip()
    print('=' * 62, flush=True)
    print(' 假雷达已启动  ws://%s:%d    档位 %s    分工 %s'
          % (args.host, args.port, args.profile, args.channels), flush=True)
    print(' 本机联调台就填：  ws://127.0.0.1:%d' % args.port, flush=True)
    if ip:
        print(' 别的电脑连它：    ws://%s:%d' % (ip, args.port), flush=True)

    if args.port2:
        srv2 = start_server(args.host, args.port2, args.profile, args.channels2, live)
        if srv2 is None:
            return 1
        print(' 第二路（双雷达）：ws://127.0.0.1:%d    分工 %s'
              % (args.port2, args.channels2), flush=True)

    print(' 切档不用重启，地址后加路径即可，例如 ws://127.0.0.1:%d/noisy' % args.port, flush=True)
    print(' 档位：' + ' / '.join(PROFILES), flush=True)
    if args.port2:
        print(' 双雷达进游戏：index.html?input=radar&radar=ws://127.0.0.1:%d'
              '&radar2=ws://127.0.0.1:%d' % (args.port, args.port2), flush=True)
    print(' 停止：Ctrl+C', flush=True)
    print('=' * 62, flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n已停止', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
