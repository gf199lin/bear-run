# -*- coding: utf-8 -*-
"""
雷达点云实时显示 —— 本地服务。

用法:
  python server.py                              # COM3 实时
  python server.py --port COM5
  python server.py --source replay --replay cap.bin
  python server.py --source loopback            # 无硬件自检
  python server.py --record out.bin             # 同时录制原始字节流

然后浏览器打开 http://127.0.0.1:8099
"""
import argparse
import json
import math
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import radar_dsp as dsp
from radar_dsp import PointCloudProcessor
from radar_actions import ActionRecognizer, load_calib
from radar_control import RadarController
from radar_source import SerialSource, ReplaySource, LoopbackSource

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENTS = set()
CLIENTS_LOCK = threading.Lock()
LATEST = {'cfg': None}
CONTROL_HANDLER = None          # 由 main() 注入：action 字符串 → 结果 dict
SHUTDOWN = threading.Event()


def make_source(args):
    """每次「开始」都新建一个数据源，避免上一次会话的残留污染新会话。"""
    def evt(msg):
        print(msg, flush=True)
    if args.source == 'serial':
        return SerialSource(args.port, args.baud, retry_delay=args.retry, on_event=evt)
    if args.source == 'replay':
        if not args.replay:
            raise SystemExit('--source replay 需要 --replay <文件>')
        return ReplaySource(args.replay, loop=True)
    return LoopbackSource()


def rd_json(r0):
    p = np.abs(r0) ** 2
    db = 10.0 * np.log10(p + 1.0)
    mx = float(db.max()) if db.size else 1.0
    mn = mx - 40.0
    q = np.clip((db - mn) / 40.0 * 99.0, 0, 99).astype(np.uint8)
    return q.reshape(-1).tolist()


def make_frame_payload(pts, r0, stats, proc, noise, control=None, actions=None):
    out = {
        'type': 'frame',
        'n': stats['frames'],
        'fps': round(stats['fps'], 2),
        'src': stats['source'],
        'skipped': stats['skipped'],
        'bad': stats['bad'],
        'points': len(pts),
        'noise': round(noise, 1),
        'connected': stats.get('connected', True),
        'reconnects': stats.get('reconnects', 0),
        'read_errors': stats.get('read_errors', 0),
        'dropped': stats.get('dropped', 0),
        'cb_errors': stats.get('cb_errors', 0),
        'pts': [[round(p['x'], 3), round(p['y'], 3), round(p['deg'], 1),
                 round(p['v'], 2), round(p['p'], 3)] for p in pts],
        'rd': rd_json(r0),
        'cfg': LATEST['cfg'],
    }
    if control is not None:
        out['control'] = control
    if actions is not None:
        out['act'] = {
            'lane': actions['lane'],
            'target': actions['target'],
            'features': actions['features'],
        }
        if actions['events']:
            out['events'] = actions['events']
    return out


def broadcast(obj):
    msg = ('data: ' + json.dumps(obj, separators=(',', ':')) + '\n\n').encode('utf-8')
    with CLIENTS_LOCK:
        dead = []
        for q in CLIENTS:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            CLIENTS.discard(q)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/', '/viewer.html', '/index.html'):
            p = os.path.join(HERE, 'viewer.html')
            with open(p, 'rb') as f:
                self._send(200, 'text/html; charset=utf-8', f.read())
            return
        if path == '/status':
            # 注意：LATEST['status'] 只在收到第一帧后才存在。用 --no-autostart 启动时
            # （或刚启动、还没出帧时）要先回退到已知的 cfg / control，
            # 否则监控端与验收脚本会读到空对象、看不到「开始/结束」状态。
            st = dict(LATEST.get('status') or {})
            if st.get('cfg') is None:
                st['cfg'] = LATEST['cfg']
            if st.get('control') is None and 'control' in LATEST:
                st['control'] = LATEST['control']
            body = json.dumps(st, ensure_ascii=False).encode('utf-8')
            self._send(200, 'application/json; charset=utf-8', body)
            return
        if path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            q = queue.Queue(maxsize=8)
            with CLIENTS_LOCK:
                CLIENTS.add(q)
            try:
                hello = ('data: ' + json.dumps({'type': 'hello', 'cfg': LATEST['cfg'],
                                                'control': LATEST.get('control', {})})
                         + '\n\n')
                self.wfile.write(hello.encode('utf-8'))
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=5.0)
                    except queue.Empty:
                        self.wfile.write(b': keepalive\n\n')
                        self.wfile.flush()
                        continue
                    self.wfile.write(msg)
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with CLIENTS_LOCK:
                    CLIENTS.discard(q)
            return
        self._send(404, 'text/plain; charset=utf-8', b'not found')

    def do_POST(self):
        """控制接口：开始 / 结束 / 重置。

        请求体 JSON：{"action": "start" | "stop" | "reset"}；也接受 ?action=xxx。
        """
        path = self.path.split('?')[0]
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b''
        if path not in ('/control', '/api/control'):
            self._send(404, 'text/plain; charset=utf-8', b'not found')
            return
        action = None
        try:
            if raw:
                action = (json.loads(raw.decode('utf-8')) or {}).get('action')
        except Exception:
            action = None
        if not action and 'action=' in self.path:
            action = self.path.split('action=', 1)[1].split('&')[0]
        if CONTROL_HANDLER is None:
            res = {'ok': False, 'msg': '控制器未初始化'}
        else:
            res = CONTROL_HANDLER(action)
        body = json.dumps(res, ensure_ascii=False).encode('utf-8')
        self._send(200, 'application/json; charset=utf-8', body)


