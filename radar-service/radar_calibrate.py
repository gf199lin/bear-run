# -*- coding: utf-8 -*-
"""
近距人体动作识别 · 现场校准工具（`radar_calibrate.py`）

════════════════════════════════════════════════════════════════════════
为什么需要它
────────────────────────────────────────────────────────────────────────
识别器（`radar_actions.py`）的判定输入是**相对位移**（`v_lat` / `dx`），
而「多大的 `v_lat`/`dx` 才算人真的动了」这件事**不能拍脑袋定**：
实测静息状态下同一套估计器的噪声量级随站位、安装几何、灵敏度差很多
（带内 0.2 点/帧 时 |Δ| 中位 0.14 m、p95 0.84 m；带内 1.3 点/帧 时 p95 到 2.3 m）。
固定阈值要么太松（静止也乱报）要么太紧（动了也不报）。

所以本工具的做法是：**用与识别器完全相同的估计器**在同一套数据上量出噪声分布，
再把阈值回填成「实测噪声的 p95」→ 写 `calib.json` → 识别器用 `--calib` 加载。
阈值从「提案数字」变成「当场测出来的数字」。

★ 重要边界
  * 本工具**只调阈值和距离带**，不改变判定原则（仍是相对位移方向，不是绝对位置）。
  * `rest` 模式不需要真值，任何人都能跑，结论可靠。
  * `sign` 模式**需要真值**（你要知道身体实际往哪边动了），否则结论无效 ——
    没有真值的测试不算测试（AGENTS.md 证据纪律）。
  * `band` 模式只量「哪个距离门有人」，是观测记录，不是动作准确率结论。
  * 产出的一切数字都是**提案，待小组确认**，`proposal: true` 会写进 JSON。

三种模式
  band  逐距离门统计占用率/点数/功率 → 推荐 `band`（哪一段才是人）
  rest  静息（人不做动作）→ 量噪声 → 推荐 `v_on`/`v_off`/`min_dx`/`max_step`
  sign  引导人往已知方向移动一次 → 量出位移符号 → 推荐 `angle_sign`（±1）

用法
  # 离线（用已有录制，最省事、可复算）
  python radar_calibrate.py --mode band --file ..\\.deepworks\\tmp\\seg\\seg_01.50.bin
  python radar_calibrate.py --mode rest --file ..\\.deepworks\\tmp\\rest.bin --out calib.json
  python radar_calibrate.py --mode sign --file ..\\.deepworks\\tmp\\wave.bin --expect left
  # 实时（人站在雷达前，按提示照做）
  python radar_calibrate.py --mode band --port COM3 --seconds 15
  python radar_calibrate.py --mode rest --port COM3 --seconds 25 --out calib.json
  python radar_calibrate.py --mode sign --port COM3 --expect left

时间轴约定：**两种模式都用「帧号 × 129.5 ms」**（与 `radar_actions.analyze_file`
一致），不用墙钟。这样实时校准量到的数字与离线复算的数字可以直接对照；
离线回放比实机快得多，若用墙钟会让时间类门限（最短切换间隔等）失真。
"""
import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import radar_actions as ra            # noqa: E402

BIN_PITCH = 0.6813        # 距离门间距（米）＝ c/(2·BW)，r_j = (j + 0.5) × pitch
DEFAULT_OUT = os.path.join(HERE, 'calib.json')

# 提案门限（待小组确认）——用于把实测分布折算成可用阈值
V_ON_FLOOR = 0.15         # v_on 下限：再灵敏也不该低于 0.15 m/s
V_OFF_RATIO = 0.55        # 滞回释放 = 触发门限 × 0.55
MIN_DX_FLOOR = 0.05       # min_dx 下限
MAX_STEP_MIN = 0.35       # max_step 下限（米/帧）
MAX_STEP_MAX = 0.80       # max_step 上限
MAX_STEP_K = 3.0          # max_step = K × p99(逐帧 |Δpx|)
BAND_OCC_MIN = 0.30       # 推荐进入距离带所需的最低占用率（提案）
LEAK_RANGE = 0.70         # 低于此距离的门视为 TX/RX 近场泄漏门，必排除（1T2R 实测值）

# ---- 板型（1T2R / 2T4R）自适应 ------------------------------------------
# BIN_PITCH / LEAK_RANGE / 时间轴换算必须与**采集时那块板**一致，否则
# 「门号 ↔ 米」与「帧号 ↔ 毫秒」会整体错位（2T4R 误用 1T2R 值 → 时间轴快 4.02×、
# 门距差 4.5×）。用 set_board() 运行时整体切换；默认仍为 1T2R 原值 →
# 不调用 set_board 时行为与改动前完全一致。
BOARD = '1t2r'            # 当前生效板型


