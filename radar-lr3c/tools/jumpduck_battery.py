# -*- coding: utf-8 -*-
"""跳/蹲 采集电池：界面与 vguide_1t2r 完全一致，段表换成多人+负样本协议。"""
import argparse, json, os, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import numpy as np, serial

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
import vguide_1t2r as vg

BAUD = 921600
PREP, REST = 1.6, 3.0
PORT_WEB = 8775


class Reader(threading.Thread):
    def __init__(self, port):
        super().__init__(daemon=True)
        self.ser = serial.Serial(port, BAUD, timeout=0.02)
        self.lock = threading.Lock()
        self.z = None; self.t = 0.0; self.n = 0; self.ts = []
        self.buf = []; self.collect = False; self.stop_flag = False

    def run(self):
        from radar_protocol import StreamParser, FrameAssembler
        p, a = StreamParser(), FrameAssembler()
        t0 = time.time()
        while not self.stop_flag:
            n = self.ser.in_waiting
            if not n:
                time.sleep(0.004); continue
            for rf in p.feed(self.ser.read(n)):
                z = a.push(rf)
                if z is None: continue
                arr = np.stack([z[0], z[1]]).astype('complex64')
                now = time.time() - t0
                with self.lock:
                    self.z = arr; self.t = now; self.n += 1
                    self.ts.append(now); self.ts = self.ts[-50:]
                    if self.collect: self.buf.append((arr, now))

    def begin(self):
        with self.lock: self.buf = []; self.collect = True

    def end(self):
        with self.lock:
            self.collect = False
            return self.buf

    def latest(self):
        with self.lock: return self.z, self.t

    def fps(self):
        with self.lock: ts = list(self.ts)
        return 0.0 if len(ts) < 2 else (len(ts) - 1) / max(1e-6, ts[-1] - ts[0])


