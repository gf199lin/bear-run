# -*- coding: utf-8 -*-
"""2T4R 串口波特率提频验证：1.5 Mbps → 3 Mbps（或指定值）。

回答的问题只有一个：
  把 UART 波特率翻倍后，130.44 ms 配置还能不能出「完整 chirp」的帧？

背景见 docs/2t4r-framerate-limit.md §5：帧周期 130.44 ms 需要 ≈267 kB/s，
而 1.5 Mbps 链路实测只能到 ≈138 kB/s，ONELAB 因此每帧报 Chirp NOT complete。

用法
----
  # 只看计划，不碰串口
  python tools/uart_baud_probe.py --dry-run

  # 真发：先按 1.5M 下发 130ms 配置 → 切 3M → 读 8 秒 → 统计 → 恢复 1.5M
  python tools/uart_baud_probe.py --send --port COM4 --baud 3000000

  # 只测当前设备状态，不重下发配置
  python tools/uart_baud_probe.py --send --no-config

安全边界
--------
- 默认 dry-run。真发必须 --send。
- 配置序列含厂商寄存器值，只从本地 .deepworks/tmp 读，不入库、不打印。
- 结束时尽力把芯片改回 1500000；失败会明确提示「拔插 USB 复位」。
"""
import argparse
import json
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SEQ_130 = os.path.join(ROOT, '.deepworks', 'tmp', 'boot_seq_2t4r_130ms.json')
SEQ_BASE = os.path.join(ROOT, '.deepworks', 'tmp', 'boot_seq_2t4r.json')

HEAD = b'\xfd\xfc\xfb\xfa'
TAIL = b'\x04\x03\x02\x01'
FRAME_BYTES = 136
BINS = 32
EXPECT_CHIRP_PER_S = 64 / 0.130440      # 490.6

OK, WARN, BAD = '[ OK ]', '[WARN]', '[FAIL]'


def ff(tag, msg):
    print('%s %s' % (tag, msg))


# ---------- 串口 ----------

def open_port(serial, port, baud):
    ser = serial.Serial(port, baud, timeout=0.05)
    # FT232 的命令转发受 DTR/RTS 门控：不拉高时端口能开但芯片不响应/不出流。
    for attr in ('dtr', 'rts'):
        try:
            setattr(ser, attr, True)
        except Exception:
            pass
    time.sleep(0.15)
    ser.reset_input_buffer()
    return ser


def read_ack(ser, timeout=0.5):
    buf = b''
    t0 = time.time()
    while time.time() - t0 < timeout:
        n = ser.in_waiting
        chunk = ser.read(n if n else 1)
        if chunk:
            buf += chunk
            if buf.endswith(TAIL):
                return buf
    return buf


def frame_cmd(raw):
    if len(raw) < 10 or raw[:4] != HEAD:
        return None, b''
    ln = struct.unpack_from('<H', raw, 4)[0]
    cmd = struct.unpack_from('<H', raw, 6)[0]
    return cmd, raw[8:8 + ln - 2]


def set_baud_frame(baud):
    """ONELAB 的 SetUartBaudrate 命令（日志实测 0x0012 + 0x0008 + u32 波特率）。"""
    return (HEAD + struct.pack('<HH', 8, 0x0012) + struct.pack('<H', 8)
            + struct.pack('<I', baud) + TAIL)


def load_seq(seq_path):
    if not os.path.isfile(seq_path):
        raise SystemExit('找不到配置序列: %s' % seq_path)
    obj = json.load(open(seq_path, encoding='utf-8'))
    return ([bytes.fromhex(x['raw']) for x in obj['frames']],
            obj.get('session_time', '?'))


def split_at_start(frames):
    """把序列切成 [配置段, Start 及之后]。"""
    for idx, raw in enumerate(frames):
        cmd, pl = frame_cmd(raw)
        if cmd == 0x0012 and len(pl) >= 6:
            sec = struct.unpack_from('<H', pl, 2)[0]
            if sec == 0x0003:
                return frames[:idx], frames[idx:]
    return frames, []


def send_config(ser, frames, gap=0.03, ack_timeout=0.12):
    sent = acks = 0
    for raw in frames:
        ser.write(raw)
        ser.flush()
        sent += 1
        if read_ack(ser, ack_timeout):
            acks += 1
        time.sleep(gap)
    return sent, acks