class Server(ThreadingHTTPServer):
    """忽略浏览器断开连接这类无意义异常，避免刷屏。"""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        et = sys.exc_info()[0]
        if et is not None and issubclass(et, (ConnectionResetError, ConnectionAbortedError,
                                              BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    ap = argparse.ArgumentParser(description='ICL1122 雷达点云实时显示')
    ap.add_argument('--source', choices=['serial', 'replay', 'loopback'], default='serial')
    ap.add_argument('--port', default='COM3', help='串口号 (默认 COM3)')
    ap.add_argument('--baud', type=int, default=921600)
    ap.add_argument('--replay', default=os.path.join(HERE, 'sample-synthetic.bin'),
                    help='回放/演示用的字节流文件 (默认本目录合成样本，不含人体数据)')
    ap.add_argument('--record', default=None)
    ap.add_argument('--http-port', type=int, default=8099)
    ap.add_argument('--calib', default=None,
                    help='校准文件 (radar_calibrate.py 产出的 calib.json)。'
                         '取其中 processor 段的 cfar_scale/min_snr/min_range/'
                         'angle_sign/angle_max 与识别阈值 band/v_on/v_off/min_dx/'
                         'max_step。优先级：显式命令行参数 > 校准文件 > 内置默认。'
                         '不含此参数时行为与本轮之前完全一致。')
    ap.add_argument('--cfar', type=float, default=None,
                    help='CFAR 门限倍数 (内置默认 2.6；越小人越容易被检出、杂波也越多)')
    ap.add_argument('--min-power', type=float, default=0.0, help='绝对功率门限, 0=关闭')
    ap.add_argument('--min-snr', type=float, default=None,
                    help='相对噪声底 SNR 门限 (dB，内置默认 7.0)')
    ap.add_argument('--no-clutter', action='store_true', help='不做静态杂波抑制')
    ap.add_argument('--min-range', type=float, default=None,
                    help='屏蔽小于该距离(米)的点, 建议 1.4 以滤掉 bin0/bin1 近距杂波'
                         '（内置默认 0.0）')
    ap.add_argument('--angle-sign', type=float, default=None, choices=[1.0, -1.0],
                    help='方位角正负约定（内置默认 1.0，可由 calib.json 给出）')
    ap.add_argument('--angle-max', type=float, default=None,
                    help='有效方位角上限（内置默认 45.0）')
    ap.add_argument('--retry', type=float, default=1.0, help='串口断开后的重连间隔 (秒)')
    ap.add_argument('--stall-timeout', type=float, default=8.0,
                    help='超过该秒数收不到帧就重建采集管道（0=关闭停滞判定）')
    ap.add_argument('--no-stall-restart', action='store_true',
                    help='关闭「线程退出/停滞」后的自动重建')
    ap.add_argument('--no-autostart', action='store_true',
                    help='启动后不自动采集，等页面点「开始」（省电/避免长期占串口）')
    ap.add_argument('--no-actions', action='store_true', help='关闭近距动作识别')
    ap.add_argument('--max-step', type=float, default=None,
                    help='单帧 px 跳变上限(米)，超过判野值不入窗。内置默认 0=关闭（稀疏点云下固定门会丢帧），建议由 calib.json 给出')
    ap.add_argument('--band', type=float, nargs=2, default=None,
                    metavar=('RMIN', 'RMAX'),
                    help='动作识别距离带（米），内置默认 0.70 1.35'
                         '（可由 calib.json 的 band 覆盖）')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    global CONTROL_HANDLER

    # ---- 校准文件解析：优先级 显式命令行 > calib.json > 内置默认 ----
    cal = load_calib(args.calib) if args.calib else None
    psec = (cal or {}).get('processor', {}) or {}
    cfar = args.cfar if args.cfar is not None else psec.get('cfar_scale', 2.6)
    min_snr = args.min_snr if args.min_snr is not None else psec.get('min_snr', 7.0)
    min_range = (args.min_range if args.min_range is not None
                 else psec.get('min_range', 0.0))
    angle_sign = (args.angle_sign if args.angle_sign is not None
                  else psec.get('angle_sign', 1.0))
    angle_max = (args.angle_max if args.angle_max is not None
                 else psec.get('angle_max', 45.0))
    band = tuple(args.band) if args.band is not None else         tuple((cal or {}).get('band', [0.70, 1.35]))
    if not args.quiet:
        print('校准: %s' % (args.calib if cal else '无（用内置默认/提案值）'))

    proc = PointCloudProcessor(cfar_scale=cfar, min_power=args.min_power,
                               min_snr=min_snr,
                               remove_clutter=not args.no_clutter,
                               angle_sign=angle_sign, angle_max=angle_max,
                               min_range=min_range)
    LATEST['cfg'] = {
        'range_res': round(dsp.RANGE_RES, 4),
        'max_range': round(dsp.MAX_RANGE, 3),
        'v_max': round(dsp.V_MAX, 3),
        'v_res': round(dsp.V_RES, 4),
        'chirps': dsp.CHIRPS,
        'bins': dsp.BINS,
        'angle_max': angle_max,
        'clutter': not args.no_clutter,
        'min_range': min_range,
        # 回显实际生效的 DSP 参数（P2-4：之前只回显 min_range，导致「按 status 复盘
        # 现场设置」时看不出真实 cfar/min_snr，与校准文件复现性相关）
        'cfar_scale': getattr(proc, 'cfar_scale', None),
        'min_snr': getattr(proc, 'min_snr', None),
        'angle_sign': getattr(proc, 'angle_sign', None),
        'band': [round(band[0], 2), round(band[1], 2)],
        'calib': os.path.basename(args.calib) if args.calib else None,
        'actions': not args.no_actions,
        'autostart': not args.no_autostart,
        'hw_stop': False,          # 硬件停机（实验·未验证）在 server 里默认不开放
    }

    state = {'last': 0.0}
    recognizer = None if args.no_actions else ActionRecognizer(
        band=band, calib=cal, max_step=args.max_step)

    def on_frame(z0, z1, stats):
        pts, noise = proc.process(z0, z1)
        r0, _ = proc.rd_map(z0, z1)
        act = recognizer.push(pts, time.time()) if recognizer is not None else None
        payload = make_frame_payload(pts, r0, stats, proc, noise,
                                     control=ctrl.status(), actions=act)
        broadcast(payload)
        LATEST['status'] = {k: v for k, v in payload.items() if k not in ('rd', 'pts')}
        if act and act['events']:
            print('[act] ' + ' '.join(
                '%s%s' % (e['name'], '(候选·未验证)' if e.get('gated') else '')
                for e in act['events']), flush=True)
        now = time.time()
        if not args.quiet and now - state['last'] > 2.0:
            state['last'] = now
            print('[%6.2fs] frames=%d fps=%.2f pts=%d skipped=%d bad=%d conn=%s rc=%d err=%d'
                  % (stats['uptime'], stats['frames'], stats['fps'], len(pts),
                     stats['skipped'], stats['bad'], stats['connected'],
                     stats['reconnects'], stats['read_errors']), flush=True)

    # 采集会话控制器：开始 / 结束 / 重置，以及线程退出或停滞后的自愈重建
    ctrl = RadarController(lambda: make_source(args), on_frame,
                           record_path=args.record,
                           log=lambda m: print(m, flush=True),
                           stall_timeout=args.stall_timeout,
                           auto_restart=not args.no_stall_restart)

    def push_control():
        st = ctrl.status()
        LATEST['control'] = st
        broadcast({'type': 'control', **st})

    def control_handler(action):
        if not action:
            return dict(ok=False, msg='缺少 action（start/stop/reset）', **ctrl.status())
        res = ctrl.control(action)
        print('[control] %s → %s'
              % (action, '成功' if res.get('ok') else res.get('msg')), flush=True)
        return res

    CONTROL_HANDLER = control_handler
    ctrl.set_on_change(lambda st: push_control())

    def heartbeat():
        while not SHUTDOWN.is_set():
            time.sleep(1.0)
            try:
                push_control()
            except Exception:
                pass

    threading.Thread(target=heartbeat, daemon=True, name='radar-heartbeat').start()

    srv = Server(('127.0.0.1', args.http_port), Handler)
    print('数据源: %s' % (('%s@%d' % (args.port, args.baud)) if args.source == 'serial'
                          else (args.replay if args.source == 'replay' else 'loopback')))
    print('点云页面: http://127.0.0.1:%d   （页面上有 开始 / 结束 / 重置 三个按钮）'
          % args.http_port)
    print('控制接口: POST /control  {"action":"start"|"stop"|"reset"}')
    if args.no_autostart:
        print('已禁用自动开始：请点页面「开始」，或 POST /control {"action":"start"}')
    else:
        ctrl.start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n停止中...')
    finally:
        SHUTDOWN.set()
        ctrl.shutdown()
        time.sleep(0.3)
        srv.server_close()


if __name__ == '__main__':
    main()
