# -*- coding: utf-8 -*-
"""分段定距采集（提示音驱动、自动打真值）—— 动作识别准确率的数据来源。

为什么要有这个脚本（AGENTS.md 证据纪律 + `docs/progress-radar-pointcloud.md` §7.9）：
- 之前那条 36 min 的长录制里**站位一直在变**，给不出可解释的真值，所以算不出准确率。
- 手工用 `label_tool.py` 边看回放边按键打标，既费时又会引入人的读数误差。
- 本脚本按固定的提示音协议驱动真人动作，并**用真实帧号**记录每个动作区间的起止，
  采完直接调用 `label_tool.py` 的对照逻辑算出精确率/召回率。

时间基准（关键，别用墙钟）：
  离线分析的时间轴是 **帧号 × 帧周期**（`radar_actions.FRAME_PERIOD`，见其 L241
  `t = asm.frames * FRAME_PERIOD`）。真机实测帧率与 0.1295 s/帧 并不完全相等，
  所以真值必须换算到**帧号**再乘 FRAME_PERIOD，否则一段 90 s 的采集会有秒级漂移，
  对照结果直接作废。本脚本在提示音响起的瞬间抓取 `stats['frames']`，正是为了这个。

提示音协议（每段相同）：
  准备 3 s（站回中间、静止） →「嘟」→ 做动作并保持 2.5 s →「停」→ 静息 3 s
  动作前留 `--trim`（默认 0.3 s，人的反应时间）不计入真值区间。

用法：
  python segment_capture.py --port COM3                     # 真机（人要在设备旁）
  python segment_capture.py --source loopback --reps 1      # 无硬件冒烟测试

无人值守（--auto，人站在雷达前：看不到屏幕、也够不到键盘）：
  默认协议要求按回车开始、段间还要按回车换站位 —— 人一旦站到雷达前就做不到。
  --auto 把这两处交互换成纯声音引导：
    · 开场         : 中文语音播报 + 最后 3 秒数拍「嗒嗒嗒」→「嘟」开始
    · 每次动作前   : 3 声短拍（该回中间了）……「嘟」(1200 Hz 长) = 开始做动作
                     → 动作结束「嘟」(700 Hz 长) = 停、回中间完全静止
    · 换动作类别时 : 先连响 3 声高音(1500 Hz)，再语音播报「做 N 次…」
    · 换站位时     : 语音提示 + 倒计时数拍（--walk-time 秒），不用碰键盘
  语音走 Windows 自带 SAPI（Microsoft Huihui 中文），在后台子进程异步播报，
  不占用采集节奏；机器上没有中文语音包时自动降级为纯提示音。--no-speak 可关。
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import radar_actions as ra            # noqa: E402
import radar_source as rs             # noqa: E402
import label_tool as lt               # noqa: E402

FP = ra.FRAME_PERIOD                  # 帧周期（s/帧）；**运行时按板型覆盖**，见 set_board()
BOARD = '1t2r'                        # 当前生效板型（默认 1T2R → 行为与改动前一致）


def set_board(board):
    """按板型切换全局帧周期 FP（1T2R 0.1295 s / 2T4R 0.520840 s），返回生效板型。

    真值窗口按「帧号 × FP」落盘，离线分析也按同一系数回算时间轴；
    两者必须同板同值 —— 2T4R 若沿用 1T2R 的 0.1295 s，整段真值偏快 4.02 倍。
    """
    global FP, BOARD
    BOARD = board if board in ('1t2r', '2t4r') else rs.DEFAULT_BOARD
    FP = rs.frame_period(BOARD)
    return BOARD


def resolve_board(args, src=None):
    """板型优先级：--board 显式 > 数据源指纹（src.board）> 默认板型。"""
    want = getattr(args, 'board', None)
    if want and want != 'auto':
        return want
    b = getattr(src, 'board', None)
    if b in ('1t2r', '2t4r'):
        return b
    return rs.DEFAULT_BOARD


def preflight_board(args):
    """开工前尽力确定板型：--board 显式 > 串口指纹 > 回放文件嗅探 > 默认。

    任何异常都降级为默认板型 —— 板型识别失败不该拦住采集。
    """
    want = getattr(args, 'board', None)
    if want and want != 'auto':
        return set_board(want)
    if getattr(args, 'source', None) == 'serial':
        try:
            b = rs.probe_board(args.port)
        except Exception:
            b = None
        if b in ('1t2r', '2t4r'):
            return set_board(b)
    if getattr(args, 'source', None) == 'replay' and getattr(args, 'replay_file', None):
        try:
            b = rs.sniff_board_from_file(args.replay_file)
        except Exception:
            b = None
        if b:
            return set_board(b)
    return set_board(rs.DEFAULT_BOARD)

# 每个距离用哪个距离带。距离门索引 j = round(r/0.6813 - 0.5)（radar_dsp: rng=(j+0.5)*0.6813）
#   r=1.00 m → j=1（门中心 1.02 m）
#   r=1.25 m → j=1（同一门！所以 1.0 与 1.25 在「距离带」这一维上是不可分的，
#              分段的意义在于分别看：人站近了会不会更容易被近场泄漏门干扰）
#   r=1.50 m → j=2（门中心 1.70 m）→ 必须换成 (1.35, 2.05)，否则目标落在带外
# 站位 → 该站位人体回波所在的距离门（position_capture.py 实测，devlog §7.25 / §7.26）。
# ⚠ 旧表把 1.0 / 1.25 m 写成 (0.70, 1.35) = **j=1 空门**，那正是「默认带对真实站位
#   完全无效」的根源（1.25 m 上 0/10 命中且主动误报 5 次）。
#   实测：0.70 m → j=1；1.00 / 1.25 / 1.35 m → j=2；1.70 m → j=3。
#   偏移 ≈ 恒定 +0.5 m（来源未定，见 devlog §7.26）⇒ 站得越远，门号越靠后。
#   只保留原有三个键（值按实测更正）；其它距离退回 ra.BAND_DEFAULT 并打印提示。
#   ⚠ 本表只影响本工具的对照步骤与真值 `_meta`；识别本身请用 band='auto'（自动选带）。
DIST_BAND = {
    1.0: (1.36, 2.04),
    1.25: (1.36, 2.04),
    1.5: (1.36, 2.04),
}
ACTION_TEXT = {'LEFT': '向左移动', 'RIGHT': '向右移动', 'DUCK': '下蹲', 'JUMP': '起跳'}
ACTION_KEY = {'left': 'LEFT', 'right': 'RIGHT', 'duck': 'DUCK', 'jump': 'JUMP'}


# 2T4R 距离带：门距只有 0.150 m，DIST_BAND 那三条 (1.36, 2.04) 是按 1T2R 门宽
# 调出来的，照用会把 2T4R 的站位切到很远处。改为以站位为中心 ±0.35 m（≈±2.3 门）。
# ⚠ 提案，待 2T4R 卷尺点检 + 左右移动实采确认；不是实测结论。
BAND_HALF_2T4R = 0.35


def band_for_dist(dist, board=None):
    """按板型返回该站位的距离带（1T2R 沿用原 DIST_BAND → 行为与改动前一致）。"""
    b = board or BOARD
    if b != '2t4r':
        return DIST_BAND.get(dist) or ra.BAND_DEFAULT
    lo = max(rs.range_res(b), dist - BAND_HALF_2T4R)
    return (round(lo, 3), round(dist + BAND_HALF_2T4R, 3))

STATE = {'frames': 0, 'fps': 0.0, 'packets': 0, 'bad': 0, 'skipped': 0,
         'connected': True, 'reconnects': 0, 'read_errors': 0}


def _stdout_utf8():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


NO_BEEP = False          # --no-beep 时置 True
NO_SPEAK = False         # --no-speak 时置 True

# 提示音词典：人站在雷达前只靠耳朵，四种音必须一耳朵能分清
BEEP = {
    'start':  (1200, 250),   # 「现在开始做动作」
    'stop':   (700, 300),    # 「停，回中间」
    'tick':   (900, 90),     # 准备期数拍（还有几秒开始）
    'switch': (1500, 150),   # 换动作类别
}


def beep(freq=None, dur=None, kind='start'):
    """一次蜂鸣。

    用 winsound.Beep（Windows 原生）而不是终端 `\\a`：Windows Terminal 默认把
    bell 变成标签页闪烁、不出声，靠 `\\a` 会整场采集都听不到动作起点。
    """
    if NO_BEEP:
        return
    f0, d0 = BEEP.get(kind, BEEP['start'])
    freq = f0 if freq is None else freq
    dur = d0 if dur is None else dur
    try:
        import winsound
        winsound.Beep(int(freq), int(dur))
        return
    except Exception:
        pass
    try:                                        # 兜底
        sys.stdout.write('\a')
        sys.stdout.flush()
    except Exception:
        pass


def _ps_literal(s):
    """把文本变成**纯 ASCII** 的 PowerShell 字符串表达式（按码点 join）。

    为什么不用中文字面量：本机实测（`.deepworks/tmp/diag_speak.py`）PowerShell 5.1
    经 `-EncodedCommand` 收到含中文字面量的脚本时会解析失败（rc=1，脚本里的中文被
    破坏成缺引号的语法错误）；而 `(-join @([char]19979,[char]36466))` 这种纯 ASCII
    表达式 rc=0 且正确播出「下蹲」。按码点构造后，脚本无论经过哪一层代码页都不会坏。
    """
    return '(-join @(' + ','.join('[char]%d' % ord(c) for c in s) + '))'


def speak(text):
    """中文语音播报（后台异步，不占用采集节奏）。

    本机没装 pywin32，所以起一个后台 PowerShell 子进程调 Windows 自带
    `System.Speech.Synthesis`（本机语音包：Microsoft Huihui - Chinese (Simplified)）。
    文本一律经 `_ps_literal()` 转成纯 ASCII 的码点表达式，再 base64(UTF-16LE) 走
    `-EncodedCommand` —— 避开中文在任何一层的编码损坏。
    任何一步失败都静默降级为纯提示音 —— 采集节奏不能因为播报失败而中断。
    返回 True 表示已投递（不代表播完）。
    """
    if NO_SPEAK or not text:
        return False
    try:
        script = ("Add-Type -AssemblyName System.Speech;"
                  "$v=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                  "$v.Rate=1;"
                  "$v.Speak(%s)" % _ps_literal(text))
        enc = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        kw = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        if hasattr(subprocess, 'CREATE_NO_WINDOW'):
            kw['creationflags'] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(['powershell', '-NoProfile', '-NonInteractive',
                          '-EncodedCommand', enc], **kw)
        return True
    except Exception:
        return False


def cue(secs, text, do_beep=False, debug=False, countdown=False, kind='start',
        tick=False):
    """给出提示并等待 secs 秒，返回 (窗口开始帧号, 窗口结束帧号)。

    动作的起点在**提示音响起的那一刻**，也就是本窗口的**开头**；
    所以调用方要拿返回值里的第一个数（不是第二个）当动作起点。
    countdown=True 时在最后 3 秒显示 3-2-1，让人知道「嘟」还有多久；
    tick=True（--auto 用）时这 3 秒**同时**各响一声短拍 —— 人站在雷达前看不到
    屏幕，只有听到「嗒 嗒 嗒 —— 嘟」才知道动作还有多久开始。
    """
    if do_beep:
        beep(kind=kind)
    print('    %s  [%.1fs]' % (text, secs), flush=True)
    f_begin = STATE['frames']
    t0 = time.time()
    shown = None
    while True:
        el = time.time() - t0
        if el >= secs:
            break
        if countdown and (secs - el) <= 3.2:
            n = int(secs - el) + 1
            if n != shown and n >= 1:
                shown = n
                print('        %d …' % n, flush=True)
                if tick:
                    beep(kind='tick')
        time.sleep(0.02)
    f_after = STATE['frames']
    if debug:
        print('      [debug] 窗口 %.2fs：帧号 %d → %d（+%d）'
              % (time.time() - t0, f_begin, f_after, f_after - f_begin), flush=True)
    return f_begin, f_after


def pause(prompt):
    """段间停靠点：等人走到下一个站位再继续（--auto 时跳过）。"""
    print('')
    print('  >>> %s' % prompt)
    try:
        input('      （准备好后按回车继续）')
    except EOFError:
        print('      （无交互输入，直接继续）')


def wait_frames(min_frames=3, timeout=20.0):
    """等管道真正出帧，避免用户在「设备没出帧」时空做动作。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if STATE['frames'] >= min_frames:
            return True
        time.sleep(0.1)
    return False


