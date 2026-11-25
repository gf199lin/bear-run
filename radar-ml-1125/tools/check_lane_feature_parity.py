# -*- coding: utf-8 -*-
"""实时特征与离线整段特征的一致性自检（build_feature_row vs build_feature_matrix[-1]）。

用法： python tools/check_lane_feature_parity.py
退出码 0 = 全部逐位一致（差异 < 1e-9）。
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
sys.path.insert(0, HERE)

import lane_model as lm  # noqa: E402
from compare_lane_models import FILES, prod_series  # noqa: E402


def main():
    bad = 0
    checked = 0
    for tag, rel, dist in FILES:
        path = os.path.join(ROOT, rel.replace('/', os.sep))
        if not os.path.exists(path):
            continue
        s = prod_series(path, tag, 1, 1, band=(1.0, 2.6), use_cache=True)
        if len(s['px']) < 320:
            print('  跳过（帧数不足长窗）: %s' % tag)
            continue
        X = lm.build_feature_matrix(s['px'], s['py'], s['en'], s['n'])
        # 模拟实时缓冲：只保留最后 313 帧
        keep = 313
        row_live = lm.build_feature_row(s['px'][-keep:], s['py'][-keep:],
                                        s['en'][-keep:], s['n'][-keep:])
        d = float(np.abs(row_live - X[-1]).max())
        checked += 1
        flag = 'OK ' if d < 1e-9 else 'BAD'
        if d >= 1e-9:
            bad += 1
            k = int(np.argmax(np.abs(row_live - X[-1])))
            print('  %s %-14s max diff %.3e（第 %d 维 %s）'
                  % (flag, tag, d, k, lm.FEATURE_NAMES[k]))
        else:
            print('  %s %-14s max diff %.1e' % (flag, tag, d))
    print('检查 %d 个文件，不一致 %d 个' % (checked, bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
