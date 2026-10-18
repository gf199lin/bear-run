# -*- coding: utf-8 -*-
"""
雷达点云实时显示 —— 本地服务。

用法:
  python server.py                              # COM 口自动识别（插上雷达即可）
  python server.py --port COM5                  # 手动指定串口
  python server.py --source replay --replay cap.bin
  python server.py --source loopback            # 无硬件自检（不插雷达也能看页面）
  python server.py --record out.bin             # 同时录制原始字节流
  python server.py --open                       # 启动后自动打开浏览器

然后浏览器打开 http://127.0.0.1:8099
（双击同目录 start_viewer.cmd 可一键完成：启动服务 + 自动打开浏览器）
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
from radar_actions import ActionRecognizer, load_calib, AUTO_BAND
from radar_control import RadarController
from radar_source import SerialSource, ReplaySource, LoopbackSource

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENTS = set()
CLIENTS_LOCK = threading.Lock()
LATEST = {'cfg': None}
CTL_REF = {'get': None}     # main() 注入；让 /status、/stream 在任何时刻都能取到控制状态与串口诊断


def current_control():
    """任何时刻都拿得到「控制状态 + 串口诊断」，而不必等第一帧或第一次心跳。

    这样页面刚打开（或服务刚起来）就能显示「有没有认到雷达、为什么没数据」，
    而不是空白等 1 秒。
    """
    st = LATEST.get('control')
    if st:
        return st
    fn = CTL_REF.get('get')
    if callable(fn):
        try:
            return fn()
        except Exception:
            return {}
    return {}
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
        'src_diag': stats.get('src_diag'),
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
            if st.get('control') is None:
                st['control'] = current_control()
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
                                                'control': current_control()})
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
    ap.add_argument('--port', default='auto', help='串口号 (默认 auto = 自动识别 CP210x/雷达串口)')
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
                    help='屏蔽小于该距离(米)的点。内置默认 0.681 = 整门排除泄漏门 '
                         'bin0(r≈0.34 m)：实测该门占 ~73%% 点数但仅 ~0.1%% 功率，'
                         '滤掉不损失真实回波（人站 1.0 m 时 bin1/bin2 一个点都不掉）；'
                         '人站得远(≥2 m)时才用 1.4 连 bin1 一起滤')
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
    ap.add_argument('--open', action='store_true', dest='open_browser',
                    help='启动后自动用默认浏览器打开点云页面')
    ap.add_argument('--max-step', type=float, default=None,
                    help='单帧 px 跳变上限(米)，超过判野值不入窗。内置默认 0=关闭（稀疏点云下固定门会丢帧），建议由 calib.json 给出')
    ap.add_argument('--band', type=float, nargs=2, default=None,
                    metavar=('RMIN', 'RMAX'),
                    help='动作识别距离带（米），内置默认 0.70 1.35'
                         '（可由 calib.json 的 band 覆盖）')
    ap.add_argument('--auto-band', action='store_true',
                    help='自动选带：开机预热若干帧，按「检出最多的距离门」自行定带'
                         '（站位换了不用重新改参数）。与 --band 互斥，本开关优先')
    ap.add_argument('--auto-band-min-frames', type=int, default=None,
                    help='自动选带最早锁定帧数，默认 30（约 3.9 s）')
    ap.add_argument('--auto-band-max-frames', type=int, default=None,
                    help='自动选带兜底锁定帧数，默认 120（约 15.5 s）')
    ap.add_argument('--crit', default=None, choices=['delta', 'drift'],
                    help='动作判据。delta=与带内静息基线比绝对位移（出厂默认）；'
                         'drift=只看相对量「当前 px - 静息基线」。'
                         '★ 离线实测：出厂默认 delta 会在 1.25 m 站位主动误报 5 次，'
                         'drift 配 top-k 1 + min-pts 1 才是真值窗命中 8/10 的组合（提案，待小组确认）')
    ap.add_argument('--top-k', type=int, default=None,
                    help='每帧只取带内最强 K 点（0=全取）。★ 必须与 --min-pts 1 同用')
    ap.add_argument('--min-pts', type=int, default=None,
                    help='带内点数低于此值即视为目标丢失，默认 2。'
                         '★ top-k=1 时必须给 1，否则每帧都被判成丢失')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    global CONTROL_HANDLER

    # ---- 校准文件解析：优先级 显式命令行 > calib.json > 内置默认 ----
    cal = load_calib(args.calib) if args.calib else None
    psec = (cal or {}).get('processor', {}) or {}
    cfar = args.cfar if args.cfar is not None else psec.get('cfar_scale', 2.6)
    min_snr = args.min_snr if args.min_snr is not None else psec.get('min_snr', 7.0)
    # 内置默认 dsp.RANGE_RES（0.6813）= 「整门排除 bin0」的边界：bin0 中心
    # 0.34 m < 0.6813，bin1 中心 1.02 m > 0.6813，正好只切掉泄漏门。
    # 优先级不变（显式命令行 > calib.json > 内置默认）；只是把内置默认从 0.0
    # 改为「排除泄漏门」，避免不传参数时 0.34 m 的泄漏云铺满画面。
    min_range = (args.min_range if args.min_range is not None
                 else psec.get('min_range', dsp.RANGE_RES))
    angle_sign = (args.angle_sign if args.angle_sign is not None
                  else psec.get('angle_sign', 1.0))
    angle_max = (args.angle_max if args.angle_max is not None
                 else psec.get('angle_max', 45.0))
    if args.auto_band and args.band is not None:
        print('注意: --auto-band 优先，--band 本次被忽略')
    if args.auto_band:
        band = AUTO_BAND              # 预热期由识别器自行锁定，band 仍是 None
    elif args.band is not None:
        band = tuple(args.band)
    else:
        band = tuple((cal or {}).get('band', [0.70, 1.35]))
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
        # band 可能是 'auto'（预热期尚未锁定）→ 先用空列表占位，锁定后由
        # on_frame 写回真实带；viewer 侧 `cfg.band[1] > 0` 天然跳过空列表。
        'band': ([] if band == AUTO_BAND
                 else [round(band[0], 2), round(band[1], 2)]),
        'band_mode': ('auto' if band == AUTO_BAND else 'fixed'),
        'calib': os.path.basename(args.calib) if args.calib else None,
        'actions': not args.no_actions,
        'autostart': not args.no_autostart,
        'port': args.port,
        'port_auto': (args.source == 'serial'
                      and str(args.port).strip().lower() in ('auto', '')),
        'hw_stop': False,          # 硬件停机（实验·未验证）在 server 里默认不开放
    }

    state = {'last': 0.0}
    a_kw = {}
    for key, val in (('crit', args.crit), ('top_k', args.top_k),
                     ('min_pts', args.min_pts),
                     ('auto_band_min_frames', args.auto_band_min_frames),
                     ('auto_band_max_frames', args.auto_band_max_frames)):
        if val is not None:
            a_kw[key] = val
    if args.top_k == 1 and args.min_pts is None:
        print('提示: --top-k 1 通常需配 --min-pts 1，否则每帧都会被判成目标丢失')
    recognizer = None if args.no_actions else ActionRecognizer(
        band=band, calib=cal, max_step=args.max_step, **a_kw)
    if recognizer is not None and not args.quiet:
        print('动作识别: band=%s crit=%s top_k=%d min_pts=%d'
              % ('自动选带（预热 %d~%d 帧）' % (recognizer.auto_band_min_frames,
                                              recognizer.auto_band_max_frames)
                 if band == AUTO_BAND else recognizer.band,
                 recognizer.crit, recognizer.top_k, recognizer.min_pts))

    def on_frame(z0, z1, stats):
        pts, noise = proc.process(z0, z1)
        r0, _ = proc.rd_map(z0, z1)
        act = recognizer.push(pts, time.time()) if recognizer is not None else None
        # 自动选带：锁定后把真实带写回 cfg，viewer 就能画出正确的带、
        # 「按 status 复盘现场设置」也看得到到底选中了哪个门。
        if recognizer is not None and band == AUTO_BAND:
            rb = recognizer.band
            if rb is not None:
                cb = LATEST['cfg'].get('band') or []
                if (len(cb) != 2 or abs(cb[0] - rb[0]) > 1e-9
                        or abs(cb[1] - rb[1]) > 1e-9):
                    LATEST['cfg']['band'] = [round(rb[0], 2), round(rb[1], 2)]
                    LATEST['cfg']['band_gate'] = recognizer.band_gate
                    LATEST['cfg']['band_locked_frame'] = \
                        recognizer.band_locked_frame
                    LATEST['cfg']['band_source'] = recognizer.band_source
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
    CTL_REF['get'] = ctrl.status
    try:
        push_control()          # 立刻推一次：页面/探测端一打开就能看到串口诊断
    except Exception:
        pass

    def heartbeat():
        while not SHUTDOWN.is_set():
            time.sleep(1.0)
            try:
                push_control()
            except Exception:
                pass

    threading.Thread(target=heartbeat, daemon=True, name='radar-heartbeat').start()

    try:
        srv = Server(('127.0.0.1', args.http_port), Handler)
    except OSError as e:
        print('端口 %d 无法监听（%s）。' % (args.http_port, e))
        print('通常是因为已经有一个点云服务在跑：先看看 http://127.0.0.1:%d 能不能打开；'
              '要另起一个就换端口，例如 --http-port %d。'
              % (args.http_port, args.http_port + 1))
        return 3
    if args.source == 'serial':
        if str(args.port).strip().lower() in ('auto', ''):
            src_txt = '自动识别串口（CP210x/雷达）, %d baud' % args.baud
        else:
            src_txt = '%s@%d' % (args.port, args.baud)
    elif args.source == 'replay':
        src_txt = '回放 %s' % args.replay
    else:
        src_txt = 'loopback（内置合成数据，不读串口）'
    url = 'http://127.0.0.1:%d' % args.http_port
    print('数据源: %s' % src_txt)
    print('点云页面: %s   （页面上有 开始 / 结束 / 重置 三个按钮）' % url)
    print('控制接口: POST /control  {"action":"start"|"stop"|"reset"}')
    if args.no_autostart:
        print('已禁用自动开始：请点页面「开始」，或 POST /control {"action":"start"}')
    else:
        ctrl.start()
    if args.open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
            print('已用默认浏览器打开: %s' % url)
        except Exception as e:
            print('未能自动打开浏览器: %s —— 请手动访问 %s' % (e, url))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n停止中...')
    finally:
        SHUTDOWN.set()
        ctrl.shutdown()
        time.sleep(0.3)
        srv.server_close()
    return 0


if __name__ == '__main__':
    main()
