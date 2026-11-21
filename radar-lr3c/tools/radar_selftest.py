# -*- coding: utf-8 -*-
"""雷达链路自检：确认两块板都连着、都在出帧。

只读，不干扰正在跑的游戏页（hub 是广播，多客户端并存）。

用法：
    py -3 tools/radar_selftest.py
    py -3 tools/radar_selftest.py --secs 5
"""
import argparse
import base64
import json
import os
import socket
import struct
import sys
import time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def ws_open(host, port, timeout=3.0):
    """最小 WebSocket 客户端（RFC6455，仅文本帧，够读本地 hub 用）。"""
    s = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode('ascii')
    req = (
        'GET / HTTP/1.1\r\n'
        'Host: %s:%d\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        'Sec-WebSocket-Key: %s\r\n'
        'Sec-WebSocket-Version: 13\r\n'
        '\r\n'
    ) % (host, port, key)
    s.sendall(req.encode('ascii'))

    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise IOError('握手时连接被关闭')
        buf += chunk
    head, rest = buf.split(b'\r\n\r\n', 1)
    if b'101' not in head.split(b'\r\n')[0]:
        raise IOError('握手失败: %s' % head.split(b'\r\n')[0].decode('latin-1'))
    s.setblocking(False)
    return s, rest


def ws_read_available(sock, rest, deadline):
    """把 rest 之后收到的帧尽量读出来，返回文本消息列表。"""
    msgs = []
    buf = rest
    while True:
        # 尝试从 buf 解出一个完整帧
        while len(buf) >= 2:
            b0, b1 = buf[0], buf[1]
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
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
            if masked:
                off += 4
            if len(buf) < off + ln:
                break
            payload = buf[off:off + ln]
            buf = buf[off + ln:]
            if opcode == 0x1:
                try:
                    msgs.append(payload.decode('utf-8'))
                except UnicodeDecodeError:
                    pass
            elif opcode == 0x8:
                return msgs, b'', True
            elif opcode == 0x9:
                # 回 pong
                try:
                    sock.sendall(struct.pack('!BB', 0x8A, 0) )
                except Exception:
                    pass

        if time.time() >= deadline:
            break
        try:
            chunk = sock.recv(65536)
            if not chunk:
                return msgs, b'', True
            buf += chunk
        except BlockingIOError:
            time.sleep(0.01)
        except Exception:
            break
    return msgs, buf, False