def capture(ser, seconds):
    buf = b''
    t0 = time.time()
    while time.time() - t0 < seconds:
        chunk = ser.read(8192)
        if chunk:
            buf += chunk
    return buf, time.time() - t0


# ---------- 解析 ----------

def parse_stream(buf):
    """扫描 DEV113P 帧，帧长随 bin 数变化。

    帧结构：55 AA | counter(2) | bins-1 | bins×4B | 校验(2) | tag(1)
    32 bin → 136 B；16 bin（FFT 输出列=16）→ 72 B。
    返回 (frames, bad_chk, skipped)。
    """
    frames = []
    bad = 0
    skipped = 0
    i = 0
    n = len(buf)
    while i + 8 <= n:
        if buf[i] == 0x55 and buf[i + 1] == 0xAA:
            bins = buf[i + 4] + 1
            flen = 8 + 4 * bins
            if bins in (8, 16, 32, 64) and i + flen <= n:
                f = buf[i:i + flen]
                s = 0
                for k in range(5, 5 + 4 * bins, 2):
                    v = (f[k] << 8) | f[k + 1]
                    if v >= 0x8000:
                        v -= 0x10000
                    s += v
                ck = 5 + 4 * bins
                chk = (f[ck] << 8) | f[ck + 1]
                if (s & 0xFFFF) == chk:
                    counter = (f[2] << 8) | f[3]
                    tag = f[ck + 2]
                    frames.append({
                        'chirp': (counter & 0x7FFF) >> 4,
                        'rx': (tag >> 2) & 0x03,
                        'seq': (tag >> 4) & 0x0F,
                        'counter': counter,
                        'bins': bins,
                    })
                    i += flen
                    continue
                bad += 1
        skipped += 1
        i += 1
    return frames, bad, skipped


def summarize(buf, seconds, target_chirp=EXPECT_CHIRP_PER_S):
    frames, bad, skipped = parse_stream(buf)
    kb = len(buf) / 1024.0 / max(seconds, 1e-9)
    frames_s = len(frames) / max(seconds, 1e-9)
    # 4RX 完整时，每 chirp 应有 4 帧
    expected_frames_s = 4 * target_chirp
    coverage = frames_s / expected_frames_s if expected_frames_s else 0.0
    per_chirp = frames_s / target_chirp if target_chirp else 0.0
    bins = frames[0]['bins'] if frames else 0

    # counter 增量分布：恒定且小 ⇒ 没有整帧丢失
    from collections import Counter as _C
    deltas = _C()
    prev = None
    for f in frames:
        if prev is not None:
            deltas[(f['counter'] - prev) & 0x7FFF] += 1
        prev = f['counter']
    delta_mode, delta_n = (deltas.most_common(1)[0] if deltas else (0, 0))
    delta_ratio = delta_n / max(sum(deltas.values()), 1)

    return {
        'bytes': len(buf), 'kb_s': kb, 'frames': len(frames),
        'bad_chk': bad, 'skipped': skipped, 'bins': bins,
        'frames_s': frames_s, 'expected_frames_s': expected_frames_s,
        'coverage': coverage, 'per_chirp': per_chirp,
        'delta_mode': delta_mode, 'delta_ratio': delta_ratio,
        'chirp_s': frames_s / 4.0,
    }


def report(st, baud, seconds):
    print('-' * 68)
    print('  实测（%d baud，%.1f 秒）' % (baud, seconds))
    print('-' * 68)
    print('  接收字节    : %d  (%.1f kB/s)' % (st['bytes'], st['kb_s']))
    print('  帧长        : %d B（%d bin）' % (8 + 4 * st['bins'], st['bins'])
          if st['bins'] else '  帧长        : 未识别')
    print('  有效帧      : %d  (%.0f 帧/s，4RX 完整应为 %.0f)'
          % (st['frames'], st['frames_s'], st['expected_frames_s']))
    print('  坏校验/跳过 : %d / %d' % (st['bad_chk'], st['skipped']))
    print('  4RX 覆盖率  : %.0f%%（每 chirp %.2f 帧）'
          % (st['coverage'] * 100, st['per_chirp']))
    print('  counter 步进: 0x%X（占 %.0f%%，恒定=无整帧丢失）'
          % (st['delta_mode'], st['delta_ratio'] * 100))
    print()
    cov = st['coverage']
    if st['frames'] == 0:
        ff(BAD, '没有解析到任何帧 —— 芯片没切波特率，或停在异常状态')
        print('       处理：拔插 USB 复位，再跑 check_radar.py 确认能出流')
        return False
    if st['bad_chk'] > st['frames'] * 0.1:
        ff(BAD, '坏校验占比 %.0f%% —— 波特率不匹配，收到的是错位字节'
           % (100.0 * st['bad_chk'] / (st['frames'] + st['bad_chk'])))
        return False
    if cov >= 0.85:
        ff(OK, '4RX 覆盖 %.0f%%、每 chirp %.2f 帧 —— 帧完整'
           % (cov * 100, st['per_chirp']))
        return True
    if cov >= 0.4:
        ff(WARN, '4RX 覆盖只有 %.0f%%（每 chirp %.2f 帧，缺 %.1f 个 RX）'
           % (cov * 100, st['per_chirp'], 4 - st['per_chirp']))
        print('       时间轴连续 ⇒ 不是带宽瓶颈，是 RX 通道数配置不对。')
        return False
    ff(BAD, '4RX 覆盖仅 %.0f%% —— 数据被大量截断' % (cov * 100))
    return False


