# -*- coding: utf-8 -*-
"""雷达桥接器：把真实雷达的原始输出，转成游戏能直接连的 WebSocket JSON 帧。

为什么需要它：
    游戏的输入协议是【WebSocket + 每帧一个 JSON】：
        {"ts":毫秒, "x":-1~1 左负右正, "height":0~1, "jump":0/1, "crouch":0/1, "confidence":0~1}
    而真实雷达通常不是这个协议 —— 它可能是串口、UDP、TCP、HTTP 接口、厂商 SDK 或上位机。
    本脚本负责把「雷达的输出」翻译成上面的格式，并以 ws://0.0.0.0:8766 推给游戏/联调台。

组员的用法（三步）：
    1. 先看雷达到底往外吐什么：加 --print-sample 跑一次，屏幕上会打印"收到的原文 + 解析结果"
    2. 按它的格式选上游(--from)与解析(--parse)，必要时用 --map/--scale-x 换算字段
    3. 游戏或联调台连 ws://127.0.0.1:8766（跨机器时用本机 IP）

支持的雷达输出形式（--from）：
    udp          雷达往本机某个 UDP 端口发包（最常见）
    tcp          雷达开了 TCP 服务，我们连过去按行收
    tcp-listen   雷达主动连到本机某个端口推数据
    http         雷达提供 HTTP 接口，定时轮询
    exec         调用厂商 SDK / 上位机命令行，读它的标准输出（万能兜底）
    stdin        从标准输入读（手工调试用）

支持的报文解析（--parse）：
    json   JSON 对象（也支持数组、{"data":{...}} 之类的外层包装）
    kv     "x=0.31 jump=1 crouch=0" 这种键值对
    csv    "0.31,0.0,1,0" 分隔符列，用 --fields 指定列名顺序
    regex  用 --regex 写命名组抓字段，例如 --regex "x=(?P<x>-?\\d+\\.?\\d*)"
    raw    纯数字按 --fields 顺序对应

常用参数速查：
    --map x=px,jump=j,crouch=duck    上游字段名 → 游戏字段名
    --scale-x 0.002 --offset-x 0     像素/毫米 → 归一化(-1~1) 的换算
    --invert-x                       左右反了时加这个
    --jump-th 0.5 --crouch-th 0.5    上游给连续值时，超过阈值算 1
    --rate 20                        输出节奏（游戏按 20Hz 设计）
    --print-sample                   打印原始报文与解析结果，第一次接必用

示例：
    # 雷达往 8888 发 JSON，字段名就是 x/jump/crouch
    python radar_bridge.py --from udp --udp-port 8888 --parse json

    # 雷达吐 "x=0.31 jump=1"，端口 9000
    python radar_bridge.py --from udp --udp-port 9000 --parse kv

    # 雷达 HTTP 接口，像素坐标 px、蹲下标志 duck，2cm 一个单位
    python radar_bridge.py --from http --http http://192.168.1.9/api/target --interval 0.05 \
        --map x=px,crouch=duck --scale-x 0.02 --crouch-th 0.5

    # 只有厂商 SDK 命令行，它每行打印一条 JSON
    python radar_bridge.py --from exec --exec "python vendor_sdk.py --stream" --parse json
"""
import argparse
import base64
import hashlib
import json
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
GAME_FIELDS = ('x', 'height', 'jump', 'crouch', 'confidence', 'ts')
NUM_RE = re.compile(r'([A-Za-z_][\w.\-]*)\s*[=:]\s*(-?\d+(?:\.\d+)?|[tT]rue|[fF]alse)')
NUMFULL_RE = re.compile(r'-?\d+(?:\.\d+)?')


def log(msg):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


# ---------------------------------------------------------------- 输出侧：WebSocket 服务