def on_frame(z0, z1, stats):
    for k in ('frames', 'fps', 'packets', 'bad', 'skipped',
              'connected', 'reconnects', 'read_errors'):
        if k in stats:
            STATE[k] = stats[k]


def make_source(args):
    if args.source == 'loopback':
        return rs.LoopbackSource()
    if args.source == 'replay':
        if not args.replay_file:
            raise SystemExit('--source replay 需要 --replay-file')
        return rs.ReplaySource(args.replay_file, loop=True)
    # baud=None → 由 SerialSource 按串口指纹自动定波特率（1T2R 921600 / 2T4R 1500000）。
    # 写死 921600 会在 2T4R 上开成错波特率 → 收不到有效帧，整段采集成废数据。
    return rs.SerialSource(port=args.port, baud=args.baud)


def probe_port(port, baud=None):
    """提前试开一次串口：把「被别的进程占着」这类问题在采集前就说清楚。

    baud=None（新默认）→ 先按串口指纹探板型再取该板波特率；探测不出用默认板型。
    """
    import serial
    if not baud:
        try:
            b = rs.probe_board(port)
        except Exception:
            b = None
        baud = rs.baud_for(b)
    try:
        ser = serial.Serial(port, baud, timeout=0.2)
    except Exception as e:
        return '%s: %s' % (type(e).__name__, e)
    try:
        ser.close()
    except Exception:
        pass
    return None


