# -*- coding: utf-8 -*-
r"""双板联合采集引导：一块板管左右换道，一块板管跳 / 蹲。

为什么需要它
------------
11/17 把左右和跳蹲接进游戏时，所有阈值都是**离线回放**定的：
  · 左右：进入门 0.22（归一化，×20° ≈ 4.4°）
  · 跳蹲：V_JUMP 0.32 / V_DUCK 0.35 / NZ_T 0.20 / defer_duck 2
这些值在「人站着、板子这样摆」的现场条件下从没验证过。本程序就是那一步：
**先判定哪块板是水平、哪块是竖直，再按固定顺序做动作，边采边出结论。**

它做什么
--------
  1. 打开两块 1T2R（CP210x），确认两路都在 7.6 fps 出帧
  2. **角色自检**（约 34 s）：站好 → 左移 → 右移 → 举手 → 蹲下
     用「左右移动时的角度变化」对「上下移动时的角度变化」，判定哪块板负责哪一轴
  3. **正式采集**（约 110 s）：静息 → 左/中/右 × 2 → 直接跳 → 中蹲 → 浅蹲 → 全蹲
     → 负样本（弯腰 / 踏步 / 转身）
  4. **立即分析**：左右给「相对中线的增量 vs 4.4° 门限」，
     跳蹲用 `jumpduck_fusion.JumpDuckFusion` 逐帧回放，按段统计命中与误报
  5. 落盘 `data/dual_guide/<时间戳>/`：两路 npz + labels.json + metrics.csv + report.md

用法
----
    python tools\dual_guide.py                      # 自动识别两块板，先自检再采
    python tools\dual_guide.py --lr COM3 --ud COM5  # 直接指定，省去猜测
    python tools\dual_guide.py --rounds 2           # 采两轮
    python tools\dual_guide.py --no-axis-check      # 跳过自检（已知摆位时）
    python tools\dual_guide.py --simulate           # 合成数据，不碰硬件
    python tools\dual_guide.py --list               # 只列串口

站姿与摆法（与 11/17 一致）
---------------------------
  · 水平板：横放，人站正前方约 1.0 m，中线 +9°（radar-pointcloud/center.json）
  · 竖直板：竖放（绕光轴转 90°），离地约 0.5 m，人站约 1.2 m
  · 全程只做「一步、一次、一次到位」的动作，不要前后走、不要转身（负样本段除外）

产出里的标签含义
----------------
  rest        静息（测误报基线）
  lane_*      左右换道的真值位置
  jump        直接起跳（不预蹲）
  duck_mid / duck_shallow / duck_full   三档蹲深
  neg_*       应当**不触发**的动作（弯腰 / 踏步 / 转身）
"""
import argparse
import json
import math
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in ('radar-pointcloud', 'radar-service'):
    if os.path.exists(os.path.join(ROOT, _p, 'radar_protocol.py')):
        sys.path.insert(0, os.path.join(ROOT, _p))
        break
sys.path.insert(0, HERE)

import vguide_1t2r as V                      # noqa: E402
from jumpduck_fusion import JumpDuckFusion   # noqa: E402

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PORT = 8773
LOCK = threading.Lock()

# 左右门的换算：游戏 enterThreshold 0.22 × scale-deg 20° = 4.4°
LANE_GATE_DEG = 0.22 * 20.0

AXIS_SEGS = [
    ('axis_stand1', '自检：站到中间，站好别动', 4.0),
    ('axis_left',   '自检：向你的左边横移一步，站住', 5.0),
    ('axis_back1',  '自检：回到中间站好', 3.0),
    ('axis_right',  '自检：向你的右边横移一步，站住', 5.0),
    ('axis_back2',  '自检：回到中间站好', 3.0),
    ('axis_up',     '自检：双手举高，站住别动', 5.0),
    ('axis_down',   '自检：慢慢蹲到底，站住', 5.0),
    ('axis_stand2', '自检：站起来，回到中间站好', 4.0),
]

SEG_LANE = [
    ('lane_left',   '向你的左边横移半步，站住别动', 6.0),
    ('lane_back1',  '回到中间站好', 5.0),
    ('lane_right',  '向你的右边横移半步，站住别动', 6.0),
    ('lane_back2',  '回到中间站好', 5.0),
    ('lane_left2',  '再向左边横移半步，站住别动', 6.0),
    ('lane_back3',  '回到中间站好', 5.0),
    ('lane_right2', '再向右边横移半步，站住别动', 6.0),
    ('lane_back4',  '回到中间站好', 5.0),
]

SEG_UD = [
    ('jump',         '直接起跳：不要预蹲，连跳 3 次', 10.0),
    ('jump_rest',    '停，站好别动', 4.0),
    ('duck_mid',     '中蹲：屈膝到大腿接近水平，连蹲 3 次', 10.0),
    ('duck_rest1',   '停，站好别动', 4.0),
    ('duck_shallow', '浅蹲：只微微屈膝，连蹲 3 次', 9.0),
    ('duck_rest2',   '停，站好别动', 4.0),
    ('duck_full',    '全蹲：蹲到底，连蹲 3 次', 10.0),
    ('duck_rest3',   '停，站好别动', 4.0),
    ('neg_bend',     '负样本：弯腰捡东西 3 次（不该触发）', 9.0),
    ('neg_march',    '负样本：原地踏步 3 秒（不该触发）', 9.0),
    ('neg_turn',     '负样本：左右转身 2 次（不该触发）', 9.0),
]

