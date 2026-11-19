# -*- coding: utf-8 -*-
"""对准页：实时显示左右板角度，直接告诉你该往左还是往右挪。

目标位置来自 radar-pointcloud/center.json（1T2R 中线 +9°）。
点云处理参数与 radar_live_ws.py 默认值一致，读数和游戏里看到的一样。

用法：
    py -3 tools\\aim_web.py --port COM5 --web-port 8774
然后浏览器打开 http://127.0.0.1:8774/
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'bear-run-new', 'radar-service'))
sys.path.insert(0, HERE)

import vguide_1t2r as V                      # noqa: E402
from radar_dsp import PointCloudProcessor    # noqa: E402
import dual_guide as DG                      # noqa: E402

TOL = 3.0                     # 绿区半宽（度）
LOCK = threading.Lock()
STATE = {
    'az': None, 'n': 0, 'fps': 0.0, 'mid': None,
    'target': 30.0, 'hint': '等帧...', 'trend': '—', 'ok': False, 'n_mid': 0, 'zero_pct': 0,
}


def _track(cand, trk):
    """单目标跟踪：优先近者 + 角/距跳变保护。与生产链路同构。"""
    if not cand:
        return None
    if not trk['ok']:
        p = max(cand, key=lambda q: q['p'])
        trk.update(ok=True, r=p['r'], deg=p['deg'], miss=0)
        return trk['deg']
    p = max(cand, key=lambda q: q['p'] / (1.0 + 3.0 * (abs(q['r'] - trk['r']) + 0.02 * abs(q['deg'] - trk['deg']))))
    if abs(p['deg'] - trk['deg']) > 12.0 and abs(p['r'] - trk['r']) > 0.3:
        trk['miss'] += 1
        if trk['miss'] < 3:
            return trk['deg']
        trk.update(r=p['r'], deg=p['deg'], miss=0)
        return trk['deg']
    trk.update(r=0.65 * trk['r'] + 0.35 * p['r'],
               deg=0.65 * trk['deg'] + 0.35 * p['deg'], miss=0)
    return trk['deg']


def worker(port, target, sim=False):
    if sim:
        slot = DG.SimSlot('A', 'lr')
    else:
        slot = DG.open_slot('A', port)
    slot.band = (1, min(3, V.BINS))
    proc = PointCloudProcessor(cfar_scale=2.6, min_snr=7.0,
                               remove_clutter=True, clutter_keep=1.0)
    trk = {'ok': False, 'r': 1.0, 'deg': 0.0, 'miss': 0}
    hist = deque(maxlen=21)
    nhist = deque(maxlen=21)
    marks = deque(maxlen=40)
    while True:
        try:
            slot.poll(time.time())
        except Exception:
            time.sleep(0.2)
            continue
        fps = 0.0
        if len(marks) >= 8:
            span = marks[-1] - marks[0]
            if span > 0.5:
                fps = (len(marks) - 1) / span
        while slot.zs:
            z = slot.zs.pop(0)
            marks.append(time.time())
            try:
                pts, _ = proc.process(z[0], z[1])
            except Exception:
                continue
            near = [p for p in pts if 0.8 <= p['r'] <= 1.4]
            cand = near or [p for p in pts if 0.5 <= p['r'] <= 2.5]
            az = _track(cand, trk) if cand else None
            nhist.append(len(cand))
            with LOCK:
                STATE['n'] = len(cand)
                STATE['n_mid'] = sorted(nhist)[len(nhist) // 2]
                zero = sum(1 for v in nhist if v == 0)
                STATE['zero_pct'] = round(100.0 * zero / len(nhist), 0)
                STATE['fps'] = round(fps, 1)
                STATE['target'] = target
                if az is None:
                    if not hist:
                        STATE['hint'] = '看不到你，往中间站一点'
                        STATE['ok'] = False
                    continue
                hist.append(az)
                srt = sorted(hist)
                mid = srt[len(srt) // 2]
                STATE['az'] = round(az, 1)
                STATE['mid'] = round(mid, 1)
                STATE['trend'] = ('往左' if az < mid - 2.0
                                  else '往右' if az > mid + 2.0 else '静止')
                delta = mid - target
                if delta > TOL:
                    STATE['hint'] = '往你的左手边走 %.1f°' % delta
                    STATE['ok'] = False
                elif delta < -TOL:
                    STATE['hint'] = '往你的右手边走 %.1f°' % (-delta)
                    STATE['ok'] = False
                else:
                    STATE['hint'] = '就这里，贴胶带'
                    STATE['ok'] = True
        time.sleep(0.004)


PAGE = '''<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>对准 - 找位置</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
     background:#0d1117;color:#e6edf3;font-family:system-ui,"Microsoft YaHei",sans-serif}
.box{text-align:center;width:100%;max-width:900px;padding:24px}
.label{font-size:22px;color:#8b949e;letter-spacing:.1em}
.az{font-size:clamp(72px,16vw,150px);font-weight:800;line-height:1;margin:8px 0 4px;
    font-variant-numeric:tabular-nums}
.hint{font-size:clamp(28px,5vw,52px);font-weight:700;margin:24px auto 0;padding:18px 36px;
      border-radius:16px;background:#30363d;color:#e6edf3;display:inline-block;min-width:60%}
.hint.ok{background:#1a7f37;color:#fff}
.hint.off{background:#8b2c2c;color:#fff}
.arrow{font-size:24px;color:#8b949e;margin-bottom:6px}
.meta{margin-top:26px;font-size:20px;color:#8b949e;font-variant-numeric:tabular-nums}
.meta b{color:#e6edf3}
</style>
<div class="box">
  <div class="label">水平板实时角度（左右）</div>
  <div class="az" id="az">--</div>
  <div class="arrow" id="arrow">→ 稳定</div>
  <div class="hint" id="hint">等帧...</div>
  <div class="meta" id="meta">正在连接雷达...</div>
</div>
<script>
const TOL = 3.0;
async function tick(){
  try{
    const r = await fetch('/data', {cache:'no-store'});
    const d = await r.json();
    const az = document.getElementById('az');
    const hint = document.getElementById('hint');
    const meta = document.getElementById('meta');
    az.textContent = (d.mid === null) ? '--' : ((d.mid > 0 ? '+' : '') + d.mid.toFixed(1) + '°');
    az.style.color = d.ok ? '#3fb950' : (d.mid === null ? '#e6edf3' : '#e6a23c');
    const arrow = document.getElementById('arrow');
    if (d.az !== null && d.mid !== null) {
      const diff = d.az - d.mid;
      arrow.textContent = diff > 2 ? '↑ 角度在变大' : (diff < -2 ? '↓ 角度在变小' : '→ 稳定');
      arrow.style.color = diff > 2 ? '#3fb950' : (diff < -2 ? '#58a6ff' : '#8b949e');
    }
    hint.textContent = d.hint;
    hint.className = 'hint' + (d.ok ? ' ok' : '');
    const t = d.target;
    meta.innerHTML = '点数 <b>' + d.n + '</b>（中位 ' + (d.n_mid||0) + '，空帧 ' + (d.zero_pct||0) + '%） ｜ 帧率 <b>' + d.fps.toFixed(1) +
      '</b> 帧/秒 ｜ 目标 <b>' + (t > 0 ? '+' : '') + t + '°</b> ｜ 最近动作 <b>' + d.trend + '</b>';
  }catch(e){
    document.getElementById('meta').textContent = '连不上服务（页面先别关，稍等重试）';
  }
}
setInterval(tick, 200);
tick();
</script>
</html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith('/data'):
            with LOCK:
                body = json.dumps(STATE, ensure_ascii=False)
            data = body.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        data = PAGE.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='COM5', help='左右板串口')
    ap.add_argument('--web-port', type=int, default=8774)
    ap.add_argument('--target', type=float, default=30.0, help='目标角度（默认 +30：斜向强区，正前方最弱）')
    ap.add_argument('--sim', action='store_true', help='合成数据，不碰硬件')
    a = ap.parse_args()

    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='gbk', errors='replace')
        except Exception:
            pass

    threading.Thread(target=worker, args=(a.port, a.target, a.sim), daemon=True).start()
    srv = ThreadingHTTPServer(('127.0.0.1', a.web_port), Handler)
    print('对准页面：http://127.0.0.1:%d/' % a.web_port)
    print('串口 %s，目标 %+.1f 度' % (a.port, a.target))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
