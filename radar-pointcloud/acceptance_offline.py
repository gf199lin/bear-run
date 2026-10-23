# -*- coding: utf-8 -*-
"""离线验收证据生成器 —— 不接雷达也能产出**可复核**的 AC 证据。

为什么要它（AGENTS.md 证据纪律 / docs/acceptance-test-table.md）：
- 验收表 AC-2 / AC-9 的部分判据可以在**录制文件**上离线复算，不必等设备；
- 但离线复算**不等于**真机验收。AC-2 要求「受试静立 60 s + 录像」，
  AC-9 要求「实时运行日志 vs 回放」对照 —— 本工具只产出**下限证据**，
  每份输出都会把这条边界写在抬头，避免被当成通过结论引用。

子命令（全部只读，不改任何仓库文件）：

  scan         扫目录下 .bin → 帧数 / 时长 / 是否有真值 / 动作数
               用途：判断「现有数据里最长连续静息有多长」，即 AC-2 的前置够不够。

  rest         离线跑产线识别器 → 按 N 秒不重叠窗统计 LANE_* 次数（次/分钟）。
               用途：AC-2 的参考下限；**不是** AC-2 的判定。

  determinism  同一 .bin 重复离线跑 N 遍 → 事件序列逐条比对。
               用途：回放路径的确定性（AC-9 的子证据；AC-9 本身还需实时日志）。

用法示例（PowerShell，工作目录 = 仓库根）：

  python radar-pointcloud\\acceptance_offline.py scan --dir .deepworks\\tmp\\seg-lr2\\1
  python radar-pointcloud\\acceptance_offline.py rest --bin .deepworks\\tmp\\seg-lr2\\1\\d0.70\\seg_00.70.bin --window 60
  python radar-pointcloud\\acceptance_offline.py determinism --bin .deepworks\\tmp\\seg\\seg_01.25.bin --runs 3

退出码：0 = 正常产出；1 = 参数/文件问题；2 = 解析到 0 帧（板型判错）。
"""
import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors='replace')
    except Exception:
        pass

import radar_source as rs                                        # noqa: E402
import radar_actions as ra                                       # noqa: E402
from radar_dsp import PointCloudProcessor                        # noqa: E402

# 与 .deepworks/tmp/eval_segments.py、regress_drift.py 保持同一口径
CFAR = 2.6
SNR = 7.0
MIN_RANGE = 0.681
LANE_NAMES = ('LANE_LEFT', 'LANE_RIGHT')
ALL_NAMES = ('LANE_LEFT', 'LANE_RIGHT', 'TARGET_LOST', 'TARGET_BACK',
             'BAND_READY', 'CALIBRATED', 'JUMP', 'DUCK')


# ------------------------------------------------------------------ 基础工具
def hr(ch='=', n=86):
    return ch * n


def md_table(headers, rows):
    out = ['| ' + ' | '.join(headers) + ' |',
           '|' + '|'.join(['---'] * len(headers)) + '|']
    for r in rows:
        out.append('| ' + ' | '.join(str(c) for c in r) + ' |')
    return '\n'.join(out)


def resolve_board(path, board):
    """显式板型优先；否则嗅探。返回 (board, 是否来自嗅探)。"""
    if board:
        return board, False
    b = rs.sniff_board_from_file(path)
    return (b or '1t2r'), True


def count_frames(path, board):
    """只做协议解析 + 组帧，不跑 DSP —— 用于快速盘点时长。"""
    parser, asm = rs.make_protocol(board)
    n = 0
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            for rf in parser.feed(chunk):
                if asm.push(rf) is not None:
                    n += 1
    info = {'frames': n, 'packets': getattr(parser, 'packets', 0),
            'skipped': getattr(parser, 'skipped_bytes', 0),
            'bad': getattr(parser, 'bad_packets', 0)}
    return info


def analyze_once(path, board, crit, cfar, snr, min_range, top_k, min_pts,
                 band=None):
    """跑一遍产线离线分析，返回 (全部事件列表, info, recognizer)。

    每帧事件顺序保留（同一帧内按识别器 push 的产出顺序）。
    band=None → 识别器默认固定带；'auto' → 产线自动选带（与演示启动器同路径）。
    """
    kw = dict(crit=crit, top_k=top_k, min_pts=min_pts)
    if band:
        kw['band'] = band
    rec = ra.ActionRecognizer(**kw)
    proc = PointCloudProcessor(cfar_scale=cfar, min_snr=snr,
                               min_range=min_range)
    records, info = ra.analyze_file(path, processor=proc, recognizer=rec,
                                    board=board)
    evs = []
    for r in records:
        for e in r.get('events', []):
            if e.get('name') in ALL_NAMES:
                evs.append({'t': round(float(e['t']), 4),
                            'frame': r.get('frame'),
                            'name': e['name'],
                            't_round2': round(float(e['t']), 2)})
    return evs, info, rec