REST_SEG = ('rest', '静息：站中间，双手自然下垂，别动', 12.0)

SEG_LABEL = {
    'rest': '静息', 'lane_left': '左移', 'lane_left2': '左移', 'lane_right': '右移',
    'lane_right2': '右移', 'lane_back1': '回中', 'lane_back2': '回中',
    'lane_back3': '回中', 'lane_back4': '回中', 'jump': '起跳',
    'jump_rest': '停', 'duck_mid': '中蹲', 'duck_shallow': '浅蹲',
    'duck_full': '全蹲', 'duck_rest1': '停', 'duck_rest2': '停', 'duck_rest3': '停',
    'neg_bend': '弯腰', 'neg_march': '踏步', 'neg_turn': '转身',
}

WEB = {
    'phase': 'boot', 'seg': '', 'seg_text': '正在启动，请稍候',
    'seg_no': 0, 'seg_total': 0, 'round': 0, 'rounds': 1,
    'remain': 0.0, 'dur': 0.0, 'fps': 0.0, 'err': '',
    'axis': {'status': 'pending', 'lr': '', 'ud': '', 'text': ''},
    'lanes': {'lr': {'present': False}, 'ud': {'present': False}},
}


def _set(**kw):
    with LOCK:
        WEB.update(kw)


def _set_lane(role, **kw):
    with LOCK:
        cur = dict(WEB['lanes'].get(role) or {})
        cur.update(kw)
        WEB['lanes'][role] = cur


# ------------------------------------------------------------------ 串口

def com_no(dev):
    m = re.search(r'(\d+)', dev or '')
    return int(m.group(1)) if m else 999


def discover_ports():
    ps = V.list_ports()
    cand = [p for p in ps if V._is_1t2r(p[1], p[2])] or ps
    return sorted({p[0] for p in cand}, key=com_no)


class Slot:
    """一路雷达：读帧 + 逐帧特征 + 真值标签。"""

    def __init__(self, key, port, reader):
        self.key = key
        self.port = port
        self.reader = reader
        self.role = None
        self.band = (1, min(3, V.BINS))
        self.zs = []
        self.ts = []
        self.labels = []
        self.feats = []
        self.marks = []
        self.label = 'idle'
        self.last_frame_at = 0.0
        self.t0 = time.time()

    def poll(self, rec):
        for z in self.reader.poll():
            self.zs.append(z)
            self.ts.append(rec)
            self.labels.append(self.label)
            self.feats.append(V.frame_feats(z, self.band))
            self.last_frame_at = time.time()

    def close(self):
        try:
            self.reader.close()
        except Exception:
            pass

    def seg_vals(self, seg, key='ang_deg'):
        return [f[key] for f, l in zip(self.feats, self.labels)
                if f and l == seg and key in f]

    def med(self, seg, key='ang_deg'):
        v = self.seg_vals(seg, key)
        return float(np.median(v)) if len(v) >= 3 else None

    def fps(self):
        n = len(self.zs)
        if n < 8:
            return 0.0
        span = self.ts[-1] - self.ts[0]
        return (n - 1) / span if span > 0.5 else 0.0


def open_slot(key, port):
    import serial
    ser = serial.Serial(port, V.BAUD, timeout=0.02)
    for attr in ('dtr', 'rts'):
        try:
            setattr(ser, attr, True)
        except Exception:
            pass
    time.sleep(0.15)
    ser.reset_input_buffer()
    return Slot(key, port, V.Reader(ser))


class SimSlot(Slot):
    """合成数据：验证程序流程，不碰硬件。"""

    def __init__(self, key, kind_hint='lr'):
        self.key = key
        self.port = 'SIM-' + key
        self.role = None
        self.band = (1, min(3, V.BINS))
        self.zs, self.ts, self.labels, self.feats, self.marks = [], [], [], [], []
        self.label = 'idle'
        self.last_frame_at = 0.0
        self.t0 = time.time()
        self.kind_hint = kind_hint
        self.sim = V.SimReader(seed=7 if key == 'A' else 11)
        self.sim.set_dphi(0.45)
        self.tick_i = 0

    def set_state(self, seg):
        self.tick_i = 0
        if self.kind_hint == 'lr':
            self.sim.set_dphi({
                'lane_left': 1.30, 'lane_left2': 1.35, 'lane_right': -0.20,
                'lane_right2': -0.25, 'axis_left': 1.30, 'axis_right': -0.20,
            }.get(seg, 0.45))
        else:
            self.sim.set_dphi({'axis_up': 1.55, 'axis_down': -0.85}.get(seg, 0.45))

    @staticmethod
    def _vr_of(seg, k):
        """按帧给出速度波形：起跳必须先蓄力（负）再上冲（正），状态机才认得出。

        每 25 帧（≈3.2 s）重复一次，模拟「连做 3 次」的真值安排。
        """
        t = k % 25
        if seg == 'jump':
            if t < 2:
                return -0.45
            if t < 5:
                return 0.65
            return 0.0
        if seg == 'duck_mid':
            return -0.50 if t < 6 else 0.0
        if seg == 'duck_full':
            return -0.70 if t < 6 else 0.0
        if seg == 'duck_shallow':
            return -0.20 if t < 6 else 0.0
        return 0.0

    def poll(self, rec):
        if self.kind_hint != 'lr':
            self.sim.set_vr(self._vr_of(self.label, self.tick_i))
        for z in self.sim.poll():
            self.tick_i += 1
            self.zs.append(z)
            self.ts.append(rec)
            self.labels.append(self.label)
            self.feats.append(V.frame_feats(z, self.band))
            self.last_frame_at = time.time()

    def close(self):
        pass


