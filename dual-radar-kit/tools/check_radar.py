# -*- coding: utf-8 -*-
"""雷达体检器：给一个地址，逐层告诉你卡在哪一环。

它做的事（每一层都单独给结论，不再是一句"连接失败"）：
  ① TCP 能不能连上        —— 端口有没有服务在听 / 防火墙 / IP 写错
  ② WebSocket 握手能不能成 —— 对方到底是不是一个 WebSocket 服务
  ③ 有没有帧、帧率多少     —— 服务在听但没数据？还是 30Hz/20Hz？
  ④ 帧格式合不合规         —— JSON？x/height/jump/crouch/confidence/ts 齐不齐？
  ⑤ 数据质量干不干净       —— x 是不是恒定不动、跳/蹲有没有信号、ts 是不是毫秒
  ⑥ 端到端延迟             —— 用 ts 算出来的采集→本机延迟

用法（只要 Python 3，不需要 pip 装任何东西）：
    python check_radar.py ws://127.0.0.1:8766
    python check_radar.py ws://192.168.1.23:8766 --seconds 6
    python check_radar.py ws://192.168.1.23:8766 --frames 60

配套用法：
    · 这台机器上没有雷达、想先确认「游戏侧链路」通不通：
        先跑 python fake_radar_ws.py，再 python check_radar.py ws://127.0.0.1:8766
    · 雷达在另一台机器上：先在「雷达那台机器」上跑本脚本测 ws://127.0.0.1:8766，
      确认服务本身合规；再换 IP 测试时用「组员这台机器」跑 ws://<雷达机器IP>:8766。
"""
import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
OK, BAD, WARN = '[OK]  ', '[FAIL]', '[注意]'


def parse_ws_url(url):
    if url.startswith('ws://'):
        rest, secure = url[5:], False
    elif url.startswith('wss://'):
        rest, secure = url[6:], True
    else:
        raise ValueError('地址要以 ws:// 开头（例如 ws://192.168.1.23:8766），当前是：%s' % url)
    if '/' in rest:
        hostport, path = rest.split('/', 1)
        path = '/' + path
    else:
        hostport, path = rest, '/'
    if ':' in hostport:
        host, port = hostport.rsplit(':', 1)
        port = int(port)
    else:
        host, port = hostport, 443 if secure else 80
    return host, port, path, secure


def recv_exact(sock, buf, n):
    while len(buf) < n:
        chunk = sock.recv(65536)
        if not chunk:
            return None, buf
        buf += chunk
    return buf[:n], buf[n:]


def read_frame(sock, buf):
    """解析一个服务端→客户端的 WebSocket 帧，返回 (opcode, payload, rest)。"""
    head, buf = recv_exact(sock, buf, 2)
    if head is None:
        return None, None, buf
    b0, b1 = head[0], head[1]
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    length = b1 & 0x7F
    if length == 126:
        ext, buf = recv_exact(sock, buf, 2)
        if ext is None:
            return None, None, buf
        length = struct.unpack('!H', ext)[0]
    elif length == 127:
        ext, buf = recv_exact(sock, buf, 8)
        if ext is None:
            return None, None, buf
        length = struct.unpack('!Q', ext)[0]
    mask = b''
    if masked:
        mask, buf = recv_exact(sock, buf, 4)
    payload, buf = recv_exact(sock, buf, length)
    if payload is None:
        return None, None, buf
    if masked:
        payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    return opcode, payload, buf


