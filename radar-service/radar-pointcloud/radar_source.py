# -*- coding: utf-8 -*-
"""数据源: 串口实时 / 录制文件回放 / 合成。"""
import time

import numpy as np

from radar_protocol import StreamParser, FrameAssembler

DEFAULT_PORT = 'COM3'
DEFAULT_BAUD = 921600

# 自动识别雷达串口时的匹配线索。
#   1T2R (DEV202H / ICL1122) 的 USB-UART 是 CP2102 → Silicon Labs 驱动
#   2T4R (DEV113P)           的 USB-UART 是 FT232  → FTDI 驱动
RADAR_PORT_HINTS = ('cp210', 'silicon labs', 'silabs', 'silead', 'icl', 'dev202',
                    'ftdi', 'ft232', 'dev113')

# ---- 板型档案：硬件指纹 → 上行协议 + 串口参数 + 物理换算 ---------------------
# 依据: docs/hardware-baseline.md 与 docs/radar-uplink-dev113p-2t4r.md
#   CP210x (10c4:ea60) → 1T2R，921600，ICLH/ICLT 包裹，8 bin × 2 RX
#   FT232  (0403:6001) → 2T4R，1500000，55AA 裸帧 136 B，32 bin × 4 RX
#
# 物理换算参数（f0 / bandwidth / range_res / ant_spacing / chirp_period /
# v_max / v_res）来源: ONELAB 上位机 Radar_Config.ini [WAVE_INFO_0] /
# [MULTI_WAVE_INFO]（见 radar_dsp.py 头部与 docs/hardware-baseline.md §3）。
#   1T2R 一列代回公式得 B=220 MHz→0.6813 m、fc=24.125 GHz→v_max 1.535 m/s，
#   与 radar_dsp 模块常量逐字吻合（5 项全中）⇒ 换算模型可信。
#   2T4R: IdleFreq 24.001 → TopFreq 25.000 GHz ⇒ B=999 MHz ⇒ range_res≈0.150 m；
#         fc=24.5005 GHz，Tc=FrameTimeUS/ChirpsPerFrame=8.138 ms ⇒ v_max≈0.376、
#         v_res≈0.0117 m/s（与真机实测 0.374 / 0.0117 吻合）；
#         HorzAngleInfo 阵元间距 5.900 mm（1T2R 为 6.250 mm）。
# 证据等级: 厂商配置推导 + 1T2R 交叉验证，**非卷尺实测**（真值点检方案见文档）。
BOARD_PROFILES = {
    '1t2r': {
        'label': 'DEV202H (ICL1122) 1T2R',
        'baud': 921600,
        'vidpid': ('10c4:ea60',),
        'desc_hints': ('cp210', 'silicon labs', 'silabs'),
        'bins': 8, 'chirps': 64, 'rx': 2,
        'f0': 24.125e9, 'bandwidth': 220e6, 'range_res': 0.6813,
        'ant_spacing': 6.250e-3, 'chirp_period': 129.5e-3 / 64,
        'v_max': 1.535, 'v_res': 0.0480,
    },
    '2t4r': {
        'label': 'DEV113P 2T4R',
        'baud': 1500000,
        'vidpid': ('0403:6001',),
        'desc_hints': ('ftdi', 'ft232'),
        'bins': 32, 'chirps': 64, 'rx': 4,
        'f0': 24.5005e9, 'bandwidth': 999e6, 'range_res': 0.1500,
        'ant_spacing': 5.900e-3, 'chirp_period': 520840e-6 / 64,
        'v_max': 0.376, 'v_res': 0.01175,
    },
}
DEFAULT_BOARD = '1t2r'

# 回放节奏（实测）：1T2R 991 包/s × 52 B；2T4R 482 帧/s × 136 B
REPLAY_RATE = {
    '1t2r': (991.0, 52),
    '2t4r': (482.0, 136),
}


