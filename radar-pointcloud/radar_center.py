# -*- coding: utf-8 -*-
r"""1T2R 的「最佳中线」配置：把物理上最灵敏的那个方位角当作 0 度。

背景（2026-11-03，用户实测观察）
--------------------------------
1T2R 板子**斜着用**时点云最密：最佳位置在板面法线一侧约 45 度。
也就是说 `az=0` 不是最佳中线，`az=+45`（或 -45）才是。

后果有两层：
1. 所有「偏多少度」「站回中间」的显示/提示，必须以这条最佳线为 0，
   否则人会一直被要求站到一个信号很差的法线位置上。
2. `radar_dsp.ANGLE_MAX = 45` 会在 45 度处把点砍掉一半。最佳区贴着
   视场边界时，必须同时把 angle_max 放宽（1T2R 两元相位差测角本身
   到 ±90 才混叠，放宽到 60~80 是安全的），否则采到的永远是半个目标。

本模块只负责读写 `center.json`，不做任何雷达 IO。
命令行工具 / 采集脚本共享同一个文件，避免各写一套参数。

文件格式
--------
    {
      "1t2r": {"az_deg": 45.0, "angle_max": 70.0, "px_ref": null,
               "updated": "2026-11-03T10:00:00", "note": "板子斜装，一侧45度最灵敏"},
      "2t4r": {"az_deg": 0.0, "angle_max": 45.0, ...}
    }

`az_deg` 是**原始 az 读数**，不是偏差。使用方一律做 `az_raw - az_deg`。
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, 'center.json')

DEFAULT = {
    '1t2r': {'az_deg': 0.0, 'angle_max': 45.0, 'px_ref': None, 'note': ''},
    '2t4r': {'az_deg': 0.0, 'angle_max': 45.0, 'px_ref': None, 'note': ''},
}


def load(board='1t2r'):
    """读中心配置；文件缺失/损坏时回退默认，绝不因为配置问题挡住采集。"""
    cfg = dict(DEFAULT.get(board) or DEFAULT['1t2r'])
    try:
        with open(PATH, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        got = raw.get(board) or {}
        for k in ('az_deg', 'angle_max', 'px_ref'):
            if got.get(k) is not None:
                cfg[k] = float(got[k])
        cfg['note'] = got.get('note', cfg.get('note', ''))
    except Exception:
        pass
    return cfg


def save(board, az_deg, angle_max=None, px_ref=None, note=''):
    """写回一个板型的中心配置，保留另一个板型的设置。"""
    raw = {}
    try:
        with open(PATH, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    entry = dict(raw.get(board) or {})
    entry['az_deg'] = round(float(az_deg), 2)
    if angle_max is not None:
        entry['angle_max'] = round(float(angle_max), 2)
    else:
        entry.setdefault('angle_max', 45.0)
    entry['px_ref'] = None if px_ref is None else round(float(px_ref), 3)
    entry['updated'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    if note:
        entry['note'] = note
    entry.setdefault('note', '')
    raw[board] = entry
    for b, d in DEFAULT.items():
        raw.setdefault(b, d)
    tmp = PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(raw, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')
    os.replace(tmp, PATH)
    return raw


def resolve(board, center_az=None, angle_max=None):
    """命令行优先，其次 center.json，最后默认。"""
    cfg = load(board)
    az = cfg['az_deg'] if center_az is None else float(center_az)
    am = cfg['angle_max'] if angle_max is None else float(angle_max)
    # 最佳中线贴在视场边缘时，旧默认 45 会把目标切一半：
    # 没显式配置 angle_max 就至少留出中线之外 25 度。
    if angle_max is None and abs(az) + 25.0 > am:
        am = min(89.0, abs(az) + 25.0)
    return {'az_deg': az, 'angle_max': am, 'note': cfg.get('note', '')}