# ------------------------------------------------------------------ 主流程

class Runner:
    def __init__(self, args, slots):
        self.args = args
        self.slots = slots                  # key -> Slot
        self.t0 = time.time()
        self.out_dir = ''
        self.err = ''
        self.axis = {'status': 'pending', 'lr': '', 'ud': '', 'text': '等待角色自检',
                     'd_lr': {}, 'd_ud': {}}
        self.lr_key = None
        self.ud_key = None
        self.rounds = max(1, args.rounds)
        self.speed = max(0.1, float(getattr(args, 'speed', 1.0)))
        self.judge = None
        self.events = []                    # (seg, ev)
        self._last_pub = 0.0
        self._judged = 0

    def _sc(self, sec):
        """段时长按 --speed 缩放（自测用；真实采集保持 1.0）。"""
        return max(0.15, float(sec) / self.speed)

    # ---- 轮询 ----
    def _pump(self, seconds, label):
        for s in self.slots.values():
            s.label = label
            if isinstance(s, SimSlot):
                s.set_state(label)
        end = time.time() + seconds
        while time.time() < end:
            rec = time.time() - self.t0
            for s in self.slots.values():
                s.poll(rec)
            self._track_events()
            self._publish(rec, max(0.0, end - time.time()))
        return {k: len(s.zs) for k, s in self.slots.items()}

    def _publish(self, rec, remain):
        now = time.time()
        if now - self._last_pub < 0.11:
            return
        self._last_pub = now
        total_n = sum(len(s.zs) for s in self.slots.values())
        fps = 0.0
        if total_n >= 8 and rec > 1.0:
            fps = total_n / rec
        upd = {'frames': total_n, 'fps': round(fps, 1),
               'remain': round(max(0.0, remain), 1), 'dur': round(WEB.get('dur') or 0.0, 1)}
        _set(**upd)
        for s in self.slots.values():
            role = s.role
            if not role:
                continue
            f = next((x for x in reversed(s.feats) if x), None)
            hb = int(1000 * (now - s.last_frame_at)) if s.last_frame_at else None
            lane = {'present': True, 'port': s.port, 'frames': len(s.zs),
                    'fps': round(s.fps(), 1), 'hb_ms': hb,
                    'peak_bin': -1, 'range_m': 0.0, 'angle_deg': 0.0,
                    'v_r': 0.0, 'width': 0.0, 'nz_share': 0.0, 'energy': [],
                    'last_ev': ''}
            if f:
                lane.update({
                    'peak_bin': f['peak_bin'], 'range_m': round(f['range_m'], 2),
                    'angle_deg': round(f['ang_deg'], 1), 'v_r': round(f['v_r'], 2),
                    'width': round(f['doppler_width'], 3),
                    'nz_share': round(f['nz_share'], 2),
                    'energy': V.profile_energy(f['profile']),
                })
            if role == 'ud' and self.judge is not None and self.events:
                lane['last_ev'] = self.events[-1][1].upper()
            if role == 'lr':
                mid = s.med('rest') if s.med('rest') is not None else None
                if mid is not None and f:
                    lane['last_ev'] = 'Δ%+.1f°' % (f['ang_deg'] - mid)
            _set_lane(role, **lane)

    # ---- 角色自检 ----
    def run_axis_check(self):
        _set(phase='axis', seg='', seg_text='角色自检：站中间', remain=0, dur=0)
        V.speak('现在做角色自检。先站到中间，站好不要动。')
        for seg, hint, sec in AXIS_SEGS:
            _set(phase='axis', seg=seg, seg_text=hint, remain=self._sc(sec + 1.6),
                 dur=self._sc(sec + 1.6))
            V.speak(hint)
            self._pump(self._sc(1.6), 'idle')
            V.beep(seg.startswith('axis_stand') or seg.startswith('axis_back'))
            time.sleep(self._sc(0.25))
            i0 = {k: len(s.zs) for k, s in self.slots.items()}
            self._pump(self._sc(sec), seg)
            for k, s in self.slots.items():
                s.marks.append({'kind': 'axis', 'seg': seg,
                                'frame_start': i0[k], 'frame_end': max(i0[k], len(s.zs) - 1),
                                'frames': len(s.zs) - i0[k]})

        def resp(slot):
            l = slot.med('axis_left')
            r = slot.med('axis_right')
            u = slot.med('axis_up')
            d = slot.med('axis_down')
            d_lr = abs(l - r) if (l is not None and r is not None) else 0.0
            d_ud = abs(u - d) if (u is not None and d is not None) else 0.0
            return d_lr, d_ud

        scores = {k: resp(s) for k, s in self.slots.items()}
        for k, (a, b) in scores.items():
            print('  [自检] %s：左右响应 %.1f° ｜ 上下响应 %.1f°'
                  % (self.slots[k].port, a, b), flush=True)

        keys = list(self.slots.keys())
        if len(keys) == 1:
            k = keys[0]
            a, b = scores[k]
            if a >= b:
                self.lr_key, self.ud_key = k, None
                text = ('单板：左右响应 %.1f° > 上下响应 %.1f°，判定为**水平（左右）板**。'
                        '要验跳蹲请再插一块竖放的板。' % (a, b))
            else:
                self.lr_key, self.ud_key = None, k
                text = ('单板：上下响应 %.1f° > 左右响应 %.1f°，判定为**竖直（跳蹲）板**。'
                        '要验左右请再插一块横放的板。' % (b, a))
            status = 'ok' if max(a, b) >= 4.0 else 'warn'
            if status == 'warn':
                text += ' 但最大响应只有 %.1f°，结果不敢下结论。' % max(a, b)
        else:
            k1, k2 = keys[0], keys[1]
            a1, b1 = scores[k1]
            a2, b2 = scores[k2]
            # 归一化比值：谁的「左右占比」高，谁就是水平板
            s1 = a1 / max(a1 + b1, 0.5)
            s2 = a2 / max(a2 + b2, 0.5)
            if s1 >= s2:
                self.lr_key, self.ud_key = k1, k2
            else:
                self.lr_key, self.ud_key = k2, k1
            lr_a = scores[self.lr_key][0]
            ud_b = scores[self.ud_key][1]
            ok = lr_a >= 4.0 and ud_b >= 4.0
            status = 'ok' if ok else 'warn'
            text = ('判定 %s（左右，响应 %.1f°）= 水平板；%s（上下，响应 %.1f°）= 竖直板。%s'
                    % (self.slots[self.lr_key].port, lr_a,
                       self.slots[self.ud_key].port, ud_b,
                       '两块板都对准了，可以采。' if ok else
                       '有一侧响应偏小（<4°），先别采，检查摆位：水平板横放、竖直板绕光轴转 90°。'))

        for k in keys:
            self.slots[k].role = 'lr' if k == self.lr_key else ('ud' if k == self.ud_key else None)
        if self.lr_key is None:
            _set_lane('lr', present=False)
        if self.ud_key is None:
            _set_lane('ud', present=False)

        self.axis = {'status': status, 'lr': self.slots[self.lr_key].port if self.lr_key else '',
                     'ud': self.slots[self.ud_key].port if self.ud_key else '',
                     'text': text,
                     'd_lr': {self.slots[k].port: round(scores[k][0], 1) for k in keys},
                     'd_ud': {self.slots[k].port: round(scores[k][1], 1) for k in keys}}
        _set(axis=self.axis)
        print('\n  [角色判定] %s\n' % text, flush=True)
        return status

    # ---- 采动作 ----
    def build_plan(self):
        want_lane = bool(self.lr_key) and self.args.part in ('all', 'lane')
        want_ud = bool(self.ud_key) and self.args.part in ('all', 'ud')
        if not want_lane and not want_ud:
            return []
        plan = [REST_SEG]
        if want_lane:
            plan += SEG_LANE
        if want_ud:
            plan += SEG_UD
        return plan

    def run_capture(self):
        a = self.args
        plan = self.build_plan()
        if not plan:
            self.err = '没有可用的雷达角色，未采集'
            _set(err=self.err)
            return
        seg_total = len(plan) * self.rounds

        _set(phase='prep', seg_text='准备开始：站到中间，双手自然下垂',
             remain=self._sc(6.0), dur=self._sc(6.0), seg_total=seg_total, seg_no=0,
             round=0, rounds=self.rounds)
        V.speak('准备开始。请面向水平雷达站好，双手自然下垂。')
        self._pump(self._sc(6.0), 'idle')

        self.pick_bands()
        self.judge = JumpDuckFusion() if self.ud_key else None
        if self.judge:
            # 判据只吃正式采集的帧：自检里人有举手/蹲下，回放会造出假事件
            self._judged = len(self.slots[self.ud_key].feats)
            self.events = []

        no = 0
        for r in range(1, self.rounds + 1):
            for seg, hint, sec in plan:
                no += 1
                _set(phase='run', seg=seg, seg_text=hint, seg_no=no, seg_total=seg_total,
                     round=r, rounds=self.rounds, remain=self._sc(sec + 2.0),
                     dur=self._sc(sec + 2.0))
                V.speak(hint)
                self._pump(self._sc(2.0), 'idle')
                is_rest = seg == 'rest' or 'rest' in seg or seg.startswith('lane_back')
                V.beep(is_rest)
                time.sleep(self._sc(0.3))

                i0 = {k: len(s.zs) for k, s in self.slots.items()}
                for s in self.slots.values():
                    s.label = seg
                    if isinstance(s, SimSlot):
                        s.set_state(seg)
                t_seg = time.time() - self.t0
                end = time.time() + self._sc(sec)
                shown = None
                while time.time() < end:
                    rec = time.time() - self.t0
                    for s in self.slots.values():
                        s.poll(rec)
                    self._publish(rec, max(0.0, end - time.time()))
                    left = end - time.time()
                    if left <= 3.2:
                        n = int(left) + 1
                        if n != shown and n >= 1:
                            shown = n
                            V.tick()
                    self._track_events()
                V.beep(True)

                for k, s in self.slots.items():
                    s.marks.append({'kind': 'segment', 'round': r, 'seg': seg,
                                    'frame_start': i0[k], 'frame_end': max(i0[k], len(s.zs) - 1),
                                    'frames': len(s.zs) - i0[k],
                                    't_start': round(t_seg, 2),
                                    't_end': round(time.time() - self.t0, 2)})
                    if len(s.zs) == i0[k]:
                        _set(err='这一段 %s 没收到帧，检查串口是否被占用' % s.port)
                print('  [%d/%d] %-13s lr=%d ud=%d 帧' % (
                    no, seg_total, seg,
                    len(self.slots[self.lr_key].zs) if self.lr_key else 0,
                    len(self.slots[self.ud_key].zs) if self.ud_key else 0), flush=True)

    def _track_events(self):
        """逐帧推进跳蹲判据。事件按「输出帧号 − 延迟帧数」回溯到动作真正发生的段。

        不回溯的话，一次蹲在段末判出、defer_duck 又压了 2 帧，
        事件会记到下一个人为段（比如 duck_rest 或负样本段），统计就失真。
        """
        if not self.ud_key or self.judge is None:
            return
        s = self.slots[self.ud_key]
        while self._judged < len(s.feats):
            i = self._judged
            out = self.judge.push(s.feats[i])
            ev = out[0] if isinstance(out, tuple) else out
            if ev:
                d = out[1] if isinstance(out, tuple) and len(out) > 1 else None
                j = i
                if isinstance(d, (int, float)) and d:
                    j = max(0, i - int(d))
                seg = s.labels[j] if j < len(s.labels) else (s.labels[i] if i < len(s.labels) else '?')
                self.events.append((seg, ev))
                print('      · 事件 %s（发生在 %s，第 %d 帧）' % (ev.upper(), seg, j), flush=True)
            self._judged += 1

    def pick_bands(self):
        """按静息段主 bin 定距离带：覆盖人所在的 1~2 个 bin。"""
        a = self.args
        for role, key, forced in (('lr', self.lr_key, a.band_lr), ('ud', self.ud_key, a.band_ud)):
            if not key:
                continue
            s = self.slots[key]
            if forced:
                s.band = forced
            else:
                bins = [f['peak_bin'] for f, l in zip(s.feats, s.labels)
                        if f and l == 'idle' and f['peak_bin'] >= 1]
                j = int(round(float(np.median(bins)))) if bins else 1
                s.band = (max(1, j - 1), min(V.BINS, j + 2))
            print('  [距离带] %s %s：bin %d–%d（%.2f–%.2f m）'
                  % (role, s.port, s.band[0], s.band[1] - 1,
                     s.band[0] * V.RANGE_RES, s.band[1] * V.RANGE_RES), flush=True)

    # ---- 保存 ----
    def save(self):
        z_any = any(s.zs for s in self.slots.values())
        if not z_any:
            _set(err='没有采到任何帧（未保存）')
            return
        stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(self.t0))
        self.out_dir = os.path.join(self.args.out, stamp)
        os.makedirs(self.out_dir, exist_ok=True)

        roles = {'lr': self.lr_key, 'ud': self.ud_key}
        for role, key in roles.items():
            if not key:
                continue
            s = self.slots[key]
            if not s.zs:
                continue
            z = np.stack(s.zs, axis=0)
            np.savez_compressed(os.path.join(self.out_dir, 'frames_%s.npz' % role),
                                z=z, t=np.asarray(s.ts, float),
                                label=np.asarray(s.labels))
            rows = ['i,t,label,dphi_rad,ang_deg,peak_bin,range_m,v_r_mps,'
                    'doppler_width,nz_share,band_power']
            for i, (f, l, t) in enumerate(zip(s.feats, s.labels, s.ts)):
                if not f:
                    continue
                rows.append('%d,%.3f,%s,%.4f,%.2f,%d,%.2f,%.3f,%.3f,%.3f,%.1f'
                            % (i, t, l, f['dphi'], f['ang_deg'], f['peak_bin'],
                               f['range_m'], f['v_r'], f['doppler_width'],
                               f['nz_share'], f['band_power']))
            with open(os.path.join(self.out_dir, 'metrics_%s.csv' % role), 'w',
                      encoding='utf-8') as fh:
                fh.write('\n'.join(rows))

        meta = {
            'created': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.t0)),
            'board': '1t2r', 'chirps': V.CHIRPS, 'bins': V.BINS, 'rx': V.N_RX,
            'frame_period_s': V.FRAME_PERIOD, 'range_res_m': V.RANGE_RES,
            'ant_spacing_m': V.D_ANT, 'k_phi': V.K_PHI,
            'simulate': bool(self.args.simulate),
            'roles': {r: (self.slots[k].port if k else None) for r, k in roles.items()},
            'bands': {r: (list(self.slots[k].band) if k else None) for r, k in roles.items()},
            'axis_check': self.axis, 'rounds': self.rounds,
            'lane_gate_deg': LANE_GATE_DEG,
            'events': [{'seg': s, 'ev': e} for s, e in self.events],
            'segments': {r: (self.slots[k].marks if k else []) for r, k in roles.items()},
            'note': '真值由段边界给出；frame_start/frame_end 是各自 npz 的行号；'
                    'idle = 播报/准备段，不属于真值；labels.json 的 roles 说明哪个口对应哪条轴。',
        }
        with open(os.path.join(self.out_dir, 'labels.json'), 'w', encoding='utf-8') as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        print('\n已保存 %s' % self.out_dir, flush=True)

    # ---- 入口 ----
    def run(self):
        try:
            if not self.args.no_axis_check:
                self.run_axis_check()
            else:
                keys = list(self.slots.keys())
                self.lr_key, self.ud_key = keys[0], (keys[1] if len(keys) > 1 else None)
                for k in keys:
                    self.slots[k].role = 'lr' if k == self.lr_key else 'ud'
                self.axis = {'status': 'skip', 'lr': self.slots[self.lr_key].port,
                             'ud': self.slots[self.ud_key].port if self.ud_key else '',
                             'text': '已跳过角色自检（--no-axis-check），按 --lr/--ud 或 COM 号顺序分配'}
                _set(axis=self.axis)
            self.run_capture()
            self.save()
            if not self.args.no_analyze:
                report = analyze(self)
                print('\n' + report, flush=True)
        except KeyboardInterrupt:
            print('\n  手动中断，正在保存已采到的帧…', flush=True)
            try:
                self.save()
            except Exception as exc:                 # noqa: BLE001
                print('  保存失败：%r' % exc, flush=True)
        except Exception as exc:                     # noqa: BLE001
            self.err = '%s: %s' % (type(exc).__name__, exc)
            _set(err=self.err)
            print('\n!! %s' % self.err, flush=True)
        finally:
            _set(phase='done', seg_text='采集结束', remain=0, dur=0)


