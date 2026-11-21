# -*- coding: utf-8 -*-
"""离线分析历史校准 npz，算左右移的方位角幅度。

支持两种 npz：
  - data/calibration/*     键 z1，形状 (帧, chirps, bins, rx)
  - data/jumpduck_battery/* 键 z， 形状 (帧, rx, chirps, bins)

用法：
    py -3 tools/analyze_calib_npz.py data/calibration/20261029-190335
    py -3 tools/analyze_calib_npz.py <dir> --cfar 2.6 --snr 7
"""
import argparse
import glob
import os
import re
import sys

import numpy as np

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'radar-pointcloud'))
from radar_dsp import PointCloudProcessor  # noqa: E402


def load_frames(path):
    """统一成 (n, chirps, bins, rx) 的数组，返回 (数组, 通道名)。"""
    d = np.load(path, allow_pickle=True)
    if 'z1' in d.files:
        return d['z1'], 'z1'
    if 'z' in d.files:
        z = d['z']
        # (n, rx, chirps, bins) -> (n, chirps, bins, rx)
        if z.ndim == 4 and z.shape[1] == 2:
            z = np.transpose(z, (0, 2, 3, 1))
        return z, 'z'
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dir')
    ap.add_argument('--cfar', type=float, default=2.6)
    ap.add_argument('--snr', type=float, default=7.0)
    ap.add_argument('--keep-clutter', type=float, default=1.0)
    a = ap.parse_args()

    proc = PointCloudProcessor(cfar_scale=a.cfar, min_snr=a.snr,
                               remove_clutter=True, clutter_keep=a.keep_clutter,
                               max_points=64)

    files = sorted(glob.glob(os.path.join(a.dir, '*.npz')))
    if not files:
        print('目录里没有 npz：%s' % a.dir)
        return 1

    groups, per_rep = {}, {}
    print('%-30s %-4s %5s %5s %9s' % ('样本', '键', '帧', '有效', '角度中位'))
    print('-' * 62)
    for f in files:
        base = os.path.basename(f)[:-4]
        z, key = load_frames(f)
        if z is None:
            print('%-30s %s' % (base, '无 z1/z 通道'))
            continue
        per = []
        for i in range(z.shape[0]):
            fr = z[i]
            if fr.ndim != 3 or fr.shape[2] < 2:
                continue
            pts, _ = proc.process(fr[:, :, 0], fr[:, :, 1])
            if pts:
                per.append(float(np.median([p['deg'] for p in pts])))
        m = re.match(r'^s(\d+)_(.+?)_r(\d+)$', base)
        label = m.group(2) if m else base
        groups.setdefault(label, []).extend(per)
        if m:
            per_rep[(label, int(m.group(3)))] = float(np.median(per)) if per else None
        print('%-30s %-4s %5d %5d %9s' % (
            base, key, z.shape[0], len(per),
            ('%9.2f' % float(np.median(per))) if per else '     None'))

    print('')
    print('=== 按动作汇总 ===')
    stat = {}
    for lb, v in groups.items():
        if not v:
            continue
        arr = np.array(v)
        stat[lb] = float(np.median(arr))
        print('  %-13s 帧%4d  中位 %7.2f   5%% %7.2f  95%% %7.2f' % (
            lb, len(v), np.median(arr), np.quantile(arr, .05), np.quantile(arr, .95)))

    center = stat.get('LANE_CENTER', stat.get('IDLE'))
    if center is not None and 'LANE_RIGHT' in stat:
        print('')
        print('=== 相对「回中」(%.2f°) ===' % center)
        for lb in ('IDLE', 'LANE_LEFT', 'LANE_RIGHT'):
            if lb in stat:
                print('  %-13s %+7.2f°' % (lb, stat[lb] - center))
        if 'LANE_LEFT' in stat:
            step = (abs(stat['LANE_LEFT'] - center) + abs(stat['LANE_RIGHT'] - center)) / 2.0
            print('')
            print('  一步 %.2f°  ->  x=%.2f (门限 0.22)  xGain=%.2f' % (
                step, step / 20.0, min(1.7, 0.55 / max(0.02, step / 20.0))))
    return 0


if __name__ == '__main__':
    sys.exit(main())