def detect_board(desc='', hwid=''):
    """按串口描述/hwid 判断板型；认不出返回 None。

    同时匹配 USB VID:PID（最可靠）与描述关键字（兜底）。
    """
    blob = ('%s %s' % (desc or '', hwid or '')).lower()
    for name, prof in BOARD_PROFILES.items():
        for vp in prof['vidpid']:
            if vp in blob:
                return name
    for name, prof in BOARD_PROFILES.items():
        for h in prof['desc_hints']:
            if h in blob:
                return name
    return None


def find_board_for(device, ports=None):
    """在枚举结果里找 device 对应的板型；找不到返回 None。"""
    for dev, desc, hwid in (ports if ports is not None else list_serial_ports()):
        if dev == device:
            return detect_board(desc, hwid)
    return None


def probe_board(port=None, prefer=(DEFAULT_PORT,)):
    """不打开端口，估算「等下会被打开的那块板」是什么型号。

    port 给了具体串口名 → 直接查该口的指纹；port 为 auto/None → 用与
    SerialSource 完全相同的挑选逻辑（find_radar_port）先定位端口再查指纹，
    这样两边的结论不会打架（同时插两块板时尤其重要）。
    """
    ports = list_serial_ports()
    if port and str(port).strip().lower() not in ('auto', ''):
        return find_board_for(port, ports)
    dev, _reason, ports = find_radar_port(prefer=prefer)
    if dev:
        b = find_board_for(dev, ports)
        if b:
            return b
    # 连端口都认不出时，退一步：随便挑一个能认出指纹的板
    for _d, desc, hwid in ports:
        b = detect_board(desc, hwid)
        if b:
            return b
    return None


def make_protocol(board):
    """按板型返回 (StreamParser, FrameAssembler) 实例。"""
    if board == '2t4r':
        from radar_protocol_dev113p import StreamParser as PS, FrameAssembler as FA
        return PS(), FA()
    from radar_protocol import StreamParser as PS, FrameAssembler as FA
    return PS(), FA()


def profile_for(board=None):
    """取板型 profile；未知/None 一律退回 DEFAULT_BOARD，永不抛异常。"""
    return BOARD_PROFILES.get(board) or BOARD_PROFILES[DEFAULT_BOARD]


def frame_period(board=None):
    """**雷达帧周期（秒）—— 全项目时间轴的唯一来源。**

    1T2R = 129.5e-3/64 × 64 = 0.1295 s；2T4R = 520840e-6/64 × 64 = 0.520840 s。
    ⚠ 采集 / 真值标注 / 离线分析三处必须用**同一板型**的同一个值：2T4R 若误用
      1T2R 的 0.1295 s，时间轴会整体偏快 4.02 倍（真值区间与数据的对齐直接作废）。
    """
    p = profile_for(board)
    return float(p['chirp_period']) * int(p['chirps'])


def range_res(board=None):
    """距离门间距（米）：1T2R 0.6813 / 2T4R 0.1500。门中心 r_j = (j + 0.5) × 本值。"""
    return float(profile_for(board)['range_res'])


def baud_for(board=None):
    """该板型的串口波特率（1T2R 921600 / 2T4R 1500000）。"""
    return int(profile_for(board)['baud'])


NAKED_1T2R_PKT = 40      # 1T2R 裸帧模式：一包 40 B（帧首 0xAA、帧尾 0x55）


def looks_like_naked_1t2r(buf, pkt=NAKED_1T2R_PKT, need=6):
    """buffer 是否是「裸帧 1T2R」：40 B 节拍上连续 ≥need 个 0xAA。

    只在文件里找一个 40 B 对齐、连续 6 包的 0xAA 起点（最多试 40 个相位）。
    故意保守：宁可判 None 让调用方回退 --board，也不要把 2T4R 误判成 1T2R。
    """
    n = len(buf)
    if n < pkt * need:
        return False
    for off in range(min(pkt, n)):
        p = off
        run = 0
        while p < n and buf[p] == 0xAA:
            run += 1
            if run >= need:
                return True
            p += pkt
    return False


