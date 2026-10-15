# -*- coding: utf-8 -*-
"""数据源: 串口实时 / 录制文件回放 / 合成。"""
import time

import numpy as np

from radar_protocol import StreamParser, FrameAssembler

DEFAULT_PORT = 'COM3'
DEFAULT_BAUD = 921600


class SourceBase:
    name = 'base'

    def open(self):
        pass

    def read(self, timeout=0.05):
        """返回原始字节块 (可能为空)。"""
        raise NotImplementedError

    def close(self):
        pass


class SerialSource(SourceBase):
    """串口源，带自动重连：USB/驱动瞬时失效不会中断整个流水线。"""

    def __init__(self, port=DEFAULT_PORT, baud=DEFAULT_BAUD, retry_delay=1.0,
                 on_event=None):
        self.name = 'serial:%s@%d' % (port, baud)
        self.port = port
        self.baud = baud
        self.ser = None
        self.retry_delay = retry_delay
        self.connected = False
        self.reconnects = 0
        self.errors = 0
        self.last_error = None
        self.on_event = on_event
        self._next_try = 0.0

    def _log(self, msg):
        if self.on_event is not None:
            try:
                self.on_event(msg)
                return
            except Exception:
                pass
        print(msg, flush=True)

    def _try_open(self):
        import serial
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self.ser = serial.Serial(self.port, self.baud, timeout=0.02,
                                 bytesize=8, parity='N', stopbits=1)
        self.ser.reset_input_buffer()
        self.connected = True
        self.last_error = None
        return True

    def open(self):
        """尽力打开；失败不抛异常，由 read() 持续重试（便于演示时不中断）。"""
        try:
            self._try_open()
        except Exception as e:
            self.connected = False
            self.errors += 1
            self.last_error = '%s: %s' % (type(e).__name__, e)
            self._next_try = time.time() + self.retry_delay
            self._log('[serial] 打开 %s 失败: %s（%.1fs 后重试）'
                      % (self.port, self.last_error, self.retry_delay))

    def read(self, timeout=0.05):
        if self.ser is None:
            now = time.time()
            if now < self._next_try:
                time.sleep(0.02)
                return b''
            try:
                self._try_open()
                self.reconnects += 1
                self._log('[serial] %s 已重连（第 %d 次）' % (self.port, self.reconnects))
            except Exception as e:
                self.connected = False
                self.errors += 1
                self.last_error = '%s: %s' % (type(e).__name__, e)
                self._next_try = now + self.retry_delay
                time.sleep(0.02)
                return b''
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                n = self.ser.in_waiting
                if n:
                    return self.ser.read(n)
            except Exception as e:
                self.connected = False
                self.errors += 1
                self.last_error = '%s: %s' % (type(e).__name__, e)
                self._log('[serial] %s 读取出错: %s（尝试重连）' % (self.port, self.last_error))
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


def build_pipeline(source, on_frame, stop, record_path=None, verbose=False):
    """读数据源 → 解析 → 拼帧 → 回调 on_frame(z0, z1, stats)。

    数据源与回调的单次异常都不会终止主循环：读错误交给数据源自愈，
    回调错误计数后继续，保证长时间演示不中断。
    """
    parser = StreamParser()
    asm = FrameAssembler()
    rec = open(record_path, 'wb') if record_path else None
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