# ---------- 主流程 ----------

def do_send(args):
    import serial

    seq = SEQ_130 if args.config else None
    frames, when = ([], '?')
    if seq:
        frames, when = load_seq(seq)
    cfg_part, start_part = split_at_start(frames)

    print('=' * 68)
    print('  2T4R 波特率提频验证')
    print('=' * 68)
    print('  端口 %s   起始 %d → 目标 %d' % (args.port, args.start_baud, args.baud))
    print('  配置 %s' % (os.path.relpath(seq, ROOT) if seq else '不重下发（--no-config）'))
    if frames:
        print('  序列 %d 帧（配置段 %d / Start 段 %d，源会话 %s）'
              % (len(frames), len(cfg_part), len(start_part), when))
    print()

    ser = None
    base_st = None
    try:
        # A) 完整下发 + 起始波特率基线
        ser = open_port(serial, args.port, args.start_baud)
        ff(OK, '串口已打开 @ %d' % args.start_baud)
        if frames:
            sent, acks = send_config(ser, frames, args.gap, args.ack_timeout)
            ff(OK, '完整序列已下发：%d 帧，回包 %d' % (sent, acks))
            time.sleep(0.3)
            ser.reset_input_buffer()
        if args.baseline_seconds > 0:
            print('\n  [A] %d 基线：采样 %.1f 秒'
                  % (args.start_baud, args.baseline_seconds))
            buf, el = capture(ser, args.baseline_seconds)
            base_st = summarize(buf, el)
            report(base_st, args.start_baud, el)
        ser.close()
        ser = None
        time.sleep(0.2)

        # B) 只发配置段（停在 Start 之前）→ 改波特率。此时芯片不出流，ACK 干净。
        ser = open_port(serial, args.port, args.start_baud)
        if cfg_part:
            sent, acks = send_config(ser, cfg_part, args.gap, args.ack_timeout)
            ff(OK, '配置段已下发（未 Start）：%d 帧，回包 %d' % (sent, acks))
            time.sleep(0.2)
            ser.reset_input_buffer()
        raw = set_baud_frame(args.baud)
        print('\n  下发 SetUartBaudrate(%d): %s' % (args.baud, raw.hex()))
        ser.write(raw)
        ser.flush()
        ack = read_ack(ser, 1.0)
        if not ack:
            ff(WARN, '没收到 ACK —— 继续按目标波特率试读')
        else:
            cmd, pl = frame_cmd(ack)
            status = struct.unpack_from('<H', pl, 0)[0] if len(pl) >= 2 else -1
            ff(OK if status == 0 else WARN,
               'ACK cmd=0x%04X status=0x%04X' % (cmd or 0, status))
        if not args.inplace:
            ser.close()
            ser = None
            time.sleep(0.2)

        # C) 切到目标波特率 → 再发 Start 段 → 采集
        if args.inplace:
            ser.baudrate = args.baud
            time.sleep(0.15)
            ser.reset_input_buffer()
            ff(OK, '同句柄改速到 %d（不重开）' % args.baud)
        else:
            ser.close()
            ser = None
            time.sleep(0.2)
            ser = open_port(serial, args.port, args.baud)
            ff(OK, '已切到 %d（重开串口）' % args.baud)
        if start_part:
            sent, acks = send_config(ser, start_part, args.gap, args.ack_timeout)
            ff(OK if acks else WARN,
               'Start 段已下发：%d 帧，回包 %d' % (sent, acks))
            time.sleep(0.2)
        buf, elapsed = capture(ser, args.seconds)
        ser.close()
        ser = None

        st = summarize(buf, elapsed)
        ok = report(st, args.baud, elapsed)

        if base_st and base_st['frames']:
            print('-' * 68)
            print('  A/B 对照（同一份 130ms 配置，只差波特率）')
            print('-' * 68)
            print('  %-10s %10s %10s %8s' % ('', '%.1fM' % (args.start_baud / 1e6),
                                            '%.1fM' % (args.baud / 1e6), '变化'))
            print('  %-10s %10.1f %10.1f %+7.0f%%'
                  % ('kB/s', base_st['kb_s'], st['kb_s'],
                     100.0 * (st['kb_s'] - base_st['kb_s']) / max(base_st['kb_s'], 1e-9)))
            print('  %-10s %10.0f %10.0f %+7.0f%%'
                  % ('chirp/s', base_st['chirp_s'], st['chirp_s'],
                     100.0 * (st['chirp_s'] - base_st['chirp_s']) / max(base_st['chirp_s'], 1e-9)))
            print('  %-10s %10.0f%% %9.0f%%'
                  % ('chirp 覆盖', base_st['coverage'] * 100, st['coverage'] * 100))
            print('  %-10s %10.2f %10.2f'
                  % ('帧/chirp', base_st['per_chirp'], st['per_chirp']))
            print('  %-10s %10d %10d' % ('坏校验', base_st['bad_chk'], st['bad_chk']))
            print()

        # 尽力恢复 1.5M
        if args.baud != 1500000:
            print()
            try:
                ser = open_port(serial, args.port, args.baud)
                ser.write(set_baud_frame(1500000))
                ser.flush()
                ack = read_ack(ser, 1.0)
                ff(OK if ack else WARN,
                   '已请求恢复 1500000' if ack else '恢复 1500000 无回包，可能需拔插 USB')
            except Exception as exc:
                ff(WARN, '恢复 1500000 失败: %s' % exc)
            finally:
                if ser:
                    ser.close()
                    ser = None
        return 0 if ok else 1
    finally:
        if ser:
            try:
                ser.close()
            except Exception:
                pass


