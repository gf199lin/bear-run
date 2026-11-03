# -*- coding: utf-8 -*-
"""端到端验证（临时）：fake 雷达 → 两个 bridge（后台判定）→ hub → 探针。

跑法：py -3 .deepworks/tmp/e2e_test.py
预期：探针收到 left/right（role=lr）与 jump/slide（role=ud），
      且 lr 路永远不出 jump/slide、ud 路永远不出 left/right。
"""
import base64
import json
import os
import socket
import subprocess
import sys
import time

ROOT = r'<PROJECT_ROOT>'
TOOLS = os.path.join(ROOT, 'radar-runner', 'tools')
TMP = os.path.join(ROOT, '.deepworks', 'tmp')
sys.path.insert(0, TOOLS)
from radar_hub import _parse_ws_url, _read_frame, _send_frame  # noqa: E402

PY = sys.executable
LOG = {}


def start(name, args):
    out = open(os.path.join(TMP, name + '.log'), 'w', encoding='utf-8', errors='replace')
    err = open(os.path.join(TMP, name + '.err'), 'w', encoding='utf-8', errors='replace')
    p = subprocess.Popen([PY, '-u'] + args, cwd=TOOLS, stdout=out, stderr=err,
                         creationflags=0x08000000)   # CREATE_NO_WINDOW
    LOG[name] = (p, out, err)
    print('[boot] %-6s pid=%s' % (name, p.pid), flush=True)
    return p


def probe(url, seconds, expect):
    host, port, path = _parse_ws_url(url)
    sock = socket.create_connection((host, port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode('ascii')
    sock.sendall(('GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n'
                  'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
                  'Sec-WebSocket-Version: 13\r\n\r\n'
                  % (path, host, port, key)).encode('latin-1'))
    head = b''
    while b'\r\n\r\n' not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError('握手被断开')
        head += chunk
    if b' 101 ' not in head.split(b'\r\n')[0]:
        raise RuntimeError('不是 WS：%s' % head[:120])
    print('[probe] 已连上 %s' % url, flush=True)

    sock.settimeout(1.0)
    t0 = time.time()
    counts = {}
    by_role = {}
    states = 0
    last = None
    bad = []
    while time.time() - t0 < seconds:
        try:
            opcode, fin, payload = _read_frame(sock)
        except socket.timeout:
            continue
        if opcode == 0x9:
            _send_frame(sock, 0xA, payload)
            continue
        if opcode != 0x1:
            continue
        try:
            obj = json.loads(payload.decode('utf-8'))
        except Exception:
            continue
        if obj.get('type') == 'action':
            act, role = obj.get('action'), obj.get('role')
            counts[act] = counts.get(act, 0) + 1
            by_role.setdefault(role, {})
            by_role[role][act] = by_role[role].get(act, 0) + 1
            print('  ACTION %-6s role=%s lane=%s' % (act, role, obj.get('lane')), flush=True)
            allowed = {'lr': ('left', 'right'), 'ud': ('jump', 'slide')}.get(role, ())
            if act not in allowed:
                bad.append((role, act))
        else:
            states += 1
            last = obj
    sock.close()

    print('-' * 62, flush=True)
    print('[probe] 动作计数 %s' % counts, flush=True)
    print('[probe] 按分工   %s' % by_role, flush=True)
    print('[probe] state 帧 %d' % states, flush=True)
    if last:
        print('[probe] 末状态 x=%s jump=%s crouch=%s lr_ok=%s ud_ok=%s'
              % (last.get('x'), last.get('jump'), last.get('crouch'),
                 last.get('lr_ok'), last.get('ud_ok')), flush=True)

    ok = True
    if bad:
        print('[FAIL] 分工串台：%s' % bad, flush=True)
        ok = False
    for a in expect:
        if not counts.get(a):
            print('[FAIL] 没收到预期动作 %s' % a, flush=True)
            ok = False
    if states == 0:
        print('[FAIL] 没有 state 帧', flush=True)
        ok = False
    print('[RESULT] %s' % ('PASS' if ok else 'FAIL'), flush=True)
    return ok


def main():
    procs = []
    try:
        procs.append(start('fake_lr', [
            'fake_radar_ws.py', '--port', '8866', '--profile', 'good',
            '--udp-out', '127.0.0.1:8888', '--udp-channels', 'lr']))
        procs.append(start('fake_ud', [
            'fake_radar_ws.py', '--port', '8867', '--profile', 'good',
            '--udp-out', '127.0.0.1:8889', '--udp-channels', 'ud']))
        time.sleep(1.5)

        procs.append(start('bridge_lr', [
            'radar_bridge.py', '--from', 'udp', '--udp-port', '8888',
            '--role', 'lr', '--judge', '--port', '9001', '--host', '127.0.0.1']))
        procs.append(start('bridge_ud', [
            'radar_bridge.py', '--from', 'udp', '--udp-port', '8889',
            '--role', 'ud', '--judge', '--port', '9002', '--host', '127.0.0.1']))
        time.sleep(2.0)

        procs.append(start('hub', [
            'radar_hub.py', '--lr', 'ws://127.0.0.1:9001',
            '--ud', 'ws://127.0.0.1:9002', '--port', '8766', '--host', '127.0.0.1']))
        time.sleep(1.5)

        # fake 的 good 档：横向 8 秒一个来回，跳 10 秒一次、蹲 10 秒一次（5.0~5.3s）
        ok = probe('ws://127.0.0.1:8766', 22.0, ['left', 'right', 'jump', 'slide'])
    finally:
        for name, (p, out, err) in LOG.items():
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(0.5)
        for name, (p, out, err) in LOG.items():
            try:
                p.kill()
            except Exception:
                pass
            out.close()
            err.close()

    print('=' * 62, flush=True)
    for name in ('bridge_lr', 'bridge_ud', 'hub'):
        path = os.path.join(TMP, name + '.log')
        if os.path.exists(path):
            print('--- %s.log 尾部 ---' % name, flush=True)
            with open(path, encoding='utf-8', errors='replace') as f:
                lines = [ln.rstrip() for ln in f if ln.strip()]
            for ln in lines[-6:]:
                print('  ' + ln, flush=True)
            e = os.path.join(TMP, name + '.err')
            if os.path.exists(e):
                with open(e, encoding='utf-8', errors='replace') as f:
                    errs = [ln.rstrip() for ln in f if ln.strip()]
                if errs:
                    print('  [stderr] ' + errs[-1], flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
