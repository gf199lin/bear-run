# -*- coding: utf-8 -*-
"""
矽典微 DEV113P (2T4R) 数据透传协议解析 —— 与 1T2R (DEV202H/ICL1122) 完全不同的格式。

帧结构 —— 2026-10-19 以 COM4@1500000 实机抓包确认
（证据: .deepworks/tmp/2t4r-COM4-b1500000-8s.bin，3908 帧、100% 间距 == 136、
  零丢帧；见 docs/radar-uplink-dev113p-2t4r.md）:

  +0    0x55 0xAA    同步字（注意与 1T2R 的 0xAA...0x55 相反，且**无** ICLH/ICLT 包裹）
  +2    counter 高字节（实测 0x80..0x83）
  +3    counter 低字节（低 4 位恒 0）
  +4    0x1F         点数-1 → 32 个距离单元
  +5    payload      32 × (int16 BE 实部, int16 BE 虚部) = 128 字节
  +133  校验和       2 字节大端 = payload 的 64 个 int16 之和 mod 65536
  +135  帧标签       bits[3:2] = RX 序号（0..3）；bits[7:4] = 4bit 滚动计数
  总长 136

  counter = (b[2]<<8) | b[3]
  chirp   = (counter >> 4) & 0x3F   → 0..63（实测 64 个值全覆盖）

  counter 只是 chirp 的**冗余编码**（实测 3908/3908）：
  counter = 0x8000 | (chirp << 4)，即 off2 = 0x80|(chirp>>4)、off3 = (chirp&0xF)<<4
  （所以 off2 ∈ 0x80..0x83、off3 低 4 位恒 0 —— 它们是 chirp 的副产品，
   不是独立计数器）。真正的计数在帧尾标签高 4 位。

  **同一 chirp 连续 4 帧**（counter 完全相同）= 该 chirp 的 4 个 RX；
  counter 每换一个 chirp 递增 0x10。因此一条上行「雷达帧」= 64 chirp × 4 RX。

帧尾 3 字节语义 —— 2026-10-19 离线收敛（证据: 同一 8s 抓包 3908/3908 帧）
  * `BE16(buf[133:135]) == sum(payload 的 64 个 int16) & 0xFFFF`，**命中率 100%**
    → 每帧可做校验和验证，丢帧/误码可量化（见 StreamParser.bad_checksum）。
  * `(buf[135] >> 2) & 0x03` 即 RX 序号，实测 976/978 个 chirp 组的 4 帧
    依次为 rx = 0,1,2,3（2 个例外 = 抓包起点残段与一次中途缺 2 帧）。
    → **RX 槽位不必再靠到达次序猜**，直接读标签（拿不到标签时才回退到达次序）。
  * `buf[135] >> 4` = **雷达帧序号 mod 16**：组内恒定、每过一个 chirp 回绕
    （chirp 63→0）恰 +1（实测 15/15 次变化都是 +1），因此可用它判定
    「整个雷达帧丢失」（见 FrameAssembler.gap_frames）。
  ⇒ 至此 136 字节**全部字段已定**，无剩余未知字节。

字节序说明: 固件 HAS_DATA_Output()（components/data_process/data_transfer.c）
对每个 32-bit word 做**字内整体字节翻转**，所以线上 int16 呈**大端序**。

帧长来源: 固件 DSP_GetDataLength()（drivers/dsp_4/icl_dsp.c）中
  DSP_1DFFT 分支 len = points*4 + 8 = 32*4 + 8 = 136。

⚠ 待确认（本模块不假设、调用方需知）：
  * 32 个距离单元的 `range_res`：配置推导 = 0.150 m（见 docs/hardware-baseline.md），
    但仍待卷尺真值点检；
  * 每 chirp 时间 Tc / 不模糊速度（本文件只做解析与组装，不换算物理量）；
  * counter 高位 0x80 的含义：已确认是 chirp 编码的固定标志位（非独立字段）；
  * 帧尾标签高 4 位：已确认 = 雷达帧序号 mod 16。
（2026-10-19 之后仅剩：`range_res` 的卷尺真值点检，以及帧序 ↔ 物理 RX 映射。）
"""
from dataclasses import dataclass

import numpy as np

SYNC = b"\x55\xAA"
FRAME_LEN = 136
FRAME_TAIL_LEN = 3
PAYLOAD_OFF = 5
POINTS_MINUS_ONE = 0x1F
BINS_PER_CHIRP = 32
CHIRPS_PER_FRAME = 64
RX_COUNT = 4
CHIRP_MASK = 0x3F
PAYLOAD_LEN = 4 * BINS_PER_CHIRP          # 128
TAIL_CHECKSUM_OFF = 133                   # 2 字节大端校验和
TAIL_TAG_OFF = 135                        # bits[7:4]=雷达帧序号 mod16, bits[3:2]=RX 序号
RX_TAG_SHIFT = 2
RX_TAG_MASK = 0x03
FRAME_TAG_SHIFT = 4
FRAME_TAG_MASK = 0x0F
COUNTER_FLAG = 0x8000                     # counter = COUNTER_FLAG | (chirp << 4)


