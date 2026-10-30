#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动推流：自动认出两块雷达 → 各起一路 WebSocket → 自检 → 打印地址。

它和你电脑上原来那个 start_dual_radar.cmd 的区别只有一点：
**不写死 COM3 / COM4**。同学的电脑上雷达可能是 COM5、COM9，
端口号一变，写死的脚本就永远打不开；这里每次都现场枚举、按 USB
指纹认板，再把真实端口号传给推流进程。

做三件事：
  1. 复用 radar_autoconnect 的端口发现（注册表 ∪ pyserial）和字节流判定，
     找出「真的在出帧」的板，跳过被占用的口并说明原因；
  2. 按板型分工：1T2R → 左右（lr，8766），2T4R → 上下（ud，8767）；
     只有一块时：1T2R 出左右，2T4R 出全字段（both）；
  3. 起 tools/radar_live_ws.py 子进程，等几秒后连上去数帧，
     确认「浏览器/游戏真的能收到数据」才算成功。

只读串口，不写任何寄存器、不发任何雷达命令。

用法：
  python radar_serve.py                 # 自动识别 + 启动 + 自检
  python radar_serve.py --dry-run       # 只看它打算怎么分配，不启动
  python radar_serve.py --stop          # 停掉上次由本工具启动的进程
  python radar_serve.py --port-lr COM7 --port-ud COM9
