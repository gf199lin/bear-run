#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双雷达自动连接工具（DEV202H 1T2R + DEV113P 2T4R）

为什么需要这个工具
------------------
项目里原来的自动选口逻辑（radar-pointcloud/radar_source.py 的
find_radar_port）只依赖 pyserial 的 comports()，有两个实证问题：

  1. 某些机器上 pyserial 枚举不全。本项目主力机上实测
     comports() 只报 COM4（FT232），完全看不到 COM3（CP210x），
     而注册表里两个都在（见 docs/radar-serial-preflight.md 3.1 第 1 条）。
  2. 它只返回「第一个命中的口」，所以两块板同时插上时只会打开一块。

于是「换一台电脑就自动连不上」「只连得上一块」的根因并不在雷达，
而在端口发现层。本工具的做法：

  * 端口清单 = 注册表扫描 ∪ pyserial 枚举（互补，任一方缺失都不影响）；
  * 板型不靠 COM 号，靠 USB VID:PID，再用真实字节流形态复核；
  * 两块板并发打开、各自独立重连，互不干扰；
  * 从不写、不发任何雷达命令，只读字节流（安全，不改设备状态）。

协议事实（本工具只做「连上没连上 / 在不在出帧」的判定）
--------------------------------------------------------
  1T2R (DEV202H / ICL1122)  CP210x 10c4:ea60  @ 921600 8-N-1
      外层 b"ICLH" + u16LE 长度 + u16LE 序号 + 40 B 雷达帧 + b"ICLT"
      一条雷达帧 = 64 chirp × 2 RX = 128 个包；实测约 991 包/s ≈ 7.74 帧/s
      （裸帧模式：没有 ICLH 包装，40 B 帧按节拍连发）
  2T4R (DEV113P)            FT232  0403:6001  @ 1500000 8-N-1
      136 B 裸帧、同步字 55 AA、帧 0xAA 尾 0x55；
      一条雷达帧 = 64 chirp × 4 RX = 256 个帧；实测约 482 帧/s ≈ 1.88 帧/s
  出处：docs/radar-uplink-protocol.md、docs/radar-uplink-dev113p-2t4r.md

依赖：Python 3.8+，pyserial（pip install pyserial）。不需要 numpy。

用法
----
  python radar_autoconnect.py scan                 只列端口，不打开（零风险）
  python radar_autoconnect.py check                枚举 + 试开 + 读流判定板型
  python radar_autoconnect.py capture --seconds 60 自动识别并录制原始字节
  python radar_autoconnect.py watch                持续监视，掉线自动重连
  python radar_autoconnect.py check --port COM7 --board 2t4r
  python radar_autoconnect.py check --json          机器可读输出

退出码
  0 = 至少一块板确认出帧（check / capture 成功）
  1 = 一块都没连上
  2 = 环境问题（缺 pyserial / 缺驱动 / 无串口）
