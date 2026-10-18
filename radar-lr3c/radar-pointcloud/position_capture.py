# -*- coding: utf-8 -*-
"""站位自检引导采集（提示音驱动、自动分段、采完当场出判据）。

为什么要有这个脚本（`docs/capture-checklist.md` §0.5 / `docs/progress-radar-pointcloud.md` §7.18）：
- 本轮离线 A/B 的结论是「**信号不存在**」（所有配置信噪比 < 1，标注 1.5 m 的横移实测位移
  只有 0.068 m），继续调算法已无信息可挖，必须采新数据。
- 当前最大的悬案是「**j=2 之谜**」：人标称站 1.00 m，实测主回波（j=2，门中心 1.70 m）
  却占 90.2% 功率，在用距离带只吃到 9.6% —— 是距离标定有恒定偏差，还是身后的强静态
  反射主导？**只有「换站位看主回波跟不跟着动」这一条路能区分**。
- 站位段人本来就是站定不动，所以这 4 段**同时也是静息数据**：一份数据同时回答
  「主回波是否跟随站位」与「静息噪声 / 阈值 / 闭环误报」。

与 `segment_capture.py` 的关系：
  复用它的同一套底层（`beep`/帧统计 `STATE`/`on_frame`/`wait_frames`/`probe_port`/
  `make_source`/管道 `radar_source.build_pipeline`），保证提示音体感与真值口径完全一致；
  差别只在流程——**一站一个独立文件**（站位之间要走路，走路是真实运动数据，混在一个
  文件里会污染静息口径），且不需要动作真值（无动作本身就是真值）。

时间基准（与全项目一致，别用墙钟）：
  时间轴 = **帧号 × 0.1295 s**（`radar_actions.FRAME_PERIOD`）。每个站位是一个独立会话，
  因此每文件内的帧号从 1 开始重新计数，与记录下来的窗口帧号一一对应。

用法：
  python position_capture.py --port COM3                     # 真机（人要在设备旁）
  python position_capture.py --source loopback --stations 0.70,1.00 --duration 3
                                                             # 无硬件冒烟测试
  python position_capture.py --analyze-only --stations 1.0,1.7 ^
      --files .deepworks\\tmp\\seg\\seg_01.00.bin,.deepworks\\tmp\\seg\\seg_01.50.bin
                                                             # 只分析已有文件，不采集

保密：产物为不含图像的人体数据，对外分享前需按课程保密要求确认。
"""
import argparse
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import radar_actions as ra            # noqa: E402
import radar_source as rs             # noqa: E402
import label_tool as lt               # noqa: E402
import radar_calibrate as rc          # noqa: E402
import segment_capture as sc          # noqa: E402  复用提示音/帧统计（体感与真值口径一致）

FP = ra.FRAME_PERIOD                  # 0.1295 s/帧
BIN_PITCH = 0.6813                    # 距离门间距（m）
LEAK_RANGE = getattr(rc, 'LEAK_RANGE', 0.70)   # 近场泄漏门，必排除（实测）
BAND_DEFAULT = tuple(getattr(ra, 'BAND_DEFAULT', (0.70, 1.35)))

BEEP_START = 'start'                  # 高音=开始
BEEP_STOP = 'stop'                    # 低音=结束