def set_board(board):
    """切换全局板型常量（BOARD / BIN_PITCH / LEAK_RANGE），返回生效板型。"""
    global BOARD, BIN_PITCH, LEAK_RANGE
    import radar_source as rs
    BOARD = board if board in ('1t2r', '2t4r') else rs.DEFAULT_BOARD
    BIN_PITCH = rs.range_res(BOARD)
    # 近场泄漏门：1T2R = 0.70 m（实测）。2T4R 门距只有 0.150 m，沿用 0.70 m 会
    # 连门 1~4 一起删掉，所以只排除门 0 —— **提案，待 2T4R 卷尺点检确认**。
    LEAK_RANGE = 0.70 if BOARD == '1t2r' else rs.range_res(BOARD)
    return BOARD


def time_axis_ms(board=None):
    """帧序号 → 毫秒的换算系数（相对毫秒 = 帧号 × 本值）。"""
    import radar_source as rs
    return rs.frame_period(board or BOARD) * 1000.0


# ------------------------------------------------------------------ 数据来源
def bin_index(r):
    """距离 → 距离门索引（与 radar_dsp 的 rng=(j+0.5)·pitch 互为逆运算）。"""
    return int(round(float(r) / BIN_PITCH - 0.5))


def frames_from_file(path, proc, limit=None, log=print, board=None):
    """离线回放录制文件，逐帧 yield (idx, t_ms, pts)。

    board=None（默认）→ 按**文件自身字节形态**嗅探板型（1T2R 含外层 ICLH 魔数 /
    2T4R 是裸 136 B 帧）；嗅探不出就沿用当前板型并打印警告 —— 不猜。
    解析器与时间轴系数都随板型走，所以同一个函数能正确读两块板的录制文件。
    """
    import radar_source as rs
    b = board or rs.sniff_board_from_file(path)
    if b is None:
        b = BOARD
        log('[calib] 警告：无法从文件判断板型，沿用当前板型 %s'
            '（可用 board= 显式指定 1t2r / 2t4r）' % b)
    b = set_board(b)
    parser, asm = rs.make_protocol(b)
    fp_ms = time_axis_ms(b)
    log('[calib] 解析器 = %s，帧周期 = %.4f s，门距 = %.4f m'
        % (b, fp_ms / 1000.0, BIN_PITCH))
    idx = 0
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            for rf in parser.feed(chunk):
                pair = asm.push(rf)
                if pair is None:
                    continue
                idx += 1
                # 只用前两个 RX（z0/z1）→ 与雷达实时管道
                # radar_source.build_pipeline 的 on_frame(pair[0], pair[1], stats)
                # 完全同口径（2T4R 的 assembler 会返回 4 个 RX，不能 *pair 展开）
                yield idx, idx * fp_ms, proc.process(pair[0], pair[1])[0]
                if limit is not None and idx >= limit:
                    return
    log('[calib] 离线回放 %s → %d 帧（坏包 %d，跳过 %d 字节）'
        % (os.path.basename(path), idx, parser.bad_packets, parser.skipped_bytes))


def frames_live(port, baud, seconds, proc, log=print, board=None):
    """实时串口，逐帧 yield (idx, t_ms, pts)。按秒数自动停止。

    baud=None → 由 SerialSource 按串口指纹自动定波特率（1T2R 921600 / 2T4R 1500000）；
    board=None → 用 src 探测出的板型（src.board）。两者都不猜。
    """
    import radar_source as rs
    src = rs.SerialSource(port=port, baud=baud)
    src.open()
    b = set_board(board or getattr(src, 'board', None))
    parser, asm = rs.make_protocol(b)
    fp_ms = time_axis_ms(b)
    log('[calib] 已连上 %s@%d（板型 %s，帧周期 %.4f s，门距 %.4f m），采集 %.1f s'
        % (port, src.baud, b, fp_ms / 1000.0, BIN_PITCH, seconds))
    t_end = time.time() + seconds
    idx = 0
    try:
        while time.time() < t_end:
            chunk = src.read(0.05)
            if not chunk:
                continue
            for rf in parser.feed(chunk):
                pair = asm.push(rf)
                if pair is None:
                    continue
                idx += 1
                yield idx, idx * fp_ms, proc.process(pair[0], pair[1])[0]
    finally:
        src.close()
    log('[calib] 采集结束：%d 帧（坏包 %d，丢弃帧 %d）'
        % (idx, parser.bad_packets, asm.dropped))


