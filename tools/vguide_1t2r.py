# -*- coding: utf-8 -*-
r"""1T2R 竖直方向采集引导（第二块板专用，独立于水平方向的采集链路）。

为什么要单独一个程序
--------------------
`radar-pointcloud/` 里那套（segment_capture / lr_guide / radar_actions）是
**水平左右移**已经验收过的链路，本项目约定不再改动。这里只**只读复用**
`radar_protocol.py` 的字节解析器，其余全部自写，因此不会影响旧板、旧程序。

1T2R 为什么能测竖直
-------------------
固件只上传原始复数 IQ（8 bin × 64 chirp × 2 RX），**没有任何角度字段**。
水平方位角是上位机用两个 RX 的相位差算的：

    Δφ = 2π·d·sinθ / λ        d = 6.25 mm, λ = 12.43 mm  → Δφ ≈ 3.16·sinθ

把**第二块板绕光轴旋转 90°**（让两个 RX 的连线竖直），同一个公式给出的
θ 就从「方位角」变成「俯仰角」——这是 1T2R 测竖直的唯一物理路径。
板子平放时，上下移动几乎不改变相位差，程序会当场判出来并提示。

程序流程
--------
  1. 自动/手动选串口（两块 1T2R 的 USB VID:PID 相同，认不出新旧，只能按口名区分）
  2. **安装自检**：站定 4 s → 双手上举 4 s → 下蹲 4 s → 站定 3 s
     用「上举/下蹲的俯仰角是否反向变化」判定这块板能不能测竖直
  3. **正式采集**：（站好 → 跳 → 站好 → 蹲 → 站好）× N 轮，全程语音+倒计时
  4. **立即分析**：逐特征给分离度（Cohen's d + 留一最近质心准确率），出 report.md

用法
----
    python tools\vguide_1t2r.py --port auto                 # 插上就自动跑
    python tools\vguide_1t2r.py --port COM5 --rounds 3
    python tools\vguide_1t2r.py --selftest-only             # 只做安装自检
    python tools\vguide_1t2r.py --simulate --no-open         # 不碰硬件，跑合成数据
    python tools\vguide_1t2r.py --list                       # 只列串口

产出
----
    data/vguide_1t2r/<时间戳>/
        frames.npz   z=(N,2,64,8) complex64, t=(N,), label=(N,)
        labels.json  段边界 + 自检结论 + 采集参数
        metrics.csv  每帧特征（相位差/俯仰角/主 bin/质心/径向速度/带能量占比）
        report.md    能不能测竖直、哪个特征可分、建议阈值（提案）
"""
import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))

from radar_protocol import StreamParser, FrameAssembler  # noqa: E402

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ---------------------------------------------------------------- 物理常量
BAUD = 921600
BINS = 8
CHIRPS = 64
N_RX = 2
RANGE_RES = 0.6813            # c/(2B), B=220 MHz（Radar_Config.ini Ver 1.1.9）
FRAME_PERIOD = 0.1295         # 1T2R 实测 129.5 ms/帧
C_LIGHT = 299792458.0
F0 = 24.125e9
LAM = C_LIGHT / F0            # 12.426 mm
D_ANT = 6.250e-3              # 1T2R 水平阵元间距
V_RES = 0.0480                # 多普勒分辨率 m/s（64 chirp）
V_MAX = 1.535
K_PHI = 2.0 * math.pi * D_ANT / LAM      # ≈3.160 rad per unit sinθ

# 汉宁窗（不依赖 numpy 里可能变动的窗函数接口）
WIN = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(CHIRPS) / (CHIRPS - 1))).astype(np.float32)

PORT = 8772
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
    'bin_width': RANGE_RES,
    'peak_bin': -1,
    'range_m': 0.0,
    'energy': [],
    'dphi': 0.0,
    'angle_deg': 0.0,
    'v_r': 0.0,
    'band_share': 0.0,
    'hist': [],
    'axis': 'unknown',
    'check_status': 'pending',
    'check_text': '等待安装自检',
    'check_resp': 0.0,
    'port': '',
    'board': '1T2R (DEV202H)',
    'done': False,
    'err': '',
}

SEGMENTS = [
    ('base1', '站好，别动', 4.0),
    ('jump', '开始跳：连跳 3 次，动作放慢', 8.0),
    ('base2', '停，站好别动', 4.0),
    ('duck', '开始蹲：连续蹲 3 次，动作放慢', 8.0),
    ('base3', '停，站好别动', 4.0),
]

SELFCHECK = [
    ('stand1', '自检：站好，别动', 4.0),
    ('jump', '自检：连跳 3 次，动作放慢', 6.0),
    ('duck', '自检：连蹲 3 次，动作放慢', 6.0),
    ('stand2', '自检：站起来，站好别动', 4.0),
]

# 轴向判别：左右横移 vs 上下举蹲，谁有「反向且幅度大」的响应，基线就朝哪。
# 不依赖人对「绕光轴转 90°」的理解，直接测出来。
AXIS_TEST = [
    ('stand1', '站好别动，先定基线', 4.0),
    ('left', '向左横移一步，站住别动', 5.0),
    ('right', '向右横移一步，站住别动', 5.0),
    ('up', '双手举高，站住别动', 5.0),
    ('down', '慢慢蹲下，站住别动', 5.0),
    ('stand2', '站起来，回到中间站好', 4.0),
]


def _set(**kw):
    with LOCK:
        WEB.update(kw)


# ------------------------------------------------------------------ 语音/蜂鸣

NO_SPEAK = False


def _ps_literal(s):
    """纯 ASCII 的 PowerShell 字符串表达式（按码点 join），避免中文编码损坏。"""
    return '(-join @(' + ','.join('[char]%d' % ord(c) for c in s) + '))'


def speak(text):
    if NO_SPEAK or not text:
        return
    try:
        script = ('Add-Type -AssemblyName System.Speech;'
                  '$v=New-Object System.Speech.Synthesis.SpeechSynthesizer;'
                  '$v.Rate=1;$v.Speak(%s)' % _ps_literal(text))
        import base64
        enc = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        kw = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        if hasattr(subprocess, 'CREATE_NO_WINDOW'):
            kw['creationflags'] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(['powershell', '-NoProfile', '-NonInteractive',
                          '-EncodedCommand', enc], **kw)
    except Exception:
        pass


def beep(ok=True):
    try:
        import winsound
        winsound.Beep(880 if ok else 440, 180)
    except Exception:
        pass


def tick():
    try:
        import winsound
        winsound.Beep(1400, 70)
    except Exception:
        pass


# ------------------------------------------------------------------ 串口选择

def list_ports():
    try:
        from serial.tools import list_ports as lp
    except Exception:
        return []
    try:
        return [(p.device, p.description or '', p.hwid or '') for p in lp.comports()]
    except Exception:
        return []


