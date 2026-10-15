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
from radar_source import SerialSource, ReplaySource, LoopbackSource, build_pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENTS = set()
CLIENTS_LOCK = threading.Lock()
LATEST = {'cfg': None}


def make_source(args):
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


def make_frame_payload(pts, r0, stats, proc, noise):
    return {
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
            body = json.dumps(LATEST.get('status', {}), ensure_ascii=False).encode('utf-8')
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
                hello = ('data: ' + json.dumps({'type': 'hello', 'cfg': LATEST['cfg']}) + '\n\n')
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
    ap.add_argument('--replay', default=os.path.join(HERE, '..', '.deepworks', 'tmp', 'cap.bin'))
    ap.add_argument('--record', default=None)
    ap.add_argument('--http-port', type=int, default=8099)
    ap.add_argument('--cfar', type=float, default=2.6)
    ap.add_argument('--min-power', type=float, default=0.0, help='绝对功率门限, 0=关闭')
    ap.add_argument('--min-snr', type=float, default=7.0, help='相对噪声底 SNR 门限 (dB)')
    ap.add_argument('--no-clutter', action='store_true', help='不做静态杂波抑制')
    ap.add_argument('--angle-sign', type=float, default=1.0, choices=[1.0, -1.0])
    ap.add_argument('--angle-max', type=float, default=45.0)
    ap.add_argument('--retry', type=float, default=1.0, help='串口断开后的重连间隔 (秒)')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    proc = PointCloudProcessor(cfar_scale=args.cfar, min_power=args.min_power,
                               min_snr=args.min_snr,
                               remove_clutter=not args.no_clutter,
                               angle_sign=args.angle_sign, angle_max=args.angle_max)
    LATEST['cfg'] = {
        'range_res': round(dsp.RANGE_RES, 4),
        'max_range': round(dsp.MAX_RANGE, 3),
        'v_max': round(dsp.V_MAX, 3),
        'v_res': round(dsp.V_RES, 4),
        'chirps': dsp.CHIRPS,
        'bins': dsp.BINS,
        'angle_max': args.angle_max,
        'clutter': not args.no_clutter,
    }

    src = make_source(args)
    stop = threading.Event()
    state = {'last': 0.0, 'frames': 0}

    def on_frame(z0, z1, stats):
        pts, noise = proc.process(z0, z1)
        r0, _ = proc.rd_map(z0, z1)
        payload = make_frame_payload(pts, r0, stats, proc, noise)
        broadcast(payload)
        LATEST['status'] = {k: v for k, v in payload.items() if k not in ('rd', 'pts')}
        now = time.time()
        if not args.quiet and now - state['last'] > 2.0:
            state['last'] = now
            print('[%6.2fs] frames=%d fps=%.2f pts=%d skipped=%d bad=%d conn=%s rc=%d err=%d'
                  % (stats['uptime'], stats['frames'], stats['fps'], len(pts),
                     stats['skipped'], stats['bad'], stats['connected'],
                     stats['reconnects'], stats['read_errors']), flush=True)

    t = threading.Thread(target=build_pipeline,
                         args=(src, on_frame, stop, args.record),
                         daemon=True)
    t.start()

    def watchdog():
        """采集线程万一退出就自动拉起，保证演示不中断。"""
        nonlocal t
        while not stop.is_set():
            time.sleep(1.0)
            if not t.is_alive():
                print('[watchdog] 采集线程已退出，正在重启...', flush=True)
                t = threading.Thread(target=build_pipeline,
                                     args=(src, on_frame, stop, args.record),
                                     daemon=True)
                t.start()

    threading.Thread(target=watchdog, daemon=True).start()

    srv = Server(('127.0.0.1', args.http_port), Handler)
    print('数据源: %s' % src.name)
    print('点云页面: http://127.0.0.1:%d' % args.http_port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n停止中...')
    finally:
        stop.set()
        time.sleep(0.3)
        srv.server_close()


if __name__ == '__main__':
    main()