# ------------------------------------------------------------------ 分析

def _table(rows, head):
    w = [len(str(h)) for h in head]
    for r in rows:
        for i, c in enumerate(r):
            w[i] = max(w[i], len(str(c)))
    line = '  '.join(str(h).ljust(w[i]) for i, h in enumerate(head))
    out = [line, '-' * len(line)]
    for r in rows:
        out.append('  '.join(str(c).ljust(w[i]) for i, c in enumerate(r)))
    return '\n'.join(out)


def analyze_lane(runner):
    key = runner.lr_key
    if not key:
        return None, []
    s = runner.slots[key]
    mid_segs = ['rest', 'lane_back1', 'lane_back2', 'lane_back3', 'lane_back4']
    mid_vals = [v for seg in mid_segs for v in s.seg_vals(seg)]
    if len(mid_vals) < 10:
        return {'ok': None, 'text': '左右：中线帧不足（%d 帧），无法判定' % len(mid_vals)}, []
    mid = float(np.median(mid_vals))
    mid_spread = float(np.percentile(mid_vals, 95) - np.percentile(mid_vals, 5))
    rows = []
    verdict = []
    for seg, name in (('lane_left', '左移-1'), ('lane_left2', '左移-2'),
                      ('lane_right', '右移-1'), ('lane_right2', '右移-2')):
        v = s.seg_vals(seg)
        if len(v) < 5:
            rows.append((name, len(v), '—', '—', '—', '帧不足'))
            continue
        p50 = float(np.median(v))
        d = p50 - mid
        ok = abs(d) >= LANE_GATE_DEG
        rows.append((name, len(v), '%+.1f' % p50, '%+.1f' % d,
                     '%.1f' % (float(np.percentile(v, 95)) - float(np.percentile(v, 5))),
                     '过门限' if ok else '未过'))
        verdict.append(ok)
    passed = sum(1 for x in verdict if x)
    total = len(verdict)
    text = ('左右：中线 %+.1f°（5–95%% 跨 %.1f°），门限 ±%.1f°（游戏 0.22×20°）。'
            '%d/%d 个动作段过门限。'
            % (mid, mid_spread, LANE_GATE_DEG, passed, total))
    if total and passed == total and mid_spread < LANE_GATE_DEG:
        text += ' 结论：左右可分，且中线抖动小于门限。'
    elif total and passed == total:
        text += ' 注意：中线抖动 %.1f° 已接近门限，静息误触发风险偏高。' % mid_spread
    elif total:
        text += ' 结论：至少一侧没过门限，先把跨步幅度加大到 20~30 cm 重跑。'
    return {'ok': passed == total and total > 0, 'text': text, 'rows': rows,
            'mid': mid, 'mid_spread': mid_spread, 'passed': passed, 'total': total}, rows