"""
import argparse
import datetime
import json
import os
import re
import sys
import threading
import time

VERSION = '1.0.0'

# --------------------------------------------------------------------------
# 板型档案：USB 指纹 + 串口参数 + 字节流形态
# --------------------------------------------------------------------------
BOARD_PROFILES = {
    '1t2r': {
        'label': 'DEV202H (ICL1122) 1T2R',
        'baud': 921600,
        'vidpids': ('10c4:ea60',),
        'hints': ('cp210', 'silicon labs', 'silabs', 'silead',
                  'dev202', 'icl1122', 'icl'),
        'chip': 'CP210x',
        'packets_per_radar_frame': 128,
        'wrapper': b'ICLH',
        'frame_len': 40,
        'expect_fps': 7.74,
    },
    '2t4r': {
        'label': 'DEV113P 2T4R',
        'baud': 1500000,
        'vidpids': ('0403:6001',),
        'hints': ('ft232', 'ftdi', 'dev113', 'ftdibus'),
        'chip': 'FT232',
        'packets_per_radar_frame': 256,
        'wrapper': None,
        'frame_len': 136,
        'expect_fps': 1.88,
    },
}

# USB-UART 芯片名（用于把扫码结果翻译成人话）
CHIP_BY_VIDPID = {
    '10c4:ea60': 'Silicon Labs CP210x  ← 本项目 1T2R 用的就是这个',
    '0403:6001': 'FTDI FT232        ← 本项目 2T4R 用的就是这个',
    '1a86:7523': 'CH340/CH341（不是本项目标配芯片）',
    '1a86:5523': 'CH343（不是本项目标配芯片）',
    '067b:2303': 'Prolific PL2303（不是本项目标配芯片）',
}

# 试错波特率顺序：先按推定板型，再退到常见值
BAUD_FALLBACK = (921600, 1500000, 460800, 2000000, 115200, 230400)

REG_ROOTS = ('USB', 'FTDIBUS', 'SERENUM', 'ACPI', 'VMBUS', 'ROOT', 'BTHENUM')


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def _init_stdout():
    """Windows 控制台默认不是 UTF-8，中文会变成问号。"""
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def hr(ch='-', n=68):
    print(ch * n)


def head(title):
    print('')
    hr('=')
    print('  ' + title)
    hr('=')


OK = '[OK]'
BAD = '[FAIL]'
WARN = '[WARN]'
INFO = '[ .. ]'


def say(tag, msg):
    print('  %-7s %s' % (tag, msg))


# --------------------------------------------------------------------------
# 端口发现：注册表 ∪ pyserial
# --------------------------------------------------------------------------
def _norm_port(name):
    return (name or '').strip().upper()


def _vidpid_of(blob):
    """从 'USB\\VID_10C4&PID_EA60' / 'USB VID:PID=0403:6001' 里取 vid:pid。"""
    s = (blob or '').replace('+', '&')
    m = re.search(r'VID[_:=]([0-9A-Fa-f]{4})[&:_]PID[_:=]([0-9A-Fa-f]{4})', s)
    if m:
        return (m.group(1).lower() + ':' + m.group(2).lower()), m.group(0)
    return None, None


def _serial_of(blob):
    m = re.search(r'SER[=:]([A-Za-z0-9]+)', blob or '')
    return m.group(1) if m else None


def enum_registry():
    """不依赖 pyserial 的 COM 口枚举（只读注册表）。

    动机见 docs/radar-serial-preflight.md 3.1：本机 pyserial 3.5 +
    Python 3.14 下 comports() 可能漏口，而注册表不漏。
    注意：注册表可能残留「拔掉的设备」的端口号，所以本清单只当候选，
    能不能用一律以 try_open() 的实测结果为准。
    """
    if os.name != 'nt':
        return []
    try:
        import winreg
    except Exception:
        return []

    base = r'SYSTEM\CurrentControlSet\Enum'
    out = []
    for root in REG_ROOTS:
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
                    try:
                        port, _ = winreg.QueryValueEx(kp, 'PortName')
                    except OSError:
                        continue
                    friendly = ''
                    try:
                        friendly, _ = winreg.QueryValueEx(ki, 'FriendlyName')
                    except OSError:
                        pass
                    out.append({
                        'device': str(port).strip(),
                        'enum': root,
                        'devid': devid,
                        'friendly': friendly,
                        'source': 'registry',
                    })
                finally:
                    try:
                        winreg.CloseKey(kp)
                    except Exception:
                        pass
            try:
                winreg.CloseKey(ki)
            except Exception:
                pass
        try:
            winreg.CloseKey(k)
        except Exception:
            pass
    return out


def enum_pyserial():
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    out = []
    try:
        for p in list_ports.comports():
            out.append({
                'device': p.device,
                'desc': p.description or '',
                'hwid': p.hwid or '',
                'serial_number': getattr(p, 'serial_number', None),
                'source': 'pyserial',
            })
    except Exception:
        return []
    return out


def discover():
    """合并两个来源 → 按端口名去重后的清单（保持稳定排序）。"""
    merged = {}

    for rec in enum_registry():
        key = _norm_port(rec['device'])
        if not key:
            continue
        blob = '%s %s' % (rec.get('devid', ''), rec.get('friendly', ''))
        vidpid, _ = _vidpid_of(blob)
        merged[key] = {
            'device': rec['device'],
            'vidpid': vidpid,
            'serial_number': _serial_of(blob),
            'blob': blob,
            'sources': ['registry'],
        }

    for rec in enum_pyserial():
        key = _norm_port(rec['device'])
        if not key:
            continue
        blob = '%s %s' % (rec.get('hwid', ''), rec.get('desc', ''))
        vidpid, _ = _vidpid_of(blob)
        cur = merged.get(key)
        if cur is None:
            merged[key] = {
                'device': rec['device'],
                'vidpid': vidpid,
                'serial_number': rec.get('serial_number') or _serial_of(blob),
                'blob': blob,
                'sources': ['pyserial'],
            }
        else:
            cur['sources'].append('pyserial')
            cur['vidpid'] = cur['vidpid'] or vidpid
            cur['serial_number'] = cur['serial_number'] or rec.get('serial_number')
            cur['blob'] = (cur['blob'] + ' ' + blob).strip()

    def sort_key(item):
        m = re.match(r'[A-Za-z]+(\d+)', item['device'])
        return (m.group(1).zfill(6) if m else item['device'])

    return [merged[k] for k in sorted(merged, key=lambda k: sort_key(merged[k]))]


def guess_board(info):
    """仅凭 USB 指纹猜板型 → ('1t2r'|'2t4r'|None, 依据文字)。"""
    vidpid = info.get('vidpid')
    if vidpid:
        for name, prof in BOARD_PROFILES.items():
            if vidpid in prof['vidpids']:
                return name, 'VID:PID=%s' % vidpid
    blob = (info.get('blob') or '').lower()
    for name, prof in BOARD_PROFILES.items():
        for h in prof['hints']:
            if h in blob:
                return name, '描述含 "%s"' % h
    return None, 'USB 指纹认不出'


# --------------------------------------------------------------------------
# 端口可用性：区分「不存在」「被占用」「可用」
# --------------------------------------------------------------------------
def try_open(port, baud=115200):
    """返回 (status, err)；status ∈ 'free' / 'busy' / 'missing' / 'error'。"""
    try:
        import serial
    except Exception as e:
        return 'error', '未安装 pyserial（%s）' % e
    try:
        s = serial.Serial(port, baud, timeout=0.15, write_timeout=0.2)
        try:
            s.close()
        except Exception:
            pass
        return 'free', None
    except Exception as e:
        name = type(e).__name__
        txt = str(e)
        low = txt.lower()
        if ('permission' in name.lower() or 'errno 13' in low
                or '拒绝访问' in txt or 'being used' in low
                or 'access is denied' in low):
            return 'busy', '%s: %s' % (name, txt)
        if ('filenotfound' in name.lower() or 'errno 2' in low
                or '找不到' in txt or 'cannot find' in low
                or 'could not open port' in low):
            return 'missing', '%s: %s' % (name, txt)
        return 'error', '%s: %s' % (name, txt)


def port_holders():
    """查「谁占着串口」——只在必要时调用（要起一次 PowerShell）。"""
    if os.name != 'nt':
        return []
    cmd = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -match "
           "'python|ONELAB|mmWave|ICL|DevTool|minicom|putty' } | "
           "ForEach-Object { '{0}`t{1}`t{2}' -f $_.ProcessId, $_.Name, $_.CommandLine }")
    try:
        import subprocess
        r = subprocess.run(['powershell', '-NoProfile', '-Command', cmd],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=25)
    except Exception:
        return []
    rows = []
    for line in (r.stdout or '').splitlines():
        parts = line.split('\t')
        if len(parts) >= 2:
            rows.append({'pid': parts[0].strip(),
                         'name': parts[1].strip(),
                         'cmd': (parts[2] if len(parts) > 2 else '').strip()})
    return rows


# --------------------------------------------------------------------------
# 字节流形态判定（纯标准库，不 import 项目代码）
# --------------------------------------------------------------------------
def looks_like_naked_1t2r(buf, pkt=40, need=6):
    """40 B 节拍上连续 ≥need 个 0xAA → 1T2R 裸帧模式。

    与 radar_source.looks_like_naked_1t2r 同一判据：故意保守，
    宁可判 None 也不要把 2T4R 误判成 1T2R（两者帧周期差 4 倍）。
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


