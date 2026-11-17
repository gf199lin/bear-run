# -*- coding: utf-8 -*-
"""跳/蹲融合判别：速度状态机（jumpduck_sm）+ 多普勒谱宽交叉裁决。

为什么这样合并（2026-11-17，data/jumpduck_battery 6 批 96 样本实测）：

1. 速度法（jumpduck_sm）擅长蹲、弱在大跳。
   蹲 47/48，跳只有 35/48；13 个漏检里 5 个是 jump_l 被判成 DUCK——
   大跳落地时反冲的负速度段有 4~8 帧，超过 nb_duck=3，被当成蹲。

2. 谱宽（doppler_width）正好能分开这两类：
       跳   活跃帧谱宽 0.232 ~ 0.479
       蹲   活跃帧谱宽 0.120 ~ 0.288
   0.30 这条线把"被误判成蹲的大跳"（≥0.345）和真正的蹲（≤0.288）完全隔开。

3. 因此只加两条裁决，不动速度法的时序：
   * 速度法报 DUCK，但本动作段谱宽均值 ≥ 0.30 → 改判 JUMP；
   * 速度法报 JUMP，但活跃帧 < 3 → 当误报吞掉（实测静息块里 3 帧弱活动就会误报）。

保留的原始判据不变：负段长度（跳 1~2 帧 / 蹲 3~4 帧）、nz 门控、冷却与抑制。
谱宽只是"第二意见"，不参与时序，所以延迟不增加。

阈值均为本批数据提案，待更多样本确认。

用法（离线）：
  python tools/jumpduck_fusion.py data/jumpduck_battery/2026*
在桥接里：
  radar_live_ws.py --jump-engine fusion
"""
import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))

import numpy as np

import vguide_1t2r as vg
from jumpduck_sm import DT_MS, TRUTH, JumpDuckSM

NZ_GATE = 0.30          # 与 jumpduck_sm 的门控一致
DEFER_DUCK = 2          # 负段凑够 nb_duck 后先观望两帧再判蹲
DUCK_W_OVERRIDE = 0.30  # 谱宽改判门限（仅 use_width=True 时生效）
USE_WIDTH = False       # 谱宽改判离线是负收益，默认关，只留作诊断
MIN_ACT = 0             # >0 时吞掉活跃帧不足的弱事件（默认不吞）
STALE = 6               # 连续这么多帧没活跃就丢弃证据（≈0.78 s）

# 速度状态机的现场取值（2026-11-17 扫参，4 批 100 样本）
V_T = 0.13              # 负方向进门限（看到动作）
V_JUMP = 0.32           # 判跳要的正速度峰值（原始值）。0.35 漏 3 个跳，0.32 全部命中
#   扫参：0.35→跳 43/46；0.32→跳 46/46、浅蹲误判跳 4/10、跳延迟 388ms。取 0.32。
V_DUCK = 0.35           # 判蹲要的负速度深度（EMA 后）。小蹲蓄力到不了，只认全蹲
#   实测：浅蹲(duck_s) 误报蹲 6/10→1/10，中蹲 22/22、全蹲 12/14 保留
NZ_T = 0.20              # 单帧动作门（零多普勒外能量占比）；0.28 会把弱动作整帧丢掉
NB_DUCK = 3             # 负段几帧判蹲。试过 4：蹲检出掉到 43/48、延迟 628ms，现场嫌蹲不灵；
#                         回到 3 后蹲 45/48、延迟 561ms，跳 44/48。
#                         注意 defer_duck=1 会把跳打到 36/48（来不及等负段转正），别往下调。
# 同口径：跳 44/48、蹲 45/48、总 89/100（延迟 561ms）


