# -*- coding: utf-8 -*-
"""一键检查并安装运行环境。

同学拿到压缩包后只需要跑一次 `1-setup.cmd`，它对应这里：
  1. 检查 Python 版本（本项目需要 3.8 及以上）；
  2. 检查 numpy / pyserial 两个依赖；
  3. 缺了就自动用 pip 安装 —— 先走清华镜像（国内快），失败再走官方源；
  4. 装完再验证一次导入，明确告诉用户「可以进入下一步了」。

它不联网上传任何数据，只访问 pip 包索引下载依赖。
"""
import argparse
import importlib
import subprocess
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

MIN_PY = (3, 8)
DEPS = (('serial', 'pyserial'), ('numpy', 'numpy'))


def missing_deps():
    out = []
    for mod, pkg in DEPS:
        try:
            importlib.import_module(mod)
        except Exception:
            out.append(pkg)
    return out


def pip_install(pkgs):
    base = [sys.executable, '-m', 'pip', 'install',
            '--disable-pip-version-check', '--no-warn-script-location',
            '--timeout', '60', '--retries', '2']
    attempts = [
        ('清华镜像', base + ['-i', 'https://pypi.tuna.tsinghua.edu.cn/simple']),
        ('官方源', base),
    ]
    for i, (label, cmd) in enumerate(attempts, 1):
        print('')
        print('[安装] 第 %d/%d 次尝试（%s）：' % (i, len(attempts), label))
        print('       %s' % ' '.join(cmd + list(pkgs)))
        try:
            rc = subprocess.call(cmd + list(pkgs))
        except Exception as e:
            print('       pip 调用失败: %s: %s' % (type(e).__name__, e))
            continue
        if rc == 0:
            return True
        print('       本次失败（退出码 %d），换源重试。' % rc)
    return False


def main():
    ap = argparse.ArgumentParser(description='检查/安装雷达点云运行环境')
    ap.add_argument('--install', action='store_true', help='缺少依赖时自动安装')
    a = ap.parse_args()

    print('=' * 64)
    print('   毫米波雷达 · 实时点云   —   环境安装')
    print('=' * 64)
    print('Python: %s' % sys.version.replace('\n', ' '))
    print('解释器: %s' % sys.executable)

    if sys.version_info[:2] < MIN_PY:
        print('')
        print('[失败] Python 版本过低，需要 %d.%d 及以上。' % MIN_PY)
        print('       请到 https://www.python.org/downloads/windows/ 安装 3.8+，')
        print('       安装时务必勾选 "Add python.exe to PATH"，然后重新运行本脚本。')
        return 2

    missing = missing_deps()
    if not missing:
        print('')
        print('[通过] numpy / pyserial 已就绪，环境无需改动。')
        print('')
        print('下一步：双击 2-start.cmd 连接雷达并打开点云页面。')
        return 0

    print('')
    print('[待装] 缺少: %s' % ', '.join(missing))
    if not a.install:
        print('       请重新运行并加 --install，或手动执行:')
        print('       %s -m pip install %s' % (sys.executable, ' '.join(missing)))
        return 1

    if not pip_install(missing):
        print('')
        print('[失败] 自动安装没有成功。请按下面顺序手动处理：')
        print('  1) 确认电脑能上网（校园网可能需要先登录）；')
        print('  2) 手动执行： %s -m pip install --user %s'
              % (sys.executable, ' '.join(missing)))
        print('  3) 若提示权限不足，用「管理员身份」打开命令提示符再执行；')
        print('  4) 若公司/学校有内网 pip 源，加 -i <源地址> 再试。')
        return 2

    still = missing_deps()
    if still:
        print('')
        print('[失败] pip 报成功，但导入仍然失败: %s' % ', '.join(still))
        print('       常见原因：装了多个 Python，pip 装到了另一个解释器。')
        print('       请执行 %s -m pip install %s 再试。'
              % (sys.executable, ' '.join(still)))
        return 2

    print('')
    print('[通过] 依赖已安装完成。')
    print('')
    print('下一步：双击 2-start.cmd 连接雷达并打开点云页面。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