def chirp_to_counter(chirp: int) -> int:
    """chirp → 帧内 counter 字段（构造合成帧用；解析方向见 parse_frame）。

    counter 是 chirp 的冗余编码：counter = 0x8000 | (chirp << 4)，
    即 off2 = 0x80 | (chirp >> 4)、off3 = (chirp & 0x0F) << 4。
    **counter 不含任何额外计数信息**（实测 3908/3908 成立）。
    """
    return COUNTER_FLAG | ((chirp & CHIRP_MASK) << 4)


def tail_checksum(payload: bytes) -> int:
    """帧尾校验和 = payload 的 64 个 int16（大端）之和 mod 65536。"""
    raw = np.frombuffer(payload, dtype='>i2')
    return int(raw.astype(np.int32).sum()) & 0xFFFF


@dataclass
class Dev113pFrame:
    """一个 DEV113P chirp 的单 RX 数据块。"""
    seq: int
    counter: int
    chirp: int
    rx: int
    bins: np.ndarray          # complex64, 长度 32
    checksum_ok: bool = True
    frame_seq: int = 0        # 雷达帧序号 mod 16（取自帧尾标签高 4 位）


def parse_frame(buf, off=0, seq=0, rx=-1, verify=True):
    """解析 buf[off:] 起始的 136 字节帧；失败返回 None。

    rx < 0 时从帧尾标签取（bits[3:2]）；verify=True 时校验帧尾校验和。
    """
    if off < 0 or off + FRAME_LEN > len(buf):
        return None
    if buf[off] != 0x55 or buf[off + 1] != 0xAA:
        return None
    if buf[off + 4] != POINTS_MINUS_ONE:
        return None
    payload = bytes(buf[off + PAYLOAD_OFF:off + PAYLOAD_OFF + PAYLOAD_LEN])
    cks_ok = True
    if verify:
        want = (buf[off + TAIL_CHECKSUM_OFF] << 8) | buf[off + TAIL_CHECKSUM_OFF + 1]
        cks_ok = tail_checksum(payload) == want
        if not cks_ok:
            return None
    tag = buf[off + TAIL_TAG_OFF]
    if rx < 0:
        rx = (tag >> RX_TAG_SHIFT) & RX_TAG_MASK
    frame_seq = (tag >> FRAME_TAG_SHIFT) & FRAME_TAG_MASK
    counter = (buf[off + 2] << 8) | buf[off + 3]
    chirp = (counter >> 4) & CHIRP_MASK
    raw = np.frombuffer(payload, dtype='>i2')
    iq = raw.astype(np.float32).reshape(BINS_PER_CHIRP, 2)
    bins = (iq[:, 0] + 1j * iq[:, 1]).astype(np.complex64)
    return Dev113pFrame(seq=seq, counter=counter, chirp=chirp, rx=rx,
                        bins=bins, checksum_ok=cks_ok, frame_seq=frame_seq)



class StreamParser:
    """增量式 DEV113P 字节流解析器，可跳过任意脏数据。

    与 1T2R 的 StreamParser 一样是「喂多少字节、产出多少帧」的纯函数式接口，
    但不认 ICLH/ICLT（DEV113P 固件不做 wrapper）。

    同步判据（四重，缺一不认）：
      1. buf[0:2] == 55 AA
      2. buf[4] == 0x1F（点数-1 恒为 31，实测 100%）
      3. 帧长恰为 136
      4. 帧尾 BE16 校验和 == payload 的 64 个 int16 之和 mod 65536
         （2026-10-19 实测 3908/3908 命中；可用 verify=False 关闭）

    RX 序号从帧尾标签 bits[3:2] 读取，不再依赖到达次序。
    """

    def __init__(self, verify=True):
        self.buf = bytearray()
        self.frames = 0
        self.bad_packets = 0
        self.bad_checksum = 0
        self.skipped_bytes = 0
        self.resyncs = 0
        self.total = 0
        self.verify = verify

    def _skip(self, k):
        if k <= 0:
            return
        self.skipped_bytes += k
        self.resyncs += 1
        del self.buf[:k]

    def feed(self, data: bytes):
        """喂入原始字节，产出 Dev113pFrame 列表。"""
        self.buf.extend(data)
        self.total += len(data)
        out = []
        buf = self.buf
        while True:
            i = buf.find(SYNC)
            if i < 0:
                # 保留末尾 1 字节（可能是 0x55 的前半），其余丢弃
                if len(buf) > 1:
                    self._skip(len(buf) - 1)
                break
            if i > 0:
                self._skip(i)
                continue
            if len(buf) < FRAME_LEN:
                break
            if buf[4] != POINTS_MINUS_ONE:
                self.bad_packets += 1
                self._skip(1)
                continue
            fr = parse_frame(buf, 0, seq=self.frames, verify=self.verify)
            if fr is None:
                self.bad_packets += 1
                if self.verify:
                    want = (buf[TAIL_CHECKSUM_OFF] << 8) | buf[TAIL_CHECKSUM_OFF + 1]
                    if tail_checksum(bytes(buf[PAYLOAD_OFF:PAYLOAD_OFF + PAYLOAD_LEN])) != want:
                        self.bad_checksum += 1
                self._skip(1)
                continue
            del buf[:FRAME_LEN]
            self.frames += 1
            out.append(fr)
        return out

    def diag(self):
        return {
            'frames': self.frames,
            'bad': self.bad_packets,
            'bad_checksum': self.bad_checksum,
            'skipped': self.skipped_bytes,
            'resyncs': self.resyncs,
            'bytes': self.total,
        }


