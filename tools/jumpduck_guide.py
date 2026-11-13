# -*- coding: utf-8 -*-
r"""跳 / 蹲采集的浏览器引导界面。

照 `radar-pointcloud/lr_guide.py` 的标准做：
本地页面 + 环形倒计时 + 动作大字 + 实时读数 + 进度 N/M，采完自动关闭。
命令行只管参数，采集在后台线程里跑，人不需要看终端。

和 lr_guide 的区别
------------------
- 动作是 站好 / 跳 / 蹲，没有方位角可看；底部换成 **16 个距离 bin 的实时能量柱**。
- 默认用 2T4R 的 130 ms 配置（7.6 Hz、32 chirp 输出、16 bin），启动时自动下发；
  这样不用记环境变量，也不会被 SerialSource 的自动 boot 冲掉。

用法
----
    python tools\jumpduck_guide.py --port COM4 --rounds 2
    python tools\jumpduck_guide.py --port COM4 --profile base      # 520 ms 对照
    python tools\jumpduck_guide.py --no-boot --no-open             # 不碰设备 / 不开浏览器

产出（与 tools\jumpduck_capture.py 同格式，可直接喂 train_jumpduck.py）
----
    data/jumpduck_guide/<时间戳>/
        frames.npz   z=(N,4,chirps,bins), t=(N,), label=(N,)
        labels.json  段边界 + 真值 + 采集参数
        level.csv    每帧峰值 bin / 距离，便于肉眼检查
"""
import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))

PORT = 8771
LOCK = threading.Lock()

WEB = {
    'phase': 'boot',
    'text': '正在启动，请稍候',
    'seg': '',
    'seg_no': 0,
    'seg_total': 0,
    'round': 0,
    'rounds': 0,
    'remain': 0.0,
    'dur': 0.0,
    'frames': 0,
    'fps': 0.0,
    'peak_bin': -1,
    'range_m': 0.0,
    'energy': [],
    'profile': '',
    'bin_width': 0.0,
    'done': False,
    'err': '',
}

# 每轮 5 段：站好 → 跳 → 站好 → 蹲 → 站好
# 段名必须与 train_jumpduck.py 一致（它只认 base1/jump/base2/duck/base3），
# 否则造样本时找不到基线、整批数据作废。
SEGMENTS = [
    ('base1', '站好，别动', 4.0),
    ('jump', '开始跳：连跳 3 次，动作放慢', 8.0),
    ('base2', '停，站好别动', 4.0),
    ('duck', '开始蹲：连续蹲 3 次，动作放慢', 8.0),
    ('base3', '停，站好别动', 4.0),
]

PHASE_WORD = {
    'boot': '启动中',
    'prep': '准备',
    'base': '站好',
    'jump': '跳',
    'duck': '蹲',
    'rest': '休息',
    'done': '完成',
}

PROFILES = {
    # bins / chirps 是「输出」口径：130 ms 配置只出偶数 chirp（32 个），16 bin。
    '130ms': dict(bins=16, chirps=32, stride=2, frame_us=130440,
                  seq='boot_seq_2t4r_130ms.json'),
    'base': dict(bins=32, chirps=64, stride=1, frame_us=520840,
                 seq='boot_seq_2t4r.json'),
}

# apply_profile 解析出的当前口径，供 DirectReader 构造解析器用
CURRENT = dict(bins=32, chirps=64, stride=1)


def _protocol_module():
    """按绝对路径加载 radar-protocol_dev113p，绝不被 sys.path 顺序影响。

    bear-run-new/radar-service 里有一份旧版同名文件（硬编码 136 B / 0x1F），
    只要它在 sys.path 前面就会把 16 bin 帧全部判坏。
    """
    import importlib.util
    name = 'radar_protocol_dev113p'
    want = os.path.join(ROOT, 'radar-pointcloud', name + '.py')
    mod = sys.modules.get(name)
    if mod is not None and getattr(mod, '__file__', None):
        if os.path.normcase(os.path.abspath(mod.__file__)) == os.path.normcase(want):
            return mod
    spec = importlib.util.spec_from_file_location(name, want)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_protocol(board):
    if board == '2t4r':
        mod = _protocol_module()
        return (mod.StreamParser(),
                mod.FrameAssembler(chirps=CURRENT['chirps'],
                                   bins=CURRENT['bins'],
                                   stride=CURRENT['stride']))
    from radar_protocol import StreamParser, FrameAssembler
    return StreamParser(), FrameAssembler()


def _set(**kw):
    with LOCK:
        WEB.update(kw)


def say(text):
    try:
        import jumpduck_capture as jc
        jc.speak(text)
    except Exception:
        pass


def beep(ok=True):
    try:
        import jumpduck_capture as jc
        jc.beep(ok)
    except Exception:
        pass