def sniff_board_from_file(path, scan=262144):
    """从**已录制的 .bin** 判断板型 → '1t2r' / '2t4r' / None（判不出）。

    判据（只看字节形态，故意做得极简且保守）：
      · 命中外层包装魔数 b'ICLH' → **1T2R**（1T2R 走 ICLH + seq + 40 B 雷达帧封装）
      · 否则命中裸帧同步字 b'\\x55\\xaa'，且其后第 3 字节（off3）**低 4 位为 0**
        （2T4R 帧 off3 = (chirp & 0x0F) << 4，低 4 位恒 0）→ **2T4R**
      · 两者都不满足 → None，调用方应回退到 --board 或默认板型（不要猜）

    顺序很关键：1T2R 的雷达帧里有 0xAA…0x55 标记，可能凑出 `55 AA` 两字节，
    所以必须先判 ICLH（1T2R 必含）再判裸帧。

    第三种形态（**10/20 实采踩到**）：1T2R 走**裸帧模式**——没有 ICLH 包装，
    直接把 40 B 雷达帧按 40 B 节拍连发（帧首 0xAA、帧尾 0x55）。此时上面的
    2T4R 判据会因为帧内凑出的 `55 AA` 误判成 2T4R（帧周期 0.52 s、门距 0.15 m），
    离线解析得到 **0 帧却不报错**。所以裸帧 1T2R 必须在 2T4R 之前判：
    以 40 B 为步长至少连续 6 个字节都是 0xAA（跨度 240 B，随机撞上不可能）。
    """
    try:
        with open(path, 'rb') as f:
            buf = f.read(int(scan))
    except Exception:
        return None
    if not buf:
        return None
    if b'ICLH' in buf:
        return '1t2r'
    if looks_like_naked_1t2r(buf):
        return '1t2r'
    i = buf.find(b'\x55\xaa')
    while i >= 0:
        if i + 4 <= len(buf) and (buf[i + 3] & 0x0F) == 0:
            return '2t4r'
        i = buf.find(b'\x55\xaa', i + 1)
    return None


def list_serial_ports():
    """枚举当前系统串口 → [(device, description, hwid), ...]。

    只读；枚举失败（缺 pyserial / 平台异常）返回空列表，不抛异常，
    保证「诊断显示」永远不会把主流程带崩。
    """
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    try:
        return [(p.device, p.description or '', p.hwid or '')
                for p in list_ports.comports()]
    except Exception:
        return []


def find_radar_port(prefer=(DEFAULT_PORT,)):
    """自动挑选雷达串口。

    返回 (device, reason, ports)：
      device 为 None 表示当前没有可用目标；
      reason ∈ 'vidpid'(USB VID:PID 命中已知雷达板) / 'hint'(按 CP210x 等线索命中) /
               'prefer'(命中历史端口名) / 'single'(系统只有一个串口) /
               'ambiguous'(多个串口但认不出) / 'none'(一个都没有)。
      ports 为本次枚举结果，供页面/日志显示原始证据。
    """
    ports = list_serial_ports()
    if not ports:
        return None, 'none', ports
    # 1) 已知板卡的 USB VID:PID（10c4:ea60 / 0403:6001）优先：
    #    串口描述会随驱动语言/版本变化（中文系统里常是"Silicon Labs CP210x..."），
    #    但 hwid 里的 VID:PID 由芯片固定，跨机器更稳。
    for dev, desc, hwid in ports:
        blob = ('%s %s' % (desc or '', hwid or '')).lower()
        for prof in BOARD_PROFILES.values():
            if any(vp in blob for vp in prof['vidpid']):
                return dev, 'vidpid', ports
    # 2) 再退化为厂商标识关键字
    for dev, desc, hwid in ports:
        blob = ('%s %s %s' % (dev, desc, hwid)).lower()
        for h in RADAR_PORT_HINTS:
            if h in blob:
                return dev, 'hint', ports
    low = [p[0].lower() for p in ports]
    for want in (prefer or ()):
        if want and want.lower() in low:
            return ports[low.index(want.lower())][0], 'prefer', ports
    if len(ports) == 1:
        return ports[0][0], 'single', ports
    return None, 'ambiguous', ports


