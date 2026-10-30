# -*- coding: utf-8 -*-
"""多板并行采集 —— 每块板一个独立会话，合流到同一页面。

设计要点
  1. **不影响单源路径**：只有传了 `--ports COM3,COM4` 才走这里；
     `python server.py --port COM4` 的行为逐字不变。
  2. **每板一份状态**：PointCloudProcessor（有杂波记忆）、ActionRecognizer
     （有滑窗）都是有状态的，绝对不能共用；各自一套。
  3. **时间轴不混**：1T2R 帧周期 129.5 ms、2T4R 520.8 ms，帧率差 4 倍。
     两条流各自按自己的 stats 统计，页面上也分开显示，不做任何插值对齐。
  4. **一板故障不连坐**：每个会话独立线程 + 独立 RadarController，
     某块板拔掉只影响它自己。

用法
  python server.py --ports COM3,COM4
  然后浏览器打开 http://127.0.0.1:8099/dual  （上下分屏）
"""
import os
import sys
import threading
import time

import radar_dsp as dsp
from radar_dsp import PointCloudProcessor
from radar_actions import ActionRecognizer
from radar_control import RadarController
from radar_source import (SerialSource, BOARD_PROFILES, DEFAULT_BOARD,
                          probe_board)

import server as srv


# ---------------------------------------------------------------- 板型参数
def dsp_kwargs_for(board):
    """按板型给出 PointCloudProcessor 的几何/刻度参数。"""
    prof = BOARD_PROFILES[board]
    if board == '2t4r':
        # DEV113P：32 距离单元 / 64 chirp / 4 RX，量程 0.376 m/s。
        # 换算依据见 docs/hardware-baseline.md 与 server.py 同处注释（厂商配置推导）。
        return dict(bins=prof['bins'], chirps=prof['chirps'],
                    range_res=prof['range_res'], v_res=prof['v_res'],
                    ant_spacing=prof['ant_spacing'],
                    lam=dsp.C / prof['f0'])
    return {}


def cfg_for(board, args, port):
    """构造该板在 viewer 里要用的 cfg（字段与单源路径保持一致）。"""
    prof = BOARD_PROFILES[board]
    if board == '2t4r':
        range_res, v_max, v_res = prof['range_res'], prof['v_max'], prof['v_res']
    else:
        range_res, v_max, v_res = dsp.RANGE_RES, dsp.V_MAX, dsp.V_RES

    min_range = args.min_range if args.min_range is not None else range_res
    if args.auto_band:
        band, band_mode = [], 'auto'
    elif args.band is not None:
        band, band_mode = [round(args.band[0], 2), round(args.band[1], 2)], 'fixed'
    else:
        band, band_mode = [0.70, 1.35], 'fixed'

    return {
        'range_res': round(range_res, 4),
        'max_range': round(range_res * prof['bins'], 3),
        'range_res_verified': True,
        'v_max': round(v_max, 3),
        'v_res': round(v_res, 4),
        'chirps': prof['chirps'],
        'bins': prof['bins'],
        'board': board,
        'board_label': prof['label'],
        'angle_max': (args.angle_max if args.angle_max is not None else 45.0),
        'clutter': not args.no_clutter,
        'min_range': min_range,
        'cfar_scale': args.cfar if args.cfar is not None else 2.6,
        'min_snr': args.min_snr if args.min_snr is not None else 7.0,
        'angle_sign': args.angle_sign if args.angle_sign is not None else 1.0,
        'band': band,
        'band_mode': band_mode,
        'calib': os.path.basename(args.calib) if args.calib else None,
        'actions': not args.no_actions,
        'autostart': not args.no_autostart,
        'port': port,
        'port_auto': False,
        'hw_stop': False,
        'multi': True,
    }