def tick():
    try:
        import jumpduck_capture as jc
        jc.short_tick()
    except Exception:
        pass


# --------------------------------------------------------------------------
# 配置：先设环境变量，再下发序列（顺序不能反，radar_source 在 import 时读 profile）
# --------------------------------------------------------------------------

def apply_profile(name, port, do_boot=True, board='2t4r'):
    """设 env + 打开串口 + （可选）下发配置，**返回保持打开的串口**。

    不能下发完就 close：FT232 的 DTR 一掉，2T4R 会退到不完整出流
    （实测 70 → 45 kB/s），之后组帧永远凑不齐。配置与采集必须同一个句柄。
    """
    p = PROFILES[name]
    os.environ['RADAR_2T4R_BINS'] = str(p['bins'])
    os.environ['RADAR_2T4R_CHIRPS'] = str(p['chirps'])
    os.environ['RADAR_2T4R_STRIDE'] = str(p['stride'])
    os.environ['RADAR_2T4R_FRAME_US'] = str(p['frame_us'])
    _set(profile=name, bin_width=round(2.82 / p['bins'], 3))

    # BOARD_PROFILES 在 import radar_source 时就按当时的 env 固化了，
    # 只改 env 不够（会拿到上一次/默认的 32 bin）。这里直接改写 profile。
    import radar_source as RS
    bp = RS.BOARD_PROFILES.get(board)
    if bp and board == '2t4r':
        bp['bins'] = p['bins']
        bp['chirps'] = p['chirps']
        bp['stride'] = p['stride']
    if board == '2t4r':
        CURRENT.update(bins=p['bins'], chirps=p['chirps'], stride=p['stride'])

    import serial
    baud = {'2t4r': 1500000, '1t2r': 921600}.get(board, 1500000)
    ser = serial.Serial(port, baud, timeout=0.02)
    for attr in ('dtr', 'rts'):
        try:
            setattr(ser, attr, True)
        except Exception:
            pass
    time.sleep(0.2)
    ser.reset_input_buffer()

    if not do_boot or board == '1t2r':
        return ser, '未下发（使用设备当前配置）'

    seq = os.path.join(ROOT, '.deepworks', 'tmp', p['seq'])
    if not os.path.isfile(seq):
        return ser, '缺少序列 %s，跳过下发' % os.path.basename(seq)

    # 用文件路径加载，**不能** sys.path.insert(bear-run-new/radar-service)：
    # 那个目录里有旧版 radar_protocol_dev113p.py（硬编码 136 B / 0x1F 同步字节），
    # 一旦抢先 import，16 bin 的帧会 100% 判定为坏包（实测 bad=3029/3029）。
    import importlib.util
    p_boot = os.path.join(ROOT, 'bear-run-new', 'radar-service',
                          'radar_boot_2t4r.py')
    spec = importlib.util.spec_from_file_location('radar_boot_2t4r', p_boot)
    B = importlib.util.module_from_spec(spec)
    # radar_boot_2t4r 会把自身目录插到 sys.path[0]（第 51 行），
    # 之后 radar_protocol_dev113p 又会被旧版抢走，所以加载完立刻还原。
    _snap = list(sys.path)
    try:
        spec.loader.exec_module(B)
        obj, frames = B.load_seq(seq)
        res = B.send_boot(ser, frames, gap=0.03, ack_timeout=0.12)
    finally:
        sys.path[:] = _snap
    time.sleep(0.3)
    return ser, '%s：下发 %d 帧 / 回包 %d / 跳过 %d' % (
        os.path.basename(seq), res['sent'], res['acks'], res['skipped'])


# --------------------------------------------------------------------------
# 读帧：直接用已打开的串口，不经过 SerialSource（它会关串口重开）
# --------------------------------------------------------------------------

class DirectReader(object):
    def __init__(self, ser, board='2t4r'):
        self.ser = ser
        self.board = board
        self.parser, self.asm = _make_protocol(board)
        self.chirps = int(getattr(self.asm, 'chirps', 0) or 0)
        self.bad = 0

    def poll(self, timeout=0.05):
        out = []
        try:
            n = self.ser.in_waiting
        except Exception:
            return out
        if not n:
            time.sleep(0.005)
            return out
        chunk = self.ser.read(n)
        for rf in self.parser.feed(chunk):
            z = self.asm.push(rf)
            if z is None:
                continue
            if isinstance(z, (tuple, list)):
                arrs = [np.asarray(v) for v in z if v is not None]
                if not arrs:
                    continue
                try:
                    z = np.stack(arrs, axis=0)
                except ValueError:
                    continue
            z = np.asarray(z)
            if z.ndim == 3:
                out.append(z)
        self.bad = getattr(self.parser, 'bad_packets', 0)
        return out

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 后台采集
# --------------------------------------------------------------------------