def make_processor(cal, args):
    """构造点云处理器：校准文件里的 processor 段作底，命令行显式参数优先。

    优先级：**显式命令行参数 > calib.json 的 processor 段 > 内置默认**。
    为此 `--cfar/--min-snr/--min-range` 的 argparse 默认必须是 None
    （用 `or` 或直接赋默认值都会破坏该优先级：`or` 会把显式传入的 falsy/更低值
    当成「没传」，直接赋默认值则会让 calib 覆盖显式参数）。
    """
    from radar_dsp import PointCloudProcessor
    p = (cal or {}).get('processor', {}) or {}
    # [!] min_range 在本工具里**固定为 0**（不随命令行/calib.json 走）。理由：
    #     band 模式的核心产出是「哪个距离门有泄漏/有人」（leak_bins / occ /
    #     pa_share），它必须统计**未被截断**的原始点云。否则一旦把实测到的泄漏门
    #     写进 processor.min_range，下一次校准就再也看不到那个门（leak_bins 变空、
    #     推荐带下界被顶到下一个门）—— 屏蔽项会把自己从证据里删掉，形成自我掩盖。
    #     「屏蔽」是应用层的事（server.py / radar_actions.py 读 calib.json 生效），
    #     不属于校准统计口径。
    want_range = getattr(args, 'min_range', None)
    if want_range not in (None, 0.0):
        print('[calib] 注意：--min-range=%s 在校准统计中不生效（固定用 0，'
              '以保证距离门占用率是「未截断」口径）；近距屏蔽请交给 '
              'server.py / radar_actions.py 执行。' % want_range)
    return PointCloudProcessor(
        cfar_scale=(args.cfar if args.cfar is not None
                    else p.get('cfar_scale', 2.6)),
        min_snr=(getattr(args, 'min_snr', None) if getattr(args, 'min_snr', None)
                 is not None else p.get('min_snr', 7.0)),
        min_range=0.0,
        angle_sign=p.get('angle_sign', 1.0),
        angle_max=p.get('angle_max', 45.0),
    )


def pct(a, q):
    return float(np.percentile(np.asarray(a, dtype=float), q)) if len(a) else float('nan')


def r3(x):
    try:
        return round(float(x), 3)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ band 模式
def mode_band(frames, band, log=print):
    """逐距离门统计：占用率、点数/帧、中位 |v|、功率占比。

    注意这是**观测记录**，不是动作准确率结论。
    """
    occ = {}
    cnt = {}
    pow_ = {}
    vs = {}
    rs = {}
    n_frames = 0
    for idx, t, pts in frames:
        n_frames += 1
        seen = set()
        for p in pts:
            j = bin_index(p['r'])
            cnt[j] = cnt.get(j, 0) + 1
            pow_[j] = pow_.get(j, 0.0) + max(float(p.get('pa', p.get('p', 0.0))), 0.0)
            vs.setdefault(j, []).append(abs(float(p['v'])))
            rs.setdefault(j, []).append(float(p['r']))
            seen.add(j)
        for j in seen:
            occ[j] = occ.get(j, 0) + 1

    total_pa = sum(pow_.values()) or 1.0
    rows = []
    for j in sorted(cnt):
        rows.append({
            'bin': j, 'r_m': r3((j + 0.5) * BIN_PITCH),
            'occ': r3(occ.get(j, 0) / max(n_frames, 1)),
            'pts_per_frame': r3(cnt[j] / max(n_frames, 1)),
            'pa_share': r3(pow_[j] / total_pa),
            'v_med': r3(np.median(vs[j])) if vs.get(j) else None,
            'r_med': r3(np.median(rs[j])) if rs.get(j) else None,
        })

    log('')
    log('  距离门  r(m)    占用率   点数/帧  功率占比  中位|v|(m/s)')
    for r in rows:
        log('  j=%-3d  %5.2f   %6.1f%%  %7.2f  %7.2f%%  %10s'
            % (r['bin'], r['r_m'], 100 * r['occ'], r['pts_per_frame'],
               100 * r['pa_share'], r['v_med']))

    # 推荐距离带：排除近场泄漏门，取「占用率达标」的连续段
    leak = [r['bin'] for r in rows if r['r_m'] < LEAK_RANGE]
    cand = [r for r in rows if r['r_m'] >= LEAK_RANGE and r['occ'] >= BAND_OCC_MIN]
    rec_band = None
    if cand:
        js = [r['bin'] for r in cand]
        # 取点数/帧最大的门，向两侧吸收相邻的达标门
        best = max(cand, key=lambda r: r['pts_per_frame'])['bin']
        lo = hi = best
        while (lo - 1) in js:
            lo -= 1
        while (hi + 1) in js:
            hi += 1
        rec_band = (r3(lo * BIN_PITCH), r3((hi + 1) * BIN_PITCH))
    log('')
    log('[calib] 近场泄漏门（r < %.2f m，必排除）: %s' % (LEAK_RANGE, leak or '无'))
    if rec_band:
        log('[calib] 推荐距离带 band = [%.2f, %.2f]  '
            '（占用率 ≥ %.0f%% 的连续段，提案，待小组确认）'
            % (rec_band[0], rec_band[1], 100 * BAND_OCC_MIN))
        if not (band[0] <= rec_band[0] and rec_band[1] <= band[1]):
            log('[calib] [!] 与当前使用中的 band=%s 不一致，请人工确认后写入' % (band,))
    else:
        log('[calib] [x] 没有任何距离门占用率 ≥ %.0f%%：'
            '要么人没站在雷达前，要么灵敏度太低（可试 --cfar 1.8 --min-snr 3.0）'
            % (100 * BAND_OCC_MIN))
    return {'n_frames': n_frames, 'bins': rows, 'leak_bins': leak,
            'recommended_band': list(rec_band) if rec_band else None}


