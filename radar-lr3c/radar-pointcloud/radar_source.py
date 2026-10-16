# -*- coding: utf-8 -*-
"""数据源: 串口实时 / 录制文件回放 / 合成。"""
import time

import numpy as np

from radar_protocol import StreamParser, FrameAssembler

DEFAULT_PORT = 'COM3'
DEFAULT_BAUD = 921600

# 自动识别雷达串口时的匹配线索（本项目的 USB-UART 是 CP2102 → 驱动 Silicon Labs）
RADAR_PORT_HINTS = ('cp210', 'silicon labs', 'silabs', 'silead', 'icl', 'dev202')


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
      reason ∈ 'hint'(按 CP210x 等线索命中) / 'prefer'(命中历史端口名) /
               'single'(系统只有一个串口) / 'ambiguous'(多个串口但认不出) / 'none'(一个都没有)。
      ports 为本次枚举结果，供页面/日志显示原始证据。
    """
    ports = list_serial_ports()
    if not ports:
        return None, 'none', ports
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

    def __init__(self, port=DEFAULT_PORT, baud=DEFAULT_BAUD, retry_delay=1.0,
                 on_event=None, prefer=(DEFAULT_PORT,)):
        self.auto = (port is None) or (str(port).strip().lower() in ('auto', ''))
        self.port = None if self.auto else str(port)
        self.baud = baud
        self.prefer = tuple(p for p in (prefer or ()) if p)
        self.name = ('serial:auto@%d' % baud) if self.auto else ('serial:%s@%d' % (port, baud))
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
        return {
            'auto': bool(self.auto),
            'port': self.port,
            'baud': self.baud,
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
        self.ser = serial.Serial(port, self.baud, timeout=0.02,
                                 bytesize=8, parity='N', stopbits=1)
        self.ser.reset_input_buffer()
        self.port = port
        self.connected = True
        self.last_error = None
        self.last_error_hint = None
        self._last_log = None
        self.name = 'serial:%s@%d' % (port, self.baud)
        if reason == 'hint':
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
                if self.auto and self.port_reason == 'hint':
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
    """按实测速率回放抓包文件（52 字节/包，约 991 包/s）。"""

    def __init__(self, path, rate=991.0, loop=True):
        self.path = path
        self.rate = float(rate)
        self.loop = loop
        self.name = 'replay:%s' % path
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
        elapsed = time.time() - self._t0
        want = int(elapsed * self.rate) - self._sent
        pkts = int(want / 52) if want > 0 else 0
        pkts = min(pkts, 400)
        if pkts <= 0:
            time.sleep(0.002)
            return b''
        n = pkts * 52
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
                   record_append=False):
    """读数据源 → 解析 → 拼帧 → 回调 on_frame(z0, z1, stats)。

    数据源与回调的单次异常都不会终止主循环：读错误交给数据源自愈，
    回调错误计数后继续，保证长时间演示不中断。

    record_append: 会话重启时用 'ab' 追加，避免把上一次会话的录制清掉。
    """
    parser = StreamParser()
    asm = FrameAssembler()
    rec = open(record_path, 'ab' if record_append else 'wb') if record_path else None
    t0 = time.time()
    frames = 0
    cb_errors = 0
    try:
        source.open()
        while not stop.is_set():
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
                    stats = {
                        'frames': frames,
                        'fps': frames / dt if dt > 0 else 0.0,
                        'packets': parser.packets,
                        'skipped': parser.skipped_bytes,
                        'bad': parser.bad_packets,
                        'dropped': asm.dropped,
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
