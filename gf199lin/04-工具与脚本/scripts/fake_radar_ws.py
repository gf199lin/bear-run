# -*- coding: utf-8 -*-
"""假雷达上游（WebSocket + JSON 帧），把真实/脚本数据喂进**真游戏**，用来演示校准向导 v2。

用法:
    python fake_radar_ws.py [port]            # 默认 8766
    # 然后浏览器打开（profile 走 URL 路径选择）:
    #   http://127.0.0.1:8123/radar-runner/index.html?input=radar&radar=ws://127.0.0.1:8766/good

profile:
    good      配合的玩家 + 干净上游      -> 5 步全过，放宽阈值
    noisy     配合的玩家 + **真实静息噪声**（features_take*.jsonl 逐帧上游判定）-> 第 4 步停用雷达跳/蹲
    inverted  上游基线 +0.62 且左右装反   -> 现场学 sign = -1
    weak      玩家动作只有 0.36/0.34      -> "太弱"提示，不解锁
    real      **真实左右数据**（lr-move d00.85 px）+ 真实噪声 -> 左右走不动，卡在第 2 步（很慢，约 50s）

帧格式与 radar-runner/js/InputSource.js 的 RadarInputSource 完全一致:
    {"ts": <epoch ms>, "x": float, "height": float, "jump": float, "crouch": float, "confidence": float}
"""
import asyncio
import csv
import json
import os
import statistics
import sys
import time

from websockets.asyncio.server import serve

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
GTD = os.path.join(ROOT, '.deepworks', 'tmp', 'gtd')
LR = os.path.join(ROOT, 'uploads', 'ses_ea91660b3ffell1v8B4tGE7NhJ', 'lr-move-frames.csv')

RATE = 20.0                      # 推流帧率 Hz
DT = 1.0 / RATE

NEG_LABELS = {'pre', 'post', 'idle_calib', 'idle', 'idle_rest', 'idle_end',
              'neg_sway', 'neg_step', 'neg_bend', 'neg_arms', 'sync_wave'}