# ------------------------------------------------------------------- scan
def cmd_scan(args):
    targets = []
    for d in args.dir:
        if os.path.isdir(d):
            for root, _dirs, files in os.walk(d):
                for fn in files:
                    if fn.lower().endswith('.bin'):
                        targets.append(os.path.join(root, fn))
        elif os.path.isfile(d) and d.lower().endswith('.bin'):
            targets.append(d)
        else:
            print('[warn] 跳过不存在的路径: %s' % d, file=sys.stderr)

    # 去重 + 稳定排序
    targets = sorted(set(os.path.abspath(p) for p in targets))
    if not targets:
        print('[error] 没找到 .bin 文件', file=sys.stderr)
        return 1

    rows = []
    for p in targets:
        b, sniffed = resolve_board(p, args.board)
        try:
            info = count_frames(p, b)
        except Exception as e:                                   # noqa: BLE001
            rows.append([os.path.basename(p), 'ERR', '-', '-', str(e)[:40]])
            continue
        if info['frames'] == 0 and sniffed:
            # 嗅探可能把 1T2R 裸帧判成 2T4R；重试另一块板，避免把「判错板型」
            # 当成「空文件」（radar_actions.analyze_file 文档里记过这个坑）
            alt = '2t4r' if b == '1t2r' else '1t2r'
            try:
                info2 = count_frames(p, alt)
                if info2['frames'] > 0:
                    b = alt
                    info = info2
            except Exception:                                    # noqa: BLE001
                pass
        dur = info['frames'] * rs.frame_period(b)
        lab = os.path.splitext(p)[0] + '.labels.jsonl'
        if os.path.exists(lab):
            try:
                n_act = sum(1 for _ in io.open(lab, encoding='utf-8'))
                labs = '%d 行' % n_act
            except Exception:                                    # noqa: BLE001
                labs = '有(读失败)'
        else:
            labs = '无'
        rel = os.path.relpath(p, ROOT)
        rows.append([rel, b, info['frames'], '%.1f' % dur, labs])

    out = []
    out.append('# 录制资产盘点（离线，不接设备）')
    out.append('')
    out.append('> 由 `radar-pointcloud/acceptance_offline.py scan` 生成。')
    out.append('> 帧数与时长来自协议解析计数（**不跑 DSP**），板型优先取 `--board`，'
               '否则嗅探并在 0 帧时回退另一板型。')
    out.append('')
    out.append(md_table(['文件', '板型', '帧数', '时长 s', '真值'], rows))

    longest = max(
        (r for r in rows if isinstance(r[2], int) and r[2] > 0),
        key=lambda r: r[3] if isinstance(r[3], str) else 0,
        default=None)
    if longest:
        out.append('')
        out.append('**最长文件**：`%s`（%s 帧 / %s s）'
                   % (longest[0], longest[2], longest[3]))
        try:
            dur = float(longest[3])
        except Exception:                                        # noqa: BLE001
            dur = 0.0
        out.append('')
        if dur < 60:
            out.append('⚠ **现有最长录制不足 60 s** —— AC-2 要求「连续静立 60 s」，'
                       '不满足即无法用现有数据判定 AC-2。')
        else:
            out.append('提示：文件够长不代表**全程静息**；AC-2 还要求录像证明'
                       '受试未做动作。用 `rest` 子命令看分窗事件分布。')

    text = '\n'.join(out)
    emit(text, args.out)
    return 0


