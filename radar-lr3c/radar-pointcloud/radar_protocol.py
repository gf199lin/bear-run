# -*- coding: utf-8 -*-
"""
ICL1122 (矽典微 DEV202H, 1T2R) 数据透传协议解析。

帧结构 —— 2026-10-15 以 COM3@921600 实机抓包确认（证据: .deepworks/tmp/cap.bin）:

  外层封装 (MCU wrapper, 实测启用):
    b"ICLH" (49 43 4C 48) | u16 LE 负载长度 P | u16 LE 包序号 | 雷达帧(P 字节) | b"ICLT" (49 43 4C 54)

  雷达帧 (P = 4 + 4*N + 4, N = 每 chirp 距离单元数 = 8):
    +0   0xAA   帧头
    +1   ID     高半字节 0x3 = RX0 / 0xB = RX1, 低半字节 = chirpIndex 高 4 位
    +2   X1     高半字节 = chirpIndex 低 4 位
    +3   X2     TX 信息 (实测恒为 0x09)
    +4   payload  8 × (int16 大端 实部, int16 大端 虚部)
    +36  tail 3 字节
    +39  0x55   帧尾

  故 chirpIndex = (ID & 0x0F) * 16 + (X1 >> 4)，取值 0..63。

对应固件实现: DEV202H/App/common/src/dataprocess.c (DataParse / DataCopy / StartDataTransfer)
与 DEV202H/App/common/inc/dataprocess.h (ID、掩码、WRAPPER_* 宏)。
"""
import struct
from dataclasses import dataclass

import numpy as np

WRAPPER_HEAD = b"ICLH"
WRAPPER_TAIL = b"ICLT"
WRAPPER_HEAD_LEN = 8
WRAPPER_TAIL_LEN = 4

FRAME_HEAD = 0xAA
FRAME_TAIL = 0x55
ID_MASK = 0xF0
RX0_ID = 0x30          # FFT0_ID_NEW
RX1_ID = 0xB0          # FFT1_ID_NEW
BINS_PER_CHIRP = 8     # 1DFFT 输出列数
FRAME_HLEN = 4         # AA ID X1 X2
FRAME_TLEN = 4         # 3 字节 + 0x55
RADAR_FRAME_LEN = FRAME_HLEN + 4 * BINS_PER_CHIRP + FRAME_TLEN   # = 40
CHIRPS_PER_FRAME = 64


@dataclass
class RadarFrame:
    """一个雷达 chirp 的数据块（单 RX）。"""
    seq: int
    chirp: int
    rx: int
    bins: np.ndarray      # complex64, 长度 8


def parse_radar_frame(frame: bytes, seq: int = 0):
    """解析 40 字节雷达帧，失败返回 None。"""
    if len(frame) != RADAR_FRAME_LEN:
        return None
    if frame[0] != FRAME_HEAD or frame[RADAR_FRAME_LEN - 1] != FRAME_TAIL:
        return None
    ident = frame[1]
    hi = ident & ID_MASK
    if hi == RX0_ID:
        rx = 0
    elif hi == RX1_ID:
        rx = 1
    else:
        return None
    chirp = ((ident & 0x0F) << 4) | (frame[2] >> 4)

    raw = np.frombuffer(frame[FRAME_HLEN:FRAME_HLEN + 4 * BINS_PER_CHIRP], dtype='>i2')
    iq = raw.astype(np.float32).reshape(BINS_PER_CHIRP, 2)
    bins = (iq[:, 0] + 1j * iq[:, 1]).astype(np.complex64)
    return RadarFrame(seq=seq, chirp=chirp, rx=rx, bins=bins)


class StreamParser:
    """增量式字节流解析器，可跳过任意脏数据。"""

    def __init__(self):
        self.buf = bytearray()
        self.bad_packets = 0
        self.skipped_bytes = 0
        self.packets = 0
        self.total = 0
        self.resync_at = []

    def feed(self, data: bytes):
        """喂入原始字节，产出 RadarFrame 列表。"""
        self.buf.extend(data)
        self.total += len(data)
        out = []
        buf = self.buf
        while True:
            pos = buf.find(WRAPPER_HEAD)
            if pos < 0:
                # 保留末尾 3 字节，防止 "ICL" 被切断
                if len(buf) > 4:
                    k = len(buf) - 4
                    self.resync_at.append(self.total - len(buf))
                    self.skipped_bytes += k
                    del buf[:k]
                break
            if pos > 0:
                self.resync_at.append(self.total - len(buf))
                self.skipped_bytes += pos
                del buf[:pos]
            if len(buf) < WRAPPER_HEAD_LEN:
                break
            plen = int.from_bytes(buf[4:6], 'little')
            seq = int.from_bytes(buf[6:8], 'little')
            if not (FRAME_HLEN + FRAME_TLEN <= plen <= 4096):
                # 长度非法 → 丢 1 字节重新同步
                self.bad_packets += 1
                self.resync_at.append(self.total - len(buf))
                self.skipped_bytes += 1
                del buf[:1]
                continue
            total = WRAPPER_HEAD_LEN + plen + WRAPPER_TAIL_LEN
            if len(buf) < total:
                break
            if bytes(buf[WRAPPER_HEAD_LEN + plen:total]) != WRAPPER_TAIL:
                self.bad_packets += 1
                self.resync_at.append(self.total - len(buf))
                self.skipped_bytes += 1
                del buf[:1]
                continue
            payload = bytes(buf[WRAPPER_HEAD_LEN:WRAPPER_HEAD_LEN + plen])
            del buf[:total]
            self.packets += 1
            rf = parse_radar_frame(payload, seq)
            if rf is not None:
                out.append(rf)
        return out


class FrameAssembler:
    """把 chirp/RX 数据块拼成一帧 (64 chirp × 2 RX × 8 距离单元)。

    含两个健壮性机制：
      * chirp 回绕检测 —— 收到比上一包更小的 chirp 号说明新帧已开始，丢弃残缺帧，
        避免把两帧数据混在一起（回放循环、丢包后都会遇到）。
      * 超时丢弃 —— 单帧内超过 2×chirps×2 个包仍未收齐，判定丢包，重新开始，
        否则一次丢包会导致此后所有帧永远无法完成。
    """

    def __init__(self, chirps=CHIRPS_PER_FRAME, bins=BINS_PER_CHIRP):
        self.chirps = chirps
        self.bins = bins
        self.data = np.zeros((2, chirps, bins), dtype=np.complex64)
        self.filled = np.zeros((2, chirps), dtype=bool)
        self.frames = 0
        self.crc_err = 0
        self.dropped = 0
        self.pending = 0
        self.last_chirp = -1

    def push(self, rf: RadarFrame):
        """返回 (z0, z1) 当一帧收齐，否则 None。"""
        if rf.chirp >= self.chirps:
            self.crc_err += 1
            return None
        self.pending += 1
        if self.pending > 1 and (rf.chirp < self.last_chirp
                                 or self.pending > 2 * self.chirps * 2):
            if not self.filled.all():
                self.dropped += 1
            self.filled[:] = False
            self.pending = 1
        self.last_chirp = rf.chirp
        self.data[rf.rx, rf.chirp, :] = rf.bins
        self.filled[rf.rx, rf.chirp] = True
        if self.filled.all():
            z0 = self.data[0].copy()
            z1 = self.data[1].copy()
            self.filled[:] = False
            self.pending = 0
            self.frames += 1
            return z0, z1
        return None