def load_neg():
    """真实负样本帧的上游跳/蹲（逐帧化 README 判据），A->B->C 顺序拼接"""
    out = []
    for take in 'ABC':
        path = os.path.join(GTD, 'features_take%s.jsonl' % take)
        with open(path, encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                r = json.loads(line)
                if r['label'] not in NEG_LABELS:
                    continue
                d1 = float(r['dev'][1])
                out.append({'jump': 1.0 if d1 < -18.0 else 0.0,
                            'crouch': 1.0 if d1 >= 12.0 else 0.0,
                            'dev1': round(d1, 3), 'src': '%s/%s' % (take, r['label'])})
    return out


NEG = load_neg()


def up_noise(i):
    """第 i 个流帧取第 i 个真实负样本帧的上游判定（1:1，保持真实事件率）"""
    r = NEG[i % len(NEG)]
    return r['jump'], r['crouch'], r['dev1'], r['src']


def load_real_lr(segment='d00.85'):
    """真实左右数据：去静息基线后归一化（meta scaleM = 0.8 m）"""
    rows = []
    # 注意：这份 CSV 首列带 BOM，必须用 utf-8-sig 读，否则 'segment' 键取不到
    with open(LR, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            if r['segment'] != segment or not r['px_m']:
                continue
            rows.append(r)
    idle = [float(r['px_m']) for r in rows if r['direction'] == 'IDLE']
    base = statistics.median(idle) if idle else 0.0
    return [{'x': (float(r['px_m']) - base) / 0.8, 'action': r['action_id'],
             'dir': r['direction'], 'events': r['events'] or ''} for r in rows], base


def ramp(a, b, n):
    return [a + (b - a) * (k + 1) / n for k in range(n)]


def jit(i, amp=0.02):
    return amp * ((i * 37 % 11) - 5) / 5.0


def build(profile):
    """返回 (frames, 说明)。frames 为 20Hz 流帧，循环推送。"""
    fr = []

    def add(x, jump=0.0, crouch=0.0, tag='', **kw):
        f = {'x': round(float(x), 4), 'jump': round(float(jump), 4),
             'crouch': round(float(crouch), 4), 'height': 0.0, 'confidence': 1.0,
             'tag': tag}
        f.update(kw)
        fr.append(f)

    if profile == 'still':
        # 纯静息档：真实静息 x（去基线）+ 干净上游（跳/蹲恒 0）
        # 用来验证联调台的"静息自检"在干净上游下判为干净
        rows = []
        with open(LR, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                if r['segment'] != 'd00.85' or not r['px_m'] or r['direction'] != 'IDLE':
                    continue
                rows.append(float(r['px_m']))
        base = statistics.median(rows) if rows else 0.0
        for k, v in enumerate(rows):
            add((v - base) / 0.8 + jit(k, 0.01), 0.0, 0.0, '静息')
        return fr, ('纯静息档：真实静息 x（基线 %.3f m，去基线/0.8）+ 干净上游；'
                    '%d 帧 = %.0f 秒一轮' % (base, len(rows), len(rows) / RATE))

    if profile == 'real':
        real, base = load_real_lr()
        for k, r in enumerate(real):
            j, c, d1, src = up_noise(k)
            ev = r.get('events') or ''
            # 设备自己在 events 列里报的 JUMP/DUCK 也算"上游会告诉游戏什么"
            add(r['x'], 1.0 if 'JUMP' in ev else j, 1.0 if 'DUCK' in ev else c,
                '真实左右数据 %s' % (r['action'] or 'IDLE'), dev1=d1, up=src, events=ev)
        return fr, ('真实左右数据 d00.85（基线 %.3f m，去基线/0.8 归一化）+ 真实静息噪声 + 设备自报 JUMP/DUCK 事件；'
                    '约 %d 帧 = %.0f 秒一轮' % (base, len(real), len(real) / RATE))

    noisy = (profile == 'noisy')

    def noise(k, tag):
        if not noisy:
            return 0.0, 0.0, {}
        j, c, d1, src = up_noise(k)
        return j, c, {'dev1': d1, 'up': src}

    n = 0
    # 第 1 步：站好不动（2.4s）
    for i in range(48):
        j, c, kw = noise(n, 'still'); n += 1
        add(0.0, j, c, '站好不动', **kw)
    # 第 2 步：向左走
    for v in ramp(0.0, -0.78, 12):
        j, c, kw = noise(n, 'left'); n += 1
        add(v, j, c, '向左走', **kw)
    for i in range(12):
        j, c, kw = noise(n, 'left'); n += 1
        add(-0.78 + jit(i, 0.03), j, c, '向左走(保持)', **kw)
    for v in ramp(-0.78, 0.0, 10):
        j, c, kw = noise(n, 'back'); n += 1
        add(v, j, c, '回中', **kw)
    # 第 3 步：向右走
    for v in ramp(0.0, 0.78, 12):
        j, c, kw = noise(n, 'right'); n += 1
        add(v, j, c, '向右走', **kw)
    for i in range(12):
        j, c, kw = noise(n, 'right'); n += 1
        add(0.78 + jit(i, 0.03), j, c, '向右走(保持)', **kw)
    for v in ramp(0.78, 0.0, 10):
        j, c, kw = noise(n, 'back'); n += 1
        add(v, j, c, '回中', **kw)
    # 第 4 步：跳（有效峰值 0.70；weak 档 0.36）
    peak_j = 0.36 if profile == 'weak' else 0.70
    for i in range(24):
        j, c, kw = noise(n, 'pre-jump'); n += 1
        add(jit(i, 0.02), j, c, '等跳跃', **kw)
    for i, v in enumerate(ramp(0.0, peak_j, 3) + [peak_j, peak_j] + ramp(peak_j, 0.0, 3)):
        j, c, kw = noise(n, 'jump'); n += 1
        add(jit(i, 0.02), max(v, j), c, '跳', **kw)
    for i in range(24):
        j, c, kw = noise(n, 'post-jump'); n += 1
        add(jit(i, 0.02), j, c, '跳完了', **kw)
    # 第 5 步：蹲（有效峰值 0.64；weak 档 0.34）
    peak_c = 0.34 if profile == 'weak' else 0.64
    for i in range(24):
        j, c, kw = noise(n, 'pre-crouch'); n += 1
        add(jit(i, 0.02), j, max(c, 0.0), '等蹲下', **kw)
    for i, v in enumerate(ramp(0.0, peak_c, 4) + [peak_c] * 8 + ramp(peak_c, 0.0, 4)):
        j, c, kw = noise(n, 'crouch'); n += 1
        add(jit(i, 0.02), j, max(v, c), '蹲', **kw)
    for i in range(40):
        j, c, kw = noise(n, 'end'); n += 1
        add(jit(i, 0.02), j, c, '结束', **kw)

    if profile == 'inverted':
        # 上游基线 +0.62 且左右装反（x' = 0.62 - x）
        for f in fr:
            f['x'] = round(0.62 - f['x'], 4)

    desc = {
        'good': '配合的玩家 + 干净上游：5 步全过，阈值按峰值放宽',
        'noisy': '配合的玩家 + 真实静息噪声（取真实负样本帧的逐帧上游判定）',
        'inverted': '上游基线 +0.62 且左右装反：现场学出 sign = -1',
        'weak': '上游干净但玩家只有 0.36/0.34：应提示"太弱"且不写存档',
    }[profile]
    return fr, '%s；约 %d 帧 = %.0f 秒一轮' % (desc, len(fr), len(fr) / RATE)


async def handler(ws):
    path = (ws.request.path or '/').strip('/') or 'good'
    profile = path.split('?')[0].split('/')[0] or 'good'
    if profile not in ('good', 'noisy', 'inverted', 'weak', 'real', 'still'):
        profile = 'good'
    frames, desc = build(profile)
    peer = getattr(ws, 'remote_address', None)
    print('[fake-radar] +client %s  profile=%s   %s' % (peer, profile, desc), flush=True)
    tags = []
    for f in frames:
        if not tags or tags[-1][0] != f['tag']:
            tags.append([f['tag'], 0])
        tags[-1][1] += 1
    print('[fake-radar]   脚本: ' + ' -> '.join('%s(%d帧)' % (t[0], t[1]) for t in tags), flush=True)
    i = 0
    epoch = time.time()
    t0 = time.monotonic()
    try:
        while True:
            # 按绝对时间对齐推流：不会因为 send 开销而累积漂移（帧率稳定在 RATE）
            target = t0 + i * DT
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            f = frames[i % len(frames)]
            msg = {'ts': int((epoch + i * DT) * 1000), 'x': f['x'], 'height': f['height'],
                   'jump': f['jump'], 'crouch': f['crouch'], 'confidence': f['confidence']}
            i += 1
            await ws.send(json.dumps(msg))
    except Exception as e:  # 客户端关闭等
        print('[fake-radar] -client %s  profile=%s (%s) 发了 %d 帧' % (peer, profile, type(e).__name__, i), flush=True)


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    print('[fake-radar] 真实负样本帧 %d 条；监听 ws://127.0.0.1:%d/<profile>' % (len(NEG), port), flush=True)
    print('[fake-radar] profile: still | good | noisy | inverted | weak | real', flush=True)
    async with serve(handler, '127.0.0.1', port, max_size=None):
        await asyncio.Future()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