# ------------------------------------------------------------------ rest 模式
def mode_rest(frames, band, log=print):
    """静息（人不做动作）→ 量噪声 → 推荐阈值。

    估计器与识别器**完全一致**（同一个 ActionRecognizer 实例），
    否则量到的噪声和实际判定的噪声不是一回事。
    不需要真值，因此这条结论可以直接采信。
    """
    # ⚠ 2026-10-20：显式 crit='delta'。本模块估的是 delta 的参数（v_on / v_off / min_dx），
    #   而 live 默认判据已改为 'xbase'，且 xbase 有**独立**的门限（xbase_thr_left/right，
    #   不吃 calib 的 v_on/min_dx）。⇒ 本工具**不校 xbase 门限**；这两套参数在 calib.json
    #   里是分开的，别把这里算出的 v_on 当成 xbase 的阈值。见 devlog §7.45.6。
    rec = ra.ActionRecognizer(band=band, crit='delta')
    v_lat, dx, px, steps, rej = [], [], [], [], 0
    n_frames = 0
    n_target = 0
    last_px = None
    # 必须先落地成 list：下面要做「回填阈值后在**同一段数据**上重跑」的闭环检查，
    # 生成器只能迭代一次，若不落地闭环检查会永远看到 0 次误报（假的通过）。
    seq = [(t, pts) for _, t, pts in frames]
    for t, pts in seq:
        n_frames += 1
        r = rec.push(pts, t / 1000.0)
        f = r['features']
        if f and f.get('rejected'):
            rej += 1
            continue
        if f and 'px' in f:
            n_target += 1
            px.append(f['px'])
            if last_px is not None:
                steps.append(abs(f['px'] - last_px))
            last_px = f['px']
        if f and 'v_lat' in f:
            v_lat.append(abs(f['v_lat']))
            dx.append(abs(f['dx']))

    log('')
    log('  帧数 %d，带内检出 %d 帧（%.1f%%），野值剔除 %d 帧'
        % (n_frames, n_target, 100.0 * n_target / max(n_frames, 1), rej))
    if len(v_lat) < 30:
        log('[calib] [x] 有效估计样本太少（%d）：人可能没在雷达前，或带内点太少。'
            '校准结论不可靠，请先解决「带内有点」再校准。' % len(v_lat))
        return None
    if n_target < 0.5 * n_frames:
        log('[calib] [!] 带内检出率不足 50%：噪声统计会偏乐观（拿不到估计的帧被跳过了），'
            '建议先按 band 模式确认距离带。')

    v95, v99 = pct(v_lat, 95), pct(v_lat, 99)
    d95 = pct(dx, 95)
    s99 = pct(steps, 99) if steps else 0.0
    v_on = max(V_ON_FLOOR, v95)
    v_off = V_OFF_RATIO * v_on
    min_dx = max(MIN_DX_FLOOR, d95)
    max_step = min(MAX_STEP_MAX, max(MAX_STEP_MIN, MAX_STEP_K * s99))

    log('  静息噪声（相对位移估计）: |v_lat| p95=%.3f p99=%.3f m/s；'
        '|dx| p95=%.3f m；逐帧 |Δpx| p99=%.3f m'
        % (v95, v99, d95, s99))
    log('[calib] 推荐 v_on=%.3f（= max(%.2f, p95)）  v_off=%.3f（= %.2f×v_on）'
        % (v_on, V_ON_FLOOR, v_off, V_OFF_RATIO))
    log('[calib] 推荐 min_dx=%.3f（= max(%.2f, |dx| p95)）  max_step=%.3f（= %.1f×p99，'
        '夹在 %.2f~%.2f）' % (min_dx, MIN_DX_FLOOR, max_step, MAX_STEP_K,
                            MAX_STEP_MIN, MAX_STEP_MAX))

    # 闭环检查：用回填后的阈值在原数据上重跑，数「静息期间误报了几次」
    rec2 = ra.ActionRecognizer(band=band, crit='delta', v_on=v_on, v_off=v_off,
                               min_dx=min_dx, max_step=max_step)
    fp = {}
    for t, pts in seq:
        for e in rec2.push(pts, t / 1000.0)['events']:
            if e['name'].startswith('LANE_'):
                fp[e['name']] = fp.get(e['name'], 0) + 1
    log('[calib] 闭环检查：回填阈值后在**同一段静息数据**上重跑，'
        '左右误报 = %s' % (fp or '0 次'))
    log('        （这段数据是静息数据，所以这里越接近 0 越好；'
        '不为 0 说明阈值仍偏松或此段不是真的静息）')
    # P2-2：阈值可信度必须随阈值一起落盘。判据取「有效估计帧数」与
    # 「闭环静息误报是否收敛到 0」两项 —— 二者都弱时阈值只是占位值。
    n_est = len(v_lat)
    if n_est >= 200 and not fp:
        tconf = 'medium'
    elif n_est >= 200 or not fp:
        tconf = 'low'
    else:
        tconf = 'very_low'
    log('  阈值可信度 = %s（有效估计帧 %d，闭环静息误报 %s）'
        % (tconf, n_est, fp if fp else 0))
    return {'v_on': r3(v_on), 'v_off': r3(v_off), 'min_dx': r3(min_dx),
            'max_step': r3(max_step),
            'threshold_confidence': tconf,
            'noise': {'n_frames': n_frames, 'n_target': n_target, 'n_est': n_est,
                      'rejected': rej, 'v_lat_p95': r3(v95), 'v_lat_p99': r3(v99),
                      'dx_p95': r3(d95), 'step_p99': r3(s99)},
            'rest_false_positives': fp}