# ------------------------------------------------------------------- rest
def load_action_spans(bin_path, labels_path):
    """读真值动作窗，返回 (spans, n_actions, labels_path)。

    labels.jsonl 首行是 `_meta`，其余每条含 action / t_start / t_end。
    没有真值文件时返回 ([], 0, None) —— 调用方须把“全片统计”当**下界参考**，
    因为真实动作会被算进事件数。
    """
    if labels_path is None:
        cand = os.path.splitext(bin_path)[0] + '.labels.jsonl'
        if os.path.exists(cand):
            labels_path = cand
    if not labels_path or not os.path.exists(labels_path):
        return [], 0, None
    spans = []
    with io.open(labels_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:                                    # noqa: BLE001
                continue
            if '_meta' in o:
                continue
            if str(o.get('action', '')).upper() in ('LEFT', 'RIGHT'):
                try:
                    spans.append((float(o['t_start']), float(o['t_end'])))
                except Exception:                                # noqa: BLE001
                    continue
    return spans, len(spans), labels_path


def subtract_spans(lo, hi, spans):
    """从 [lo,hi] 里挖掉动作窗，返回剩余静息区间列表（已合并、已裁掉空段）。"""
    if not spans:
        return [(lo, hi)]
    merged = []
    for a, b in sorted(spans):
        a, b = max(lo, a), min(hi, b)
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    out = []
    cur = lo
    for a, b in merged:
        if a > cur:
            out.append((cur, a))
        cur = max(cur, b)
    if cur < hi:
        out.append((cur, hi))
    return out


def cmd_rest(args):
    if not os.path.exists(args.bin):
        print('[error] 找不到文件: %s' % args.bin, file=sys.stderr)
        return 1
    board, sniffed = resolve_board(args.bin, args.board)
    evs, info, rec = analyze_once(args.bin, board, args.crit, args.cfar,
                                  args.snr, args.min_range, args.top_k,
                                  args.min_pts,
                                  band='auto' if args.auto_band else None)
    if info['frames'] == 0:
        print('[error] 解析到 0 帧（板型=%s）。若这是 1T2R 裸帧录制，'
              '请显式加 --board 1t2r。' % board, file=sys.stderr)
        return 2

    span = info['frames'] * rs.frame_period(board)
    lane = [e for e in evs if e['name'] in LANE_NAMES]
    win = float(args.window)

    # 真值动作窗（含回程缓冲）。回程是人走回中位，会被判据当成反向动作
    # （docs/handoff-lr2-1022 §6：20 个动作里 16 个有这种尾巴），
    # 所以默认把动作窗后再延长 --post 秒一并排除；否则“回程”会被算成误报。
    spans, n_act, lab_path = load_action_spans(args.bin, args.labels)
    if args.post > 0:
        spans = [(a, b + args.post) for a, b in spans]
    rest_spans = subtract_spans(0.0, span, spans)
    rest_dur = sum(b - a for a, b in rest_spans)

    def in_rest(e):
        return any(a <= e['t'] < b for a, b in rest_spans)

    # 只有落在静息区间内的事件才是“误报候选”
    lane_rest = [e for e in lane if in_rest(e)]
    # 分窗：以静息区间为单位（而不是从 0 起等分），更贴近 AC-2 的“连续静立”
    rows = []
    for a, b in rest_spans:
        dur = b - a
        if dur < args.min_span:
            continue
        bs = [e for e in lane_rest if a <= e['t'] < b]
        left = sum(1 for e in bs if e['name'] == 'LANE_LEFT')
        right = sum(1 for e in bs if e['name'] == 'LANE_RIGHT')
        rows.append(['%.1f–%.1f' % (a, b), '%.1f' % dur, len(bs), left,
                     right, '%.2f' % (len(bs) / dur * 60.0)])

    # 60 s 滑窗：找“任意连续 60 s 内最差 / 最好”的误报率（AC-2 的阈值口径）
    slide = []
    if rest_dur > win:
        t0 = 0.0
        while t0 + win <= span:
            bs = [e for e in lane_rest if t0 <= e['t'] < t0 + win]
            slide.append((t0, len(bs) / win * 60.0))
            t0 += 1.0

    out = []
    out.append('# 静息误报·离线统计（AC-2 参考，**不是** AC-2 判定）')
    out.append('')
    out.append('> 由 `radar-pointcloud/acceptance_offline.py rest` 生成。')
    out.append('> **边界**：AC-2 要求「受试静立 **连续 60 s** + **录像**确认未做动作」。')
    out.append('> 本表只在录制里**按真值挖掉动作窗**后统计，属**分段静息**证据；')
    out.append('> 分段静息率 **不能** 直接当 AC-2 的判定，但可以作为“是否接近阈值”的判断依据。')
    out.append('')
    out.append('| 项 | 值 |')
    out.append('|---|---|')
    out.append('| 文件 | `%s` |' % os.path.relpath(args.bin, ROOT))
    out.append('| 板型 | %s%s |' % (board, '（嗅探）' if sniffed else '（显式）'))
    out.append('| 帧数 / 时长 | %d 帧 / %.1f s |' % (info['frames'], span))
    out.append('| 判据 / 参数 | crit=%s top_k=%d min_pts=%d cfar=%.2f '
               'min_snr=%.1f min_range=%.3f |'
               % (args.crit, args.top_k, args.min_pts, args.cfar, args.snr,
                  args.min_range))
    band_src = ('自动选带 → j=%s / %.2f~%.2f m（第 %s 帧锁定）'
                % (getattr(rec, 'band_gate', None),
                   (rec.band or (0, 0))[0], (rec.band or (0, 0))[1],
                   getattr(rec, 'band_locked_frame', None))
                if args.auto_band else
                ('固定带 %.2f~%.2f m（识别器默认，`--band` 未开）'
                 % ((rec.band or (0, 0))[0], (rec.band or (0, 0))[1])))
    out.append('| 距离带 | %s |' % band_src)
    out.append('| 真值动作窗 | %s |'
               % (('%d 个（`%s`）' % (n_act, os.path.relpath(lab_path, ROOT)))
                  if n_act else '**无**（下界参考：真实动作会被算进事件）'))
    out.append('| 排除缓冲 | 动作结束后 +%.1f s（覆盖回程） |' % args.post)
    out.append('| 全片 LANE_* | %d |' % len(lane))
    out.append('| 静息区间内 LANE_* | **%d** |' % len(lane_rest))
    out.append('| 静息总时长 | %.1f s（%d 段） |'
               % (rest_dur, len([1 for a, b in rest_spans
                                 if b - a >= args.min_span])))
    out.append('| 静息分钟率 | **%.2f 次/分钟** |'
               % ((len(lane_rest) / rest_dur * 60.0) if rest_dur > 0 else 0.0))
    out.append('')
    out.append('## 逐段（≥ %.0f s 的静息段）' % args.min_span)
    out.append('')
    if rows:
        out.append(md_table(['区间 (s)', '时长 s', '事件', 'LEFT', 'RIGHT',
                             '次/分钟'], rows))
    else:
        out.append('（没有满足最短长度的静息段）')

    if slide:
        rates = [r for _t, r in slide]
        worst_t, worst_r = max(slide, key=lambda x: x[1])
        out.append('')
        out.append('## 任意连续 %.0f s 滑窗（1 s 步进）' % win)
        out.append('')
        out.append('- 最差窗：%.0f–%.0f s → **%.2f 次/分钟**'
                   % (worst_t, worst_t + win, worst_r))
        out.append('- 最好窗：**%.2f 次/分钟** ｜ 中位 **%.2f 次/分钟**'
                   % (min(rates), sorted(rates)[len(rates) // 2]))
        over = sum(1 for r in rates if r > 2.0)
        out.append('- 超过 2 次/分钟的窗占比：**%d/%d**'
                   % (over, len(rates)))
        out.append('')
        out.append('⚠ 滑窗跨过动作窗边缘时会把“回程”算进来，看最差值请对照上面的'
                   '逐段表；逐段表才是按真值挖干净的口径。')
    else:
        out.append('')
        out.append('（静息总长不足 %.0f s，无法做滑窗）' % win)

    out.append('')
    if rest_dur < 60:
        out.append('**结论**：本文件静息总长仅 %.1f s（< 60 s），'
                   '**不满足 AC-2 的前置**；上面的率只能当下界参考。' % rest_dur)
    else:
        out.append('**结论**：本文件可用静息 %.1f s（分段）。若有计划按 AC-2 '
                   '口径重采，应在**同一段连续 60 s** 内复现这个量级。' % rest_dur)

    if args.events_out:
        d = os.path.dirname(os.path.abspath(args.events_out))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with io.open(args.events_out, 'w', encoding='utf-8') as f:
            json.dump({'file': os.path.relpath(args.bin, ROOT),
                       'board': board, 'frames': info['frames'],
                       'span_s': round(span, 3),
                       'crit': args.crit,
                       'rest_spans': [[round(a, 2), round(b, 2)]
                                      for a, b in rest_spans],
                       'rest_lane_events': lane_rest,
                       'events': evs},
                      f, ensure_ascii=False, indent=1)
        out.append('')
        out.append('事件序列已导出：`%s`'
                   % os.path.relpath(args.events_out, ROOT))

    emit('\n'.join(out), args.out)
    return 0


# ------------------------------------------------------------ determinism
def cmd_determinism(args):
    if not os.path.exists(args.bin):
        print('[error] 找不到文件: %s' % args.bin, file=sys.stderr)
        return 1
    board, sniffed = resolve_board(args.bin, args.board)

    runs = []
    infos = []
    for i in range(max(2, args.runs)):
        evs, info, _rec = analyze_once(
            args.bin, board, args.crit, args.cfar, args.snr,
            args.min_range, args.top_k, args.min_pts,
            band='auto' if args.auto_band else None)
        runs.append(evs)
        infos.append(info)
    if infos[0]['frames'] == 0:
        print('[error] 解析到 0 帧（板型=%s）' % board, file=sys.stderr)
        return 2

    base = runs[0]
    same_all = all(r == base for r in runs[1:])
    details = []
    for i, r in enumerate(runs[1:], start=2):
        if r == base:
            details.append('第 %d 遍：与第 1 遍**逐条相同**（%d 条）'
                           % (i, len(r)))
            continue
        diff_at = next((k for k in range(min(len(r), len(base)))
                        if r[k] != base[k]), min(len(r), len(base)))
        details.append('第 %d 遍：**不同**（%d 条 vs %d 条，首个差异 at #%d：'
                       '%r vs %r）'
                       % (i, len(r), len(base), diff_at + 1,
                          base[diff_at] if diff_at < len(base) else None,
                          r[diff_at] if diff_at < len(r) else None))

    lane_n = sum(1 for e in base if e['name'] in LANE_NAMES)
    out = []
    out.append('# 回放一致性·离线确定性（AC-9 子证据）')
    out.append('')
    out.append('> 由 `radar-pointcloud/acceptance_offline.py determinism` 生成。')
    out.append('> **边界**：AC-9 的定义是「**实时运行**日志 vs 回放日志」一致率 ≥ 90%。')
    out.append('> 本表只证明「同一录制文件重复离线分析，事件序列是否逐条可复现」，')
    out.append('> 即回放路径的**确定性**；它不是 AC-9 的判定。')
    out.append('')
    out.append('| 项 | 值 |')
    out.append('|---|---|')
    out.append('| 文件 | `%s` |' % os.path.relpath(args.bin, ROOT))
    out.append('| 板型 | %s%s |' % (board, '（嗅探）' if sniffed else '（显式）'))
    out.append('| 帧数 / 时长 | %d 帧 / %.1f s |'
               % (infos[0]['frames'],
                  infos[0]['frames'] * rs.frame_period(board)))
    out.append('| 重复次数 | %d |' % len(runs))
    out.append('| 判据 / 参数 | crit=%s top_k=%d min_pts=%d |'
               % (args.crit, args.top_k, args.min_pts))
    out.append('| 全部事件 / LANE_* | %d / %d |' % (len(base), lane_n))
    out.append('| 结论 | %s |' % ('**逐条一致（确定性成立）**' if same_all
                                  else '**存在差异（不确定性）**'))
    out.append('')
    for d in details:
        out.append('- ' + d)
    out.append('')
    out.append('## 基准事件序列（第 1 遍）')
    out.append('')
    if base:
        out.append(md_table(['#', 't (s)', '帧', '事件'],
                            [[k + 1, '%.2f' % e['t_round2'], e['frame'],
                              e['name']] for k, e in enumerate(base)]))
    else:
        out.append('（无事件）')

    emit('\n'.join(out), args.out)
    return 0


# ---------------------------------------------------------------- gamepath
# 游戏侧阈值：radar-runner/js/config.js L471-475（enterThreshold 0.36 /
# exitThreshold 0.22 / jumpThreshold 0.5 / crouchThreshold 0.5 /
# minConfidence 0.3），与 .deepworks/tmp/serial_to_udp.py 的 EMA 系数 0.55。
GAME_ENTER = 0.36
GAME_EXIT = 0.22
GAME_MIN_CONF = 0.30
GAME_JUMP_TH = 0.5
GAME_DUCK_TH = 0.5
GAME_SMOOTH = 0.55
GAME_HOLD_S = 0.30


def score_events(evs, labels, tol=0.5):
    """窗级召回 + 误报；与 regress_drift.score 同口径（每条事件最多配一个窗）。"""
    used = set()
    hit = {'LEFT': 0, 'RIGHT': 0}
    tot = {'LEFT': 0, 'RIGHT': 0}
    for lb in labels:
        k = str(lb.get('action', '')).upper()
        if k in tot:
            tot[k] += 1
    fp = 0
    for (t, kind) in sorted(evs):
        m = None
        for j, lb in enumerate(labels):
            k = str(lb.get('action', '')).upper()
            if j in used or k != kind:
                continue
            if lb['t_start'] - tol <= t <= lb['t_end'] + tol:
                m = j
                break
        if m is None:
            fp += 1
        else:
            used.add(m)
            hit[kind] += 1
    return hit, tot, fp


def simulate_game_path(records, x_scale=1.0, smooth=GAME_SMOOTH):
    """复现 serial_to_udp.py → InputSource.decoder 的判定链。

    链路（实测代码，不是设计稿）：
      server.py SSE 的 act.features.px（横向**米**）
        → serial_to_udp.py: x = px * x_scale，EMA(smooth) 平滑
        → radar_bridge.py: ws 转发 {x,height,jump,crouch,confidence}
        → InputSource.decoder: |x| ≥ 0.36 切道、≤0.22 回中，conf<0.3 丢帧

    返回 (lane_events, jumps, slides, x_stats)。
    """
    xs = 0.0
    lane = 1
    evs = []
    jumps = 0
    slides = 0
    prev_jump = False
    prev_duck = False
    jump_until = -1.0
    duck_until = -1.0
    x_vals = []
    n_target = 0

    for r in records:
        t = float(r['t'])
        feat = r.get('features') or {}
        target = bool(r.get('target'))
        if target:
            n_target += 1
            px = float(feat.get('px') or 0.0)
            raw = max(-1.0, min(1.0, px * x_scale))
            xs = raw if smooth <= 0 else (smooth * xs + (1 - smooth) * raw)
            x_vals.append(xs)

        for e in r.get('events') or []:
            if e.get('name') == 'JUMP':
                jump_until = t + GAME_HOLD_S
            elif e.get('name') == 'DUCK':
                duck_until = t + GAME_HOLD_S

        conf = 1.0 if target else 0.15
        if conf < GAME_MIN_CONF:
            continue

        new = lane
        if lane == 1:
            if xs <= -GAME_ENTER:
                new = 0
            elif xs >= GAME_ENTER:
                new = 2
        elif lane == 0:
            if xs >= GAME_ENTER:
                new = 2
            elif xs >= -GAME_EXIT:
                new = 1
        else:
            if xs <= -GAME_ENTER:
                new = 0
            elif xs <= GAME_EXIT:
                new = 1

        while lane != new:
            if new > lane:
                evs.append((t, 'RIGHT'))
                lane += 1
            else:
                evs.append((t, 'LEFT'))
                lane -= 1

        j = 1.0 if t < jump_until else 0.0
        if j >= GAME_JUMP_TH and not prev_jump:
            jumps += 1
        prev_jump = j >= GAME_JUMP_TH

        d = 1.0 if t < duck_until else 0.0
        if d >= GAME_DUCK_TH and not prev_duck:
            slides += 1
        prev_duck = d >= GAME_DUCK_TH

    stats = {}
    if x_vals:
        s = sorted(x_vals)
        stats = {'n': len(s), 'min': s[0], 'p05': s[int(0.05 * (len(s) - 1))],
                 'p50': s[len(s) // 2], 'p95': s[int(0.95 * (len(s) - 1))],
                 'max': s[-1]}
    return evs, jumps, slides, stats, n_target


def cmd_gamepath(args):
    if not os.path.exists(args.bin):
        print('[error] 找不到文件: %s' % args.bin, file=sys.stderr)
        return 1
    board, sniffed = resolve_board(args.bin, args.board)

    kw = dict(crit=args.crit, top_k=args.top_k, min_pts=args.min_pts)
    if args.auto_band:
        kw['band'] = 'auto'
    rec = ra.ActionRecognizer(**kw)
    proc = PointCloudProcessor(cfar_scale=args.cfar, min_snr=args.snr,
                               min_range=args.min_range)
    records, info = ra.analyze_file(args.bin, processor=proc, recognizer=rec,
                                    board=board)
    if info['frames'] == 0:
        print('[error] 解析到 0 帧（板型=%s）' % board, file=sys.stderr)
        return 2

    span = info['frames'] * rs.frame_period(board)
    spans, n_act, lab_path = load_action_spans(args.bin, args.labels)
    labels = []
    if lab_path:
        with io.open(lab_path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:                                # noqa: BLE001
                    continue
                if '_meta' in o:
                    continue
                if str(o.get('action', '')).upper() in ('LEFT', 'RIGHT'):
                    labels.append(o)

    # ① 雷达侧（产物口径：LANE_* 事件）
    radar_evs = []
    for r in records:
        for e in r.get('events') or []:
            if e.get('name') == 'LANE_LEFT':
                radar_evs.append((float(e['t']), 'LEFT'))
            elif e.get('name') == 'LANE_RIGHT':
                radar_evs.append((float(e['t']), 'RIGHT'))

    # ② 游戏侧（实际画面口径）
    game_evs, jumps, slides, x_stats, n_target = simulate_game_path(
        records, x_scale=args.x_scale, smooth=args.smooth)

    out = []
    out.append('# 两条识别路径的对照（雷达事件流 vs 游戏画面）')
    out.append('')
    out.append('> 由 `radar-pointcloud/acceptance_offline.py gamepath` 生成。')
    out.append('> **这条链是从代码读出来的，不是设计稿**：')
    out.append('> `server.py` SSE 的 `act.features.px`（横向米）')
    out.append('> → `.deepworks/tmp/serial_to_udp.py`（`x = px*scale`，EMA %.2f）'
               % args.smooth)
    out.append('> → `radar-runner/tools/radar_bridge.py`（ws 转发）')
    out.append('> → `radar-runner/js/InputSource.js` L226-241（|x| ≥ %.2f 切道、'
               '≤ %.2f 回中、conf < %.1f 丢帧）。'
               % (GAME_ENTER, GAME_EXIT, GAME_MIN_CONF))
    out.append('')
    out.append('| 项 | 值 |')
    out.append('|---|---|')
    out.append('| 文件 | `%s` |' % os.path.relpath(args.bin, ROOT))
    out.append('| 板型 | %s%s |' % (board, '（嗅探）' if sniffed else '（显式）'))
    out.append('| 帧数 / 时长 | %d 帧 / %.1f s |' % (info['frames'], span))
    out.append('| 真值动作 | %s |'
               % (('%d 个（`%s`）' % (n_act, os.path.relpath(lab_path, ROOT)))
                  if n_act else '**无**（只能看事件数，不能判分）'))
    out.append('| 判别参数 | crit=%s top_k=%d min_pts=%d 带=%s |'
               % (args.crit, args.top_k, args.min_pts,
                  'auto' if args.auto_band else '固定'))
    out.append('| x 缩放 | %.3f（px 米 → 归一化；默认 1.0 即 0.36 m 门槛） |'
               % args.x_scale)
    out.append('')

    if labels:
        h1, tot, fp1 = score_events(radar_evs, labels)
        h2, _tot2, fp2 = score_events(game_evs, labels)
        out.append('## 判分对照（同一批真值、同一 tol=0.5 s）')
        out.append('')
        out.append(md_table(
            ['路径', 'LEFT', 'RIGHT', '合计', '误报'],
            [['雷达事件流（`ActionRecognizer`）',
              '%d/%d' % (h1['LEFT'], tot['LEFT']),
              '%d/%d' % (h1['RIGHT'], tot['RIGHT']),
              '%d/%d' % (h1['LEFT'] + h1['RIGHT'],
                         tot['LEFT'] + tot['RIGHT']), fp1],
             ['游戏画面（px 绝对值 + EMA）',
              '%d/%d' % (h2['LEFT'], tot['LEFT']),
              '%d/%d' % (h2['RIGHT'], tot['RIGHT']),
              '%d/%d' % (h2['LEFT'] + h2['RIGHT'],
                         tot['LEFT'] + tot['RIGHT']), fp2]]))
        out.append('')
    else:
        out.append('## 判分对照')
        out.append('')
        out.append('（无真值，跳过判分；下面只报事件数）')
        out.append('')

    out.append('## 事件数')
    out.append('')
    out.append(md_table(['路径', 'LEFT', 'RIGHT', 'JUMP', 'SLIDE'],
                        [['雷达事件流', 
                          sum(1 for _t, k in radar_evs if k == 'LEFT'),
                          sum(1 for _t, k in radar_evs if k == 'RIGHT'),
                          sum(1 for r in records for e in (r.get('events') or [])
                              if e.get('name') == 'JUMP'),
                          sum(1 for r in records for e in (r.get('events') or [])
                              if e.get('name') == 'DUCK')],
                         ['游戏画面', 
                          sum(1 for _t, k in game_evs if k == 'LEFT'),
                          sum(1 for _t, k in game_evs if k == 'RIGHT'),
                          jumps, slides]]))
    out.append('')
    out.append('**JUMP/SLIDE 列的含义**：游戏侧数值 = 玩家实际会看到的跳跃/滑铲次数。'
               '游戏会消费 `serial_to_udp.py` 从 SSE `events` 转发来的 JUMP/DUCK，'
               '所以这两个候选规则**会真影响画面**。')
    out.append('')

    out.append('## 游戏侧 x 的分布（决定 0.36 门槛能不能跨过）')
    out.append('')
    if x_stats:
        out.append(md_table(
            ['min', 'p05', 'p50', 'p95', 'max', 'target 帧'],
            [['%.3f' % x_stats['min'], '%.3f' % x_stats['p05'],
              '%.3f' % x_stats['p50'], '%.3f' % x_stats['p95'],
              '%.3f' % x_stats['max'], x_stats['n']]]))
        out.append('')
        if x_stats['min'] > -GAME_ENTER:
            out.append('⚠ **全片 x 从未低于 −%.2f** ⇒ 按游戏侧判据，'
                       '**LEFT 在数学上不可能触发**。' % GAME_ENTER)
        if x_stats['max'] < GAME_ENTER:
            out.append('⚠ **全片 x 从未达到 +%.2f** ⇒ 游戏侧 **RIGHT 也不可能触发**。'
                       % GAME_ENTER)
        if x_stats['min'] <= -GAME_ENTER or x_stats['max'] >= GAME_ENTER:
            over = [v for v in (x_stats['min'], x_stats['max'])
                    if abs(v) >= GAME_ENTER]
            out.append('x 越界情况：min=%.3f / max=%.3f（门槛 ±%.2f）'
                       % (x_stats['min'], x_stats['max'], GAME_ENTER))
    else:
        out.append('（无 target 帧，x 恒为 0）')

    out.append('')
    out.append('## 边界')
    out.append('')
    out.append('1. 这是**离线重放**：真实链路还有网络/浏览器定时抖动，未计入。')
    out.append('2. `px` 取的是识别器 `features.px`，其口径由 `--top-k/--min-pts` '
               '决定；现场若用不同参数，x 分布会变。')
    out.append('3. 游戏侧还有一层「雷达校准向导」可能改写 `scale_x/offset_x/invert`，'
               '本表按 `x_scale=%.2f`、无偏置计算。' % args.x_scale)

    emit('\n'.join(out), args.out)
    return 0


# -------------------------------------------------------------------- 输出
def emit(text, out_path):
    if out_path:
        d = os.path.dirname(os.path.abspath(out_path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with io.open(out_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text + '\n')
        print('[ok] 已写入 %s' % out_path)
    else:
        print(text)


def build_parser():
    ap = argparse.ArgumentParser(
        description='离线验收证据生成器（不接雷达；只读）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('scan', help='盘点目录下 .bin 的帧数/时长/真值')
    p.add_argument('--dir', action='append', required=True,
                   help='目录或 .bin 路径，可重复给')
    p.add_argument('--board', choices=['1t2r', '2t4r'], default=None)
    p.add_argument('--out', default=None, help='Markdown 输出路径')
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser('rest', help='离线分窗统计 LANE_* 频率（AC-2 参考）')
    p.add_argument('--bin', required=True)
    p.add_argument('--window', type=float, default=60.0, help='窗长秒（默认 60）')
    p.add_argument('--labels', default=None,
                   help='真值 labels.jsonl；默认找同名文件，找不到则全片统计')
    p.add_argument('--post', type=float, default=6.0,
                   help='动作窗后再排除的秒数（默认 6，覆盖走回中位的回程）')
    p.add_argument('--min-span', type=float, default=10.0,
                   help='逐段表里只列 ≥ 该秒数的静息段（默认 10）')
    p.add_argument('--auto-band', action='store_true',
                   help='用产线自动选带（与演示启动器同路径）')
    p.add_argument('--board', choices=['1t2r', '2t4r'], default=None)
    p.add_argument('--crit', default=ra.CRIT_DEFAULT,
                   choices=['delta', 'drift', 'xbase'])
    p.add_argument('--top-k', type=int, default=0)
    p.add_argument('--min-pts', type=int, default=2)
    p.add_argument('--cfar', type=float, default=CFAR)
    p.add_argument('--snr', type=float, default=SNR)
    p.add_argument('--min-range', type=float, default=MIN_RANGE)
    p.add_argument('--events-out', default=None, help='事件序列 JSON 导出')
    p.add_argument('--out', default=None, help='Markdown 输出路径')
    p.set_defaults(func=cmd_rest)

    p = sub.add_parser('determinism', help='重复离线跑，比对事件序列')
    p.add_argument('--bin', required=True)
    p.add_argument('--runs', type=int, default=3)
    p.add_argument('--auto-band', action='store_true',
                   help='用产线自动选带（默认固定带）')
    p.add_argument('--board', choices=['1t2r', '2t4r'], default=None)
    p.add_argument('--crit', default=ra.CRIT_DEFAULT,
                   choices=['delta', 'drift', 'xbase'])
    p.add_argument('--top-k', type=int, default=0)
    p.add_argument('--min-pts', type=int, default=2)
    p.add_argument('--cfar', type=float, default=CFAR)
    p.add_argument('--snr', type=float, default=SNR)
    p.add_argument('--min-range', type=float, default=MIN_RANGE)
    p.add_argument('--out', default=None)
    p.set_defaults(func=cmd_determinism)

    p = sub.add_parser('gamepath',
                       help='对照：雷达事件流 vs 游戏画面实际判定链')
    p.add_argument('--bin', required=True)
    p.add_argument('--labels', default=None)
    p.add_argument('--board', choices=['1t2r', '2t4r'], default=None)
    p.add_argument('--crit', default=ra.CRIT_DEFAULT,
                   choices=['delta', 'drift', 'xbase'])
    p.add_argument('--top-k', type=int, default=0)
    p.add_argument('--min-pts', type=int, default=2)
    p.add_argument('--auto-band', action='store_true')
    p.add_argument('--x-scale', type=float, default=1.0,
                   help='px(米) → 归一化 x 的缩放（默认 1.0）')
    p.add_argument('--smooth', type=float, default=GAME_SMOOTH,
                   help='EMA 系数（默认 0.55，与 serial_to_udp.py 一致）')
    p.add_argument('--cfar', type=float, default=CFAR)
    p.add_argument('--snr', type=float, default=SNR)
    p.add_argument('--min-range', type=float, default=MIN_RANGE)
    p.add_argument('--out', default=None)
    p.set_defaults(func=cmd_gamepath)

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
