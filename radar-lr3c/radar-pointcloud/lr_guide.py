# -*- coding: utf-8 -*-
r"""左右移采集的浏览器引导界面。

为什么不直接看终端：人在雷达前看不到屏幕，终端文字和倒计时对不上，
而 opencv/HTML 大屏能同时给出「还要几秒」「现在该往哪走」「当前站在几度」。

用法（参数与 segment_capture.py 完全一致，只是多一个有界面的壳）：

    python radar-pointcloud\lr_guide.py --port COM3 --board 1t2r ^
        --distances 1.0 --actions left,right --reps 8 --stride 0.3 ^
        --auto --lead 20 --outdir data\lr3

启动后自动打开 http://127.0.0.1:8770 。

产出的 bin / labels.jsonl 与 segment_capture.py 逐字节同格式，
可以直接喂 tools\lane_separability.py、tools\lane_eval.py，
不需要为这个界面单独写评估。
"""
import os
import sys
import json
import time
import math
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
for _p in (HERE, os.path.join(ROOT, '.deepworks', 'tmp')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import segment_capture as sc  # noqa: E402

PORT = 8770
LOCK = threading.Lock()
WEB = {
    'phase': 'boot',      # boot/lead/prep/move/settle/rest/other/done
    'text': '正在启动，请稍候',
    't_start': 0.0,
    't_end': 0.0,
    'dur': 0.0,
    'action': '',
    'move_no': 0,
    'move_total': 0,
    'dist': 0.0,
    'say': '',
    'done': False,
    'err': '',
}


def _set(**kw):
    with LOCK:
        WEB.update(kw)


# --------------------------------------------------------------------------
# 把原采集器的三个阶段钩子挂到 WEB 上（不改 segment_capture.py 一行）
# --------------------------------------------------------------------------

def classify(text):
    if '开场' in text or '站定保持不动' in text:
        return 'lead'
    if '现在开始' in text or '横移' in text or '开始「' in text:
        return 'move'
    if '【停】' in text or '回到最佳中线，完全静止' in text:
        return 'settle'
    if '最后' in text:
        return 'rest'
    if '准备' in text or '测量你相对' in text:
        return 'prep'
    return 'other'


_orig_cue = sc.cue


def cue(secs, text, **kw):
    ph = classify(text)
    now = time.time()
    upd = dict(text=text, phase=ph, t_start=now, t_end=now + secs, dur=secs)
    if ph == 'move':
        upd['move_no'] = WEB.get('move_no', 0) + 1
        if '【左】' in text:
            upd['action'] = 'LEFT'
        elif '【右】' in text:
            upd['action'] = 'RIGHT'
    _set(**upd)
    return _orig_cue(secs, text, **kw)


sc.cue = cue

_orig_speak = getattr(sc, 'speak', None)
if _orig_speak is not None:
    def speak(text, *a, **kw):
        _set(say=text)
        return _orig_speak(text, *a, **kw)
    sc.speak = speak


_orig_main = sc.main


def run_capture(argv):
    try:
        _orig_main(argv)
    except SystemExit as e:
        _set(err='采集器提前退出（code=%s），检查串口/板型。' % e.code)
    except Exception as e:                      # noqa: BLE001
        _set(err='%s: %s' % (type(e).__name__, e))
    finally:
        _set(phase='done', text='采集结束', done=True, t_end=0.0)


# --------------------------------------------------------------------------
# 实时读数（比 az_stats 的 5 s 窗口快，界面才对得上动作）
# --------------------------------------------------------------------------

def live_read():
    buf = list(sc.AZ_BUF)
    if len(buf) < 3:
        return None
    t_now = buf[-1][0]
    xs = [q for q in buf if q[0] >= t_now - 1.2]
    if len(xs) < 3:
        xs = buf[-5:]
    az = sorted(q[1] for q in xs)[len(xs) // 2]
    r = sorted(q[2] for q in xs)[len(xs) // 2]
    return az, r


def snapshot():
    with LOCK:
        d = dict(WEB)
    now = time.time()
    d['now'] = now
    d['remain'] = max(0.0, d['t_end'] - now) if d['t_end'] else 0.0
    d['center'] = float(getattr(sc, 'CENTER_AZ', 0.0) or 0.0)
    d['frames'] = int(sc.STATE.get('frames', 0))
    d['fps'] = round(float(sc.STATE.get('fps', 0.0)), 1)
    rd = live_read()
    if rd:
        az, r = rd
        d['az'] = round(az, 1)
        d['r'] = round(r, 2)
        d['dev'] = round(az - d['center'], 1)
        d['px'] = round(r * math.sin(math.radians(az - d['center'])), 2)
    else:
        d['az'] = None
        d['r'] = None
        d['dev'] = None
        d['px'] = None
    return d


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def load_page():
    with open(os.path.join(HERE, 'guide.html'), encoding='utf-8') as f:
        return f.read()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith('/state'):
            body = json.dumps(snapshot(), ensure_ascii=False).encode('utf-8')
            ctype = 'application/json; charset=utf-8'
        else:
            body = load_page().encode('utf-8')
            ctype = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def argval(argv, name, default=None):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + '='):
            return a.split('=', 1)[1]
    return default


def main():
    argv = sys.argv[1:]

    reps = int(argval(argv, '--reps', '8'))
    acts = argval(argv, '--actions', 'left,right')
    n_act = len([a for a in acts.split(',') if a.strip()])
    dists = argval(argv, '--distances', '1.0')
    try:
        dist = float(dists.split(',')[0])
    except ValueError:
        dist = 0.0
    _set(move_total=reps * n_act, dist=dist)

    port = int(argval(argv, '--web-port', str(PORT)))
    url = 'http://127.0.0.1:%d/' % port

    # 界面自己的开关不能透传给 segment_capture（它的 argparse 不认识会 exit 2）
    clean, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a == '--no-open':
            i += 1
            continue
        if a == '--web-port':
            i += 2
            continue
        if a.startswith('--web-port='):
            i += 1
            continue
        clean.append(a)
        i += 1

    t = threading.Thread(target=run_capture, args=(clean,), daemon=True)
    t.start()

    if '--no-open' not in argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print('')
    print('  引导界面：%s' % url)
    print('  关闭这个窗口 = 结束引导（采集进程随之退出）')
    print('')

    httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)

    def watch():
        while not WEB.get('done'):
            time.sleep(0.5)
        time.sleep(6.0)          # 留几秒让人看到「完成」和最终读数
        httpd.shutdown()

    threading.Thread(target=watch, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