def analyze_stream(buf, seconds):
    """对原始字节做形态统计 → dict。

    只回答「这块板在出什么格式的流」，不做动作识别、不判对错。
    """
    n = len(buf)
    res = {
        'bytes': n,
        'rate_bps': n / seconds if seconds > 0 else 0.0,
        'board': None,
        'form': None,
        'packets': 0,
        'est_fps': 0.0,
        'evidence': [],
    }
    if n == 0:
        return res

    # --- 1T2R wrapper: ICLH ... ICLT ---
    iclh = buf.count(b'ICLH')
    iclt = buf.count(b'ICLT')
    if iclh >= 3:
        res['board'] = '1t2r'
        res['form'] = 'ICLH 封装帧'
        res['packets'] = iclh
        res['evidence'].append('ICLH×%d / ICLT×%d' % (iclh, iclt))
        per = BOARD_PROFILES['1t2r']['packets_per_radar_frame']
        res['est_fps'] = (iclh / float(per)) / seconds if seconds > 0 else 0.0
        return res

    # --- 2T4R: 55 AA + 136 B 步长 ---
    cnt2 = 0
    i = buf.find(b'\x55\xaa')
    while i >= 0:
        if i + 4 <= n and (buf[i + 3] & 0x0F) == 0:
            cnt2 += 1
        i = buf.find(b'\x55\xaa', i + 1)
    if cnt2 >= 20:
        # 用「连续 136 B 间距」验一下，避免把噪声里的 55AA 当真
        step136 = 0
        i = buf.find(b'\x55\xaa')
        while i >= 0 and i + 136 * 3 < n:
            if buf[i + 136:i + 138] == b'\x55\xaa' and buf[i + 272:i + 274] == b'\x55\xaa':
                step136 += 1
            i = buf.find(b'\x55\xaa', i + 1)
        if step136 >= 3:
            res['board'] = '2t4r'
            res['form'] = '136 B 裸帧（55 AA）'
            res['packets'] = step136
            per = BOARD_PROFILES['2t4r']['packets_per_radar_frame']
            res['est_fps'] = (step136 / float(per)) / seconds if seconds > 0 else 0.0
            res['evidence'].append('55AA 命中 %d，其中 136 B 等距 %d' % (cnt2, step136))
            return res

    # --- 1T2R 裸帧 ---
    if looks_like_naked_1t2r(buf):
        res['board'] = '1t2r'
        res['form'] = '40 B 裸帧（无 ICLH）'
        res['evidence'].append('40 B 节拍连续 0xAA')
        # 估算：40 B 一个包，每雷达帧 128 包
        res['packets'] = n // 40
        per = BOARD_PROFILES['1t2r']['packets_per_radar_frame']
        res['est_fps'] = (res['packets'] / float(per)) / seconds if seconds > 0 else 0.0
        return res

    res['evidence'].append('未命中任何已知形态（ICLH=%d, 55AA=%d）' % (iclh, cnt2))
    return res


