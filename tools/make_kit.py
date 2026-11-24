# -*- coding: utf-8 -*-
"""打交接包 mmwave-bear-run-kit-YYYYMMDD.zip

用法（在项目根目录）：
    python tools/make_kit.py              # 生成到 outputs/
    python tools/make_kit.py --keep       # 保留中间目录，便于检查
    python tools/make_kit.py --no-zip      # 只组装目录，不压 zip

为什么要有这个脚本：
    11/20 的交接包是手工拼的，里面的 tools/dual_radar_manager.py 引用
    UD_ARGS 却没有定义，组员插上竖直板一启动就 NameError，8767 永远空着。
    手工打包一定会漏文件，所以改成「白名单 + 规则」的可复现脚本。
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT_OVERLAY = os.path.join(ROOT, 'tools', 'kit')
PKG_BASE = os.path.join(ROOT, '.deepworks', 'tmp', '_pkg')

# 包根：这些文件名会从项目根 / tools/kit 拷到包根
ROOT_FILES_FROM_ROOT = [
    'AGENTS.md',
    '.gitignore',
]
# 项目根下所有 .cmd 启动器都带上
ROOT_CMD_GLOB = True
# tools/kit 下的覆盖文件（打包时提升到包根）
KIT_FILES = [
    '00-先读我.md',
    'AI-接手手册.md',
    '一键-自动启动.cmd',
    '安装依赖.cmd',
    '环境检查.cmd',
    '检查-雷达链路.cmd',
    'requirements.txt',
]

# data 只带这些批次（都是带真值标注的正式数据）
DATA_DIRS = [
    'aimcheck', 'calibration', 'dual_guide',
    'horizontal', 'horizontal_guide',
    'jumpduck_battery', 'jumpduck_guide',
    'lr-1119', 'lr-1119b', 'lr-1119c',
    'lr3', 'lr3b', 'lr3c',
    'rxpair', 'vertical', 'vguide_1t2r',
]

TREES = {
    'radar-pointcloud': {
        'excl_dirs': {'__pycache__'},
        'excl_ext': {'.pyc', '.log', '.err'},
        'excl_files': {'bg_baseline.json'},
    },
    'tools': {
        'excl_dirs': {'__pycache__', 'kit'},
        'excl_ext': {'.pyc', '.log', '.err'},
    },
    'bear-run-new': {
        'excl_dirs': {'.git', '.github', '.git-clone-backup', '__pycache__', 'node_modules'},
        'excl_ext': {'.pyc', '.log', '.err'},
    },
    'docs': {
        'excl_dirs': {'__pycache__'},
        'excl_ext': {'.pyc'},
    },
    'models': {},
    'outputs': {
        'excl_ext': {'.zip', '.cmd', '.bat', '.exe'},
        'max_file_mb': 8.0,
    },
}


def log(msg):
    print(msg, flush=True)


def copy_file(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree(src, dst, rule):
    excl_dirs = rule.get('excl_dirs') or set()
    excl_ext = rule.get('excl_ext') or set()
    excl_files = rule.get('excl_files') or set()
    max_bytes = int((rule.get('max_file_mb') or 0) * 1024 * 1024)
    n = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in excl_dirs]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in excl_ext or fn in excl_files:
                continue
            sp = os.path.join(dirpath, fn)
            try:
                if max_bytes and os.path.getsize(sp) > max_bytes:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(sp, src)
            copy_file(sp, os.path.join(dst, rel))
            n += 1
    return n


def git_commit():
    try:
        out = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=ROOT, stderr=subprocess.DEVNULL)
        return out.decode('ascii', 'ignore').strip()
    except Exception:
        return 'unknown'


def dir_size(path):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total


def build(name, keep):
    target = os.path.join(PKG_BASE, name)
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(target, exist_ok=True)

    counts = {}

    # 1) 根文件
    n = 0
    for fn in ROOT_FILES_FROM_ROOT:
        src = os.path.join(ROOT, fn)
        if os.path.isfile(src):
            copy_file(src, os.path.join(target, fn))
            n += 1
    if ROOT_CMD_GLOB:
        for fn in sorted(os.listdir(ROOT)):
            if fn.lower().endswith('.cmd') and os.path.isfile(os.path.join(ROOT, fn)):
                copy_file(os.path.join(ROOT, fn), os.path.join(target, fn))
                n += 1
    for fn in KIT_FILES:
        src = os.path.join(KIT_OVERLAY, fn)
        if os.path.isfile(src):
            copy_file(src, os.path.join(target, fn))
            n += 1
        else:
            log('  !! 缺少覆盖文件 tools/kit/%s' % fn)
    counts['(root)'] = n

    # 2) 通用目录
    for d, rule in TREES.items():
        src = os.path.join(ROOT, d)
        if not os.path.isdir(src):
            log('  !! 跳过不存在的目录 %s' % d)
            continue
        c = copy_tree(src, os.path.join(target, d), rule)
        counts[d] = c

    # 3) radar-bridge：只带桥接器本体
    bridge_src = os.path.join(ROOT, 'dual-radar-kit', 'tools', 'radar_live_ws.py')
    if os.path.isfile(bridge_src):
        copy_file(bridge_src, os.path.join(target, 'radar-bridge', 'tools', 'radar_live_ws.py'))
        counts['radar-bridge'] = 1
    else:
        log('  !! 找不到 dual-radar-kit/tools/radar_live_ws.py')

    # 4) data 白名单
    n = 0
    for d in DATA_DIRS:
        src = os.path.join(ROOT, 'data', d)
        if os.path.isdir(src):
            n += copy_tree(src, os.path.join(target, 'data', d), {'excl_dirs': {'__pycache__'}})
    counts['data'] = n

    # 5) MANIFEST
    total_files = sum(counts.values())
    total_bytes = dir_size(target)
    lines = [
        'mmwave-bear-run-kit %s' % name,
        'built        : %s' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'git commit   : %s' % git_commit(),
        'files        : %d' % total_files,
        'size         : %.1f MB' % (total_bytes / 1048576.0),
        '',
        'by directory:',
    ]
    for k in sorted(counts, key=lambda x: -counts[x]):
        lines.append('  %-20s %5d files' % (k, counts[k]))
    lines += [
        '',
        'excluded on purpose:',
        '  uploads/            vendor docs + course materials (confidential)',
        '  *.lic               license files',
        '  cache/, *-1030/     stale copies',
        '  outputs/*.zip       bulk archives (kit itself, capture dumps)',
        '  outputs/*.cmd       one-off historical launchers',
        '  __pycache__, *.pyc, *.log',
    ]
    with open(os.path.join(target, 'MANIFEST.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

    log('  assembled: %d files, %.1f MB' % (total_files, total_bytes / 1048576.0))
    return target, total_files, total_bytes


def make_zip(target, name, outdir):
    os.makedirs(outdir, exist_ok=True)
    zpath = os.path.join(outdir, name + '.zip')
    if os.path.exists(zpath):
        os.remove(zpath)
    base = os.path.dirname(target)
    log('  zipping -> %s' % os.path.relpath(zpath, ROOT))
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames.sort()
            for fn in sorted(filenames):
                sp = os.path.join(dirpath, fn)
                arc = os.path.relpath(sp, base)
                z.write(sp, arc)
    return zpath


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=datetime.date.today().strftime('%Y%m%d'))
    ap.add_argument('--name', default=None)
    ap.add_argument('--outdir', default=os.path.join(ROOT, 'outputs'))
    ap.add_argument('--keep', action='store_true', help='保留中间目录')
    ap.add_argument('--no-zip', action='store_true')
    args = ap.parse_args()

    name = args.name or ('mmwave-bear-run-kit-%s' % args.date)
    log('building %s' % name)
    target, files, size = build(name, args.keep)

    if not args.no_zip:
        zpath = make_zip(target, name, args.outdir)
        zsize = os.path.getsize(zpath) / 1048576.0
        log('done: %s (%.1f MB, %d files)' % (os.path.relpath(zpath, ROOT), zsize, files))
        if not args.keep:
            shutil.rmtree(target, ignore_errors=True)
            log('  intermediate dir removed (use --keep to inspect)')
    else:
        log('done: %s' % os.path.relpath(target, ROOT))
    return 0


if __name__ == '__main__':
    sys.exit(main())