def probe(name, port, role, secs):
    print('【%s】127.0.0.1:%d  (%s)' % (name, port, role))
    try:
        sock, rest = ws_open('127.0.0.1', port)
    except Exception as e:
        print('   × 连不上：%s' % e)
        return {'ok': False, 'err': str(e)}

    deadline = time.time() + secs
    msgs, _rest, closed = ws_read_available(sock, rest, deadline)
    try:
        sock.close()
    except Exception:
        pass

    frames = []
    for m in msgs:
        try:
            o = json.loads(m)
        except Exception:
            continue
        if isinstance(o, dict):
            frames.append(o)

    if not frames:
        print('   × 连上了但 %d 秒内一帧都没收到' % secs)
        print('     板子可能没在出数据（USB 被拔、驱动没起、或板子被别的程序占用）')
        return {'ok': False, 'err': 'no frames'}

    fps = len(frames) / float(secs)
    xs = [f.get('x') for f in frames if isinstance(f.get('x'), (int, float))]
    jumps = sum(1 for f in frames if f.get('jump'))
    ducks = sum(1 for f in frames if f.get('crouch'))
    confs = [f.get('confidence') for f in frames if isinstance(f.get('confidence'), (int, float))]
    ts = [f.get('ts') for f in frames if isinstance(f.get('ts'), (int, float))]

    print('   ✓ 收到 %d 帧 / %d 秒  ≈ %.1f Hz' % (len(frames), secs, fps))
    if ts and len(ts) >= 2:
        span = (ts[-1] - ts[0]) / 1000.0
        if span > 0:
            print('     时间戳跨 %.2f 秒，实际 %.1f Hz' % (span, (len(ts) - 1) / span))
    if xs:
        print('     x 范围 [%.3f, %.3f]  （帧内有效 %d/%d）' % (min(xs), max(xs), len(xs), len(frames)))
    else:
        print('     ! x 字段缺失')
    if confs:
        print('     confidence 中位 %.2f，最低 %.2f' % (sorted(confs)[len(confs) // 2], min(confs)))

    if role == 'lr':
        if jumps or ducks:
            print('     ! 左右板不该出跳/蹲，但收到了 jump=%d crouch=%d（角色配错？）' % (jumps, ducks))
        if xs:
            spread = max(xs) - min(xs)
            if spread < 1e-6:
                print('     ! %d 秒内 x 一直等于 %.3f —— 有帧但没目标在动' % (secs, xs[0]))
                print('       可能人不在视场内，或者 --exclude-az 把人的扇区排掉了')
    else:
        if jumps or ducks:
            print('     ✓ 本段内跳出 %d 帧、蹲出 %d 帧' % (jumps, ducks))
        else:
            print('     · 本段没收到跳/蹲（正常：没人跳蹲时就是 0）')

    return {
        'ok': True, 'frames': len(frames), 'fps': fps,
        'xmin': min(xs) if xs else None, 'xmax': max(xs) if xs else None,
        'jump': jumps, 'crouch': ducks,
    }


def watch(port, secs):
    """实时刷 az / zero / x / 点数，用来让人站好一边看一边调位置。"""
    sock, rest = ws_open('127.0.0.1', port)
    deadline = time.time() + secs
    buf = rest
    n = 0
    print('  时间    az°     zero°    x      点数  置信  车道')
    print('  ' + '-' * 52)
    while time.time() < deadline:
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        except BlockingIOError:
            time.sleep(0.02)
            continue
        while len(buf) >= 2:
            b0, b1 = buf[0], buf[1]
            op = b0 & 0x0F
            ln = b1 & 0x7F
            off = 2
            if ln == 126:
                if len(buf) < 4:
                    break
                ln = struct.unpack('!H', buf[2:4])[0]; off = 4
            elif ln == 127:
                if len(buf) < 10:
                    break
                ln = struct.unpack('!Q', buf[2:10])[0]; off = 10
            if b1 & 0x80:
                off += 4
            if len(buf) < off + ln:
                break
            pay = buf[off:off + ln]; buf = buf[off + ln:]
            if op != 0x1:
                continue
            try:
                o = json.loads(pay.decode('utf-8'))
            except Exception:
                continue
            r = o.get('raw', {})
            n += 1
            if n % 3:
                continue
            az = r.get('az'); z = r.get('zero'); pt = r.get('n')
            flag = ''
            if az is not None and z is not None:
                d = az - z
                if abs(d) > 20:
                    flag = '  <-- 饱和'
            print('  %5.1fs  %s  %s  %-6s %-5s %-5s %s%s' % (
                secs - max(0, deadline - time.time()),
                ('%7.2f' % az) if az is not None else '   None',
                ('%7.2f' % z) if z is not None else '   None',
                ('%6.3f' % o.get('x')) if o.get('x') is not None else '  None',
                pt, o.get('confidence'), r.get('lane'), flag))
    sock.close()
    print('')
    print('  看点数为 0 的行：那是雷达视野里没人，角度是上一次的残留。')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--watch', action='store_true', help='实时观察模式（看 az/zero/x）')
    ap.add_argument('--port', type=int, default=8766, help='--watch 时连哪个端口')
    ap.add_argument('--secs', type=float, default=4.0, help='每块板采样秒数')
    ap.add_argument('--lr', type=int, default=8766)
    ap.add_argument('--ud', type=int, default=8767)
    ap.add_argument('--guide', type=int, default=8778)
    a = ap.parse_args()

    if a.watch:
        print('=' * 58)
        print(' 实时观察 127.0.0.1:%d  （%.0f 秒）' % (a.port, a.secs))
        print('=' * 58)
        print('')
        watch(a.port, a.secs)
        return 0

    print('=' * 58)
    print(' 雷达链路自检')
    print('=' * 58)
    print('')

    # 1) 端口
    ports_ok = True
    for label, p in [('左右板', a.lr), ('上下板', a.ud), ('分配台', a.guide)]:
        ok = False
        try:
            s = socket.create_connection(('127.0.0.1', p), timeout=1.0)
            s.close()
            ok = True
        except Exception:
            ok = False
        print('  端口 %-6s %-6d %s' % (label, p, 'LISTEN ✓' if ok else '× 没在监听'))
        if label != '分配台' and not ok:
            ports_ok = False
    print('')

    if not ports_ok:
        print('先双击「启动-双雷达游戏.cmd」，等两块板都出「已监听」再看。')
        print('')
        return 2

    # 2) 收帧
    r1 = probe('左右板', a.lr, 'lr', a.secs)
    print('')
    r2 = probe('上下板', a.ud, 'ud', a.secs)
    print('')

    # 3) 结论
    print('=' * 58)
    problems = []
    if not r1.get('ok'):
        problems.append('左右板收不到帧')
    elif r1.get('fps', 0) < 4:
        problems.append('左右板帧率只有 %.1f Hz' % r1['fps'])
    if not r2.get('ok'):
        problems.append('上下板收不到帧')

    if problems:
        print('  × 不能开始采集：' + '；'.join(problems))
        print('')
        print('  排查顺序：')
        print('   1) 板子 USB 是否插好（COM3 = 左右，COM6 = 上下）')
        print('   2) 是否有别的程序占着串口（之前残留的 py 进程）')
        print('   3) 看 .deepworks\\tmp\\radar-lr.log / radar-ud.log 末尾')
        print('=' * 58)
        return 1

    print('  ✓ 两块板都在出帧，可以开始采集')
    if r1.get('xmin') is not None and abs(r1['xmax'] - r1['xmin']) < 1e-6:
        print('  · 提醒：此刻 x 恒定，说明人不在视场内。采集时务必站到雷达前。')
    print('=' * 58)
    return 0


if __name__ == '__main__':
    sys.exit(main())