def read_stream(port, baud, seconds, stop=None):
    """打开端口读 seconds 秒 → (bytes, err)。打开失败返回 (b'', err)。"""
    try:
        import serial
    except Exception as e:
        return b'', '未安装 pyserial（%s）' % e

    buf = bytearray()
    try:
        s = serial.Serial(port, baud, timeout=0.05, write_timeout=0.2)
    except Exception as e:
        return b'', '%s: %s' % (type(e).__name__, e)

    try:
        try:
            s.reset_input_buffer()
        except Exception:
            pass
        t0 = time.time()
        while time.time() - t0 < seconds:
            if stop is not None and stop.is_set():
                break
            try:
                n = s.in_waiting
                if n:
                    buf += s.read(n)
                else:
                    time.sleep(0.005)
            except Exception as e:
                return bytes(buf), '读取出错: %s: %s' % (type(e).__name__, e)
    finally:
        try:
            s.close()
        except Exception:
            pass
    return bytes(buf), None


# --------------------------------------------------------------------------
# 单口检测
# --------------------------------------------------------------------------
def check_one(port, board_hint=None, seconds=2.5, verbose=True):
    """对一个端口做完整判定：存在性 → 板型 → 出帧。

    返回 dict：port, status, board, form, fps, baud, evidence, error
    """
    r = {
        'port': port, 'status': 'unknown', 'board': None, 'form': None,
        'fps': 0.0, 'baud': None, 'bytes': 0, 'evidence': [], 'error': None,
    }

    st, err = try_open(port)
    r['open_status'] = st
    if st == 'missing':
        r.update(status='missing', error=err)
        return r
    if st == 'error':
        r.update(status='error', error=err)
        return r
    if st == 'busy':
        r.update(status='busy', error=err)
        return r

    # 端口能打开 → 试波特率。先猜板型，再按需回退。
    cands = []
    if board_hint in BOARD_PROFILES:
        cands.append(BOARD_PROFILES[board_hint]['baud'])
    for b in BAUD_FALLBACK:
        if b not in cands:
            cands.append(b)

    best = None
    for baud in cands:
        buf, err = read_stream(port, baud, seconds)
        if err and not buf:
            r['error'] = err
            # 打开后立刻出错（如被顶掉）→ 不再试其它波特率
            if 'Permission' in err or '拒绝访问' in err:
                r['status'] = 'busy'
                return r
            continue
        info = analyze_stream(buf, seconds)
        info['baud'] = baud
        if info['bytes'] > 0:
            best = info
            if info['board']:
                break          # 形态已确认，不用再试
            # 有字节但认不出形态 → 继续换波特率
        else:
            if best is None:
                best = info

    if best is None:
        r['status'] = 'no_data'
        r['error'] = '端口能打开，但一个字节都没收到'
        return r

    r['bytes'] = best['bytes']
    r['baud'] = best['baud']
    r['board'] = best['board']
    r['form'] = best['form']
    r['fps'] = round(best['est_fps'], 2)
    r['evidence'] = best['evidence']

    if best['board']:
        r['status'] = 'streaming'
    elif best['bytes'] > 0:
        r['status'] = 'unrecognized'
    else:
        r['status'] = 'no_data'
    return r


