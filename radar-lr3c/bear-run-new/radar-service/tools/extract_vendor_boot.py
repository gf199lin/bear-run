#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从厂商 ONELAB 日志里提取 2T4R(DEV113P) 的启动帧序列。

做的是「按协议语义定位一次完整启动」，不靠硬编码行号：
  1. 找 Start 段声明：cmd=0x0012, sec=0x0003, cnt=9
  2. 向前找最近的 cmd=0x00FF（打开命令模式）
  3. 向后找最近的 cmd=0x00FE（关闭命令模式）
这一段即一次完整启动，输出其**原始帧字节**。

为什么原样提取而不是重新打包
----------------------------
2T4R 用 A32D32（addr u32 + val u32，8 B/条），单帧最长 510 B，
远超 1T2R 工具里的 54 B 缓冲假设。原样重放可以完全绕开
帧边界、长度、字节序这些容易出错的细节。

输出
----
默认写 `<项目根>/.deepworks/tmp/boot_seq_2t4r.json`。
该文件含厂商寄存器配置，**不要提交、不要外传**（.deepworks/tmp 已在 .gitignore）。

用法
----
  python tools/extract_vendor_boot.py
  python tools/extract_vendor_boot.py --log "D:\\...\\LogFile_2026_10_19_14_11_10.txt"
  python tools/extract_vendor_boot.py --logdir "D:\\...\\ONELAB...\\Log"
"""
import argparse
import json
import os
import re
import struct
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT_DIR = os.path.join(ROOT, '.deepworks', 'tmp')

HEAD = b'\xfd\xfc\xfb\xfa'
TAIL = b'\x04\x03\x02\x01'

SEC_NAME = {0x0000: 'EnvSetup', 0x0001: 'Stop', 0x0002: 'Config',
            0x0003: 'Start', 0x0004: 'Clear', 0x0010: 'ModCal'}

# decode_registers.py 里记录的、确认含 2T4R 完整配置的那份日志
DEFAULT_LOG_NAME = 'LogFile_2026_10_19_14_11_10.txt'

RE_METHOD = re.compile(r'CRadarProtocolDevice::(\w+)_(Send|Recv):')
RE_HEX = re.compile(r'^\s*((?:[0-9A-Fa-f]{2}\s+)*[0-9A-Fa-f]{2})\s*$')


def find_log(logdir, name):
    path = os.path.join(logdir, name)
    if os.path.isfile(path):
        return path
    if not os.path.isdir(logdir):
        return None
    cands = [f for f in os.listdir(logdir)
             if f.startswith('LogFile_') and f.endswith('.txt')]
    if not cands:
        return None
    cands.sort(key=lambda f: os.path.getsize(os.path.join(logdir, f)),
               reverse=True)
    return os.path.join(logdir, cands[0])


def parse_sends(path):
    """抽出所有下行帧（CRadarProtocolDevice::XXX_Send）。"""
    lines = open(path, 'rb').read().decode('utf-8-sig', 'replace').splitlines()
    out = []
    for i, ln in enumerate(lines):
        m = RE_METHOD.search(ln)
        if not m or m.group(2) != 'Send':
            continue
        h = RE_HEX.match(ln[m.end():])
        if h:
            hexs = ' '.join(h.group(1).split())
        else:
            if i + 1 >= len(lines):
                continue
            h2 = RE_HEX.match(lines[i + 1])
            if not h2:
                continue
            hexs = ' '.join(h2.group(1).split())
        raw = bytes(int(x, 16) for x in hexs.split())
        if len(raw) >= 12 and raw[:4] == HEAD and raw[-4:] == TAIL:
            out.append((raw, i + 1))
    return out


def decode(raw):
    ln = struct.unpack_from('<H', raw, 4)[0]
    cmd = struct.unpack_from('<H', raw, 6)[0]
    return cmd, raw[8:8 + ln - 2]


def label(cmd, payload):
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


def main():
    ap = argparse.ArgumentParser(description='提取 2T4R 启动帧序列')
    ap.add_argument('--log', help='指定的 ONELAB 日志文件')
    ap.add_argument('--logdir', help='ONELAB 的 Log 目录')
    ap.add_argument('--out', default=os.path.join(OUT_DIR, 'boot_seq_2t4r.json'))
    args = ap.parse_args()

    if args.log:
        path = args.log
    else:
        logdir = args.logdir
        if not logdir:
            raise SystemExit(
                '请用 --log 指定日志文件，或用 --logdir 指定 ONELAB 的 Log 目录。\n'
                '典型位置：\n'
                '  <ONELAB安装目录>\\ONELAB毫米波传感器开发工具\\Log\\')
        path = find_log(logdir, DEFAULT_LOG_NAME)

    if not path or not os.path.isfile(path):
        print('!! 找不到日志文件:', path)
        return 2

    sends = parse_sends(path)
    print('日志:', os.path.basename(path))
    print('下行帧:', len(sends))

    dec = [(raw, line) + decode(raw) for raw, line in sends]

    start_decl = None
    for i, (raw, line, cmd, pl) in enumerate(dec):
        if cmd == 0x0012 and len(pl) >= 6:
            _param, sec, cnt = struct.unpack_from('<HHH', pl, 0)
            if sec == 0x0003 and cnt == 9:      # Start 段声明
                start_decl = i
                break

    if start_decl is None:
        print('!! 没找到 Start 段声明 (SYS_PARAM sec=0x0003 cnt=9)')
        print('   这份日志可能不含完整启动。换一份再试。')
        return 1

    open_idx = max(i for i in range(start_decl) if dec[i][2] == 0x00FF)
    close_idx = min(i for i in range(start_decl, len(dec)) if dec[i][2] == 0x00FE)

    block = dec[open_idx:close_idx + 1]
    print('\n启动序列：原始下标 %d..%d，共 %d 帧\n' % (
        open_idx, close_idx, len(block)))

    frames = []
    total = 0
    for k, (raw, line, cmd, pl) in enumerate(block):
        total += len(raw)
        frames.append({
            'i': k,
            'cmd': cmd,
            'label': label(cmd, pl),
            'raw': raw.hex(' '),
            'src_line': line,
        })
        print('  %2d  L%-5d %-42s %3d B' % (k, line, label(cmd, pl), len(raw)))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump({
            'source_log': os.path.basename(path),
            'chip_addr': 0x0024,
            'baud': 1500000,
            'note': ('Verbatim frames extracted from vendor ONELAB log. '
                     'Contains vendor register config. DO NOT COMMIT.'),
            'frames': frames,
        }, f, ensure_ascii=False, indent=1)

    print('\n帧数=%d  总字节=%d' % (len(frames), total))
    print('已写出:', args.out)
    print('提示：该文件含厂商寄存器配置，不要提交、不要外传。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
