# -*- coding: utf-8 -*-
"""扫描雷达实际能覆盖的方位扇区 —— 判断"雷达到底看得到哪一侧"。

为什么需要这个：
    雷达斜放时，波束并不覆盖正前方。2026-11-19 空场景实测：
    有回波的只有 -35°~-10°（显示器/桌子那一侧），+5° 以上一帧都没有。
    人站"正前方"看不见，不是参数问题，是波束没打到那里。

用法：
    # 1) 先停掉正式服务
    #    POST http://127.0.0.1:8778/api/stop

    # 2) 起一个"保留全部静止"的诊断实例（否则站着不动的东西会被 MTI 减掉）
    py -3 dual-radar-kit/tools/radar_live_ws.py --port COM3 --role lr ^
         --ws-port 8781 --clutter-keep 0.0

    # 3) 扫描
    py -3 tools/sector_scan.py
    py -3 tools/sector_scan.py --secs 15 --port 8781

    # 4) 关掉诊断实例，重新 POST /api/start

判读：
    · 直方图集中在哪个角度段，那里就是波束真正覆盖的方向。
    · 空的那些角度段，人站过去也看不见 —— 要转雷达，不是调参数。
    · n 长期为 0 而 az 有值 = 跟踪器在报上一次的残留角度，不是真有目标。
"""
import argparse
import json
import socket
import struct
import sys
import time
from collections import Counter

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def ws_open(host, port, timeout=3.0):
    s = socket.create_connection((host, port), timeout=timeout)
    import base64
    import os as _os
    key = base64.b64encode(_os.urandom(16)).decode('ascii')
    req = ('GET / HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n'
           'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
           'Sec-WebSocket-Version: 13\r\n\r\n') % (host, port, key)
    s.sendall(req.encode('ascii'))
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise IOError('握手时连接被关闭')
        buf += chunk
    head, rest = buf.split(b'\r\n\r\n', 1)
    if b'101' not in head.split(b'\r\n')[0]:
        raise IOError('握手失败：%s' % head.split(b'\r\n')[0].decode('latin-1'))
    s.setblocking(False)
    return s, rest


def collect(sock, rest, secs):
    out = []
    buf = rest
    deadline = time.time() + secs
    while time.time() < deadline:
        while len(buf) >= 2:
            b0, b1 = buf[0], buf[1]
            op = b0 & 0x0F
            ln = b1 & 0x7F
            off = 2
            if ln == 126:
                if len(buf) < 4:
                    break
                ln = struct.unpack('!H', buf[2:4])[0]
                off = 4
            elif ln == 127:
                if len(buf) < 10:
                    break
                ln = struct.unpack('!Q', buf[2:10])[0]
                off = 10
            if b1 & 0x80:
                off += 4
            if len(buf) < off + ln:
                break
            pay = buf[off:off + ln]
            buf = buf[off + ln:]
            if op != 0x1:
                continue
            try:
                out.append(json.loads(pay.decode('utf-8')))
            except Exception:
                pass
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        except BlockingIOError:
            time.sleep(0.01)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8781, help='诊断实例端口')
    ap.add_argument('--secs', type=float, default=12.0)
    ap.add_argument('--bucket', type=float, default=5.0)
    a = ap.parse_args()

    try:
        sock, rest = ws_open('127.0.0.1', a.port)
    except Exception as e:
        print('连不上 127.0.0.1:%d —— %s' % (a.port, e))
        print('先起诊断实例（--clutter-keep 0.0），见本文件顶部说明。')
        return 2

    frames = collect(sock, rest, a.secs)
    try:
        sock.close()
    except Exception:
        pass

    if not frames:
        print('%d 秒内没收到帧' % a.secs)
        return 1

    azs, ns, zeros = [], [], []
    for f in frames:
        r = f.get('raw', {})
        if isinstance(r.get('az'), (int, float)):
            azs.append(r['az'])
        if isinstance(r.get('n'), (int, float)):
            ns.append(r['n'])
        if isinstance(r.get('zero'), (int, float)):
            zeros.append(r['zero'])

    print('采到 %d 帧（%.1f Hz）' % (len(frames), len(frames) / a.secs))
    print('')

    print('=== 点数 ===')
    c = Counter(ns)
    pos = sum(1 for x in ns if x > 0)
    for k in sorted(c):
        print('  n=%-3s %4d 帧' % (k, c[k]))
    print('  有回波的帧 %d/%d (%.0f%%)' % (pos, len(ns), 100.0 * pos / max(1, len(ns))))

    if zeros:
        print('')
        print('  zero（中线）: %s' % ', '.join('%+.1f' % z for z in sorted(set(round(v, 1) for v in zeros))[:6]))

    print('')
    print('=== 跟踪方位角直方图（%.0f° 一桶）===' % a.bucket)
    if not azs:
        print('  完全没有方位角输出')
        return 0
    bins = Counter(int(x // a.bucket) * int(a.bucket) for x in azs)
    mx = max(bins.values())
    print('  az 范围 [%.1f, %.1f]  中位 %.1f' % (min(azs), max(azs), sorted(azs)[len(azs) // 2]))
    print('')
    for b in range(-45, 45, int(a.bucket)):
        v = bins.get(b, 0)
        bar = '#' * int(40.0 * v / mx) if v else ''
        mark = '  <-- 空' if v == 0 else ''
        print('  %+4d°~%+4d° %4d 帧 %s%s' % (b, b + int(a.bucket), v, bar, mark))
    return 0


if __name__ == '__main__':
    sys.exit(main())