def capture_segment(args, dist, band, bin_path, labels_path, index, total):
    tag = 'd%05.2f' % dist

    print('')
    print('=' * 76)
    print('第 %d/%d 段：站位 %.2f m   距离带 %s m   每动作 %d 次'
          % (index, total, dist, band, args.reps))
    print('  动作: %s' % '、'.join(ACTION_TEXT[a] for a in args.action_codes))
    print('  录制: %s' % bin_path)
    print('=' * 76)

    if STATE['frames'] == 0 and not wait_frames(3, args.start_delay + 5):
        print('  !! 没有收到任何雷达帧，本段跳过（检查设备/串口）。')
        return None, None

    labels = []
    print('  协议：准备 %.1f s →（嘟）做动作 %.1f s →（停）静息 %.1f s；'
          '动作前 %.1f s 算人的反应时间，不计入真值。'
          % (args.prep, args.move, args.settle, args.trim))
    if args.lead > 0:
        # 开场先录一段纯站定：既给「自动选带」攒够检出帧，也给 rest 基线一个干净窗口。
        # 默认 0 = 不插这段，旧协议逐帧不变。
        cue(args.lead,
            '开场：请站定保持不动 %.0f 秒（用于自动选带与静息基线）' % args.lead,
            countdown=True, tick=args.auto)
    cue(args.prep, '【准备】站到 %.2f m，正对雷达，站回中间、完全静止' % dist,
        countdown=True, tick=args.auto)

    trim_f = int(round(args.trim / FP))

    last_act = None
    for act in args.action_codes:
        if args.auto and act != last_act:
            # 人也看不到屏幕：换动作类别时先连响 3 声高音，再用语音说明本轮做什么，
            # 否则他无法知道这一次该蹲还是该跳（这是旧协议在雷达前失效的直接原因）。
            for _ in range(3):
                beep(kind='switch')
                time.sleep(0.18)
            speak('现在做 %d 次%s。听到长音就开始，保持住。'
                  % (args.reps, ACTION_TEXT[act]))
        last_act = act
        for k in range(1, args.reps + 1):
            print('  -- %s 第 %d/%d 次 --' % (ACTION_TEXT[act], k, args.reps))
            if act in ('LEFT', 'RIGHT') and args.stride > 0:
                cue(args.prep, '准备：站回中间（左右两侧各留 ≥%.1f m 空间）、完全静止'
                    % args.stride, countdown=True, tick=args.auto)
            else:
                cue(args.prep, '准备：站回中间、完全静止', countdown=True,
                    tick=args.auto)
            print('    ' + '>' * 24)
            if act in ('LEFT', 'RIGHT') and args.stride > 0:
                act_txt = ('大跨步向【%s】横移，至少 %.1f 米，走到后停住别动'
                           % ('左' if act == 'LEFT' else '右', args.stride))
            else:
                act_txt = '「%s」' % ACTION_TEXT[act]
            f_beep, _ = cue(args.move, '现在开始%s！保持 %.1f 秒'
                            % (act_txt, args.move), do_beep=True,
                            debug=args.debug_frames)
            print('    ' + '<' * 24)
            f_stop, _ = cue(args.settle, '【停】回到中间，完全静止',
                            do_beep=True, kind='stop')
            s = f_beep + trim_f              # 扣掉人的反应时间
            if f_stop <= s:                  # 窗口内一帧都没来，别写出无效真值
                print('    !! 本段帧太少（%d→%d），本次不给真值' % (f_beep, f_stop))
                continue
            note = 'cue 驱动；%s' % tag
            if act in ('JUMP', 'DUCK'):
                note += '；起跳/下蹲为候选动作'
            if args.stride > 0 and act in ('LEFT', 'RIGHT'):
                note += '；要求横移≥%.1f m' % args.stride
            labels.append({
                'file': os.path.basename(bin_path), 'action': act,
                't_start': round(s * FP, 3), 'frame_start': s,
                't_end': round(f_stop * FP, 3), 'frame_end': f_stop,
                'duration': round((f_stop - s) * FP, 3),
                'note': note,
            })

    print('  -- 静息基线段（无真值：任何预测都计为误报） --')
    if args.auto:
        speak('最后一段，请完全静止 %d 秒，保持不动。' % int(round(args.rest)))
    cue(args.rest, '最后：完全静止 %.0f 秒（不要动）' % args.rest)
    t_seg_end = STATE['frames']

    meta = {'file': os.path.basename(bin_path), 'distance_m': dist,
            'band': list(band), 'frame_period': FP,
            'frames': t_seg_end, 'source': args.source,
            'lead': args.lead,
            'board': BOARD,
            'note': '时间轴 = 帧号 × %.4f s（板型 %s，radar_source.frame_period）；'
                    '真值由 segment_capture.py 按提示音时刻的真实帧号生成'
                    % (FP, BOARD)}
    lt.save_labels(labels_path, labels, meta)
    print('  本段真值 %d 条 → %s' % (len(labels), labels_path))
    return bin_path, labels_path


