# -*- coding: utf-8 -*-
"""真雷达 → 游戏 WebSocket 桥接器（双雷达分工用）。

把矽典微雷达串口的实时点云，翻译成 Bear Run / 联调台约定的 ws JSON 帧：

    {"ts": 毫秒, "x": -1..1, "height": 0..1, "jump": 0|1, "crouch": 0|1,
     "confidence": 0..1, "raw": {...}}

分工（与 START-HERE.md 一致）：
  --role lr   只负责左右：发 x，jump/crouch/height 恒 0
  --role ud   只负责上下：发 jump/crouch/height，x 恒 0
  --role both 全字段（单雷达 / 自测）

判定来源（不新造算法）：
  左右  → radar-pointcloud/radar_actions.py 的 ActionRecognizer（项目已验收的 xbase 判据）
  上下  → 同一识别器的 JUMP/DUCK 事件，**项目里仍是"候选项"**：
          1T2R 无俯仰角，2T4R 链路 2026-10-16 起在验、结论未出。
          本脚本照原样透出，事件带 gated 标记，日志标 [未验证]。

用法：
  # 第一台：1T2R 管左右
  python radar_live_ws.py --port COM3 --role lr --ws-port 8766
  # 第二台：2T4R 管上下
  python radar_live_ws.py --port COM4 --role ud --ws-port 8767
"""
import argparse
import base64
import hashlib
import json
import math
import os
import socket
import struct
import sys
import threading
import time

# ---- 复用项目已有的雷达链路模块（radar-pointcloud）--------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
# 兼容两种布局：
#   ① <项目根>\radar-pointcloud + <项目根>\dual-radar-kit\tools\radar_live_ws.py
#   ② <分发包>\radar-pointcloud + <分发包>\tools\radar_live_ws.py
# 谁先找到就用谁，找不到也不报错（后面的 import 会给出清楚的原因）。
PIPELINE_DIR = None
for _cand in (os.path.join(HERE, os.pardir, 'radar-pointcloud'),
              os.path.join(HERE, os.pardir, os.pardir, 'radar-pointcloud')):
    _cand = os.path.abspath(_cand)
    if os.path.isdir(_cand):
        PIPELINE_DIR = _cand
        PROJECT = os.path.dirname(_cand)
        break
else:
    PROJECT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir))
if PIPELINE_DIR:
    sys.path.insert(0, PIPELINE_DIR)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from radar_source import SerialSource, build_pipeline, BOARD_PROFILES  # noqa: E402
from radar_dsp import PointCloudProcessor                              # noqa: E402
from radar_actions import (ActionRecognizer, LANE_LEFT, LANE_RIGHT,    # noqa: E402
                           JUMP, DUCK, TARGET_LOST, TARGET_BACK)

GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

# 车道电平：游戏 enterThreshold=0.36 / exitThreshold=0.22，±0.6 留足余量
LANE_X = {'LEFT': -0.6, 'CENTER': 0.0, 'RIGHT': 0.6}


# ------------------------------------------------------------------ ws 服务端
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


def ws_frame(text):
    payload = text.encode('utf-8')
    n = len(payload)
    if n < 126:
        return struct.pack('!BB', 0x81, n) + payload
    if n < 65536:
        return struct.pack('!BBH', 0x81, 126, n) + payload
    return struct.pack('!BBQ', 0x81, 127, n) + payload


class Hub(object):
    """把同一路帧广播给所有已连接的浏览器。"""

    def __init__(self):
        self.clients = set()
        self.lock = threading.Lock()

    def add(self, conn, addr):
        with self.lock:
            self.clients.add(conn)
            n = len(self.clients)
        log('  [+] %s 接入（当前 %d 个客户端）' % (addr[0], n))

    def drop(self, conn, addr):
        with self.lock:
            self.clients.discard(conn)
            n = len(self.clients)
        log('  [-] %s 断开（剩 %d 个客户端）' % (addr[0], n))

    def broadcast(self, text):
        if not self.clients:
            return
        data = ws_frame(text)
        dead = []
        with self.lock:
            targets = list(self.clients)
        for c in targets:
            try:
                c.sendall(data)
            except Exception:
                dead.append(c)
        for c in dead:
            try:
                c.close()
            except Exception:
                pass
            with self.lock:
                self.clients.discard(c)

    def count(self):
        with self.lock:
            return len(self.clients)


def drain(conn):
    """只推不收；客户端断开即可感知。"""
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


def serve_client(conn, addr, hub):
    try:
        conn.settimeout(10)
        req = read_request(conn)
        if not req or not do_handshake(conn, req):
            return
        conn.settimeout(None)
        hub.add(conn, addr)
        drain(conn)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
        hub.drop(conn, addr)


