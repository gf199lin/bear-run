# -*- coding: utf-8 -*-
"""雷达汇聚器：把「两个后台判定的程序」合成一条流，游戏只连这一个地址。

为什么要多这一层：
    双雷达现场是三个进程：
        radar_bridge.py --role lr --judge   → ws://127.0.0.1:9001（只出 left/right）
        radar_bridge.py --role ud --judge   → ws://127.0.0.1:9002（只出 jump/slide）
        radar_hub.py                        → ws://127.0.0.1:8766（游戏连这个）
    判定全部在上游两个进程里完成，本脚本**不做任何阈值判定**，只负责：
        1. 把两路的动作帧原样转发给游戏（补 via/seq，便于现场对数）
        2. 把两路的状态帧合并成一条，按 --state-hz 心跳发出（面板显示 x / 高度 / 在线）
        3. 单路掉线时继续放行另一路，并在状态帧里写 lr_ok / ud_ok

协议（游戏侧只认 type）：
    动作：{"type":"action","action":"left|right|jump|slide","ts":毫秒,"seq":n,
           "role":"lr|ud","lane":0|1|2,"confidence":0~1,"via":"hub"}
    状态：{"type":"state","ts":毫秒,"x":-1~1,"height":0~1,"jump":0/1,"crouch":0/1,
           "lr_ok":bool,"ud_ok":bool,"lr_ageMs":int|null,"ud_ageMs":int|null,"via":"hub"}

用法：
    python radar_hub.py --lr ws://127.0.0.1:9001 --ud ws://127.0.0.1:9002
    python radar_hub.py --lr ws://127.0.0.1:9001            # 只接一路（单雷达）
    python radar_hub.py --lr ... --ud ... --port 8766 --state-hz 10

零依赖：只用标准库。上游断线自动重连（2 秒），游戏侧不用刷新页面。
"""
import argparse
import base64
import json
import os
import socket
import struct
import sys
import threading
import time

from radar_bridge import Hub, ws_server, log

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
ACTIONS = ('left', 'right', 'jump', 'slide')


# ---------------------------------------------------------------- WebSocket 客户端
# 标准库里没有 WS 客户端，这里按 RFC6455 手写最小实现：
# 只处理文本帧 / 分片 / ping / close，足够连我们自己的 bridge。