def plan_of(tiers, fast):
    k = 0.5 if fast else 1.0
    P = []

    def add(bid, ph, truth, text, say, sec, kind, reps=1, gap=REST):
        for i in range(reps):
            P.append(dict(id=bid, ph=ph, truth=truth, text=text,
                          say=say if reps == 1 else '%s，第 %d 次' % (say, i + 1),
                          sec=sec * k, kind=kind, gap=gap * k))

    if 'p0' in tiers:
        add('rest0', 'base', 'rest', '站好，不要动', '请站在雷达正前方，站好不要动', 12, 'cal', 1, 0)
        add('jump_m', 'jump', 'jump', '跳', '中等高度，跳一次', 3.5, 'act', 4)
        add('duck_m', 'duck', 'duck', '蹲', '中等深度，蹲一次，蹲到位停一下再起来', 3.5, 'act', 4)
        add('walk', 'walk', 'walk', '原地踏步', '原地踏步，正常走路速度', 8, 'neg', 2, 2)
        add('left', 'left', 'left', '左移', '向左横移一步，站住', 3.5, 'lane', 3)
        add('right', 'right', 'right', '右移', '向右横移一步，站住', 3.5, 'lane', 3)
    if 'p1' in tiers:
        add('jump_s', 'jump', 'jump', '小跳', '只跳一点点，小幅度', 3.5, 'act', 2)
        add('jump_l', 'jump', 'jump', '大跳', '尽量跳高，大幅度', 3.5, 'act', 2)
        add('duck_s', 'duck', 'duck', '浅蹲', '只蹲一点点，浅蹲', 3.5, 'act', 2)
        add('duck_l', 'duck', 'duck', '深蹲', '蹲到底，深蹲', 3.5, 'act', 2)
        add('bend', 'bend', 'bend', '弯腰', '弯腰捡东西，上身往下再起来', 3.5, 'neg', 3)
        add('turn', 'turn', 'turn', '转身', '原地转身一百八十度，再转回来', 3.5, 'neg', 3)
    if 'p2' in tiers:
        add('combo_j', 'combo', 'jump', '连续动作', '连续跳三次', 6, 'combo', 2)
        add('combo_d', 'combo', 'duck', '连续动作', '连续蹲三次', 6, 'combo', 2)
        add('alt', 'combo', 'alt', '连续动作', '跳一次，蹲一次，再跳一次', 8, 'combo', 2)
    if 'p4' in tiers:
        # 标定补采：同幅度、同站位、只采中跳/中蹲，用于留一验证
        add('rest0', 'base', 'rest', '站好，不要动', '请站在雷达正前方，站好不要动', 12, 'cal', 1, 0)
        add('jump_m', 'jump', 'jump', '跳', '中等高度，跳一次', 3.5, 'act', 10)
        add('duck_m', 'duck', 'duck', '蹲', '中等深度，蹲一次，蹲到位停一下再起来', 3.5, 'act', 10)
    if 'p5' in tiers:
        # 边界标定：把「蓄力」和「幅度」分开。jump_m 明确要求不预蹲，
        # jump_l 自然起跳（会蓄力），duck_s 要求慢慢蹲（避免负段过短）。
        add('rest0', 'base', 'rest', '站好，不要动', '请站在雷达正前方，站好不要动', 12, 'cal', 1, 0)
        add('jump_m', 'jump', 'jump', '跳', '中等高度，直接起跳，不要先蹲', 3.5, 'act', 8)
        add('duck_m', 'duck', 'duck', '蹲', '中等深度，蹲到位停一下再起来', 3.5, 'act', 8)
        add('jump_l', 'jump', 'jump', '大跳', '尽量跳高，自然起跳', 3.5, 'act', 8)
        add('duck_s', 'duck', 'duck', '浅蹲', '只蹲一点点，浅蹲，慢慢蹲下去', 3.5, 'act', 8)
    if 'p6' in tiers:
        # 最后一组：验证「轻跳(浅蓄力)」能否与深蹲分开。
        # 依据：大跳实际蓄力 38 cm，比中蹲 30 cm 还深，所以按位移分不开；
        # 只有把跳跃做成"浅蓄力+快起跳"，nb 才稳定落在 1-2 帧。
        add('rest0', 'base', 'rest', '站好，不要动', '请站在雷达正前方，站好不要动', 12, 'cal', 1, 0)
        add('jump_k', 'jump', 'jump', '轻跳', '只弯一点点膝盖就起跳，脚跟离地就行', 3.5, 'act', 12)
        add('duck_l', 'duck', 'duck', '深蹲', '深蹲，蹲到底，慢慢蹲慢慢起', 3.5, 'act', 12)
    return P


def pick_band(r, sec=2.0):
    r.begin(); time.sleep(sec); fr = r.end()
    if not fr:
        return (1, 3)
    pf = np.zeros(8)
    for z, _ in fr:
        pf += np.abs(z).mean(axis=(0, 1))
    pf /= len(fr)
    j = int(np.argmax(pf[1:])) + 1
    return (max(1, j - 1), min(7, j + 1) + 1)


WEB, LOCK = {}, threading.Lock()
HIST = []


def st(**kw):
    with LOCK:
        WEB.update(kw)