class Hub:
    """维护连上来的游戏/联调台客户端，把帧广播给它们。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []
        self.sent = 0

    def add(self, conn, addr):
        with self.lock:
            self.clients.append((conn, addr))
            n = len(self.clients)
        log('客户端接入 %s（当前 %d 个）' % (addr[0], n))

    def drop(self, conn, addr):
        with self.lock:
            self.clients = [c for c in self.clients if c[0] is not conn]
            n = len(self.clients)
        log('客户端断开 %s（当前 %d 个）' % (addr[0], n))

    def broadcast(self, text):
        payload = text.encode('utf-8')
        n = len(payload)
        if n < 126:
            head = struct.pack('!BB', 0x81, n)
        elif n < 65536:
            head = struct.pack('!BBH', 0x81, 126, n)
        else:
            head = struct.pack('!BBQ', 0x81, 127, n)
        dead = []
        with self.lock:
            clients = list(self.clients)
        for conn, addr in clients:
            try:
                conn.sendall(head + payload)
                self.sent += 1
            except Exception:
                dead.append((conn, addr))
        for conn, addr in dead:
            try:
                conn.close()
            except Exception:
                pass
            self.drop(conn, addr)


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
    conn.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                  'Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n' % accept).encode('latin-1'))
    return True


def serve_client(conn, addr, hub):
    try:
        conn.settimeout(10)
        req = read_request(conn)
        if not req or not do_handshake(conn, req):
            return
        conn.settimeout(None)
        hub.add(conn, addr)
        while True:                                  # 只读不处理，读到断开为止
            if not conn.recv(4096):
                break
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
        hub.drop(conn, addr)


def ws_server(host, port, hub):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((host, port))
    except OSError as e:
        log('输出端口 %s:%d 绑定失败：%s' % (host, port, e))
        log('端口被占用？换一个：--port 8767（游戏里也要填对应的端口）')
        raise SystemExit(1)
    srv.listen(8)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=serve_client, args=(conn, addr, hub), daemon=True).start()


# ---------------------------------------------------------------- 上游：读原始报文

def make_source(cfg, warn):
    kind = cfg['from_']

    if kind == 'udp':
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((cfg['udp_host'], cfg['udp_port']))
        log('等待雷达往 UDP %s:%d 发包' % (cfg['udp_host'], cfg['udp_port']))

        def gen():
            while True:
                data, addr = sock.recvfrom(65536)
                for line in data.decode('utf-8', 'replace').splitlines():
                    line = line.strip()
                    if line:
                        yield line, addr[0]
        return gen()

    if kind == 'tcp':
        host, _, port = cfg['tcp'].rpartition(':')
        port = int(port)
        log('连接雷达 TCP %s:%d' % (host, port))

        def gen():
            while True:
                try:
                    s = socket.create_connection((host, port), timeout=5)
                    f = s.makefile('rb')
                    for raw in f:
                        line = raw.decode('utf-8', 'replace').strip()
                        if line:
                            yield line, host
                    f.close()
                    s.close()
                    warn('雷达 TCP 断开，5 秒后重连')
                except OSError as e:
                    warn('连不上雷达 TCP %s:%d（%s），5 秒后重试' % (host, port, e))
                time.sleep(5)
        return gen()

    if kind == 'tcp-listen':
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((cfg['host'], cfg['listen_port']))
        srv.listen(4)
        log('等待雷达连到本机 TCP %s:%d 推数据' % (cfg['host'], cfg['listen_port']))

        def gen():
            while True:
                conn, addr = srv.accept()
                log('雷达已接入 %s' % addr[0])
                f = conn.makefile('rb')
                for raw in f:
                    line = raw.decode('utf-8', 'replace').strip()
                    if line:
                        yield line, addr[0]
                f.close()
                conn.close()
                warn('雷达断开，等待重连')
        return gen()

    if kind == 'http':
        log('轮询雷达 HTTP %s（间隔 %.3fs）' % (cfg['http'], cfg['interval']))

        def gen():
            while True:
                try:
                    with urllib.request.urlopen(cfg['http'], timeout=cfg['http_timeout']) as r:
                        body = r.read().decode('utf-8', 'replace')
                    yield body, cfg['http']
                except Exception as e:
                    warn('HTTP 请求失败：%s' % e)
                time.sleep(cfg['interval'])
        return gen()

    if kind == 'exec':
        log('运行外部命令读它的输出：%s' % cfg['exec_cmd'])

        def gen():
            while True:
                try:
                    p = subprocess.Popen(cfg['exec_cmd'], shell=True, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True,
                                         encoding='utf-8', errors='replace')
                    for line in p.stdout:
                        line = line.strip()
                        if line:
                            yield line, 'exec'
                    warn('外部命令结束（退出码 %s），3 秒后重启' % p.wait())
                except Exception as e:
                    warn('外部命令启动失败：%s' % e)
                time.sleep(3)
        return gen()

    # stdin
    log('从标准输入读报文（每行一条）')

    def gen():
        for raw in sys.stdin:
            line = raw.strip()
            if line:
                yield line, 'stdin'
    return gen()


# ---------------------------------------------------------------- 解析：报文 → 字段字典

def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def parse_sample(text, cfg):
    kind = cfg['parse']

    if kind == 'json':
        obj = json.loads(text)
        for _ in range(2):
            if isinstance(obj, list):
                if not obj:
                    return None
                obj = obj[min(cfg['array_index'], len(obj) - 1)]
            elif isinstance(obj, dict):
                for key in ('data', 'result', 'payload', 'target', 'person', 'track', 'points'):
                    inner = obj.get(key)
                    if isinstance(inner, (dict, list)):
                        obj = inner
                        break
                else:
                    return obj
            else:
                return None
        return obj if isinstance(obj, dict) else None

    if kind == 'kv':
        d = {}
        for k, v in NUM_RE.findall(text):
            d[k] = (True if v.lower() == 'true' else False if v.lower() == 'false' else float(v))
        return d or None

    if kind == 'csv' or kind == 'raw':
        sep = r'[,\t;|]' if kind == 'csv' else r'[\s,]+'
        parts = [p.strip() for p in re.split(sep, text.strip()) if p.strip() != '']
        if not parts:
            return None
        names = None
        if kind == 'csv':
            if cfg['header']:
                if cfg['_csv_header'] is None:
                    cfg['_csv_header'] = parts
                    return None
                names = cfg['_csv_header']
            else:
                names = cfg['fields']
        else:
            names = cfg['fields']
        if not names:
            names = list(GAME_FIELDS[:len(parts)])
        d = {}
        for i, p in enumerate(parts):
            if i >= len(names):
                break
            if NUMFULL_RE.fullmatch(p):
                d[names[i]] = float(p)
            elif p.lower() in ('true', 'false'):
                d[names[i]] = p.lower() == 'true'
            else:
                d[names[i]] = p
        return d or None

    # regex
    m = re.search(cfg['regex'], text)
    if not m:
        return None
    d = {}
    for k, v in m.groupdict().items():
        if v is None:
            continue
        if NUMFULL_RE.fullmatch(v):
            d[k] = float(v)
        elif v.lower() in ('true', 'false'):
            d[k] = v.lower() == 'true'
        else:
            d[k] = v
    return d or None


# ---------------------------------------------------------------- 换算：字段字典 → 游戏帧

def build_frame(sample, cfg, now_ms):
    m = cfg['map'] or {}

    def pick(name):
        return sample.get(m.get(name, name))

    rx = _num(pick('x'))
    x = 0.0
    if rx is not None:
        x = rx * cfg['scale_x'] + cfg['offset_x']
        if cfg['invert_x']:
            x = -x
        x = max(-1.0, min(1.0, x))

    def flag(name, th):
        v = pick(name)
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        n = _num(v)
        if n is None:
            return 0.0
        return (1.0 if n >= th else 0.0) if th is not None else n

    jump = flag('jump', cfg['jump_th'])
    crouch = flag('crouch', cfg['crouch_th'])
    height = _num(pick('height')) or 0.0
    conf = _num(pick('confidence'))
    conf = cfg['default_confidence'] if conf is None else conf

    t = _num(pick('ts'))
    if t is not None:
        if t < 1e6:            # 上游给的是秒
            t *= 1000.0
        if t < 1e11:           # 不像毫秒时间戳 → 用本机时间
            t = now_ms
    else:
        t = now_ms

    return {'ts': int(t), 'x': round(x, 4), 'height': round(height, 4),
            'jump': jump, 'crouch': crouch, 'confidence': round(conf, 3),
            'raw': sample}


# ---------------------------------------------------------------- 后台判定
#
# 为什么要搬到后台：
#   原来的判定在游戏前端（js/InputSource.js 的 RadarDecoder）。双雷达时前端要开两条
#   WebSocket、各自跑一个解码器、还要处理两台时钟不同源 —— 现场出问题很难定位。
#   现在判定放在「读雷达的那个进程」里，游戏只收 left/right/jump/slide 结果。

def apply_role(frame, role):
    """按分工裁剪维度（口径与前端 pickRoleFrame 一致）。

    lr = 只管左右：jump/crouch/height 清零，永远产生不出跳/蹲
    ud = 只管上下：x 清零，永远换不了道
    none / 空 = 不裁剪，单雷达全维度判定
    """
    if not role or role == 'none':
        return frame
    out = dict(frame)
    if role == 'lr':
        out['height'] = 0.0
        out['jump'] = 0.0
        out['crouch'] = 0.0
    else:
        out['x'] = 0.0
    return out


class Judge:
    """把连续帧判成离散动作 left / right / jump / slide。

    判定口径与前端 RadarDecoder 保持一致（车道迟滞、单帧限档、换道冷却、上升沿），
    另外补两条只有在后端才需要处理的现场问题：

      1. 跳/蹲最小间隔：上游抖动时同一动作不会连发；
      2. 掉线恢复后第一帧不补发动作：上下雷达只有 1.92Hz，断一帧再回来时
         上游很可能还保持着 jump=1，不挡住就会「补跳一次」。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.role = cfg.get('role') or 'none'
        self.lane = 1
        self.prev_jump = False
        self.prev_crouch = False
        self.last_lane_at = 0.0
        self.last_jump_at = 0.0
        self.last_slide_at = 0.0
        self.last_frame_at = 0.0
        self.resume_guard = True       # True = 首帧 / 刚掉线恢复，本帧不判上升沿
        self.seq = 0

    def feed(self, frame, now_ms):
        """喂一帧，返回本次触发的动作列表 [{'action':..., 'lane':..., 'ts':...}]"""
        cfg = self.cfg
        stale_ms = cfg.get('judge_stale_ms', 800)
        events = []

        # ---- 掉线恢复：先清掉上升沿记忆，并让这一帧只做状态同步 ----
        if self.last_frame_at and now_ms - self.last_frame_at > stale_ms:
            self.prev_jump = False
            self.prev_crouch = False
            self.resume_guard = True
        self.last_frame_at = now_ms

        if frame.get('confidence', 1) < cfg.get('min_conf', 0.3):
            self.resume_guard = False
            return events

        # ---- 横向：只对 lr（或单雷达）生效 ----
        if self.role in ('lr', 'none'):
            gain = cfg.get('x_gain', 1.0)
            x = frame.get('x', 0.0)
            if gain and gain != 1.0:
                x = x * gain
            x = max(-1.0, min(1.0, x))

            enter_th = cfg.get('enter_th', 0.36)
            exit_th = cfg.get('exit_th', 0.22)
            lane = self.lane
            if lane == 1:
                if x <= -enter_th:
                    lane = 0
                elif x >= enter_th:
                    lane = 2
            elif lane == 0:
                if x >= enter_th:
                    lane = 2
                elif x >= -exit_th:
                    lane = 1
            else:
                if x <= -enter_th:
                    lane = 0
                elif x <= exit_th:
                    lane = 1

            # 单帧限档：一下甩到最右边也只走一格
            step = max(1, int(cfg.get('max_lane_step', 1)))
            if lane > self.lane + step:
                lane = self.lane + step
            elif lane < self.lane - step:
                lane = self.lane - step

            # 换道冷却：冷却中这一帧先不动，下一帧继续判（动作不会丢）
            if lane != self.lane:
                cd = cfg.get('lane_cooldown_ms', 120)
                if cd > 0 and self.last_lane_at and now_ms - self.last_lane_at < cd:
                    lane = self.lane
                else:
                    self.last_lane_at = now_ms

            while self.lane != lane:
                if lane > self.lane:
                    events.append({'action': 'right', 'lane': self.lane + 1})
                    self.lane += 1
                else:
                    events.append({'action': 'left', 'lane': self.lane - 1})
                    self.lane -= 1

        # ---- 上下：只对 ud（或单雷达）生效 ----
        if self.role in ('ud', 'none'):
            jump_th = cfg.get('judge_jump_th', 0.5)
            crouch_th = cfg.get('judge_crouch_th', 0.5)
            jumping = (frame.get('jump', 0.0) >= jump_th
                       or frame.get('height', 0.0) >= jump_th)
            crouching = frame.get('crouch', 0.0) >= crouch_th

            if not self.resume_guard:
                if jumping and not self.prev_jump:
                    gap = cfg.get('jump_gap_ms', 450)
                    if not self.last_jump_at or now_ms - self.last_jump_at >= gap:
                        self.last_jump_at = now_ms
                        events.append({'action': 'jump', 'lane': self.lane})
                if crouching and not self.prev_crouch:
                    gap = cfg.get('slide_gap_ms', 400)
                    if not self.last_slide_at or now_ms - self.last_slide_at >= gap:
                        self.last_slide_at = now_ms
                        events.append({'action': 'slide', 'lane': self.lane})

            self.prev_jump = jumping
            self.prev_crouch = crouching

        self.resume_guard = False

        for ev in events:
            self.seq += 1
            ev['seq'] = self.seq
            ev['ts'] = now_ms
            ev['role'] = self.role
            ev['confidence'] = frame.get('confidence', 1)
        return events