def _dw(s):
    """显示宽度：中日韩字符占两列，用来把中文表头对齐。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def _pad(s, n):
    return s + ' ' * max(0, n - _dw(s))


def _tag(dist):
    """站位距离 → 文件名标签：1.0 → '1p00'。"""
    return ('%.2f' % float(dist)).replace('.', 'p')


def hold(secs, text, tick=5.0, kind=BEEP_START, end_kind=None, debug=False):
    """提示 + 蜂鸣 + 保持 secs 秒；返回 (窗口开始帧号, 窗口结束帧号)。

    与 `segment_capture.cue` 同一口径：**窗口起点是蜂鸣响起的那一刻**，
    所以调用方拿返回值第一个数当区间起点。
    25 s 的静息段不能全程无提示，所以每 tick 秒打一次进度。
    kind/end_kind 传 None 表示该处不鸣笛（准备阶段保持安静）。
    """
    if kind:
        sc.beep(kind=kind)
    print('    %s  [%.1fs]' % (text, secs), flush=True)
    f_begin = sc.STATE['frames']
    t0 = time.time()
    last_tick = 0
    while True:
        el = time.time() - t0
        if el >= secs:
            break
        if el - last_tick >= tick:
            last_tick = el
            print('        ... 已保持 %.0f s / %.0f s（继续别动）'
                  % (el, secs), flush=True)
        time.sleep(0.02)
    f_after = sc.STATE['frames']
    took = time.time() - t0
    if end_kind:
        sc.beep(kind=end_kind)
    if debug:
        print('      [debug] 窗口 %.2fs：帧号 %d -> %d（+%d）'
              % (took, f_begin, f_after, f_after - f_begin), flush=True)
    return f_begin, f_after


# ------------------------------------------------------------------ 采集
def capture_station(args, dist, bin_path, labels_path, index, total):
    """采集一个站位（一个独立会话 → 一个独立文件）。成功返回记录 dict，失败返回 None。"""
    want_frames = args.duration / FP
    print('')
    print('=' * 76)
    print('第 %d/%d 段：站位 %.2f m' % (index, total, dist))
    print('  录制: %s' % bin_path)
    print('  要求: 站定**完全不动** %.0f s（约 %.0f 帧）' % (args.duration, want_frames))
    print('=' * 76)

    sc.pause('走到 %.2f m 的标记处站好：正对雷达、脚跟对齐标记，站定后别再挪' % dist)

    stop = threading.Event()
    src = sc.make_source(args)          # 每段新数据源（Loopback/Replay 有内部计时状态）
    th = threading.Thread(target=rs.build_pipeline,
                          args=(src, sc.on_frame, stop),
                          kwargs={'record_path': bin_path}, daemon=True)
    th.start()
    try:
        if not sc.wait_frames(3, args.start_delay + 5):
            print('  !! 没有收到任何雷达帧，本段作废（检查设备/串口/是否被别的进程占着）。')
            return None

        if args.warmup > 0:
            hold(args.warmup, '先做信号稳定（保持不动，这段不计入统计）',
                 tick=99, kind=None)

        # 高音 = 正式开始（真值窗口起点）；低音 = 结束可以动
        hold(3.0, '稳住，3 秒后正式开始记录', tick=99, kind=None)
        f0, f1 = hold(args.duration, '正式记录：完全不动',
                      kind=BEEP_START, end_kind=BEEP_STOP,
                      debug=args.debug_frames)
    finally:
        stop.set()
        th.join(timeout=5.0)

    got = f1 - f0
    fps = sc.STATE.get('fps', 0.0)
    print('  本段收帧 %d（窗口内 %d，期望约 %.0f），实测 %.2f 帧/秒'
          % (sc.STATE['frames'], got, want_frames, fps))

    if got < 0.6 * want_frames:
        print('  [!] 窗口内帧数明显偏少：本段**不建议**进入统计（宁可重采）。')
        valid = False
    else:
        valid = True

    meta = {'file': os.path.basename(bin_path), 'distance_m': float(dist),
            'band': list(args.band), 'frame_period': FP,
            'frames_file': int(sc.STATE['frames']),
            'window': {'frame_start': int(f0), 'frame_end': int(f1),
                       't_start': round(f0 * FP, 3), 't_end': round(f1 * FP, 3)},
            'fps_measured': round(float(fps), 3),
            'source': args.source,
            'valid': bool(valid),
            'note': '站位自检引导采集（position_capture.py）：本段是静息（人站定不动），'
                    '**无动作真值** -> 任何 LANE_* 预测都计为误报；'
                    '时间轴 = 帧号 × 0.1295 s'}
    lt.save_labels(labels_path, [], meta)
    print('  真值文件（0 条动作 + 元数据）-> %s' % labels_path)

    return {'dist': float(dist), 'bin': bin_path, 'labels': labels_path,
            'valid': valid, 'frames_window': got, 'frames_file': sc.STATE['frames'],
            'f0': int(f0), 'f1': int(f1), 'fps': float(fps),
            'walking_frames': int(f0),      # 窗口前 = 稳定期（未混入段间走动，段间走动没在录）
            'dropped': int(sc.STATE.get('skipped', 0))}


# ------------------------------------------------------------------ 分析
def window_frames(bin_path, proc, f0=None, f1=None):
    """只取 [f0, f1] 窗口内的帧，避免把管道刚打开时的垃圾帧算进统计。"""
    for idx, t, pts in rc.frames_from_file(bin_path, proc, log=lambda m: None):
        if f0 is not None and idx < f0:
            continue
        if f1 is not None and idx > f1:
            break
        yield idx, t, pts


def main_echo(bins):
    """主回波门 = 功率占比最大、且不在近场泄漏区的门。"""
    cand = [b for b in bins if (b['r_m'] or 0) >= LEAK_RANGE]
    if not cand:
        return None
    return max(cand, key=lambda b: (b['pa_share'] or 0.0))


def analyze_station(rec, args, proc):
    """对单站位做 band 统计 → 观测记录（不是准确率结论）。"""
    frames = window_frames(rec['bin'], proc,
                           rec.get('f0') if args.use_window else None,
                           rec.get('f1') if args.use_window else None)
    res = rc.mode_band(frames, args.band, log=lambda m: None)
    me = main_echo(res['bins'])
    rec['n_frames'] = res['n_frames']
    rec['bins'] = res['bins']
    rec['recommended_band'] = res['recommended_band']
    rec['main_bin'] = me['bin'] if me else None
    rec['main_r'] = me['r_m'] if me else None
    rec['main_pa_share'] = me['pa_share'] if me else None
    rec['main_r_med'] = me['r_med'] if me else None
    rec['offset_m'] = (round(me['r_med'] - rec['dist'], 3)
                       if (me and me['r_med'] is not None) else None)
    return rec


def verdict_lines(recs):
    """把观测转成「提示」——**不是结论**，必须人工核对后才能写进文档。"""
    ok = [r for r in recs
          if r.get('valid') and r.get('main_bin') is not None
          and r.get('main_r_med') is not None]
    out = []
    if len(ok) < 2:
        out.append('[x] 有效站位不足 2 个，无法判断主回波是否随站位平移。')
        return out

    rs_med = [r['main_r_med'] for r in ok]
    spread = max(rs_med) - min(rs_med)
    offs = [r['offset_m'] for r in ok if r['offset_m'] is not None]
    inc = sum(1 for a, b in zip(rs_med, rs_med[1:]) if b > a)
    n_pair = len(rs_med) - 1

    out.append('主回波 r_med: %s'
               % '  '.join('%.2fm->%.2fm' % (r['dist'], r['main_r_med']) for r in ok))
    out.append('每站偏移量 (r_med - 站位): %s'
               % '  '.join('%.2fm:%+.2fm' % (r['dist'], r['offset_m']) for r in ok))
    out.append('跨度 %.2f m（约 %.1f 个距离门），非降序对 %d/%d'
               % (spread, spread / BIN_PITCH, inc, n_pair))

    if spread < 0.5:
        out.append('[自动提示] 主回波**不随站位平移**（跨度 < 0.5 m）-> 更像环境强静态反射'
                   '/静态杂波主导，而不是距离标定偏差。先排查板后 1 m 内的金属、墙、'
                   '显示器，或改安装位置。')
    elif offs and (max(offs) - min(offs)) <= 0.35:
        out.append('[自动提示] 主回波**跟随站位平移**，且偏移量近似恒定（%.2f~%.2f m，'
                   '约 %.1f 个距离门）-> 更像**恒定距离偏移**（标定/门索引偏差）'
                   '，可把距离带按实测主回波整体搬移。'
                   % (min(offs), max(offs), (max(offs) + min(offs)) / 2 / BIN_PITCH))
    else:
        out.append('[自动提示] 主回波**跟随站位平移但偏移量不恒定**（%.2f~%.2f m）'
                   '-> 需要更多站位点或重采后才能定性。'
                   % (min(offs), max(offs)))
    out.append('  （以上由程序按阈值自动生成，属**提示**，不是结论；'
               '写进文档前必须人工核对原始逐门表与现场记录。）')
    return out


def build_report(args, recs, rest, outdir, started, elapsed):
    import datetime
    lines = []
    lines.append('# 站位自检采集报告（position_capture.py）')
    lines.append('')
    lines.append('- 生成时间: %s' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    if args.analyze_only:
        # 只分析已有文件时，不能写「数据源 serial」「静息时长 25 s」——那件事没发生
        lines.append('- 模式: **仅分析已有文件（本次未采集）**')
        lines.append('- 分析文件: %s'
                     % '、'.join(os.path.basename(r['bin']) for r in recs))
        lines.append('- 站位（m，按 --stations 顺序对应上面的文件）: %s'
                     % '、'.join('%.2f' % r['dist'] for r in recs))
    else:
        lines.append('- 数据源: %s%s'
                     % (args.source,
                        ('（%s）' % args.replay_file) if args.replay_file else ''))
        lines.append('- 站位（m）: %s' % '、'.join('%.2f' % r['dist'] for r in recs))
        lines.append('- 静息时长/站: %.1f s（约 %.0f 帧）'
                     % (args.duration, args.duration / FP))
    lines.append('- 距离带（分析用）: %s m' % str(tuple(args.band)))
    lines.append('- 校准文件: %s' % (args.calib or '（未使用）'))
    lines.append('- %s: %s 起，用时 %.1f 分钟'
                 % ('分析区间' if args.analyze_only else '采集区间',
                    time.strftime('%H:%M:%S', time.localtime(started)), elapsed / 60.0))
    lines.append('')
    lines.append('> 本报告是**观测记录**，不是动作准确率结论。'
                 '跳/蹲不涉及（受控能力，验证门未过）。')
    lines.append('')
    lines.append('## 1. 主回波与站位对照（j=2 之谜的直接判据）')
    lines.append('')
    lines.append('| 站位(m) | 文件 | 窗口帧 | 主回波门 j | 门中心(m) | r_med(m) | 偏移(m) | 功率占比 | 有效 |')
    lines.append('|---|---|---|---|---|---|---|---|---|')
    for r in recs:
        lines.append('| %.2f | %s | %d | %s | %s | %s | %s | %s | %s |'
                     % (r['dist'], os.path.basename(r['bin']), r.get('frames_window', 0),
                        r.get('main_bin'), r.get('main_r'), r.get('main_r_med'),
                        r.get('offset_m'),
                        ('%.1f%%' % (100 * r['main_pa_share']))
                        if r.get('main_pa_share') is not None else '—',
                        'Y' if r.get('valid') else 'N'))
    lines.append('')
    lines.append('### 自动提示（非结论）')
    lines.append('')
    for l in verdict_lines(recs):
        lines.append('- %s' % l)
    lines.append('')
    lines.append('## 2. 逐距离门明细')
    lines.append('')
    for r in recs:
        lines.append('### 站位 %.2f m（%s）' % (r['dist'], os.path.basename(r['bin'])))
        lines.append('')
        lines.append('| 距离门 j | r(m) | 占用率 | 点数/帧 | 功率占比 | 中位\\|v\\|(m/s) |')
        lines.append('|---|---|---|---|---|---|')
        for b in r.get('bins') or []:
            lines.append('| %d | %.2f | %.1f%% | %.2f | %.2f%% | %s |'
                         % (b['bin'], b['r_m'], 100 * (b['occ'] or 0),
                            b['pts_per_frame'] or 0, 100 * (b['pa_share'] or 0),
                            b['v_med']))
        if r.get('recommended_band'):
            lines.append('')
            lines.append('推荐带（提案，待小组确认）: %s' % r['recommended_band'])
        lines.append('')
    lines.append('## 3. 静息噪声 / 阈值（各站静息合并）')
    lines.append('')
    if rest is None:
        lines.append('未做 rest 分析（--no-rest，或有效估计样本不足）。')
    else:
        n = rest['noise']
        lines.append('- 帧数 %d，带内检出 %d 帧（%.1f%%），野值剔除 %d 帧'
                     % (n['n_frames'], n['n_target'],
                        100.0 * n['n_target'] / max(n['n_frames'], 1), n['rejected']))
        lines.append('- 静息噪声: \\|v_lat\\| p95=%.3f p99=%.3f m/s；\\|dx\\| p95=%.3f m；'
                     '逐帧 \\|dpx\\| p99=%.3f m'
                     % (n['v_lat_p95'], n['v_lat_p99'], n['dx_p95'], n['step_p99']))
        lines.append('- **推荐阈值（提案，待小组确认）**: v_on=%.3f  v_off=%.3f  '
                     'min_dx=%.3f  max_step=%.3f'
                     % (rest['v_on'], rest['v_off'], rest['min_dx'], rest['max_step']))
        lines.append('- 闭环检查：回填阈值后在**同一段静息数据**上重跑，'
                     '左右误报 = %s' % (rest['rest_false_positives'] or '0 次'))
        lines.append('- 阈值可信度 = **%s**（有效估计帧 %d）'
                     % (rest['threshold_confidence'], n['n_est']))
    lines.append('')
    lines.append('## 4. 这份报告**不主张**什么')
    lines.append('')
    lines.append('- 不主张动作识别准确率：本批数据**只含静息**，没有任何动作真值。')
    lines.append('- 不涉及跳/蹲：仍是无真值、无俯仰角的受控能力，验证门未过。')
    lines.append('- 阈值是**提案**，待小组确认；单次采集 / 单人 / 单一安装几何。')
    lines.append('- 主回波判据来自程序自动提示，需人工核对后方可写入正式文档。')
    lines.append('')
    return '\n'.join(lines) + '\n'


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(
        description='站位自检引导采集（自动分段 + 采完当场出 j=2 判据）')
    ap.add_argument('--port', default='COM3')
    ap.add_argument('--baud', type=int, default=921600)
    ap.add_argument('--source', default='serial',
                    choices=['serial', 'loopback', 'replay'])
    ap.add_argument('--replay-file', default=None)
    ap.add_argument('--stations', default='0.70,1.00,1.35,1.70',
                    help='站位距离（米，逗号分隔）。默认含 1.00 与 1.70：'
                         'j=2 之谜的正对照')
    ap.add_argument('--duration', type=float, default=25.0,
                    help='每个站位的静息时长（秒）。建议 ≥25 s ≈ 193 帧')
    ap.add_argument('--warmup', type=float, default=2.0,
                    help='正式记录前的信号稳定秒数（不计入统计窗口）')
    ap.add_argument('--outdir',
                    default=os.path.join(os.path.dirname(HERE),
                                         '.deepworks', 'tmp', 'cap-1016'))
    ap.add_argument('--calib', default=None, help='可选 calib.json')
    ap.add_argument('--band', default=None,
                    help='分析用距离带，如 "0.70 1.35"；默认用内置默认带')
    ap.add_argument('--cfar', type=float, default=None,
                    help='显式覆盖（优先级：显式参数 > calib.json > 内置默认）')
    ap.add_argument('--min-snr', type=float, default=None)
    ap.add_argument('--min-range', type=float, default=None)
    ap.add_argument('--start-delay', type=float, default=10.0)
    ap.add_argument('--no-beep', action='store_true')
    ap.add_argument('--no-rest', action='store_true',
                    help='跳过四站合并的静息阈值分析')
    ap.add_argument('--no-window', action='store_false', dest='use_window',
                    help='统计整段（默认只统计正式记录窗口）')
    ap.add_argument('--analyze-only', action='store_true',
                    help='不采集，只分析 --files 给出的已有文件')
    ap.add_argument('--files', default=None,
                    help='--analyze-only 时的文件列表（逗号分隔，顺序对应 --stations）')
    ap.add_argument('--debug-frames', action='store_true')
    # 与另外三个入口一致的默认值：显式参数 > calib.json > 内置默认
    args = ap.parse_args(argv)
    sc._stdout_utf8()
    sc.NO_BEEP = bool(args.no_beep)

    stations = [float(x) for x in args.stations.split(',') if x.strip()]
    if args.band:
        args.band = tuple(float(x) for x in args.band.replace(',', ' ').split())
    else:
        args.band = BAND_DEFAULT

    cal = None
    if args.calib:
        with open(args.calib, 'r', encoding='utf-8') as f:
            cal = json.load(f)
        print('[cfg] 已载入校准文件 %s（processor 段作底，显式参数优先）' % args.calib)

    os.makedirs(args.outdir, exist_ok=True)

    started = time.time()
    recs = []

    if args.analyze_only:
        files = [x.strip() for x in (args.files or '').split(',') if x.strip()]
        if not files:
            raise SystemExit('--analyze-only 需要 --files')
        if len(files) != len(stations):
            print('[!] --files 个数(%d) 与 --stations 个数(%d) 不一致，'
                  '按较短的对齐' % (len(files), len(stations)))
        for dist, fp_ in zip(stations, files):
            # 只分析已有文件时，窗口信息要从同目录的标签文件 _meta.window 里取回来；
            # 否则 window_frames() 会退化成「整段统计」，把稳定期/倒计时的走动算进去。
            f0 = f1 = None
            lab = os.path.splitext(fp_)[0] + '.labels.jsonl'
            if args.use_window and os.path.exists(lab):
                try:
                    with open(lab, 'r', encoding='utf-8') as f:
                        head = json.loads(f.readline())
                    w = (head.get('_meta') or {}).get('window') or {}
                    if w.get('frame_start') is not None and w.get('frame_end') is not None:
                        f0, f1 = int(w['frame_start']), int(w['frame_end'])
                        print('[win] %s 用标签窗口 [%d, %d]（%d 帧）'
                              % (os.path.basename(fp_), f0, f1, f1 - f0))
                except Exception as e:
                    print('[!] %s 窗口读取失败，将按整段统计: %s' % (lab, e))
            if f0 is None:
                print('[win] %s 无可用窗口 -> 按整段统计'
                      '（可能混入稳定期走动，数字不可与在线报告直接比较）'
                      % os.path.basename(fp_))
            recs.append({'dist': float(dist), 'bin': fp_, 'valid': True,
                         'frames_window': (f1 - f0) if f0 is not None else 0,
                         'labels': lab, 'f0': f0, 'f1': f1})
    else:
        print('=' * 76)
        print('站位自检引导采集')
        print('  站位: %s m' % '、'.join('%.2f' % d for d in stations))
        print('  每站静息 %.0f s；预计采集约 %.1f 分钟（不含走路/贴标记时间）'
              % (args.duration,
                 len(stations) * (args.warmup + 3.0 + args.duration + 2.0) / 60.0))
        print('  说明: 一站一个文件；站位之间走路不会被录进去')
        print('=' * 76)

        if args.source == 'serial':
            err = sc.probe_port(args.port, args.baud)
            if err:
                print('')
                print('[x] 串口 %s 打不开: %s' % (args.port, err))
                print('    常见原因: (1) 别的进程占着串口（server.py/viewer/ONELAB），')
                print('              (2) 端口号不对，(3) 设备没插好。')
                print('    先把占用的进程关掉，或换 --port，然后重跑。')
                return 3
            print('[ok] 串口 %s 可用' % args.port)

        for i, dist in enumerate(stations, 1):
            tag = _tag(dist)
            bin_path = os.path.join(args.outdir, 'pos_%s.bin' % tag)
            labels_path = os.path.join(args.outdir, 'pos_%s.labels.jsonl' % tag)
            r = capture_station(args, dist, bin_path, labels_path, i, len(stations))
            if r is None:
                recs.append({'dist': float(dist), 'bin': bin_path, 'valid': False,
                             'frames_window': 0, 'labels': labels_path})
            else:
                recs.append(r)

    # ---------------- 分析 ----------------
    proc = rc.make_processor(cal, args)
    print('')
    print('=' * 76)
    print('分析（观测记录，不是准确率结论）')
    print('=' * 76)
    for r in recs:
        if not os.path.exists(r['bin']):
            print('[x] 文件不存在: %s' % r['bin'])
            r['valid'] = False
            continue
        if not r.get('valid', True):
            print('[!] 跳过作废段: %s' % r['bin'])
            continue
        analyze_station(r, args, proc)

    print('')
    print('-- 主回波 vs 站位（j=2 之谜判据）--')
    print('%s %8s %7s %8s %8s %9s'
          % (_pad('站位(m)', 9), '主回波门', '门中心', 'r_med', '偏移', '功率占比'))
    for r in recs:
        print('%s %8s %7s %8s %8s %9s'
              % (_pad('%.2f' % r['dist'], 9), r.get('main_bin', '—'),
                 r.get('main_r', '—'), r.get('main_r_med', '—'),
                 r.get('offset_m', '—'),
                 ('%.1f%%' % (100 * r['main_pa_share']))
                 if r.get('main_pa_share') is not None else '—'))
    print('')
    for l in verdict_lines(recs):
        print('  %s' % l)

    rest = None
    if not args.no_rest:
        ok = [r for r in recs if r.get('valid') and r.get('main_bin') is not None]
        if not ok:
            print('')
            print('[!] 没有有效站位，跳过静息阈值分析。')
        else:
            print('')
            print('-- 静息噪声 / 阈值（%d 站合并）--' % len(ok))
            # mode_rest 接收的是 (idx, t, pts) 三元组，别在这里剥成二元组
            merged = (fr for r in ok
                      for fr in window_frames(
                          r['bin'], proc,
                          r.get('f0') if args.use_window else None,
                          r.get('f1') if args.use_window else None))
            rest = rc.mode_rest(merged, args.band)

    elapsed = time.time() - started
    report_md = build_report(args, recs, rest, args.outdir, started, elapsed)
    rp_md = os.path.join(args.outdir, 'station-report.md')
    with open(rp_md, 'w', encoding='utf-8') as f:
        f.write(report_md)
    rp_json = os.path.join(args.outdir, 'station-report.json')
    with open(rp_json, 'w', encoding='utf-8') as f:
        json.dump({'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
                   'args': {'source': args.source, 'stations': stations,
                            'duration': args.duration, 'band': list(args.band),
                            'calib': args.calib},
                   'stations': [{k: v for k, v in r.items() if k != 'labels'}
                                for r in recs],
                   'rest': rest, 'elapsed_s': round(elapsed, 1)},
                  f, ensure_ascii=False, indent=2)

    print('')
    print('报告: %s' % rp_md)
    print('机器可读: %s' % rp_json)
    bad = [r for r in recs if not r.get('valid')]
    if bad:
        print('[!] 有 %d 个站位作废（%s）：建议重采这些站位。'
              % (len(bad), '、'.join('%.2fm' % r['dist'] for r in bad)))
        return 2
    print('[ok] 全部 %d 个站位采集/分析完成。' % len(recs))
    return 0


if __name__ == '__main__':
    sys.exit(main())
