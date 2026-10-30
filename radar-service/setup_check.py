#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境检查与安装（给同学用，双击 1-安装环境.cmd 会跑到这里）。

只做四件事：
  1. 确认 Python 版本够（>= 3.8）
  2. 确认 pyserial 已安装（没有就 pip 装）
  3. 确认 numpy（可选，只有推流/点云才需要）
  4. 打印下一步该做什么

所有中文都在本文件里输出 —— 启动用的 .cmd 保持纯 ASCII，
避免 cmd.exe 在 chcp 切换时按字节偏移读文件、把中文行切断。
"""
import os
import subprocess
import sys

MIN_PY = (3, 8)


def _init_stdout():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def hr(ch='=', n=68):
    print(ch * n)


def head(t):
    print('')
    hr('=')
    print('  ' + t)
    hr('=')


def run(cmd):
    print('    $ ' + ' '.join(cmd))
    try:
        return subprocess.call(cmd)
    except Exception as e:
        print('    执行失败：%s' % e)
        return 1


def has_module(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


def main():
    _init_stdout()

    head('双雷达自动连接工具 - 环境检查')
    print('  你的 Python：%s' % sys.version.split()[0])
    print('  解释器路径 ：%s' % sys.executable)

    if sys.version_info < MIN_PY:
        print('')
        print('  [失败] Python 版本太低，需要 3.8 或更高。')
        print('         请到 https://www.python.org/downloads/windows/ 装新版，')
        print('         安装时勾选 "Add python.exe to PATH"。')
        return 1

    print('  版本检查   ：通过（>= 3.8）')

    # ---------- pyserial ----------
    head('依赖 1/2：pyserial（读串口必需）')
    if has_module('serial'):
        try:
            import serial
            print('  已安装，版本 %s' % getattr(serial, '__version__', '未知'))
        except Exception:
            print('  已安装')
        ok_serial = True
    else:
        print('  没装，正在安装（需要联网，约 10 秒）...')
        rc = run([sys.executable, '-m', 'pip', 'install',
                  '--disable-pip-version-check', 'pyserial'])
        ok_serial = (rc == 0) and has_module('serial')
        if ok_serial:
            print('  安装完成。')
        else:
            print('')
            print('  [失败] pyserial 装不上，通常是没联网。')
            print('')
            print('  办法 A：连上网，重新双击 1-安装环境.cmd。')
            print('')
            print('  办法 B（离线）：找一台有网的电脑执行')
            print('      python -m pip download pyserial numpy -d whl')
            print('    把生成的 whl 文件夹和本工具放一起，然后执行')
            print('      python -m pip install --no-index --find-links whl pyserial')

    # ---------- numpy（可选）----------
    head('依赖 2/2：numpy（可选，只有推流/点云才需要）')
    if has_module('numpy'):
        print('  已安装，可以直接用推流功能。')
    else:
        print('  没装。正在尝试安装（约 30 秒）...')
        rc = run([sys.executable, '-m', 'pip', 'install',
                  '--disable-pip-version-check', 'numpy'])
        if (rc == 0) and has_module('numpy'):
            print('  安装完成。')
        else:
            print('')
            print('  [提示] numpy 没装上，这不影响下面两件事：')
            print('           · 检查雷达连没连上（2-检查雷达.cmd）')
            print('           · 采集原始数据（3-采集数据.cmd）')
            print('         只影响"把雷达数据推给游戏"。可以先跳过。')

    # ---------- 结论 ----------
    head('结论')
    if ok_serial:
        print('  环境就绪。')
        print('')
        print('  下一步：双击  2-检查雷达.cmd')
        print('')
        hr('-')
        print('  如果"检查雷达"报「端口不存在」，多数是缺 USB 串口驱动：')
        print('    1T2R（板上有 CP2102 芯片）→ 装 CP210x 驱动')
        print('        https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers')
        print('    2T4R（板上有 FT232 芯片） → 装 FTDI VCP 驱动')
        print('        https://ftdichip.com/drivers/vcp-drivers/')
        print('  驱动装好后拔插一次 USB，再跑 2-检查雷达.cmd。')
        print('')
        print('  注意：本项目 1T2R 用的是 CP2102，装 CP210x 驱动即可；')
        print('        "装 FTDI 驱动"只对 2T4R 那块板成立。')
        return 0
    else:
        print('  [未就绪] 先把 pyserial 装上，再回来跑 2-检查雷达.cmd。')
        return 1


if __name__ == '__main__':
    sys.exit(main())