def analyze_ud(runner):
    key = runner.ud_key
    if not key:
        return {'ok': None, 'text': '跳蹲：没有竖直板，未采'}, []
    per = {}
    for seg, ev in runner.events:
        per.setdefault(seg, []).append(ev.lower())

    def cnt(seg, kind):
        return sum(1 for e in per.get(seg, []) if e == kind)

    rows = [
        ('静息 rest', cnt('rest', 'jump') + cnt('rest', 'duck'), 0, '应为 0'),
        ('直接起跳 jump', cnt('jump', 'jump'), '≥2', '3 次动作'),
        ('中蹲 duck_mid', cnt('duck_mid', 'duck'), '≥2', '3 次动作'),
        ('全蹲 duck_full', cnt('duck_full', 'duck'), '≥2', '3 次动作'),
        ('浅蹲 duck_shallow', cnt('duck_shallow', 'duck'), '≤1', '不该算蹲'),
        ('负样本 弯腰', len(per.get('neg_bend', [])), 0, '不该触发'),
        ('负样本 踏步', len(per.get('neg_march', [])), 0, '不该触发'),
        ('负样本 转身', len(per.get('neg_turn', [])), 0, '不该触发'),
    ]
    hits = (cnt('jump', 'jump') >= 2) + (cnt('duck_mid', 'duck') >= 2) + (cnt('duck_full', 'duck') >= 2)
    false_pos = (cnt('duck_shallow', 'duck') > 1) + (cnt('rest', 'jump') + cnt('rest', 'duck') > 0)
    neg_fp = sum(1 for seg in ('neg_bend', 'neg_march', 'neg_turn') if per.get(seg))
    text = ('跳蹲：主项 %d/3 命中（跳/中蹲/全蹲），浅蹲与静息误报 %d 处，'
            '负样本触发 %d/3 类。' % (hits, false_pos, neg_fp))
    ok = hits == 3 and false_pos == 0
    return {'ok': ok, 'text': text, 'rows': rows, 'per': per}, rows


