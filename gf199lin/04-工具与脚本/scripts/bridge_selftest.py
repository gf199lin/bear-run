# -*- coding: utf-8 -*-
"""radar_bridge.py（零依赖版）端到端自测：UDP / TCP / WS 三种上游 -> ws:// 出。"""
import json
import os
import socket
import subprocess
import sys
import time

import websocket

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
BRIDGE = os.path.join(ROOT, 'radar-runner', 'tools', 'radar_bridge.py')
FAKE = os.path.join(ROOT, 'radar-runner', 'tools', 'fake_radar_ws.py')

FAILS = []
PROCS = []


def check(ok, msg):
    print(('  [OK] ' if ok else '  [FAIL] ') + msg)
    if not ok:
        FAILS.append(msg)


def free_port(kind='tcp'):
    s = socket.socket(socket.AF_INET,
                      socket.SOCK_STREAM if kind == 'tcp' else socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start(args, tag):
    p = subprocess.Popen([sys.executable] + args, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True,
                         encoding='utf-8', errors='replace')
    PROCS.append((tag, p))
    return p


def stop_all():
    for tag, p in PROCS:
        p.terminate()
    for tag, p in PROCS:
        try:
            out = p.communicate(timeout=5)[0]
        except Exception:
            p.kill()
            out = ''
        print('--- %s 日志 ---' % tag)
        print((out or '').strip())


try:
    # ============ 阶段 1：UDP 上游 ============
    print('\n== 阶段1: UDP -> ws ==')
    ws_port, udp_port = free_port(), free_port('udp')
    start([BRIDGE, '--in', 'udp', '--udp-port', str(udp_port), '--port', str(ws_port),
           '--x-gain', '1.25'], 'bridge-udp')
    time.sleep(1.2)
    ws = websocket.create_connection('ws://127.0.0.1:%d' % ws_port, timeout=8)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    s.sendto(json.dumps({'px_m': 0.64, 'jump': 1, 'conf': 0.9}).encode(),
             ('127.0.0.1', udp_port))
    f1 = json.loads(ws.recv())
    print('  帧:', json.dumps(f1, ensure_ascii=False))
    check(abs(f1['x'] - 0.8) < 1e-6, 'x 换算 0.64*1.25=0.8，实际 %s' % f1['x'])
    check(abs(f1['jump'] - 1) < 1e-6, 'jump 透传')
    check(abs(f1['confidence'] - 0.9) < 1e-6, 'confidence 透传')
    check('px_m' in f1.get('raw', {}), 'raw 原始字段保留')
    check(bool(f1.get('ts')), 'ts 自动补全')

    s.sendto(b'0.0,0,0,0,1.0', ('127.0.0.1', udp_port))
    f2 = json.loads(ws.recv())
    check(abs(f2['x']) < 1e-6, '非 JSON 行按位置解析')

    s.sendto(b'{"hello":"world"}', ('127.0.0.1', udp_port))
    ws.settimeout(1.0)
    try:
        extra = ws.recv()
        check(False, '无 x 的包应丢弃，却收到 %s' % extra)
    except Exception:
        check(True, '无 x 的包被丢弃')

    ws2 = websocket.create_connection('ws://127.0.0.1:%d' % ws_port, timeout=8)
    time.sleep(0.3)
    s.sendto(json.dumps({'x': -0.5}).encode(), ('127.0.0.1', udp_port))
    a, b = json.loads(ws.recv()), json.loads(ws2.recv())
    print('  广播: A=%s  B=%s（阶段1 带 --x-gain 1.25，期望 -0.5*1.25=-0.625）'
          % (a.get('x'), b.get('x')))
    check(a['x'] == b['x'] == -0.625, '多客户端广播一致 (A=%s B=%s)' % (a.get('x'), b.get('x')))
    ws2.close()
    ws.close()

    # ============ 阶段 2：TCP 上游 ============
    print('\n== 阶段2: TCP -> ws ==')
    ws_port2, tcp_port = free_port(), free_port()
    start([BRIDGE, '--in', 'tcp', '--tcp-port', str(tcp_port), '--port', str(ws_port2)],
          'bridge-tcp')
    time.sleep(1.2)
    ws = websocket.create_connection('ws://127.0.0.1:%d' % ws_port2, timeout=8)
    c = socket.create_connection(('127.0.0.1', tcp_port), timeout=5)
    c.sendall(b'{"x": -0.42, "crouch": 1, "ts": 1730000000000}\n')
    f3 = json.loads(ws.recv())
    print('  帧:', json.dumps(f3, ensure_ascii=False))
    check(abs(f3['x'] + 0.42) < 1e-6, 'TCP 行 JSON 解析')
    check(abs(f3['crouch'] - 1) < 1e-6, 'crouch 透传')
    check(f3['ts'] == 1730000000000, '上游 ts 毫秒原样透传')
    c.close()
    ws.close()

    # ============ 阶段 3：WS 上游（用项目自带的零依赖假雷达） ============
    print('\n== 阶段3: ws -> ws（上游 = tools/fake_radar_ws.py） ==')
    up_port, ws_port3 = free_port(), free_port()
    start([FAKE, '--port', str(up_port), '--profile', 'good'], 'fake-radar-upstream')
    time.sleep(1.2)
    start([BRIDGE, '--in', 'ws', '--url', 'ws://127.0.0.1:%d' % up_port,
           '--port', str(ws_port3)], 'bridge-ws')
    time.sleep(1.5)
    ws = websocket.create_connection('ws://127.0.0.1:%d' % ws_port3, timeout=8)
    f4 = json.loads(ws.recv())
    f5 = json.loads(ws.recv())
    print('  帧:', json.dumps(f4, ensure_ascii=False))
    check(f4.get('raw', {}).get('profile') == 'good', 'WS 上游 raw 里带 profile')
    check('x' in f4 and 'jump' in f4, 'WS 上游帧字段完整')
    check(f4['ts'] != f5['ts'] or f4['x'] != f5['x'], '连续帧在更新')
    ws.close()

finally:
    stop_all()

print()
if FAILS:
    print('FAIL %d:' % len(FAILS))
    for f in FAILS:
        print('  -', f)
    sys.exit(1)
print('全部 PASS：UDP / TCP / WS 三种上游，字段映射、x 换算、raw 透传、'
      '垃圾包丢弃、多客户端广播、ts 透传 均正常')