def compare_segment(bin_path, labels_path, band, tol):
    records, info = ra.analyze_file(bin_path, band=tuple(band), log=lambda m: None)
    labels = lt.load_labels(labels_path)
    stats, _rows, preds = lt.compare(records, labels, tol=tol)
    return {'bin': bin_path, 'info': info, 'labels': len(labels),
            'stats': stats, 'preds': preds}


def _dw(s):
    """显示宽度：中日韩字符占两列，用来把中文表头对齐。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def _pad(s, n):
    return s + ' ' * max(0, n - _dw(s))


def table(agg):
    lines = ['%s %6s %6s %6s %6s %8s %8s'
             % (_pad('动作', 11), '真值', '命中', '漏报', '误报', '精确率', '召回率')]
    for act, s in sorted(agg.items()):
        prec = s['hit'] / s['pred'] if s['pred'] else float('nan')
        rec_ = s['hit'] / s['truth'] if s['truth'] else float('nan')
        lines.append('%s %6d %6d %6d %6d %8s %8s'
                     % (_pad(ACTION_TEXT.get(act, act), 11), s['truth'], s['hit'],
                        s['miss'], s['fp'],
                        ('%.2f' % prec) if s['pred'] else '—',
                        ('%.2f' % rec_) if s['truth'] else '—'))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description='分段定距采集 + 自动打真值 + 自动对照')
    ap.add_argument('--port', default='COM3')
    ap.add_argument('--baud', type=int, default=None,
                    help='串口波特率；默认 None = 按板型指纹自动'
                         '（1T2R 921600 / 2T4R 1500000）')
    ap.add_argument('--board', default='auto',
                    choices=['auto', '1t2r', '2t4r'],
                    help='板型；默认 auto = 从串口指纹/回放文件自动判断')
    ap.add_argument('--source', default='serial',
                    choices=['serial', 'loopback', 'replay'])
    ap.add_argument('--replay-file', default=None,
                    help='--source replay 时的抓包文件（循环回放，用于无硬件自检）')
    ap.add_argument('--outdir', default=os.path.join(os.path.dirname(HERE),
                                                     '.deepworks', 'tmp', 'seg'))
    ap.add_argument('--distances', default='1.0,1.25,1.5',
                    help='站位距离（米，逗号分隔）')
    ap.add_argument('--actions', default='left,right',
                    help='要采集的动作（left,right,duck,jump）。'
                         '跳/蹲当前只能给候选项，默认不含')
    ap.add_argument('--reps', type=int, default=5, help='每个动作重复次数')
    ap.add_argument('--prep', type=float, default=3.0, help='每次动作前的准备秒数')
    ap.add_argument('--lead', type=float, default=0.0,
                    help='开场先录一段纯站定秒数（>0 时给自动选带攒帧 + 让静息基线'
                         '干净；默认 0 = 与旧协议逐帧一致）')
    ap.add_argument('--move', type=float, default=2.5, help='动作保持秒数')
    ap.add_argument('--stride', type=float, default=0.0,
                    help='左/右移要求的最小横移距离(米)。>0 时提示语与真值备注改为'
                         '「大跨步横移」，用于验证「位移够大才可判」；0=用旧提示语')
    ap.add_argument('--settle', type=float, default=3.0, help='动作后静息秒数')
    ap.add_argument('--rest', type=float, default=25.0, help='段尾静息基线秒数')
    ap.add_argument('--trim', type=float, default=0.3, help='真值区间起点留出的反应时间')
    ap.add_argument('--tol', type=float, default=0.45, help='对照时真值区间放宽秒数')
    ap.add_argument('--start-delay', type=float, default=10.0, help='开场阅读说明的秒数')
    ap.add_argument('--auto', action='store_true',
                    help='无人值守：不要求按回车（人站在雷达前看不到屏幕、够不到键盘）；'
                         '准备期最后 3 秒数拍，换动作/换站位改用语音+提示音说明')
    ap.add_argument('--walk-time', type=float, default=20.0,
                    help='--auto 时两段之间留给你走到下一站位的秒数（默认 20）')
    ap.add_argument('--no-speak', action='store_true',
                    help='关掉中文语音播报（默认用 Windows SAPI / Microsoft Huihui）')
    ap.add_argument('--no-compare', action='store_true', help='采完不做对照')
    ap.add_argument('--no-beep', action='store_true',
                    help='关掉蜂鸣（默认用 winsound.Beep，Windows 原生可靠）')
    ap.add_argument('--debug-frames', action='store_true',
                    help='打印每个动作窗口内实际收到的帧数（排查节奏问题）')
    args = ap.parse_args(argv)
    _stdout_utf8()
    global NO_BEEP, NO_SPEAK
    NO_BEEP = bool(args.no_beep)
    NO_SPEAK = bool(args.no_speak)

    args.action_codes = [ACTION_KEY[a.strip().lower()]
                         for a in args.actions.split(',') if a.strip()]
    distances = [float(x) for x in args.distances.split(',') if x.strip()]
    os.makedirs(args.outdir, exist_ok=True)
    preflight_board(args)
    print('[板型] %s：帧周期 %.4f s，门距 %.4f m%s'
          % (BOARD, FP, rs.range_res(BOARD),
             '（--board 显式指定）' if getattr(args, 'board', 'auto') != 'auto' else ''))

    per_action = args.reps * args.move
    est = len(distances) * (len(args.action_codes) * args.reps
                            * (args.prep + args.move + args.settle)
                            + args.rest + args.lead + 10) / 60.0
    print('=' * 76)
    print('分段定距采集（提示音驱动，自动打真值）')
    print('=' * 76)
    print('数据源: %s%s' % (args.source, ('  ' + args.port) if args.source == 'serial' else ''))
    print('站位: %s m    动作: %s × %d 次    预计耗时约 %.1f 分钟'
          % (distances, [ACTION_TEXT[a] for a in args.action_codes], args.reps, est))
    print('输出目录: %s' % args.outdir)
    print('')
    print('请你这样做（人要在设备旁）：')
    print('  1) 站到提示的距离，正对雷达，站回中间；')
    print('  2) 每次看到「准备」就把身体回到中间、完全静止；')
    print('  3) 听到『嘟』立刻开始做指定动作，并**保持住** %.1f 秒；' % args.move)
    print('  4) 看到「停」就回到中间，继续等下一次提示；')
    print('  5) 每段最后有一段「完全静止」，任何误报都会计入统计，请不要动。')
    print('  动作幅度：横向跨半步~一步，自然速度，别冲刺；手机等金属物别放雷达正前方。')
    print('')

    if args.source == 'serial':
        err = probe_port(args.port, args.baud)
        if err:
            print('!! 打不开 %s：%s' % (args.port, err))
            print('   先确认 server.py 已停（串口独占），再重试。')
            return 2

    if args.auto:
        # 人站在雷达前，键盘是够不到的：用语音 + 数拍倒计时替掉「按回车」。
        # 8 s 是「从电脑走到 1 m 站位」的下限；--start-delay 更大时用更大的。
        lead = max(args.start_delay, 8.0)
        speak('采集即将开始。请站到雷达正前方 %.2f 米处，面向雷达，站好不要动。'
              % distances[0])
        cue(lead, '【无人值守】请站到 %.2f m 处、正对雷达、完全静止'
            '（约 %.0f 秒后自动开始）' % (distances[0], lead),
            countdown=True, tick=True)
    else:
        print('按回车开始（%s）…' % ACTION_TEXT.get(args.action_codes[0], ''))
        try:
            input()
        except EOFError:
            print('（无交互输入，直接开始）')

    results = []
    for i, dist in enumerate(distances, 1):
        band = DIST_BAND.get(dist)
        if band is None:
            band = ra.BAND_DEFAULT
            print('  (%.2f m 没有预设距离带，用默认 %s)' % (dist, band))
        if BOARD == '2t4r':
            band = band_for_dist(dist)
            print('  (2T4R：%.2f m 站位 → 距离带 %s（提案，门距 %.3f m）'
                  % (dist, band, rs.range_res('2t4r')))

        if i > 1:
            if args.auto:
                speak('请走到雷达正前方 %.2f 米处，站好不要动，下一段马上开始。' % dist)
                cue(args.walk_time,
                    '请走到 %.2f m 处，正对雷达站好（约 %.0f 秒后自动继续）'
                    % (dist, args.walk_time), countdown=True, tick=True)
            else:
                pause('请走到 %.2f m 处，正对雷达站好（下一段马上开始）' % dist)

        stop = threading.Event()
        STATE.update({'frames': 0, 'fps': 0.0, 'packets': 0, 'bad': 0,
                      'skipped': 0, 'read_errors': 0})
        stem = os.path.join(args.outdir, 'seg_%05.2f' % dist)
        bs = stem + '.bin'
        lp = stem + '.labels.jsonl'
        src = make_source(args)
        th = threading.Thread(target=rs.build_pipeline,
                              args=(src, on_frame, stop),
                              kwargs={'record_path': bs, 'verbose': False},
                              daemon=True)
        th.start()
        time.sleep(0.5)
        # src.open() 里已完成串口指纹识别 → 以它为权威刷新帧周期（预检只是预判）
        set_board(resolve_board(args, src))
        try:
            bin_path, labels_path = capture_segment(args, dist, band, bs, lp,
                                                    i, len(distances))
        except KeyboardInterrupt:
            print('\n!! 用户中断采集。')
            stop.set()
            th.join(timeout=3)
            break
        finally:
            stop.set()
            th.join(timeout=3)

        if bin_path is None:
            continue
        print('  段统计: 帧 %d，平均 fps %.2f（1/fps = %.4f s/帧，'
              '离线按 %.4f s/帧算时间轴 → 漂移 %.2f%%）'
              % (STATE['frames'], STATE['fps'],
                 (1 / STATE['fps']) if STATE['fps'] else 0.0, FP,
                 (abs(1 / STATE['fps'] - FP) / FP * 100) if STATE['fps'] else 0.0))
        results.append({'dist': dist, 'band': band, 'bin': bin_path,
                        'labels': labels_path, 'fps': STATE['fps'],
                        'frames': STATE['frames']})

    if not results:
        print('没有任何完成的采集段，结束。')
        return 3

    # ---------------- 对照 ----------------
    out_txt = os.path.join(args.outdir, 'compare-all.txt')
    head = ['分段定距采集 —— 预测 vs 真值 汇总',
            '数据源 %s   动作 %s   reps=%d   反应时间扣除 %.1f s'
            % (args.source, args.actions, args.reps, args.trim),
            '时间轴 = 帧号 × %.4f s；对照 tol = ±%.2f s' % (FP, args.tol),
            '-' * 76]
    body = []
    agg = {}
    if not args.no_compare:
        for r in results:
            print('')
            print('对照中: %s（带 %s）…' % (os.path.basename(r['bin']), r['band']))
            c = compare_segment(r['bin'], r['labels'], r['band'], args.tol)
            r['stats'] = c['stats']
            n = c['info']['frames']
            body.append('【%.2f m】帧 %d，采集期实测 %.2f fps（%.4f s/帧，离线按 %.4f）　带 %s'
                        % (r['dist'], n, r['fps'],
                           (1 / r['fps']) if r['fps'] else 0.0, FP, tuple(r['band'])))
            for act, st in c['stats'].items():
                a = agg.setdefault(act, {'truth': 0, 'hit': 0, 'miss': 0,
                                         'pred': 0, 'fp': 0})
                for k in a:
                    a[k] += st[k]
            body += ['  ' + ln for ln in table(c['stats'])]
            body.append('')
        body.append('=' * 76)
        body.append('合计（各段相加，仅 %s 动作；静息段无真值 → 出现的事件全算误报）'
                    % '/'.join(ACTION_TEXT[a] for a in args.action_codes))
        body += table(agg)
        body.append('')
        body.append('说明：跳/蹲在当前硬件（无俯仰角）下只能给候选项，'
                    '本表不得作为跳/蹲的验收依据（AGENTS.md 验证门）。')
        console = '\n'.join(head + body)
        print('')
        print(console)
        with open(out_txt, 'w', encoding='utf-8') as f:
            f.write(console + '\n')
        print('\n汇总已写入: %s' % out_txt)
    else:
        print('\n（--no-compare：跳过对照。稍后可手动跑 label_tool.py --compare）')

    print('')
    print('逐段复算命令：')
    for r in results:
        print('  python label_tool.py --file "%s" --band %s --compare'
              % (r['bin'], ' '.join('%.2f' % b for b in r['band'])))
    if args.auto:
        speak('采集完成，请回到电脑前查看结果。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