STATUS_TEXT = {
    'streaming': (OK, '出帧正常'),
    'unrecognized': (WARN, '有数据但认不出格式（波特率或固件模式不对）'),
    'no_data': (BAD, '端口能打开但没有数据（雷达未出流 / 未上电）'),
    'busy': (BAD, '端口被其它程序占用'),
    'missing': (BAD, '端口不存在（设备没插或已被拔掉）'),
    'error': (BAD, '打开出错'),
    'unknown': (INFO, '未检测'),
}


# --------------------------------------------------------------------------
# 命令：scan
# --------------------------------------------------------------------------
def cmd_scan(args):
    head('串口枚举（只读，不打开端口）')
    ports = discover()

    reg_n = len(enum_registry())
    ps_n = len(enum_pyserial())
    print('  注册表扫描 = %d 个    pyserial 枚举 = %d 个    合并去重 = %d 个'
          % (reg_n, ps_n, len(ports)))
    if reg_n and ps_n == 0:
        say(WARN, 'pyserial 一个都没枚举到，但注册表有 %d 个 → 本机 pyserial 枚举失效，' % reg_n)
        print('           本工具已自动走注册表，你不需要处理。')
    if reg_n and ps_n and reg_n != ps_n:
        say(INFO, '两个来源数量不一致，属正常（注册表可能保留已拔设备的残留项）')

    print('')
    if not ports:
        say(BAD, '一个串口都没有 —— 板子没插好，或 USB-UART 驱动没装。')
        print('')
        print('  检查顺序：')
        print('    1) 换一根「能传数据」的 USB 线（很多线只能充电）')
        print('    2) 设备管理器 → 端口(COM 和 LPT) 里应出现对应芯片')
        print('       1T2R 用 CP2102 → 装 CP210x 驱动')
        print('       2T4R 用 FT232  → 装 FTDI VCP 驱动')
        print('    3) 拔掉重插，再跑一次本命令')
        return 3

    for info in ports:
        board, why = guess_board(info)
        vidpid = info.get('vidpid') or '未知'
        chip = CHIP_BY_VIDPID.get(vidpid, '未收录的芯片')
        print('  %-8s  VID:PID %-10s  %s' % (info['device'], vidpid, chip))
        print('            指纹判断: %s' % ('→ ' + BOARD_PROFILES[board]['label']
                                          if board else '→ ' + why))
        if info.get('serial_number'):
            print('            序列号  : %s' % info['serial_number'])
        print('            来源    : %s' % ' + '.join(info['sources']))
        print('')

    print('  提示：scan 不判断端口是否真的可用。要确认能不能连，跑：')
    print('        python radar_autoconnect.py check')
    return 0