def snapshot():
    with LOCK:
        return json.dumps(WEB, ensure_ascii=False).encode('utf-8')


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/state'):
            b = snapshot(); ct = 'application/json; charset=utf-8'
        else:
            b = open(os.path.join(HERE, 'jumpduck_battery.html'), 'rb').read()
            ct = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def live(r, band, phase, text, remain, dur, extra=None):
    z, _ = r.latest()
    kw = dict(phase=phase, text=text, remain=round(max(0, remain), 1), dur=round(dur, 2),
              frames=r.n, fps=round(r.fps(), 1), bin_width=0.681, err=None, done=False)
    if extra:
        kw['check_status'] = extra.get('s')
        kw['check_text'] = extra.get('t')
    if z is not None:
        f = vg.frame_feats(z, band)
        if f:
            pf = np.asarray(f['profile'], dtype=float).reshape(-1)
            pf = pf / max(1e-9, pf.max()) * 100
            kw.update(energy=[round(float(v), 1) for v in pf], peak_bin=f['peak_bin'],
                      range_m=round(f['range_m'], 2), dphi=round(f['dphi'], 1),
                      angle_deg=round(f['ang_deg'], 1), v_r=round(f['v_r'], 3),
                      vr_up=round(max(0, f['v_r']), 3), vr_dn=round(min(0, f['v_r']), 3),
                      band_share=round(f['nz_share'], 3),
                      doppler_width=round(f['doppler_width'], 3),
                      nz_share=round(f['nz_share'], 3))
            nz, w = f['nz_share'], f['doppler_width']
            kw['action'] = 'DUCK' if (nz >= 0.30 and w >= 0.38) else ('JUMP' if nz >= 0.30 else None)
            HIST.append(round(f['v_r'], 3)); del HIST[:-120]
            kw['hist'] = HIST[-120:]
    st(**kw)


