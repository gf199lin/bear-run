# -*- coding: utf-8 -*-
"""
用游戏自带的离线回放通道，无头 Chrome 逐帧抓图，产出可合成视频的图片序列。

- HTTP 服务在脚本内的守护线程里起（8802，直接复用 serve_nocache.py 的逻辑），
  脚本退出即结束，不留后台进程。
- Chrome 用 subprocess 起，finally 里 kill；--mute-audio 保证回放全程不出声。
- 每张图记录真实墙钟耗时，写进 ffmpeg concat 清单的 duration，
  这样即使抓图跟不上帧率，视频时间轴仍与游戏内时间一致。

用法：py capture_replay.py gt_d00p70 px_d00p70 ...
"""
import base64
import io
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import http.server
import urllib.request

import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, '..', '..'))
ROOT = os.path.join(BASE, 'radar-runner')
CAP = os.path.join(HERE, 'cap')

PORT = 8802
CDP_PORT = 9333
PROFILE = os.path.join(HERE, 'chrome-cap-profile')
CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'

SPEED = int(float(os.environ.get('CAP_SPEED', '4')))       # 回放倍速
SECONDS = float(os.environ.get('CAP_SECONDS', '36'))       # 每个站位抓多久
TARGET_FPS = float(os.environ.get('CAP_FPS', '10'))        # 抓图目标帧率


# ---------------- 静态服务（daemon 线程） ----------------

class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def log_message(self, fmt, *a):
        pass


class ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_server():
    srv = ThreadedServer(('127.0.0.1', PORT), NoCacheHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ---------------- CDP ----------------

class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.n = 0

    def call(self, method, params=None):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get('id') == mid:
                if 'error' in msg:
                    raise RuntimeError('%s -> %s' % (method, msg['error']))
                return msg.get('result', {})

    def eval(self, expr):
        r = self.call('Runtime.evaluate', {
            'expression': expr, 'returnByValue': True, 'awaitPromise': False})
        return r.get('result', {}).get('value')

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def find_page_target():
    for _ in range(120):
        try:
            raw = urllib.request.urlopen('http://127.0.0.1:%d/json/list' % CDP_PORT, timeout=2).read()
            for t in json.loads(raw.decode('utf-8', 'replace')):
                if t.get('type') == 'page' and t.get('webSocketDebuggerUrl'):
                    return t['webSocketDebuggerUrl']
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError('没找到 Chrome 页面 target')


# ---------------- 单站位抓图 ----------------

def capture_one(cdp, series, seconds, outdir):
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    url = ('http://127.0.0.1:%d/index.html?input=replay'
           '&replay=./_radarreplay/radar-lr-move.js'
           '&replaymode=truth&series=%s&replayspeed=%d'
           '&autostart=1&hp=99&replayloop=0' % (PORT, series, SPEED))
    print('  -> %s' % url, flush=True)
    cdp.call('Page.navigate', {'url': url})

    # 等游戏真的跑起来
    t0 = time.time()
    state = None
    while time.time() - t0 < 40:
        try:
            state = cdp.eval('window.RRGame ? window.RRGame.state : null')
        except Exception:
            state = None
        if state in ('playing', 'dead'):
            break
        time.sleep(0.2)
    print('     state=%s (%.1fs)' % (state, time.time() - t0), flush=True)

    files = []
    t_start = time.time()
    prev = t_start
    i = 0
    while True:
        now = time.time() - t_start
        if now >= seconds:
            break
        r = cdp.call('Page.captureScreenshot', {
            'format': 'jpeg', 'quality': int(os.environ.get('CAP_Q', '75')),
            'optimizeForSpeed': True})
        data = base64.b64decode(r['data'])
        i += 1
        name = 'f_%05d.jpg' % i
        with io.open(os.path.join(outdir, name), 'wb') as fh:
            fh.write(data)
        t = time.time()
        files.append((name, max(1.0 / 60.0, t - prev)))
        prev = t
        # 简单节流到目标帧率
        slack = (1.0 / TARGET_FPS) - (time.time() - prev)
        if slack > 0:
            time.sleep(slack)

    stats = None
    try:
        stats = cdp.eval('(document.getElementById("replay-stat")||{}).textContent')
    except Exception:
        pass

    # ffmpeg concat 清单：每帧带真实时长
    lst = os.path.join(outdir, 'frames.txt')
    with io.open(lst, 'w', encoding='utf-8') as fh:
        for name, dur in files:
            fh.write("file '%s'\n" % name)
            fh.write('duration %.4f\n' % dur)
        fh.write("file '%s'\n" % files[-1][0])

    el = time.time() - t_start
    print('     抓到 %d 帧，用时 %.1fs（%.2f fps）' % (len(files), el, len(files) / max(el, 1e-6)))
    if stats:
        print('     replay-stat: %s' % stats.replace('"', '').strip())
    return {'series': series, 'dir': outdir, 'frames': len(files),
            'seconds': round(time.time() - t_start, 2), 'stat': stats}


def main(argv):
    series_list = argv[1:]
    if not series_list:
        print('用法: py capture_replay.py <series> [<series> ...]')
        return 2

    srv = start_server()
    print('static server: http://127.0.0.1:%d  ->  %s' % (PORT, ROOT), flush=True)

    args = [
        CHROME,
        '--headless=new',
        '--remote-debugging-port=%d' % CDP_PORT,
        '--user-data-dir=%s' % PROFILE,
        '--window-size=%s' % os.environ.get('CAP_SIZE', '1280,720'),
        '--hide-scrollbars',
        '--mute-audio',                 # 关键：回放全程不出声
        '--autoplay-policy=no-user-gesture-required',
        '--disable-extensions', '--no-first-run', '--no-default-browser-check',
    ]
    gl = os.environ.get('CAP_GL', 'swiftshader')
    if gl == 'gpu':
        # 走真实 GPU（D3D11/ANGLE），截图像素读回快很多
        args += ['--use-angle=d3d11', '--ignore-gpu-blocklist', '--enable-gpu-rasterization']
    else:
        args += ['--use-angle=swiftshader', '--enable-unsafe-swiftshader']
    args += ['--disable-features=CalculateNativeWinOcclusion', 'about:blank']
    chrome = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    results = []
    cdp = None
    try:
        ws = find_page_target()
        cdp = CDP(ws)
        cdp.call('Page.enable')
        cdp.call('Runtime.enable')
        for s in series_list:
            print('== %s' % s, flush=True)
            results.append(capture_one(cdp, s, SECONDS, os.path.join(CAP, s)))
    finally:
        if cdp:
            cdp.close()
        try:
            chrome.kill()
        except Exception:
            pass
        try:
            srv.shutdown()
        except Exception:
            pass

    print()
    print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