# --------------------------------------------------------------------------
# 命令：check
# --------------------------------------------------------------------------
def cmd_check(args):
    head('雷达自动连接检查')

    try:
        import serial  # noqa: F401
    except Exception:
        say(BAD, '没有安装 pyserial。先运行 1-安装环境.cmd，或手工执行：')
        print('        python -m pip install pyserial')
        return 2

    if args.port:
        targets = [{'device': args.port, 'vidpid': None,
                    'serial_number': None, 'blob': '', 'sources': ['手动指定']}]
    else:
        targets = discover()

    if not targets:
        say(BAD, '一个串口都没找到 —— 板子没插好或驱动没装。')
        print('        先跑：python radar_autoconnect.py scan')
        return 2

    say(INFO, '开始检测 %d 个端口，每个约 %d 秒（只读，不发任何命令）'
        % (len(targets), int(args.seconds * 2)))
    print('')

    results = []
    for info in targets:
        port = info['device']
        hint = args.board if args.board != 'auto' else guess_board(info)[0]
        print('  ▶ %s ...' % port, end='', flush=True)
        r = check_one(port, board_hint=hint, seconds=args.seconds, verbose=False)
        r['vidpid'] = info.get('vidpid')
        r['hint'] = hint
        results.append(r)
        tag, txt = STATUS_TEXT.get(r['status'], (INFO, r['status']))
        print('\r  %s %-8s %s' % (tag, port, txt))

        if r['board']:
            prof = BOARD_PROFILES[r['board']]
            print('            板型 %s（%s）' % (r['board'], prof['label']))
            print('            形态 %s @ %d 8-N-1' % (r['form'], r['baud']))
            print('            速率 %.0f B/s，估算 %.2f 雷达帧/s（典型 %.2f）'
                  % (r['bytes'] / max(args.seconds, 0.01), r['fps'], prof['expect_fps']))
        if r['evidence']:
            print('            证据 %s' % '；'.join(r['evidence']))
        if r['error']:
            print('            原因 %s' % r['error'])
            if r['status'] == 'busy':
                holders = port_holders()
                if holders:
                    print('            谁占着：')
                    for h in holders:
                        print('              PID %-7s %s %s'
                              % (h['pid'], h['name'], h['cmd'][:70]))
                else:
                    print('            没查到明显的占用进程，可能是别的串口助手/仪器软件。')
        print('')

    # ---- 汇总 ----
    good = [r for r in results if r['status'] == 'streaming']
    print('')
    hr('=')
    print('  结论')
    hr('=')
    print('  可用（出帧正常）: %d 块' % len(good))
    for r in good:
        prof = BOARD_PROFILES.get(r['board'])
        print('    %-8s %-28s %.2f 帧/s @ %d'
              % (r['port'], prof['label'] if prof else '未知', r['fps'], r['baud']))
    bad = [r for r in results if r['status'] != 'streaming']
    if bad:
        print('  未通过: %d 块' % len(bad))
        for r in bad:
            tag, txt = STATUS_TEXT.get(r['status'], (INFO, r['status']))
            print('    %-8s %s %s' % (r['port'], tag, txt))

    print('')
    if len(good) >= 2:
        say(OK, '两块板可以同时使用，互不冲突。')
    elif len(good) == 1:
        say(WARN, '只连上 %d 块。如果两块都插着，看下面「没有通过」那几行的原因。' % len(good))
        print('        最常见：另一块被 ONELAB / 网页点云 server / 串口助手占着。')
        print('        把这些程序关掉再跑一次 check，不要两边同时开同一个口。')
    else:
        say(BAD, '一块都没连上。先跑 scan 确认系统能看到几个口。')

    if args.json:
        print('')
        print(json.dumps({'version': VERSION, 'results': results},
                         ensure_ascii=False, indent=2))

    return 0 if good else 1