class FrameAssembler:
    """把 chirp/RX 数据块拼成一帧 (4 RX × 64 chirp × 32 距离单元)。

    帧边界判据 = **帧尾标签的雷达帧序号变化**（frame_seq，mod 16）。
    实测该序号在组内恒定、每到 chirp 63→0 就 +1，因此比「chirp 号回绕」
    更可靠（不需要假设 chirp 单调）。

    RX 槽位取帧尾标签 `fr.rx`（bits[3:2]，实测可靠），
    只有在标签不可用时才回退到「同一 counter 内的到达次序」。

    ⚠ 回退模式下的已知缺陷（标签模式没有）：若某 chirp 的**第 1 帧丢失**，
    到达次序会把 rx1/rx2/rx3 的数据错填进槽位 0/1/2 —— 因此标签可用时必须用标签。

    诊断计数：
      dropped     —— 结算时未填满（丢过 RX 帧）的雷达帧数
      gap_frames  —— frame_seq 跳跃 → 整个雷达帧都没收到的次数
      gap_groups  —— 同一雷达帧内 chirp 号不连续 → 丢了整组 chirp 的次数
      overrun     —— 同一槽位重复到达 / 槽位越界的次数
    """

    def __init__(self, chirps=CHIRPS_PER_FRAME, bins=BINS_PER_CHIRP, rx=RX_COUNT):
        self.chirps = chirps
        self.bins = bins
        self.rx = rx
        self.data = np.zeros((rx, chirps, bins), dtype=np.complex64)
        self.filled = np.zeros((rx, chirps), dtype=bool)
        self.frames = 0
        self.dropped = 0
        self.cur_counter = None
        self.cur_frame = None
        self.cur_n = 0
        self.last_chirp = -1
        self.overrun = 0
        self.gap_frames = 0
        self.gap_groups = 0
        self.settled = 0

    def push(self, fr: Dev113pFrame):
        """返回 (z0, z1, z2, z3) 当一帧收齐，否则 None。"""
        if fr.chirp >= self.chirps:
            return None
        res = None
        if fr.frame_seq != self.cur_frame:
            if self.cur_frame is not None:
                # 结算上一雷达帧
                self.settled += 1
                if self.filled.all():
                    res = self.data.copy()
                    self.frames += 1
                else:
                    self.dropped += 1
                self.filled[:] = False
                if ((fr.frame_seq - self.cur_frame) & FRAME_TAG_MASK) != 1:
                    self.gap_frames += 1
            self.cur_frame = fr.frame_seq
            self.last_chirp = -1
        if fr.counter != self.cur_counter:
            self.cur_counter = fr.counter
            self.cur_n = 0
            if self.last_chirp >= 0 and \
                    ((fr.chirp - self.last_chirp) & CHIRP_MASK) != 1:
                self.gap_groups += 1
            self.last_chirp = fr.chirp
        if 0 <= fr.rx < self.rx:
            slot = fr.rx
        else:
            slot = self.cur_n
        self.cur_n += 1
        if slot < self.rx:
            if self.filled[slot, fr.chirp]:
                # 同槽位重复到达（丢帧后重传/乱序）→ 记一次 overrun 但保留数据
                self.overrun += 1
            self.data[slot, fr.chirp, :] = fr.bins
            self.filled[slot, fr.chirp] = True
        else:
            self.overrun += 1
        if res is not None:
            return tuple(res[k] for k in range(self.rx))
        return None

    def diag(self):
        return {
            'frames': self.frames,
            'dropped': self.dropped,
            'overrun': self.overrun,
            'gap_frames': self.gap_frames,
            'gap_groups': self.gap_groups,
            'settled': self.settled,
        }
