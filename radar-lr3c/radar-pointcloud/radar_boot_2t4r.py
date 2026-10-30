#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""纯 Python 启动 2T4R(DEV113P) —— 重放厂商 ONELAB 的启动帧序列。

为什么需要它
------------
2T4R 上电**不自启**：插上后 COM4 静默 0 字节，必须先写入
Env / ModCal / Stop / Config / Start 五段寄存器，芯片才开始往串口吐 136 B 帧。
历史上只能靠 ONELAB 唤醒一次（唤醒状态保持到断电为止）。

本工具把厂商日志里那次唤醒的**原始下行帧**逐字节重放，
让脚本自己完成启动，不再依赖 ONELAB。

序列从哪来
----------
`tools/extract_vendor_boot.py` 从 ONELAB 的 `LogFile_*.txt` 里按协议语义
自动定位「一次完整启动」（Open → 清段 → Env/ModCal/Stop/Config/Start → Close），
输出 `.deepworks/tmp/boot_seq_2t4r.json`。
该 JSON 含厂商寄存器配置，**不入库、不外传**。

已验证 / 未验证（证据纪律）
--------------------------
- 帧格式（HEAD/LEN/CMD/TAIL、A32D32 8 B/条、chipAddr=0x0024）：
  **已由厂商日志实测确认**（见 docs/radar-uplink-dev113p-2t4r.md §7.7）。
- 序列的段序与条数（Env 68 / ModCal 60 / Stop 6 / Config 155 / Start 9 = 298）：
  **已与厂商日志逐帧对账一致**。
- 「重放这套序列能让本机 2T4R 出流」：**尚未在实机验证**，
  首次使用请手边能重新插拔 USB。

安全边界
--------
- 默认 dry-run：只做离线校验，不打开串口
- 真发必须显式 `--send`
- 不写任何本地文件、不碰 Radar_Config.ini
- 原样重放，不重新打包（避免帧边界/字节序/长度出错）

用法
----
  python radar_boot_2t4r.py --dry-run
  python radar_boot_2t4r.py --send --port COM4
  python radar_boot_2t4r.py --send --port COM4 --verify