class Runner(object):
    def __init__(self, args, ser=None):
        self.args = args
        self.ser = ser
        self.zs, self.ts, self.labels = [], [], []
        self.marks = []
        self.t0 = time.time()
        self.reader = None
        self.out_dir = None
        self.err = ''

    # ---- 单帧轮询 ----
    def _pump(self, seconds, label):
        reader = self.reader
        end = time.time() + seconds
        while time.time() < end:
            rec = time.time() - self.t0
            for z in reader.poll(0.05):
                self.zs.append(z.astype('complex64'))
                self.ts.append(rec)
                self.labels.append(label)
            self._publish(rec, max(0.0, end - time.time()))
        return len(self.zs)

    def _publish(self, rec, remain):
        n = len(self.zs)
        fps = 0.0
        # 同一轮 poll 可能一次吐多帧、时间戳相同；用最近 15 帧的跨度算会虚高，
        # 所以取全局平均（跨度不足 1 s 时不出数）。
        if n >= 8:
            span = self.ts[-1] - self.ts[0]
            if span > 1.0:
                fps = (n - 1) / span
        upd = dict(frames=n, fps=round(fps, 1), remain=round(remain, 1))
        if self.zs:
            import numpy as np
            z = self.zs[-1]
            p = np.abs(z).mean(axis=0).mean(axis=0)
            k = int(np.argmax(p))
            mx = float(p[k]) or 1.0
            upd['peak_bin'] = k
            upd['range_m'] = round((k + 0.5) * WEB['bin_width'], 2)
            upd['energy'] = [int(round(100.0 * v / mx)) for v in p]
        _set(**upd)

    # ---- 主流程 ----
    def run(self):
        a = self.args
        try:
            import jumpduck_capture as jc

            _set(phase='prep', text='正在连接雷达…', remain=0, dur=0)
            self.reader = DirectReader(self.ser, self.args.board)
            if self.reader.chirps:
                _set(chirps=self.reader.chirps)

            stamp = time.strftime('%Y%m%d-%H%M%S')
            self.out_dir = os.path.join(a.out, stamp)
            os.makedirs(self.out_dir, exist_ok=True)

            prep = max(3.0, float(getattr(a, 'prep', 6.0)))
            _set(phase='prep', text='请站到雷达正前方 1.2 m，站好别动',
                 remain=prep, dur=prep, round=0, rounds=a.rounds,
                 seg_total=a.rounds * len(SEGMENTS), seg_no=0)
            say('跳蹲采集即将开始。请站到雷达正前方一点二米处，站好不要动。')
            self._pump(prep, 'idle')

            seg_total = a.rounds * len(SEGMENTS)
            no = 0
            for r in range(1, a.rounds + 1):
                for seg, hint, default_sec in SEGMENTS:
                    no += 1
                    is_base = seg.startswith('base')
                    sec = a.sec_base if is_base else a.sec_act
                    if sec <= 0:
                        sec = default_sec
                    _set(phase=('base' if is_base else seg), text=hint, seg=seg,
                         seg_no=no, seg_total=seg_total, round=r, rounds=a.rounds,
                         remain=sec + 1.6, dur=sec + 1.6)
                    say(hint)
                    self._pump(1.6, 'idle')            # 播报/准备，不计入真值
                    beep(is_base)
                    time.sleep(0.35)

                    f0 = len(self.zs)
                    t_seg = time.time() - self.t0
                    shown = None
                    end = time.time() + sec
                    while time.time() < end:
                        remain = end - time.time()
                        rec = time.time() - self.t0
                        for z in self.reader.poll(0.05):
                            self.zs.append(z.astype('complex64'))
                            self.ts.append(rec)
                            self.labels.append(seg)
                        self._publish(rec, max(0.0, remain))
                        if not is_base and remain <= 3.2:
                            n = int(remain) + 1
                            if n != shown and n >= 1:
                                shown = n
                                tick()
                    beep(not is_base)
                    f1 = len(self.zs)
                    self.marks.append(dict(
                        round=r, seg=seg, frame_start=f0,
                        frame_end=max(f0, f1 - 1), frames=f1 - f0,
                        t_start=round(t_seg, 2),
                        t_end=round(time.time() - self.t0, 2)))
                    if f1 == f0:
                        _set(err='这一段没收到帧，检查串口是否被其它程序占用')
        except Exception as exc:                   # noqa: BLE001
            self.err = '%s: %s' % (type(exc).__name__, exc)
            _set(err=self.err)
        finally:
            self._save()
            _set(phase='done', text='采集结束', remain=0, dur=0, done=True)

    # ---- 保存 ----
    def _save(self):
        import numpy as np
        if not self.zs:
            _set(err=(self.err or '没有采到任何帧') + '（未保存）')
            return
        a = self.args
        z = np.stack(self.zs, axis=0)
        np.savez_compressed(os.path.join(self.out_dir, 'frames.npz'),
                            z=z, t=np.asarray(self.ts, float),
                            label=np.asarray(self.labels))
        prof = PROFILES[a.profile]
        try:
            from radar_source import frame_period
            fp = frame_period('2t4r')
        except Exception:
            fp = prof['frame_us'] / 1e6
        meta = dict(
            created=time.strftime('%Y-%m-%d %H:%M:%S'),
            port=a.port, board='2t4r', profile=a.profile,
            chirps=prof['chirps'], bins=prof['bins'], stride=prof['stride'],
            frame_period=fp,
            rounds=a.rounds, sec_base=a.sec_base, sec_act=a.sec_act,
            # train_jumpduck 用 band 算俯仰相位差；16 bin 时 21 越界，必须按 bins 截。
            band=[6, min(21, int(prof['bins']))],
            frames=int(z.shape[0]), shape=list(z.shape[1:]),
            segments=self.marks,
            note='真值由段边界给出；frame_start/frame_end 是 frames.npz 的行号；'
                 'idle = 播报/准备段，不属于动作真值',
        )
        with open(os.path.join(self.out_dir, 'labels.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        rows = ['i,t,label,peak_bin,range_m']
        for i, fr in enumerate(self.zs):
            p = np.abs(fr).mean(axis=0).mean(axis=0)
            k = int(np.argmax(p))
            rows.append('%d,%.3f,%s,%d,%.2f'
                        % (i, self.ts[i], self.labels[i], k,
                           (k + 0.5) * WEB['bin_width']))
        with open(os.path.join(self.out_dir, 'level.csv'), 'w',
                  encoding='utf-8') as f:
            f.write('\n'.join(rows))

        print('\n已保存 %s（%d 帧 %s）'
              % (self.out_dir, z.shape[0], ' × '.join(str(v) for v in z.shape[1:])),
              flush=True)
        print('下一步：python tools\\train_jumpduck.py --data %s'
              % self.out_dir, flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def snapshot():
    with LOCK:
        d = dict(WEB)
    d['now'] = time.time()
    return d


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith('/state'):
            body = json.dumps(snapshot(), ensure_ascii=False).encode('utf-8')
            ctype = 'application/json; charset=utf-8'
        else:
            with open(os.path.join(HERE, 'jumpduck_guide.html'),
                      encoding='utf-8') as f:
                body = f.read().encode('utf-8')
            ctype = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser(description='跳/蹲采集浏览器引导')
    ap.add_argument('--port', default='COM4')
    ap.add_argument('--board', default='2t4r')
    ap.add_argument('--profile', choices=sorted(PROFILES), default='130ms')
    ap.add_argument('--rounds', type=int, default=2)
    ap.add_argument('--sec-base', type=float, default=4.0)
    ap.add_argument('--sec-act', type=float, default=8.0)
    ap.add_argument('--out', default=os.path.join(ROOT, 'data', 'jumpduck_guide'))
    ap.add_argument('--web-port', type=int, default=PORT)
    ap.add_argument('--no-boot', action='store_true', help='不下发配置，用设备当前状态')
    ap.add_argument('--no-open', action='store_true', help='不自动打开浏览器')
    ap.add_argument('--no-speak', action='store_true')
    ap.add_argument('--prep', type=float, default=6.0,
                    help='开始前«站好别动»的预备秒数，默认 6')
    args = ap.parse_args()

    if args.no_speak:
        os.environ['JUMPDUCK_NO_SPEAK'] = '1'

    ser = None
    try:
        ser, boot_msg = apply_profile(args.profile, args.port,
                                      not args.no_boot, args.board)
    except Exception as exc:
        boot_msg = '打开串口/下发失败：%s: %s' % (type(exc).__name__, exc)
        print('\n  %s\n' % boot_msg)
        print('  检查：USB 是否插紧 / 是否被其它程序占用（pythonw.exe、radar_serve）')
        return 3

    if args.no_speak:
        try:
            import jumpduck_capture as jc
            jc.NO_SPEAK = True
        except Exception:
            pass

    print('')
    print('  配置：%s' % boot_msg)
    print('  引导界面：http://127.0.0.1:%d/' % args.web_port)
    print('  关闭这个窗口 = 结束引导')
    print('')

    runner = Runner(args, ser=ser)
    threading.Thread(target=runner.run, daemon=True).start()

    url = 'http://127.0.0.1:%d/' % args.web_port
    if not args.no_open:
        def _open():
            time.sleep(1.2)
            try:
                os.startfile(url)
            except Exception:
                webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    httpd = ThreadingHTTPServer(('127.0.0.1', args.web_port), Handler)

    def watch():
        while not WEB.get('done'):
            time.sleep(0.5)
        time.sleep(6.0)
        httpd.shutdown()

    threading.Thread(target=watch, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    if runner.reader is not None:
        try:
            runner.reader.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