def do_dry(args):
    print('=' * 68)
    print('  2T4R 波特率提频验证 — 计划（不打开串口）')
    print('=' * 68)
    print('  串口      : %s' % args.port)
    print('  起始波特率: %d' % args.start_baud)
    print('  目标波特率: %d' % args.baud)
    print('  配置序列  : %s' % (os.path.relpath(SEQ_130, ROOT)
                               if args.config else '不重下发'))
    if args.config:
        if not os.path.isfile(SEQ_130):
            ff(BAD, '缺少 %s（先跑 .deepworks/tmp/extract_130ms.py）' % SEQ_130)
            return 2
        obj = json.load(open(SEQ_130, encoding='utf-8'))
        ff(OK, '序列存在：%d 帧（源会话 %s）'
           % (len(obj['frames']), obj.get('session_time')))
    print('  采样时长  : %.1f 秒' % args.seconds)
    print()
    print('  下发帧    : %s' % set_baud_frame(args.baud).hex())
    print('  判据      : 4RX 覆盖率 ≥ 85%（约 1667 帧/s）')
    print()
    print('  真发请加 --send')
    return 0


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = argparse.ArgumentParser(description='2T4R 串口波特率提频验证')
    ap.add_argument('--port', default='COM4')
    ap.add_argument('--baud', type=int, default=3000000, help='目标波特率')
    ap.add_argument('--start-baud', type=int, default=1500000, help='起始波特率')
    ap.add_argument('--seconds', type=float, default=8.0)
    ap.add_argument('--baseline-seconds', type=float, default=3.0,
                    help='切频前先测 1.5M 基线的秒数，0=跳过')
    ap.add_argument('--inplace', action='store_true',
                    help='同句柄改波特率（不重开），对齐 ONELAB 行为')
    ap.add_argument('--send', action='store_true', help='真发（默认只打印计划）')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-config', dest='config', action='store_false',
                    help='不重下发 130ms 配置')
    ap.add_argument('--gap', type=float, default=0.03)
    ap.add_argument('--ack-timeout', type=float, default=0.12)
    ap.set_defaults(config=True)
    args = ap.parse_args()
    return do_send(args) if args.send else do_dry(args)


if __name__ == '__main__':
    sys.exit(main())