class SourceBase:
    name = 'base'

    def open(self):
        pass

    def read(self, timeout=0.05):
        """返回原始字节块 (可能为空)。"""
        raise NotImplementedError

    def close(self):
        pass

    def diag(self):
        """链路诊断信息（页面/`/status` 用）；无串口语义的源返回 None。"""
        return None


class SerialSource(SourceBase):
    """串口源，带自动重连：USB/驱动瞬时失效不会中断整个流水线。

    port='auto'（现在的默认值）时**自动识别雷达串口**：每次重试都重新枚举系统串口，
    优先命中 CP210x/Silicon Labs（本项目 USB-UART 芯片），并在打不开时给出中文原因。
    因此「先开软件、后插雷达」或「雷达换了 USB 口」都能自动连上，不需要重启软件。
    """

    def __init__(self, port=DEFAULT_PORT, baud=None, retry_delay=1.0,
                 on_event=None, prefer=(DEFAULT_PORT,)):
        self.auto = (port is None) or (str(port).strip().lower() in ('auto', ''))
        self.port = None if self.auto else str(port)
        # baud=None → 按探测到的板型自动定波特率（1T2R 921600 / 2T4R 1500000）；
        # 显式给数值时以调用方为准（现场排障 / 换固件场景）。
        self.baud_auto = baud is None
        self.baud = int(baud) if baud else DEFAULT_BAUD
        self.board = None            # '1t2r' / '2t4r'，open() 时按硬件指纹探测
        self.prefer = tuple(p for p in (prefer or ()) if p)
        self.name = ('serial:auto@%d' % self.baud) if self.auto else ('serial:%s@%d' % (port, self.baud))
        self.ser = None
        self.retry_delay = retry_delay
        self.connected = False
        self.reconnects = 0
        self.errors = 0
        self.last_error = None
        self.last_error_hint = None
        self.port_reason = None
        self.ports_seen = []
        self.on_event = on_event
        self._next_try = 0.0
        self._last_log = None

    def _log(self, msg):
        if self.on_event is not None:
            try:
                self.on_event(msg)
                return
            except Exception:
                pass
        print(msg, flush=True)

    def _log_once(self, msg):
        """同一原因只打一次，避免「没插雷达」时每秒刷屏。"""
        if msg == self._last_log:
            return
        self._last_log = msg
        self._log(msg)

    def diag(self):
        prof = BOARD_PROFILES.get(self.board) or {}
        return {
            'auto': bool(self.auto),
            'port': self.port,
            'baud': self.baud,
            'baud_auto': bool(self.baud_auto),
            'board': self.board,
            'board_label': prof.get('label'),
            'port_reason': self.port_reason,
            'ports': [{'device': d, 'desc': desc} for d, desc, _ in (self.ports_seen or [])],
            'connected': bool(self.connected),
            'last_error': self.last_error,
            'last_error_hint': self.last_error_hint,
        }

    @staticmethod
    def _explain(port, e):
        """把底层异常翻成可操作的中文原因。"""
        txt = '%s: %s' % (type(e).__name__, e)
        low = txt.lower()
        if '未检测到任何串口设备' in txt:
            return txt, ('未检测到任何串口设备 —— 雷达 USB 是否插好？'
                         '设备管理器里应出现 "Silicon Labs CP210x USB to UART Bridge"')
        if '认不出雷达' in txt:
            return txt, None        # 异常文本本身已经写清了怎么办
        if ('拒绝访问' in txt or 'permission' in low or 'access is denied' in low
                or 'being used' in low):
            return txt, ('%s 被其它程序占用（厂商工具 ICL_mmWaveSensorDevTool 还开着？'
                         '或另一个 server 实例在跑）' % port)
        if 'could not open port' in low or 'filenotfound' in low or 'cannot find' in low:
            return txt, ('%s 不存在（雷达是否已插好 USB？换了接口的话端口号会变）' % port)
        return txt, None

    def _resolve_target(self):
        """返回 (port, reason, ports)；固定端口模式下也带回枚举结果，便于诊断。"""
        if not self.auto:
            return self.port, 'fixed', list_serial_ports()
        return find_radar_port(self.prefer)

    def _try_open(self):
        import serial
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        port, reason, ports = self._resolve_target()
        self.port_reason = reason
        self.ports_seen = ports
        if port is None:
            if reason == 'ambiguous':
                raise RuntimeError('检测到多个串口但认不出雷达（%s），'
                                   '请用 --port 明确指定'
                                   % ', '.join(p[0] for p in ports))
            raise RuntimeError('未检测到任何串口设备')
        # ---- 板型探测：硬件指纹 → 上行协议 + 波特率 -------------------------
        # 必须在 serial.Serial() 之前，因为 1T2R/2T4R 的波特率不同
        # (921600 / 1500000)，开错了端口只会收到乱码。
        hit = find_board_for(port, ports)
        if hit:
            self.board = hit
            if self.baud_auto:
                want = BOARD_PROFILES[hit]['baud']
                if want != self.baud:
                    self.baud = want
                    self._log('[serial] 识别到板型 %s（%s）→ 波特率 %d'
                              % (hit, BOARD_PROFILES[hit]['label'], want))
        self.ser = serial.Serial(port, self.baud, timeout=0.02,
                                 bytesize=8, parity='N', stopbits=1)
        self.ser.reset_input_buffer()
        self.port = port
        self.connected = True
        self.last_error = None
        self.last_error_hint = None
        self._last_log = None
        self.name = 'serial:%s@%d' % (port, self.baud)
        if reason == 'vidpid':
            self._log('[serial] 自动识别到雷达串口 %s（USB VID:PID 命中已知雷达板）' % port)
        elif reason == 'hint':
            self._log('[serial] 自动识别到雷达串口 %s（CP210x/Silicon Labs 线索）' % port)
        elif reason in ('prefer', 'single'):
            self._log('[serial] 使用串口 %s（自动选择规则: %s）' % (port, reason))
        return True

    def _on_fail(self, port, e):
        self.connected = False
        self.errors += 1
        port = port or self.port or '（自动识别）'
        txt, hint = self._explain(port, e)
        self.last_error = txt
        self.last_error_hint = hint
        return txt, hint

    def open(self):
        """尽力打开；失败不抛异常，由 read() 持续重试（便于演示时不中断）。"""
        try:
            self._try_open()
        except Exception as e:
            txt, hint = self._on_fail(self.port, e)
            self._next_try = time.time() + self.retry_delay
            self._log_once('[serial] 打开失败: %s%s（%.1fs 后重试；当前系统串口: %s）'
                           % (hint or txt, '', self.retry_delay, self._ports_text()))

    def _ports_text(self):
        if not self.ports_seen:
            return '无'
        return ', '.join('%s(%s)' % (d, desc) if desc else d for d, desc, _ in self.ports_seen)

    def read(self, timeout=0.05):
        if self.ser is None:
            now = time.time()
            if now < self._next_try:
                time.sleep(0.02)
                return b''
            try:
                self._try_open()
                self.reconnects += 1
                if self.auto and self.port_reason in ('vidpid', 'hint'):
                    pass          # _try_open 内已打过「自动识别」日志
                else:
                    self._log('[serial] %s 已重连（第 %d 次）' % (self.port, self.reconnects))
            except Exception as e:
                txt, hint = self._on_fail(self.port, e)
                self._next_try = now + self.retry_delay
                self._log_once('[serial] 打开失败: %s（%.1fs 后重试；当前系统串口: %s）'
                               % (hint or txt, self.retry_delay, self._ports_text()))
                time.sleep(0.02)
                return b''
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                n = self.ser.in_waiting
                if n:
                    return self.ser.read(n)
            except Exception as e:
                port = self.port
                txt, hint = self._on_fail(port, e)
                self._log('[serial] 读取出错: %s（尝试重连）' % (hint or txt))
                self.close()
                self._next_try = time.time() + self.retry_delay
                return b''
            time.sleep(0.002)
        return b''

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self.connected = False