def make_action(ev):
    return {'type': 'action', 'action': ev['action'], 'ts': ev['ts'],
            'seq': ev['seq'], 'role': ev['role'], 'lane': ev['lane'],
            'confidence': ev['confidence']}


def make_state(frame, cfg, ok, age_ms):
    st = {'type': 'state', 'ts': int(time.time() * 1000), 'role': cfg.get('role') or 'none',
          'ok': bool(ok), 'ageMs': int(age_ms * 1000)}
    if frame:
        st['x'] = frame.get('x', 0.0)
        st['height'] = frame.get('height', 0.0)
        st['jump'] = frame.get('jump', 0.0)
        st['crouch'] = frame.get('crouch', 0.0)
        st['confidence'] = frame.get('confidence', 1)
        st['srcTs'] = frame.get('ts')
    return st


# ---------------------------------------------------------------- 主流程

def ticker_loop(cfg, hub, latest, lock):
    dt = 1.0 / cfg['rate']
    judge = bool(cfg.get('judge'))
    state_gap = (1.0 / max(1.0, cfg.get('state_hz', 8.0))) if judge else 0.0
    t0 = time.time()
    i = 0
    last_warn = 0.0
    last_log = time.time()
    last_state = 0.0
    while True:
        due = t0 + i * dt
        gap = due - time.time()
        if gap > 0:
            time.sleep(gap)
        with lock:
            frame = latest['frame']
            at = latest['at']
        age = (time.time() - at) if frame else None
        if frame is not None:
            if judge:
                # 判定模式：动作在收帧那一刻已经即时发出（不等 tick，少 50ms 延迟），
                # ticker 只负责按 state_hz 发状态心跳，供游戏面板显示 x / 高度 / 在线状态。
                if state_gap and (time.time() - last_state) >= state_gap:
                    last_state = time.time()
                    ok = age is not None and age <= cfg.get('judge_stale_ms', 800) / 1000.0
                    hub.broadcast(json.dumps(make_state(frame, cfg, ok, age or 0.0),
                                             ensure_ascii=False))
            else:
                hub.broadcast(json.dumps(frame, ensure_ascii=False))
        if frame is not None and age is not None and age > cfg['stale'] and time.time() - last_warn > 5:
            last_warn = time.time()
            log('注意：上游已经 %.1fs 没有新数据了（仍在重复上一帧）—— 检查雷达是否在推数据' % age)
        if time.time() - last_log >= 10:
            last_log = time.time()
            log('运行中：已推送 %d 帧，在线客户端 %d 个%s'
                % (hub.sent, len(hub.clients), '，判定动作 %d 个' % latest.get('events', 0) if judge else ''))
        i += 1


