# -*- coding: utf-8 -*-
"""
试验采集的真值标注工具（离线，不开串口）。

为什么需要它：AGENTS.md 证据纪律要求「每项测试都要有真值标注」。
`radar_actions.py` 能给出**预测**事件，但没有真值就没法算命中/漏报/误报，
更不能拿去做验收结论。本工具负责产出真值，并直接给出预测 vs 真值的对照。

两种用法
1) 实时回放标注（推荐，人在旁边看画面/视频，按键打标）
       python label_tool.py --file wave1.bin --out labels.jsonl
   按时间轴等速回放录制的点云分析结果，边看边按：
       1 = 左移   2 = 右移   3 = 蹲   4 = 跳
       0/空格 = 结束当前标注      u = 撤销上一条
       p = 暂停/继续             s = 立即保存     q = 保存并退出
   同一条动作再按一次同一个键也能结束标注。

2) 与预测对照（标完以后）
       python label_tool.py --file wave1.bin --labels labels.jsonl --compare
   把真值区间与 `radar_actions` 的预测事件逐条匹配，输出每个动作的
   命中 / 漏报 / 误报，以及精确率与召回率，写入 <labels>.report.txt。

时间轴一律用 **帧号 × 0.1295 s**（与离线分析一致），不用墙钟。
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import radar_actions as ra

ACTIONS = {'1': 'LEFT', '2': 'RIGHT', '3': 'DUCK', '4': 'JUMP'}
ACTION_TEXT = {'LEFT': '左移', 'RIGHT': '右移', 'DUCK': '蹲(候选)', 'JUMP': '跳(候选)'}
# 预测事件名 → 真值动作名
EV_TO_ACT = {'LANE_LEFT': 'LEFT', 'LANE_RIGHT': 'RIGHT', 'JUMP': 'JUMP', 'DUCK': 'DUCK'}


def _stdout_utf8():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


def load_labels(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if '_meta' in obj or 'action' not in obj:
                continue          # 跳过 _meta 头行/缺字段行，避免污染统计
            out.append(obj)
    return out


def load_meta(path):
    """读真值文件头行的 `_meta`（板型 / 距离带 / 时间轴口径）→ dict（没有就 {}）。

    对比步骤必须用它：同一个 `.bin`，板型判错就是 0 帧、带判错就是 0 命中，
    而这两种「0」看起来都像算法失效。
    """
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if '_meta' in obj:
                return obj['_meta'] or {}
            if 'action' in obj:
                return {}
    return {}


def save_labels(path, labels, meta=None):
    with open(path, 'w', encoding='utf-8') as f:
        if meta:
            f.write(json.dumps({'_meta': meta}, ensure_ascii=False) + '\n')
        for lb in labels:
            f.write(json.dumps(lb, ensure_ascii=False) + '\n')


def _keyboard():
    """返回 (poll, 说明)；Windows 用 msvcrt 直接读键，其它平台退回回车式输入。"""
    try:
        import msvcrt

        def poll():
            if not msvcrt.kbhit():
                return None
            ch = msvcrt.getch()
            try:
                return ch.decode('utf-8', 'ignore').lower()
            except Exception:
                return None
        return poll, '热键模式（msvcrt）'
    except Exception:
        return None, '回车式输入模式（每敲一个键需回车）'


def play(records, labels, out_path, meta, speed=1.0, start_frame=0):
    poll, mode = _keyboard()
    print('键盘: %s' % mode)
    print('1=左移  2=右移  3=蹲  4=跳   0/空格=结束标注   u=撤销   p=暂停   s=保存   q=保存退出')
    print('-' * 78)
    open_lb = None
    last_t = None
    paused = False
    n = 0
    try:
        for r in records:
            if r['frame'] < start_frame:
                continue
            if speed > 0:
                dt = 0.0 if last_t is None else (r['t'] - last_t)
                last_t = r['t']
                time.sleep(max(0.0, min(dt / speed, 0.5)))
            ft = r['features'] or {}
            ev = ' '.join(e['name'] for e in r['events'])
            line = ('t=%6.2fs f=%4d | 带内 n=%s px=%s v=%s | 预测 %-4s | %s'
                    % (r['t'], r['frame'], ft.get('n', '—'),
                       ('%+.2f' % ft['px']) if 'px' in ft else '  —  ',
                       ('%+.2f' % ft['v_lat']) if 'v_lat' in ft else '  —  ',
                       r['lane'] or '—', ev or (('标注中: ' + ACTION_TEXT[open_lb])
                                                if open_lb else '')))
            sys.stdout.write('\r' + line[:118].ljust(118))
            sys.stdout.flush()
            n += 1

            while True:                       # 尽量把本帧时间片内按下的键全处理掉
                k = _blocking_getch(poll, paused)
                if k is None:
                    break
                if k == 'q':
                    if open_lb:
                        labels.append(finish_lb(open_lb, r))
                    save_labels(out_path, labels, meta)
                    print('\n已保存 %d 条真值 → %s' % (len(labels), out_path))
                    return labels
                if k == 's':
                    save_labels(out_path, labels, meta)
                    sys.stdout.write('\r[已保存 %d 条]' % len(labels) + ' ' * 80)
                if k == 'p':
                    paused = not paused
                if k == 'u' and labels:
                    gone = labels.pop()
                    sys.stdout.write('\r[撤销 %s %0.2f-%0.2f]' % (
                        ACTION_TEXT.get(gone['action'], gone['action']),
                        gone['t_start'], gone['t_end']) + ' ' * 60)
                if k in ACTIONS:
                    # 同键再按 = 结束；否则先结束旧的再开新的
                    if open_lb and open_lb['action'] == ACTIONS[k]:
                        labels.append(finish_lb(open_lb, r))
                        open_lb = None
                    else:
                        if open_lb:
                            labels.append(finish_lb(open_lb, r))
                        open_lb = {'file': meta['file'], 'action': ACTIONS[k],
                                   't_start': r['t'], 'frame_start': r['frame'],
                                   'v_lat_at_start': ft.get('v_lat'), 'note': ''}
                        sys.stdout.write('\r[开始标注 %s]' % ACTION_TEXT[ACTIONS[k]] + ' ' * 60)
                if k in ('0', ' ', '\r'):
                    if open_lb:
                        labels.append(finish_lb(open_lb, r))
                        open_lb = None
                        sys.stdout.write('\r[标注结束]' + ' ' * 70)
            if paused:
                while paused:
                    k = _blocking_getch(poll, True, timeout=0.2)
                    if k == 'p':
                        paused = False
                    elif k == 'q':
                        save_labels(out_path, labels, meta)
                        print('\n已保存 %d 条真值 → %s' % (len(labels), out_path))
                        return labels
    except KeyboardInterrupt:
        print('\n中断。')
    if open_lb:
        labels.append(finish_lb(open_lb, records[-1]))
    print('\n回放结束：%d 帧，%d 条真值。' % (n, len(labels)))
    save_labels(out_path, labels, meta)
    print('真值已写入: %s' % out_path)
    return labels


def _blocking_getch(poll, paused, timeout=0.0):
    """poll=None 时退回 input()（每键回车）；否则读一个键。"""
    if poll is None:
        if timeout:
            return None
        try:
            s = input().strip().lower()
            return s[:1] if s else ' '
        except EOFError:
            return 'q'
    if timeout:
        end = time.time() + timeout
        while time.time() < end:
            k = poll()
            if k is not None:
                return k
            time.sleep(0.01)
        return None
    return poll()


def finish_lb(open_lb, r):
    lb = dict(open_lb)
    lb['t_end'] = r['t']
    lb['frame_end'] = r['frame']
    lb['duration'] = round(max(0.0, lb['t_end'] - lb['t_start']), 3)
    if lb.get('v_lat_at_start') is None:
        lb.pop('v_lat_at_start', None)
    return lb


# ------------------------------------------------------------------ 预测 vs 真值
def compare(records, labels, tol=0.45):
    """把预测事件与真值区间逐条匹配。

    tol：真值区间两端各放宽多少秒（人的起止时刻不精确）。
    返回 (统计, 明细行)。
    """
    preds = []
    for r in records:
        for e in r['events']:
            act = EV_TO_ACT.get(e['name'])
            if act:
                preds.append({'name': e['name'], 'act': act, 't': e['t'],
                              'gated': bool(e.get('gated')), 'seq': e.get('seq'),
                              'hit': False})
    rows = []
    stats = {}
    for lb in labels:
        act = lb['action']
        s = stats.setdefault(act, {'truth': 0, 'hit': 0, 'pred': 0, 'miss': 0, 'fp': 0})
        s['truth'] += 1
        t0, t1 = lb['t_start'] - tol, lb['t_end'] + tol
        inside = [p for p in preds if p['act'] == act and t0 <= p['t'] <= t1]
        for p in inside:
            p['hit'] = True
        if inside:
            s['hit'] += 1
            s['pred'] += len(inside)
            first = min(p['t'] for p in inside)
            rows.append('%s %6.2f-%6.2f s (%.2f s) → 命中 %d 次，首个 +%.2fs%s'
                        % (ACTION_TEXT.get(act, act), lb['t_start'], lb['t_end'],
                           lb.get('duration', 0.0), len(inside), first - lb['t_start'],
                           '（候选项）' if any(p['gated'] for p in inside) else ''))
        else:
            s['miss'] += 1
            rows.append('%s %6.2f-%6.2f s (%.2f s) → 漏报（真值区间内无同名事件）'
                        % (ACTION_TEXT.get(act, act), lb['t_start'], lb['t_end'],
                           lb.get('duration', 0.0)))
    for p in preds:
        if not p['hit']:
            s = stats.setdefault(p['act'], {'truth': 0, 'hit': 0, 'pred': 0,
                                            'miss': 0, 'fp': 0})
            s['pred'] += 1
            s['fp'] += 1
    return stats, rows, preds


def report(records, labels, labels_path, tol=0.45, max_fp_show=8):
    stats, rows, preds = compare(records, labels, tol=tol)
    fp = [p for p in preds if not p['hit']]
    by_act = {}
    for p in fp:
        by_act.setdefault(p['act'], []).append(p)
    fp_summary = []
    for act, lst in sorted(by_act.items()):
        ts = ', '.join('%.2f' % p['t'] for p in lst[:max_fp_show])
        fp_summary.append('%s 误报 %d 次：t=%s%s'
                          % (ACTION_TEXT.get(act, act), len(lst), ts,
                             ' …(完整清单见报告文件)' if len(lst) > max_fp_show else ''))

    head = ['真值 vs 预测 对照（tol=±%.2fs，时间轴 = 帧号×0.1295s）' % tol,
            '真值文件: %s  共 %d 条' % (labels_path, len(labels)),
            '预测事件: %d 条（其中候选项 %d 条，误报 %d 条）'
            % (len(preds), sum(1 for p in preds if p['gated']), len(fp)),
            '-' * 74]
    table = ['-' * 74,
             '%-10s %6s %6s %6s %6s %8s %8s' % ('动作', '真值', '命中', '漏报',
                                                 '误报', '精确率', '召回率')]
    for act, s in sorted(stats.items()):
        prec = s['hit'] / s['pred'] if s['pred'] else float('nan')
        rec_ = s['hit'] / s['truth'] if s['truth'] else float('nan')
        table.append('%-10s %6d %6d %6d %6d %8s %8s' %
                     (ACTION_TEXT.get(act, act), s['truth'], s['hit'], s['miss'],
                      s['fp'],
                      ('%.2f' % prec) if s['pred'] else '—',
                      ('%.2f' % rec_) if s['truth'] else '—'))
    note = ['',
            '注意：蹲/跳在当前硬件（无俯仰角）下只能给候选项，'
            '本表不能作为跳/蹲的验收依据（见 AGENTS.md 验证门）。']

    console = '\n'.join(head + (rows or ['（无命中/漏报明细）']) + [''] + fp_summary
                        + table + note)
    print(console)
    detail = ['%s t=%.2f s → 误报（无对应真值区间）%s'
              % (ACTION_TEXT.get(p['act'], p['act']), p['t'],
                 '（候选项）' if p['gated'] else '') for p in fp]
    out = os.path.splitext(labels_path)[0] + '.report.txt'
    with open(out, 'w', encoding='utf-8') as f:
        f.write(console + '\n')
        if detail:
            f.write('\n' + '-' * 74 + '\n全部误报明细（%d 条）\n' % len(detail))
            f.write('\n'.join(detail) + '\n')
    print('\n对照报告已写入: %s' % out)
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description='试验采集真值标注 / 预测对照（离线）')
    ap.add_argument('--file', required=True, help='录制的 .bin（server.py --record 产出）')
    ap.add_argument('--labels', default=None, help='真值文件（默认 <file>.labels.jsonl）')
    ap.add_argument('--out', default=None, help='标注输出（默认与 --labels 相同）')
    ap.add_argument('--compare', action='store_true', help='只做预测 vs 真值对照')
    ap.add_argument('--speed', type=float, default=1.0,
                    help='回放速度倍率（1.0=实时，2.0=两倍速，0=不等待直接跑完）')
    ap.add_argument('--start-frame', type=int, default=0)
    ap.add_argument('--band', type=float, nargs=2, default=list(ra.BAND_DEFAULT),
                    help='动作识别距离带，与线上一致才可比')
    ap.add_argument('--limit', type=int, default=None, help='只分析前 N 帧')
    ap.add_argument('--tol', type=float, default=0.45, help='对照时区间放宽秒数')
    args = ap.parse_args(argv)
    _stdout_utf8()

    labels_path = args.labels or (args.file + '.labels.jsonl')
    out_path = args.out or labels_path

    t0 = time.time()
    records, info = ra.analyze_file(args.file, band=tuple(args.band), limit=args.limit,
                                    log=lambda m: print(m))
    print('解析: %d 帧 / %d 包，坏包 %d，丢字节 %d（耗时 %.1fs）'
          % (info['frames'], info['packets'], info['bad'], info['skipped'],
             time.time() - t0))
    print('文件: %s  距离带 %s m' % (info['file'], tuple(args.band)))
    if not records:
        print('没有解析出任何帧，检查文件是否为 server.py --record 的原始字节流。')
        return 2

    if args.compare:
        labels = load_labels(labels_path)
        if not labels:
            print('真值文件不存在或为空: %s' % labels_path)
            return 3
        report(records, labels, labels_path, tol=args.tol)
        return 0

    labels = load_labels(out_path)
    if labels:
        print('已存在 %d 条真值，继续追加（撤销只能撤本次新增的）' % len(labels))
    meta = dict(info)
    meta['band'] = list(args.band)
    meta['note'] = '时间轴 = 帧号×0.1295s；动作名 LEFT/RIGHT/DUCK/JUMP'
    play(records, labels, out_path, meta, speed=args.speed, start_frame=args.start_frame)
    print('再看对照: python label_tool.py --file %s --compare' % args.file)
    return 0


if __name__ == '__main__':
    sys.exit(main())