class ReplaySource(SourceBase):
    """按实测速率回放抓包文件。

    1T2R: 52 字节/包 (ICLH 包裹 + 40B 雷达帧)，约 991 包/s
    2T4R: 136 字节/帧 (55AA 裸帧)，约 482 帧/s
    """

    def __init__(self, path, rate=None, loop=True, board=None, packet_len=None):
        self.path = path
        # board 同时决定回放节奏与下游协议解析器（build_pipeline 读 source.board）
        self.board = board or DEFAULT_BOARD
        d_rate, d_len = REPLAY_RATE.get(self.board, REPLAY_RATE[DEFAULT_BOARD])
        self.rate = float(rate) if rate else d_rate
        self.packet_len = int(packet_len) if packet_len else d_len
        self.loop = loop
        self.name = 'replay:%s(%s)' % (path, self.board)
        self.data = None
        self.pos = 0

    def open(self):
        self.data = open(self.path, 'rb').read()
        self.pos = 0
        self._t0 = time.time()
        self._sent = 0

    def read(self, timeout=0.05):
        if not self.data:
            time.sleep(timeout)
            return b''
        L = self.packet_len
        elapsed = time.time() - self._t0
        want = int(elapsed * self.rate) - self._sent
        pkts = int(want / L) if want > 0 else 0
        pkts = min(pkts, 400)
        if pkts <= 0:
            time.sleep(0.002)
            return b''
        n = pkts * L
        if self.pos + n > len(self.data):
            if self.loop:
                self.pos = 0
                self._t0 = time.time()
                self._sent = 0
            else:
                time.sleep(timeout)
                return b''
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        self._sent += pkts
        return chunk

    def close(self):
        self.data = None