def analyze(runner):
    lane, lane_rows = analyze_lane(runner)
    ud, ud_rows = analyze_ud(runner)
    lines = ['# 双雷达现场验证报告', '',
             '生成：%s' % time.strftime('%Y-%m-%d %H:%M:%S'), '']

    lines += ['## 1. 角色与摆位', '']
    ax = runner.axis
    lines += ['- 状态：%s' % ax.get('status'), '- 左右板：%s' % (ax.get('lr') or '未接入'),
              '- 跳蹲板：%s' % (ax.get('ud') or '未接入'), '- 判定依据：%s' % ax.get('text', ''), '']
    if ax.get('d_lr'):
        rows = [(k, v, ax['d_ud'].get(k, '—')) for k, v in ax['d_lr'].items()]
        lines += ['```', _table(rows, ('端口', '左右响应°', '上下响应°')), '```', '']

    lines += ['## 2. 左右换道', '']
    if lane:
        lines += [lane['text'], '']
        if lane_rows:
            lines += ['```', _table(lane_rows, ('动作段', '帧数', '中位角度°', '相对中线°', '5–95%跨°', '判定')), '```', '']

    lines += ['## 3. 跳 / 蹲', '']
    if ud:
        lines += [ud['text'], '']
        if ud_rows:
            lines += ['```', _table(ud_rows, ('项目', '命中数', '期望', '说明')), '```', '']
        if ud.get('per'):
            lines += ['逐段事件：', '']
            for seg in sorted(ud['per']):
                lines.append('- %s：%s' % (seg, '、'.join(e.upper() for e in ud['per'][seg])))
            lines.append('')

    lines += ['## 4. 口径与限制', '',
              '- 所有结论来自本次现场采集，样本量见各段帧数（7.6 fps，1 s ≈ 7.6 帧）。',
              '- 左右门限 4.4° = 游戏 `enterThreshold 0.22` × `scale-deg 20°`。',
              '- 跳蹲判据用 `jumpduck_fusion.JumpDuckFusion`（V_JUMP 0.32 / V_DUCK 0.35 / NZ_T 0.20 / defer 2）逐帧回放。',
              '- 两块板同时工作会有相互注入，报告里的误报需扣掉这一项再下结论。',
              '- 阈值仍属本项目提案，待小组确认。', '']

    text = '\n'.join(lines)
    if runner.out_dir:
        with open(os.path.join(runner.out_dir, 'report.md'), 'w', encoding='utf-8') as fh:
            fh.write(text)
        print('报告：%s' % os.path.join(runner.out_dir, 'report.md'), flush=True)
    return text


