# -*- coding: utf-8 -*-
"""开局个人校准：量出这个人自己的零点、左幅、右幅，生成个人满量程。

流程（约 13 秒）：
    1) 站定别动 3 秒        -> zero
    2) 往左手边走一大步      -> left_amp
    3) 往右手边走一大步      -> right_amp
    4) 算 scale_deg，写 radar-pointcloud/personal-cal.json

为什么需要：radar_live_ws 的 x = (az - zero) / scale_deg，scale_deg 固定 20
只是某一个人的经验值。人的横移幅度差很多，固定 20 会让动作小的人卡在门限下。
按个人半幅归一化后，任何人走一半都能到 x≈1，0.22 的门限对所有人都一样好使。

用法：
    py -3 tools\\calibrate_person.py --port COM5 --web-port 8779
浏览器打开 http://127.0.0.1:8779/
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

CAL_PATH = os.path.join(ROOT, 'radar-pointcloud', 'personal-cal.json')

# 阶段：(名字, 时长秒, 提示)
PHASES = [
    ('still', 3.0, '站定，别动'),
    ('left',  5.0, '往你的左手边走一大步，然后站住'),
    ('right', 5.0, '往你的右手边走一大步，然后站住'),
]
TAIL = 1.5          # 每段取最后 1.5 秒做中位
MIN_AMP = 5.0       # 单侧幅度低于此值视为信号太差
SPACE_WARN = 10.0   # 单侧低于此值提示站位没空间

LOCK = threading.Lock()
STATE = {
    'phase': 'idle', 'phase_name': '', 'remain': 0.0, 'msg': '准备中...',
    'az': None, 'n': 0, 'fps': 0.0,
    'zero': None, 'left': None, 'right': None,
    'scale': None, 'done': False, 'warn': '',
}


def _track(cand, trk):
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


def _median(vals):
    if not vals:
        return None
    s = sorted(vals)
    return s[len(s) // 2]


def worker(port, sim=False):
    if sim:
        slot = DG.SimSlot('A', 'lr')
    else:
        slot = DG.open_slot('A', port)
    slot.band = (1, min(3, V.BINS))
    proc = PointCloudProcessor(cfar_scale=2.6, min_snr=7.0,
                               remove_clutter=True, clutter_keep=1.0)
    trk = {'ok': False, 'r': 1.0, 'deg': 0.0, 'miss': 0}
    marks = deque(maxlen=40)

    with LOCK:
        STATE['msg'] = '3 秒后开始，请站好'

    time.sleep(1.5)
    results = {}
    for name, dur, tip in PHASES:
        buf = deque(maxlen=300)   # (t, az, n)
        t0 = time.time()
        with LOCK:
            STATE.update(phase=name, phase_name=tip, remain=dur, msg=tip)
        while True:
            now = time.time()
            passed = now - t0
            if passed >= dur:
                break
            try:
                slot.poll(now)
            except Exception:
                time.sleep(0.1)
                continue
            fps = 0.0
            if len(marks) >= 8:
                span = marks[-1] - marks[0]
                if span > 0.5:
                    fps = (len(marks) - 1) / span
            while slot.zs:
                z = slot.zs.pop(0)
                marks.append(now)
                try:
                    pts, _ = proc.process(z[0], z[1])
                except Exception:
                    continue
                cand = [p for p in pts if 0.5 <= p['r'] <= 2.5]
                az = _track(cand, trk) if cand else None
                buf.append((now, az, len(cand)))
                with LOCK:
                    STATE['az'] = None if az is None else round(az, 1)
                    STATE['n'] = len(cand)
                    STATE['fps'] = round(fps, 1)
                    STATE['remain'] = round(max(0.0, dur - passed), 1)
            time.sleep(0.005)
        # 取最后 TAIL 秒
        tail = [a for (t, a, n) in buf if a is not None and (time.time() - t) <= TAIL + 0.3]
        results[name] = _median(tail)
        key = 'zero' if name == 'still' else name
        with LOCK:
            STATE[key] = None if results[name] is None else round(results[name], 1)

    zero = results.get('still')
    left = results.get('left')
    right = results.get('right')

    warn = []
    if zero is None:
        warn.append('静息段没测到人')
    if left is None or right is None:
        warn.append('动作段没测到人')
    elif zero is not None:
        la = abs(left - zero)
        ra = abs(right - zero)
        if la < MIN_AMP or ra < MIN_AMP:
            warn.append('有一侧几乎没动（左 %.1f° / 右 %.1f°），检查站位或动作幅度' % (la, ra))
        else:
            if la < SPACE_WARN:
                warn.append('左侧空间不足（%.1f°），建议整体往右站半步' % la)
            if ra < SPACE_WARN:
                warn.append('右侧空间不足（%.1f°），建议整体往左站半步' % ra)
            scale = (la + ra) / 2.0
            with LOCK:
                STATE['scale'] = round(scale, 1)
            payload = {
                'zero': round(zero, 2),
                'left_amp': round(left - zero, 2),
                'right_amp': round(right - zero, 2),
                'scale_deg': round(scale, 2),
                'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            }
            try:
                os.makedirs(os.path.dirname(CAL_PATH), exist_ok=True)
                with open(CAL_PATH, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except Exception as e:
                warn.append('写文件失败：%s' % e)

    with LOCK:
        STATE['phase'] = 'done'
        STATE['done'] = True
        STATE['warn'] = ' / '.join(warn)
        STATE['msg'] = '校准完成' if not warn else '校准有问题'


PAGE = '''<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>开局校准</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
     background:#0d1117;color:#e6edf3;font-family:system-ui,"Microsoft YaHei",sans-serif}
.box{text-align:center;width:100%;max-width:900px;padding:24px}
.phase{font-size:clamp(30px,5vw,56px);font-weight:800;margin-bottom:10px}
.remain{font-size:clamp(48px,10vw,96px);font-weight:800;color:#e6a23c;font-variant-numeric:tabular-nums}
.az{font-size:clamp(36px,7vw,64px);font-weight:700;margin:14px 0;font-variant-numeric:tabular-nums}
.meta{font-size:18px;color:#8b949e;margin-top:10px}
.result{margin-top:22px;font-size:22px;line-height:1.9}
.result b{color:#3fb950;font-size:26px}
.warn{margin-top:18px;padding:14px 20px;border-radius:10px;background:#8b2c2c;font-size:20px}
</style>
<div class="box">
  <div class="phase" id="phase">准备中...</div>
  <div class="remain" id="remain"></div>
  <div class="az" id="az">--</div>
  <div class="meta" id="meta"></div>
  <div class="result" id="result"></div>
  <div class="warn" id="warn" style="display:none"></div>
</div>
<script>
async function tick(){
  try{
    const r = await fetch('/data',{cache:'no-store'});
    const d = await r.json();
    document.getElementById('phase').textContent = d.msg;
    document.getElementById('remain').textContent = (d.phase==='done') ? '✓' : (d.remain>0 ? d.remain.toFixed(1)+'s' : '');
    document.getElementById('az').textContent = (d.az===null) ? '--' : ((d.az>0?'+':'')+d.az.toFixed(1)+'°');
    document.getElementById('meta').textContent =
      '点数 '+d.n+' ｜ 帧率 '+d.fps.toFixed(1)+' ｜ 零点 '+(d.zero===null?'--':d.zero)+
      ' ｜ 左 '+(d.left===null?'--':d.left)+' ｜ 右 '+(d.right===null?'--':d.right);
    if(d.done){
      document.getElementById('remain').style.color = d.warn ? '#f85149' : '#3fb950';
      document.getElementById('result').innerHTML = d.scale===null ? '' :
        '个人满量程 <b>'+d.scale+'°</b><br>以后走一半就能过门限';
    }
    const w = document.getElementById('warn');
    if(d.warn){ w.style.display='block'; w.textContent = d.warn; }
    else { w.style.display='none'; }
  }catch(e){}
}
setInterval(tick,200); tick();
</script>
</html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith('/data'):
            with LOCK:
                body = json.dumps(STATE, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = PAGE.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='COM5')
    ap.add_argument('--web-port', type=int, default=8779)
    ap.add_argument('--sim', action='store_true')
    a = ap.parse_args()

    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='gbk', errors='replace')
        except Exception:
            pass

    threading.Thread(target=worker, args=(a.port, a.sim), daemon=True).start()
    srv = ThreadingHTTPServer(('127.0.0.1', a.web_port), Handler)
    print('校准页面：http://127.0.0.1:%d/' % a.web_port)
    print('结果写入：%s' % CAL_PATH)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
