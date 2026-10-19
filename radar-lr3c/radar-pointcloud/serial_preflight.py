# -*- coding: utf-8 -*-
r"""
串口预检 / 裸流嗅探 —— 换板（尤其 2T4R）前的**零协议假设**准备工具。

设计原则（为什么不用现成的 probe.py）：
  * probe.py / server.py 走的是**已冻结的 1T2R 解析链**（`radar_protocol.py` 里
    `CHIRPS_PER_FRAME = 64`、RX ID 只有 `0x30 / 0xB0`、组帧固定 `(2, chirps, bins)`）。
    换一块板若 RX 数或帧长不同，那条链**一个包都认不出**，而且不会告诉你为什么。
  * 本工具**不假设任何协议**：只统计字节流的客观特征（魔数出现次数、包长字段、
    RX/ID 取值集合、包速率），并把「认不出的部分」如实标成未知。
  * 已冻结的 2T4R（DEV113P）136 B 帧格式与厂商命令模式已内置（`KNOWN_2T4R` /
    `DOC_FRAMES`）：`analyze` **优先**用三重判据（`55 AA` + `off4 == 0x1F` + 帧尾
    校验和）核对，命中就逐条比对实测事实；不命中才退回零假设探索。命中只证明
    「字节流符合已冻结契约」，**不等于**链路可用于游戏（那是 M0/M1 的事）。
    ⇒ 拿到 2T4R 后，先跑它，再用**实测出来的参数**去改协议层。

子命令：
  scan                     枚举串口 + 硬件指纹 + 与项目已知端口对照（**不打开端口**，零风险）
  sniff  --port COM4       打开端口读 N 秒，落盘原始字节，并当场做同样的统计
  analyze <file.bin>       离线分析已有抓包（零风险，可反复跑）
  probe --port COM4        命令模式板型指纹（默认干跑；--send 才真发，会停波形流）
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

# ---- 2T4R 已确认的事实（来源: docs/radar-uplink-dev113p-2t4r.md §2/§7.6，真机实测）----
# 用途：换板时**不再靠猜**——直接按已冻结的事实做契约自检（见 analyze_dev113p）。
KNOWN_2T4R = {
    'port': 'COM4',
    'vid_pid': 'VID_0403 PID_6001',
    'chip': 'FTDI FT232',
    'baud': 1500000,
    'wrapper': None,
    'frame_len': 136,
    'sync': (0x55, 0xAA),
    'bins': 32,
    'points_minus_one': 0x1F,
    'rx_per_chirp': 4,
    'chirps_per_frame': 64,
    'packets_per_frame': 256,
    'checksum_off': 133,
    'tag_off': 135,
    'rx_tag_shift': 2,
    'frame_tag_shift': 4,
    'pkts_per_s': 482.0,          # 真机实测（3908 帧 / 8.105 s）
    'radar_fps': 1.88,            # = 482 / 256（配置推导的帧周期 520.8 ms 对应 1.92）
    'frame_ms': 531.0,            # = 1000 / 1.88（实测口径；配置口径 520.8 ms）
}

# ---- 串口命令模式（来源: 厂商协议文档 V0.17 中文版，字节串取自文档"示例"节）----
# 帧格式 / 命令码 / 结果码已由厂商上位机日志的真机字节证实（10/19）；唯一与文档不一致处是
#   「打开命令模式」的 payload：文档示例 `01 00`，真机实测 `02 00`（=> MEASURED_OPEN_VER）。
#   用途：不依赖 USB-UART 芯片就能问出板型 —— 若厂商换 UART 芯片，指纹表会失效，命令模式不会。
CMD_HEAD = b'\xfd\xfc\xfb\xfa'
CMD_FOOT = b'\x04\x03\x02\x01'
CMD_READ_VERSION = 0x0000      # → 0x0100：结果 + (u16 长度 + ASCII 版本串)
CMD_READ_MODULE = 0x0005       # → 0x0105：结果 + 模组名字符串
CMD_FACTORY_CFG = 0x0024       # → 0x0124：结果 + 子板型号/级联芯片数/通道数/数据类型/1DFFT/chirp/降采样
CMD_OPEN_CMD_MODE = 0x00FF     # payload = u16 预期协议版本号；→ 0x01FF：结果 + 协议版本号 + 收发缓冲区大小
CMD_CLOSE_CMD_MODE = 0x00FE    # → 0x01FE：结果
DOC_OPEN_VER = 0x0001          # 文档示例里「打开命令模式」带的预期版本号

MEASURED_OPEN_VER = 0x0002     # ★ 真机实测值（10/19）：1T2R 与 2T4R 的厂商上位机
                               #   都发 payload `02 00`；文档示例写的是 `01 00`。
                               #   证据 = ONELAB 日志 Log\LogFile_*.txt 里
                               #   `OpenCmdMode_Send` 的原始 hex（见
                               #   docs/radar-uplink-dev113p-2t4r.md §7.7）。
                               #   `probe` 默认用它；复现文档口径传 --open-payload 1。
# 文档示例字节串（用于离线单测 codec，不碰串口）
DOC_FRAMES = {
    '读取版本 req': 'fd fc fb fa 02 00 00 00 04 03 02 01',
    '读取版本 resp': 'FD FC FB FA 0D 00 00 01 00 00 07 00 76 31 2E 34 2E 31 34 04 03 02 01',
    '打开命令模式 req': 'fd fc fb fa 04 00 ff 00 01 00 04 03 02 01',
    '打开命令模式 resp': 'fd fc fb fa 08 00 ff 01 00 00 02 00 00 04 04 03 02 01',
    '关闭命令模式 req': 'fd fc fb fa 02 00 fe 00 04 03 02 01',
    '关闭命令模式 resp': 'fd fc fb fa 04 00 fe 01 00 00 04 03 02 01',
    '进入厂测模式 req': 'fd fc fb fa 02 00 24 00 04 03 02 01',
    '进入厂测模式 resp': ('fd fc fb fa 12 00 24 01 00 00 00 00 02 00 04 00 00 00 '
                          '40 00 20 00 02 00 04 03 02 01'),
}


def build_cmd(cmd, payload=b''):
    r"""构造一个命令帧：`FD FC FB FA` + u16LE(数据长) + u16LE(命令) + payload + `04 03 02 01`。

    帧头在文档里写作 0xFAFBFCFD，但**线上顺序**是 `fd fc fb fa`（小端 u32）——
    这一点由文档示例逐字节核对确认（见 DOC_FRAMES / selftest）。
    """
    data = int(cmd).to_bytes(2, 'little') + bytes(payload)
    return CMD_HEAD + len(data).to_bytes(2, 'little') + data + CMD_FOOT


def parse_cmds(buf, with_offset=False):
    """从字节流里扫出所有**结构自洽**的命令响应帧（容错扫描，不假设对齐）。

    判据：`fd fc fb fa` + u16LE(L) + 恰好 L 字节 + `04 03 02 01`。
    返回 [{'cmd','payload','raw'}]；with_offset 时额外给 'off'。
    """
    b = bytes(buf)
    out = []
    i = b.find(CMD_HEAD)
    while i >= 0:
        if i + 6 + 4 <= len(b):
            ln = int.from_bytes(b[i + 4:i + 6], 'little')
            end = i + 6 + ln + 4
            if end <= len(b) and b[end - 4:end] == CMD_FOOT and ln >= 2:
                data = b[i + 6:end - 4]
                d = {'cmd': int.from_bytes(data[:2], 'little'),
                     'payload': data[2:],
                     'raw': b[i:end]}
                if with_offset:
                    d['off'] = i
                out.append(d)
                i = b.find(CMD_HEAD, end)
                continue
        i = b.find(CMD_HEAD, i + 1)
    return out


# 结果码（来源 = ONELAB 日志里的真机响应字节，10/19 两板实测）
RESULT_CODES = {
    0x0000: '成功',
    0x0001: '失败/不支持',
    0x0FA7: '不支持该命令',
    0x0FA8: '参数不支持/失败',
    0xA001: '打开命令模式失败',
}


def describe_result(code):
    """把 u16 结果码渲染成可读文本；未知码不吞掉，仍带原始十六进制。"""
    c = int(code) & 0xFFFF
    if c == 0:
        return '成功'
    return '失败 0x%04X(%d) %s' % (c, c, RESULT_CODES.get(c, '未知'))


def decode_cmd_reply(cmd, payload):
    """把命令响应的 payload 解成人类可读字段。字段顺序取自文档表；结果码取自真机实测。"""
    if len(payload) < 2:
        return {'结果': '**响应过短**'}
    code = int.from_bytes(payload[:2], 'little')
    r = {'结果': describe_result(code), '结果码': '0x%04X' % code}
    p = payload[2:]
    if cmd == 0x0100 and len(p) >= 2:                 # 读取版本
        n = int.from_bytes(p[:2], 'little')
        r['版本'] = p[2:2 + n].decode('ascii', 'replace')
        r['帧内数据长'] = n
    elif cmd == 0x01FF and len(p) >= 4:               # 打开命令模式
        r['协议版本号'] = int.from_bytes(p[:2], 'little')
        r['收发缓冲区大小'] = int.from_bytes(p[2:4], 'little')
    elif cmd == 0x0124 and len(p) >= 14:              # 厂测配置 / 板型指纹
        keys = ['子板型号', '级联芯片数', '接收通道数', '数据类型',
                '1DFFT大小', '每帧chirp数', '降采样间隔']
        dtypes = {0: '1DFFT', 1: '2DFFT', 2: '2DFFT PEAK', 3: 'DSRAW', 4: 'IORAW'}
        for k, i in zip(keys, range(0, 14, 2)):
            v = int.from_bytes(p[i:i + 2], 'little')
            r[k] = dtypes.get(v, v) if k == '数据类型' else v
    elif cmd in (0x0105, 0x0111, 0x0113) and p:
        r['字符串/原始'] = p.decode('ascii', 'replace').strip('\x00') or p.hex(' ')
    return r


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


def analyze_dev113p(data, dt=None):
    """按**已冻结的 2T4R 事实**做契约自检（不是零假设探索，而是逐条核对）。

    判据（全部来自真机实测，见 docs/radar-uplink-dev113p-2t4r.md §7.6）：
      * 帧起 `55 AA`、`off4 == 0x1F`、总长 136、相邻命中间距恒 136；
      * 校验和 `BE16[133:135] == sum(64 个 BE int16) mod 65536`；
      * 帧尾 `off135`：bits[3:2] = RX 序号（应为 0..3）、bits[7:4] = 雷达帧序号 mod 16；
      * 组装完整性：按 frame_seq 结算，统计整帧丢失 / 丢 chirp 组。

    这个函数让「换板预检」在 2T4R 之后仍然可用：故意把同一个固件的板子插到
    **另一个 UART 芯片**上时，指纹表可能失效，但仍能靠本函数确认「这确实是 DEV113P 流」。
    """
    b = bytes(data)
    fl = KNOWN_2T4R['frame_len']
    out = {'frame_len': fl, 'sync_candidates': b.count(b'\x55\xAA')}

    def cks(payload):
        v = 0
        for i in range(0, len(payload), 2):
            v += (payload[i] << 8) | payload[i + 1]
        return v & 0xFFFF

    # 三重判据（同步字 + off4 + 校验和）后仍命中的才认定为帧
    frames, rejected = [], 0
    for p in _find_all(b, b'\x55\xAA'):
        if p + fl > len(b):
            continue
        if b[p + 4] != KNOWN_2T4R['points_minus_one']:
            rejected += 1
            continue
        if cks(b[p + 5:p + 133]) != ((b[p + 133] << 8) | b[p + 134]):
            rejected += 1
            continue
        frames.append(p)
    out['frames'] = len(frames)
    out['rejected_after_sync'] = rejected
    out['looks_like_dev113p'] = False
    if not frames:
        out['note'] = ('没有一帧同时满足「55 AA + off4=0x1F + 校验和」⇒ 不是 DEV113P 136B 流'
                       '（或帧被截断/波特率不对）')
        return out

    out['looks_like_dev113p'] = True
    out['first_frame_off'] = frames[0]
    out['tail_bytes'] = len(b) - frames[-1] - fl
    gaps = Counter(frames[i + 1] - frames[i] for i in range(len(frames) - 1))
    out['frame_gap_hist'] = _hist(gaps, 6)
    out['gap_eq_136'] = sum(v for k, v in gaps.items() if k == fl)
    out['gap_eq_136_ratio'] = round((sum(v for k, v in gaps.items() if k == fl)
                                     / max(1, len(frames) - 1)), 4)

    rx = Counter((b[p + KNOWN_2T4R['tag_off']] >> KNOWN_2T4R['rx_tag_shift']) & 0x03
                 for p in frames)
    fseq = Counter((b[p + KNOWN_2T4R['tag_off']] >> KNOWN_2T4R['frame_tag_shift']) & 0x0F
                   for p in frames)
    out['rx_tag_hist'] = _hist(rx, 8)
    out['rx_tag_set'] = sorted(k for k, _ in rx.items())
    out['frame_seq_distinct'] = len(fseq)

    # 按 chirp 分组（同 counter 的 4 帧），核对「每组恰 4 帧 且 RX 依次 0,1,2,3」
    groups, runs = [], []
    for p in frames:
        key = b[p + 2:p + 4]
        if groups and groups[-1][0] == key:
            groups[-1][1].append(p)
        else:
            groups.append((key, [p]))
    for key, ps in groups:
        runs.append({'n': len(ps),
                     'rx': [(b[p + KNOWN_2T4R['tag_off']] >> KNOWN_2T4R['rx_tag_shift']) & 0x03
                            for p in ps]})
    out['chirp_groups'] = len(groups)
    out['groups_of_4'] = sum(1 for g in runs if g['n'] == 4)
    out['groups_rx_ordered'] = sum(1 for g in runs if g['rx'] == [0, 1, 2, 3])
    out['groups_other'] = [(g['n'], g['rx']) for g in runs if g['n'] != 4 or g['rx'] != [0, 1, 2, 3]][:4]

    # 组装完整性（按 frame_seq 结算；逻辑与 radar_protocol_dev113p 同源，此处独立实现）
    settled = complete = incomplete = lost = 0
    cur, seen = None, set()
    for p in frames:
        fs = (b[p + KNOWN_2T4R['tag_off']] >> KNOWN_2T4R['frame_tag_shift']) & 0x0F
        if cur is None:
            cur = fs          # 首包只用来初始化 frame_seq；**不能 continue**
        elif fs != cur:       # 否则首包不进 seen，首帧永远被判「不完整」
            settled += 1
            if len(seen) == KNOWN_2T4R['chirps_per_frame'] * KNOWN_2T4R['rx_per_chirp']:
                complete += 1
            else:
                incomplete += 1
            if ((fs - cur) & 0x0F) != 1:
                lost += 1
            cur, seen = fs, set()
        seen.add((b[p + 2:p + 4],
                  (b[p + KNOWN_2T4R['tag_off']] >> KNOWN_2T4R['rx_tag_shift']) & 0x03))
    out['assembled_complete'] = complete
    out['assembled_incomplete'] = incomplete
    out['settled_frames'] = settled
    out['frames_lost_whole'] = lost
    if dt and dt > 0:
        out['packet_rate_pps'] = round(len(frames) / dt, 1)
        out['radar_fps'] = round(len(frames) / dt / KNOWN_2T4R['packets_per_frame'], 3)
        out['ref_2t4r_pps'] = KNOWN_2T4R['pkts_per_s']
    return out


def analyze_stream(data, dt=None, label='stream'):
    """零协议假设地统计一段原始字节流；若命中已知格式，再附上该格式的契约自检。

    两段式的原因：**换板**时要的是「零假设探索」（下面这部分），
    **已破解的板**再插回来时要的是「逐条对照冻结事实」（analyze_dev113p）。
    只做前者会让人重新推一遍协议，只做后者会在换板时彻底无用。
    """
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
    # 已知格式优先：DEV113P 无 wrapper，若字节流里碰巧出现 'ICLH'（载荷巧合）
    # 会走错分支，所以先用「同步字 + off4 + 校验和」三重判据确认一次。
    dv = analyze_dev113p(b, dt)
    if dv.get('looks_like_dev113p'):
        res['dev113p'] = dv
    elif head_pos:
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
        # —— 无 wrapper 且不是已知格式：退回零假设探索 ——
        res['bare'] = _bare_analysis(b, dt)
        res['dev113p_probe'] = {k: dv[k] for k in
                               ('sync_candidates', 'frames', 'rejected_after_sync', 'note')
                               if k in dv}

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
    dv = res.get('dev113p')
    if dv:
        line('')
        line('[已知格式命中：DEV113P / 2T4R 136 B 帧 —— 逐条核对冻结事实]')
        line('  帧数(三重判据)    : %d（同步字候选 %d，判据不全被否 %d）'
             % (dv['frames'], dv['sync_candidates'], dv['rejected_after_sync']))
        line('  首帧偏移 / 尾部残 : %d B / %d B' % (dv['first_frame_off'], dv['tail_bytes']))
        line('  相邻帧间距 136 占比: %.1f%%（%d/%d）'
             % (dv['gap_eq_136_ratio'] * 100, dv['gap_eq_136'], max(1, dv['frames'] - 1)))
        line('  ★ 校验和           : 全部通过（三重判据含校验和，未通过者已被否）')
        line('  ★ RX 标签取值集     : %s（期望 [0, 1, 2, 3]）'
             % ', '.join('%d×%d' % (k, n) for k, n in dv['rx_tag_hist']))
        line('  chirp 组数/恰 4 帧  : %d / %d；RX 依次 0,1,2,3 的组 %d'
             % (dv['chirp_groups'], dv['groups_of_4'], dv['groups_rx_ordered']))
        if dv['groups_other']:
            line('  例外的组           : %s（若集中在抓包首尾 ⇒ 只是截断，不是丢帧）'
                 % (dv['groups_other'],))
        line('  雷达帧序号 distinct : %d（期望 16）' % dv['frame_seq_distinct'])
        line('  组装              : 完整 %d / 不完整 %d / 整帧丢失 %d（结算 %d 次）'
             % (dv['assembled_complete'], dv['assembled_incomplete'],
                dv['frames_lost_whole'], dv['settled_frames']))
        if 'packet_rate_pps' in dv:
            line('  包速率 / 雷达帧率   : %.1f 包/s → %.2f 雷达帧/s（2T4R 实测参考 %.0f 包/s）'
                 % (dv['packet_rate_pps'], dv['radar_fps'], dv['ref_2t4r_pps']))
    dp = res.get('dev113p_probe')
    if dp:
        line('')
        line('[DEV113P 格式核对：未命中] 同步字候选 %d，三重判据命中 %d，判据不全 %d'
             % (dp.get('sync_candidates', 0), dp.get('frames', 0),
                dp.get('rejected_after_sync', 0)))
        if dp.get('note'):
            line('  注: %s' % dp['note'])
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


def _synth_dev113p(frames=3, chirps=64, rx=4, drop_at=None, shift_seq=0):
    """按**实测冻结格式**合成 DEV113P 流（独立实现，不复用 radar_protocol_dev113p，
    这样 selftest 验证的是「预检工具自己认识这个格式」，而不是「模块自洽」）。

    off2 = 0x80|(chirp>>4)、off3 = (chirp&0xF)<<4、off4 = 0x1F、
    payload = 32 bin × (I,Q) 大端 int16、off133:135 = 大端校验和、
    off135 = (帧序号 mod 16)<<4 | (RX<<2)。
    drop_at = (帧序, chirp, rx) 时故意漏发那一包，用来验证「丢帧能被检出」。
    """
    out = bytearray()
    for f in range(frames):
        fs = (f + shift_seq) & 0x0F
        for c in range(chirps):
            for r in range(rx):
                if drop_at and (f, c, r) == drop_at:
                    continue
                iq = bytearray()
                for b in range(32):
                    v = (r + 1) * 100 + b          # 任意确定性载荷
                    iq += (v & 0xFFFF).to_bytes(2, 'big') + b'\x00\x00'
                ck = 0
                for i in range(0, 128, 2):
                    ck += (iq[i] << 8) | iq[i + 1]
                out += bytes([0x55, 0xAA, 0x80 | (c >> 4), (c & 0x0F) << 4, 0x1F])
                out += bytes(iq)
                out += (ck & 0xFFFF).to_bytes(2, 'big')
                out += bytes([(fs << 4) | (r << 2)])
    return bytes(out)


def _read_until_frames(ser, want=1, timeout=0.8):
    """读到凑够 want 个自洽命令帧或超时，返回原始字节。"""
    rec = bytearray()
    t0 = time.time()
    while time.time() - t0 < timeout:
        n = ser.in_waiting
        if n:
            rec += ser.read(n)
            if len(parse_cmds(rec)) >= want:
                break
        else:
            time.sleep(0.01)
    return bytes(rec)


def cmd_probe(args):
    """板型指纹（命令模式）——**默认干跑**，加 --send 才真正发字节。

    为什么需要它：现有两条识别路径（USB VID:PID 指纹、裸流形态）都依赖
    「厂商用哪一种 UART 芯片 / 是否自动出流」。若现场换了线或换了 UART 芯片，
    两条都会失效；命令模式是直接问设备本身，最稳。
    ⚠ 代价：命令模式会**停掉波形数据流**。命令码与响应字段已由厂商上位机日志的
    真机字节证实（10/19），但**本工具自身尚未在真机上跑过**（只在干跑/合成流上验证），
    所以默认干跑，并把「预期响应」按文档列出。
    """
    # 真机实测 `02 00` 为默认；--open-payload 可回到文档示例或试别的版本号
    open_pl = int(MEASURED_OPEN_VER if args.open_payload is None
                  else args.open_payload).to_bytes(2, 'little')

    seq = [('打开命令模式', CMD_OPEN_CMD_MODE, open_pl),
           ('读取版本', CMD_READ_VERSION, b''),
           ('读取模组名', CMD_READ_MODULE, b''),
           ('获取厂测配置(板型指纹)', CMD_FACTORY_CFG, b''),
           ('关闭命令模式', CMD_CLOSE_CMD_MODE, b'')]

    print('=== 命令模式板型指纹（%s）===' % ('实发' if args.send else '干跑：不发任何字节'))
    print('端口 %s @ %d 8-N-1' % (args.port, args.baud))
    print('')
    for name, cmd, pl in seq:
        print('  %-24s cmd=0x%04X payload=%-9s → %s'
              % (name, cmd, (pl.hex(' ') or '无'), build_cmd(cmd, pl).hex(' ')))
    print('')
    print('文档示例（核对 codec 与字段顺序用；数值是文档示例，不是本机实测）：')
    for k, v in DOC_FRAMES.items():
        print('  %-18s %s' % (k, v))
    print('')
    if not args.send:
        print('**这是干跑**：未打开端口、未发送任何字节。')
        print('要真发（会停掉波形流；需先关掉 server.py 与厂商上位机）：')
        print('  python serial_preflight.py probe --port COM4 --send')
        return 0

    import serial
    print('⚠ 即将切到命令模式，波形数据会停止；结束时会发「关闭命令模式」恢复。')
    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.05,
                            bytesize=8, parity='N', stopbits=1)
    except Exception as e:
        print('打开失败: %s: %s' % (type(e).__name__, e))
        return 2
    try:
        # 文档三步走：① 发一次（回显不可信，不解析）② delay 清缓存 ③ 再发一次并解析
        ser.reset_input_buffer()
        ser.write(build_cmd(CMD_OPEN_CMD_MODE, open_pl))
        ser.flush()
        time.sleep(0.15)
        ser.reset_input_buffer()
        ser.write(build_cmd(CMD_OPEN_CMD_MODE, open_pl))
        ser.flush()
        buf = _read_until_frames(ser, want=1, timeout=args.seconds)
        reps = parse_cmds(buf)
        if reps:
            for d in reps:
                print('  [0x%04X] %s' % (d['cmd'], decode_cmd_reply(d['cmd'], d['payload'])))
        else:
            print('  ⚠ 未收到结构自洽响应（原始 %d 字节）: %s'
                  % (len(buf), buf[:64].hex(' ')))
            print('  ⇒ 可能：该固件不支持这套命令模式 / 波特率不对 / 已不在波形流状态。')
        for name, cmd, pl in seq[1:-1]:
            ser.reset_input_buffer()
            ser.write(build_cmd(cmd, pl))
            ser.flush()
            got = _read_until_frames(ser, want=1, timeout=args.seconds)
            reps = parse_cmds(got)
            print('  %-24s cmd=0x%04X%s'
                  % (name, cmd, ('' if reps else '   → 原始 %s' % got[:48].hex(' '))))
            for d in reps:
                print('      0x%04X %s' % (d['cmd'], decode_cmd_reply(d['cmd'], d['payload'])))
    finally:
        try:
            ser.reset_input_buffer()
            ser.write(build_cmd(CMD_CLOSE_CMD_MODE))
            ser.flush()
            time.sleep(0.15)
        except Exception as e:
            print('关闭命令模式失败: %s（建议重新上电）' % e)
        ser.close()
        print('已关闭端口（波形流应恢复）。')
    return 0



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

    # ---- DEV113P（2T4R）已知格式：契约自检必须能确认 ----
    print('-- 合成 DEV113P 136B: %d 字节' % len(_synth_dev113p(frames=3)))
    d8 = _synth_dev113p(frames=3)
    r8 = analyze_stream(d8, dt=1.0, label='synth-dev113p')
    dv8 = r8.get('dev113p') or {}
    check('命中已知格式 dev113p', bool(dv8))
    check('帧数 = 3×64×4', dv8.get('frames') == 3 * 64 * 4, str(dv8.get('frames')))
    check('RX 取值集 = 0..3', dv8.get('rx_tag_set') == [0, 1, 2, 3], str(dv8.get('rx_tag_set')))
    check('chirp 组数 = 192', dv8.get('chirp_groups') == 3 * 64, str(dv8.get('chirp_groups')))
    check('每组恰 4 帧', dv8.get('groups_of_4') == 3 * 64, str(dv8.get('groups_of_4')))
    check('RX 依次 0,1,2,3', dv8.get('groups_rx_ordered') == 3 * 64)
    check('相邻帧间距恒 136', dv8.get('gap_eq_136') == dv8.get('frames', 0) - 1,
          str(dv8.get('gap_eq_136')))
    check('组装出 2 个完整帧（末帧未结算）',
          dv8.get('assembled_complete') == 2 and dv8.get('assembled_incomplete') == 0,
          'complete=%s incomplete=%s' % (dv8.get('assembled_complete'),
                                         dv8.get('assembled_incomplete')))
    check('无整帧丢失', dv8.get('frames_lost_whole') == 0, str(dv8.get('frames_lost_whole')))

    print('-- 合成 DEV113P 漏掉 1 包（第 2 帧 chirp5 RX2）')
    dv9 = (analyze_stream(_synth_dev113p(frames=3, drop_at=(1, 5, 2)), dt=1.0,
                          label='synth-dev113p-drop').get('dev113p') or {})
    check('漏 1 包仍命中已知格式', bool(dv9))
    check('帧数 = 768-1', dv9.get('frames') == 3 * 64 * 4 - 1, str(dv9.get('frames')))
    check('不完整帧被检出 = 1', dv9.get('assembled_incomplete') == 1,
          str(dv9.get('assembled_incomplete')))
    check('完整帧仍为 1', dv9.get('assembled_complete') == 1, str(dv9.get('assembled_complete')))

    print('-- 合成 DEV113P 整帧丢失（帧序号 0 后直接跳到 2）')
    d10 = _synth_dev113p(frames=1, shift_seq=0) + _synth_dev113p(frames=2, shift_seq=2)
    dv10 = (analyze_stream(d10, dt=1.0, label='synth-dev113p-lost').get('dev113p') or {})
    check('整帧丢失被检出 = 1', dv10.get('frames_lost_whole') == 1,
          str(dv10.get('frames_lost_whole')))

    # ---- 命令模式 codec：与厂商文档示例**逐字节**一致 ----
    print('-- 命令模式 codec（对照厂商协议文档示例字节）')
    check('读取版本 req 与文档逐字节一致',
          build_cmd(CMD_READ_VERSION).hex(' ') == DOC_FRAMES['读取版本 req'],
          build_cmd(CMD_READ_VERSION).hex(' '))
    check('打开命令模式 req 与文档一致',
          build_cmd(CMD_OPEN_CMD_MODE, DOC_OPEN_VER.to_bytes(2, 'little')).hex(' ')
          == DOC_FRAMES['打开命令模式 req'])
    check('关闭命令模式 req 与文档一致',
          build_cmd(CMD_CLOSE_CMD_MODE).hex(' ') == DOC_FRAMES['关闭命令模式 req'])
    check('进入厂测模式 req 与文档一致',
          build_cmd(CMD_FACTORY_CFG).hex(' ') == DOC_FRAMES['进入厂测模式 req'])
    for k in ('读取版本 resp', '打开命令模式 resp', '关闭命令模式 resp', '进入厂测模式 resp'):
        bb = bytes.fromhex(DOC_FRAMES[k])
        rr = parse_cmds(bb)
        check('%s 可解析且 raw 精确' % k, len(rr) == 1 and rr[0]['raw'] == bb,
              'n=%d' % len(rr))
    rf = parse_cmds(bytes.fromhex(DOC_FRAMES['进入厂测模式 resp']))[0]
    dec = decode_cmd_reply(rf['cmd'], rf['payload'])
    check('厂测响应 cmd = 0x0124', rf['cmd'] == 0x0124, '0x%04X' % rf['cmd'])
    check('厂测响应：结果 = 成功', dec['结果'] == '成功', str(dec['结果']))
    check('厂测响应：级联芯片数 = 2（文档示例）', dec.get('级联芯片数') == 2)
    check('厂测响应：通道数 = 4（文档示例）', dec.get('接收通道数') == 4)
    check('厂测响应：1DFFT = 64（文档示例）', dec.get('1DFFT大小') == 64)
    check('厂测响应：每帧 chirp = 32（文档示例）', dec.get('每帧chirp数') == 32)
    check('厂测响应：数据类型 0 → 1DFFT', dec.get('数据类型') == '1DFFT', str(dec.get('数据类型')))
    rv = parse_cmds(bytes.fromhex(DOC_FRAMES['读取版本 resp']))[0]
    check('版本响应解出 v1.4.14',
          decode_cmd_reply(rv['cmd'], rv['payload']).get('版本') == 'v1.4.14',
          str(decode_cmd_reply(rv['cmd'], rv['payload'])))
    op = parse_cmds(bytes.fromhex(DOC_FRAMES['打开命令模式 resp']))[0]
    d2 = decode_cmd_reply(op['cmd'], op['payload'])
    check('打开命令模式响应：版本 = 2 / 缓冲区 = 1024',
          d2.get('协议版本号') == 2 and d2.get('收发缓冲区大小') == 1024, str(d2))
    pad = b'\x00\xff' * 5 + bytes.fromhex(DOC_FRAMES['读取版本 resp']) + b'\x55' * 3
    check('容错扫描：前后夹垃圾仍扫出 1 帧', len(parse_cmds(pad)) == 1)
    check('残帧（截尾 1 字节）不误报',
          parse_cmds(bytes.fromhex(DOC_FRAMES['读取版本 resp'])[:-1]) == [])

    print('-- 真机实测口径（10/19 厂商上位机日志）')
    check('实测 payload 02 00 → 帧字节正确',
          build_cmd(CMD_OPEN_CMD_MODE, MEASURED_OPEN_VER.to_bytes(2, 'little')).hex(' ')
          == 'fd fc fb fa 04 00 ff 00 02 00 04 03 02 01', build_cmd(CMD_OPEN_CMD_MODE, MEASURED_OPEN_VER.to_bytes(2, 'little')).hex(' '))
    check('文档示例 payload 01 00 仍可构造（--open-payload 1 走这条）',
          build_cmd(CMD_OPEN_CMD_MODE, DOC_OPEN_VER.to_bytes(2, 'little')).hex(' ')
          == DOC_FRAMES['打开命令模式 req'])
    check('结果码 0x0000 → 成功', describe_result(0) == '成功', describe_result(0))
    check('结果码 0x0001 → 失败/不支持且保留原始码',
          describe_result(1).startswith('失败') and '0x0001' in describe_result(1),
          describe_result(1))
    check('结果码 0x0FA7 → 带十进制 4007',
          '(4007)' in describe_result(0x0FA7), describe_result(0x0FA7))
    check('结果码 0x0FA8 → 带十进制 4008',
          '(4008)' in describe_result(0x0FA8), describe_result(0x0FA8))
    check('结果码 0xA001 → 带十进制 40961',
          '(40961)' in describe_result(0xA001), describe_result(0xA001))
    check('未知结果码不吞掉（保留原始十六进制）',
          '0x1234' in describe_result(0x1234), describe_result(0x1234))
    check('失败响应经 decode_cmd_reply 仍可读',
          decode_cmd_reply(0x0105, b'\xa7\x0f').get('结果', '').startswith('失败'))

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

    p = sub.add_parser('probe', help='命令模式板型指纹（默认干跑；--send 才真发，会停波形流）')
    p.add_argument('--port', required=True)
    p.add_argument('--baud', type=int, default=1500000)
    p.add_argument('--seconds', type=float, default=0.8)
    p.add_argument('--send', action='store_true', help='真正发送命令（会停掉波形流）')
    p.add_argument('--open-payload', type=lambda v: int(v, 0), default=None,
                   help='覆盖「打开命令模式」的预期版本号 payload（默认 2 = 真机实测；文档示例是 1）')
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser('selftest', help='用合成流自检统计器')
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    if args.cmd == 'sniff' and getattr(args, 'try_bauds', None):
        return cmd_try_bauds(args)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