"""
import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import radar_autoconnect as ac  # noqa: E402

GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
LOGDIR = os.path.join(HERE, 'logs')
PIDFILE = os.path.join(LOGDIR, 'serve-pids.json')
LIVE = os.path.join(HERE, 'tools', 'radar_live_ws.py')

ROLE_DEFAULT_PORT = {'lr': 8766, 'ud': 8767}
ROLE_TITLE = {
    'lr': '左右（x → 换道）',
    'ud': '上下（jump / crouch）',
    'both': '全字段（左右 + 上下）',
}


# --------------------------------------------------------------------------
# 计划
# --------------------------------------------------------------------------
def plan_roles(items):
    """把「在出帧的板」分配成 (lr, ud)。items 是 list[dict]。

    优先级：板型对上就按板型（1T2R→左右，2T4R→上下）；
    剩下的（同型号第二块 / 认不出板型）按端口号顺序补空位。
    """
    lr = ud = None
    for it in items:
        if it['board'] == '1t2r' and lr is None:
            lr = it
            continue
        if it['board'] == '2t4r' and ud is None:
            ud = it
    for it in items:
        if it is lr or it is ud:
            continue
        if lr is None:
            lr = it
        elif ud is None:
            ud = it
    return lr, ud


def build_plan(items, args):
    """→ [{port, board, baud, role, ws_port}]"""
    lr, ud = plan_roles(items)

    # 手动指定优先
    if args.port_lr:
        lr = next((i for i in items if i['port'].upper() == args.port_lr.upper()),
                  {'port': args.port_lr, 'board': args.board_lr, 'baud': None})
    if args.port_ud:
        ud = next((i for i in items if i['port'].upper() == args.port_ud.upper()),
                  {'port': args.port_ud, 'board': args.board_ud, 'baud': None})

    plan = []
    if lr is not None and ud is not None:
        plan.append({'port': lr['port'], 'board': lr['board'],
                     'baud': lr.get('baud'), 'role': 'lr',
                     'ws_port': args.ws_port_lr})
        plan.append({'port': ud['port'], 'board': ud['board'],
                     'baud': ud.get('baud'), 'role': 'ud',
                     'ws_port': args.ws_port_ud})
    elif lr is not None:
        role = 'lr' if lr['board'] == '1t2r' else 'both'
        plan.append({'port': lr['port'], 'board': lr['board'],
                     'baud': lr.get('baud'), 'role': role,
                     'ws_port': args.ws_port_lr})
    elif ud is not None:
        plan.append({'port': ud['port'], 'board': ud['board'],
                     'baud': ud.get('baud'), 'role': 'both',
                     'ws_port': args.ws_port_lr})
    return plan


# --------------------------------------------------------------------------
# 端口/进程工具
# --------------------------------------------------------------------------
def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def load_pids():
    try:
        with open(PIDFILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _is_python_pid(pid):
    """停止前校验：这个 PID 现在还是不是 python 进程。

    只写 PID 有个隐患：进程正常退出后，Windows 会把同一个 PID 分给别的程序，
    此时 taskkill 就误杀了。所以先按映像名确认一次，不像 python 就跳过。
    """
    try:
        r = subprocess.run(['tasklist', '/FI', 'PID eq %s' % pid,
                            '/FO', 'CSV', '/NH'],
                           capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=15)
    except Exception:
        return True          # 查不了就不阻断，交给 taskkill 的结果
    out = (r.stdout or '').lower()
    # 只认 ASCII 的 python：tasklist 在本机不存在该 PID 时返回的是中文提示
    # （按 GBK 输出，这里可能显示为乱码），用 'python' 判断不受影响。
    return 'python' in out


def stop_previous():
    info = load_pids()
    if not info or not info.get('procs'):
        ac.say(ac.INFO, '没有找到本工具上次启动的记录（%s）。' % PIDFILE)
        print('        如果是手动开的窗口，直接关掉那个窗口即可。')
        return 0
    rc = 0
    for p in info['procs']:
        pid = p.get('pid')
        if not _is_python_pid(pid):
            print('  跳过 PID %-6s：该 PID 现在不是 python 进程（已退出，或被系统复用）。'
                  % pid)
            continue
        print('  停止 PID %-6s %-8s %s' % (pid, p.get('port'), p.get('role')))
        try:
            r = subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                               capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=20)
            if r.returncode != 0:
                print('        （可能已经退出）%s' % (r.stdout or r.stderr).strip()[:80])
        except Exception as e:
            print('        停止失败：%s' % e)
            rc = 1
    try:
        os.remove(PIDFILE)
    except Exception:
        pass
    ac.say(ac.OK, '已清理。')
    return rc


# --------------------------------------------------------------------------
# WebSocket 自检（标准库，和 tools/check_radar.py 同一套解析）
# --------------------------------------------------------------------------
def recv_exact(sock, buf, n):
    while len(buf) < n:
        chunk = sock.recv(65536)
        if not chunk:
            return None, buf
        buf += chunk
    return buf[:n], buf[n:]


def read_ws_frame(sock, buf):
    head, buf = recv_exact(sock, buf, 2)
    if head is None:
        return None, None, buf
    opcode = head[0] & 0x0F
    masked = head[1] & 0x80
    length = head[1] & 0x7F
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


def probe_ws(port, seconds=5.0, want=6):
    """连上本地 ws 数帧 → (frames, seconds, err)。"""
    try:
        sk = socket.create_connection(('127.0.0.1', port), timeout=3)
    except OSError as e:
        return 0, 0.0, 'TCP 连不上 127.0.0.1:%d（%s）' % (port, e)

    key = base64.b64encode(os.urandom(16)).decode()
    req = ('GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n'
           'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
           'Sec-WebSocket-Version: 13\r\n\r\n' % (port, key))
    try:
        sk.sendall(req.encode('latin-1'))
        sk.settimeout(3)
        head = b''
        while b'\r\n\r\n' not in head and len(head) < 65536:
            chunk = sk.recv(4096)
            if not chunk:
                break
            head += chunk
    except OSError as e:
        try:
            sk.close()
        except Exception:
            pass
        return 0, 0.0, '握手失败：%s' % e

    status = head.decode('latin-1', 'replace').split('\r\n', 1)[0]
    if ' 101' not in status:
        try:
            sk.close()
        except Exception:
            pass
        return 0, 0.0, '对方不是 WebSocket 服务：%s' % status.strip()

    buf = head.split(b'\r\n\r\n', 1)[1]
    got = 0
    t0 = time.time()
    sk.settimeout(2)
    while got < want and time.time() - t0 < seconds:
        try:
            opcode, payload, buf = read_ws_frame(sk, buf)
        except socket.timeout:
            break
        except OSError as e:
            return got, time.time() - t0, '读帧出错：%s' % e
        if opcode is None:
            break
        if opcode == 0x1:
            got += 1
    dt = time.time() - t0
    try:
        sk.close()
    except Exception:
        pass
    return got, dt, None


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------
def find_streaming(verbose=True):
    """枚举 + 实测，返回 (streaming, problems)。"""
    ports = ac.discover()
    if verbose:
        print('  发现 %d 个串口，逐个试开（只读，不发命令）...' % len(ports))
    streaming, problems = [], []
    for info in ports:
        port = info['device']
        hint = ac.guess_board(info)[0]
        if verbose:
            print('    %-8s (%s) ...' % (port, hint or '板型待定'), end='', flush=True)
        r = ac.check_one(port, board_hint=hint, seconds=2.0, verbose=False)
        if r['status'] == 'streaming':
            streaming.append({'port': port, 'board': r['board'],
                              'baud': r['baud'], 'fps': r['fps']})
            if verbose:
                print(' 出帧 %.2f 帧/s' % r['fps'])
        else:
            tag, txt = ac.STATUS_TEXT.get(r['status'], (ac.INFO, r['status']))
            problems.append({'port': port, 'status': r['status'],
                             'text': txt, 'error': r.get('error')})
            if verbose:
                print(' %s %s' % (tag, txt))
    return streaming, problems


def cmd_serve(args):
    ac.head('自动推流（识别两块雷达 → 起 WebSocket → 自检）')

    try:
        import serial  # noqa: F401
    except Exception:
        ac.say(ac.BAD, '没有安装 pyserial，先运行 1-安装环境.cmd')
        return 2
    if not os.path.isfile(LIVE):
        ac.say(ac.BAD, '缺少 tools/radar_live_ws.py —— 压缩包没解压完整，请整包重新解压。')
        return 2

    streaming, problems = find_streaming()

    if not streaming:
        ac.say(ac.BAD, '没有一块板在出帧，无法推流。')
        for p in problems:
            print('        %-8s %s %s' % (p['port'], p['text'],
                                          ('（%s）' % p['error']) if p['error'] else ''))
        print('')
        print('        先跑 2-检查雷达.cmd 看具体卡在哪一环；')
        print('        被占用的话，关闭 ONELAB / 点云服务 / 之前开的推流窗口再试。')
        return 1

    # 被占用但板型已知的口：多半是已经在推流了
    for p in problems:
        if p['status'] == 'busy':
            ac.say(ac.WARN, '%s 被别的程序占用，本次跳过。'
                            '如果是上次开的推流，先运行 --stop 或关掉那个窗口。' % p['port'])

    plan = build_plan(streaming, args)

    ac.head('分配方案')
    for p in plan:
        prof = ac.BOARD_PROFILES.get(p['board'])
        print('  串口 %-8s %-28s → %-6s ws://127.0.0.1:%d'
              % (p['port'], prof['label'] if prof else '未知板型',
                 p['role'], p['ws_port']))
        print('            负责：%s' % ROLE_TITLE.get(p['role'], p['role']))
        if p['role'] in ('ud', 'both'):
            print('            注意：jump / crouch 仍是项目里的「候选项」，'
                  '只有 2T4R 俯仰角链路验证通过才算可靠。')
    if len(plan) == 2 and plan[0]['board'] == plan[1]['board']:
        ac.say(ac.WARN, '两块板是同一型号，容易互相干扰；建议错开摆位、别正对。')
    if len(plan) == 2 and plan[0]['ws_port'] == plan[1]['ws_port']:
        ac.say(ac.BAD, '两路推流被分到了同一个端口 %d。' % plan[0]['ws_port'])
        print('        用 --ws-port-lr / --ws-port-ud 指定不同的端口（默认 8766 / 8767）。')
        return 2

    if args.dry_run:
        print('')
        ac.say(ac.INFO, '--dry-run：只打印方案，没有启动任何进程。')
        return 0

    # 端口占用预检：上一次的推流没关掉时，这里能提前说清楚
    for p in plan:
        if not port_free(p['ws_port']):
            ac.say(ac.BAD, '本地 %d 端口已被占用（上次的推流还在跑？）。' % p['ws_port'])
            print('        先运行：python radar_serve.py --stop')
            print('        或用 netstat -ano ^| findstr :%d 找到 PID 后关掉。' % p['ws_port'])
            return 2

    os.makedirs(LOGDIR, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    procs = []
    ac.head('启动推流')
    for p in plan:
        log = os.path.join(LOGDIR, 'radar-%s-%s.log' % (p['role'], stamp))
        cmd = [sys.executable, LIVE,
               '--port', p['port'],
               '--role', p['role'],
               '--ws-port', str(p['ws_port']),
               '--log', log]
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, **kwargs)
        except Exception as e:
            ac.say(ac.BAD, '%s 启动失败：%s' % (p['port'], e))
            continue
        procs.append({'pid': proc.pid, 'port': p['port'], 'role': p['role'],
                      'ws_port': p['ws_port'], 'log': log, 'board': p['board']})
        print('  PID %-6d %-8s %-6s → ws://127.0.0.1:%d  日志 %s'
              % (proc.pid, p['port'], p['role'], p['ws_port'],
                 os.path.basename(log)))

    if not procs:
        ac.say(ac.BAD, '一个进程都没起来。把上面的报错截图发回来。')
        return 1

    with open(PIDFILE, 'w', encoding='utf-8') as f:
        json.dump({'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                   'procs': procs}, f, ensure_ascii=False, indent=2)

    # 等待 + 自检
    wait = max(3.0, float(args.wait))
    print('')
    print('  等 %.0f 秒让雷达出第一帧...' % wait)
    time.sleep(wait)

    ac.head('自检：浏览器/游戏能不能收到数据')
    good = []
    for p in procs:
        try:
            # 进程死掉时 probe 会在 TCP 层就失败，这里只做展示
            frames, secs, err = probe_ws(p['ws_port'], seconds=5.0)
        except Exception as e:
            frames, secs, err = 0, 0.0, str(e)
        if frames > 0 and not err:
            hz = frames / secs if secs > 0 else 0.0
            ac.say(ac.OK, '%-8s %-6s ws://127.0.0.1:%-5d 收到 %d 帧（%.1f 帧/s）'
                  % (p['port'], p['role'], p['ws_port'], frames, hz))
            good.append(p)
        else:
            ac.say(ac.BAD, '%-8s %-6s ws://127.0.0.1:%-5d 没收到数据：%s'
                  % (p['port'], p['role'], p['ws_port'], err or '0 帧'))
            print('           看日志：%s' % p['log'])

    print('')
    if good:
        ac.head('可以直接用的地址')
        for p in good:
            print('  %-6s ws://127.0.0.1:%d' % (p['role'], p['ws_port']))
        if len(good) >= 2:
            q = '?input=radar&radar=ws://127.0.0.1:%d&radar2=ws://127.0.0.1:%d' % (
                good[0]['ws_port'], good[1]['ws_port'])
        else:
            q = '?input=radar&radar=ws://127.0.0.1:%d' % good[0]['ws_port']
        print('')
        print('  游戏地址后面接：%s' % q)
        print('  （把这段拼到你本机的 index.html 后面，或用联调台手动填上面的地址）')
        print('')
        print('  停止推流：python radar_serve.py --stop')
    else:
        ac.say(ac.BAD, '推流进程起来了，但自检没收到帧。')
        print('        1) 看 logs 目录里最新日志的 [serial] 行，确认串口是打开还是报错')
        print('        2) 端口 8766/8767 被防火墙拦了？本机访问一般不会')
        print('        3) 把日志最后 20 行发回来')

    return 0 if good else 1


def cmd_stop(args):
    ac.head('停止本工具启动的推流')
    return stop_previous()


def main(argv=None):
    ac._init_stdout()
    # 允许 `radar_serve.py stop` 这种子命令写法
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'stop':
        return cmd_stop(None)
    ap = argparse.ArgumentParser(prog='radar_serve', description='自动识别两块雷达并启动 WebSocket 推流')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--wait', type=float, default=6.0)
    ap.add_argument('--ws-port-lr', type=int, default=ROLE_DEFAULT_PORT['lr'])
    ap.add_argument('--ws-port-ud', type=int, default=ROLE_DEFAULT_PORT['ud'])
    ap.add_argument('--port-lr', default=None)
    ap.add_argument('--port-ud', default=None)
    ap.add_argument('--board-lr', default=None, choices=['1t2r', '2t4r'])
    ap.add_argument('--board-ud', default=None, choices=['1t2r', '2t4r'])
    args = ap.parse_args(argv)
    args.func = cmd_serve
    return cmd_serve(args)


if __name__ == '__main__':
    sys.exit(main())
