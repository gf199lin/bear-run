# -*- coding: utf-8 -*-
"""
点云生成流水线: 2D-FFT → CFAR/峰值 → 2 通道相位差测角 → 直角坐标点云。

参数来源（Radar_Config.ini, Ver 1.1.9）:
  频率 24.015~24.235 GHz (B = 220 MHz)   → 距离分辨率 c/(2B) = 0.681 m
  每 chirp 8 个 ADC 采样点，覆盖整个 212 µs 上扫 → 8 个距离单元, 最大 5.45 m
  每帧 64 chirp, 帧周期 129.5 ms          → chirp 周期 2.023 ms,
                                            v_max = ±λ/(4·Tc) = ±1.535 m/s,
                                            v_res = 0.048 m/s
  2 个 RX, 水平阵元间距 6.250 mm ≈ λ/2     → 相位差测角 Δφ = (2π·d/λ)·sinθ
"""
import math

import numpy as np

C = 299792458.0
F0 = 24.125e9                  # 中心频率
BANDWIDTH = 220e6
CHIRPS = 64
CHIRP_PERIOD = 129.5e-3 / CHIRPS
BINS = 8
ANT_SPACING = 6.250e-3
LAMBDA = C / F0
RANGE_RES = C / (2 * BANDWIDTH)          # 0.6813 m
MAX_RANGE = RANGE_RES * BINS             # 5.45 m
V_MAX = LAMBDA / (4 * CHIRP_PERIOD)      # 1.535 m/s
V_RES = 2 * V_MAX / CHIRPS               # 0.0480 m/s
ANGLE_MAX = 45.0                         # 水平视场 ±45°


