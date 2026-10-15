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
                 angle_max=ANGLE_MAX, min_range=0.0):
        self.cfar_scale = float(cfar_scale)
        self.min_power = float(min_power)       # 绝对门限（功率，0 = 关闭）
        self.min_snr = float(min_snr)           # 相对本帧噪声底的 SNR 门限 (dB)
        self.remove_clutter = bool(remove_clutter)
        self.angle_sign = float(angle_sign)
        self.max_points = int(max_points)
        self.angle_max = float(angle_max)
        # 近距门屏蔽：距离门分辨率仅 0.6813 m，bin0(0.34 m)/bin1(1.02 m) 天然
        # 被雷达自身、外壳、线缆与桌面等近距强反射占满（实测占 95% 点数、
        # 90% 功率且逐帧闪烁），把它们过滤掉才能看清 2 m 以外的真实目标。
        self.min_range = float(min_range)
        self.win = np.hamming(CHIRPS).astype(np.float32)
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
                z = z - z.mean(axis=0, keepdims=True)      # 去静态杂波（仅去 DC 多普勒）
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
        """输入两 RX 的 (64, 8) 复矩阵，返回点云列表。"""
        r0, r1 = self.rd_map(z0, z1)
        power = (np.abs(r0) ** 2)
        mask, noise_med = self.detect(power)
        pts = []
        dmax = power.max() if power.size else 1.0
        for i, j in self._peaks(mask, power):
            p = power[i, j]
            # 距离
            rng = (j + 0.5) * RANGE_RES
            if rng < self.min_range:
                continue
            # 多普勒 → 径向速度（i 已 fftshift，中心为 0）
            dop = i - CHIRPS // 2
            vel = dop * V_RES
            # 两通道相位差测角
            zz = r1[i, j] * np.conj(r0[i, j])
            dphi = math.atan2(zz.imag, zz.real)
            s = dphi * LAMBDA / (2 * math.pi * ANT_SPACING)
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
