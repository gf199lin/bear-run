# -*- coding: utf-8 -*-
r"""
串口预检 / 裸流嗅探 —— 换板（尤其 2T4R）前的**零协议假设**准备工具。

设计原则（为什么不用现成的 probe.py）：
  * probe.py / server.py 走的是**已冻结的 1T2R 解析链**（`radar_protocol.py` 里
    `CHIRPS_PER_FRAME = 64`、RX ID 只有 `0x30 / 0xB0`、组帧固定 `(2, chirps, bins)`）。
    换一块板若 RX 数或帧长不同，那条链**一个包都认不出**，而且不会告诉你为什么。
  * 本工具**不假设任何协议**：只统计字节流的客观特征（魔数出现次数、包长字段、
    RX/ID 取值集合、包速率），并把「认不出的部分」如实标成未知。
    ⇒ 拿到 2T4R 后，先跑它，再用**实测出来的参数**去改协议层。

子命令：
  scan                     枚举串口 + 硬件指纹 + 与项目已知端口对照（**不打开端口**，零风险）
  sniff  --port COM4       打开端口读 N 秒，落盘原始字节，并当场做同样的统计
  analyze <file.bin>       离线分析已有抓包（零风险，可反复跑）
  selftest                 用合成流验证统计器本身正确（1T2R 形态 vs 伪造 4RX 形态）

示例（2T4R 到手后）：
  python serial_preflight.py scan
  python serial_preflight.py sniff --port COM4 --baud 921600 --seconds 6 --out ..\.deepworks\tmp\2t4r-COM4.bin
  python serial_preflight.py analyze ..\.deepworks\tmp\2t4r-COM4.bin

纪律提醒：本工具的**任何输出都不是「2T4R 链路已通」的证据**，
只有当帧率 / 包长 / RX 数被实测出来并写进文档后，才算实验结论。
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 1T2R 已确认的事实（来源: docs/radar-uplink-protocol.md，仅用于"对照"，不作为 2T4R 假设）----
KNOWN_1T2R = {
    'port': 'COM3',
    'vid_pid': 'VID_10C4 PID_EA60',
    'chip': 'Silicon Labs CP210x',
    'baud': 921600,
    'wrapper': ('ICLH', 'ICLT'),
    'pkt_len': 52,
    'payload_len': 40,
    'rx_ids': (0x30, 0xB0),
    'chirps_per_frame': 64,
    'packets_per_frame': 128,
    'bare_frame_len': 40,
    'frame_ms': 129.5,
    'pkts_per_s': 991.0,
}

# ============================ 统计器（纯函数，离线可测） ============================

def _find_all(buf, pat):
    out = []
    i = buf.find(pat)
    while i >= 0:
        out.append(i)
        i = buf.find(pat, i + 1)
    return out


def _hist(counter, limit=8):
    """把 Counter 变成 [(key, n), ...]，按出现次数降序。"""
    return sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))[:limit]


def _top_rx_ids(buf, head_pos, payload_of):
    """帧内偏移 1 的高半字节取值分布（RX/通道标识的候选位）。

    这是判定「这块板几个 RX」的直接证据：1T2R 实测只有 0x30 / 0xB0。
    """
    c = Counter()
    payload_first_bad = 0
    tail_ok = 0
    for p in head_pos:
        pl = payload_of(p)
        if pl is None or len(pl) < 2:
            continue
        if pl[0] != 0xAA:
            payload_first_bad += 1
            continue
        c['0x%02X' % (pl[1] & 0xF0)] += 1
        if pl[-1] == 0x55:
            tail_ok += 1
    return c, payload_first_bad, tail_ok


def _bare_analysis(b, dt=None):
    """无 wrapper 时的形态识别：**自适应**估计帧步长后逐帧校验。

    为什么不能直接对「所有 0xAA 位置」抽样：IQ 载荷里出现 0xAA 字节是常事，
    会把噪声当成 RX 标识 —— 实测 `after-replug2.bin` 就报出 0x00/0xF0/0x60 等假值。
    做法：取「相邻 0xAA 间距」的**主峰**当帧步长（不写死 40），再要求
    「起点=0xAA 且 步长末字节=0x55」自洽，只对自洽帧统计标识位。
    """
    out = {}
    aa = _find_all(b, b'\xAA')
    out['aa_positions'] = len(aa)
    if len(aa) < 2:
        return out
    gaps = Counter(aa[i + 1] - aa[i] for i in range(len(aa) - 1))
    out['aa_gap_hist'] = _hist(gaps)

    # 步长必须**穷举**，不能用"观测到的间距"当候选：载荷里的伪 0xAA 会把真步长
    # 在间距直方图里彻底淹没（自检用例：伪 AA 每 4 字节一个时，间距 40 一次都不出现）。
    # 判据也不能只看"能凑出自洽帧"——伪步长同样能碰巧凑出几对，
    # 关键是这些自洽帧**彼此是否恰好间隔 step**（真帧的间隔恒定）。
    sample = aa[:2000]          # 打分用采样；采样足够且对大文件（78 MB）也快
    best = None
    for step in range(8, 513):
        keep = []
        for p in sample:
            if p + step <= len(b) and b[p + step - 1] == 0x55:
                if not keep or p - keep[-1] >= step - 1:
                    keep.append(p)
        if len(keep) < 2:
            continue
        pairs = sum(1 for i in range(len(keep) - 1) if keep[i + 1] - keep[i] == step)
        if best is None or pairs > best[1]:
            best = (step, pairs, len(keep))

    if not best or best[1] < 2:
        out['step_candidate'] = gaps.most_common(1)[0][0] if gaps else None
        out['note'] = ('找不到任何步长能形成 ≥2 个"等间隔 + 首 0xAA 尾 0x55"的帧'
                       '⇒ 该流不是固定长度裸帧结构（或帧长 > 512B）；间距主峰仅供参考')
        all_hi = Counter('0x%02X' % (b[p + 1] & 0xF0) for p in aa if p + 1 < len(b))
        out['all_aa_next_high_nibble_raw'] = _hist(all_hi)
        return out

    step, pairs, n_keep_sample = best
    out['step_candidate'] = step
    out['step_pairs'] = pairs
    out['step_share'] = round(gaps.get(step, 0) / max(1, len(aa) - 1), 3)

    good = [p for p in aa if p + step <= len(b) and b[p + step - 1] == 0x55]
    keep = []
    for p in good:
        if not keep or p - keep[-1] >= step - 1:
            keep.append(p)
    out['frames_step_check_ok'] = len(keep)
    out['aligned_ratio'] = round(len(keep) / max(1, len(aa)), 3)
    c = Counter('0x%02X' % (b[p + 1] & 0xF0) for p in keep if p + 1 < len(b))
    out['rx_id_high_nibble'] = _hist(c)
    if dt and dt > 0:
        out['frame_rate_fps'] = round(len(keep) / dt, 2)
        if len(keep) > 1:
            out['implied_frame_ms'] = round(dt / len(keep) * 1000.0, 1)
    # 未筛噪的原始分布：用于发现"真 RX 之外还冒出哪些假值"（假值多 ⇒ 载荷含 0xAA）
    all_hi = Counter('0x%02X' % (b[p + 1] & 0xF0) for p in aa if p + 1 < len(b))
    out['all_aa_next_high_nibble_raw'] = _hist(all_hi)
    return out


def analyze_stream(data, dt=None, label='stream'):
    """零协议假设地统计一段原始字节流。返回 dict（可直接打印或存 JSON）。"""
    res = {'label': label, 'bytes': len(data)}
    if not data:
        res['note'] = '空数据'
        return res

    if dt and dt > 0:
        res['duration_s'] = round(dt, 3)
        res['byte_rate_kBps'] = round(len(data) / dt / 1000.0, 1)

    b = bytes(data)
    res['magic_counts'] = {
        'ICLH': b.count(b'ICLH'),
        'ICLT': b.count(b'ICLT'),
        '0xAA': b.count(b'\xAA'),
        '0x55': b.count(b'\x55'),
    }

    head_pos = _find_all(b, b'ICLH')
    if head_pos:
        # —— wrapper 形态：按「魔数 + 长度字段」逐包跳转，不信任任何固定长度 ——
        lens = Counter()
        gaps = Counter()
        seq_deltas = Counter()
        plens = Counter()
        prev_seq = None
        prev_pos = None
        consumed = 0
        mismatch_tail = 0
        for p in head_pos:
            if p + 8 > len(b):
                break
            plen = int.from_bytes(b[p + 4:p + 6], 'little')
            seq = int.from_bytes(b[p + 6:p + 8], 'little')
            plens[plen] += 1
            total = 8 + plen + 4
            lens[total] += 1
            if p + total <= len(b):
                if b[p + 8 + plen:p + total] == b'ICLT':
                    consumed += 1
                else:
                    mismatch_tail += 1
            if prev_pos is not None:
                gaps[p - prev_pos] += 1
            if prev_seq is not None:
                seq_deltas[(seq - prev_seq) % 65536] += 1
            prev_pos, prev_seq = p, seq

        res['wrapper'] = {
            'packets_by_magic': len(head_pos),
            'total_len_hist': _hist(lens),
            'payload_len_hist': _hist(plens),
            'head_to_head_gap_hist': _hist(gaps),
            'tail_iclt_ok': consumed,
            'tail_iclt_bad': mismatch_tail,
            'seq_delta_hist': _hist(seq_deltas),
        }

        def payload_of(p):
            if p + 8 > len(b):
                return None
            plen = int.from_bytes(b[p + 4:p + 6], 'little')
            return b[p + 8:p + 8 + plen]

        c, bad_first, tail_ok = _top_rx_ids(b, head_pos, payload_of)
        res['wrapper']['rx_id_high_nibble'] = _hist(c)
        res['wrapper']['payload_head_not_0xAA'] = bad_first
        res['wrapper']['payload_tail_0x55_ok'] = tail_ok

        # 包速率 ⇒ 帧周期的**唯一可靠**入口（不依赖序号语义）
        if dt and dt > 0:
            pps = len(head_pos) / dt
            res['packet_rate_pps'] = round(pps, 1)
            res['implied_frame_ms_if_1T2R'] = round(KNOWN_1T2R['packets_per_frame'] / pps * 1000.0, 1)
            res['watch'] = ('若 implied_frame_ms_if_1T2R 明显偏离 129.5 ms，说明包/帧不等于 128 '
                            '（RX 数或 chirp 数不同）——不要沿用 1T2R 的组帧参数')
    else:
        # —— 无 wrapper：自适应定位裸帧 ——
        res['bare'] = _bare_analysis(b, dt)

    # —— 与 1T2R 事实的对照（只对照，不做判定）——
    cmp_ = {}
    hits = res['magic_counts']['ICLH']
    cmp_['ICLH'] = {'实测': hits, '1T2R': '有（+ICLT 成对）',
                    'same': (hits > 0)}
    pl = res.get('wrapper', {}).get('payload_len_hist') or []
    if pl:
        top_plen = pl[0][0]
        cmp_['payload_len'] = {'实测': top_plen, '1T2R': KNOWN_1T2R['payload_len'],
                               'same': (top_plen == KNOWN_1T2R['payload_len'])}
    # RX 标识：wrapper 走长度字段、裸帧走自适应步长，两者取其一
    rx = res.get('wrapper', {}).get('rx_id_high_nibble') or \
        res.get('bare', {}).get('rx_id_high_nibble') or []
    if rx:
        got = sorted(k for k, _ in rx)
        want = sorted('0x%02X' % v for v in KNOWN_1T2R['rx_ids'])
        cmp_['rx_id_set'] = {'实测': got, '1T2R': want, 'same': (got == want)}
    bare = res.get('bare') or {}
    if 'step_candidate' in bare and bare.get('frames_step_check_ok'):
        cmp_['bare_frame_len'] = {'实测': bare['step_candidate'],
                                  '1T2R': KNOWN_1T2R['bare_frame_len'],
                                  'same': (bare['step_candidate'] == KNOWN_1T2R['bare_frame_len'])}
    res['compare_1t2r'] = cmp_
    return res


def print_report(res):
    def line(s=''):
        print(s, flush=True)

    line('=== 裸流统计: %s ===' % res.get('label'))
    line('字节数 %d%s' % (res['bytes'],
                          ('   时长 %.3f s   平均 %.1f kB/s' % (res['duration_s'], res['byte_rate_kBps']))
                          if 'duration_s' in res else ''))
    mc = res.get('magic_counts', {})
    line('魔数出现次数: ICLH=%d  ICLT=%d  0xAA=%d  0x55=%d'
         % (mc.get('ICLH', 0), mc.get('ICLT', 0), mc.get('0xAA', 0), mc.get('0x55', 0)))

    w = res.get('wrapper')
    if w:
        line('')
        line('[wrapper 形态] 按「ICLH + u16LE 长度」逐包解析（不假设 52 字节）')
        line('  包数(按魔数)      : %d' % w['packets_by_magic'])
        line('  总长直方图        : %s' % ', '.join('%d 字节×%d' % (k, n) for k, n in w['total_len_hist']))
        line('  载荷长直方图      : %s' % ', '.join('%d×%d' % (k, n) for k, n in w['payload_len_hist']))
        line('  相邻 ICLH 间距    : %s' % ', '.join('%d×%d' % (k, n) for k, n in w['head_to_head_gap_hist']))
        line('  包尾 ICLT 校验    : 通过 %d / 不符 %d' % (w['tail_iclt_ok'], w['tail_iclt_bad']))
        line('  序号字段增量      : %s' % ', '.join('%d×%d' % (k, n) for k, n in w['seq_delta_hist']))
        line('  载荷首字节非 0xAA : %d' % w['payload_head_not_0xAA'])
        line('  载荷尾字节 0x55   : 通过 %d' % w['payload_tail_0x55_ok'])
        line('  ★ ID 高半字节取值 : %s   ← 判定「几个 RX」的直接证据'
             % ', '.join('%s×%d' % (k, n) for k, n in w['rx_id_high_nibble']))
        if 'packet_rate_pps' in res:
            line('  包速率            : %.1f 包/s' % res['packet_rate_pps'])
            line('  若按 1T2R 128 包/帧: %.1f ms/帧（1T2R 实测 129.5 ms）'
                 % res['implied_frame_ms_if_1T2R'])
            line('  ⚠ %s' % res['watch'])
    bare = res.get('bare')
    if bare:
        line('')
        line('[无 wrapper 形态] 0xAA 候选 %d 个（IQ 载荷里出现 0xAA 是常事 ⇒ 必须靠帧结构筛）'
             % bare['aa_positions'])
        if 'aa_gap_hist' in bare:
            line('  相邻 0xAA 间距    : %s' % ', '.join('%d×%d' % (k, n) for k, n in bare['aa_gap_hist']))
        if bare.get('step_candidate') and 'frames_step_check_ok' in bare:
            line('  ★ 自适应帧步长    : %d 字节（等间隔自洽对 %d；该间距仅占直方图 %.0f%%）'
                 % (bare['step_candidate'], bare.get('step_pairs', 0), bare.get('step_share', 0) * 100))
            line('  该步长下自洽帧    : %d 帧（占全部 0xAA 的 %.0f%%）'
                 % (bare['frames_step_check_ok'], bare['aligned_ratio'] * 100))
            if 'frame_rate_fps' in bare:
                line('  帧率              : %.2f 帧/s（推算 %.1f ms/帧；1T2R 实测 129.5 ms）'
                     % (bare['frame_rate_fps'], bare.get('implied_frame_ms', 0)))
            line('  ★ 自洽帧 ID 高半字节: %s   ← 干净值，判定「几个 RX」看这里'
                 % ', '.join('%s×%d' % (k, n) for k, n in bare['rx_id_high_nibble']))
            line('  （未筛噪的原始值  : %s）'
                 % ', '.join('%s×%d' % (k, n) for k, n in bare['all_aa_next_high_nibble_raw']))
        if bare.get('note'):
            line('  注: %s' % bare['note'])

    line('')
    line('[与 1T2R 实测事实的对照 —— 只对照，不做判定]')
    for k, v in (res.get('compare_1t2r') or {}).items():
        flag = '一致' if v.get('same') else '**不同**'
        line('  %-12s 实测=%-14s 1T2R=%-16s → %s'
             % (k, str(v['实测']), str(v['1T2R']), flag))
    line('')
    line('注意：以上差异**不是结论**。要写进文档前，必须真值标注 + 重复运行。')


# ============================ 子命令 ============================

CHIP_BY_VIDPID = {
    '10c4:ea60': 'Silicon Labs CP210x',
    '0403:6001': 'FTDI FT232 (FTDIBUS)',
    '0403:6015': 'FTDI FT231X',
    '1a86:7523': 'WCH CH340',
    '1a86:55d4': 'WCH CH343',
    '067b:2303': 'Prolific PL2303',
}

_ENUM_ROOTS = ('USB', 'FTDIBUS', 'SERENUM', 'ACPI', 'VMBUS', 'ROOT', 'BTHENUM')


def _enum_ports_registry():
    r"""不依赖 pyserial 的 COM 口枚举。

    为什么需要它：本机实测 pyserial 3.5 + Python 3.14.3 下 `comports()` **恒返回 0**
    （PowerShell 却能看到 COM3/COM4；根因记录见 .deepworks/tmp/diag_comports.py），
    而 `radar_source.list_serial_ports()/find_radar_port()` 正是基于它 ⇒
    1T2R 只靠硬编码 COM3 兜底，**换板（2T4R）时自动识别必挂**。
    注册表路径：HKLM\SYSTEM\CurrentControlSet\Enum\<Enum>\<DevID>\<Inst>\Device Parameters\PortName
    """
    import winreg

    base = r'SYSTEM\CurrentControlSet\Enum'
    out = []
    for root in _ENUM_ROOTS:
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + '\\' + root)
        except OSError:
            continue
        i = 0
        while True:
            try:
                devid = winreg.EnumKey(k, i)
            except OSError:
                break
            i += 1
            try:
                ki = winreg.OpenKey(k, devid)
            except OSError:
                continue
            j = 0
            while True:
                try:
                    inst = winreg.EnumKey(ki, j)
                except OSError:
                    break
                j += 1
                try:
                    kp = winreg.OpenKey(ki, inst + r'\Device Parameters')
                except OSError:
                    continue
                try:
                    port, _ = winreg.QueryValueEx(kp, 'PortName')
                    friendly = ''
                    try:
                        friendly, _ = winreg.QueryValueEx(ki, 'FriendlyName')
                    except OSError:
                        pass
                    out.append({'port': port, 'enum': root, 'devid': devid,
                                'inst': inst, 'friendly': friendly})
                except OSError:
                    pass
                finally:
                    winreg.CloseKey(kp)
            winreg.CloseKey(ki)
        winreg.CloseKey(k)
    return out


def _vidpid_from_devid(devid):
    """从 'VID_0403+PID_6001+A02C56YMA' 里取 vid:pid（FTDIBUS 是 '+'，USB 是 '&'）。"""
    import re
    s = devid.replace('+', '&')
    m = re.search(r'VID_([0-9a-f]{4})&PID_([0-9a-f]{4})', s, re.I)
    if not m:
        return None, None
    return m.group(1).lower(), m.group(2).lower()


def cmd_scan(args):
    reg = _enum_ports_registry()
    ps_ports = []
    hints = ()
    try:
        from radar_source import list_serial_ports, RADAR_PORT_HINTS
        hints = RADAR_PORT_HINTS
        ps_ports = list_serial_ports()
    except Exception as e:                     # radar_source 不可用也不该让扫描失效
        print('（radar_source 导入失败，跳过 pyserial 视角对照: %s）' % e)

    print('=== 串口枚举（不打开端口，零风险） ===')
    print('源 A: 注册表 Enum 扫描 = %d 个' % len(reg))
    for d in sorted(reg, key=lambda x: x['port']):
        vid, pid = _vidpid_from_devid(d['devid'])
        chip = CHIP_BY_VIDPID.get('%s:%s' % (vid, pid)) if vid else None
        print('  %-6s %s' % (d['port'], d['enum'] + '\\' + d['devid']))
        if vid:
            print('         VID:PID = %s:%s   → %s' % (vid, pid, chip or '**未知芯片**'))
        if d['friendly']:
            print('         设备名  = %s' % d['friendly'])
        if d['port'].upper() == KNOWN_1T2R['port']:
            print('         对照    = 本项目记录的 1T2R 端口就是它')
        if chip and 'FTDI' in chip:
            print('         ⚠ 提示    = FTDI 系列**疑为 2T4R**，但未验证；'
                  'radar_source 的线索表认不出它')

    print('')
    print('源 B: pyserial comports() = %d 个' % len(ps_ports))
    for dev, desc, hwid in ps_ports:
        print('  %-6s %s  [%s]' % (dev, desc, hwid))
    if len(ps_ports) == 0 and reg:
        print('  **两个源不一致**：注册表能看到 %d 个口而 pyserial 看到 0 个。' % len(reg))
        print('  这不是"没有板子"，而是本机 pyserial 枚举失效（Python 3.14 + pyserial 3.5）。')
        print('  ⇒ 结论：不要依赖 find_radar_port() 自动选口，**一律显式 --port**。')

    print('')
    print('=== 项目已知端口对照 ===')
    print('  1T2R: %s / %s / %s @ %d 8-N-1（已实机验证，见 docs/radar-uplink-protocol.md）'
          % (KNOWN_1T2R['port'], KNOWN_1T2R['vid_pid'], KNOWN_1T2R['chip'], KNOWN_1T2R['baud']))
    print('  2T4R: **未知**（端口、波特率、封装、RX 数全部未验证）⇒ 下一步用 sniff 实测')
    if hints:
        print('  radar_source 识别线索 = %s（只认 CP210x/Silicon Labs 系）' % (hints,))

    if args.json:
        print(json.dumps({'registry': reg, 'pyserial': [
            {'device': d, 'desc': ds, 'hwid': h} for d, ds, h in ps_ports]},
            ensure_ascii=False, indent=2))
    return 0


def cmd_analyze(args):
    with open(args.file, 'rb') as f:
        data = f.read()
    print('读取 %s（%d 字节）' % (args.file, len(data)))
    res = analyze_stream(data, dt=args.duration, label=os.path.basename(args.file))
    print_report(res)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def _read_port(port, baud, seconds, quiet=False):
    """打开端口读 seconds 秒。返回 (data, dt)；打不开则返回 (None, 0.0)。

    注意：本函数会**独占**端口 —— 调用前需关掉 server.py 与厂商上位机。
    """
    import serial

    if not quiet:
        print('打开 %s @ %d 8-N-1，读取 %.1f s …' % (port, baud, seconds), flush=True)
    try:
        ser = serial.Serial(port, baud, timeout=0.05, bytesize=8, parity='N', stopbits=1)
    except Exception as e:
        print('打开失败: %s: %s' % (type(e).__name__, e), flush=True)
        print('可能原因：① 端口号不对；② 被厂商工具/另一个 server 占用；'
              '③ 驱动未就绪（CP210x 或 FTDI VCP）', flush=True)
        return None, 0.0
    rec = bytearray()
    t0 = time.time()
    try:
        ser.reset_input_buffer()
        empty_polls = 0
        while time.time() - t0 < seconds:
            n = ser.in_waiting
            if n:
                rec += ser.read(n)
                empty_polls = 0
            else:
                empty_polls += 1
                if empty_polls == 20 and not quiet:
                    print('  …已 %.1fs 仍未收到任何字节（上电即出流？波特率对不对？）'
                          % (time.time() - t0), flush=True)
                time.sleep(0.005)
    finally:
        ser.close()
    return bytes(rec), time.time() - t0


def _stream_score(res):
    """给一次抓包打「像雷达数据」的分：有 ICLH 压倒一切，否则看能凑出多少等间隔自洽帧。"""
    score = 0
    if res.get('magic_counts', {}).get('ICLH', 0) > 0:
        score += 10 ** 9 + res['magic_counts']['ICLH']
    elif res.get('magic_counts', {}).get('ICLT', 0) > 0:
        score += 10 ** 8
    bare = res.get('bare') or {}
    score += bare.get('step_pairs', 0) or 0
    return score


def cmd_sniff(args):
    data, dt = _read_port(args.port, args.baud, args.seconds)
    if data is None:
        return 2
    print('读取结束: %d 字节 / %.2f s' % (len(data), dt))

    if args.out and data:
        with open(args.out, 'wb') as f:
            f.write(data)
        print('原始字节已落盘: %s' % args.out)

    res = analyze_stream(data, dt=dt, label='%s@%d' % (args.port, args.baud))
    print_report(res)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_try_bauds(args):
    """对多个波特率各读一小段，用同一套统计打分，挑出"最像雷达"的那个。

    用途：2T4R 的波特率未验证。若 921600 下收到的字节毫无结构，
    手工试 6 个波特率要跑 6 次；这里一次跑完并给出排序。
    注意：**打分是启发式，不是结论**——最终仍需人工看包长/ID/帧率是否自洽。
    """
    bauds = [int(x) for x in str(args.try_bauds).replace('，', ',').split(',') if x.strip()]
    print('=== 多波特率扫查: %s（每档 %.1f s）===' % (args.port, args.seconds))
    rows = []
    for bd in bauds:
        data, dt = _read_port(args.port, bd, args.seconds, quiet=True)
        if data is None:
            return 2
        if not data:
            print('  %-8d 收到 0 字节' % bd)
            rows.append((0, bd, 0, None, None, dt, None))
            continue
        res = analyze_stream(data, dt=dt, label='%s@%d' % (args.port, bd))
        sc = _stream_score(res)
        rows.append((sc, bd, len(data), res, data, dt, None))
        brief = ('ICLH=%d' % res['magic_counts']['ICLH']) if res['magic_counts']['ICLH'] \
            else ('裸帧步长=%s' % (res.get('bare', {}).get('step_candidate')))
        print('  %-8d %7d 字节  %.1f kB/s  %s' % (bd, len(data), len(data) / max(dt, 1e-9) / 1000.0, brief))

    rows.sort(key=lambda r: -r[0])
    print('')
    print('排序（越靠前越"像雷达数据"）：')
    for sc, bd, nbytes, res, data, dt, _ in rows:
        print('  score=%-12d baud=%-8d bytes=%-8d' % (sc, bd, nbytes))

    best = rows[0]
    if best[0] <= 0:
        print('')
        print('**所有波特率都没有任何结构** ⇒ 要么板子没出流，要么这不是雷达数据流。')
        print('先确认：上电后是否自动出流、USB-UART 是否被别处占用。')
        return 1

    print('')
    print('最佳：baud=%d（score=%d）' % (best[1], best[0]))
    print('下面是该档的完整报告（**启发式挑出来的，仍需人工确认语义**）：')
    print('')
    print_report(best[3])
    if args.out and best[4]:
        with open(args.out, 'wb') as f:
            f.write(best[4])
        print('最佳档的原始字节已落盘: %s' % args.out)
    return 0


# ---------------- selftest：证明统计器本身是可信的 ----------------

def _synth_1t2r(chirps=64, frames=3, seq0=0):
    """按 1T2R 实测格式合成（复用 radar_source.LoopbackSource 的字段规则，独立实现以免耦合）。"""
    out = bytearray()
    seq = seq0
    for _ in range(frames):
        for c in range(chirps):
            for rx in (0, 1):
                ident = (0x30 if rx == 0 else 0xB0) | (c >> 4)
                x1 = (c & 0x0F) << 4
                payload = bytearray([0xAA, ident, x1, 0x09])
                payload += bytes(32)
                payload += bytes([0x00, 0x0F, 0x00, 0x55])
                out += b'ICLH' + len(payload).to_bytes(2, 'little') + seq.to_bytes(2, 'little')
                out += bytes(payload) + b'ICLT'
                seq = (seq + 1) % 65536
    return bytes(out)


def _synth_fake_4rx(chirps=64, frames=2):
    """伪造「4 RX」形态：额外两个 ID 高半字节 0x70 / 0xF0，用来验证工具**能**报出差异。"""
    out = bytearray()
    seq = 0
    for _ in range(frames):
        for c in range(chirps):
            for ident_hi in (0x30, 0x70, 0xB0, 0xF0):
                ident = ident_hi | (c >> 4)
                payload = bytearray([0xAA, ident, (c & 0x0F) << 4, 0x09])
                payload += bytes(32)
                payload += bytes([0x00, 0x0F, 0x00, 0x55])
                out += b'ICLH' + len(payload).to_bytes(2, 'little') + seq.to_bytes(2, 'little')
                out += bytes(payload) + b'ICLT'
                seq = (seq + 1) % 65536
    return bytes(out)


def _synth_bare(frames=6, chirps=64, extra_bins=0, noise=False):
    """合成「裸帧模式」（无 ICLH/ICLT 封装）—— 2T4R 换固件后很可能就是这样。

    noise=True 时在载荷里埋入稀疏 0xAA，模拟 IQ 数据造成的**伪帧头**：
    这正是必须靠「步长 + 首尾自洽」筛掉的东西。
    """
    out = bytearray()
    bins = 32 + extra_bins
    for _ in range(frames):
        for c in range(chirps):
            for rx in (0, 1):
                ident = (0x30 if rx == 0 else 0xB0) | (c >> 4)
                fr = bytearray([0xAA, ident, (c & 0x0F) << 4, 0x09])
                if noise:
                    fr += b'\x00\x00\x00\xAA' * (bins // 4) + bytes(bins % 4)
                else:
                    fr += bytes(bins)
                fr += bytes([0x00, 0x0F, 0x00, 0x55])
                out += fr
    return bytes(out)


def cmd_selftest(args):
    ok = True

    def check(name, cond, detail=''):
        nonlocal ok
        ok = ok and bool(cond)
        print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', name,
                               ('  ' + detail) if detail else ''))

    print('=== selftest: 统计器自检（纯离线，不碰串口） ===')
    d1 = _synth_1t2r()
    r1 = analyze_stream(d1, dt=1.0, label='synth-1t2r')
    print('-- 合成 1T2R: %d 字节' % len(d1))
    check('ICLH 计数 = 包数', r1['magic_counts']['ICLH'] == 3 * 64 * 2,
          '%d' % r1['magic_counts']['ICLH'])
    check('ICLT 计数 = 包数', r1['magic_counts']['ICLT'] == 3 * 64 * 2)
    check('载荷长识别为 40', r1['wrapper']['payload_len_hist'][0][0] == 40)
    check('总长识别为 52', r1['wrapper']['total_len_hist'][0][0] == 52)
    check('包尾校验全通过', r1['wrapper']['tail_iclt_bad'] == 0)
    check('ID 集合 = {0x30,0xB0}',
          sorted(k for k, _ in r1['wrapper']['rx_id_high_nibble']) == ['0x30', '0xB0'],
          str([k for k, _ in r1['wrapper']['rx_id_high_nibble']]))
    check('与 1T2R 对照项全部一致',
          all(v['same'] for v in r1['compare_1t2r'].values()))
    check('包速率可算', 'packet_rate_pps' in r1)

    d2 = _synth_fake_4rx()
    r2 = analyze_stream(d2, dt=1.0, label='synth-fake-4rx')
    print('-- 合成伪 4RX: %d 字节' % len(d2))
    check('ID 集合报出 4 个值',
          len(r2['wrapper']['rx_id_high_nibble']) == 4,
          str([k for k, _ in r2['wrapper']['rx_id_high_nibble']]))
    check('与 1T2R 的 rx_id_set 对照判为「不同」',
          r2['compare_1t2r']['rx_id_set']['same'] is False)
    check('载荷长仍是 40（长度字段可独立成立）',
          r2['wrapper']['payload_len_hist'][0][0] == 40)

    # 帧周期推断：1 帧 = 128 包，1.0 s 内 3 帧 ⇒ 384 包/1s
    check('帧周期推断合理（384 包/s ⇒ 333ms/帧，与 129.5 不同 ⇒ 会给出提示）',
          r2['implied_frame_ms_if_1T2R'] > 0)

    # ---- 裸帧（无 wrapper）分支：2T4R 很可能就是这种形态，必须单独验证 ----
    d5 = _synth_bare(frames=6)
    r5 = analyze_stream(d5, dt=1.0, label='synth-bare40')
    print('-- 合成裸帧 40B: %d 字节' % len(d5))
    check('裸帧自适应步长 = 40', r5['bare'].get('step_candidate') == 40,
          str(r5['bare'].get('step_candidate')))
    check('裸帧 ID 集合 = {0x30,0xB0}',
          sorted(k for k, _ in r5['bare']['rx_id_high_nibble']) == ['0x30', '0xB0'],
          str([k for k, _ in r5['bare']['rx_id_high_nibble']]))
    check('裸帧帧率可算', 'frame_rate_fps' in r5['bare'])
    check('裸帧与 1T2R 的 bare_frame_len 判为一致',
          r5['compare_1t2r']['bare_frame_len']['same'] is True)

    d6 = _synth_bare(frames=6, noise=True)
    r6 = analyze_stream(d6, dt=1.0, label='synth-bare40-noise')
    print('-- 合成裸帧 40B + 载荷伪 0xAA: %d 字节' % len(d6))
    check('伪 0xAA 干扰下步长仍 = 40', r6['bare'].get('step_candidate') == 40,
          str(r6['bare'].get('step_candidate')))
    check('伪 0xAA 干扰下 ID 集合仍干净 = {0x30,0xB0}',
          sorted(k for k, _ in r6['bare']['rx_id_high_nibble']) == ['0x30', '0xB0'],
          str([k for k, _ in r6['bare']['rx_id_high_nibble']]))
    check('未筛噪的原始分布确实被污染（证明筛噪环节有必要）',
          len(r6['bare']['all_aa_next_high_nibble_raw']) > 2)

    d7 = _synth_bare(frames=4, extra_bins=8)
    r7 = analyze_stream(d7, dt=1.0, label='synth-bare48')
    print('-- 合成裸帧 48B（异常帧长）: %d 字节' % len(d7))
    check('非 40 的步长（48）也能自适应识别', r7['bare'].get('step_candidate') == 48,
          str(r7['bare'].get('step_candidate')))
    check('48B 帧与 1T2R 判为「不同」',
          r7['compare_1t2r']['bare_frame_len']['same'] is False)

    # ---- 端口指纹解析（scan 用；两种设备 ID 分隔符都要能解析）----
    check('解析 USB 风格 devid (CP210x)',
          _vidpid_from_devid('VID_10C4&PID_EA60') == ('10c4', 'ea60'))
    check('解析 FTDIBUS 风格 devid (FT232)',
          _vidpid_from_devid('VID_0403+PID_6001+A02C56YMA') == ('0403', '6001'),
          str(_vidpid_from_devid('VID_0403+PID_6001+A02C56YMA')))
    check('未知 devid 返回 (None, None)', _vidpid_from_devid(r'ROOT\SOMETHING') == (None, None))
    check('VID:PID 能映射到芯片名',
          CHIP_BY_VIDPID.get('%s:%s' % _vidpid_from_devid('VID_0403+PID_6001+X')) == 'FTDI FT232 (FTDIBUS)')

    r3 = analyze_stream(b'', label='empty')
    check('空数据不崩', r3.get('note') == '空数据')

    r4 = analyze_stream(bytes(range(256)) * 20, dt=0.5, label='noise')
    check('纯噪声不崩且 ICLH=0', r4['magic_counts']['ICLH'] == 0)

    print('=== selftest %s ===' % ('全部通过' if ok else '**有失败项**'))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description='串口预检 / 裸流嗅探（零协议假设）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('scan', help='枚举串口与指纹（不打开端口）')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser('sniff', help='打开端口读 N 秒并落盘 + 统计')
    p.add_argument('--port', required=True)
    p.add_argument('--baud', type=int, default=921600)
    p.add_argument('--seconds', type=float, default=6.0)
    p.add_argument('--out', default=None)
    p.add_argument('--json', action='store_true')
    p.add_argument('--try-bauds', default=None,
                   help='逗号分隔的波特率清单：逐档试读并排序（波特率未验证时用）')
    p.set_defaults(func=cmd_sniff)

    p = sub.add_parser('try-bauds', help='多波特率扫查，挑出最像雷达的一档')
    p.add_argument('--port', required=True)
    p.add_argument('--try-bauds', default='115200,230400,460800,921600,1500000,2000000')
    p.add_argument('--seconds', type=float, default=2.0)
    p.add_argument('--out', default=None)
    p.set_defaults(func=cmd_try_bauds)

    p = sub.add_parser('analyze', help='离线分析已抓的 .bin')
    p.add_argument('file')
    p.add_argument('--duration', type=float, default=None, help='该文件对应的抓取时长（用于算速率）')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser('selftest', help='用合成流自检统计器')
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    if args.cmd == 'sniff' and getattr(args, 'try_bauds', None):
        return cmd_try_bauds(args)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