def hold(sec, fn, tick=0.06):
    t0 = time.time()
    while time.time() - t0 < sec:
        fn(sec - (time.time() - t0))
        time.sleep(tick)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--id', default='S00')
    ap.add_argument('--height', default='')
    ap.add_argument('--note', default='')
    ap.add_argument('--tier', action='append', default=None)
    ap.add_argument('--port', default='auto')
    ap.add_argument('--fast', action='store_true')
    ap.add_argument('--plan', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--no-speak', action='store_true')
    a = ap.parse_args()
    tiers = a.tier or ['p0']
    plan = plan_of(tiers, a.fast)
    if a.limit > 0:
        plan = plan[:a.limit]

    if a.plan:
        tot = 0
        for i, b in enumerate(plan):
            d = PREP * (0.5 if a.fast else 1) + b['sec'] + b['gap']
            tot += d
            print('%3d  %-9s %-10s %5.1fs  %s' % (i + 1, b['id'], b['truth'], d, b['text']))
        print('共 %d 块，约 %.1f 分钟' % (len(plan), tot / 60))
        return

    if a.no_speak:
        vg.NO_SPEAK = True
    port = a.port if a.port != 'auto' else 'COM3'
    r = Reader(port); r.start()
    time.sleep(0.5)
    if r.n == 0:
        print('串口 %s 没数据，检查板子' % port); return
    band = pick_band(r)
    print('串口 %s  band=%s  fps=%.2f' % (port, band, r.fps()))

    stamp = time.strftime('%Y%m%d-%H%M%S')
    out = os.path.join(ROOT, 'data', 'jumpduck_battery', '%s_%s' % (stamp, a.id))
    os.makedirs(os.path.join(out, 'blocks'), exist_ok=True)
    meta = dict(subject=a.id, height=a.height, note=a.note, port=port, tiers=tiers,
                band=band, fps=round(r.fps(), 2), start=stamp, plan=len(plan))
    json.dump(meta, open(os.path.join(out, 'meta.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2)

    threading.Thread(target=ThreadingHTTPServer(('127.0.0.1', PORT_WEB), H).serve_forever,
                     daemon=True).start()
    webbrowser.open('http://127.0.0.1:%d/' % PORT_WEB)

    ZS, TS, LS, META_BLK, FROWS = [], [], [], [], []
    ok_n = 0
    base_nz = [0.0]
    prep_sec = PREP * (0.5 if a.fast else 1.0)
    try:
        for i, b in enumerate(plan):
            tag = '%s · 第 %d/%d 块' % (a.id, i + 1, len(plan))
            ex = dict(s='ok' if ok_n else 'pending', t=tag)
            st(phase='prep', text=b['say'], remain=prep_sec, dur=prep_sec, seg_no=i + 1,
               seg_total=len(plan), check_status=ex['s'], check_text=tag, action=None)
            vg.speak(b['say'])
            hold(prep_sec, lambda rm: st(remain=round(rm, 1)))
            vg.beep(True)

            r.begin()
            hold(b['sec'], lambda rm, b=b: live(r, band, b['ph'], b['text'], rm, b['sec']))
            body_frag = r.end()

            rest_frag = []
            if b['gap'] > 0:
                r.begin()
                hold(b['gap'], lambda rm, b=b: live(r, band, 'base', '站好，别动', rm, b['gap']))
                rest_frag = r.end()

            frag = body_frag + rest_frag
            if not frag:
                print('!! 块 %s 无帧' % b['id']); continue
            zz = np.stack([f[0] for f in frag])
            tt = np.array([f[1] for f in frag])
            labels = np.array([b['truth']] * len(body_frag) + ['rest'] * len(rest_frag),
                              dtype='U8')

            np.savez_compressed(os.path.join(out, 'blocks', '%03d_%s.npz' % (i + 1, b['id'])),
                                z=zz, t=tt, label=labels)
            feats = [vg.frame_feats(z, band) for z in zz]
            nz = [f['nz_share'] for f in feats if f]
            med = float(np.median(nz)) if nz else 0.0
            p90 = float(np.percentile(nz, 90)) if nz else 0.0
            m = [f for f, l in zip(feats, labels) if l != 'rest' and f]
            mnz = [f['nz_share'] for f in m]
            mp90 = float(np.percentile(mnz, 90)) if mnz else 0.0
            # 静息/校准段看中位数（抗一次路过），动作段看 p90（动作只占窗口一小部分）
            good = True
            if b['kind'] == 'cal' and b['id'] == 'rest0':
                base_nz[0] = med
                if med > 0.30:
                    print('!! 场地底噪中位数 %.2f 过高：雷达视场内有人或物持续在动' % med)
                    print('!! 请让所有人退到视场外、关掉风扇，然后重跑本程序')
            act_floor = 0.25 if base_nz[0] > 0.30 else max(0.25, base_nz[0] + 0.20)
            if b['kind'] in ('act', 'combo') and mp90 < act_floor: good = False
            if b['kind'] == 'neg' and p90 > max(0.35, base_nz[0] + 0.25): good = False
            print('%3d %-9s %-5s frames=%-3d nz_med=%.3f nz_p90=%.3f %s'
                  % (i + 1, b['id'], b['truth'], len(zz), med, p90,
                     '' if good else '<< 质检告警'))
            ok_n += 1 if good else 0
            META_BLK.append(dict(i=i + 1, id=b['id'], truth=b['truth'], kind=b['kind'],
                                 frames=int(len(zz)), nz_med=round(med, 3),
                                 nz_p90=round(p90, 3), act_nz_p90=round(mp90, 3),
                                 qc='ok' if good else 'warn'))
            ZS.append(zz); TS.append(tt); LS.append(labels)
            FROWS.append((i + 1, b['id'], len(zz), med, p90, mp90))
    except KeyboardInterrupt:
        print('\n中断，保存已采部分')

    r.stop_flag = True
    if ZS:
        np.savez_compressed(os.path.join(out, 'frames.npz'),
                            z=np.concatenate(ZS), t=np.concatenate(TS),
                            label=np.concatenate(LS))
        with open(os.path.join(out, 'labels.json'), 'w', encoding='utf-8') as f:
            json.dump(dict(meta=meta, blocks=META_BLK), f, ensure_ascii=False, indent=2)
        with open(os.path.join(out, 'metrics.csv'), 'w', encoding='utf-8') as f:
            f.write('block,id,frames,nz_med,nz_p90,act_nz_p90\n')
            for row in FROWS:
                f.write('%d,%s,%d,%.3f,%.3f,%.3f\n' % row)
    st(phase='done', text='采集完成，谢谢', remain=0, dur=0, done=True,
       check_status='ok', check_text='%s 完成 %d 块' % (a.id, len(META_BLK)))
    print('\n已保存: %s  共 %d 块' % (out, len(META_BLK)))
    print('浏览器页面还开着，Ctrl+C 结束')


if __name__ == '__main__':
    main()