def parse_map(s):
    d = {}
    for part in (s or '').split(','):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition('=')
        if k.strip() and v.strip():
            d[k.strip()] = v.strip()
    return d


def load_config(path):
    with open(path, encoding='utf-8') as f:
        raw = json.load(f)
    return {k.replace('-', '_'): v for k, v in raw.items()}


def main():
    ap = argparse.ArgumentParser(description='雷达桥接器：雷达原始输出 → 游戏可连的 ws JSON',
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', default='', help='JSON 配置文件（命令行参数优先）')
    ap.add_argument('--from', dest='from_', default='udp',
                    choices=['udp', 'tcp', 'tcp-listen', 'http', 'exec', 'stdin'],
                    help='雷达数据以什么形式出来，默认 udp')
    ap.add_argument('--udp-host', default='0.0.0.0', help='UDP 监听地址')
    ap.add_argument('--udp-port', type=int, default=8888, help='UDP 监听端口')
    ap.add_argument('--tcp', default='127.0.0.1:9000', help='--from tcp 时雷达的 host:port')
    ap.add_argument('--listen-port', type=int, default=9100, help='--from tcp-listen 时本机监听端口')
    ap.add_argument('--http', default='http://127.0.0.1:8080/api/target', help='--from http 时的接口地址')
    ap.add_argument('--interval', type=float, default=0.05, help='HTTP 轮询间隔（秒）')
    ap.add_argument('--http-timeout', type=float, default=2.0, help='HTTP 请求超时（秒）')
    ap.add_argument('--exec', dest='exec_cmd', default='', help='--from exec 时要运行的命令')
    ap.add_argument('--parse', default='json', choices=['json', 'kv', 'csv', 'raw', 'regex'],
                    help='报文解析方式，默认 json')
    ap.add_argument('--fields', default='', help='csv/raw 的列名顺序，如 x,height,jump,crouch')
    ap.add_argument('--header', action='store_true', help='csv 首行是表头')
    ap.add_argument('--regex', default='', help='regex 解析用的命名组正则')
    ap.add_argument('--array-index', type=int, default=0, help='JSON 是数组（或多人）时取第几个')
    ap.add_argument('--map', default='', help='字段名映射，如 x=px,jump=j,crouch=duck')
    ap.add_argument('--scale-x', type=float, default=1.0, help='x 的缩放（像素/毫米 → 归一化）')
    ap.add_argument('--offset-x', type=float, default=0.0, help='x 的零点偏移')
    ap.add_argument('--invert-x', action='store_true', help='左右反了时打开（等价于 x 取负）')
    ap.add_argument('--jump-th', type=float, default=None, help='上游是连续值时，超过它算跳')
    ap.add_argument('--crouch-th', type=float, default=None, help='上游是连续值时，超过它算蹲')
    ap.add_argument('--default-confidence', type=float, default=0.9, help='上游没有 confidence 时用它')
    ap.add_argument('--rate', type=float, default=20.0, help='输出帧率，默认 20（游戏按 20Hz 设计）')
    ap.add_argument('--host', default='0.0.0.0', help='输出监听地址')
    ap.add_argument('--port', type=int, default=8766, help='输出端口，默认 8766')
    ap.add_argument('--stale', type=float, default=2.0, help='上游多久没数据就提醒（秒）')
    ap.add_argument('--print-sample', action='store_true', help='打印原始报文与解析结果（首次接入必用）')

    # ---- 后台判定：游戏只收结果，不再做阈值判定 ----
    g = ap.add_argument_group('后台判定（--judge）')
    g.add_argument('--judge', action='store_true',
                   help='打开后台判定：输出 left/right/jump/slide 动作事件，游戏侧只消费结果')
    g.add_argument('--role', default='none', choices=['none', 'lr', 'ud'],
                   help='分工：lr 只管左右(x)，ud 只管上下(jump/crouch/height)，none=全维度')
    g.add_argument('--enter-th', type=float, default=0.36, help='进左右车道阈值（默认 0.36）')
    g.add_argument('--exit-th', type=float, default=0.22, help='回中间车道阈值（默认 0.22）')
    g.add_argument('--x-gain', type=float, default=1.0, help='横向增益，等价前端 RADAR.xGain')
    g.add_argument('--max-lane-step', type=int, default=1, help='单帧最多换几档（默认 1）')
    g.add_argument('--lane-cooldown-ms', type=int, default=120, help='两次换道最小间隔(ms)')
    g.add_argument('--judge-jump-th', type=float, default=0.5, help='判定用的起跳阈值（0~1）')
    g.add_argument('--judge-crouch-th', type=float, default=0.5, help='判定用的下蹲阈值（0~1）')
    g.add_argument('--min-conf', type=float, default=0.3, help='低于此可信度的帧直接丢弃')
    g.add_argument('--jump-gap-ms', type=int, default=450, help='两次 jump 的最小间隔（防抖）')
    g.add_argument('--slide-gap-ms', type=int, default=400, help='两次 slide 的最小间隔（防抖）')
    g.add_argument('--judge-stale-ms', type=int, default=800,
                   help='多久没收到帧算掉线（默认 800；上下雷达 1.92Hz 需大于 520）')
    g.add_argument('--state-hz', type=float, default=8.0,
                   help='判定模式下状态帧的心跳频率（面板显示用，默认 8Hz）')
    args = ap.parse_args()

    cfg = vars(args)
    cfg['fields'] = [p.strip() for p in (args.fields or '').split(',') if p.strip()] or None
    cfg['map'] = parse_map(args.map)
    cfg['_csv_header'] = None
    if args.config:
        cfg.update(load_config(args.config))
        cfg['map'] = cfg.get('map') or {}
        if isinstance(cfg.get('fields'), str):
            cfg['fields'] = [p.strip() for p in cfg['fields'].split(',') if p.strip()]
    if cfg['from_'] == 'exec' and not cfg['exec_cmd']:
        log('--from exec 需要 --exec "命令"')
        return 2

    print('=' * 66, flush=True)
    log('雷达桥接器启动')
    log('上游 %s  解析 %s%s%s' % (cfg['from_'], cfg['parse'],
                                  ('  字段映射 %s' % cfg['map']) if cfg['map'] else '',
                                  ('  x 缩放 %.4g 偏移 %.4g%s' % (cfg['scale_x'], cfg['offset_x'],
                                   ' 左右反转' if cfg['invert_x'] else ''))))
    log('输出 ws://%s:%d  →  游戏/联调台填 ws://127.0.0.1:%d' % (cfg['host'], cfg['port'], cfg['port']))
    if cfg['judge']:
        log('后台判定：开（分工 %s）—— 游戏只收 left/right/jump/slide 结果' % cfg['role'])
        log('  进道 %.2f / 回道 %.2f / 横向增益 %.2f / 换道冷却 %dms'
            % (cfg['enter_th'], cfg['exit_th'], cfg['x_gain'], cfg['lane_cooldown_ms']))
        log('  跳 %.2f / 蹲 %.2f / 动作防抖 %dms / 掉线 %.0fms / 状态心跳 %.1fHz'
            % (cfg['judge_jump_th'], cfg['judge_crouch_th'], cfg['jump_gap_ms'],
               cfg['judge_stale_ms'], cfg['state_hz']))
        if cfg['role'] == 'none':
            log('  提示：双雷达现场请用 --role lr / --role ud 各跑一个进程，避免两路互相抢动作')
    elif not cfg['print_sample']:
        log('第一次接雷达建议加 --print-sample，先确认收到的报文长什么样')
    print('=' * 66, flush=True)

    hub = Hub()
    latest = {'frame': None, 'at': 0.0, 'events': 0}
    lock = threading.Lock()
    judge = Judge(cfg) if cfg['judge'] else None

    threading.Thread(target=ws_server, args=(cfg['host'], cfg['port'], hub), daemon=True).start()
    threading.Thread(target=ticker_loop, args=(cfg, hub, latest, lock), daemon=True).start()

    def warn(msg):
        log('⚠ ' + msg)

    last_bad = [0.0]

    try:
        for text, src in make_source(cfg, warn):
            try:
                sample = parse_sample(text, cfg)
            except Exception as e:
                if time.time() - last_bad[0] > 5:
                    last_bad[0] = time.time()
                    log('解析失败（%s）—— 原文：%s' % (e, text[:160]))
                continue
            if not sample:
                continue
            frame = build_frame(sample, cfg, int(time.time() * 1000))
            frame = apply_role(frame, cfg['role'])        # 分工裁剪：本进程只保留自己管的维度
            if cfg['print_sample']:
                log('收到: %s' % text[:160])
                log('解析: %s' % json.dumps(sample, ensure_ascii=False)[:160])
                log('成帧: x=%.4f jump=%.2f crouch=%.2f' % (frame['x'], frame['jump'], frame['crouch']))

            if judge is not None:
                # 判定就在收帧这一刻做，动作即时广播 —— 不等 ticker，少 50ms 延迟
                events = judge.feed(frame, frame['ts'])
                for ev in events:
                    hub.broadcast(json.dumps(make_action(ev), ensure_ascii=False))
                if events:
                    with lock:
                        latest['events'] += len(events)
                    log('动作: %s' % ' '.join(
                        '%s(lane=%d)' % (e['action'], e['lane']) for e in events))

            with lock:
                latest['frame'] = frame
                latest['at'] = time.time()
    except KeyboardInterrupt:
        log('已停止')
    return 0


if __name__ == '__main__':
    sys.exit(main())