"""
import argparse
import json
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

HEAD = b'\xfd\xfc\xfb\xfa'
TAIL = b'\x04\x03\x02\x01'

DEFAULT_SEQ = os.path.join(
    os.path.dirname(HERE), '.deepworks', 'tmp', 'boot_seq_2t4r.json')

SEC_NAME = {0x0000: 'EnvSetup', 0x0001: 'Stop', 0x0002: 'Config',
            0x0003: 'Start', 0x0004: 'Clear', 0x0010: 'ModCal'}


def load_seq(path):
    if not os.path.isfile(path):
        raise SystemExit(
            '找不到启动序列文件:\n  %s\n\n'
            '先运行提取脚本（从厂商 ONELAB 日志生成）:\n'
            '  python tools/extract_vendor_boot.py\n' % path)
    with open(path, 'r', encoding='utf-8') as f:
        obj = json.load(f)
    frames = [bytes.fromhex(x['raw']) for x in obj.get('frames', [])]
    if not frames:
        raise SystemExit('序列文件里没有帧: %s' % path)
    return obj, frames


def check_frame(raw):
    """校验一帧的结构完整性（不设 54 字节上限，2T4R 帧最长 510 B）。

    返回 (ok, 说明)。
    """
    if len(raw) < 12:
        return False, '过短 %d B' % len(raw)
    if raw[:4] != HEAD:
        return False, '帧头不符'
    if raw[-4:] != TAIL:
        return False, '帧尾不符'
    ln = struct.unpack_from('<H', raw, 4)[0]
    if len(raw) != ln + 10:
        return False, '长度不符: len字段=%d 实际=%d' % (ln, len(raw) - 10)
    cmd = struct.unpack_from('<H', raw, 6)[0]
    if cmd not in (0x00FF, 0x00FE, 0x0000, 0x0001, 0x0002, 0x0003,
                   0x0004, 0x0005, 0x0011, 0x0012, 0x0014, 0x0016,
                   0x0020, 0x0024):
        return False, '未知 cmd=0x%04X' % cmd
    return True, label_of(cmd, raw[8:8 + ln - 2])


def label_of(cmd, payload):
    if cmd == 0x00FF:
        return 'OPEN_CMD_MODE'
    if cmd == 0x00FE:
        return 'CLOSE_CMD_MODE'
    if cmd == 0x0012 and len(payload) >= 6:
        _param, sec, cnt = struct.unpack_from('<HHH', payload, 0)
        return 'SYS_PARAM %s(0x%04X) cnt=%d' % (
            SEC_NAME.get(sec, '?'), sec, cnt)
    if cmd == 0x0001 and len(payload) >= 2:
        chip = struct.unpack_from('<H', payload, 0)[0]
        return 'WRITE_REG chip=0x%04X n=%d' % (chip, (len(payload) - 2) // 8)
    return 'cmd=0x%04X plen=%d' % (cmd, len(payload))


def read_ack(ser, timeout):
    """读完这段时间内到达的所有字节。

    注意：2T4R 回包存在两种形态——
      · 8 位紧凑流：FD FC FB FA | len | cmd | status | ... | 04 03 02 01
      · 32 位字对齐：每个字段占 4 字节 LE
    因此不能只用 TAIL 判断结束，改为「等到超时且缓冲已清空」。
    """
    buf = b''
    t0 = time.time()
    idle = 0
    while time.time() - t0 < timeout:
        n = ser.in_waiting
        if n:
            buf += ser.read(n)
            idle = 0
        else:
            idle += 1
            if buf and idle >= 3:
                break
            time.sleep(0.01)
    return buf


def parse_ack_compact(raw):
    """解析 8 位紧凑回包：HEAD | len | cmd | data | TAIL。

    只有 0x00FF(OPEN) / 0x00FE(CLOSE) / 0x0001(WRITE_REG) 的 data 才是明确的
    status（1=OK）；其他命令（如 0x0011 / 0x0005）返回的是数据（如模组 ID 0x0FA7），
    不能当状态用，这里返回 ok=None 表示「无法判定」。
    """
    if len(raw) < 12 or raw[:4] != HEAD or raw[-4:] != TAIL:
        return None
    ln = struct.unpack_from('<H', raw, 4)[0]
    if len(raw) != ln + 10:
        return None
    cmd = struct.unpack_from('<H', raw, 6)[0]
    data = struct.unpack_from('<H', raw, 8)[0]
    ok = None
    # 回包 cmd = 请求 cmd | 0x0100（如 0x00FF -> 0x01FF），所以取低字节再比
    if (cmd & 0x00FF) in (0x00FF, 0x00FE, 0x0001):
        ok = (data == 1)
    return {'cmd': cmd, 'status': data, 'ok': ok}


def parse_ack(raw):
    """解析 ONELAB 回包（2T4R 实测格式）。

    实测结构：**32 位字对齐**，每个字段占 4 字节（小端）：
        words[0:4] = FD FC FB FA
        words[4]   = len
        words[5]   = 保留(0)
        words[6]   = cmd
        words[7]   = status      ← 1 = OK
        ...
    例：READ_VERSION 回包 words[6]=0x0000 words[7]=0x1，数据为 "VA1.0.0"。

    注意：radar_hwstop.parse_ack 按 8 位字节流解析，对 2T4R 会整体错位
    （会把模组 ID 0x0FA7 误读成 status=4007），所以这里不能用它。
    """
    if len(raw) < 36 or len(raw) % 4:
        return None
    w = [struct.unpack_from('<I', raw, i)[0] for i in range(0, len(raw), 4)]
    if w[:4] != [0xFD, 0xFC, 0xFB, 0xFA]:
        return None
    if len(w) < 9:
        return None
    return {'cmd': w[6], 'status': w[7], 'ok': w[7] == 1}


def verify_stream(ser, seconds):
    """发完后读 seconds 秒，用 DEV113P 解析器统计出流情况。"""
    import radar_protocol_dev113p as P
    parser = P.StreamParser()
    total = 0
    t0 = time.time()
    while time.time() - t0 < seconds:
        chunk = ser.read(4096)
        if chunk:
            total += len(chunk)
            parser.feed(chunk)
    return {
        'bytes': total,
        'frames': parser.frames,
        'bad': parser.bad_packets,
        'bad_checksum': parser.bad_checksum,
    }


def do_dry_run(obj, frames):
    print('=' * 68)
    print('  2T4R 启动序列 — 离线校验（不打开串口）')
    print('=' * 68)
    print('  来源日志: %s' % obj.get('source_log'))
    print('  chipAddr: 0x%04X   波特率: %d' % (
        obj.get('chip_addr', 0), obj.get('baud', 1500000)))
    print('  帧数: %d   总字节: %d\n' % (
        len(frames), sum(len(f) for f in frames)))

    bad = 0
    for i, raw in enumerate(frames):
        ok, desc = check_frame(raw)
        if not ok:
            bad += 1
            desc = '✘ ' + desc
        else:
            desc = '✔ ' + desc
        print('  %2d  %3d B  %s' % (i, len(raw), desc))

    print('\n  结构校验: %d/%d 通过' % (len(frames) - bad, len(frames)))
    if bad:
        print('  !! 有 %d 帧不合格，禁止真发' % bad)
        return 1
    print('  OK —— 帧结构全部合格，可以 --send 真发')
    return 0


def send_boot(ser, frames, gap=0.03, ack_timeout=0.12, log=None):
    """在**已打开**的串口上重放启动序列。

    与 do_send() 的区别：不负责打开/关闭串口、不打印长表格、
    返回结构化结果，便于 SerialSource 在自动重连时复用。

    参数
      ser          : 已打开的 pyserial 实例
      frames       : list[bytes]（load_seq() 的第二个返回值）
      gap          : 帧间间隔（秒）
      ack_timeout  : 每帧等回包的时长（秒）0 表示不等
      log          : 可选的模块日志回调

    返回 dict(sent, skipped, acks, no_ack, elapsed)。
    ACK 只作中性计数，不做 OK/FAIL 判定（回包语义不统一）。
    """
    say = log or (lambda _m: None)
    sent = skipped = acks = no_ack = 0
    t0 = time.time()
    for i, raw in enumerate(frames):
        ok, desc = check_frame(raw)
        if not ok:
            skipped += 1
            say('[boot] 跳过第 %d 帧（结构不合格）: %s' % (i, desc))
            continue
        ser.write(raw)
        ser.flush()
        sent += 1
        if ack_timeout > 0:
            if read_ack(ser, ack_timeout):
                acks += 1
            else:
                no_ack += 1
        time.sleep(gap)
    return {'sent': sent, 'skipped': skipped, 'acks': acks,
            'no_ack': no_ack, 'elapsed': time.time() - t0}


def do_send(obj, frames, args):
    import serial

    print('=' * 68)
    print('  2T4R 启动序列 — 真机重放')
    print('=' * 68)
    print('  !! 未验证路径：首次实机请手边能重新插拔 USB')
    print('  端口: %s @ %d   gap=%.3fs  ack_timeout=%.2fs' % (
        args.port, args.baud, args.gap, args.ack_timeout))
    print('  帧数: %d   总字节: %d\n' % (
        len(frames), sum(len(f) for f in frames)))

    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.05)
    except Exception as exc:
        print('!! 打不开 %s: %s' % (args.port, exc))
        print('   检查：① 板子插好 ② 没有别的程序占着（server.py / ONELAB）')
        return 2

    ok_cnt = 0
    ack_cnt = 0
    no_ack = 0
    fail_cnt = 0
    try:
        ser.reset_input_buffer()
        pre = ser.read(ser.in_waiting or 0)
        print('  发送前缓冲: %d 字节（若明显 >0 说明已经在出流）\n' % len(pre))

        for i, raw in enumerate(frames):
            ok, desc = check_frame(raw)
            if not ok:
                print('  %2d  跳过（结构不合格）: %s' % (i, desc))
                continue
            ser.write(raw)
            ser.flush()

            if args.no_ack:
                print('  %2d  %-44s %3d B' % (i, desc, len(raw)))
                time.sleep(args.gap)
                continue

            ack = read_ack(ser, args.ack_timeout)
            # ACK 只做中性显示：不同命令回包的数据字段含义不同
            # （OPEN 返 1=OK，CLOSE 返 0=OK），强行判 OK/FAIL 只会误导。
            # 最终判据一律交给「是否真的出流」。
            if ack:
                ack_cnt += 1
                tag = '回包 %dB' % len(ack)
            else:
                no_ack += 1
                tag = '(无回包)'
            print('  %2d  %-44s %3d B  %s' % (i, desc, len(raw), tag))
            time.sleep(args.gap)
    finally:
        ser.close()

    print('\n  完成：帧=%d  有回包=%d  无回包=%d' % (
        len(frames), ack_cnt, no_ack))
    print('  ACK 仅表示「命令已被接收」，不代表启动成功。')
    print('  最终判据看 --verify 的出流结果。')
    return 0


def do_verify(args):
    import serial
    print('\n' + '=' * 68)
    print('  出流验证（%s，%.1f 秒）' % (args.port, args.verify_seconds))
    print('=' * 68)
    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.2)
    except Exception as exc:
        print('!! 打不开 %s: %s' % (args.port, exc))
        return 2
    try:
        st = verify_stream(ser, args.verify_seconds)
    finally:
        ser.close()

    rate = st['bytes'] / args.verify_seconds / 1024.0
    print('  收到字节   : %d  (%.1f kB/s)' % (st['bytes'], rate))
    print('  有效帧     : %d' % st['frames'])
    print('  坏包/坏校验: %d / %d' % (st['bad'], st['bad_checksum']))
    if st['frames'] == 0:
        print('\n  ✘ 没有解析到 DEV113P 帧 —— 启动未生效')
        print('    可尝试：拔插 USB 后重跑 --send；或临时用 ONELAB 唤醒一次')
        return 1
    fps = st['frames'] / args.verify_seconds
    print('\n  ✔ 出流正常：%.2f 包/s' % fps)
    return 0


def main():
    ap = argparse.ArgumentParser(
        description='纯 Python 启动 2T4R(DEV113P)：重放厂商 ONELAB 启动序列')
    ap.add_argument('--seq', default=DEFAULT_SEQ,
                    help='启动序列 JSON（默认 .deepworks/tmp/boot_seq_2t4r.json）')
    ap.add_argument('--port', default='COM4', help='串口，默认 COM4')
    ap.add_argument('--baud', type=int, default=1500000, help='波特率，默认 1500000')
    ap.add_argument('--dry-run', action='store_true', help='只做离线校验（默认行为）')
    ap.add_argument('--send', action='store_true', help='真机发送（需显式指定）')
    ap.add_argument('--verify', action='store_true', help='发送后验证是否出流')
    ap.add_argument('--no-ack', action='store_true', help='不等 ACK，快速连发')
    ap.add_argument('--gap', type=float, default=0.05, help='帧间隔秒，默认 0.05')
    ap.add_argument('--ack-timeout', type=float, default=0.4, help='等 ACK 超时秒')
    ap.add_argument('--verify-seconds', type=float, default=3.0, help='验证时长秒')
    args = ap.parse_args()

    obj, frames = load_seq(args.seq)

    if args.verify and not args.send:
        return do_verify(args)
    if not args.send:
        return do_dry_run(obj, frames)

    rc = do_send(obj, frames, args)
    if rc != 0:
        return rc
    if args.verify:
        return do_verify(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