# ------------------------------------------------------------------ HTTP

def snapshot():
    with LOCK:
        return json.loads(json.dumps(WEB))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith('/state'):
            body = json.dumps(snapshot(), ensure_ascii=False).encode('utf-8')
            ctype = 'application/json; charset=utf-8'
        else:
            with open(os.path.join(HERE, 'dual_guide.html'), encoding='utf-8') as f:
                body = f.read().encode('utf-8')
            ctype = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


# ------------------------------------------------------------------ 入口

def parse_band(s):
    if not s:
        return None
    lo, hi = [int(x) for x in s.replace(' ', ',').split(',')]
    return (lo, hi)


def main():
    ap = argparse.ArgumentParser(description='双板联合采集引导（左右 + 跳蹲）')
    ap.add_argument('--lr', default='auto', help='水平板串口，如 COM3；auto = 自动发现')
    ap.add_argument('--ud', default='auto', help='竖直板串口，如 COM5；auto = 自动发现')
    ap.add_argument('--rounds', type=int, default=1)
    ap.add_argument('--no-axis-check', action='store_true', help='跳过角色自检')
    ap.add_argument('--part', choices=('all', 'lane', 'ud'), default='all',
                    help='只跑一部分：lane=左右，ud=跳蹲，all=全部（默认）')
    ap.add_argument('--band-lr', default=None, help='水平板距离带，如 1,3')
    ap.add_argument('--band-ud', default=None, help='竖直板距离带，如 1,3')
    ap.add_argument('--out', default=os.path.join(ROOT, 'data', 'dual_guide'))
    ap.add_argument('--web-port', type=int, default=PORT)
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--simulate', action='store_true')
    ap.add_argument('--speed', type=float, default=1.0,
                    help='段时长缩放倍数（自测用，如 --speed 20；真实采集保持 1）')
    ap.add_argument('--no-open', action='store_true')
    ap.add_argument('--no-speak', action='store_true')
    ap.add_argument('--no-analyze', action='store_true')
    a = ap.parse_args()

    if a.no_speak:
        V.NO_SPEAK = True
    a.band_lr = parse_band(a.band_lr)
    a.band_ud = parse_band(a.band_ud)

    if a.list:
        ports = V.list_ports()
        if not ports:
            print('  （没有发现串口）')
        for dev, desc, hwid in ports:
            print('  %-6s %-42s %s%s' % (dev, desc, hwid,
                                         '  <- CP210x/1T2R 指纹'
                                         if V._is_1t2r(desc, hwid) else ''))
        return 0

    slots = {}
    if a.simulate:
        if a.speed > 1:
            # 段时长缩了 N 倍，合成数据的出帧间隔也同比缩短，保证每段帧数不变
            V.FRAME_PERIOD = max(0.004, V.FRAME_PERIOD / a.speed)
        slots['A'] = SimSlot('A', 'lr')
        slots['B'] = SimSlot('B', 'ud')
        boot = '合成数据（--simulate）'
    else:
        found = discover_ports()
        if a.lr != 'auto' and a.ud != 'auto':
            want = [a.lr, a.ud]
        elif a.lr != 'auto':
            want = [a.lr] + [p for p in found if p != a.lr][:1]
        elif a.ud != 'auto':
            want = [p for p in found if p != a.ud][:1] + [a.ud]
        else:
            want = found[:2]
        want = [w for w in want if w]
        if not want:
            print('!! 没有发现串口。检查：USB 插紧、CP210x 驱动、设备管理器里有 COM 口。')
            return 3
        print('\n  使用的串口：%s' % '、'.join(want))
        try:
            for i, p in enumerate(want):
                slots['AB'[i]] = open_slot('AB'[i], p)
        except Exception as exc:                     # noqa: BLE001
            print('!! 打开串口失败：%r' % exc)
            print('   常见原因：被其它程序占用（manager / radar_live_ws / 浏览器页面）。')
            for s in slots.values():
                s.close()
            return 3
        boot = '、'.join('%s=%s' % (k, s.port) for k, s in slots.items())

    srv = ThreadingHTTPServer(('127.0.0.1', a.web_port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print('')
    print('  双板联合采集引导')
    print('  串口：%s' % boot)
    print('  界面：http://127.0.0.1:%d/' % a.web_port)
    print('  关闭这个窗口 = 结束采集')
    print('')
    print('  摆位（11/17 基线）：水平板横放、人站 1.0 m；竖直板竖放、离地 0.5 m、人站 1.2 m。')
    print('')

    if not a.no_open:
        try:
            webbrowser.open('http://127.0.0.1:%d/' % a.web_port)
        except Exception:
            pass

    runner = Runner(a, slots)
    try:
        runner.run()
    finally:
        for s in slots.values():
            s.close()
        srv.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
