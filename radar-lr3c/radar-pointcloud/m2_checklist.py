# -*- coding: utf-8 -*-
"""M2 验收检测引导（中文交互菜单）—— 逐项做掉，每项留证据。

为什么要有这个文件（AGENTS.md 证据纪律 + `docs/progress-radar-pointcloud.md` §7.41）：
- §7.41 用**干预实验**否掉了「左侧被 ±45° 视场截断」这一假说：`angle_max` 是软件截断
  （`radar_dsp.py` L165），抬到 80° 后左右「可得性」拉平（空帧 2% vs 2%），但识别命中
  不涨（93% vs 93%）、静息误报 +36%。⇒ **LEFT 弱侧差的是「幅度」不是「可得性」**
  （dev 0.161 vs 0.302），根因候选只剩「人实际没移那么远」。
- 而现有真值 `labels.jsonl` 里只有**指令值**（`--stride` 写的「要求横移≥0.6 m」），
  **没有实测位移** ⇒ 「人没移到位」与「雷达对 LEFT 退化」在数据上**无法区分**。
- 所以复采必须补「实测位移真值」。本引导把「复采 → 录入实测位移 → 离线分析 →
  决定性回归」串成一条不会漏项的流水线，并把每条命令、每个输出都留在磁盘上。

真值契约扩展（**向后兼容**：新字段可选，旧文件照常能读）：
  labels.jsonl 每条动作真值行可加：
    dx_cm     float ≥ 0   **实测**横向位移（厘米）。方向由 action 字段决定：
                          LEFT ⇒ 期望 dev > 0；RIGHT ⇒ 期望 dev < 0
                          （该符号约定依赖安装几何与 angle_sign，换站位须重标 → §7.40.3）
    dx_method str         量测方式：mark(地面尺标) / tape(卷尺) / eyeball(目测，可信度低)
    dx_by     str         量测人
  ⇒ `dx_cm` 一旦补齐，本引导的「决定性回归」就能判断 LEFT 弱侧到底出在哪一环。

用法：
  python m2_checklist.py                # 交互引导（双击 m2_checklist.cmd 同效）
  python m2_checklist.py --step env     # 只跑某一步（脚本化用）
  python m2_checklist.py --selftest     # 自检本引导自身（不需硬件）
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 控制台容错：cp936 控制台下出现不可编码字符时用 ? 代替，而不是崩溃
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors='replace')
    except Exception:
        pass

DEFAULT_DIR = os.path.join(ROOT, '.deepworks', 'tmp', 'm2')
CALIB = os.path.join(HERE, 'calib.json')

# 复采站位：1.5~2.0 m（§7.41 建议）——0.7 m 是问题站位，保留对照用
DEFAULT_STATIONS = '1.5,2.0'
DEFAULT_STRIDE = 0.6


# ---------------------------------------------------------------- 界面助手
def hr(ch='-', n=78):
    print(ch * n)


def title(t):
    print()
    hr('=')
    print(t)
    hr('=')


def say(*a):
    print(*a)


def ok(t):
    print('  [OK]   %s' % t)


def warn(t):
    print('  [注意] %s' % t)


def bad(t):
    print('  [问题] %s' % t)


def ask(prompt, default=''):
    # EOF（管道输入用完 / 控制台关闭）时退回默认值，不要抛 Traceback
    try:
        s = input('%s%s: ' % (prompt, (' [%s]' % default) if default else '')).strip()
    except EOFError:
        print()
        return default
    return s if s else default


def ask_float(prompt, default=None):
    while True:
        s = ask(prompt, '' if default is None else str(default))
        if not s:
            return default
        try:
            return float(s)
        except ValueError:
            bad('请输入数字（例如 1.5）')


def ask_int(prompt, default):
    while True:
        s = ask(prompt, str(default))
        try:
            return int(s)
        except ValueError:
            bad('请输入整数')


def confirm(prompt, default=True):
    d = 'y' if default else 'n'
    s = ask('%s (y/n)' % prompt, d).lower()
    return s.startswith('y')


def pause():
    try:
        input('\n按回车返回菜单…')
    except EOFError:
        pass


def rundir():
    # 必须绝对：run_py 的子进程 cwd 是 radar-pointcloud/，
    # 相对仓库根的路径在里面会解析失败（踩过 FileNotFoundError）
    d = os.path.abspath(STATE.get('dir') or DEFAULT_DIR)
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def resolve_file(p):
    """把粘贴进来的路径解析成绝对路径（**修 step_band 的 FileNotFoundError**）。

    为什么需要：`run_py()` 的子进程 cwd 固定是 `radar-pointcloud/`，而用户从仓库根
    复制的路径（`.deepworks\\tmp\\...`）在这个 cwd 下不存在 ⇒ 下层直接
    `FileNotFoundError`，但界面只说「退出码 1」，看不出是路径问题。

    解析顺序（都相对已存在的目录）：
      1. 绝对路径 → 原样
      2. `radar-pointcloud/` 相对（`radar_calibrate.py` 文档里的 `..\\.deepworks\\...`）
      3. 仓库根相对（用户从资源管理器/根目录复制的 `.deepworks\\...`）
    都不存在时返回仓库根下的绝对路径，让下层的报错里能看到**完整路径**。
    """
    if not p:
        return p
    # Windows「复制为路径」会带双引号，手打时也可能带单引号
    p = p.strip().strip('"').strip("'")
    p = os.path.expandvars(os.path.expanduser(p))
    if os.path.isabs(p):
        return os.path.abspath(p)
    for base in (HERE, ROOT):
        cand = os.path.abspath(os.path.join(base, p))
        if os.path.exists(cand):
            return cand
    return os.path.abspath(os.path.join(ROOT, p))


def run_py(script, args, cwd=None):
    """跑同一目录下的 python 脚本，输出直通终端（不吞日志——证据要留在屏幕上）。"""
    path = os.path.join(HERE, script)
    if not os.path.exists(path):
        bad('找不到 %s' % path)
        return 127
    cmd = [sys.executable, path] + [str(a) for a in args]
    print()
    # 只把脚本名缩成 basename，参数原样打印 —— 曾经把所有参数都 basename 了，
    # 结果日志里 `--file D:\...\pos_0p70.bin` 显示成 `--file pos_0p70.bin`（证据失真）
    shown = [os.path.basename(path)] + [('"%s"' % a if ' ' in a else a) for a in cmd[2:]]
    print('  $ python %s' % ' '.join(shown))
    print()
    # 子进程也兜一层编码容错：cp936 控制台打印 '⇒' 会 UnicodeEncodeError 直接
    # exit 1，界面只会说「退出码 1」，而日志里其实已经算完了（踩过）。
    # 不写死编码，只补 errors=replace；用户自己设了 PYTHONIOENCODING 就不动。
    env = os.environ.copy()
    env.setdefault('PYTHONIOENCODING', ':replace')
    try:
        return subprocess.call(cmd, cwd=cwd or HERE, env=env)
    except KeyboardInterrupt:
        warn('已中断')
        return 130


# ---------------------------------------------------------------- 真值读写
def load_labels_raw(path):
    """返回 (meta, labels)。**保留 _meta** —— label_tool.load_labels 会把它丢掉。"""
    meta, labels = None, []
    if not os.path.exists(path):
        return meta, labels
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if '_meta' in obj:
                meta = obj['_meta']
            elif 'action' in obj:
                labels.append(obj)
    return meta, labels


def save_labels_raw(path, meta, labels):
    with open(path, 'w', encoding='utf-8') as f:
        if meta:
            f.write(json.dumps({'_meta': meta}, ensure_ascii=False) + '\n')
        for lb in labels:
            f.write(json.dumps(lb, ensure_ascii=False) + '\n')


def find_segments(d):
    """找出目录下所有 (bin, labels) 配对。"""
    out = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith('.bin'):
            continue
        stem = name[:-4]
        for cand in (stem + '.labels.jsonl', stem + '.labels.json'):
            p = os.path.join(d, cand)
            if os.path.exists(p):
                out.append((os.path.join(d, name), p))
                break
    return out


def dx_status(labels):
    """统计实测位移录入情况。"""
    acts = [l for l in labels if l.get('action') in ('LEFT', 'RIGHT')]
    withdx = [l for l in acts if isinstance(l.get('dx_cm'), (int, float))]
    return len(withdx), len(acts)


# ---------------------------------------------------------------- 环境自检
def list_ports():
    try:
        from serial.tools import list_ports as lp
    except Exception as e:
        return None, 'pyserial 不可用：%s' % e
    ports = []
    for p in lp.comports():
        ports.append((p.device, p.description or '', p.hwid or ''))
    return ports, None


def step_env(args=None):
    title('0. 环境与链路自检')
    v = sys.version_info
    if v[:2] >= (3, 7):
        ok('Python %d.%d.%d' % (v[0], v[1], v[2]))
    else:
        bad('Python %d.%d.%d 过旧（需 ≥3.7）' % (v[0], v[1], v[2]))

    say('')
    say('  依赖模块：')
    for mod in ('numpy', 'serial', 'winsound'):
        try:
            __import__(mod)
            ok(mod)
        except Exception as e:
            (warn if mod == 'winsound' else bad)('%s 不可用（%s）' % (mod, e))

    say('')
    say('  串口候选（USB-UART 芯片应为 **CP2102**，装 **CP210x 驱动**；')
    say('  小组原方案写的 FTDI 是错的 —— 见 AGENTS.md 事实基线）：')
    ports, err = list_ports()
    if err:
        bad(err)
    elif not ports:
        bad('未发现任何串口。检查：USB 是否插好 / CP210x 驱动是否安装 / 是否被别的程序占用')
    else:
        for dev, desc, hwid in ports:
            tag = ''
            if 'CP210' in (desc + hwid).upper():
                tag = '  ← CP2102，雷达应当用这个'
            say('    %-8s %s%s' % (dev, desc, tag))
        STATE['port'] = ports[0][0]

    say('')
    say('  校准文件：')
    for p, keys in ((CALIB, ('processor', 'band', 'calibration')),
                    (os.path.join(HERE, 'bg_baseline.json'), None)):
        if not os.path.exists(p):
            warn('%s 不存在' % os.path.basename(p))
            continue
        try:
            with open(p, 'r', encoding='utf-8') as f:
                j = json.load(f)
        except Exception as e:
            bad('%s 解析失败：%s' % (os.path.basename(p), e))
            continue
        if keys:
            ok('%s：processor=%s band=%s' % (
                os.path.basename(p),
                {k: j.get('processor', {}).get(k) for k in
                 ('angle_sign', 'min_snr', 'min_range', 'angle_max')},
                j.get('band')))
        else:
            ok('%s：%d 个键' % (os.path.basename(p), len(j)))

    say('')
    warn('`angle_max` 是**软件截断**（radar_dsp.py L165），不是物理视场（阵元间距 = λ/2 ⇒ 无模糊 ±90°）。')
    warn('§7.41 实测：抬到 80° 无识别增益、误报 +36% ⇒ **保持 45**。')

    say('')
    say('  下一步若要看实时画面 / 做空场底噪：')
    say('    直接双击 radar-pointcloud\\start_viewer.cmd')
    if confirm('现在启动实时点云界面？', default=False):
        cmdc = os.path.join(HERE, 'start_viewer.cmd')
        if os.path.exists(cmdc):
            subprocess.Popen(['cmd', '/c', 'start', '', cmdc], cwd=HERE)
            ok('已在独立窗口启动（关掉那个窗口即可停止）')
        else:
            run_py('start_viewer.py', [])
    return 0


# ---------------------------------------------------------------- 1 空场底噪
def step_bg(args=None):
    title('1. 空场底噪 / 环境基线')
    say('  目的：记录**房间没人**时的点云占用率，作为「静态杂波」的基线。')
    say('  这一项必须在**所有人离开房间**后做；人在场的数据不能当底噪。')
    say('')
    warn('采集时房间无人这一条，软件无法验证 —— 必须由人确认并写进记录。')
    say('')
    say('  操作：启动实时点云界面 → 等人全部离开 → 点界面上的「环境底噪（背景基线）」按钮')
    say('        得到的是 `bg_baseline.json`（占用率 + 推荐带）。')
    if confirm('现在启动实时点云界面（用于底噪）？'):
        cmdc = os.path.join(HERE, 'start_viewer.cmd')
        if os.path.exists(cmdc):
            subprocess.Popen(['cmd', '/c', 'start', '', cmdc], cwd=HERE)
            ok('已启动')
        else:
            run_py('start_viewer.py', [])
    say('')
    say('  采完可离线核对（占用率口径自检）：')
    say('    python radar_bgcalib.py')
    return 0


# ---------------------------------------------------------------- 2 站位与带
def step_band(args=None):
    title('2. 站位与距离带校准')
    say('  目的：确认你打算站的**新站位（1.5~2.0 m）**是否落在带内，并拿到推荐带。')
    say('  这是复采能不能用的前提：站位变了，`band` 和 `xbase` 阈值都必须重推。')
    say('')
    src = ask('数据源 (live/file)', 'live').lower()
    band_args = ['--mode', 'band']
    if src.startswith('f'):
        f = resolve_file(ask('录制文件路径(.bin)'))
        if not os.path.exists(f):
            bad('文件不存在：%s' % f)
            say('  提示：仓库根相对路径（.deepworks\\tmp\\...）和 radar-pointcloud\\ 相对')
            say('        路径（..\\.deepworks\\tmp\\...）现在都能识别，仍报错说明文件真的不在。')
            return 1
        say('  → 使用绝对路径：%s' % f)
        band_args += ['--file', f]
    else:
        band_args += ['--port', ask('串口', STATE.get('port') or 'COM3')]
        band_args += ['--seconds', str(ask_float('采集秒数', 20.0))]
    band_args += ['--board', ask('板型 (auto/1t2r/2t4r)', 'auto')]
    if confirm('是否把结果写入 calib.json？', default=True):
        pass
    else:
        band_args += ['--dry-run']
    rc = run_py('radar_calibrate.py', band_args)
    if rc == 0:
        ok('完成。把推荐带填进后续采集与分析。')
    else:
        bad('退出码 %s，请先解决再继续' % rc)
    return rc


# ---------------------------------------------------------------- 3 静息采集
def step_rest(args=None):
    title('3. 静息站位采集（复采）')
    say('  目的：得到**人在场、完全不动**的对照段（任何 LANE_* 事件都是误报）。')
    say('  站位与复采站位**必须一致**，否则误报率不可比。')
    say('')
    stations = ask('站位距离（米，逗号分隔）', DEFAULT_STATIONS)
    a = ['--port', ask('串口', STATE.get('port') or 'COM3'),
         '--stations', stations,
         '--duration', str(ask_float('每站时长(秒)', 30.0)),
         '--outdir', rundir(),
         '--board', ask('板型', '1t2r')]
    rc = run_py('position_capture.py', a)
    if rc == 0:
        ok('静息段已落到 %s' % rundir())
        warn('静息段没有真值行，靠「无动作」这个已知事实当对照 —— 别把它当动作数据。')
    return rc


# ---------------------------------------------------------------- 4 动作复采
def step_action(args=None):
    title('4. 左右动作复采（带地面尺标）')
    say('  这是本轮最关键的一次采集。开工前请先在地上做好标记：')
    say('    · 站位线：1.5 m 与 2.0 m 各一条（与雷达正面平行）')
    say('    · 横向刻度：在站位线上从**人体中心**向左右各标 10/20/30/40/60 cm')
    say('  ⇒ 每次动作后，记录人能报出「我移了多远」。**没有这个数，复采就白采。**')
    say('')
    say('  --auto 是无人值守模式：人站在雷达前看不到屏幕、够不到键盘，')
    say('  全程由语音 + 提示音驱动（3 声短拍=回中间，「嘟」=开始，低「嘟」=停）。')
    say('')
    stations = ask('站位距离（米）', DEFAULT_STATIONS)
    reps = ask_int('每个动作重复次数（左右各 N 次，建议 ≥7）', 7)
    stride = ask_float('要求的最小横移距离(米)（真值备注会写它）', DEFAULT_STRIDE)
    a = ['--port', ask('串口', STATE.get('port') or 'COM3'),
         '--auto',
         '--distances', stations,
         '--actions', 'left,right',
         '--reps', str(reps),
         '--stride', str(stride),
         '--outdir', rundir(),
         '--board', ask('板型', '1t2r')]
    if confirm('关掉语音播报（--no-speak）？', default=False):
        a.append('--no-speak')
    rc = run_py('segment_capture.py', a)
    if rc == 0:
        ok('动作段已落到 %s' % rundir())
        n, tot = 0, 0
        for _b, lp in find_segments(rundir()):
            meta, labels = load_labels_raw(lp)
            c, t = dx_status(labels)
            n += c
            tot += t
            say('    %-28s 左右真值 %d 条，其中**已有实测位移** %d 条'
                % (os.path.basename(lp), t, c))
        say('')
        if tot and n < tot:
            warn('还有 %d 条缺实测位移 ⇒ 下一步「5. 实测位移录入」必须做完，'
                 '否则无法区分「人没移到位」与「雷达对 LEFT 退化」' % (tot - n))
        else:
            ok('实测位移已齐（或本段无左右动作）')
    return rc


# ---------------------------------------------------------------- 5 位移录入
def step_dx(args=None):
    title('5. 实测位移录入（补齐真值缺口，§7.41 的决定性一环）')
    say('  为什么必须做：现有真值只有**指令值**「要求横移≥0.6 m」，没有实测值。')
    say('  所以 LEFT dev 只有 0.161 m 时，无法判断是「人没真移 0.6 m」还是')
    say('  「雷达对 LEFT 退化」。补上实测位移，这个二选一当场可分。')
    say('')
    segs = find_segments(rundir())
    if not segs:
        bad('在 %s 下没找到 (bin, labels) 配对，请先做第 4 步' % rundir())
        return 1

    say('  待录文件：')
    for i, (b, lp) in enumerate(segs):
        meta, labels = load_labels_raw(lp)
        c, t = dx_status(labels)
        say('    [%d] %-26s 左右 %d 条，已录 %d 条%s'
            % (i + 1, os.path.basename(b), t, c, '' if c == t else '   ← 待补'))
    if len(segs) == 1:
        say('    → 只有一段，自动选中。')
        idx = 0
    elif STATE.get('segment'):
        idx = STATE['segment'] - 1
        if not (0 <= idx < len(segs)):
            bad('--segment %d 越界（本目录共 %d 段）' % (STATE['segment'], len(segs)))
            return 1
    else:
        idx = ask_int('选哪个（1 起）', 1) - 1
        if not (0 <= idx < len(segs)):
            bad('序号越界')
            return 1
    binp, lp = segs[idx]
    meta, labels = load_labels_raw(lp)
    if not labels:
        bad('该文件没有真值行')
        return 1

    who = ask('量测人姓名/代号（写进真值，证据纪律要求）', '')
    mdef = ask('量测方式 mark(地面尺标)/tape(卷尺)/eyeball(目测)', 'mark')

    todo = [l for l in labels if l.get('action') in ('LEFT', 'RIGHT')]
    say('')
    say('  共 %d 条左右动作。逐条输入**实测横向位移的绝对值**（厘米，只填数字）。' % len(todo))
    say('  直接回车 = 跳过（保持缺省）；填 -1 = 本次放弃录入并退出。')
    say('  （左右方向不用填：action 字段已给出方向）')
    say('')

    filled = 0
    for i, lb in enumerate(todo):
        pre = lb.get('dx_cm')
        hint = ('  已有 %.0f cm' % pre) if isinstance(pre, (int, float)) else ''
        side = '左' if lb['action'] == 'LEFT' else '右'
        p = '  (%d/%d) %s 第 %s 次%s → 实测位移 cm' % (
            i + 1, len(todo), side, lb.get('rep', i + 1), hint)
        s = ask(p, '' if pre is None else str(int(pre)))
        if s == '-1':
            warn('放弃本次录入')
            break
        if s == '':
            continue
        try:
            v = float(s)
        except ValueError:
            warn('不是数字，跳过该条')
            continue
        if v < 0:
            warn('位移不能为负（方向由 action 决定），跳过')
            continue
        lb['dx_cm'] = round(v, 1)
        lb['dx_method'] = mdef
        if who:
            lb['dx_by'] = who
        filled += 1

    if filled:
        save_labels_raw(lp, meta, labels)
        c, t = dx_status(labels)
        ok('已写入 %d 条 → %s（左右真值 %d 条，现有实测位移 %d 条）'
           % (filled, os.path.basename(lp), t, c))
    else:
        warn('没有写入任何内容')
    return 0


# ---------------------------------------------------------------- 6 离线分析
def _pick_segment(prompt='选哪个（1 起）'):
    segs = find_segments(rundir())
    if not segs:
        bad('在 %s 下没找到 (bin, labels) 配对' % rundir())
        return None
    for i, (b, lp) in enumerate(segs):
        meta, labels = load_labels_raw(lp)
        c, t = dx_status(labels)
        d = (meta or {}).get('distance_m')
        say('    [%d] %-26s 站位 %s m  左右 %d 条（实测位移 %d 条）'
            % (i + 1, os.path.basename(b), d, t, c))
    if len(segs) == 1:
        say('    → 只有一段，自动选中。')
        return segs[0]
    if STATE.get('segment'):
        idx = STATE['segment'] - 1
        if 0 <= idx < len(segs):
            say('    → 按 --segment %d 选中。' % STATE['segment'])
            return segs[idx]
        bad('--segment %d 越界（本目录共 %d 段）' % (STATE['segment'], len(segs)))
        return None
    idx = ask_int(prompt, 1) - 1
    if not (0 <= idx < len(segs)):
        bad('序号越界')
        return None
    return segs[idx]


def step_analyze(args=None):
    title('6. 离线分析（crit=xbase 的现场表现 + 命中/漏报/误报对照表）')
    say('  这一项回答「命中多少、误报多少」。注意三点：')
    say('   (a) 默认判据自 2026-10-20 起是 `xbase`（devlog §7.45.6：`delta` 的方向符号在')
    say('       1.50/2.00 m 实采上与人体左右相反，0/11 正确；`xbase` 10/11 正确），')
    say('       所以 `--crit xbase` 现在只是显式写出来，不再必需。')
    say('   (b) `radar_actions.py` 已有 `--board` 旗标（2026-10-20 加），但**仍会自动嗅探**；')
    say('       裸流有被误判成 2T4R 的先例 ⇒ 输出里那行「[actions] 板型 = …」')
    say('       **必须核一眼**。若显示 2t4r 而实际是 1T2R，本步结果作废。')
    say('   (c) `label_tool.py --compare` 用的是**默认判据**（现为 xbase），')
    say('       与 live 一致；下面第 2 段由本引导再补算一遍（带真值窗与延迟）。')
    say('')
    pick = _pick_segment()
    if not pick:
        return 1
    binp, lp = pick
    meta, labels = load_labels_raw(lp)
    band = (meta or {}).get('band') or [0.70, 1.35]
    mboard = (meta or {}).get('board') or '1t2r'
    if mboard != '1t2r':
        warn('这段数据 meta 记录的板型是 %s，务必与下面的自动嗅探结果核对' % mboard)

    say('')
    say('  --- 第 1 段：产线 CLI 原样跑（原始事件日志，可留档当证据）---')
    rc = run_py('radar_actions.py', ['--file', binp, '--crit', 'xbase',
                                     '--band', str(band[0]), str(band[1])])

    if not labels:
        warn('该文件没有真值行 ⇒ 没有命中率可算。')
        warn('若这是**静息段**：对照口径下任何 LANE_* 都是误报，'
             '用上面日志里的 LANE_* 条数 ÷ 段长 算每分钟误报。')
        return rc

    say('')
    say('  --- 第 2 段：xbase 的命中/漏报/误报对照表（tol=±0.45 s）---')
    try:
        import label_tool as lt
    except Exception as e:
        bad('label_tool 不可用（%s）' % e)
        return rc
    try:
        records, _info = analyze_cached(binp, tuple(band), mboard, log=say)
    except Exception as e:
        bad('离线分析失败：%s: %s' % (type(e).__name__, e))
        return rc
    lt.report(records, labels, lp, tol=0.45)
    show_hit_compare(records, labels, board=mboard)
    say('')
    say('  读法（重要）：')
    say('   · 命中率要**对照零假设**看（§7.40：真实 93% vs 随机平移 19%）。')
    say('     单看「命中 7/7」不算证据 —— 事件密度高也能刷出高命中。')
    say('   · 跳/蹲那一行**不能当验收依据**（当前硬件无俯仰角，只能给候选项）。')
    say('   · 想看 delta 判据的同一张表（做对照基线）：')
    say('       python label_tool.py --file %s --compare' % os.path.basename(binp))
    return rc


# ---------------------------------------------------------------- 7 决定性回归
def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def _slope_origin(xs, ys):
    """过原点斜率 k = Σ(xy)/Σ(x²)：物理含义 = 每 1 m 实移，量到多少 m 位移。"""
    sxx = sum(x * x for x in xs)
    if sxx <= 0:
        return None
    return sum(x * y for x, y in zip(xs, ys)) / sxx


def hit_stats(records, labels, tail_frames=40, frame_period=0.1295, tol=0.45):
    """**两种口径**的命中统计 —— 同一份数据、同一判据，只差容差。

    严口径（strict）= 产线工具 `label_tool.compare` 的默认 ±0.45 s
    宽口径（loose） = 真值窗末端再放宽 `tail_frames` 帧 = §7.40 的 evaluate 口径

    为什么必须两个都报：识别本身有约 2.3 s 固有延迟，事件常落在真值窗**之后**，
    于是严格容差把它记成漏报。同一段数据严格 71%、宽松 93% 都「对」，
    但**只有宽口径可比 §7.40 的 93%**；不写明口径就会被当成互相矛盾。
    """
    import label_tool as lt
    preds = []
    for r in records:
        for e in (r.get('events') or []):
            act = lt.EV_TO_ACT.get(e.get('name'))
            if act:
                preds.append((act, e.get('t')))
    out = {}
    for tag, t_lo, t_hi in (('strict', tol, tol),
                            ('loose', tol, tail_frames * frame_period)):
        st = {}
        for lb in labels:
            act = lb.get('action')
            if not act:
                continue
            s = st.setdefault(act, {'truth': 0, 'hit': 0})
            s['truth'] += 1
            t0 = lb['t_start'] - t_lo
            t1 = lb['t_end'] + t_hi
            if any(a == act and isinstance(t, (int, float)) and t0 <= t <= t1
                   for a, t in preds):
                s['hit'] += 1
        out[tag] = st
    return out


def show_hit_compare(records, labels, board='1t2r'):
    """把两种口径并排打出来，避免「71% vs 93%」被当成矛盾。"""
    try:
        import radar_source as rs
        fp = rs.frame_period(board)
    except Exception:
        fp = 0.1295
    h = hit_stats(records, labels, frame_period=fp)
    acts = sorted(set(h['strict']) | set(h['loose']))
    if not acts:
        return
    say('')
    hr()
    say('  两种「命中」口径对照（同一数据、同一判据，只差容差）')
    say('   动作           严口径 ±0.45s      宽口径 窗末+40帧(≈+%.1fs)' % (40 * fp))
    hr()
    tot = {'strict': [0, 0], 'loose': [0, 0]}
    lane = {'strict': [0, 0], 'loose': [0, 0]}
    for act in acts:
        s0 = h['strict'].get(act, {'truth': 0, 'hit': 0})
        s1 = h['loose'].get(act, {'truth': 0, 'hit': 0})
        say('   %-12s   %2d/%-2d = %3.0f%%      %2d/%-2d = %3.0f%%'
            % (ACTION_TEXT_CN.get(act, act),
               s0['hit'], s0['truth'],
               100.0 * s0['hit'] / s0['truth'] if s0['truth'] else 0,
               s1['hit'], s1['truth'],
               100.0 * s1['hit'] / s1['truth'] if s1['truth'] else 0))
        for tag, s in (('strict', s0), ('loose', s1)):
            tot[tag][0] += s['hit']
            tot[tag][1] += s['truth']
            if act in ('LEFT', 'RIGHT'):
                lane[tag][0] += s['hit']
                lane[tag][1] += s['truth']
    hr()
    say('   左右小计       %2d/%-2d = %3.0f%%      %2d/%-2d = %3.0f%%'
        % (lane['strict'][0], lane['strict'][1],
           100.0 * lane['strict'][0] / lane['strict'][1] if lane['strict'][1] else 0,
           lane['loose'][0], lane['loose'][1],
           100.0 * lane['loose'][0] / lane['loose'][1] if lane['loose'][1] else 0))
    say('')
    warn('**只有宽口径可比 §7.40 的 93%**；产线工具 label_tool 默认是严口径。')
    warn('两者差 = 识别约 2.3 s 的固有延迟 ⇒ 事件落在真值窗**之后**被记成漏报。')
    warn('对外写数字时必须写明口径，否则 71% 与 93% 会被当成互相矛盾。')
    say('')
    say('  严口径的意义：它才接近**玩家实际感受**（动作做完多久内角色换道才有用）。')
    say('  宽口径的意义：它衡量**信息是否存在**（信号里到底有没有这次移动）。')


ACTION_TEXT_CN = {'LEFT': '左移', 'RIGHT': '右移', 'DUCK': '蹲(候选)', 'JUMP': '跳(候选)'}


def _feat(r, key):
    """取逐帧特征。

    `radar_actions.analyze_file` 返回的 record 是**嵌套**的：
      {'frame','t','lane','target','noise','events','features'}
    逐帧特征（px/xdev/xbase…）在 `features` 子字典里，不在顶层。
    """
    f = r.get('features')
    if isinstance(f, dict) and key in f:
        return f[key]
    return r.get(key)


_ANALYSIS_CACHE = {}


def _build_processor(log=print):
    from radar_dsp import PointCloudProcessor
    proc = PointCloudProcessor()
    if os.path.exists(CALIB):
        try:
            with open(CALIB, 'r', encoding='utf-8') as f:
                p = (json.load(f) or {}).get('processor') or {}
            if p:
                proc = PointCloudProcessor(
                    cfar_scale=float(p.get('cfar_scale', 2.6)),
                    min_snr=float(p.get('min_snr', 7.0)),
                    remove_clutter=True,
                    angle_sign=float(p.get('angle_sign', 1.0) or 1.0),
                    max_points=64,
                    angle_max=float(p.get('angle_max', 45.0)),
                    min_range=float(p.get('min_range', 0.0)))
        except Exception as e:
            log('  [注意] calib.json 读取失败（%s），改用内置默认' % e)
    return proc


def analyze_cached(binp, band, board, log=None):
    """逐帧分析并**缓存**：步骤 6 与步骤 7 共用一次分析，不重复跑。

    与产线 CLI 的差别：这里**显式传 board**，绕开 `analyze_file` 的自动嗅探
    （裸流有被误判成 2T4R 的先例）。
    """
    key = (os.path.abspath(binp), tuple(band), board)
    if key in _ANALYSIS_CACHE:
        if log:
            log('  （复用本会话已算好的逐帧记录，跳过重复分析）')
        return _ANALYSIS_CACHE[key]
    import radar_actions as ra
    proc = _build_processor(log or (lambda *a: None))
    rec = ra.ActionRecognizer(band=tuple(band), crit='xbase',
                              window=ra.WINDOW_DEFAULT,   # 2026-10-20 起与 live 默认对齐（8）
                              min_pts=2, confirm=3, min_interval=0.5)
    records, info = ra.analyze_file(binp, processor=proc, recognizer=rec,
                                    band=tuple(band), board=board,
                                    log=lambda *a: None)
    _ANALYSIS_CACHE[key] = (records, info)
    return records, info


def regress_rows(records, labels, win_tail=40):
    """纯函数：把逐帧记录里「真值窗内正确方向的最大 dev」与实测位移配成对。

    win_tail：真值窗末端再放宽多少帧（与 §7.40 的 evaluate 同口径，便于对比）。
    """
    byframe = {r.get('frame'): r for r in records}
    rows = []
    seq = {'LEFT': 0, 'RIGHT': 0}
    for lb in labels:
        act = lb.get('action')
        if act not in ('LEFT', 'RIGHT'):
            continue
        dx = lb.get('dx_cm')
        if not isinstance(dx, (int, float)):
            continue
        # 真值的 schema 里没有「第几次」字段 ⇒ 按方向自己数
        seq[act] += 1
        f0, f1 = lb.get('frame_start'), lb.get('frame_end')
        if f0 is None or f1 is None:
            continue
        devs = []
        for f in range(f0, f1 + win_tail + 1):
            r = byframe.get(f)
            if not r:
                continue
            v = _feat(r, 'xdev')
            if isinstance(v, (int, float)):
                devs.append(v)
        if not devs:
            continue
        signed = max(devs) if act == 'LEFT' else min(devs)
        rows.append(dict(action=act, dx_m=dx / 100.0, dev=signed,
                         rep=lb.get('rep') or seq[act], method=lb.get('dx_method')))
    return rows


def regress_one(binp, lp, band, board, log=print):
    """分析 + 配对（保留给外部脚本调用的便捷入口）。"""
    meta, labels = load_labels_raw(lp)
    if meta:
        band = tuple(meta.get('band') or band)
        board = meta.get('board') or board
    records, info = analyze_cached(binp, band, board, log=log)
    return regress_rows(records, labels), info


def step_regress(args=None):
    title('7. ★ 决定性回归：实测位移 vs 雷达量到的位移')
    say('  问的问题只有一个：**同样的实际位移，LEFT 和 RIGHT 量到的 dev 是否一样？**')
    say('    · 若两边斜率接近 ⇒ 雷达量得对称 ⇒ LEFT dev 小是因为「人没真移那么远」')
    say('      ⇒ 根因在**协议/场地**，不是雷达；调参永远修不好，重采协议才是解。')
    say('    · 若 LEFT 斜率显著低于 RIGHT ⇒ 雷达对 LEFT 真的退化 ⇒ 才值得去查')
    say('      视场/几何/信号，或考虑改变安装。')
    say('  两种结论对应完全不同的下一步 —— 这就是补实测位移的全部意义。')
    say('')
    pick = _pick_segment()
    if not pick:
        return 1
    binp, lp = pick
    meta, labels = load_labels_raw(lp)
    band = tuple((meta or {}).get('band') or (0.70, 1.35))
    board = (meta or {}).get('board') or '1t2r'

    if not os.path.exists(os.path.join(HERE, 'radar_actions.py')):
        bad('找不到 radar_actions.py')
        return 1

    say('')
    say('  正在离线分析 %s …（约 1~2 s / 100 帧）' % os.path.basename(binp))
    rows, info = regress_one(binp, lp, band, board, log=say)
    if not rows:
        bad('没有可用样本：该文件的左右真值行都缺 `dx_cm`')
        warn('请先做第 5 步「实测位移录入」；指令值（--stride）不能代替实测值')
        return 1

    say('')
    hr()
    say('  逐条样本（dev = 真值窗内正确方向的位移峰值，单位 m；dx = 实测位移，单位 m）')
    hr()
    say('  %-6s %-5s %8s %10s %10s  %s' % ('动作', '序号', '实测m', '量到m', '比值', '量测方式'))
    for r in rows:
        ratio = (abs(r['dev']) / r['dx_m']) if r['dx_m'] > 0 else float('nan')
        say('  %-6s %-5s %8.2f %10.3f %10.2f  %s'
            % ('左' if r['action'] == 'LEFT' else '右',
               r.get('rep') if r.get('rep') is not None else '-',
               r['dx_m'], r['dev'], ratio, r.get('method') or '-'))

    say('')
    hr()
    say('  分方向汇总')
    hr()
    say('  %-6s %5s %10s %10s %10s %10s' % ('动作', 'n', '实测m中位', '量到m中位', '过原点斜率', '相关r'))
    stats = {}
    for act, name in (('LEFT', '左'), ('RIGHT', '右')):
        sub = [r for r in rows if r['action'] == act]
        if not sub:
            say('  %-6s %5d %10s %10s %10s %10s' % (name, 0, '-', '-', '-', '-'))
            continue
        dxs = [r['dx_m'] for r in sub]
        devs = [abs(r['dev']) for r in sub]
        k = _slope_origin(dxs, devs)
        rr = _pearson(dxs, devs)
        stats[act] = dict(n=len(sub), dx=dxs, dev=devs, k=k, r=rr)
        say('  %-6s %5d %10.2f %10.3f %10s %10s'
            % (name, len(sub),
               sorted(dxs)[len(dxs) // 2], sorted(devs)[len(devs) // 2],
               ('%.2f' % k) if k else '-', ('%.2f' % rr) if rr else '-'))

    say('')
    hr('=')
    say('  判定')
    hr('=')
    L, R = stats.get('LEFT'), stats.get('RIGHT')
    if not L or not R:
        warn('左右样本不齐（左 %d / 右 %d），无法做对称性判定' %
             (L['n'] if L else 0, R['n'] if R else 0))
        return 0

    mdx_l = sorted(L['dx'])[len(L['dx']) // 2]
    mdx_r = sorted(R['dx'])[len(R['dx']) // 2]
    if max(mdx_l, mdx_r) > 0:
        gap = (mdx_r - mdx_l) / max(mdx_l, mdx_r) * 100.0
        say('  ① 实测位移本身：左中位 %.2f m vs 右中位 %.2f m（差 %.0f%%）'
            % (mdx_l, mdx_r, gap))
        if abs(gap) > 25:
            warn('  ⇒ **人左右根本没移一样远**。这一条本身就足以解释 LEFT dev 偏小；')
            warn('    以前没有实测位移，这个原因被误当成「雷达对 LEFT 退化」。')
        else:
            say('  ⇒ 左右实测位移基本相当，可以继续看②')

    if L['k'] and R['k']:
        say('  ② 量测效率（每 1 m 实移量到多少 m）：左 %.2f vs 右 %.2f' % (L['k'], R['k']))
        ratio = L['k'] / R['k'] if R['k'] else float('inf')
        say('     左/右 = %.2f' % ratio)
        say('')
        if ratio > 0.75:
            ok('判定：**雷达左右量得对称**（比值 %.2f > 0.75）' % ratio)
            say('     ⇒ LEFT 弱侧的根因在**协议/场地（实际位移不足）**，')
            say('       不是雷达退化 ⇒ 调整雷达安装（含斜放）**修不好**；')
            say('       修法是改采集协议 + 确认人每次都移到位。')
        else:
            bad('判定：**LEFT 量测效率显著低于 RIGHT**（比值 %.2f）' % ratio)
            say('     ⇒ 雷达对左向确实退化 ⇒ 值得继续查视场/几何/信号。')
            say('       §7.41 已排除「±45° 软件截断」这一条；下一步查安装几何')
            say('       与站位距离（可用本引导第 2 步换站位复采对照）。')
    else:
        warn('样本不足以算斜率（每方向至少 3 条带实测位移的真值）')

    say('')
    if L['r'] is not None and L['r'] < 0.3:
        warn('左向相关 r=%.2f 偏低 ⇒ dev 与实测位移关系弱，可能存在未记录因素'
             '（激励不足 / 记录不准 / 站位漂移）' % L['r'])
    if L['r'] is None or R['r'] is None:
        warn('相关 r 显示为「-」= 该方向实测位移**没有变化**（方差为 0），'
             '相关系数在数学上无定义。此时只看斜率，r 无从判断。')
        warn('若想得到 r，请让每次动作的位移**有差异**（例如 20/30/40 cm 混着做）。')
    say('  诚实边界：本判定依赖**实测位移的准确性**。若用的是目测（eyeball），')
    say('  误差可能就有 ±10 cm 量级 —— 那结论只能当方向性参考，不能当定量结论。')
    return 0


# ---------------------------------------------------------------- 8 回归自测
def step_selftest(args=None):
    title('8. 项目回归自测（selftest.py）')
    say('  改动过任何产线文件后必须跑；全绿才允许进入验收。')
    say('  特别关注这两条：')
    say('    · `crit 默认 = xbase` —— 默认判据已按 §7.45.6 从 delta 改为 xbase，')
    say('      这条断言防的是「再被静默改回去」')
    say('    · `闭环 40/40`   —— 编码-解码环回（与动作判据的用例条数无关）')
    say('')
    if not confirm('现在跑？（约 1~3 分钟）'):
        return 0
    rc = run_py('selftest.py', [])
    if rc == 0:
        ok('selftest 全绿')
    else:
        bad('selftest 退出码 %s —— 先修，再谈验收' % rc)
    return rc


# ---------------------------------------------------------------- 一键流程
def step_pipeline(args=None):
    title('9. 一键走完「复采 → 录入 → 分析 → 回归」')
    say('  顺序固定、每步失败即停，避免出现「采了但没录位移」的废数据。')
    say('  预计用时：布置尺标 5 min + 采集 15~25 min + 录入 10 min + 分析 3 min。')
    say('')
    warn('开工前：其它 2.4G/5G 设备能关就关（§7.11 实测干扰会让目标丢失）。')
    if not confirm('准备好了，开始？'):
        return 0
    for name, fn in (('4. 动作复采', step_action), ('5. 实测位移录入', step_dx),
                     ('7. 决定性回归', step_regress)):
        rc = fn()
        if rc not in (0, None):
            bad('「%s」未通过（退出码 %s），流程中止' % (name, rc))
            return rc
    ok('流程走完。把第 7 步的判定结论抄进 docs/progress-radar-pointcloud.md')
    return 0


def step_session(args=None):
    title('10. 现场一条线：自检 → 定带 → 静息 → 动作复采 → 录位移 → 决定性回归')
    say('  这是「本轮复采」的推荐入口（双击 m2_session.cmd 同效）。')
    say('  它跟第 9 步的区别：多做了**环境自检 / 定带 / 静息**，因为换站位后')
    say('  `band`、`xbase`、误报率全部要重推 —— 沿用 0.7 m 的旧值会得出错误结论。')
    say('')
    warn('开工前：房间里别有其他人走动；其它 2.4G/5G 设备能关就关（§7.11）。')
    warn('地面尺标（0 点 = 静息站位中心，左右各到 ±0.6 m，每 10 cm 一格）没做好，')
    warn('就别开始第 4 步 —— 没有实测位移，这轮数据仍然**归不了因**。')
    if not confirm('准备好了，开始？'):
        return 0

    seq = (('0. 环境与链路自检', step_env),
           ('2. 站位与距离带', step_band),
           ('3. 静息站位采集', step_rest),
           ('4. 左右动作复采', step_action),
           ('5. 实测位移录入', step_dx),
           ('7. 决定性回归', step_regress))
    for i, (name, fn) in enumerate(seq):
        rc = fn()
        if rc not in (0, None):
            bad('「%s」未通过（退出码 %s），流程中止' % (name, rc))
            return rc
        if i < len(seq) - 1 and not confirm('「%s」完成，继续下一步？' % name):
            warn('已停在「%s」之后。数据都留在 %s' % (name, rundir()))
            return 0
    say('')
    ok('一条线走完。把上面第 7 步的判定（左/右斜率比、r、样本数）抄进')
    say('   docs/progress-radar-pointcloud.md，并注明**量测方式**（mark/tape/eyeball）。')
    say('   别把「斜率比 > 0.75」写成定论 —— 0.75 是提案值，且每方向 7 次样本量下')
    say('   ±1 个窗的差异不显著。')
    return 0


# ---------------------------------------------------------------- 菜单
STEPS = [
    ('0', '环境与链路自检', step_env),
    ('1', '空场底噪 / 实时点云', step_bg),
    ('2', '站位与距离带校准', step_band),
    ('3', '静息站位采集（复采）', step_rest),
    ('4', '★ 左右动作复采（带尺标）', step_action),
    ('5', '★ 实测位移录入（补真值缺口）', step_dx),
    ('6', '离线分析（crit=xbase）', step_analyze),
    ('7', '★ 决定性回归：实测位移 vs 量到位移', step_regress),
    ('8', '项目回归自测 selftest', step_selftest),
    ('9', '一键走完复采流程', step_pipeline),
    ('s', '★ 现场一条线（含自检/定带/静息）', step_session),
]

STEP_BY_NAME = {
    'env': step_env, 'bg': step_bg, 'band': step_band, 'rest': step_rest,
    'action': step_action, 'dx': step_dx, 'analyze': step_analyze,
    'regress': step_regress, 'selftest': step_selftest, 'pipeline': step_pipeline,
    'session': step_session,
}

STATE = {'port': None, 'dir': None, 'segment': None}


def show_menu():
    title('毫米波雷达跑酷 · M2 验收检测引导')
    say('  工作目录：%s' % rundir())
    say('  串口    ：%s' % (STATE.get('port') or '（未探测，先做第 0 步）'))
    segs = find_segments(rundir())
    if segs:
        n, tot = 0, 0
        for _b, lp in segs:
            _m, labels = load_labels_raw(lp)
            c, t = dx_status(labels)
            n += c
            tot += t
        say('  已采数据：%d 段；左右真值 %d 条，其中已录实测位移 %d 条'
            % (len(segs), tot, n))
        if tot and n < tot:
            warn('实测位移未录齐 → 第 5 步。**不录就没有决定性结论。**')
        elif tot:
            ok('实测位移已录齐 → 可直接做第 7 步回归')
    else:
        say('  已采数据：（空）')
    say('')
    hr()
    for k, name, _fn in STEPS:
        star = ' ★' if '★' in name else ''
        say('   [%s] %s%s' % (k, name.replace('★ ', ''), star))
    say('   [q] 退出')
    hr()


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument('--step', default=None,
                    help='只跑一步：env/bg/band/rest/action/dx/analyze/regress/selftest/pipeline')
    ap.add_argument('--dir', default=None, help='工作目录（默认 .deepworks/tmp/m2）')
    ap.add_argument('--port', default=None, help='串口（默认自动探测）')
    ap.add_argument('--segment', type=int, default=None,
                    help='直接指定第 N 段（1 起），用于 --step 脚本化')
    ap.add_argument('--selftest', action='store_true', help='自检本引导自身（无需硬件）')
    a = ap.parse_args()

    STATE['dir'] = a.dir or DEFAULT_DIR
    STATE['port'] = a.port
    STATE['segment'] = a.segment

    if a.selftest:
        return guide_selftest()

    if a.step:
        fn = STEP_BY_NAME.get(a.step)
        if not fn:
            bad('未知步骤 %r；可选：%s' % (a.step, '/'.join(STEP_BY_NAME)))
            return 2
        return fn() or 0

    while True:
        show_menu()
        c = ask('选择').strip().lower()
        if c in ('q', 'quit', 'exit', ''):
            say('  退出。')
            return 0
        hit = [fn for k, _n, fn in STEPS if k == c]
        if not hit:
            bad('无效选择')
            continue
        try:
            hit[0]()
        except KeyboardInterrupt:
            warn('已中断')
        except Exception as e:
            bad('该步骤出错：%s: %s' % (type(e).__name__, e))
        pause()


def guide_selftest():
    """自检本引导自身：不碰硬件，只验证真值读写与回归数学是对的。"""
    import tempfile
    title('引导自检（无需硬件）')
    fails = []

    d = tempfile.mkdtemp(prefix='m2chk_')
    lp = os.path.join(d, 't.bin.labels.jsonl')
    meta = {'distance_m': 1.5, 'band': [0.70, 1.35], 'board': '1t2r',
            'note': '自检用'}
    labels = [
        {'file': 't.bin', 'action': 'LEFT', 'frame_start': 10, 'frame_end': 30,
         'note': 'x'},
        {'file': 't.bin', 'action': 'RIGHT', 'frame_start': 50, 'frame_end': 70,
         'note': 'x'},
        {'file': 't.bin', 'action': 'DUCK', 'frame_start': 90, 'frame_end': 110,
         'note': 'x'},
    ]
    save_labels_raw(lp, meta, labels)
    m2, l2 = load_labels_raw(lp)
    if m2 != meta:
        fails.append('_meta 往返丢失')
    if l2[0].get('dx_cm') is not None:
        fails.append('初始不应带 dx_cm')
    l2[0]['dx_cm'] = 24.0
    l2[0]['dx_method'] = 'mark'
    l2[0]['dx_by'] = 'selftest'
    save_labels_raw(lp, m2, l2)
    m3, l3 = load_labels_raw(lp)
    if l3[0].get('dx_cm') != 24.0 or l3[0].get('dx_by') != 'selftest':
        fails.append('dx_cm 持久化失败')
    c, t = dx_status(l3)
    if (c, t) != (1, 2):
        fails.append('dx_status 计数错（%d/%d，应为 1/2）' % (c, t))
    if any(l.get('dx_cm') for l in l3 if l['action'] == 'DUCK'):
        fails.append('非左右动作不该被要求位移')

    # 回归数学：构造 dev = 0.5 * dx 的关系（过原点斜率应 ≈0.5）
    for k, dv in ((0.25, 0.125), (0.40, 0.20), (0.60, 0.30)):
        _ = (k, dv)
    xs = [0.25, 0.40, 0.60]
    ys = [0.125, 0.20, 0.30]
    got = _slope_origin(xs, ys)
    if got is None or abs(got - 0.5) > 1e-6:
        fails.append('_slope_origin 错（%s，应为 0.5）' % got)
    r = _pearson(xs, ys)
    if r is None or abs(r - 1.0) > 1e-6:
        fails.append('_pearson 错（%s，应为 1.0）' % r)
    if _pearson([1.0], [1.0]) is not None:
        fails.append('_pearson 单点应返回 None')
    if _slope_origin([], []) is not None:
        fails.append('_slope_origin 空输入应返回 None')

    # 菜单/步骤表一致性
    names = set()
    for _k, _n, fn in STEPS:
        if fn.__name__ in names:
            fails.append('步骤函数重复：%s' % fn.__name__)
        names.add(fn.__name__)
    for key in STEP_BY_NAME:
        if STEP_BY_NAME[key] not in [fn for _k, _n, fn in STEPS]:
            fails.append('STEP_BY_NAME 里的 %s 不在菜单中' % key)
    keys = [k for k, _n, _fn in STEPS]
    if len(keys) != len(set(keys)):
        fails.append('菜单按键重复：%s' % keys)

    # 路径解析：三种写法都必须落到同一个已存在文件（step_band 的旧 bug）
    expect = os.path.join(HERE, 'selftest.py')
    for raw in ('selftest.py', 'radar-pointcloud/selftest.py',
                r'radar-pointcloud\selftest.py', ' "selftest.py" '):
        got = resolve_file(raw)
        if os.path.normcase(got) != os.path.normcase(expect):
            fails.append('resolve_file(%r) = %s，应为 %s' % (raw, got, expect))
    if resolve_file('no-such-file.bin') == 'no-such-file.bin':
        fails.append('resolve_file 对缺失文件应返回绝对路径（便于报错定位）')

    say('')
    if fails:
        for f in fails:
            bad(f)
        say('')
        bad('引导自检失败：%d 项' % len(fails))
        return 1
    ok('真值往返（含 _meta 保留）')
    ok('dx_cm 持久化与计数')
    ok('回归数学（过原点斜率 / 相关 / 边界）')
    ok('步骤表一致性')
    ok('路径解析（相对/根相对/带引号）')
    say('')
    ok('全部通过')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\n已中断')
        raise SystemExit(130)
