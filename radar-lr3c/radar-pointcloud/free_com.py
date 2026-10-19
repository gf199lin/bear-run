# -*- coding: utf-8 -*-
"""串口占用体检：确认端口能独占打开；必要时结束本项目自己的 viewer 进程。

为什么要有这个脚本
    `radar-pointcloud\\server.py`（网页点云服务）启动时用 `--port auto`
    自动认领 COM4，并且一直抓着不放。之后任何采集脚本再打开同一个口，
    拿到的都是 Windows 的 `PermissionError(13, '拒绝访问')`，报错文本很像
    「设备没插好」，其实设备好好的 —— 是服务占着。
    这个脚本把「谁占着」直接列出来，并在明确要求（`--kill`）时只结束
    **本项目** 的 viewer 进程树，**不碰 ONELAB**、不碰数据目录。

用法
    python radar-pointcloud\\free_com.py COM4            只体检
    python radar-pointcloud\\free_com.py COM4 --kill     体检 + 释放 + 复检
    python radar-pointcloud\\free_com.py COM4 --baud 921600

退出码
    0 = 最后确认端口可用
    1 = 端口仍不可用（占用者不是本项目进程，或释放失败）
"""
import argparse
import os
import subprocess
import sys
import time

# 强匹配：命令行里出现项目目录名，一定是本项目的进程
STRONG = ('radar-pointcloud',)
# 弱匹配：这些脚本名很专一，但启动时命令行里只有裸文件名（没有路径）
WEAK = ('start_viewer.py', 'free_com.py', 'segment_capture.py',
        'position_capture.py')
# 厂商工具：只报告，绝不结束
VENDOR = ('onelab', 'mmwavesensordevtool', 'icl_mmwave', 'radar_config',
          'icldataprocess')


def ps_list():
    """列出相关进程，返回 [(pid, name, cmdline)] 或 (None, 错误信息)。"""
    cmd = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -match "
           "'python|ONELAB|mmWave|ICL|DevTool' } | ForEach-Object { "
           "'{0}`t{1}`t{2}' -f $_.ProcessId, $_.Name, $_.CommandLine }")
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-Command', cmd],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=30)
    except Exception as e:                                # noqa: BLE001
        return None, '%s: %s' % (type(e).__name__, e)
    rows = []
    for line in (r.stdout or '').splitlines():
        parts = line.split('\t')
        if len(parts) >= 2:
            pid = parts[0].strip()
            name = parts[1].strip()
            cl = parts[2].strip() if len(parts) > 2 else ''
            rows.append((pid, name, cl))
    return rows, None


def try_open(port, baud):
    """能独占打开返回 None，否则返回异常对象。"""
    import serial
    try:
        s = serial.Serial(port, baud, timeout=0.2)
        s.close()
        return None
    except Exception as e:                                # noqa: BLE001
        return e


def classify(cmdline):
    low = (cmdline or '').lower()
    if any(k in low for k in STRONG):
        return 'ours'
    if any(k in low for k in WEAK):
        return 'ours'
    if any(k in low for k in VENDOR):
        return 'vendor'
    return 'other'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('port')
    ap.add_argument('--baud', type=int, default=1500000)
    ap.add_argument('--kill', action='store_true')
    args = ap.parse_args()

    err = try_open(args.port, args.baud)
    if err is None:
        print('  [可以] %s 能独占打开，没有别的进程占着它。' % args.port)
        return 0

    name = type(err).__name__
    print('  [占用] %s 打不开：%s: %s' % (args.port, name, err))
    if 'PermissionError' not in name:
        print('  （这不像"被别人占用"，更像端口号写错或设备没插好。）')
        return 1

    rows, perr = ps_list()
    if rows is None:
        print('  查不到进程列表（%s）。' % perr)
        return 1

    me = str(os.getpid())
    ours, vendor = [], []
    for pid, pname, cl in rows:
        if pid == me:
            continue
        kind = classify(cl or pname)
        if kind == 'ours':
            ours.append((pid, pname, cl))
        elif kind == 'vendor':
            vendor.append((pid, pname, cl))

    if ours or vendor:
        print('  谁占着：')
        for pid, pname, cl in ours:
            print('     [本项目] PID %-7s %s' % (pid, cl or pname))
        for pid, pname, cl in vendor:
            print('     [厂商工具] PID %-7s %s' % (pid, cl or pname))
    else:
        print('  没找到本项目或厂商工具的进程 —— 可能是串口助手等别的程序。')

    if not args.kill:
        return 1
    if not ours:
        print('  没有本项目自己的进程可结束；ONELAB 需要你手动关掉。')
        return 1

    for pid, pname, cl in ours:
        print('  结束本项目进程 PID %s ...' % pid)
        subprocess.run(['taskkill', '/PID', pid, '/T', '/F'],
                       capture_output=True, text=True)
    time.sleep(2.0)

    err = try_open(args.port, args.baud)
    if err is None:
        print('  [可以] %s 已释放，能独占打开了。' % args.port)
        return 0
    print('  释放后仍然打不开：%s' % err)
    return 1


if __name__ == '__main__':
    sys.exit(main())