# --------------------------------------------------------------------------
# 命令：capture
# --------------------------------------------------------------------------
def _capture_worker(port, baud, device_board, outdir, seconds, stop, log):
    """单口录制线程：把原始字节写盘，返回统计。"""
    import serial

    ok, err = None, None
    try:
        s = serial.Serial(port, baud, timeout=0.05)
    except Exception as e:
        return {'port': port, 'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}

    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    name = '%s-%s-%s.bin' % (device_board or 'unknown', port.replace(':', ''), stamp)
    path = os.path.join(outdir, name)
    nbytes = 0
    t0 = time.time()
    try:
        try:
            s.reset_input_buffer()
        except Exception:
            pass
        with open(path, 'wb') as f:
            while time.time() - t0 < seconds:
                if stop.is_set():
                    break
                try:
                    n = s.in_waiting
                    chunk = s.read(n) if n else b''
                except Exception as e:
                    err = '读取出错: %s: %s' % (type(e).__name__, e)
                    break
                if chunk:
                    f.write(chunk)
                    nbytes += len(chunk)
                else:
                    time.sleep(0.005)
    finally:
        try:
            s.close()
        except Exception:
            pass
    dur = time.time() - t0
    log.append({
        'port': port, 'ok': nbytes > 0, 'path': path, 'bytes': nbytes,
        'duration_s': round(dur, 2), 'baud': baud, 'error': err,
        'board': device_board,
    })


def cmd_capture(args):
    head('采集原始雷达数据')

    try:
        import serial  # noqa: F401
    except Exception:
        say(BAD, '没有安装 pyserial，先运行 1-安装环境.cmd')
        return 2

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)

    # 1) 先自动识别，避免用户手填错端口
    if args.port:
        # 手动指定端口时也必须现场判定板型：1T2R 和 2T4R 的波特率不同
        # （921600 / 1500000），猜错就只会录到乱码；板型未知时后面
        # 取波特率会直接 KeyError（v1.0 的真实崩溃点）。
        hint = args.board if args.board != 'auto' else None
        say(INFO, '先确认 %s 的板型和波特率（约 2 秒）...' % args.port)
        r = check_one(args.port, board_hint=hint, seconds=2.0)
        tag, txt = STATUS_TEXT.get(r['status'], (INFO, r['status']))
        say(tag, '%-8s %s' % (args.port, txt))
        board = r['board'] or (hint if hint in BOARD_PROFILES else None)
        baud = r['baud'] or (BOARD_PROFILES[board]['baud'] if board else None)
        if r['status'] != 'streaming':
            say(WARN, '它现在没有出帧，仍按你的要求尝试录制（很可能得到 0 字节）。')
        plan = [{'port': args.port, 'board': board, 'baud': baud}]
    else:
        say(INFO, '先自动识别在出帧的板（每块约 2 秒）...')
        ports = discover()
        if not ports:
            say(BAD, '没有串口，无法采集。')
            return 2
        plan = []
        for info in ports:
            hint = guess_board(info)[0]
            r = check_one(info['device'], board_hint=hint, seconds=2.0)
            if r['status'] == 'streaming':
                plan.append({'port': info['device'], 'board': r['board'],
                             'baud': r['baud']})
            elif r['status'] == 'busy':
                say(WARN, '%s 被占用，跳过（关掉占用它的程序后重跑）' % info['device'])
            else:
                say(INFO, '%s 未出帧，跳过（%s）' % (info['device'],
                                                   STATUS_TEXT.get(r['status'], ('', ''))[1]))

    if not plan:
        say(BAD, '没有可用的板子，采集取消。')
        print('        先跑 check 看具体原因。')
        return 1

    # 2) 每块板一个线程并发录
    print('')
    say(OK, '开始采集 %d 块板，%d 秒，输出目录：%s' % (len(plan), args.seconds, outdir))
    for p in plan:
        prof = BOARD_PROFILES.get(p['board'])
        print('    %-8s %s @ %d' % (p['port'],
                                   prof['label'] if prof else '未知板型',
                                   p.get('baud') or (prof['baud'] if prof else 921600)))

    stop = threading.Event()
    log = []
    threads = []
    for p in plan:
        baud = p.get('baud') or (BOARD_PROFILES.get(p['board']) or {}).get('baud') or 921600
        t = threading.Thread(target=_capture_worker,
                             args=(p['port'], baud, p['board'], outdir,
                                   args.seconds, stop, log),
                             daemon=True)
        t.start()
        threads.append(t)

    # 进度
    t0 = time.time()
    try:
        while any(t.is_alive() for t in threads):
            el = time.time() - t0
            left = max(0, args.seconds - el)
            sys.stdout.write('\r  录制中... 剩余 %3d 秒' % int(left))
            sys.stdout.flush()
            time.sleep(0.5)
    except KeyboardInterrupt:
        say(WARN, '收到 Ctrl+C，提前结束')
        stop.set()
        for t in threads:
            t.join(timeout=5)

    for t in threads:
        t.join(timeout=5)
    sys.stdout.write('\r' + ' ' * 40 + '\r')

    # 3) 落元数据（证据纪律：谁、哪块板、多久、多少字节）
    meta_path = os.path.join(
        outdir, 'meta-%s.json' % datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    meta = {
        'tool': 'radar_autoconnect', 'version': VERSION,
        'created_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'requested_seconds': args.seconds,
        'files': log,
        'note': '原始串口字节流，未做任何解析或改码；truth=未标注',
    }
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print('')
    hr('=')
    for rec in log:
        if rec.get('ok'):
            say(OK, '%-8s %8.1f KB  %s' % (rec['port'], rec['bytes'] / 1024.0,
                                           os.path.basename(rec['path'])))
        else:
            say(BAD, '%-8s 失败：%s' % (rec['port'], rec.get('error') or '0 字节'))
    print('')
    print('  元数据：%s' % meta_path)
    print('  说明  ：meta 里记录了板型、端口、波特率、字节数、时长。')
    print('          没有真值标注的数据只能做链路检查，不能当识别准确率证据。')

    return 0 if any(r.get('ok') for r in log) else 1


# --------------------------------------------------------------------------
# 命令：watch
# --------------------------------------------------------------------------
def cmd_watch(args):
    head('持续监视（掉线自动重连）')
    say(INFO, '按 Ctrl+C 结束。只读，不发命令。')

    try:
        import serial  # noqa: F401
    except Exception:
        say(BAD, '没有安装 pyserial，先运行 1-安装环境.cmd')
        return 2

    known = {}       # port -> {'board','baud','state'}
    t_start = time.time()
    try:
        while True:
            ports = discover()
            seen = set()
            for info in ports:
                port = info['device']
                seen.add(port)
                st = known.get(port)
                if st and st['state'] == 'online':
                    # 已在线：轻量确认还能打开（不重复读流，避免抢数据）
                    ok, _ = try_open(port)
                    if ok != 'free':
                        st['state'] = 'offline'
                        print('  [%s] %s 掉线' % (time.strftime('%H:%M:%S'), port))
                    continue
                r = check_one(port, board_hint=guess_board(info)[0],
                              seconds=args.seconds)
                if r['status'] == 'streaming':
                    known[port] = {'board': r['board'], 'baud': r['baud'],
                                   'state': 'online'}
                    print('  [%s] %s 上线  %s  %.2f 帧/s'
                          % (time.strftime('%H:%M:%S'), port,
                             BOARD_PROFILES[r['board']]['label'], r['fps']))
                else:
                    tag, txt = STATUS_TEXT.get(r['status'], (INFO, r['status']))
                    known[port] = {'board': None, 'baud': None, 'state': 'offline'}
                    print('  [%s] %s %s %s'
                          % (time.strftime('%H:%M:%S'), port, tag, txt))

            for port in list(known.keys()):
                if port not in seen:
                    if known[port]['state'] == 'online':
                        print('  [%s] %s 已拔出' % (time.strftime('%H:%M:%S'), port))
                    del known[port]

            online = [p for p, v in known.items() if v['state'] == 'online']
            sys.stdout.write('\r  在线 %d 块  %s  (已运行 %d 秒)   '
                             % (len(online), ','.join(online) or '-',
                                time.time() - t_start))
            sys.stdout.flush()
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        print('')
        say(INFO, '监视结束。')
        return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        prog='radar_autoconnect',
        description='双雷达（1T2R + 2T4R）自动连接与采集工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='示例:\n'
               '  python radar_autoconnect.py scan\n'
               '  python radar_autoconnect.py check\n'
               '  python radar_autoconnect.py capture --seconds 60\n'
               '  python radar_autoconnect.py watch\n')
    sub = ap.add_subparsers(dest='cmd')

    p = sub.add_parser('scan', help='只列串口和芯片型号（不打开端口）')
    p.add_argument('--json', action='store_true', help='额外输出 JSON')
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser('check', help='试开每个端口并判断板型/是否出帧')
    p.add_argument('--port', default=None, help='只检查指定端口，如 COM7')
    p.add_argument('--board', default='auto', choices=['auto', '1t2r', '2t4r'],
                   help='强制指定板型（默认自动）')
    p.add_argument('--seconds', type=float, default=2.5,
                   help='每个波特率读多少秒（默认 2.5）')
    p.add_argument('--json', action='store_true', help='额外输出 JSON')
    p.set_defaults(func=cmd_check)

    p = sub.add_parser('capture', help='自动识别并录制原始字节流')
    p.add_argument('--seconds', type=float, default=60.0, help='录制秒数（默认 60）')
    p.add_argument('--outdir', default='captures', help='输出目录（默认 captures）')
    p.add_argument('--port', default=None, help='只录指定端口')
    p.add_argument('--board', default='auto', choices=['auto', '1t2r', '2t4r'])
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser('watch', help='持续监视插拔与出帧状态')
    p.add_argument('--interval', type=float, default=3.0, help='轮询间隔秒（默认 3）')
    p.add_argument('--seconds', type=float, default=1.5, help='每次确认读流秒数')
    p.set_defaults(func=cmd_watch)

    return ap


def main(argv=None):
    _init_stdout()
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, 'func', None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