# ---------------------------------------------------------------- 单板会话
class RadarSession:
    """一块板的完整采集单元：数据源 → DSP → 动作识别 → 广播。"""

    def __init__(self, port, args):
        self.port = port
        self.args = args
        self.board = probe_board(port) or DEFAULT_BOARD
        self.prof = BOARD_PROFILES[self.board]
        self.cfg = cfg_for(self.board, args, port)
        self.proc = PointCloudProcessor(
            cfar_scale=self.cfg['cfar_scale'],
            min_power=args.min_power,
            min_snr=self.cfg['min_snr'],
            remove_clutter=not args.no_clutter,
            angle_sign=self.cfg['angle_sign'],
            angle_max=self.cfg['angle_max'],
            min_range=self.cfg['min_range'],
            clutter_keep=(args.clutter_keep
                          if args.clutter_keep is not None else 1.0),
            **dsp_kwargs_for(self.board))
        self.recognizer = None if args.no_actions else ActionRecognizer(
            band=(tuple(self.cfg['band']) if self.cfg['band_mode'] == 'fixed'
                  else 'auto'))
        self.latest = {}
        self._last_log = 0.0
        self.ctrl = RadarController(
            self._make_source, self._on_frame,
            record_path=args.record,
            log=self._log,
            stall_timeout=args.stall_timeout,
            auto_restart=not args.no_stall_restart)

    # -- 数据源工厂（每次「开始」新建，避免残留状态污染）
    def _make_source(self):
        return SerialSource(
            self.port, self.args.baud, retry_delay=self.args.retry,
            on_event=self._log,
            auto_boot=not getattr(self.args, 'no_boot_2t4r', False),
            boot_seq=getattr(self.args, 'boot_seq', None))

    def _log(self, msg):
        print('[%s] %s' % (self.port, msg), flush=True)

    # -- 每帧回调
    def _on_frame(self, z0, z1, stats):
        try:
            pts, noise = self.proc.process(z0, z1)
            r0, _ = self.proc.rd_map(z0, z1)
            act = (self.recognizer.push(pts, time.time())
                   if self.recognizer is not None else None)
            # 自动选带锁定后写回本板 cfg
            if (self.recognizer is not None
                    and self.cfg['band_mode'] == 'auto'
                    and self.recognizer.band is not None):
                self.cfg['band'] = [round(self.recognizer.band[0], 2),
                                    round(self.recognizer.band[1], 2)]
            payload = srv.make_frame_payload(
                pts, r0, stats, self.proc, noise,
                control=self.ctrl.status(), actions=act, cfg=self.cfg)
            payload['board'] = self.board
            payload['port'] = self.port
            self.latest = {k: v for k, v in payload.items()
                           if k not in ('rd', 'pts')}
            srv.broadcast(payload, board=self.board)
            now = time.time()
            if (not self.args.quiet) and now - self._last_log > 3.0:
                self._last_log = now
                print('[%s] frames=%d fps=%.2f pts=%d skipped=%d bad=%d'
                      % (self.port, stats['frames'], stats['fps'], len(pts),
                         stats['skipped'], stats['bad']), flush=True)
        except Exception as exc:                  # 单帧异常绝不能终止采集
            print('[%s] 帧处理异常: %s: %s'
                  % (self.port, type(exc).__name__, exc), flush=True)

    # -- 生命周期
    def start(self):
        self.ctrl.start()

    def stop(self):
        try:
            self.ctrl.stop('multi-shutdown')
        except Exception:
            pass

    def status(self):
        st = dict(self.latest)
        st.setdefault('cfg', self.cfg)
        try:
            st['control'] = self.ctrl.status()
        except Exception:
            st['control'] = {}
        st['board'] = self.board
        st['port'] = self.port
        return st


# ---------------------------------------------------------------- 服务入口
def run(args):
    ports = [p.strip() for p in args.ports.split(',') if p.strip()]
    if not ports:
        raise SystemExit('--ports 不能为空')

    sessions = {}
    order = []
    for port in ports:
        try:
            s = RadarSession(port, args)
        except Exception as exc:
            print('[multi] %s 初始化失败: %s: %s'
                  % (port, type(exc).__name__, exc), flush=True)
            continue
        sessions[s.board] = s            # 板型唯一键；同型号多板会互相覆盖
        order.append(s.board)
        print('[multi] %s -> %s (%s)' % (port, s.board, s.prof['label']),
              flush=True)

    if not sessions:
        raise SystemExit('没有可用的雷达会话')

    # 注入全局：让 Handler 的 /status、/control 能按 board 路由
    srv.MULTI['sessions'] = sessions
    srv.MULTI['order'] = order
    srv.MULTI['enabled'] = True

    def control_handler(action, board=None):
        """统一控制入口：board 为空时作用于所有板。"""
        targets = ([sessions[board]] if board in sessions else
                   [sessions[b] for b in order])
        out = {'ok': True, 'action': action, 'results': {}}
        for s in targets:
            try:
                s.ctrl.control(action)
                out['results'][s.board] = {'ok': True}
            except Exception as exc:
                out['results'][s.board] = {'ok': False, 'msg': str(exc)}
                out['ok'] = False
        return out

    srv.set_multi_control(control_handler)

    httpd = srv.Server(('127.0.0.1', args.http_port), srv.Handler)
    print('\n多板模式:%s' % ('  '.join('%s(%s)' % (b, sessions[b].port)
                                        for b in order)), flush=True)
    print('页面: http://127.0.0.1:%d/dual' % args.http_port, flush=True)
    print('单板: http://127.0.0.1:%d/?board=%s' % (args.http_port, order[0]),
          flush=True)

    if not args.no_autostart:
        for b in order:
            sessions[b].start()

    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        while not srv.SHUTDOWN.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for b in order:
            sessions[b].stop()
        httpd.shutdown()
    return 0