class JumpDuckFusion:
    """与 JumpDuckSM 同接口：push(f) -> (事件, 延迟帧, 延迟ms)。"""

    def __init__(self, sm=None, nz_gate=NZ_GATE,
                 duck_w_override=DUCK_W_OVERRIDE, min_act=MIN_ACT,
                 stale=STALE, defer_duck=DEFER_DUCK, use_width=USE_WIDTH,
                 **sm_kw):
        if sm is None:
            # 2026-11-17 现场调参：跳要更灵、蹲要更稳（4 组扫参结论，见 docs）。
            #   v_t 0.15→0.12、nz_t 0.30→0.26 → 轻跳也能起触发；
            #   nb_duck 3→4 → 蹲要凑够 4 帧负段，不再一动就蹲。
            #   实测：跳 44/48→46/48，蹲 46/48→43/48，总 90%→89%。
            sm_kw.setdefault('v_t', V_T)
            sm_kw.setdefault('nz_t', NZ_T)
            sm_kw.setdefault('nb_duck', NB_DUCK)
            sm_kw.setdefault('v_jump', V_JUMP)
            sm_kw.setdefault('v_duck', V_DUCK)
            sm_kw.setdefault('defer_duck', defer_duck)
            sm = JumpDuckSM(**sm_kw)
        self.sm = sm
        self.nz_gate = nz_gate
        self.duck_w_override = duck_w_override
        self.use_width = use_width
        self.min_act = min_act
        self.stale = stale
        self.act_n = 0
        self.act_w = []
        self.idle_run = 0
        self.spike = None        # 现场排错：最近一次动作段的谱宽/帧数

    def reset(self):
        self.act_n, self.act_w, self.idle_run = 0, [], 0

    def push(self, f):
        if f is None:
            self.idle_run += 1
            if self.idle_run >= self.stale:
                self.reset()
            out = self.sm.push(None)
            return self._take(out)

        nz = float(f.get('nz_share') or 0.0)
        if nz >= self.nz_gate:
            self.act_n += 1
            self.act_w.append(float(f.get('doppler_width') or 0.0))
            self.idle_run = 0
        else:
            self.idle_run += 1
            if self.idle_run >= self.stale:
                self.reset()

        return self._take(self.sm.push(f))

    def _take(self, out):
        ev = out[0] if isinstance(out, tuple) else out
        if not ev:
            return out if isinstance(out, tuple) else (None, None, None)

        n = self.act_n
        w = (sum(self.act_w) / len(self.act_w)) if self.act_w else 0.0
        self.spike = {'ev': ev, 'w': round(w, 3), 'act': n}
        self.reset()

        tail = tuple(out[1:]) if isinstance(out, tuple) else (None, None)

        # 大跳的落地反冲被速度法当成蹲 → 谱宽否决（use_width 打开时才生效）
        if self.use_width and ev == 'duck' and w >= self.duck_w_override:
            return ('jump',) + tail
        # 太弱的短段（站着晃一下）→ 吞掉
        if ev == 'jump' and n < self.min_act:
            return (None, None, None)
        return (ev,) + tail


def eval_dir(D, verbose=True, factory=None):
    lp = os.path.join(D, 'labels.json')
    if not os.path.exists(lp):
        return []
    make = factory or JumpDuckFusion
    lab = json.load(open(lp, encoding='utf-8'))
    band = tuple((lab.get('meta') or {}).get('band') or (1, 4))
    tag = os.path.basename(D.rstrip('\\/'))
    rows = []
    if verbose:
        print('\n=== %s  band=%s ===' % (tag, band))
    for rec in lab['blocks']:
        truth = TRUTH.get(rec['id'])
        if truth is None and rec['id'] != 'rest0':
            continue
        truth = truth or 'rest'
        p = os.path.join(D, 'blocks', '%03d_%s.npz' % (rec['i'], rec['id']))
        if not os.path.exists(p):
            continue
        z = np.load(p, allow_pickle=False)['z']
        det = make()
        evs = []
        for k in range(len(z)):
            f = vg.frame_feats(z[k].astype('complex64'), band)
            ev = det.push(f)
            if ev and ev[0]:
                evs.append(ev)
        for _ in range(STALE + 2):
            ev = det.push({'v_r': 0.0, 'nz_share': 0.0, 'doppler_width': 0.0})
            if ev and ev[0]:
                evs.append(ev)
        pred = evs[0][0] if evs else 'none'
        rows.append({'id': rec['id'], 'truth': truth, 'pred': pred,
                     'lag': evs[0][1] if evs else None,
                     'ms': evs[0][2] if evs else None,
                     'nev': len(evs), 'spike': det.spike})
        if verbose:
            sp = det.spike or {}
            print('  %-4s %-8s 真%-4s 判%-5s w=%-6s act=%-3s'
                  % (rec['i'], rec['id'], truth, pred, sp.get('w'), sp.get('act')))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dirs', nargs='+')
    a = ap.parse_args()
    dirs = []
    for d in a.dirs:
        dirs += sorted(glob.glob(d)) if any(ch in d for ch in '*?') else [d]

    rows = []
    for D in dirs:
        D = D if os.path.isabs(D) else os.path.join(ROOT, D)
        rows += eval_dir(D)

    jd = [r for r in rows if r['truth'] in ('jump', 'duck')]
    rest = [r for r in rows if r['truth'] == 'rest']
    ok = sum(1 for r in jd if r['truth'] == r['pred'])
    one = sum(1 for r in jd if r['nev'] == 1)
    lags = [r['ms'] for r in jd if r['ms'] is not None]
    fpos = sum(1 for r in rest if r['pred'] != 'none')

    print('\n=== 汇总 ===')
    print('跳/蹲 %d 样本，判对 %d = %.1f%%' % (len(jd), ok, 100.0 * ok / max(len(jd), 1)))
    print('只发一次事件：%d/%d = %.1f%%' % (one, len(jd), 100.0 * one / max(len(jd), 1)))
    print('静息误报：%d/%d' % (fpos, len(rest)))
    if lags:
        print('延迟中位 %.0f ms，均值 %.0f ms，最差 %.0f ms'
              % (np.median(lags), np.mean(lags), max(lags)))
    for t in ('jump', 'duck'):
        s = [r for r in jd if r['truth'] == t]
        o = sum(1 for r in s if r['pred'] == t)
        print('  %-4s %d/%d = %.1f%%' % (t, o, len(s), 100.0 * o / max(len(s), 1)))
    bad = [(r['id'], r['truth'], r['pred']) for r in jd if r['truth'] != r['pred']]
    if bad:
        print('  判错：', bad)


if __name__ == '__main__':
    main()