class LoopbackSource(SourceBase):
    """把合成帧编码成真实字节流，端到端验证解析链路。"""

    def __init__(self, rate=991.0, targets=None, frames=24):
        self.rate = rate
        self.name = 'loopback'
        # 合成帧由 radar_dsp.synth_frame() 生成，尺度固定为 1T2R (64×8)，
        # 因此板型恒为 '1t2r'（--board 2t4r 时 server 会提示并强制回退）。
        self.board = DEFAULT_BOARD
        self.targets = targets or [(0.0, 3.0, 0.0)]
        self.nframes = frames
        self.frame_targets = []

    def open(self):
        from radar_dsp import synth_frame
        self.raw = bytearray()
        seq = 0
        self.frame_targets = []
        for rep in range(self.nframes):
            t = self.targets[rep % len(self.targets)]
            self.frame_targets.append(t)
            z0, z1 = synth_frame(target=t, seed=rep)
            for c in range(64):
                for rx, z in ((0, z0), (1, z1)):
                    self.raw += self._packet(z, rx, c, seq)
                    seq = (seq + 1) % 65536
        self.data = bytes(self.raw)
        self.pos = 0
        self._t0 = time.time()
        self._sent = 0

    @staticmethod
    def _packet(z, rx, chirp, seq):
        ident = (0x30 if rx == 0 else 0xB0) | (chirp >> 4)
        x1 = (chirp & 0x0F) << 4
        payload = bytearray([0xAA, ident, x1, 0x09])
        for i in range(8):
            payload += int(round(z[chirp, i].real)).to_bytes(2, 'big', signed=True)
            payload += int(round(z[chirp, i].imag)).to_bytes(2, 'big', signed=True)
        payload += bytes([0x00, 0x0F, 0x00, 0x55])
        head = b'ICLH' + len(payload).to_bytes(2, 'little') + seq.to_bytes(2, 'little')
        return head + bytes(payload) + b'ICLT'

    def read(self, timeout=0.05):
        elapsed = time.time() - self._t0
        want = int(elapsed * self.rate) - self._sent
        pkts = min(max(want // 52, 0), 400)
        if pkts <= 0:
            time.sleep(0.002)
            return b''
        n = pkts * 52
        if self.pos + n > len(self.data):
            self.pos = 0
            self._t0 = time.time()
            self._sent = 0
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        self._sent += pkts
        return chunk

    def close(self):
        pass


def build_pipeline(source, on_frame, stop, record_path=None, verbose=False,
                   record_append=False, board=None):
    """读数据源 → 解析 → 拼帧 → 回调 on_frame(z0, z1, stats)。

    数据源与回调的单次异常都不会终止主循环：读错误交给数据源自愈，
    回调错误计数后继续，保证长时间演示不中断。

    board: '1t2r' / '2t4r' / None。None 时跟随数据源自报的 board
           （SerialSource 按硬件指纹探测），仍未知则回退 DEFAULT_BOARD。
           板型变化时重建解析器 —— 换板不用重启进程。

    record_append: 会话重启时用 'ab' 追加，避免把上一次会话的录制清掉。
    """
    parser = None
    asm = None
    cur_board = None
    rec = open(record_path, 'ab' if record_append else 'wb') if record_path else None
    t0 = time.time()
    frames = 0
    cb_errors = 0
    try:
        source.open()
        while not stop.is_set():
            want = board or getattr(source, 'board', None) or DEFAULT_BOARD
            if want != cur_board:
                parser, asm = make_protocol(want)
                cur_board = want
                if verbose:
                    print('[pipeline] 板型 %s → 协议解析器 %s'
                          % (want, type(parser).__name__), flush=True)
            try:
                chunk = source.read(0.05)
            except Exception as e:
                print('[pipeline] 数据源异常: %s: %s' % (type(e).__name__, e), flush=True)
                time.sleep(0.2)
                continue
            if not chunk:
                continue
            if rec is not None:
                try:
                    rec.write(chunk)
                except Exception:
                    rec = None
            for rf in parser.feed(chunk):
                pair = asm.push(rf)
                if pair is not None:
                    frames += 1
                    dt = time.time() - t0
                    # DEV113P 解析器把「已解析包数」记在 frames 下，1T2R 记在
                    # packets 下；这里统一取一次，避免两个解析器字段名不一致。
                    pkt = getattr(parser, 'packets', None)
                    if pkt is None:
                        pkt = getattr(parser, 'frames', 0)
                    stats = {
                        'frames': frames,
                        'fps': frames / dt if dt > 0 else 0.0,
                        'packets': pkt,
                        'skipped': getattr(parser, 'skipped_bytes', 0),
                        'bad': getattr(parser, 'bad_packets', 0),
                        'dropped': getattr(asm, 'dropped', 0),
                        'board': cur_board,
                        'source': source.name,
                        'uptime': dt,
                        'connected': getattr(source, 'connected', True),
                        'reconnects': getattr(source, 'reconnects', 0),
                        'read_errors': getattr(source, 'errors', 0),
                        'last_error': getattr(source, 'last_error', None),
                        'src_diag': source.diag() if hasattr(source, 'diag') else None,
                        'cb_errors': cb_errors,
                    }
                    try:
                        on_frame(pair[0], pair[1], stats)
                    except Exception as e:
                        cb_errors += 1
                        print('[pipeline] 回调异常 #%d: %s: %s'
                              % (cb_errors, type(e).__name__, e), flush=True)
    finally:
        source.close()
        if rec is not None:
            rec.close()