def _recv_exact(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('上游关闭连接')
        buf += chunk
    return buf


def _send_frame(sock, opcode, payload):
    """客户端发出的帧必须加掩码（RFC6455 要求）。"""
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    if n < 126:
        head = struct.pack('!BB', 0x80 | opcode, 0x80 | n)
    elif n < 65536:
        head = struct.pack('!BBH', 0x80 | opcode, 0x80 | 126, n)
    else:
        head = struct.pack('!BBQ', 0x80 | opcode, 0x80 | 127, n)
    sock.sendall(head + mask + masked)


def _read_frame(sock):
    h = _recv_exact(sock, 2)
    fin = bool(h[0] & 0x80)
    opcode = h[0] & 0x0f
    masked = bool(h[1] & 0x80)
    n = h[1] & 0x7f
    if n == 126:
        n = struct.unpack('!H', _recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack('!Q', _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, n) if n else b''
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, fin, payload


def _parse_ws_url(url):
    if not url.startswith('ws://'):
        raise ValueError('只支持 ws://（不支持 wss://）: %s' % url)
    rest = url[len('ws://'):]
    if '/' in rest:
        hostport, path = rest.split('/', 1)
        path = '/' + path
    else:
        hostport, path = rest, '/'
    host, _, port = hostport.rpartition(':')
    if not host:
        host, port = hostport, '80'
    return host, int(port), path


class Upstream:
    """一条上游连接：连 bridge 的 ws，读 JSON，交给 merger；断了 2 秒重连。"""

    def __init__(self, role, url, merger):
        self.role = role
        self.url = url
        self.merger = merger
        self.sock = None
        self.online = False
        self.reconnects = 0
        self.closed = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.closed = True
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

    def _run(self):
        while not self.closed:
            try:
                self._connect_and_read()
            except Exception as e:
                if self.online:
                    log('%s 上游断开：%s' % (self.role.upper(), e))
                self.online = False
                self.merger.mark_offline(self.role)
                try:
                    if self.sock:
                        self.sock.close()
                except Exception:
                    pass
                self.sock = None
            if self.closed:
                return
            self.reconnects += 1
            time.sleep(2.0)

    def _connect_and_read(self):
        host, port, path = _parse_ws_url(self.url)
        sock = socket.create_connection((host, port), timeout=5)
        self.sock = sock
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        req = ('GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n'
               'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
               'Sec-WebSocket-Version: 13\r\n\r\n') % (path, host, port, key)
        sock.sendall(req.encode('latin-1'))

        head = b''
        while b'\r\n\r\n' not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError('握手阶段被关闭')
            head += chunk
            if len(head) > 65536:
                raise ConnectionError('握手响应过长')
        if b' 101 ' not in head.split(b'\r\n')[0]:
            raise ConnectionError('上游不是 WS 服务：%s' % head.split(b'\r\n')[0][:80])

        sock.settimeout(None)
        self.online = True
        log('%s 上游已连接 %s' % (self.role.upper(), self.url))

        buf = b''      # 分片累积
        frag_opcode = 0
        while not self.closed:
            opcode, fin, payload = _read_frame(sock)
            if opcode == 0x8:                     # close
                raise ConnectionError('上游发来 close')
            if opcode == 0x9:                     # ping → pong
                _send_frame(sock, 0xA, payload)
                continue
            if opcode == 0xA:                     # pong：不用处理
                continue
            if opcode in (0x1, 0x2):
                frag_opcode = opcode
                buf = payload
            elif opcode == 0x0:
                buf += payload
            else:
                continue
            if not fin:
                continue
            if frag_opcode == 0x1:
                self._handle_text(buf.decode('utf-8', 'replace'))
            buf = b''

    def _handle_text(self, text):
        try:
            obj = json.loads(text)
        except Exception:
            return
        items = obj if isinstance(obj, list) else [obj]
        for it in items:
            if isinstance(it, dict):
                self.merger.on_frame(self.role, it)


# ---------------------------------------------------------------- 合并
# 注意：这里**不做判定**。动作是上游两个 bridge 判出来的，本层只转发 + 去重。

class Merger:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.latest = {'lr': None, 'ud': None}
        self.at = {'lr': 0.0, 'ud': 0.0}
        self.last_action = {}
        self.seq = 0
        self.forwarded = 0
        self.dropped = 0
        self.warned_unjudged = False

    def mark_offline(self, role):
        with self.lock:
            self.at[role] = 0.0

    def on_frame(self, role, obj):
        kind = obj.get('type')
        act = obj.get('action')

        # 上游发的是连续帧（没开 --judge）：本层不替它判定，只提示一次
        if kind is None and act is None:
            if not self.warned_unjudged:
                self.warned_unjudged = True
                log('⚠ %s 上游发的是连续帧（没有 type 字段）—— 它没开 --judge，'
                    '动作不会产生。请用 --judge 重启那个 bridge。' % role.upper())
            with self.lock:
                self.latest[role] = obj
                self.at[role] = time.time()
            return

        if kind == 'action' or act is not None:
            self._forward_action(role, obj, act)
            return

        if kind == 'state':
            with self.lock:
                self.latest[role] = obj
                self.at[role] = time.time()

    def _forward_action(self, role, obj, act):
        if act not in ACTIONS:
            return
        now_ms = time.time() * 1000.0
        dedup = self.cfg.get('dedup_ms', 60)
        key = (role, act)
        last = self.last_action.get(key, 0.0)
        if dedup > 0 and now_ms - last < dedup:
            with self.lock:
                self.dropped += 1
            return
        self.last_action[key] = now_ms

        with self.lock:
            self.seq += 1
            seq = self.seq
            self.forwarded += 1
        out = {
            'type': 'action', 'action': act, 'ts': int(now_ms), 'seq': seq,
            'role': role, 'lane': obj.get('lane'),
            'confidence': obj.get('confidence', 1),
            'srcSeq': obj.get('seq'), 'via': 'hub',
        }
        self.cfg['hub'].broadcast(json.dumps(out, ensure_ascii=False))
        log('→ 游戏 %s（%s%s）' % (act, role,
                                  ('，lane=%s' % obj.get('lane')) if obj.get('lane') is not None else ''))

    def merged_state(self):
        now = time.time()
        stale = self.cfg.get('stale_ms', 1500) / 1000.0
        with self.lock:
            lr, ud = self.latest['lr'], self.latest['ud']
            lr_at, ud_at = self.at['lr'], self.at['ud']

        lr_ok = bool(lr) and lr_at > 0 and (now - lr_at) <= stale
        ud_ok = bool(ud) and ud_at > 0 and (now - ud_at) <= stale

        def pick(frame, key, ok, dflt=0.0):
            return frame.get(key, dflt) if (frame and ok) else dflt

        conf_lr = pick(lr, 'confidence', lr_ok, 1.0)
        conf_ud = pick(ud, 'confidence', ud_ok, 1.0)

        return {
            'type': 'state', 'ts': int(now * 1000),
            'x': round(float(pick(lr, 'x', lr_ok)), 4),
            'height': float(pick(ud, 'height', ud_ok)),
            'jump': float(pick(ud, 'jump', ud_ok)),
            'crouch': float(pick(ud, 'crouch', ud_ok)),
            'confidence': min(conf_lr, conf_ud),
            'lr_ok': lr_ok, 'ud_ok': ud_ok,
            'lr_ageMs': int((now - lr_at) * 1000) if lr_at else None,
            'ud_ageMs': int((now - ud_at) * 1000) if ud_at else None,
            'via': 'hub',
        }


def state_loop(cfg, merger):
    gap = 1.0 / max(0.5, cfg.get('state_hz', 10.0))
    last_log = time.time()
    while True:
        time.sleep(gap)
        try:
            cfg['hub'].broadcast(json.dumps(merger.merged_state(), ensure_ascii=False))
        except Exception:
            pass
        if time.time() - last_log >= 10:
            last_log = time.time()
            log('运行中：转发动作 %d 个，去重丢弃 %d 个，在线客户端 %d 个'
                % (merger.forwarded, merger.dropped, len(cfg['hub'].clients)))


def main():
    ap = argparse.ArgumentParser(
        description='雷达汇聚器：两个后台判定进程 → 一条流给游戏（本层不做判定）',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--lr', default='ws://127.0.0.1:9001', help='左右雷达 bridge 的地址')
    ap.add_argument('--ud', default='ws://127.0.0.1:9002', help='上下雷达 bridge 的地址（留空=只接一路）')
    ap.add_argument('--host', default='0.0.0.0', help='给游戏的监听地址')
    ap.add_argument('--port', type=int, default=8766, help='给游戏的端口，默认 8766')
    ap.add_argument('--state-hz', type=float, default=10.0, help='合并状态帧心跳频率，默认 10Hz')
    ap.add_argument('--dedup-ms', type=int, default=60, help='同一路同一动作的去重窗口(ms)')
    ap.add_argument('--stale-ms', type=int, default=1500,
                    help='某一路多久没消息算掉线(ms)，默认 1500（要大于上游 1.92Hz 的帧间隔）')
    args = ap.parse_args()

    cfg = vars(args)
    hub = Hub()
    cfg['hub'] = hub
    merger = Merger(cfg)

    print('=' * 66, flush=True)
    log('雷达汇聚器启动（只转发，不做判定）')
    log('左右上游 %s' % (args.lr or '（未配置）'))
    log('上下上游 %s' % (args.ud or '（未配置）'))
    log('输出 ws://%s:%d  →  游戏里填 ws://127.0.0.1:%d' % (args.host, args.port, args.port))
    log('游戏侧参数：?input=radar&judge=backend&radar=ws://127.0.0.1:%d' % args.port)
    print('=' * 66, flush=True)

    threading.Thread(target=ws_server, args=(args.host, args.port, hub), daemon=True).start()
    threading.Thread(target=state_loop, args=(cfg, merger), daemon=True).start()

    ups = []
    if args.lr:
        ups.append(Upstream('lr', args.lr, merger))
    if args.ud:
        ups.append(Upstream('ud', args.ud, merger))
    if not ups:
        log('--lr 和 --ud 至少给一个')
        return 2
    for u in ups:
        u.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log('已停止')
        for u in ups:
            u.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