# ------------------------------------------------------------------ sign 模式
def _drift_over(px, ts, i0, i1):
    """对 [i0, i1) 区间做与识别器同源的稳健位移估计（相对位移，非绝对位置）。"""
    xs = np.asarray(px[i0:i1], dtype=float)
    tt = np.asarray(ts[i0:i1], dtype=float)
    if len(xs) < 4:
        return None, None
    k = max(1, len(xs) // 3)
    dx = float(np.median(xs[-k:]) - np.median(xs[:k]))
    slopes = []
    for a in range(len(xs) - 1):
        for b in range(a + 1, len(xs)):
            dt = tt[b] - tt[a]
            if dt > 1e-9:
                slopes.append((xs[b] - xs[a]) / dt)
    v = float(np.median(slopes)) if slopes else 0.0
    return dx, v


def mode_sign(frames, band, expect, labels=None, rep=1, log=print):
    """引导人往已知方向移动一次 → 量出位移符号 → 推荐 angle_sign。

    必须有真值（`--expect left|right`，即人实际往哪边动了）；没有真值的校准无效。
    """
    rec = ra.ActionRecognizer(band=band)
    px, ts = [], []
    for idx, t, pts in frames:
        f = rec.push(pts, t / 1000.0)['features']
        if f and f.get('px') is not None and not f.get('rejected'):
            px.append(f['px'])
            ts.append(t / 1000.0)
    if len(px) < 6:
        log('[calib] [x] 带内几乎没检到目标（%d 帧），无法判定符号。' % len(px))
        return None

    def m(x):
        return (x - px[0]) * 1000.0

    if labels and os.path.exists(labels):
        # 有真值标注时，用标注的动作区间定位这一组动作，
        # 避免把「左 5 次 + 右 5 次」这类多次动作混成一次估计。
        # 复用 label_tool 的既有解析（jsonl，跳 _meta 头行），不要另写一套口径。
        try:
            import label_tool as lt
            reps = lt.load_labels(labels)
            r = reps[rep - 1]
            f0 = int(r['frame_start'])
            f1 = int(r.get('frame_end') or r.get('frame_start'))
            act = r.get('action', '?')
            i0, i1 = f0, min(f1, len(px))
            dx, v = _drift_over(px, ts, i0, i1)
            log('[calib] 用标注第 %d 组（%s，帧 %d~%d）估计' % (rep, act, f0, f1))
            expect_from_label = {'LEFT': 'left', 'RIGHT': 'right'}.get(act)
            if expect_from_label and expect_from_label != expect:
                log('[calib] [!] 标注写的方向(%s)与 --expect(%s) 不一致：'
                    '以 --expect 为准，但请先确认哪个才是真值'
                    % (expect_from_label, expect))
        except Exception as e:
            log('[calib] 标注读取失败（%s），退回自动探测' % (e,))
            labels = None
    if not labels:
        # 自动探测：滑动 1.0 s 窗口里 |Δ| 最大的那一段（即「最明显的一次移动」）
        W = 8
        best = (0.0, 0, W)
        for i in range(0, max(1, len(px) - W)):
            d, _ = _drift_over(px, ts, i, i + W)
            if d is not None and abs(d) > best[0]:
                best = (abs(d), i, i + W)
        _, i0, i1 = best
        dx, v = _drift_over(px, ts, i0, i1)
        log('[calib] 自动探测到最明显的一次移动：帧 %d~%d（Δ=%.2f m）'
            % (m(i0), m(i1), dx if dx is not None else float('nan')))

    if dx is None:
        log('[calib] [x]样本不足，无法估计位移')
        return None

    # ---- 信号 vs 噪声：符号只有在「位移明显超过本段噪声」时才有意义 ----
    # 零假设分布 = 本段录音里所有 8 帧窗的 |Δ|（排除被标注的动作窗本身）。
    # 若这次移动的 |Δ| 连噪声 p95 都超不过，符号就是在赌噪声，
    # 这种情况下**必须拒绝给结论**，否则会输出一个看起来成功、其实无效的符号翻转。
    W = 8
    null = []
    for i in range(0, max(1, len(px) - W)):
        if i0 is not None and not (i + W <= i0 - 2 or i >= i1 + 2):
            continue            # 跳过与被测动作重叠的窗，避免噪声基线被自己抬高
        d, _ = _drift_over(px, ts, i, i + W)
        if d is not None:
            null.append(abs(d))
    p95 = pct(null, 95)
    ratio = (abs(dx) / p95) if p95 and p95 > 1e-9 else float('inf')
    log('')
    log('  本次移动 |Δpx| = %.3f m；本段噪声基线 p95(|Δpx|) = %.3f m（%d 个对照窗）；'
        '信噪比 = %.2f×' % (abs(dx), p95, len(null), ratio))
    if ratio < 1.0:
        log('[calib] [x]证据不足：本次移动的位移量**小于本段噪声的 p95**，'
            '信号与噪声不可区分 → 不给出 angle_sign 结论。')
        log('        注：这说明当前几何下「人体横向位移」本身弱于噪声，'
            '翻符号没有意义；应优先提高带内点数（灵敏度/站位/距离带），'
            '而不是调符号。')
        return None
    if ratio < 2.0:
        log('[calib] [!] 信噪比偏低（%.2f×，未到 2 倍）：结论只能作参考，'
            '建议重做一次更清晰的移动。' % ratio)
    # P2-1：把信噪比一并落盘。只写 angle_sign 而不写它的可信度，
    # 会让后来者（或另一次会话）把一条低信噪比结论误当成硬证据。
    conf = 'low' if ratio < 2.0 else ('medium' if ratio < 3.0 else 'high')

    moved_right = dx > 0
    agree = (moved_right and expect == 'right') or ((not moved_right) and expect == 'left')
    log('')
    log('  真值：人实际往【%s】移动' % ('左' if expect == 'left' else '右'))
    log('  实测：稳健 Δpx = %+.3f m，稳健斜率 = %+.3f m/s  → 系统判为【%s】'
        % (dx, v, '右' if moved_right else '左'))
    if agree:
        log('[calib] [ok] 与真值一致 → 推荐 angle_sign = +1.0（保持当前符号）')
        sign = 1.0
    else:
        log('[calib] [x] 与真值相反 → 推荐 angle_sign = -1.0（把方位角符号翻转）')
        sign = -1.0
    log('        [!] 这条结论依赖「人确实往 %s 动了」这个真值；'
        '若你自己都不确定，请重做。' % ('左' if expect == 'left' else '右'))
    return {'angle_sign': sign,
            'sign_confidence': conf,
            'sign_evidence': {'expect': expect, 'dx': r3(dx), 'v_lat': r3(v),
                              'observed': 'right' if moved_right else 'left',
                              'snr_ratio': r3(ratio), 'noise_p95': r3(p95),
                              'n_null_windows': len(null),
                              'confidence': conf,
                              'agree': bool(agree), 'window': [int(i0), int(i1)]}}


# ------------------------------------------------------------------ 主流程
def main(argv=None):
    ap = argparse.ArgumentParser(
        description='近距动作识别 · 现场校准（band / rest / sign）')
    ap.add_argument('--mode', required=True, choices=['band', 'rest', 'sign'])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--file', help='离线：录制文件（.bin）')
    src.add_argument('--port', help='实时：串口，如 COM3')
    ap.add_argument('--baud', type=int, default=None,
                    help='串口波特率；默认 None = 按板型指纹自动'
                         '（1T2R 921600 / 2T4R 1500000）')
    ap.add_argument('--board', default='auto',
                    choices=['auto', '1t2r', '2t4r'],
                    help='板型；默认 auto = 按文件/串口指纹自动判断')
    ap.add_argument('--seconds', type=float, default=20.0, help='实时采集时长（秒）')
    ap.add_argument('--band', type=float, nargs=2, default=None,
                    metavar=('RMIN', 'RMAX'), help='当前使用的距离带（米）')
    ap.add_argument('--expect', choices=['left', 'right'], default=None,
                    help='sign 模式必填：人这次实际往哪边移动（真值）')
    ap.add_argument('--labels', default=None, help='sign 模式可选：真值标注 jsonl')
    ap.add_argument('--rep', type=int, default=1, help='sign 模式用标注里的第几组（1 起）')
    ap.add_argument('--limit', type=int, default=None, help='离线只读前 N 帧（调试）')
    ap.add_argument('--cfar', type=float, default=None,
                    help='CFAR 门限倍数（内置默认 2.6）')
    ap.add_argument('--min-snr', type=float, default=None)
    ap.add_argument('--min-range', type=float, default=None)
    ap.add_argument('--calib', default=None, help='已有校准文件（作为本次的底稿）')
    ap.add_argument('--out', default=DEFAULT_OUT, help='输出 calib.json 路径')
    ap.add_argument('--dry-run', action='store_true', help='只打印，不写文件')
    args = ap.parse_args(argv)

    if args.mode == 'sign' and not args.expect:
        ap.error('sign 模式必须给 --expect left|right（人实际移动方向的真值），'
                 '没有真值的校准无效')

    # 底稿：--calib 指定，或 --out 已存在就接着更新
    base = {}
    for p in (args.calib, args.out):
        if p and os.path.exists(p):
            try:
                base = json.loads(open(p, encoding='utf-8').read())
                print('[calib] 载入底稿 %s' % p)
                break
            except Exception as e:
                print('[calib] 底稿 %s 读取失败：%s' % (p, e))
    band = tuple(args.band) if args.band else tuple(base.get('band', ra.BAND_DEFAULT))
    proc = make_processor(base, args)

    print('=' * 74)
    print('校准 · mode=%s   来源=%s   距离带=%s'
          % (args.mode, args.file or ('串口 ' + args.port), band))
    print('=' * 74)

    board_arg = None if getattr(args, 'board', 'auto') in (None, 'auto') \
        else args.board
    if args.file:
        it = frames_from_file(args.file, proc, limit=args.limit,
                              board=board_arg)
    else:
        if args.mode == 'rest':
            print('>> 请站到雷达前【保持不动】%.0f 秒（可以自然呼吸，别迈步/挥手）'
                  % args.seconds)
        elif args.mode == 'sign':
            print('>> 站好，然后按下面这个方向**移动一次**并停住')
            print('>> 本次方向（真值）：%s' % ('左' if args.expect == 'left' else '右'))
        else:
            print('>> 请站在雷达前，保持正常站位 %.0f 秒（band 模式只看距离分布）'
                  % args.seconds)
        print('>> 倒计时 3 秒后开始…')
        for i in (3, 2, 1):
            print('   %d' % i)
            time.sleep(1)
        it = frames_live(args.port, args.baud, args.seconds, proc,
                         board=board_arg)

    if args.mode == 'band':
        res = mode_band(it, band)
        patch = {'band_source': 'measured:band',
                 'band_source_file': args.file or ('serial:%s' % args.port)}
        if res['recommended_band']:
            patch['band'] = res['recommended_band']
        patch['band_stats'] = res
    elif args.mode == 'rest':
        res = mode_rest(it, band)
        if res is None:
            print('[calib] 未产出阈值（样本不足）')
            return 2
        patch = {'band': list(band), 'v_on': res['v_on'], 'v_off': res['v_off'],
                 'min_dx': res['min_dx'], 'max_step': res['max_step'],
                 'noise': res['noise'],
                 'threshold_confidence': res.get('threshold_confidence'),
                 'rest_false_positives': res['rest_false_positives'],
                 'threshold_source': 'measured:rest',
                 'threshold_source_file': args.file or ('serial:%s' % args.port)}
    else:
        res = mode_sign(it, band, args.expect, labels=args.labels, rep=args.rep)
        if res is None:
            print('[calib] 未产出符号（样本不足）')
            return 2
        patch = {'sign_confidence': res.get('sign_confidence'),
                 'processor': dict(base.get('processor') or {},
                                   angle_sign=res['angle_sign'])}
        # [!] 若信噪比不足，sign 模式已在 mode_sign 内 return None（EXIT=2），
        # 这里落盘的必定是 ratio>=1.0 的结论，但仍带 confidence 便于追溯强度。
        patch['sign_evidence'] = res['sign_evidence']

    out = dict(base)
    out.update(patch)
    out['version'] = 1
    out['proposal'] = True
    out['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    out.setdefault('created', out['updated'])
    out.setdefault('processor', {'angle_sign': 1.0, 'min_snr': 7.0,
                                 'min_range': 0.0, 'angle_max': 45.0})
    # 把本模式**实际生效**的 DSP 参数写回 processor 段：
    # 否则多次运行会留下陈旧值（例如上一次的 min_snr=7.0），
    # 导致「按 calib.json 复现现场设置」失真 —— 而 cfar/snr 恰是灵敏度最大杠杆。
    # 注意 angle_sign 保留已由 sign 模式判定的结果，不被本次输入值覆盖。
    p_used = dict(out.get('processor') or {})
    p_used.update(cfar_scale=float(getattr(
                      proc, 'cfar_scale', None) or args.cfar or 2.6),
                  min_snr=float(getattr(proc, 'min_snr', 7.0)),
                  angle_max=float(getattr(proc, 'angle_max', 45.0)))
    p_used.setdefault('angle_sign', float(getattr(proc, 'angle_sign', 1.0)))
    # min_range **不取 proc 的值**（校准恒用 0，见 make_processor 的说明），而是由
    # 本次实测的泄漏门推算：(最远的泄漏门 + 1) × 距离门宽 = 「整门排除泄漏」的边界。
    # 这样「识别到泄漏门」自动变成「应用层会执行的屏蔽值」，闭环不再依赖人工转录；
    # 而统计用的是未截断口径，所以下一次仍能看见同一个门（不会自我掩盖）。
    # rest / sign 模式没有 leak_bins，沿用文件里已有的值，不擅自改写。
    leak_bins = res.get('leak_bins') if isinstance(res, dict) else None
    if leak_bins:
        p_used['min_range'] = round((max(leak_bins) + 1) * BIN_PITCH, 3)
        print('[calib] 实测泄漏门 %s → processor.min_range = %s'
              '（应用层将据此屏蔽近场泄漏云）' % (leak_bins, p_used['min_range']))
    elif 'min_range' not in p_used:
        p_used['min_range'] = 0.0
    out['processor'] = p_used
    out.setdefault('source', args.file or ('serial:%s' % args.port))
    notes = list(out.get('notes') or [])
    notes.append('本文件所有阈值均为实测提案，待小组确认（AGENTS.md 证据纪律）；'
                 '判定原则仍是「相对位移方向」，校准只调阈值与距离带。')
    out['notes'] = notes

    print('')
    print('[calib] 结果: %s' % json.dumps(patch, ensure_ascii=False)[:400])
    if args.dry_run:
        print('[calib] --dry-run：未写文件')
    else:
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print('[calib] 已写出 %s' % args.out)
        print('[calib] 用法：python radar_actions.py --file X.bin --calib %s'
              % os.path.basename(args.out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
