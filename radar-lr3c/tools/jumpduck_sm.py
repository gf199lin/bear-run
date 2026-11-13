# -*- coding: utf-8 -*-
"""跳/蹲 实时判别状态机（逐帧流式，不进游戏前不依赖整块数据）。

设计依据（2026-11-13，40 个跳/蹲样本 + 33 块边界数据）：
  跳：最长连续负段 1-2 帧，之后立刻转正（只有 7.7 Hz 才这样）
  蹲：最长连续负段 3-4 帧，之后才是长正段（起身）

因此：
  IDLE   --v<-thr-->  NEG(nb=1)
  NEG    --继续负, nb>=3-->  发 DUCK, 进 SUPPRESS
  NEG    --转正-->           发 JUMP
  SUPPRESS --静默 quiet 帧--> IDLE   (起身后摇被吞掉，不产生 JUMP)

延迟：跳约 2-3 帧（0.26-0.39 s），蹲 3 帧（0.39 s）。1 帧 = 129.5 ms。

仅供离线评估与游戏层复用；本文件的阈值属提案，待小组确认。

用法：
  python tools/jumpduck_sm.py data/jumpduck_battery/<目录> [<目录2> ...]
"""
import argparse, glob, io, json, os, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
import numpy as np
import vguide_1t2r as vg

FPS = 7.73
DT_MS = 1000.0 / FPS
TRUTH = {'jump_m': 'jump', 'jump_s': 'jump', 'jump_l': 'jump', 'jump_k': 'jump', 'combo_j': 'jump',
         'duck_m': 'duck', 'duck_s': 'duck', 'duck_l': 'duck', 'combo_d': 'duck'}


class JumpDuckSM:
    """逐帧推入，返回 ('JUMP'|'DUCK'|None)。

    参数（提案）：
      v_t    径向速度门限 m/s
      nz_t   单帧动作门（零多普勒外能量占比）
      nb_duck 负段达到几帧判蹲
      quiet  蹲确认后静默多少帧回到 IDLE（吞掉起身后摇）
    """

    def __init__(self, v_t=0.15, nz_t=0.30, nb_duck=3, quiet=6, cooldown=8, alpha=0.5):
        self.v_t, self.nz_t, self.nb_duck, self.quiet = v_t, nz_t, nb_duck, quiet
        self.cooldown, self.cd = cooldown, 0
        # 因果 EMA：慢速动作(浅蹲)的 v_r 会在 ±0 之间来回翻符号，
        # 不平滑就永远凑不出"连续同号段"（实测 duck_s 的 nb/pb 全为 0）。
        self.alpha = alpha
        self.vs = 0.0
        self.stall = 0
        self.state = 'IDLE'
        self.nb = 0
        self.quiet_n = 0
        self.t0 = None          # 动作起始帧号
        self.i = 0

    def reset(self):
        self.state, self.nb, self.quiet_n, self.t0 = 'IDLE', 0, 0, None
        self.stall = 0

    def push(self, f):
        """f: frame_feats 字典（可为 None）。返回 (事件, 延迟帧数, 延迟ms)。"""
        i = self.i
        self.i += 1
        if self.cd > 0:
            self.cd -= 1
            return None, None, None
        if f is None:
            return self._tick_idle(i)
        v, nz = f['v_r'], f['nz_share']
        active = nz >= self.nz_t

        if self.state == 'IDLE':
            if not active:
                self.vs *= 0.5
                return None, None, None
            # 触发用原始 v：EMA 冷启动会把首帧削半（0.19 -> 0.096），轻跳会漏
            if self.t0 is None:
                self.t0 = i
            if v < -self.v_t:
                self.vs, self.state, self.nb, self.stall = v, 'NEG', 1, 0
            elif v > self.v_t:
                self.vs, self.state, self.stall = v, 'POS0', 0
            else:
                self.vs = self.alpha * v + (1.0 - self.alpha) * self.vs
            return None, None, None

        # 动作一旦开始，不再要求 nz：起跳腾空瞬间 nz 会掉到门限以下，
        # 用 nz 维持会把跳跃正段掐断（实测 jump_k 有 3 块因此完全无事件）。
        self.vs = self.alpha * v + (1.0 - self.alpha) * self.vs
        v = self.vs

        if self.state == 'NEG':
            if v < -self.v_t:
                self.nb += 1
                self.stall = 0
                if self.nb >= self.nb_duck:
                    self.state, self.quiet_n = 'SUPPRESS', 0
                    return self._emit('DUCK', i)
            elif v > self.v_t:
                return self._emit('JUMP', i)
            else:
                self.stall += 1
                if self.stall >= 3:
                    if self.nb >= self.nb_duck:
                        self.state, self.quiet_n = 'SUPPRESS', 0
                        return self._emit('DUCK', i)
                    if self.nb >= 1:
                        # 短负段后平静下来且没有转正：7.7 Hz 漏掉了上冲（jump_k
                        # 有 3 块就是这样）。负段短 = 快速动作 = 跳，不能丢。
                        return self._emit('JUMP', i)
                    self.reset()
            return None, None, None

        if self.state == 'POS0':
            self.stall = self.stall + 1 if abs(v) < self.v_t else 0
            if self.stall >= 3:
                self.reset()
            return None, None, None

        if self.state == 'SUPPRESS':
            if not active:
                self.quiet_n += 1
            else:
                self.quiet_n = 0
            if self.quiet_n >= self.quiet:
                self.reset()
            return None, None, None

        return None, None, None

    def _tick_idle(self, i):
        self.quiet_n += 1
        if self.quiet_n >= self.quiet:
            self.reset()
        return None, None, None

    def _emit(self, ev, i):
        d = (i - self.t0) if self.t0 is not None else 0
        self.t0 = None
        self.cd = self.cooldown
        # 事件后必须进抑制态：否则同一动作的残余速度会在冷却结束后再报一次
        self.state, self.quiet_n, self.nb = 'SUPPRESS', 0, 0
        return ev.lower(), d, d * DT_MS