def _is_1t2r(desc, hwid):
    blob = ('%s %s' % (desc or '', hwid or '')).lower()
    return any(k in blob for k in ('10c4:ea60', 'cp210', 'silicon labs', 'silabs'))


def pick_port(requested):
    """返回 (port, 说明)。两块 1T2R 的 VID:PID 完全相同，只能按口名区分。"""
    ports = list_ports()
    if requested and str(requested).strip().lower() not in ('auto', ''):
        want = str(requested).strip().upper()
        hit = [p for p in ports if p[0].upper() == want]
        if not hit:
            raise SystemExit('!! 系统里没有 %s。当前串口：%s'
                             % (want, '、'.join(p[0] for p in ports) or '（一个都没有）'))
        return want, ('--port 指定' if _is_1t2r(hit[0][1], hit[0][2])
                      else '--port 指定（不是 CP210x 指纹，按你给的用）')

    cands = [p for p in ports if _is_1t2r(p[1], p[2])]
    if not cands:
        cands = ports
    if not cands:
        raise SystemExit('!! 没有发现任何串口。先确认：USB 插紧、装了 CP210x 驱动、'
                         '设备管理器里能看到 COM 口。')
    if len(cands) == 1:
        d = cands[0]
        return d[0], '自动识别（唯一候选）：%s | %s' % (d[1], d[2])

    # 多个候选：两块 1T2R 的指纹一样，必须由人来指定新板的口
    print('\n  发现多个串口候选：')
    for i, (dev, desc, hwid) in enumerate(cands, 1):
        print('    %d) %-6s %s  [%s]' % (i, dev, desc, hwid))
    print('  两块 1T2R 的 USB 指纹相同，程序无法分辨新旧。')
    print('  建议：先把旧板拔掉，只插新板，再跑一次；或用 --port COMx 直接指定。')
    sel = ''
    try:
        sel = input('  请输入新板端口序号（回车退出）: ').strip()
    except EOFError:
        sel = ''
    if not sel:
        raise SystemExit('!! 已退出：请用 --port COMx 指定新板端口。')
    try:
        i = int(sel) - 1
        d = cands[i]
    except Exception:
        raise SystemExit('!! 序号无效。')
    return d[0], '手动选择：%s | %s' % (d[1], d[2])


# ------------------------------------------------------------------ 读帧

class Reader:
    """串口原始流 → (2, 64, 8) 复数帧。1T2R 上电即出流，不需要下发配置。"""

    def __init__(self, ser):
        self.ser = ser
        self.parser = StreamParser()
        self.asm = FrameAssembler()
        self.bad = 0
        self.dropped = 0

    def poll(self):
        out = []
        try:
            n = self.ser.in_waiting
        except Exception:
            return out
        if not n:
            time.sleep(0.004)
            return out
        try:
            chunk = self.ser.read(n)
        except Exception:
            return out
        for rf in self.parser.feed(chunk):
            z = self.asm.push(rf)
            if z is None:
                continue
            z0, z1 = z
            out.append(np.stack([z0, z1], axis=0).astype('complex64'))
        self.bad = getattr(self.parser, 'bad_packets', 0)
        self.dropped = getattr(self.asm, 'dropped', 0)
        return out

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class SimReader:
    """合成数据：不碰硬件，用来端到端验证程序本身。

    按「当前想要的俯仰角」合成两 RX 的相位差，噪声按实测 SNR 量级给。
    """

    def __init__(self, seed=7, bin_lo=1, bin_hi=3):
        self.rng = np.random.default_rng(seed)
        self.dphi = 0.15
        self.vr = 0.0
        self.bad = 0
        self.dropped = 0
        self.bin_lo = bin_lo
        self.bin_hi = bin_hi
        self.t_last = 0.0

    def set_dphi(self, v):
        self.dphi = float(v)

    def set_vr(self, v):
        self.vr = float(v)

    def poll(self):
        now = time.time()
        if now - self.t_last < FRAME_PERIOD:
            time.sleep(0.004)
            return []
        self.t_last = now
        z = (self.rng.normal(0, 1, (2, CHIRPS, BINS))
             + 1j * self.rng.normal(0, 1, (2, CHIRPS, BINS))).astype('complex64') * 12.0
        b = int(round(self.rng.normal((self.bin_lo + self.bin_hi) / 2.0, 0.25)))
        b = max(1, min(BINS - 1, b))
        amp = 260.0
        dph = self.dphi + float(self.rng.normal(0, 0.02))
        fchirp = self.vr / V_RES
        for c in range(CHIRPS):
            ph = 2 * math.pi * fchirp * c / CHIRPS
            z[0, c, b] += amp * np.exp(1j * ph)
            z[1, c, b] += amp * np.exp(1j * (ph + dph))
        return [z]

    def close(self):
        pass


# ------------------------------------------------------------------ 特征

