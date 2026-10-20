# -*- coding: utf-8 -*-
"""
离线自检（无需硬件）:
  1) 解析实机抓包 cap.bin → 帧同步、包数、帧率、坏包位置、真实帧上的点云输出
  2) 编码-解码闭环：合成已知目标 → 编码为真实字节流 → 解析 → 反算目标位置
  3) 方位角符号与线性度
  4) 采集会话控制：开始 / 结束 / 重置 / 停滞自愈（不碰串口，全用合成源）
  5) 硬件停机下行帧 与 固件解析规则 的一致性（只构造不发送）
  6) 近距动作识别（合成点序列）
输出 UTF-8 文本 (selftest-report.txt)，作为可复查证据。
"""
import io
import math
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import radar_dsp as dsp
from radar_dsp import PointCloudProcessor
from radar_protocol import StreamParser, FrameAssembler
from radar_source import LoopbackSource

OUT = io.StringIO()


def p(*a):
    print(*a)
    print(*a, file=OUT)


def test_capture(path):
    p('=' * 74)
    p('测试 1  实机抓包解析: %s' % path)
    p('=' * 74)
    if not os.path.exists(path):
        p('!! 文件不存在，跳过')
        return
    data = open(path, 'rb').read()
    parser = StreamParser()
    asm = FrameAssembler()
    frames = []
    first_rf = None
    for i in range(0, len(data), 4096):
        for rf in parser.feed(data[i:i + 4096]):
            if first_rf is None:
                first_rf = rf
            pair = asm.push(rf)
            if pair is not None:
                frames.append(pair)
    dt = len(data) / (52 * 991.0)     # 按实测速率折算时长
    p('字节数              : %d' % len(data))
    p('解析包数            : %d' % parser.packets)
    p('跳过字节 / 坏包     : %d / %d   重同步位置: %s' %
      (parser.skipped_bytes, parser.bad_packets, parser.resync_at[:8]))
    p('拼装完整雷达帧      : %d   丢弃残缺帧: %d' % (len(frames), asm.dropped))
    p('估算帧率            : %.2f fps   (期望 ≈7.72 fps)' % (len(frames) / dt))
    p('首包 chirp/rx       : chirp=%d rx=%d' % (first_rf.chirp, first_rf.rx))
    p('首包 8 距离单元 (大端 int16 → 复数):')
    p('  ' + '  '.join('%.0f%+.0fj' % (z.real, z.imag) for z in first_rf.bins))
    if frames:
        z0, z1 = frames[len(frames) // 2]
        p('中位帧矩阵          : z0=%s z1=%s' % (z0.shape, z1.shape))
        p('幅度 均值/峰值      : %.1f / %.1f' % (np.abs(z0).mean(), np.abs(z0).max()))
        proc = PointCloudProcessor(cfar_scale=2.0, min_power=50.0, remove_clutter=False)
        p('真实数据点云（末帧, 静态杂波保留）:')
        for k, (a, b) in enumerate(frames[-3:]):
            pts, noise = proc.process(a, b)
            s = '; '.join('r=%.2fm θ=%+.1f° v=%+.2f p=%.2f' %
                          (q['r'], q['deg'], q['v'], q['p']) for q in pts[:4])
            p('  帧#%d 噪声底=%.0f 点数=%d   %s' % (len(frames) - 3 + k, noise, len(pts), s or '—'))
    return frames


def test_loopback(targets):
    p('')
    p('=' * 74)
    p('测试 2  编码-解码闭环（合成已知目标，走真实字节流）')
    p('=' * 74)
    proc = PointCloudProcessor(cfar_scale=2.0, min_power=200.0, remove_clutter=True)
    src = LoopbackSource(targets=targets, frames=48)
    src.open()
    parser = StreamParser()
    asm = FrameAssembler()
    ok = tested = 0
    t0 = time.time()
    while tested < 40 and time.time() - t0 < 60:
        chunk = src.read(0.05)
        if not chunk:
            continue
        for rf in parser.feed(chunk):
            pair = asm.push(rf)
            if pair is None:
                continue
            tested += 1
            tx, ty, tv = src.frame_targets[tested - 1]
            tr = math.hypot(tx, ty)
            ta = math.degrees(math.atan2(tx, ty))
            pts, _ = proc.process(*pair)
            if not pts:
                p('  帧#%-3d 未检出' % tested)
                continue
            q = pts[0]
            good = (abs(q['r'] - tr) <= 0.7 and abs(q['deg'] - ta) <= 6.0
                    and abs(q['v'] - tv) <= 0.12)
            ok += good
            if tested <= 6 or tested % 12 == 0 or not good:
                p('  帧#%-3d 真值 r=%5.2f θ=%+6.1f° v=%+.2f  →  测得 r=%5.2f θ=%+6.1f° v=%+.2f '
                  '点数=%d %s' % (tested, tr, ta, tv, q['r'], q['deg'], q['v'], len(pts),
                                  'OK' if good else 'FAIL'))
    p('闭环一致            : %d / %d' % (ok, tested))
    return ok, tested


def test_angle():
    p('')
    p('=' * 74)
    p('测试 3  方位角符号与线性度（静态目标，保留 DC）')
    p('=' * 74)
    proc = PointCloudProcessor(cfar_scale=2.0, min_power=200.0, remove_clutter=False)
    errs = []
    for x in (-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0):
        z0, z1 = dsp.synth_frame(target=(x, 3.0, 0.0), seed=1)
        pts, _ = proc.process(z0, z1)
        got = pts[0]['deg'] if pts else float('nan')
        want = math.degrees(math.atan2(x, 3.0))
        errs.append(abs(got - want))
        p('  真实 x=%+4.1f m → 期望方位 %+6.1f°   测得 %+6.1f°   (r=%.2f m)'
          % (x, want, got, pts[0]['r'] if pts else float('nan')))
    p('方位角最大误差      : %.2f°' % max(errs))
    return max(errs)


class _FastLoopback(LoopbackSource):
    """把合成数据一次性吐完，之后不再产出 —— 用来人为制造「停滞」。"""

    def read(self, timeout=0.05):
        if getattr(self, 'pos', 0) >= len(getattr(self, 'data', b'')):
            time.sleep(0.01)
            return b''
        n = min(len(self.data) - self.pos, 400 * 52)
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk


def test_control():
    p('')
    p('=' * 74)
    p('测试 4  采集会话控制（开始 / 结束 / 重置 / 停滞自愈）')
    p('=' * 74)
    from radar_control import RadarController

    got = {'n': 0}
    boxes = []

    def on_frame(z0, z1, stats):
        got['n'] += 1

    def factory():
        s = _FastLoopback(frames=5)      # 每次会话只吐 5 帧真实字节流，然后沉默
        boxes.append(s)
        return s

    ctl = RadarController(factory, on_frame, stall_timeout=1.2, log=p)
    st = ctl.status()
    p('  初始              : active=%s sessions=%d' % (st['active'], st['sessions']))
    a_ok = (not st['active']) and st['sessions'] == 0

    ctl.control('start')
    time.sleep(1.0)
    st = ctl.status()
    p('  开始后            : active=%s sessions=%d frames=%d 源=%s'
      % (st['active'], st['sessions'], st['frames'], st['source']))
    b_ok = st['active'] and st['sessions'] == 1 and st['frames'] > 0

    ctl.control('stop')
    time.sleep(0.4)
    st = ctl.status()
    p('  结束后            : active=%s stops=%d elapsed=%.1fs since_frame=%s'
      % (st['active'], st['stops'], st['elapsed'], st['since_frame']))
    c_ok = (not st['active']) and st['stops'] == 1 and st['elapsed'] == 0.0

    ctl.control('start')
    time.sleep(3.2)
    st = ctl.status()
    p('  再次开始+停滞自愈 : sessions=%d restarts=%d frames=%d'
      % (st['sessions'], st['restarts'], st['frames']))
    d_ok = st['sessions'] == 2 and st['restarts'] >= 1

    n_before = st['frames']
    st = ctl.control('reset')
    time.sleep(0.3)
    p('  重置（采集中）    : frames %d→%d restarts→%d active=%s（清屏/重新计时，采集继续）'
      % (n_before, st['frames'], st['restarts'], st['active']))
    e_ok = st['active'] and st['frames'] <= n_before and st['restarts'] == 0

    ctl.stop()
    time.sleep(0.3)
    st = ctl.control('reset')
    time.sleep(0.2)
    p('  重置（已停止）    : sessions=%d active_seconds=%.1f'
      % (st['sessions'], st['active_seconds']))
    f_ok = st['sessions'] == 0 and st['active_seconds'] == 0.0
    ctl.shutdown()

    ok = all([a_ok, b_ok, c_ok, d_ok, e_ok, f_ok])
    p('  状态机自检        : %s  (初始=%s 开始=%s 结束=%s 自愈=%s 重置中=%s 重置停=%s)'
      % ('通过' if ok else '需复核', a_ok, b_ok, c_ok, d_ok, e_ok, f_ok))
    return ok


def test_hwstop():
    p('')
    p('=' * 74)
    p('测试 5  硬件停机下行帧 与 固件解析规则 的一致性（离线，不发送任何数据）')
    p('=' * 74)
    from radar_hwstop import (frame, build_plan_frames, FirmwareChecker,
                              decode_write_reg, parse_ack, START_CFG_CMD,
                              FINISH_CFG_CMD, WRITE_REG_CMD, MAX_REGS_PER_FRAME)
    checks = []

    # (a) 进入/退出命令模式帧与固件头文件给出的样例逐字节一致
    want_start = 'fd fc fb fa 02 00 ff 00 04 03 02 01'
    got_start = frame(START_CFG_CMD).hex(' ')
    checks.append(('命令模式帧字节', got_start == want_start, got_start))

    # (b) 写寄存器帧能被固件那条状态机原样解析回来
    regs = [(0x40, 0x1234), (0x41, 0x0000)]
    f = frame(WRITE_REG_CMD, b'\x09\x00' + b''.join(
        int(a).to_bytes(2, 'little') + int(v).to_bytes(2, 'little') for a, v in regs))
    chk = FirmwareChecker()
    cmds = chk.feed(f)
    dev, back = decode_write_reg(cmds[0]) if cmds else (None, [])
    checks.append(('WRITE_REG 往返', len(cmds) == 1 and cmds[0]['type'] == WRITE_REG_CMD
                   and chk.errors == 0 and dev == 9 and back == regs,
                   'dev=0x%02X regs=%s' % (dev or 0, back)))
    checks.append(('固件左移语义', dev == 9,
                   'cmdData[0]=0x09 → 固件侧 devAddress=0x%02X' % ((dev or 0) << 1)))

    # (c) 超过单帧寄存器上限必须分帧，且每帧都能解析
    many = [(0x40 + i, i) for i in range(MAX_REGS_PER_FRAME + 1)]
    fs = build_plan_frames([{'dev': 9, 'regs': many, 'label': 'x'}])
    wr = [x for lab, x in fs if FirmwareChecker().feed(x)
          and FirmwareChecker().feed(x)[0]['type'] == WRITE_REG_CMD]
    ok_c = len(wr) == 2 and all(len(FirmwareChecker().feed(x)) == 1 for x in wr)
    checks.append(('分帧 ≤%d 条/帧' % MAX_REGS_PER_FRAME, ok_c, '%d 帧' % len(wr)))

    over = False
    try:
        frame(WRITE_REG_CMD, b'\x00' * 60)
    except ValueError:
        over = True
    checks.append(('超长数据域被拒', over, '数据域上限由固件 CMD_LEN_MAX=64 推出'))

    # (d) 应答解析。**注意约定**：固件启动时 masterProtocolVer=0（老约定），
    #     此时 FillCmdAck 反转状态 → 1=ACK_OK、0=ACK_FAIL；
    #     2026-10-15 T1 实机实测到的应答就是 status=1（老约定 = OK）。
    ack_bytes = bytes.fromhex('fdfcfbfa0400ff01010004030201')
    a = parse_ack(ack_bytes)
    checks.append(('ACK 解析（老约定 1=OK）', a and a['type'] == 0x00FF and a['status'] == 1
                   and a['status_text'] == 'ACK_OK' and a['ok'] is True,
                   'cmdType=0x%04X status=%s convention=%s'
                   % (a['type'], a['status_text'], a['convention'])))
    bad = parse_ack(bytes.fromhex('fdfcfbfa0400ff01000004030201'))
    checks.append(('ACK_FAIL 解析（老约定 0=FAIL）',
                   bad and bad['status'] == 0 and bad['ok'] is False, bad['status_text']))
    new = parse_ack(bytes.fromhex('fdfcfbfa0400ff01000004030201'), master_protocol_ver=1)
    checks.append(('新约定可切换（0=OK）', new and new['status'] == 0 and new['ok'] is True,
                   '%s convention=%s' % (new['status_text'], new['convention'])))

    # (e) 噪声流不应让校验器崩溃或误判
    chk = FirmwareChecker()
    chk.feed(bytes(range(256)) * 4)
    checks.append(('噪声流稳健', chk.errors > 0, '误字节 %d 个' % chk.errors))

    for name, ok, extra in checks:
        p('  %-18s: %s   %s' % (name, '通过' if ok else '失败', extra))
    p('  —— 以上只证明「帧构造与固件解析规则一致」；是否真能停机、能否恢复，')
    p('     必须等设备空闲后在实机上验证（见 docs/radar-hardware-stop-plan.md）。')
    return all(ok for _, ok, _ in checks)


def test_actions():
    p('')
    p('=' * 74)
    p('测试 6  近距动作识别（合成点序列，规则法，纯离线）')
    p('=' * 74)
    from radar_actions import ActionRecognizer

    def mk(px, r=1.2, pa=1.0):
        """合成一个「目标」：真实雷达一个人通常打出多点，这里用两点簇（带宽 6 cm）。"""
        out = []
        for dx, w in ((-0.03, 1.0), (0.03, 0.6)):
            x = px + dx
            out.append({'x': x, 'y': r, 'r': r,
                        'deg': math.degrees(math.atan2(x, r)),
                        'v': 0.0, 'p': pa, 'pa': pa * w})
        return out

    def run(rec, seq, dt=0.1295, t0=0.0):
        t = [t0]
        evs = []
        for pts in seq:
            r = rec.push(pts, t[0])
            evs += [e for e in r['events']]
            t[0] += dt
        return evs, t[0]

    checks = []

    # (a) 向右移动 0.06 m/帧 ≈ 0.46 m/s > 阈值 0.35 → LANE_RIGHT
    # ⚠ 2026-10-20：本段用例锁的是 **crit='delta' 的位移语义**（v_on/min_dx 只对 delta 生效），
    #   故显式指定，不受默认判据改为 xbase 的影响（两套约定符号相反，见 devlog §7.45.6）。
    rec = ActionRecognizer(crit='delta')
    evs, t_end = run(rec, [mk(-0.36 + 0.06 * i) for i in range(14)])
    names = [e['name'] for e in evs]
    checks.append(('右移识别', 'LANE_RIGHT' in names and 'LANE_LEFT' not in names,
                   '事件=%s' % names))

    # (b) 向左移动 → LANE_LEFT
    rec = ActionRecognizer(crit='delta')     # 同 (a)：锁 delta 语义
    evs, _ = run(rec, [mk(0.36 - 0.06 * i) for i in range(14)])
    names = [e['name'] for e in evs]
    checks.append(('左移识别', 'LANE_LEFT' in names and 'LANE_RIGHT' not in names,
                   '事件=%s' % names))

    # (c) 静止不动 → 不应产生左右事件
    rec = ActionRecognizer()
    evs, _ = run(rec, [mk(0.10) for _ in range(16)])
    names = [e['name'] for e in evs]
    checks.append(('静止不误报', not any(n in ('LANE_LEFT', 'LANE_RIGHT') for n in names),
                   '事件=%s' % (names or '无')))

    # (d) 带外目标（2.5 m）应被忽略
    rec = ActionRecognizer()
    evs, _ = run(rec, [mk(-0.36 + 0.06 * i, r=2.5) for i in range(14)])
    checks.append(('带外目标忽略', not evs, '事件=%s' % ([e['name'] for e in evs] or '无')))

    # (e) 目标消失 → TARGET_LOST；回来 → TARGET_BACK
    rec = ActionRecognizer()
    _, t1 = run(rec, [mk(0.0) for _ in range(6)])
    evs, t2 = run(rec, [[] for _ in range(8)], t0=t1)     # 8×0.13s = 1.04s > 0.6s
    lost = [e['name'] for e in evs]
    evs2, _ = run(rec, [mk(0.0) for _ in range(3)], t0=t2)
    back = [e['name'] for e in evs2]
    checks.append(('丢失/恢复', 'TARGET_LOST' in lost and 'TARGET_BACK' in back,
                   '丢失=%s 恢复=%s' % (lost, back)))

    # (f) 跳/蹲：必须是「持续的变化过程」才报候选，且候选必须自带未验证标记
    #     单帧能量毛刺属静态噪声尖峰，不算动作（用户要求判「移动的动态过程」）
    rec = ActionRecognizer()
    run(rec, [mk(0.0, pa=1.0) for _ in range(8)])
    evs, _ = run(rec, [mk(0.0, pa=3.2)])
    names = [e['name'] for e in evs]
    checks.append(('单帧毛刺不算跳', not any(n in ('JUMP', 'DUCK') for n in names),
                   '事件=%s' % (names or '无')))

    rec = ActionRecognizer()
    run(rec, [mk(0.0, pa=1.0) for _ in range(8)])
    evs, _ = run(rec, [mk(0.0, pa=3.2) for _ in range(5)])
    jump = [e for e in evs if e['name'] == 'JUMP']
    ok_j = bool(jump) and jump[0].get('gated') is True and jump[0].get('verified') is False
    checks.append(('跳=候选且未验证', ok_j,
                   '事件=%s' % ([e['name'] for e in evs] or '无')))

    rec = ActionRecognizer()
    run(rec, [mk(0.0, pa=1.0) for _ in range(8)])
    evs, _ = run(rec, [mk(0.0, pa=0.25) for _ in range(5)])
    duck = [e for e in evs if e['name'] == 'DUCK']
    ok_d = bool(duck) and duck[0].get('gated') is True
    checks.append(('蹲=候选且未验证', ok_d,
                   '事件=%s' % ([e['name'] for e in evs] or '无')))

    # (g) 判定必须只看「相对位移方向」，与质心绝对偏移无关
    #     实测质心可系统性偏 +0.5~+1.0 m（站位/安装几何导致）：
    #     偏置既不得在静止时误报，也不得让偏置方向的移动判错。
    rec = ActionRecognizer()
    evs, _ = run(rec, [mk(-0.80) for _ in range(16)])
    names = [e['name'] for e in evs]
    checks.append(('偏置静止不误报',
                   not any(n in ('LANE_LEFT', 'LANE_RIGHT') for n in names),
                   'px 恒为 -0.80，事件=%s' % (names or '无')))

    rec = ActionRecognizer(crit='delta')     # 同 (a)：锁 delta 语义
    evs, _ = run(rec, [mk(0.80 + 0.06 * i) for i in range(14)])
    names = [e['name'] for e in evs]
    checks.append(('偏置下右移仍判对',
                   'LANE_RIGHT' in names and 'LANE_LEFT' not in names,
                   'px 由 +0.80 起右移，事件=%s' % names))

    # (h) 单帧野值（杂波/重新捕获）不得污染「相对位移」
    #     注：max_step 默认 0（关闭）——稀疏点云下固定 0.5 m 门会丢掉大部分帧
    #     （见 radar_actions.MAX_STEP_DEFAULT 的实测依据）。这里显式打开以验证该能力，
    #     实际使用时该阈值应由 radar_calibrate.py 实测 p99(|Δpx|) 给出。
    rec = ActionRecognizer(max_step=0.5)
    t = [0.0]
    rej = False
    names = []
    for pts in ([mk(0.0) for _ in range(8)] + [mk(1.2)]
                + [mk(0.0) for _ in range(8)]):
        r = rec.push(pts, t[0])
        if r['features'] and r['features'].get('rejected'):
            rej = True
        names += [e['name'] for e in r['events']]
        t[0] += 0.1295
    checks.append(('野值帧被剔除',
                   rej and not any(n.startswith('LANE_') for n in names),
                   'rejected=%s 事件=%s' % (rej, names or '无')))

    # (i) 校准值生效：阈值来自实测（radar_calibrate.py），而非拍脑袋
    rec = ActionRecognizer(crit='delta', calib={'band': [1.0, 1.6], 'v_on': 0.9, 'v_off': 0.4,
                                                'min_dx': 0.6, 'max_step': 0.3})
    evs, _ = run(rec, [mk(-0.36 + 0.06 * i) for i in range(14)])
    names = [e['name'] for e in evs]
    checks.append(('校准阈值生效',
                   rec.band == (1.0, 1.6) and rec.v_on == 0.9 and rec.min_dx == 0.6
                   and not any(n.startswith('LANE_') for n in names),
                   'band=%s v_on=%.2f 事件=%s' % (rec.band, rec.v_on, names or '无')))
    # (j) crit='drift'（静息水平偏移）= 2026-10-18 新增判据，参考系由「窗内起点」
    #     换成「时间基线 base（近 rest_win 秒 px 中位数）」。它**仍是相对量**
    #     （只用 px − base，不比较 px 绝对值），所以「人停在一侧不动」不得误报，
    #     包括停在大负偏移一侧。背景与真值数据实测见
    #     docs/progress-radar-pointcloud.md §7.26~§7.28。
    checks.append(('crit 默认 = xbase', ActionRecognizer().crit == 'xbase',
                   '默认 crit=%s（2026-10-20 由 delta 改为 xbase，见 devlog §7.45.6：'
                   'delta 的方向符号与人体左右相反，0/11 方向正确）' % ActionRecognizer().crit))
    checks.append(('crit 仍可选 delta', ActionRecognizer(crit='delta').crit == 'delta',
                   'delta 保留为可选项（历史对照/回归用）'))

    rec = ActionRecognizer(crit='drift')
    evs, _ = run(rec, [mk(1.10) for _ in range(40)] + [mk(-1.20) for _ in range(8)])
    names = [e['name'] for e in evs]
    checks.append(('drift 左移识别',
                   names.count('LANE_LEFT') == 1 and 'LANE_RIGHT' not in names,
                   '事件=%s' % names))

    rec = ActionRecognizer(crit='drift')
    evs, _ = run(rec, [mk(1.10) for _ in range(40)] + [mk(0.60) for _ in range(8)])
    names = [e['name'] for e in evs]
    checks.append(('drift 右移识别',
                   names.count('LANE_RIGHT') == 1 and 'LANE_LEFT' not in names,
                   '事件=%s' % names))

    # 停在一侧不动（绝对偏置很大、甚至为负）→ 不得误报：base 会把它吸收为参考系。
    # 这条正是「drift 仍是相对量」的锚点，与 (g) 对应。
    rec = ActionRecognizer(crit='drift')
    evs, _ = run(rec, [mk(-2.00) for _ in range(40)])
    names = [e['name'] for e in evs]
    checks.append(('drift 偏置静止不误报',
                   not any(n in ('LANE_LEFT', 'LANE_RIGHT') for n in names),
                   'px 恒为 -2.00，事件=%s' % (names or '无')))

    # 基线样本不足（< 8 帧）→ 不产生方向证据（宁可不报也不猜）
    rec = ActionRecognizer(crit='drift')
    evs, _ = run(rec, [mk(-1.50) for _ in range(6)])
    names = [e['name'] for e in evs]
    checks.append(('drift 无基线不猜',
                   not any(n in ('LANE_LEFT', 'LANE_RIGHT') for n in names),
                   '仅 6 帧，事件=%s' % (names or '无')))

    # 非法参数必须被拒（防止 typo 静默退化成旧判据）
    try:
        ActionRecognizer(crit='drfit')
        bad = False
    except ValueError:
        bad = True
    try:
        ActionRecognizer(crit='drift', drift_left=0.2, drift_right=0.3)
        bad2 = False
    except ValueError:
        bad2 = True
    checks.append(('drift 参数校验', bad and bad2,
                   '未知 crit 与 right>=left 均被拒'))


    # (j2) crit='xbase'（段内自适应基线）= 2026-10-20 新增判据。
    #     动机：crit='delta' 与 crit='drift' 都在真机真值上失败——
    #       delta 在 cap-long-1020（1T2R，人站 0.7 m，14 个左右动作 + 366 s
    #         纯静息）上只有 3/14=21%、延迟中位 4.92 s；
    #       drift（拿**早先采的静息段**当参考系）6/14=43%——根因是静息段与
    #         动作段相隔数分钟、站位/姿态已慢漂移，参考系本身就过期了。
    #     xbase 把参考系换成**该段自身的近期水平**：
    #       dev = median(近 xbase_win 秒 px) − median(近 xbase_base_win 秒 px)
    #     两项同源相减 ⇒ 免疫慢漂移。实测 13/14=93%、延迟中位 2.33 s、
    #     静息误报 2.46 次/分；零假设对照（把真值窗整体平移 96~288 s 再评分）
    #     只有 19% ⇒ 超出随机基准 +74 个百分点，不是事件密度碰巧。
    #     ★ 左右阈值必须独立且符号已用真值标定（本几何下 dev>0 ⇒ LEFT）。
    #     合成序列把窗口缩到 0.40 s / 1.30 s，验证的是同一套逻辑。
    #     数据见 .deepworks/tmp/_cap1020_xbase_{verify,tune,fp,null}.txt。
    from radar_actions import XBASE_MIN_SAMPLES

    XW = dict(xbase_win=0.40, xbase_base_win=1.30)

    # 默认值必须能构造——默认值就是真机定标的那一组，不得被静默改掉
    rec = ActionRecognizer(crit='xbase')
    checks.append(('xbase 默认可构造',
                   rec.crit == 'xbase' and rec.xbase_thr_left == 0.10
                   and rec.xbase_thr_right == 0.18 and rec.xbase_release == 0.05,
                   'thr_left=+%.2f thr_right=-%.2f release=%.2f 长窗=%.0f s'
                   % (rec.xbase_thr_left, rec.xbase_thr_right,
                      rec.xbase_release, rec.xbase_base_win)))

    # 左移：px 由基线 0.50 抬到 0.80 ⇒ dev≈+0.30 >= +0.10 ⇒ LEFT
    rec = ActionRecognizer(crit='xbase', **XW)
    evs, _ = run(rec, [mk(0.50) for _ in range(40)] + [mk(0.80) for _ in range(12)])
    names = [e['name'] for e in evs]
    checks.append(('xbase 左移识别',
                   names.count('LANE_LEFT') == 1 and 'LANE_RIGHT' not in names,
                   '事件=%s' % names))

    # 右移：px 由 0.50 降到 0.20 ⇒ dev≈−0.30 <= −0.18 ⇒ RIGHT
    rec = ActionRecognizer(crit='xbase', **XW)
    evs, _ = run(rec, [mk(0.50) for _ in range(40)] + [mk(0.20) for _ in range(12)])
    names = [e['name'] for e in evs]
    checks.append(('xbase 右移识别',
                   names.count('LANE_RIGHT') == 1 and 'LANE_LEFT' not in names,
                   '事件=%s' % names))

    # 停在一侧不动（绝对偏置很大）→ 不得误报：两个窗口都吸收同一偏置。
    rec = ActionRecognizer(crit='xbase', **XW)
    evs, _ = run(rec, [mk(1.60) for _ in range(40)])
    names = [e['name'] for e in evs]
    checks.append(('xbase 偏置静止不误报',
                   not any(n in ('LANE_LEFT', 'LANE_RIGHT') for n in names),
                   'px 恒为 1.60，事件=%s' % (names or '无')))

    # ★ 本判据相对 drift 的**增量**：缓慢漂移不得误报。
    #   这是真机上真正的失败模式（站位在数分钟内慢漂）。静态参考系（drift）
    #   会把漂移当成动作；xbase 的长窗跟着漂，dev 始终保持小值。
    rec = ActionRecognizer(crit='xbase', **XW)
    evs, _ = run(rec, [mk(0.30 + 0.002 * i) for i in range(120)])
    names = [e['name'] for e in evs]
    checks.append(('xbase 慢漂移不误报',
                   not any(n in ('LANE_LEFT', 'LANE_RIGHT') for n in names),
                   'px 0.30→0.54 缓漂 120 帧（共 0.24 m），事件=%s' % (names or '无')))

    # 长窗样本不足 → 不产生方向证据（宁可不报也不猜，与 drift 同纪律）
    rec = ActionRecognizer(crit='xbase', **XW)
    evs, _ = run(rec, [mk(-1.50) for _ in range(6)])
    names = [e['name'] for e in evs]
    checks.append(('xbase 无长窗不猜',
                   not any(n in ('LANE_LEFT', 'LANE_RIGHT') for n in names),
                   '仅 6 帧(<%d)，事件=%s' % (XBASE_MIN_SAMPLES, names or '无')))

    # 非法参数必须被拒（否则会静默退化成「无滞回」或「长短窗颠倒」）
    bads = []
    for kw in (dict(xbase_base_win=0.40),      # 长窗必须严长于近窗
               dict(xbase_release=0.20),       # 释放门必须 < 两侧阈值
               dict(xbase_release_confirm=0),  # 释放确认至少 1 帧
               dict(xbase_thr_left=0.0)):      # 触发门必须 > 0
        try:
            ActionRecognizer(crit='xbase', **kw)
            bads.append(False)
        except ValueError:
            bads.append(True)
    checks.append(('xbase 参数校验', all(bads),
                   '%d 个非法取值均被拒: %s' % (len(bads), bads)))

    # (k) 距离带自动选择（band='auto'）= 2026-10-19 新增能力
    #     动机：出厂默认带 (0.70,1.35) 圈在 j=1 这个**空门**，真实站位落在 j=2
    #     ⇒ 用默认带 0/10 命中且**主动误报 3 次**（§7.28.3）。
    #     做法：预热期只按距离门数点数，锁定后照常判动作；
    #     预热期**不得**发任何动作事件（没有带就没有 px，宁可不判也不猜）。
    from radar_actions import BAND_DEFAULT

    rec = ActionRecognizer()
    checks.append(('自动带 默认关闭',
                   rec.band_mode == 'fixed' and rec.band == BAND_DEFAULT
                   and rec.band_source == 'fixed',
                   '默认 band_mode=%s band=%s' % (rec.band_mode, rec.band)))

    # 目标落在 j=2（r=1.70 m → 门 1.36~2.04 m）：预热 10 帧后必须锁定该门
    rec = ActionRecognizer(band='auto', auto_band_min_frames=10,
                           auto_band_max_frames=20, auto_band_min_points=4)
    evs, _ = run(rec, [mk(0.0, r=1.70) for _ in range(10)])
    names = [e['name'] for e in evs]
    lo, hi = 2 * dsp.RANGE_RES, 3 * dsp.RANGE_RES
    ok_k = (rec.band_gate == 2 and rec.band_source == 'auto'
            and abs(rec.band[0] - lo) < 1e-6 and abs(rec.band[1] - hi) < 1e-6
            and rec.band_locked_frame == 10
            and any(e['name'] == 'BAND_READY' and e.get('info') for e in evs))
    checks.append(('自动带 选中最强门', ok_k,
                   '门 j=%s band=(%.2f, %.2f) 第 %s 帧锁定 事件=%s'
                   % (rec.band_gate, rec.band[0], rec.band[1],
                      rec.band_locked_frame, names)))

    # 预热期内不得发动作事件（未达 min_frames：11 < 12）
    rec = ActionRecognizer(band='auto', auto_band_min_frames=12,
                           auto_band_max_frames=20)
    evs, _ = run(rec, [mk(-0.36 + 0.06 * i, r=1.70) for i in range(11)])
    names = [e['name'] for e in evs]
    checks.append(('自动带 预热不发动作', not names,
                   '预热 11 帧（未达 12）事件=%s' % (names or '无')))

    # 只有近场泄漏门（j=0）有点 → 不许假装选对了，必须退回默认带并如实标注
    rec = ActionRecognizer(band='auto', auto_band_min_frames=4,
                           auto_band_max_frames=6, auto_band_min_points=2)
    run(rec, [mk(0.0, r=0.30) for _ in range(6)])
    ok_fb = (rec.band_locked_frame == 6 and rec.band_source == 'auto-fallback'
             and rec.band == BAND_DEFAULT and rec.band_gate is None)
    checks.append(('自动带 泄漏门不冒充', ok_fb,
                   'source=%s band=%s' % (rec.band_source, rec.band)))

    # 锁定之后必须照常工作：同一序列在自动选出的带上应能判出右移
    # ⚠ 2026-10-20：这里**显式**用 crit='delta'。默认已改为 'xbase'（devlog §7.45.6），
    #   而两套判据的符号约定相反（delta: v_lat>0 ⇒ RIGHT；xbase: dev>0 ⇒ LEFT）。
    #   本用例测的是「自动选带锁定后判据照常工作」，与符号约定无关，故固定 delta 保持原断言。
    rec = ActionRecognizer(band='auto', auto_band_min_frames=6, crit='delta',
                           auto_band_max_frames=12, auto_band_min_points=4)
    run(rec, [mk(0.0, r=1.70) for _ in range(6)])
    evs, _ = run(rec, [mk(-0.36 + 0.06 * i, r=1.70) for i in range(14)],
                 t0=6 * 0.1295)
    names = [e['name'] for e in evs]
    checks.append(('自动带 锁定后可判',
                   'LANE_RIGHT' in names and 'LANE_LEFT' not in names,
                   'band=%s 事件=%s' % (rec.band, names)))

    # 参数校验：手误必须当场报错，不能被「没启用自动选带」吞掉
    bad_k = []
    for kw in ({'auto_band_min_frames': 0},
               {'auto_band_min_frames': 10, 'auto_band_max_frames': 5},
               {'auto_band_min_points': 0},
               {'auto_band_dominance': 0.5}):
        try:
            ActionRecognizer(**kw)
            bad_k.append(False)
        except ValueError:
            bad_k.append(True)
    checks.append(('自动带 参数校验', all(bad_k),
                   '4 个非法取值均被拒: %s' % bad_k))


    for name, ok, extra in checks:
        p('  %-18s: %s   %s' % (name, '通过' if ok else '失败', extra))
    return all(ok for _, ok, _ in checks)


if __name__ == '__main__':
    cap = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.join(HERE, '..', '.deepworks', 'tmp', 'cap.bin')
    test_capture(cap)
    ok, tested = test_loopback([(1.2, 3.0, -0.5), (0.0, 2.0, 0.4), (-1.6, 4.0, 0.45)])
    e = test_angle()
    c = test_control()
    h = test_hwstop()
    a = test_actions()
    p('')
    p('=' * 74)
    p('结论: 解析链路 %s；闭环 %d/%d；方位角误差 %.2f°；'
      '会话控制 %s；停机帧一致性 %s；近距动作 %s'
      % ('通过' if tested and ok == tested else '需复核', ok, tested, e,
         '通过' if c else '需复核', '通过' if h else '需复核', '通过' if a else '需复核'))
    p('  注：测试 5 只验证帧构造与固件解析规则一致，不代表实机能停机（未验证）。')
    p('      测试 6 的跳/蹲仅为规则法候选，未过 P0→T0→T1→T2 验证门，不得当验收结论。')
    p('=' * 74)
    out = os.path.join(HERE, 'selftest-report.txt')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(OUT.getvalue())
    print('\n报告已写入: %s' % out)