def eval_dir(D, verbose=True):
    lj = os.path.join(D, 'labels.json')
    lab = json.load(open(lj, encoding='utf-8'))
    band = tuple(lab['meta']['band'])
    tag = os.path.basename(D.rstrip('\\/'))
    out = []
    if verbose:
        print('\n=== %s  band=%s ===' % (tag, band))
        print('%-4s %-8s %-6s %-6s %4s %s'
              % ('#', 'id', 'truth', 'pred', 'lag', 'lag_ms'))
    for rec in lab['blocks']:
        if rec['id'] not in TRUTH and rec['id'] != 'rest0':
            continue
        truth = TRUTH.get(rec['id'], 'rest')
        p = os.path.join(D, 'blocks', '%03d_%s.npz' % (rec['i'], rec['id']))
        if not os.path.exists(p):
            continue
        q = np.load(p, allow_pickle=False)
        z, lb = q['z'], q['label']
        sm = JumpDuckSM()
        evs = []
        for zz in z:
            f = vg.frame_feats(zz.astype('complex64'), band)
            ev, d, ms = sm.push(f)
            if ev:
                evs.append((ev, d, ms))
        pred = evs[0][0] if evs else 'none'
        lag = evs[0][1] if evs else None
        ms = evs[0][2] if evs else None
        out.append(dict(id=rec['id'], truth=truth, pred=pred,
                        lag=lag, ms=ms, nev=len(evs)))
        if verbose:
            print('%-4d %-8s %-6s %-6s %4s %s   (%d 次事件)'
                  % (rec['i'], rec['id'], truth, pred,
                     '-' if lag is None else lag,
                     '-' if ms is None else '%.0f' % ms, len(evs)))
    return out


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
        if os.path.exists(os.path.join(D, 'labels.json')):
            rows += eval_dir(D)

    jd = [r for r in rows if r['truth'] in ('jump', 'duck')]
    ok = sum(1 for r in jd if r['truth'] == r['pred'])
    one = sum(1 for r in jd if r['nev'] == 1)
    lags = [r['ms'] for r in jd if r['ms'] is not None]
    print('\n=== 汇总 ===')
    print('跳/蹲 %d 样本，判对 %d = %.1f%%' % (len(jd), ok, 100.0 * ok / len(jd)))
    print('只发一次事件：%d/%d = %.1f%%' % (one, len(jd), 100.0 * one / max(len(jd), 1)))
    if lags:
        print('延迟中位 %.0f ms，均值 %.0f ms，最差 %.0f ms'
              % (np.median(lags), np.mean(lags), max(lags)))
    for t in ('jump', 'duck'):
        s = [r for r in jd if r['truth'] == t]
        if s:
            c = sum(1 for r in s if r['pred'] == t)
            ls = [r['ms'] for r in s if r['ms'] is not None]
            print('  %-5s %d/%d  延迟中位 %s ms'
                  % (t, c, len(s), '%.0f' % np.median(ls) if ls else '-'))
    for src in sorted({os.path.basename(d.rstrip('\\/')) for d in
                       [x if os.path.isabs(x) else os.path.join(ROOT, x) for x in dirs]}):
        pass


if __name__ == '__main__':
    main()