def main():
    ap = argparse.ArgumentParser(description='雷达 WebSocket 体检器')
    ap.add_argument('url', help='雷达地址，例如 ws://127.0.0.1:8766')
    ap.add_argument('--frames', type=int, default=40, help='至少收多少帧，默认 40')
    ap.add_argument('--seconds', type=float, default=5.0, help='最多观察多少秒，默认 5')
    args = ap.parse_args()

    print('=' * 64)
    print(' 雷达体检：%s' % args.url)
    print('=' * 64)

    try:
        host, port, path, secure = parse_ws_url(args.url)
    except ValueError as e:
        print('%s 地址格式不对：%s' % (BAD, e))
        return 2
    if secure:
        print('%s 本脚本只支持 ws://（wss:// 需要证书环境，请在浏览器里试）' % WARN)

    # ① TCP
    t0 = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=5)
    except socket.timeout:
        print('%s ① TCP 连接超时：%s:%d 没响应' % (BAD, host, port))
        print('      · 那台机器上服务没起来？端口写错？防火墙挡了？不在同一网段？')
        return 1
    except OSError as e:
        print('%s ① TCP 连不上 %s:%d —— %s' % (BAD, host, port, e))
        if getattr(e, 'errno', None) in (10061, 111, 61):
            print('      · 这个端口上没有程序在监听（服务没启动 / 端口填错）')
            print('      · 想先确认「游戏侧链路」没问题：在这台电脑上跑 python fake_radar_ws.py')
        return 1
    print('%s ① TCP 已连接 %s:%d（%.0f ms）' % (OK, host, port, (time.time() - t0) * 1000))

    # ② 握手
    key = base64.b64encode(os.urandom(16)).decode()
    req = ('GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
           'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n' % (path, host, port, key))
    try:
        sock.sendall(req.encode('latin-1'))
        sock.settimeout(5)
        head = b''
        while b'\r\n\r\n' not in head and len(head) < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
    except OSError as e:
        print('%s ② 握手阶段连接断了：%s' % (BAD, e))
        return 1

    text = head.decode('latin-1', 'replace')
    status_line = text.split('\r\n', 1)[0] if text else '(无响应)'
    if ' 101' not in status_line:
        print('%s ② 对方不是 WebSocket 服务：回的是一句 HTTP 响应 → %s' % (BAD, status_line.strip()))
        print('      · 端口通了、但协议不对：那个端口上跑的是普通 HTTP / 别的服务')
        print('      · 请确认雷达的 WebSocket 端口（我们约定 8766），或让雷达侧提供 ws 服务')
        return 1
    expect = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
    if expect.lower() not in text.lower():
        print('%s ② 握手回声不对（Sec-WebSocket-Accept 不匹配）' % WARN)
    else:
        print('%s ② WebSocket 握手成功（101 Switching Protocols）' % OK)

    # ③④⑤ 收帧
    buf = head.split(b'\r\n\r\n', 1)[1]
    got, bytes_total, first_ts, last_ts = 0, 0, None, None
    xs, jumps, crouches = [], [], []
    field_seen = set()
    optional_acc = {'height': 0, 'confidence': 0}
    field_total = 0
    bad_json = 0
    non_text = 0
    t_start = time.time()
    sock.settimeout(3)
    while got < args.frames and time.time() - t_start < args.seconds:
        try:
            opcode, payload, buf = read_frame(sock, buf)
        except socket.timeout:
            break
        if opcode is None:
            break
        if opcode == 0x8:
            print('%s ③ 服务端主动关闭了连接（close 帧）' % WARN)
            break
        if opcode in (0x9, 0xA):
            continue
        if opcode == 0x2:
            non_text += 1
            continue
        if opcode != 0x1:
            continue
        bytes_total += len(payload)
        got += 1
        try:
            d = json.loads(payload.decode('utf-8'))
        except Exception:
            bad_json += 1
            continue
        if not isinstance(d, dict):
            bad_json += 1
            continue
        if not field_seen:
            field_seen = set(d.keys())
        field_total += 1
        for k in optional_acc:
            if k in d:
                optional_acc[k] += 1
        if isinstance(d.get('x'), (int, float)):
            xs.append(float(d['x']))
        for k, box in (('jump', jumps), ('crouch', crouches)):
            v = d.get(k)
            if isinstance(v, bool):
                box.append(1.0 if v else 0.0)
            elif isinstance(v, (int, float)):
                box.append(float(v))
        ts = d.get('ts')
        if isinstance(ts, (int, float)) and ts > 1e11:
            last_ts = float(ts)
            if first_ts is None:
                first_ts = float(ts)
    elapsed = time.time() - t_start

    if got == 0:
        print('%s ③ 握手成功但一帧都没收到（%.1fs）' % BAD, )
        print('      · 服务在听、但没往这条连接推数据：雷达没采到人 / 推送逻辑没启动')
        print('      · 或者它推的是二进制帧（本次收到 %d 个二进制帧）' % non_text)
        return 1
    print('%s ③ 收到 %d 帧 / %.1fs = %.1f Hz，共 %.1f KB' % (OK, got, elapsed, got / max(elapsed, 1e-6),
                                                            bytes_total / 1024.0))
    if bad_json:
        print('%s ④ 其中 %d 帧 JSON 解析失败（非 JSON：游戏会直接忽略这些帧）' % (BAD, bad_json))
    else:
        print('%s ④ 每帧都是合法 JSON 对象' % OK)
    need = {'x', 'ts'}
    miss = sorted(n for n in need if n not in field_seen)
    if miss:
        print('%s ④ 缺关键字段 %s —— 游戏需要 x（横向位置，左负右正）%s'
              % (BAD, miss, '，ts 用来算延迟' if 'ts' in miss else ''))
    else:
        print('%s ④ 关键字段齐全；本连接见过的字段：%s' % (OK, ', '.join(sorted(field_seen))))
    for k in ('height', 'confidence', 'jump', 'crouch'):
        if k not in field_seen:
            print('      · 没见到 %s（可选字段；缺失时游戏会用默认值）' % k)

    if len(xs) >= 2:
        span = max(xs) - min(xs)
        avg = sum(xs) / len(xs)
        if span < 0.05:
            print('%s ⑤ x 几乎不动：跨度 %.3f（均值 %.3f）→ 探测区没人走动，或 x 根本没接上'
                  % (WARN, span, avg))
        else:
            print('%s ⑤ x 有变化：跨度 %.3f，范围 %.3f .. %.3f，均值 %.3f'
                  % (OK, span, min(xs), max(xs), avg))
        if abs(avg) > 0.35:
            print('      · 静息基线偏离 0 较多（均值 %.3f）：人可能一直站在一侧，或上游没做去基线' % avg)
    if jumps:
        hj = sum(1 for v in jumps if v > 0)
        hc = sum(1 for v in crouches if v > 0)
        print('%s ⑤ jump 命中 %d/%d 帧，crouch 命中 %d/%d 帧（%s）'
              % (OK if (hj or hc) else WARN, hj, len(jumps), hc, len(crouches),
                 '看到跳/蹲信号' if (hj or hc) else '这段时间没跳没蹲，正常'))
    if last_ts is not None:
        lag = time.time() * 1000 - last_ts
        print('%s ⑥ ts 是毫秒时间戳，端到端延迟约 %.0f ms（越大说明采集/网络越慢）' % (OK, lag))
    else:
        print('%s ⑥ 没有可用的 ts 毫秒时间戳，联调台的延迟一栏会显示 —' % WARN)

    print('-' * 64)
    if bad_json == 0 and not miss and got >= 5:
        print(' 结论：这是一个能用的雷达 WebSocket。把同一个地址填进联调台即可。')
        print('       跨机器时记得：服务绑 0.0.0.0 + 防火墙放行 + 同网段。')
    else:
        print(' 结论：能连上，但帧格式还有问题（见上面 [FAIL] 行），把这几行截图发给开发。')
    try:
        sock.close()
    except Exception:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