class PointCloudProcessor:
    def __init__(self, cfar_scale=2.6, min_power=0.0, min_snr=7.0,
                 remove_clutter=True, angle_sign=1.0, max_points=64,
                 angle_max=ANGLE_MAX, min_range=0.0,
                 chirps=None, bins=None, range_res=None, v_res=None,
                 ant_spacing=None, lam=None, clutter_keep=1.0):
        self.cfar_scale = float(cfar_scale)
        self.min_power = float(min_power)       # 绝对门限（功率，0 = 关闭）
        self.min_snr = float(min_snr)           # 相对本帧噪声底的 SNR 门限 (dB)
        self.remove_clutter = bool(remove_clutter)
        # clutter_keep: MTI 保留系数。1.0 = 完全减掉静止分量（**原行为，默认不变**）；
        # <1.0 = 只减掉一部分，保留一份 DC 能量。
        # 依据（2026-10-22 横穿实测，见 docs/静止误报诊断-左门与噪声.md §15）：
        #   人**正对**雷达站着不动时，回波能量高度集中在零多普勒——
        #   实测 j1 的 MTI 残余仅 48.1%（斜站时 76~82%），即被减掉一半以上；
        #   关掉 MTI 后"正对"帧的保留点从 1.38 → 1.92 点/帧（+39%）。
        #   但关掉 MTI 会放进静态杂波，故改为"可调"而非"开关"。
        self.clutter_keep = float(clutter_keep)
        self.angle_sign = float(angle_sign)
        self.max_points = int(max_points)
        self.angle_max = float(angle_max)
        # 近距门屏蔽。本类默认 0（不屏蔽），保持「纯 DSP 原语」的中性口径，
        # 便于离线分析/校准仍能看见近场门；**默认屏蔽由应用层负责**
        # （server.py / radar_actions.py 的内置默认 0.681，或 calib.json）。
        #
        # 依据（.deepworks/tmp/seg/seg_01.00.bin，900 帧，人站 1.0 m 实测）：
        #   bin0 中心 r≈0.34 m，占 **73.5% 点数却只占 0.1% 功率** —— 它不是目标，
        #   而是一团「弱而多」的近场泄漏/残差：每帧 ~5.2 个点散布在**不同的多普勒
        #   单元**（64 个单元中 63 个出现过）、角度近似**均匀铺满 ±45°**
        #   （|角|≤3° 仅 12.8%，说明测角在强泄漏下已无物理意义），且减掉 chirp
        #   均值（去静态杂波）后仍剩 87.2% 能量、多普勒谱平坦度 0.306
        #   → 它是**宽谱**的，无法用 MTI/多普勒开窗清掉，只能按距离门屏蔽。
        #   屏蔽代价：掉 0.1% 功率；识别带下界 0.681 m 本就不含 bin0。
        #   距离分辨率仅 0.6813 m，0~0.68 m 整门必然混有天线罩/外壳/PCB 与芯片内
        #   TX→RX 直耦，物理上不可能为「空」。
        # 注：以上 0.1% 功率这一项随「场里有没有别的强回波」而变（无人近处时
        #     bin0 可以是最强项）；点数占比 73.5% 才是它「扎眼」的直接原因。
        self.min_range = float(min_range)
        # ---- 多板型参数 ----------------------------------------------------
        # 默认值 == 模块常量（1T2R / DEV202H 实测口径），因此不传参时
        # 逐字等价于本文件之前的实现。DEV113P (2T4R) 由调用方覆盖：
        #   bins=32、range_res≈0.150 m、ant_spacing=5.900 mm、λ=24.5005 GHz
        #   （来源 ONELAB Radar_Config.ini [WAVE_INFO_0]，见 radar_source.BOARD_PROFILES）
        self.chirps = int(chirps) if chirps else CHIRPS
        self.bins = int(bins) if bins else BINS
        self.range_res = float(range_res) if range_res else RANGE_RES
        self.v_res = float(v_res) if v_res else V_RES
        self.ant_spacing = float(ant_spacing) if ant_spacing else ANT_SPACING
        # 波长：默认 1T2R 的 24.125 GHz（模块常量）。测角 sinθ = Δφ·λ/(2π·d) 同时
        # 依赖 λ 与阵元间距 d，两块板两者都不同，必须成对覆盖 —— 只改 d 不改 λ
        # 会留下 ~1.5% 的固定角误差（24.125 vs 24.5005 GHz）。
        self.lam = float(lam) if lam else LAMBDA
        self.win = np.hamming(self.chirps).astype(np.float32)
        self._guard = 1
        self._train = 2

    # ---------------- 2D FFT ----------------
    def rd_map(self, z0, z1, remove_clutter=None):
        if remove_clutter is None:
            remove_clutter = self.remove_clutter
        out = []
        for z in (z0, z1):
            z = np.asarray(z, dtype=np.complex64)
            if remove_clutter:
                # clutter_keep=1.0 时为原行为（完全去 DC）；<1.0 时保留一部分，
                # 以免把"正对雷达站着不动"的人一起减掉（见 __init__ 注释）。
                z = z - self.clutter_keep * z.mean(axis=0, keepdims=True)
            sp = np.fft.fft(z * self.win[:, None], axis=0)
            out.append(np.fft.fftshift(sp, axes=0))
        return out[0], out[1]

    # ---------------- CFAR ----------------
    def cfar(self, power):
        """2D 单元平均 CFAR，返回布尔检测掩膜。"""
        d, b = power.shape
        noise = np.zeros_like(power)
        g = t1 = self._guard
        t2 = self._train
        for i in range(d):
            for j in range(b):
                i0, i1 = max(0, i - g - t2), min(d, i + g + t2 + 1)
                j0, j1 = max(0, j - g - t2), min(b, j + g + t2 + 1)
                blk = power[i0:i1, j0:j1].copy()
                bi, bj = i - i0, j - j0
                g0i, g1i = max(0, bi - g), min(blk.shape[0], bi + g + 1)
                g0j, g1j = max(0, bj - g), min(blk.shape[1], bj + g + 1)
                cell = blk[g0i:g1i, g0j:g1j].copy()
                blk[g0i:g1i, g0j:g1j] = 0.0
                n = blk.size - cell.size
                if n <= 0:
                    continue
                noise[i, j] = blk.sum() / n
        return noise

    def detect(self, power):
        """CFAR + 自适应 SNR 门限 + 绝对门限，返回 (掩膜, 噪声底)。"""
        noise = self.cfar(power)
        noise_med = float(np.median(noise)) if noise.size else 1.0
        noise_med = max(noise_med, 1e-6)
        thr_snr = noise_med * (10.0 ** (self.min_snr / 10.0))
        thr = np.maximum(noise * self.cfar_scale, thr_snr)
        if self.min_power > 0:
            thr = np.maximum(thr, self.min_power)
        return power > thr, noise_med

    # ---------------- 峰值挑选 ----------------
    @staticmethod
    def _peaks(mask, power):
        d, b = power.shape
        idx = []
        for i in range(d):
            for j in range(b):
                if not mask[i, j]:
                    continue
                i0, i1 = max(0, i - 1), min(d, i + 2)
                j0, j1 = max(0, j - 1), min(b, j + 2)
                if power[i, j] >= power[i0:i1, j0:j1].max() - 1e-9:
                    idx.append((i, j))
        return idx

    # ---------------- 主入口 ----------------
    def process(self, z0, z1):
        """输入两 RX 的 (chirps, bins) 复矩阵，返回点云列表。"""
        r0, r1 = self.rd_map(z0, z1)
        power = (np.abs(r0) ** 2)
        mask, noise_med = self.detect(power)
        pts = []
        dmax = power.max() if power.size else 1.0
        for i, j in self._peaks(mask, power):
            p = power[i, j]
            # 距离
            rng = (j + 0.5) * self.range_res
            if rng < self.min_range:
                continue
            # 多普勒 → 径向速度（i 已 fftshift，中心为 0）
            dop = i - self.chirps // 2
            vel = dop * self.v_res
            # 两通道相位差测角
            zz = r1[i, j] * np.conj(r0[i, j])
            dphi = math.atan2(zz.imag, zz.real)
            s = dphi * self.lam / (2 * math.pi * self.ant_spacing)
            s = max(-1.0, min(1.0, s))
            ang = math.degrees(math.asin(s)) * self.angle_sign
            if abs(ang) > self.angle_max:
                continue
            a = math.radians(ang)
            pts.append({
                'x': rng * math.sin(a),
                'y': rng * math.cos(a),
                'r': rng,
                'deg': ang,
                'v': vel,
                'p': float(p / dmax) if dmax > 0 else 0.0,
                'pa': float(p),
            })
        pts.sort(key=lambda q: -q['pa'])
        return pts[:self.max_points], noise_med


def synth_frame(n=1, target=(0.0, 3.0, 0.0), snr=25.0, seed=0):
    """合成一帧数据，用于无硬件时自检。target=(x, y, v)。"""
    rng = np.random.default_rng(seed)
    x, y, v = target
    r = math.hypot(x, y)
    deg = math.degrees(math.atan2(x, y))
    dphi = 2 * math.pi * ANT_SPACING * math.sin(math.radians(deg)) / LAMBDA
    b = int(r / RANGE_RES)
    d = int(round(v / V_RES))
    amp = 300.0
    z0 = (rng.normal(0, 1, (CHIRPS, BINS)) + 1j * rng.normal(0, 1, (CHIRPS, BINS))) * 20.0
    z1 = (rng.normal(0, 1, (CHIRPS, BINS)) + 1j * rng.normal(0, 1, (CHIRPS, BINS))) * 20.0
    for c in range(CHIRPS):
        ph = 2 * math.pi * d * c / CHIRPS
        i = int(np.clip(b, 0, BINS - 1))
        z0[c, i] += amp * np.exp(1j * ph)
        z1[c, i] += amp * np.exp(1j * (ph + dphi))
    return z0.astype(np.complex64), z1.astype(np.complex64)
