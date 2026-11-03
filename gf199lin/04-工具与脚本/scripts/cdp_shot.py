"""一次性自检：用 CDP 驱动无头 Chrome 真正玩一局（跑到第 2 区），
在横幅可见的瞬间截图，同时打印游戏状态，用来判断"醒目不遮挡"。"""

import base64
import json
import os
import subprocess
import time
import urllib.request

import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = r"<TEMP>\cdp-profile"
PORT = 9333
URL = 'http://127.0.0.1:8802/index.html'


def wait_devtools(timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/json/list' % PORT, timeout=2) as r:
                items = json.loads(r.read().decode('utf-8'))
            for it in items:
                if it.get('type') == 'page' and it.get('webSocketDebuggerUrl'):
                    return it['webSocketDebuggerUrl']
        except Exception:
            pass
        time.sleep(0.4)
    return None


PROBE = (
    "(function(){"
    "var g=window.RRGame;"
    "var e=document.getElementById('region-banner');"
    "var s=e?getComputedStyle(e):null;"
    "var nm=document.querySelector('.region-banner-name');"
    "return [g?g.state:'no-game',"
    "g?g.obstacles.stageIndex:-1,"
    "g?Math.round(g.distance):-1,"
    "s?s.display:'?',"
    "s?Number(s.opacity).toFixed(2):'?',"
    "(document.getElementById('stat-stage')||{}).textContent,"
    "nm?nm.textContent:''].join('|');})()"
)


def main():
    proc = subprocess.Popen(
        [CHROME, '--headless=new', '--remote-debugging-port=%d' % PORT,
         '--window-size=1280,720', '--hide-scrollbars', '--no-first-run',
         '--no-default-browser-check', '--disable-extensions',
         '--enable-unsafe-swiftshader', '--remote-allow-origins=*',
         '--user-data-dir=' + PROFILE, URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = wait_devtools()
        if not ws_url:
            print('FAIL: devtools not reachable')
            return
        ws = websocket.create_connection(ws_url, timeout=30)
        counter = [0]

        def cmd(method, params=None):
            counter[0] += 1
            mid = counter[0]
            ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get('id') == mid:
                    return msg

        def ev(expr):
            r = cmd('Runtime.evaluate', {'expression': expr, 'returnByValue': True})
            return (r.get('result', {}).get('result', {}) or {}).get('value')

        def shot(name):
            data = cmd('Page.captureScreenshot', {'format': 'png'})['result']['data']
            path = os.path.join(HERE, name)
            with open(path, 'wb') as f:
                f.write(base64.b64decode(data))
            print('shot ->', path, os.path.getsize(path), 'bytes')

        cmd('Page.enable')
        cmd('Runtime.enable')
        time.sleep(3.0)

        print('boot :', ev(PROBE))

        # 让第 2 区提前到 20m 出现，几秒内就能看到横幅
        print('setup:', ev("(function(){RR.CFG.DIFFICULTY[1].at=20;"
                           "document.getElementById('btn-start').click();return 'clicked';})()"))

        seen = False
        log = []
        t0 = time.time()
        while time.time() - t0 < 25:
            st = str(ev(PROBE))
            log.append(st)
            parts = st.split('|')
            if len(parts) >= 5 and parts[3] != 'none':
                try:
                    if float(parts[4]) > 0.9:
                        seen = True
                        break
                except ValueError:
                    pass
            time.sleep(0.2)

        for line in log[-14:]:
            print('poll :', line)
        print('banner visible:', seen)

        if seen:
            shot('shot-banner-live.png')
            time.sleep(1.0)
            print('hold :', ev(PROBE))
            shot('shot-banner-live2.png')

        # 等横幅淡出，再截一张干净的跑动画面做对比
        t0 = time.time()
        while time.time() - t0 < 12:
            st = str(ev(PROBE))
            parts = st.split('|')
            if len(parts) >= 5 and (parts[3] == 'none' or parts[4] == '0.00'):
                break
            time.sleep(0.4)
        time.sleep(0.6)
        print('after:', ev(PROBE))
        shot('shot-run-nobanner.png')
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == '__main__':
    main()