def frame_feats(z, band):
    """单帧特征：两 RX 在距离-多普勒图上的相位差 → 角度。

    - dphi_peak：带内最强单元的相位差（锐利，但跳/蹲跨 bin 时会跳）
    - dphi_band：带内按功率加权的圆平均（稳，主指标）
    - v_r：带内能量加权的平均径向速度
    """
    lo, hi = int(band[0]), int(band[1])
    lo = max(0, lo)
    hi = min(BINS, hi)
    if hi <= lo:
        return None
    z0 = z[0]
    z1 = z[1]
    sp0 = np.fft.fftshift(np.fft.fft(z0 * WIN[:, None], axis=0), axes=0)
    sp1 = np.fft.fftshift(np.fft.fft(z1 * WIN[:, None], axis=0), axes=0)
    P = np.abs(sp0) ** 2 + np.abs(sp1) ** 2
    Pb = P[:, lo:hi]
    tot = float(Pb.sum())
    if tot <= 0:
        return None
    w1 = Pb.shape[1]
    k = int(np.argmax(Pb))
    i, j = divmod(k, w1)
    j += lo
    dphi_peak = float(np.angle(sp1[i, j] * np.conj(sp0[i, j])))

    # 加权只在「能量最高的 3 个 bin」上做：宽带的场合（自检用的是全带 1..8），
    # 若把弱 bin 一起平均，另一侧显示器/机箱的静止强反射会把人拉偏。
    # 正式采集时 band 通常只有 2~3 个 bin，不触发这一步，行为不变。
    prof_all = Pb.sum(axis=0)
    if prof_all.size > 3:
        keep = np.argsort(prof_all)[-3:]
        keep.sort()
        Pb = Pb[:, keep]
        dsel = sp1[:, lo:hi] * np.conj(sp0[:, lo:hi])
        dsel = dsel[:, keep]
    else:
        dsel = sp1[:, lo:hi] * np.conj(sp0[:, lo:hi])
    d = dsel
    vec = (Pb * np.exp(1j * np.angle(d))).sum()
    dphi_band = float(np.angle(vec)) if abs(vec) > 0 else 0.0

    idx = (np.arange(CHIRPS) - CHIRPS // 2).astype(np.float64) * V_RES
    wv = Pb.sum(axis=1)
    v_r = float((wv * idx).sum() / wv.sum())
    # 多普勒谱宽：垂直速度的展宽量。跳(1m/s)与蹲(0.5m/s)差约 2 倍，
    # 是 64 chirp / 129.5ms CPI 下唯一稳定可分的量（实测 d=1.88，AUC 91.8%）。
    doppler_width = float(np.sqrt((wv * (idx - v_r) ** 2).sum() / wv.sum()))
    # 零多普勒外能量占比：动作门控。静息≈0.00，跳/蹲 0.4 上下。
    dc_mask = np.abs(idx) < 0.15
    nz_share = 1.0 - float(Pb[dc_mask].sum() / max(tot, 1e-9))

    # 显示/质心仍用带内全部 bin，只有相位加权收窄到 top-3
    prof = prof_all
    bins = np.arange(lo, hi, dtype=np.float64)
    cen = float((prof * bins).sum() / max(prof.sum(), 1e-12))

    s = max(-1.0, min(1.0, dphi_band / K_PHI))
    ang = math.degrees(math.asin(s))

    return {
        'dphi_peak': dphi_peak,
        'dphi': dphi_band,
        'ang_deg': ang,
        'peak_bin': int(j),
        'range_m': (j + 0.5) * RANGE_RES,
        'centroid_bin': cen,
        'v_r': v_r,
        'doppler_width': doppler_width,
        'nz_share': nz_share,
        'band_power': tot,
        'total_power': float(P.sum()),
        'profile': prof,
    }


class ActionJudge:
    """把逐帧谱宽/门控累计成一次动作事件。

    依据 2026-11-12 真实数据（data/vguide_1t2r/20261112-203419）：
      * 静息零多普勒外能量占比 ≈ 0.00，动作段 ≈ 0.4 → 用 0.30 做门控；
      * 活跃帧的 5 帧滑窗谱宽：跳 0.308 / 蹲 0.202 → 阈值 0.255；
      * 事件级留一验证：第 2、3 轮各 5/5、6/6 全对。
    阈值均为本批数据的提案值，待更多样本确认。
    """

    def __init__(self, gate=0.30, thr=0.255, lo=0.16, win=5, min_frames=8):
        self.gate = gate
        self.thr = thr
        self.lo = lo
        self.win = win
        self.min_frames = min_frames
        self.nz_buf = []
        self.w_buf = []
        self.act_w = []
        self.act_len = 0

    def push(self, nz, w):
        """喂一帧；完成一次动作时返回 (label, width_mean, frames)，否则 None。"""
        self.nz_buf.append(float(nz))
        self.w_buf.append(float(w))
        if len(self.nz_buf) > self.win:
            self.nz_buf = self.nz_buf[-self.win:]
            self.w_buf = self.w_buf[-self.win:]
        nz_s = sum(self.nz_buf) / len(self.nz_buf)
        w_s = sum(self.w_buf) / len(self.w_buf)
        if nz_s > self.gate:
            self.act_w.append(w_s)
            self.act_len += 1
            return None
        if self.act_len >= self.min_frames:
            m = sum(self.act_w) / len(self.act_w)
            nf = self.act_len
            self.act_w = []
            self.act_len = 0
            # 谱宽太窄说明只是轻微晃动/走动，不构成明确的跳或蹲
            if m < self.lo:
                return None
            return ('JUMP' if m > self.thr else 'DUCK', m, nf)
        self.act_w = []
        self.act_len = 0
        return None


def profile_energy(prof):
    mx = float(np.max(prof)) or 1.0
    return [int(round(100.0 * float(v) / mx)) for v in prof]


# ------------------------------------------------------------------ 采集主流程

class Runner:
    def __init__(self, args, reader):
        self.args = args
        self.reader = reader
        self.zs = []
        self.ts = []
        self.labels = []
        self.marks = []
        self.feats = []
        self.t0 = time.time()
        self.out_dir = None
        self.err = ''
        self.check = {'status': 'pending', 'axis': 'unknown', 'text': '未执行',
                      'base': None, 'up': None, 'down': None, 'resp': 0.0}
        self.band = (1, min(3, BINS))
        self._hist = []
        self.judge = ActionJudge()
        self.actions = []
        self._last_pub_n = -1

    # ---- 单帧轮询 ----
    def _pump(self, seconds, label, band=None, sim_angle=None):
        band = band or self.band
        end = time.time() + seconds
        while time.time() < end:
            rec = time.time() - self.t0
            for z in self.reader.poll():
                self.zs.append(z)
                self.ts.append(rec)
                self.labels.append(label)
                f = frame_feats(z, band)
                self.feats.append(f)
            self._publish(rec, max(0.0, end - time.time()), band)
        return len(self.zs)

    def _publish(self, rec, remain, band):
        n = len(self.zs)
        fps = 0.0
        if n >= 8:
            span = self.ts[-1] - self.ts[0]
            if span > 1.0:
                fps = (n - 1) / span
        upd = {'frames': n, 'fps': round(fps, 1), 'remain': round(remain, 1),
               'bin_width': RANGE_RES}
        if self.feats and self.feats[-1] and n != self._last_pub_n:
            self._last_pub_n = n
            f = self.feats[-1]
            self._hist.append(round(f['v_r'], 4))
            if len(self._hist) > 160:
                self._hist = self._hist[-160:]
            recent = self._hist[-20:]
            upd.update({
                'peak_bin': f['peak_bin'],
                'range_m': round(f['range_m'], 2),
                'energy': profile_energy(f['profile']),
                'dphi': round(f['dphi'], 3),
                'angle_deg': round(f['ang_deg'], 1),
                'v_r': round(f['v_r'], 2),
                'doppler_width': round(f['doppler_width'], 3),
                'nz_share': round(f['nz_share'], 3),
                'vr_up': round(max(recent), 2) if recent else 0.0,
                'vr_dn': round(min(recent), 2) if recent else 0.0,
                'vr_base': round(float(np.median(recent)), 2) if recent else 0.0,
                'band_share': round(100.0 * f['band_power'] /
                                    max(f['total_power'], 1e-9), 1),
                'hist': list(self._hist),
            })
            ev = self.judge.push(f['nz_share'], f['doppler_width'])
            if ev:
                self.actions.append({'label': ev[0], 'width': round(ev[1], 3),
                                     'frames': ev[2], 'n': n})
                upd.update(action=ev[0], action_w=round(ev[1], 3))
        _set(**upd)

    # ---- 段级统计 ----
    def _seg_feats(self, i0, i1):
        return [f for f in self.feats[i0:i1] if f]

    def _bin_mode(self):
        """自检段主 bin 的众数占比：低于 70% 说明目标跨 bin，测角会跳。"""
        from collections import Counter
        v = [self.feats[i]['peak_bin'] for i in range(len(self.feats))
             if self.feats[i] and self.labels[i].startswith('check_')]
        if not v:
            return None
        c = Counter(v)
        b, n = c.most_common(1)[0]
        return {'bin': int(b), 'share': round(100.0 * n / len(v), 1)}

    def run_selfcheck(self):
        """安装自检：站定 / 上举 / 下蹲 的俯仰角必须反向变化。"""
        a = self.args
        band = (1, BINS)          # 自检不挑带，避开 bin0 近场泄漏
        _set(phase='prep', text='安装自检：站到雷达正前方约 1.2 m', remain=4.0, dur=4.0)
        speak('安装自检。请站到雷达正前方约一点二米处，站好不要动。')
        self._pump(4.0, 'check_idle', band)

        for seg, hint, sec in SELFCHECK:
            if self.args.simulate:
                self._sim_set(seg)
            _set(phase='check', text=hint, seg=seg, remain=sec + 1.4, dur=sec + 1.4)
            speak(hint)
            self._pump(1.4, 'check_idle', band)      # 播报，不计入
            beep(seg in ('stand1', 'stand2'))
            time.sleep(0.3)
            i0 = len(self.zs)
            self._pump(sec, 'check_' + seg, band)
            i1 = len(self.zs)
            self.marks.append({'kind': 'selfcheck', 'seg': 'check_' + seg,
                               'frame_start': i0, 'frame_end': max(i0, i1 - 1),
                               'frames': i1 - i0})

        def values(seg, key):
            return [self.feats[i][key] for i in range(len(self.feats))
                    if self.feats[i] and self.labels[i] == 'check_' + seg]

        def med_of(seg, key):
            v = values(seg, key)
            return float(np.median(v)) if v else float('nan')

        def med_of(seg, key):
            v = values(seg, key)
            return float(np.median(v)) if v else float('nan')

        def pctl(seg, key, q):
            v = values(seg, key)
            return float(np.percentile(v, q)) if v else float('nan')

        def r2(x):
            return None if (x is None or (isinstance(x, float) and math.isnan(x))) else round(x, 3)

        # 仰视几何：跳起 = 远离雷达 = v_r 正；蹲下 = 靠近 = v_r 负。
        # 段内动作只占两三帧，用 p90/p10 抓峰值；中位数会被静息帧稀释成 0。
        base_hi = pctl('stand1', 'v_r', 90)
        base_lo = pctl('stand1', 'v_r', 10)
        b2_hi = pctl('stand2', 'v_r', 90)
        b2_lo = pctl('stand2', 'v_r', 10)
        if not math.isnan(b2_hi):
            base_hi = b2_hi if math.isnan(base_hi) else 0.5 * (base_hi + b2_hi)
        if not math.isnan(b2_lo):
            base_lo = b2_lo if math.isnan(base_lo) else 0.5 * (base_lo + b2_lo)
        j_hi = pctl('jump', 'v_r', 90)
        d_lo = pctl('duck', 'v_r', 10)

        up_resp = j_hi - base_hi
        dn_resp = base_lo - d_lo
        resp = 0.0
        status = 'none'
        axis = 'doppler'
        text = ''

        if math.isnan(up_resp) or math.isnan(dn_resp):
            text = '自检没采到足够帧（检查串口/是否被占用）'
        else:
            resp = max(up_resp, dn_resp)
            noise = max(0.05, base_hi - base_lo)
            if up_resp >= 0.20 and dn_resp >= 0.20:
                status = 'ok'
                text = ('有多普勒响应：跳 +%.2f m/s、蹲 −%.2f m/s（静息底噪 ±%.2f）'
                        '→ 仰视几何可用，可以采'
                        % (up_resp, dn_resp, noise / 2))
            elif up_resp >= 0.10 and dn_resp >= 0.10:
                status = 'weak'
                text = ('有多普勒响应但偏小：跳 +%.2f、蹲 −%.2f m/s。'
                        '把板子再抬高一点、或人站近一点，再跑一次自检'
                        % (up_resp, dn_resp))
            elif resp < 0.05:
                status = 'none'
                text = ('跳和蹲都没有出现径向速度（峰值 %.2f m/s）。'
                        '雷达多半还是平视、人不在垂直波束内；'
                        '把雷达降到约 0.5 m、板面向上抬约 20°' % max(0.0, resp))
            else:
                status = 'partial'
                text = ('只有一个方向有响应（跳 %+.2f、蹲 %+.2f m/s）。'
                        '多半是蹲的幅度太小、或蹲下时人往后坐，加大幅度重跑'
                        % (up_resp, dn_resp))

        self.check = {
            'status': status, 'axis': axis, 'text': text,
            'vr_base_hi': r2(base_hi), 'vr_base_lo': r2(base_lo),
            'vr_jump_p90': r2(j_hi), 'vr_duck_p10': r2(d_lo),
            'up_resp': r2(up_resp), 'dn_resp': r2(dn_resp), 'resp': round(resp, 3),
            'dphi_ref': {'stand1': r2(med_of('stand1', 'ang_deg')),
                         'jump': r2(med_of('jump', 'ang_deg')),
                         'duck': r2(med_of('duck', 'ang_deg'))},
            'peak_bin_mode': self._bin_mode(),
        }
        color = {'ok': 'ok', 'weak': 'warn', 'partial': 'warn',
                 'none': 'bad', 'drift': 'warn'}.get(status, 'warn')
        _set(check_status=color, check_text=text, axis=axis, check_resp=round(resp, 2))
        print('\n  [安装自检] %s' % text, flush=True)
        return status

    def _sim_set(self, seg):
        if not isinstance(self.reader, SimReader):
            return
        # 合成数据里直接注入「想要的俯仰角」，让自检与正式流程都能跑通
        self.reader.set_dphi({
            'stand1': 0.45, 'armsup': 1.55, 'crouch': -0.85, 'stand2': 0.45,
            'jump': 1.35, 'duck': -0.75,
        }.get(seg, 0.45))
        self.reader.set_vr({'jump': 0.55, 'duck': -0.50}.get(seg, 0.0))

    def run_capture(self):
        a = self.args
        if a.alternate:
            a.rounds = 1
            segs = []
            for _ in range(a.alternate):
                segs += [('idle', '站好，别动', 2.0),
                         ('jump', '跳一次', 2.0),
                         ('idle', '站好，别动', 2.0),
                         ('duck', '蹲一次', 2.0),
                         ('idle', '站好，别动', 2.0)]
            base_sec = act_sec = 2.0
            prep_text = ('雷达保持上次的摆法（离地约 0.5 m、仰角不变），人站正前方约 1.2 m。'
                         '接下来：跳一次 → 站好 2 秒 → 蹲一次 → 站好 2 秒')
            prep_say = ('请保持上次的摆法。每个动作只做一次，做完站好，等下一个提示。')
        else:
            segs = SEGMENTS
            base_sec, act_sec = a.sec_base, a.sec_act
            prep_text = '雷达离地约 0.5 m、板面向上抬约 20°；人站到正前方约 1.2 m，站好别动'
            prep_say = ('请确认雷达离地约五十厘米，板面向上抬二十度。'
                        '人站到正前方约一点二米处，站好不要动。')
        seg_total = a.rounds * len(segs)
        _set(phase='prep', text=prep_text,
             remain=max(3.0, a.prep), dur=max(3.0, a.prep), round=0,
             rounds=a.rounds, seg_total=seg_total, seg_no=0)
        speak(prep_say)
        self._pump(max(3.0, a.prep), 'idle', self.band)

        # 用静息段的能量主 bin 定距离带（1T2R 在 1.0 m 站位通常落在 bin1/bin2）
        if a.band:
            self.band = a.band
        else:
            stand = [self.feats[i] for i in range(len(self.feats))
                     if self.feats[i] and self.labels[i] == 'idle']
            if stand:
                bins = [f['peak_bin'] for f in stand if f['peak_bin'] >= 1]
                j = int(round(float(np.median(bins)))) if bins else 1
                lo = max(1, j - 1)
                hi = min(BINS, j + 2)
                self.band = (lo, hi)
        _set(bin_width=RANGE_RES)
        print('  [距离带] bin %d–%d（%.2f–%.2f m）'
              % (self.band[0], self.band[1] - 1,
                 self.band[0] * RANGE_RES, self.band[1] * RANGE_RES), flush=True)

        no = 0
        for r in range(1, a.rounds + 1):
            for seg, hint, default_sec in segs:
                no += 1
                is_base = seg.startswith('base') or seg == 'idle'
                sec = base_sec if is_base else act_sec
                if sec <= 0:
                    sec = default_sec
                _set(phase=('base' if is_base else seg), text=hint, seg=seg,
                     seg_no=no, seg_total=seg_total, round=r, rounds=a.rounds,
                     remain=sec + 1.6, dur=sec + 1.6)
                speak(hint)
                self._pump(1.6, 'idle', self.band)
                beep(is_base)
                time.sleep(0.35)
                if a.simulate:
                    self._sim_set(seg)

                f0 = len(self.zs)
                t_seg = time.time() - self.t0
                shown = None
                end = time.time() + sec
                while time.time() < end:
                    rec = time.time() - self.t0
                    for z in self.reader.poll():
                        self.zs.append(z)
                        self.ts.append(rec)
                        self.labels.append(seg)
                        self.feats.append(frame_feats(z, self.band))
                    self._publish(rec, max(0.0, end - time.time()), self.band)
                    if not is_base and end - time.time() <= 3.2:
                        n = int(end - time.time()) + 1
                        if n != shown and n >= 1:
                            shown = n
                            tick()
                beep(not is_base)
                f1 = len(self.zs)
                self.marks.append({'kind': 'segment', 'round': r, 'seg': seg,
                                   'frame_start': f0, 'frame_end': max(f0, f1 - 1),
                                   'frames': f1 - f0,
                                   't_start': round(t_seg, 2),
                                   't_end': round(time.time() - self.t0, 2)})
                if f1 == f0:
                    _set(err='这一段没收到帧，检查串口是否被其它程序占用')
                print('  [%d/%d] %-6s %d 帧' % (no, seg_total, seg, f1 - f0), flush=True)

    # ---- 主流程 ----
    def run(self):
        a = self.args
        try:
            stamp = time.strftime('%Y%m%d-%H%M%S')
            self.out_dir = os.path.join(a.out, stamp)
            os.makedirs(self.out_dir, exist_ok=True)

            if a.no_selfcheck:
                _set(check_status='pending',
                     check_text='已跳过安装自检，直接采动作数据（轴向待离线分析）',
                     axis='unknown')
                self.check = {'status': 'skipped', 'axis': 'unknown',
                              'text': '已跳过安装自检（--no-selfcheck）',
                              'base': None, 'up': None, 'down': None,
                              'resp': 0.0, 'snr': 0.0}
            elif isinstance(self.reader, SimReader):
                _set(check_status='ok', check_text='合成数据模式：自检结果由模拟器给出',
                     axis='vertical', check_resp=30.0)
                self.check = {'status': 'ok', 'axis': 'doppler',
                              'text': '合成数据（--simulate），非真实硬件结论',
                              'vr_base_hi': 0.05, 'vr_base_lo': -0.05,
                              'vr_jump_p90': 0.55, 'vr_duck_p10': -0.50,
                              'up_resp': 0.50, 'dn_resp': 0.45, 'resp': 0.50}
            else:
                self.run_selfcheck()

            if a.selftest_only:
                self._save()
                _set(phase='done', text='安装自检结束（未采集动作数据）', done=True,
                     remain=0, dur=0)
                return

            self.run_capture()
            self._save()
            if not a.no_analyze:
                report = analyze(self.out_dir, self.check, a)
                print('\n' + report, flush=True)
        except Exception as exc:                     # noqa: BLE001
            self.err = '%s: %s' % (type(exc).__name__, exc)
            _set(err=self.err)
            print('\n!! %s' % self.err, flush=True)
        finally:
            _set(phase='done', text='采集结束', remain=0, dur=0, done=True)

    # ---- 保存 ----
    def _save(self):
        if not self.zs:
            _set(err='没有采到任何帧（未保存）')
            return
        a = self.args
        z = np.stack(self.zs, axis=0)
        np.savez_compressed(os.path.join(self.out_dir, 'frames.npz'),
                            z=z, t=np.asarray(self.ts, float),
                            label=np.asarray(self.labels))
        meta = dict(
            created=time.strftime('%Y-%m-%d %H:%M:%S'),
            port=a.port_used, board='1t2r', simulate=bool(a.simulate),
            chirps=CHIRPS, bins=BINS, rx=N_RX,
            frame_period=FRAME_PERIOD, range_res=RANGE_RES,
            bin_width_m=RANGE_RES, ant_spacing_m=D_ANT, lambda_m=LAM,
            k_phi_rad_per_sin=K_PHI,
            band=list(self.band), rounds=a.rounds,
            sec_base=a.sec_base, sec_act=a.sec_act,
            frames=int(z.shape[0]), shape=list(z.shape[1:]),
            selfcheck=self.check, segments=self.marks,
            note='真值由段边界给出；frame_start/frame_end 是 frames.npz 的行号；'
                 'idle = 播报/准备段，不属于动作真值；'
                 'ang_deg = asin(dphi/k_phi)，只有板子绕光轴转 90° 时才是俯仰角',
        )
        with open(os.path.join(self.out_dir, 'labels.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        rows = ['i,t,label,dphi_rad,ang_deg,peak_bin,range_m,centroid_bin,'
                'v_r_mps,band_power']
        for i, f in enumerate(self.feats):
            if not f:
                continue
            rows.append('%d,%.3f,%s,%.4f,%.2f,%d,%.2f,%.3f,%.3f,%.1f'
                        % (i, self.ts[i], self.labels[i], f['dphi'],
                           f['ang_deg'], f['peak_bin'], f['range_m'],
                           f['centroid_bin'], f['v_r'], f['band_power']))
        with open(os.path.join(self.out_dir, 'metrics.csv'), 'w',
                  encoding='utf-8') as f:
            f.write('\n'.join(rows))

        print('\n已保存 %s（%d 帧 %s）'
              % (self.out_dir, z.shape[0],
                 ' × '.join(str(v) for v in z.shape[1:])), flush=True)


# ------------------------------------------------------------------ 分析

FEATURES = [
    ('ang_deg', '俯仰角（度）'),
    ('dphi', '两 RX 相位差（rad）'),
    ('v_r', '带内平均径向速度（m/s）'),
    ('centroid_bin', '能量质心（bin）'),
    ('peak_bin', '峰值 bin'),
]


def _cohen_d(a, b):
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    if a.size < 2 or b.size < 2:
        return float('nan')
    s = math.sqrt((a.var(ddof=1) + b.var(ddof=1)) / 2.0)
    if s <= 0:
        return float('nan')
    return float((a.mean() - b.mean()) / s)


def _loo_nc_acc(x, y):
    """留一法最近质心准确率（特征先标准化）。"""
    x = np.asarray(x, float).reshape(len(x), -1)
    y = np.asarray(y)
    if len(np.unique(y)) < 2 or len(x) < 6:
        return float('nan')
    mu = x.mean(axis=0)
    sd = x.std(axis=0)
    sd[sd <= 0] = 1.0
    xn = (x - mu) / sd
    ok = 0
    for i in range(len(xn)):
        m = np.ones(len(xn), bool)
        m[i] = False
        cents = {c: xn[m][y[m] == c].mean(axis=0) for c in np.unique(y[m])}
        d = {c: float(np.linalg.norm(xn[i] - v)) for c, v in cents.items()}
        pred = min(d, key=d.get)
        ok += int(pred == y[i])
    return float(ok) / len(xn)


def analyze(out_dir, check, args):
    """读回刚存的数据，给每个特征算分离度，并写 report.md。"""
    meta = {}
    lp = os.path.join(out_dir, 'labels.json')
    if os.path.isfile(lp):
        with open(lp, encoding='utf-8') as f:
            meta = json.load(f)
    d = np.load(os.path.join(out_dir, 'frames.npz'), allow_pickle=True)
    z, t, label = d['z'], d['t'], d['label']
    band = tuple(meta.get('band') or (1, min(3, BINS)))

    feats = [frame_feats(z[i], band) for i in range(len(z))]
    labels = [str(v) for v in label]

    groups = {}
    for i, f in enumerate(feats):
        if not f:
            continue
        groups.setdefault(labels[i], []).append(f)

    lines = []
    add = lines.append
    add('# 1T2R 竖直方向采集报告')
    add('')
    add('- 目录：`%s`' % out_dir)
    add('- 时间：%s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    add('- 板型：1T2R (DEV202H / ICL1122)，%d bin × %d chirp × %d RX'
        % (BINS, CHIRPS, N_RX))
    add('- 距离带：bin %d–%d（%.2f–%.2f m），门距 %.3f m'
        % (band[0], band[1] - 1, band[0] * RANGE_RES,
           band[1] * RANGE_RES, RANGE_RES))
    add('- 帧数：%d，理论帧率 %.1f Hz' % (len(z), 1.0 / FRAME_PERIOD))
    add('- 数据来源：%s' % ('**合成数据（--simulate），不能当作硬件结论**'
                            if meta.get('simulate') else '真实硬件'))
    add('')

    add('## 一、安装自检（仰视几何 + 多普勒）')
    add('')
    st = (check or {}).get('status', 'unknown')
    add('- 结论：**%s**' % {'ok': '有多普勒响应，可用',
                            'weak': '响应偏小，建议抬高板面重跑',
                            'partial': '只有一个方向有响应',
                            'drift': '基线在漂，不是动作响应',
                            'skipped': '本次跳过了安装自检，几何未判定',
                            'none': '当前几何测不到跳/蹲'}.get(st, st))
    add('- 说明：%s' % ((check or {}).get('text') or '—'))
    c = check or {}
    if c.get('vr_jump_p90') is not None:
        add('- 径向速度（m/s）：静息 p10/p90 %+.2f / %+.2f，跳 p90 %+.2f，蹲 p10 %+.2f'
            % (c.get('vr_base_lo') or 0.0, c.get('vr_base_hi') or 0.0,
               c.get('vr_jump_p90') or 0.0, c.get('vr_duck_p10') or 0.0))
    if c.get('up_resp') is not None:
        add('- 响应：跳 %+.2f m/s、蹲 %+.2f m/s（判据要求各 ≥0.20）'
            % (c.get('up_resp') or 0.0, c.get('dn_resp') or 0.0))
    if c.get('vr_jump_p90') is not None and c.get('vr_jump_p90'):
        # 反推等效视线仰角：v_r = v_body · sinθ，取 v_body ≈ 1.0 m/s
        import math as _m
        _vr = max(abs(c['vr_jump_p90']), abs(c.get('vr_duck_p10') or 0.0))
        _th = _m.degrees(_m.asin(min(1.0, _vr / 1.0)))
        add('- 等效视线仰角 ≈ %.1f°（按 v_body≈1.0 m/s 反推；要看清跳/蹲需 ≥25°）'
            % _th)
    if c.get('dphi_ref'):
        d = c['dphi_ref']
        add('- 参考（不参与判据）：dphi 站定 %s° / 跳 %s° / 蹲 %s°'
            % (d.get('stand1'), d.get('jump'), d.get('duck')))
    add('')

    add('## 二、各段特征分布')
    add('')
    add('| 段 | 帧数 | 角度中位(°) | 角度 IQR(°) | 径向速度(m/s) | 质心(bin) |')
    add('|---|---|---|---|---|---|')
    for seg in ('base1', 'jump', 'base2', 'duck', 'base3'):
        if seg not in groups:
            continue
        g = groups[seg]
        a = np.array([x['ang_deg'] for x in g])
        v = np.array([x['v_r'] for x in g])
        c = np.array([x['centroid_bin'] for x in g])
        add('| %s | %d | %+.1f | %.1f | %+.2f | %.2f |'
            % (seg, len(g), np.median(a),
               float(np.percentile(a, 75) - np.percentile(a, 25)),
               np.median(v), np.median(c)))
    add('')

    add('## 三、跳 / 蹲 可分性（单特征）')
    add('')
    act = [i for i, l in enumerate(labels) if l in ('jump', 'duck') and feats[i]]
    if len(act) < 6:
        add('> 动作帧不足（%d 帧），本次不下结论。' % len(act))
    else:
        add('| 特征 | 跳 均值 | 蹲 均值 | 跳-蹲 | Cohen\'s d | 留一准确率 |')
        add('|---|---|---|---|---|---|')
        y = [1 if labels[i] == 'jump' else 0 for i in act]
        for key, name in FEATURES:
            xs = [feats[i][key] for i in act]
            jx = [x for x, yy in zip(xs, y) if yy == 1]
            dx = [x for x, yy in zip(xs, y) if yy == 0]
            dd = _cohen_d(jx, dx)
            acc = _loo_nc_acc(np.array(xs).reshape(-1, 1), np.array(y))
            add('| %s | %+.3f | %+.3f | %+.3f | %.2f | %s |'
                % (name, np.mean(jx), np.mean(dx), np.mean(jx) - np.mean(dx),
                   dd, ('%.0f%%' % (100 * acc)) if not math.isnan(acc) else '—'))
        add('')

        add('## 四、动作 vs 站定（能否检出「有动作」）')
        add('')
        add('这里用「相对站定基线的绝对偏离」——跳和蹲方向相反，直接求均值会互相抵消。')
        add('')
        add('| 特征 | 站定 偏离 | 动作 偏离 | 动作/站定 倍数 | 留一准确率 |')
        add('|---|---|---|---|---|')
        probe = [i for i in range(len(feats))
                 if feats[i] and labels[i] in ('base1', 'base2', 'base3',
                                               'jump', 'duck')]
        yb = [1 if labels[i] in ('jump', 'duck') else 0 for i in probe]
        for key, name in FEATURES:
            xs = np.array([feats[i][key] for i in probe], float)
            base_x = xs[np.array(yb) == 0]
            if base_x.size == 0:
                continue
            bm = float(np.median(base_x))
            dev = np.abs(xs - bm)
            scale = float(np.median(dev[np.array(yb) == 0])) or 1e-9
            acc = _loo_nc_acc(dev.reshape(-1, 1), np.array(yb))
            add('| %s | %.3f | %.3f | %.1f× | %s |'
                % (name, float(np.median(dev[np.array(yb) == 0])),
                   float(np.median(dev[np.array(yb) == 1])),
                   float(np.median(dev[np.array(yb) == 1])) / scale,
                   ('%.0f%%' % (100 * acc)) if not math.isnan(acc) else '—'))
        add('')

        # ---- 结论 ----
        best, best_d = None, 0.0
        for key, name in FEATURES:
            xs = [feats[i][key] for i in act]
            jx = [x for x, yy in zip(xs, y) if yy == 1]
            dx = [x for x, yy in zip(xs, y) if yy == 0]
            dd = abs(_cohen_d(jx, dx)) if not math.isnan(_cohen_d(jx, dx)) else 0.0
            if dd > best_d:
                best, best_d = (key, name), dd
        # ---- 实时事件判定（谱宽法）----
        jd = ActionJudge()
        evs = []
        for i, f in enumerate(feats):
            if not f:
                continue
            ev = jd.push(f['nz_share'], f['doppler_width'])
            if ev:
                evs.append((i, ev))
        add('## 五、实时事件判定（谱宽法，可直接接游戏）')
        add('')
        add('- 规则：零多普勒外能量占比（5 帧滑窗）> %.2f 判为「有人在动」；'
            '动作期间谱宽均值 > %.3f 判为跳，否则判为蹲' % (jd.gate, jd.thr))
        add('- 检出事件 **%d** 个' % len(evs))
        if evs:
            add('')
            add('| 结束帧 | 真值 | 判定 | 谱宽 | 持续 | 结果 |')
            add('|---|---|---|---|---|---|')
            hit = tot = 0
            for i, (lb, w, nf) in evs:
                # 事件常跨越段边界（动作比提示略晚），用中心帧判定所属段落
                i0 = max(0, i - nf // 2)
                tv = labels[i0]
                truth = ('JUMP' if 'jump' in tv else 'DUCK' if 'duck' in tv
                         else 'IDLE' if ('base' in tv or 'idle' in tv or 'stand' in tv) else '?')
                ok = (truth in ('JUMP', 'DUCK') and truth == lb)
                if truth in ('JUMP', 'DUCK'):
                    tot += 1
                    hit += 1 if ok else 0
                add('| %d | %s | %s | %.3f | %d 帧 | %s |'
                    % (i, truth, lb, w, nf,
                       '✓' if ok else ('误报（静息段）' if truth == 'IDLE' else '✗')))
            add('')
            if tot:
                add('- **动作段事件准确率：%d/%d = %.1f%%**'
                    '（阈值属本批提案，待更多样本确认）' % (hit, tot, 100.0 * hit / tot))
            add('- 静息段误报主要来自「走动/换姿势」：走和蹲都是缓速运动、谱都窄，'
                '可用方位角是否变化进一步区分（尚未验证）')
        add('')

        add('## 六、结论与建议')
        add('')
        if st == 'none':
            add('1. **当前几何测不到跳/蹲。** 跳和蹲都没出现径向速度，'
                '说明垂直运动在雷达视线方向上的投影接近零。')
            add('2. 改法：雷达降到离地约 0.5 m、板面向上抬约 20°（对准胸口），人站约 1.2 m。')
            add('3. 改完先跑 `--selftest-only`，确认跳能给出 +0.2 m/s 以上再正式采。')
        elif st == 'partial':
            add('1. **只有一个方向有响应。** 通常是蹲的幅度不足、或蹲下时人往后坐。')
            add('2. 蹲的时候让重心垂直下降、别往后退，幅度再大一点后重跑自检。')
            add('   （注：自检用的是**平均多普勒**，在 129.5 ms 的 CPI 下跳与蹲天然互相'
                '抵消，出现「只有一个方向」并不代表绝对不可用；最终判定见第五节**谱宽法**。）')
        elif st == 'drift':
            add('1. **自检判为「漂移」。** 测到的不是动作响应，而是站位或板子在动。')
            add('2. 固定好板子和站位后重跑 `--selftest-only`。')
        elif st == 'skipped':
            add('1. **本次跳过了安装自检**，所以「能不能测竖直」这个问题**没有回答**。')
            add('2. 下面的数字只反映「动作能不能和站定分开」，不能当作竖直可用性的证据。')
            add('3. 要判定轴向，请另跑一次不带 `--no-selfcheck` 的自检。')
            add('4. 单特征最好的是 **%s**（|Cohen\'s d| = %.2f），仅供参考。'
                % (best[1] if best else '—', best_d))
        elif best:
            add('1. 自检通过，说明当前朝向可以测竖直。')
            add('2. 单特征里分离度最好的是 **%s**（|Cohen\'s d| = %.2f）。'
                % (best[1], best_d))
            if best_d >= 1.2:
                add('3. 按惯例 d≥1.2 可视为「强分离」，值得进入下一步（定阈值 + 滞回）。')
            elif best_d >= 0.8:
                add('3. d 在 0.8~1.2，属于中等分离，需要多特征组合或加大动作幅度。')
            else:
                add('3. d < 0.8，单特征不够。先检查动作幅度与站位，再考虑多特征。')
            add('4. 阈值、滞回、最小切换间隔都**还没有定**，本节数字只是「可分性」证据，'
                '不能写进已实现功能或验收结论（AGENTS.md 验证门）。')
        add('')
        add('> 报告中所有阈值建议均属**提案，待小组确认**；'
            '本报告只回答「数据里有没有可分性」。')
    add('')

    md = '\n'.join(lines)
    with open(os.path.join(out_dir, 'report.md'), 'w', encoding='utf-8') as f:
        f.write(md)
    return md


# ------------------------------------------------------------------ HTTP

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
            with open(os.path.join(HERE, 'vguide_1t2r.html'), encoding='utf-8') as f:
                body = f.read().encode('utf-8')
            ctype = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


# ------------------------------------------------------------------ 入口

def main():
    ap = argparse.ArgumentParser(description='1T2R 竖直方向采集引导')
    ap.add_argument('--port', default='auto')
    ap.add_argument('--rounds', type=int, default=2)
    ap.add_argument('--alternate', type=int, default=0,
                    help='最小验证模式：跳一次→停→蹲一次→停，重复 N 轮（自动跳过自检）')
    ap.add_argument('--sec-base', type=float, default=4.0)
    ap.add_argument('--sec-act', type=float, default=8.0)
    ap.add_argument('--prep', type=float, default=6.0)
    ap.add_argument('--band', default=None,
                    help='距离带 bin lo,hi（如 1,3）；默认按静息段能量主 bin 自动定')
    ap.add_argument('--out', default=os.path.join(ROOT, 'data', 'vguide_1t2r'))
    ap.add_argument('--web-port', type=int, default=PORT)
    ap.add_argument('--selftest-only', action='store_true',
                    help='只做安装方向自检，不采动作数据')
    ap.add_argument('--no-selfcheck', action='store_true',
                    help='跳过安装自检，直接采动作数据（已知侧向不对时用）')
    ap.add_argument('--simulate', action='store_true', help='合成数据，不碰硬件')
    ap.add_argument('--list', action='store_true', help='只列出串口后退出')
    ap.add_argument('--no-open', action='store_true', help='不自动开浏览器')
    ap.add_argument('--no-speak', action='store_true')
    ap.add_argument('--no-analyze', action='store_true')
    a = ap.parse_args()

    if a.no_speak:
        global NO_SPEAK
        NO_SPEAK = True
    if a.alternate:
        # 最小验证只要动作本身，自检的 20 秒不值得等
        a.no_selfcheck = True
        a.prep = max(3.0, min(a.prep, 6.0))
    if a.band:
        try:
            lo, hi = [int(x) for x in a.band.replace(' ', ',').split(',')]
            a.band = (lo, hi)
        except Exception:
            raise SystemExit('!! --band 需要 lo,hi，例如 1,3')

    if a.list:
        ports = list_ports()
        if not ports:
            print('  （没有发现串口）')
        for dev, desc, hwid in ports:
            print('  %-6s %-42s %s%s' % (dev, desc, hwid,
                                         '  <- CP210x/1T2R 指纹'
                                         if _is_1t2r(desc, hwid) else ''))
        return 0

    reader = None
    boot_msg = ''
    if a.simulate:
        reader = SimReader()
        a.port_used = 'SIM'
        boot_msg = '合成数据（--simulate）'
    else:
        port, why = pick_port(a.port)
        a.port_used = port
        try:
            import serial
            ser = serial.Serial(port, BAUD, timeout=0.02)
            for attr in ('dtr', 'rts'):
                try:
                    setattr(ser, attr, True)
                except Exception:
                    pass
            time.sleep(0.2)
            ser.reset_input_buffer()
        except Exception as exc:
            print('\n  !! 打不开 %s：%s: %s' % (port, type(exc).__name__, exc))
            print('  检查：USB 是否插紧 / 是否被其它程序占用（server.py、radar_serve、'
                  'start_viewer）')
            print('  两块 1T2R 同时插着时，用 --port COMx 指定新板那一个。')
            return 3
        reader = Reader(ser)
        boot_msg = '%s @ %d（%s）' % (port, BAUD, why)

    runner = Runner(a, reader)
    _set(port=a.port_used, bin_width=RANGE_RES)
    print('')
    print('  1T2R 竖直方向采集引导')
    print('  串口：%s' % boot_msg)
    print('  界面：http://127.0.0.1:%d/' % a.web_port)
    print('  关闭这个窗口 = 结束采集')
    print('')
    print('  放好板子再开始：')
    print('   · 要做竖直检测，板子必须绕光轴旋转 90°（两个天线一上一下）；')
    print('   · 人站在约 1.2 m，雷达高度与胸口接近；')
    print('   · 程序会先做 15 秒安装自检，直接告诉你这块板能不能测竖直。')
    print('')

    threading.Thread(target=runner.run, daemon=True).start()

    url = 'http://127.0.0.1:%d/' % a.web_port
    if not a.no_open:
        def _open():
            time.sleep(1.2)
            try:
                os.startfile(url)
            except Exception:
                webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    try:
        httpd = ThreadingHTTPServer(('127.0.0.1', a.web_port), Handler)
    except OSError as exc:
        print('\n  !! 端口 %d 被占用：%s' % (a.web_port, exc))
        print('  换一个：--web-port 8773')
        try:
            reader.close()
        except Exception:
            pass
        return 4

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
    try:
        reader.close()
    except Exception:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