def start_server(host, port, hub):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((host, port))
    except OSError as e:
        srv.close()
        log('!! 端口 %d 绑定失败：%s（换个 --ws-port，或先关掉占用的进程）' % (port, e))
        return None
    srv.listen(8)

    def loop():
        while True:
            try:
                conn, addr = srv.accept()
            except Exception:
                return
            threading.Thread(target=serve_client, args=(conn, addr, hub),
                             daemon=True).start()

    threading.Thread(target=loop, name='ws-accept', daemon=True).start()
    return srv


# ------------------------------------------------------------------ 主逻辑
def log(msg):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class ZeroCal(object):
    """滚动零点：最近 `secs` 秒的角度中位数 = "中道"。

    这样定义中道的好处：你站在哪，站够 `min_secs` 秒，那就算中道；
    走开一步的那 1–2 秒只占窗口一小部分，中位数不动 → 偏移立刻被看到。
    比"启动瞬间锁一次"稳，因为你启动时可能在动。
    """

    def __init__(self, secs, min_secs=6.0, max_std=4.5, short_secs=3.0, adapt=False):
        self.secs = secs
        self.min_secs = min_secs
        self.max_std = max_std
        self.short_secs = short_secs
        self.adapt = adapt
        self.buf = []
        self.zero = None

    def push(self, az, now):
        # 默认锁定后不再更新：零点跟着人跑会"吃掉"移动量，行为不可预测。
        if self.zero is not None and not self.adapt:
            return self.zero
        self.buf.append((now, az))
        cut = now - self.secs
        while self.buf and self.buf[0][0] < cut:
            self.buf.pop(0)
        if len(self.buf) < 6:
            return self.zero
        # 判定用"最近 short_secs 秒是否站稳"：快，人一停下来就能判
        short = sorted(v for t, v in self.buf if t >= now - self.short_secs)
        if len(short) < 5:
            return self.zero
        n = len(short)
        mean = sum(short) / n
        std = (sum((v - mean) ** 2 for v in short) / n) ** 0.5
        # 值仍用长窗中位数：站不到长窗一半，零点就不会跟过去，
        # 避免"走一步 → 零点跟过去 → x 回 0 → 角色又弹回中间"。
        if self.zero is None or std <= self.max_std:
            vals = sorted(v for _, v in self.buf)
            self.zero = vals[len(vals) // 2]
        return self.zero


def main():
    ap = argparse.ArgumentParser(description='真雷达 → ws JSON 桥接器（单路）')
    ap.add_argument('--port', default='auto', help='串口，如 COM3；auto=自动识别')
    ap.add_argument('--role', default='lr', choices=['lr', 'ud', 'both'],
                    help='lr=管左右，ud=管上下，both=全字段')
    ap.add_argument('--ws-port', type=int, default=8766, help='推流端口，默认 8766')
    ap.add_argument('--host', default='127.0.0.1', help='监听地址，局域网用 0.0.0.0')
    ap.add_argument('--calib', default=None,
                    help='calib.json；默认用 radar-pointcloud/calib.json（仅 1T2R 有效）')
    ap.add_argument('--band', default=None,
                    help='距离带 r1,r2（米）；不给则用 calib / 板型默认 / auto')
    ap.add_argument('--cfar', type=float, default=2.6)
    ap.add_argument('--clutter-keep', type=float, default=1.0,
                    help='MTI 静态抑制系数：1.0=完全去静止(默认)；0.3=保留70%%静止回波，'
                         '站着不动也能被看到（噪声会变多）')
    ap.add_argument('--min-snr', type=float, default=7.0)
    ap.add_argument('--x-amp', type=float, default=0.6, help='车道电平幅值')
    ap.add_argument('--mode', default='pos', choices=['pos', 'event'],
                    help='pos=瞬时角度出位置电平(低延迟,默认)；event=识别器离散事件(~2s)')
    ap.add_argument('--scale-deg', type=float, default=20.0,
                    help='位置电平满幅对应的角度(度)：20° 时约 7.2° 触发换道')
    ap.add_argument('--zero-secs', type=float, default=3.0,
                    help='启动后多少秒的角度中位数作为中道零点')
    ap.add_argument('--hold-secs', type=float, default=1.5,
                    help='丢帧后保持最近有效位置的时长（秒）')
    ap.add_argument('--jump-deg', type=float, default=12.0,
                    help='角度跳变超过该值(度)判为换了反射体，需连续3帧确认')
    ap.add_argument('--zero-adapt', action='store_true',
                    help='允许零点在站稳后继续跟随（默认锁定后固定）')
    ap.add_argument('--vertical', action='store_true',
                    help='这块板竖放（测角轴=垂直）：height 用同一角度给出 0..1')
    ap.add_argument('--quiet', action='store_true', help='不打印逐帧日志')
    ap.add_argument('--log', default=None,
                    help='把全部输出追加写进该文件（用 pythonw 后台跑时必填）')
    args = ap.parse_args()

    if args.log:
        # pythonw 下 sys.stdout 是 None，先挂文件再打印
        fp = open(args.log, 'a', encoding='utf-8', buffering=1)
        sys.stdout = fp
        sys.stderr = fp

    hub = Hub()
    if start_server(args.host, args.ws_port, hub) is None:
        return 2
    log('ws://%s:%d 已监听（role=%s）' % (args.host, args.ws_port, args.role))

    # ---- 数据源：SerialSource 自动按 USB 指纹选板型与波特率 -----------------
    src = SerialSource(args.port, baud=None, retry_delay=1.0)
    proc = PointCloudProcessor(cfar_scale=args.cfar, min_snr=args.min_snr,
                               remove_clutter=True, clutter_keep=args.clutter_keep)

    # ---- 动作识别器 --------------------------------------------------------
    band = None
    if args.band:
        lo, hi = [float(v) for v in args.band.split(',')]
        band = (lo, hi)
    calib = args.calib
    if calib is None:
        cand = os.path.join(PIPELINE_DIR, 'calib.json')
        calib = cand if os.path.isfile(cand) else None
    rec_kw = {}
    if band is not None:
        rec_kw['band'] = band
    elif args.calib is None and calib:
        # 默认 calib.json 是 1T2R 实测（band 0.681~1.363 m）；
        # 2T4R 距离分辨率不同（0.15 m），不能套用同一距离带 → 走 auto
        rec_kw['band'] = 'auto'
    rec = ActionRecognizer(calib=calib, **rec_kw)
    log('识别器：band=%s calib=%s' % (rec.band if hasattr(rec, 'band') else 'auto',
                                    os.path.basename(calib) if calib else '无'))

    state = {'lane': 'CENTER', 'jump': 0.0, 'crouch': 0.0, 'n': 0}
    zcal = ZeroCal(args.zero_secs, adapt=args.zero_adapt)

    def emit(rec_out, ts_ms, board, py_val, n_pts, extra, az_hint=None):
        """把一帧识别结果转成 ws 帧并按 role 裁剪。

        mode=pos ：用**瞬时角度**直接出位置电平，游戏端唯一的迟滞判定层生效，
                   端到端延迟 ≈ 1 帧（130 ms）。
                   （识别器的 xbase 判据自带 1.8 s 中位延迟，只适合离线判动作，
                    不适合当实时输入 —— 见 radar_actions.py L269-L270。）
        mode=event：保留识别器的离散 lane/事件，延迟 ~2 s，用于对照。
        """
        lane = rec_out.get('lane') or 'CENTER'
        feat = rec_out.get('features') or {}
        px = float(feat.get('px') or 0.0)
        py = float(feat.get('py') or 0.0)
        npts = feat.get('n') or 0
        # 无目标帧的 px/py 是无意义的 0，不能拿去当"角度 0"用
        # 角度来源优先级：跟踪器从点云直接选出的目标（az_hint）> 识别器的加权平均。
        # 识别器的 px/py 是"所有点按能量加权平均"，墙面/门框会把人拉偏，
        # 所以有跟踪结果时一律以跟踪值为准。
        is_target = az_hint is not None or (bool(rec_out.get('target')) and npts >= 2 and py > 0.3)
        if az_hint is not None:
            az = az_hint
        else:
            az = math.degrees(math.atan2(px, py)) if is_target else None

        if az is not None:
            if az_hint is not None:
                state['az_ema'] = az          # 跟踪器已平滑，不再叠一层 EMA
            else:
                # 轻平滑：时间常数 ~2 帧(0.26 s)，压住单帧角度抖动又不起明显延迟
                prev = state.get('az_ema')
                state['az_ema'] = az if prev is None else (0.55 * prev + 0.45 * az)
            zero = zcal.push(state['az_ema'], time.time())
            state['last_t'] = time.time()
        else:
            zero = zcal.zero
        az_use = state.get('az_ema') if az is not None else None

        if args.mode == 'pos' and zero is not None and az_use is not None:
            pos = clamp((az_use - zero) / args.scale_deg, -1.0, 1.0)
            state['last_pos'] = pos
        elif args.mode == 'pos':
            pos = state.get('last_pos', 0.0)   # 目标丢失：保持最后位置，不发假 0
        else:
            pos = LANE_X.get(lane, 0.0) * (args.x_amp / 0.6)

        # 稀疏传感器：短时丢帧用"最近有效位置"续帧，游戏端才能连续判定；
        # 超过 --hold-secs 才降为低置信（角色就地保持，不乱动）。
        age = time.time() - state.get('last_t', 0.0)
        fresh = is_target
        stale_ok = (not fresh) and age <= args.hold_secs and state.get('last_pos') is not None

        jump = state['jump']
        crouch = state['crouch']
        # 竖放时 px 不再是"横向偏移"而是"高度方向偏移"，所以 height 用同一个 pos；
        # 横放时这块板测不出高度 → 只把原始角度透出来，不伪造 height。
        height = clamp(0.5 + 0.5 * pos, 0.0, 1.0) if args.vertical else 0.0

        if args.role == 'lr':
            jj, cc = 0.0, 0.0
        elif args.role == 'ud':
            pos = 0.0
            jj, cc = jump, crouch
        else:
            jj, cc = jump, crouch

        conf = 0.9 if fresh else (0.75 if stale_ok else 0.2)
        if args.mode == 'pos' and zero is None:
            conf = min(conf, 0.5)      # 零点未锁定：低置信，让游戏忽略
        frame = {
            'ts': ts_ms,
            'x': round(pos, 4),
            'height': round(height, 4),
            'jump': jj,
            'crouch': cc,
            'confidence': conf,
            'raw': {
                'src': 'radar', 'board': board, 'role': args.role,
                'lane': lane, 'n': npts, 'py': round(py_val, 3),
                'az': (round(az_use, 2) if az_use is not None else None),
                'zero': (round(zero, 2) if zero is not None else None),
                'features': feat,
            },
        }
        frame['raw'].update(extra)
        hub.broadcast(json.dumps(frame, ensure_ascii=False))
        return frame

    trk = {'ok': False, 'r': 1.0, 'deg': 0.0, 'miss': 0}

    def track_az(pts):
        """从点云里挑出"跟得住"的人体目标，返回它平滑后的角度。

        每帧点云按能量降序，直接取最强点会在人体/墙面之间来回跳，
        所以用「能量 / 距离偏离」打分做最近邻关联；
        跳变超过 --jump-deg 时先按住不动，连续 3 帧都指向同处才认新目标。
        """
        cand = [q for q in pts if 0.5 <= q['r'] <= 2.2]
        if not cand:
            trk['miss'] += 1
            return None
        if not trk['ok']:
            q = max(cand, key=lambda q: q['p'])
            trk.update(ok=True, r=q['r'], deg=q['deg'], miss=0)
            return trk['deg']
        q = max(cand, key=lambda q: q['p'] / (
            1.0 + 3.0 * (abs(q['r'] - trk['r']) + 0.02 * abs(q['deg'] - trk['deg']))))
        jumped = abs(q['deg'] - trk['deg']) > args.jump_deg and abs(q['r'] - trk['r']) > 0.3
        if jumped:
            trk['miss'] += 1
            if trk['miss'] < 3:
                return trk['deg']          # 疑似跳到别的反射体：先按住，别乱换道
            trk.update(r=q['r'], deg=q['deg'], miss=0)
            return trk['deg']
        trk.update(r=0.65 * trk['r'] + 0.35 * q['r'],
                   deg=0.65 * trk['deg'] + 0.35 * q['deg'], miss=0)
        return trk['deg']

    def on_frame(z0, z1, stats):
        pts, _noise = proc.process(z0, z1)
        out = rec.push(pts, t=time.time())
        az_hint = track_az(pts)
        state['n'] += 1

        # 事件 → 一次性脉冲（游戏只看上升沿）
        state['jump'] = 0.0
        state['crouch'] = 0.0
        fired = []
        for e in out.get('events') or []:
            name = e.get('name')
            if name in (JUMP, DUCK):
                state['jump' if name == JUMP else 'crouch'] = 1.0
                fired.append('%s%s' % (name, '[未验证]' if e.get('gated') else ''))
            elif name in (LANE_LEFT, LANE_RIGHT):
                fired.append(name)
            elif name in (TARGET_LOST, TARGET_BACK):
                fired.append(name)

        feat = out.get('features') or {}
        board = src.board or '?'
        f = emit(out, int(time.time() * 1000), board, float(feat.get('py') or 0.0),
                 int(feat.get('n') or 0), {'events': fired}, az_hint=az_hint)

        if not args.quiet:
            fps = stats.get('fps')
            fps = float(fps) if fps is not None else 0.0
            if fired or state['n'] % 10 == 1:
                log('帧%d fps=%.1f az=%s zero=%s x=%.2f h=%.2f r=%.2f n=%s c=%.2f 客户端=%d %s'
                    % (state['n'], fps, f['raw'].get('az'), f['raw'].get('zero'),
                       f['x'], f['height'], float(feat.get('py') or 0.0),
                       feat.get('n'), f['confidence'], hub.count(),
                       ('事件:' + ','.join(fired)) if fired else ''))

    stop = threading.Event()
    try:
        build_pipeline(src, on_frame, stop)
    except KeyboardInterrupt:
        stop.set()
    log('退出。共处理 %d 帧' % state['n'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
