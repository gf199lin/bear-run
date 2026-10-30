# -*- coding: utf-8 -*-
"""环境底噪校准（背景基线）：空场采集 → 逐距离门占用率 B_j → 落盘 / 读取。

===============================================================================
⚠ 命名区分：「校准」这个词在本项目里已经被占用过一次，不要混用
===============================================================================
- `radar_calibrate.py` 的 **F3「校准」**：
  玩家站正前方 3 s，定 LEFT/RIGHT 灵敏度与推荐距离带 —— 那是「**在场校准**」。
- **本模块 =「环境底噪校准 / 背景基线」**：
  人**离开波束**后采一段（默认 186 帧 ≈ 24 s），拿到每个距离门的**固有占用率** `B_j`。
  用途是「换场地/换站位时，先把环境自身的杂波摸清楚」。

两者数据来源相反（人在场 / 人不在场），结论也不可互换。UI 与文档文案不得混用。

===============================================================================
口径（必须与 `radar_calibrate.mode_band` 一致，否则与已验证的离线结论不可比）
===============================================================================
- 距离门      `j = int(round(r / pitch - 0.5))`   （同 `radar_calibrate.bin_index`）
- 占用率      `occ[j] = 「该门至少出现 1 个点的帧数」/「计入的总帧数」`
- 基线        `B_j ≡ occ[j]`（不做平滑、不设阈值、不掺任何先验）

本模块**不构造点云处理器**：调用方（server.py）负责给「未截断口径」的点云，
即 `min_range = 0`。理由见 `radar_calibrate.make_processor` L186-197：
一旦用被近距屏蔽过的点云统计，j=0（近场泄漏门）会从证据里消失，
形成「屏蔽项把自己删掉」的自我掩盖 —— 本项目已踩过这个坑。

===============================================================================
诚实边界（依据 `docs/bg-baseline-verification.md` 的实测）
===============================================================================
1. **采集期间必须空场。** 基线若被污染（人在波束内），后续减底噪会**变差**，
   且**不会自动报警**。负对照：把「人站 1.00 m」的录音冒充空场 → 命中率 8/10 → 7/10。
2. **`B_j` 会漂移。** 实测同一会话 4 min 内逐门极差可达 **0.124**（j=4、j=7 近三倍）。
   故本模块落盘 `created_at` 供事后判断「是否过期」，**不承诺长期有效**。
3. **本模块只做「采 / 算 / 存 / 读」。** 是否把 `B_j` 接进自动选带
   （`radar_actions._auto_band_step`）是**另一个独立决策，目前未定**；
   本模块**默认不介入**选门，也不改变任何现有识别行为。
4. **`D` 只作诊断数字，不设红/黄/绿语义线**（用户裁定 2026-10-20，
   `docs/ai-decision-log.md` #38）。理由：纯噪声本底最大 **14.6%** 已**超过**
   真·0.15 m 场景变化的中位 **11%**，两分布严重重叠（信噪比≈1.4:1），
   任何语义阈值都会被噪声跨过 —— 设红黄绿只会制造假警报。
"""

import json
import os
import threading
import time

import radar_source as rs

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, 'bg_baseline.json')

SCHEMA = 'bg-baseline/1'

# 丢弃开头的若干帧：管道刚起时可能有瞬态（离线站位数据的窗口 f0≈47 亦印证存在启动垃圾）
HEAD_FRAMES = 8

# 默认目标帧数。取 186 是为了与离线验证用过的空场窗口（empty_a/empty_b 各 186 帧）
# **逐帧同量级、可直接对比**，而不是随手取整。提案，待小组确认。
DEFAULT_FRAMES = 186

# 近场泄漏门（1T2R 实测 0.70 m）。与 radar_calibrate.set_board 同判据。
LEAK_RANGE_1T2R = 0.70


def leak_range_for(board):
    """近场泄漏门的上界（米）。2T4R 门距仅 0.150 m，沿用 0.70 m 会连删 j=0..4，
    故只排 j=0 —— 与 `radar_calibrate.set_board` 同一取舍（那里也标了「提案，待确认」）。
    """
    return LEAK_RANGE_1T2R if board == '1t2r' else rs.range_res(board)


def bin_of(r, pitch):
    """距离 → 距离门索引。与 `radar_calibrate.bin_index` 逐字同式。"""
    return int(round(float(r) / float(pitch) - 0.5))


def bin_center(j, pitch):
    """门中心距离（米）：r_j = (j + 0.5) × pitch。"""
    return (j + 0.5) * float(pitch)


def est_seconds(board, frames):
    """按板型帧周期估算采集这些帧需要多少秒（只用于提示，不参与判定）。"""
    return max(0.0, float(frames)) * rs.frame_period(board)


def occ_from_point_frames(frames, pitch):
    """逐门占用率。`frames` 是「每帧一个点列表」的可迭代。

    与 `radar_calibrate.mode_band` 的 occ 定义完全一致（同样的门索引、同样的
    「至少 1 点即算该帧占用」口径）。独立实现是为了避免 import `radar_calibrate`
    带来的模块级 `set_board()` 全局副作用；一致性由 `_selftest_occ_parity()` 钉住。
    """
    seen = {}
    n = 0
    for pts in frames:
        n += 1
        js = set()
        for p in pts:
            js.add(bin_of(p['r'], pitch))
        for j in js:
            seen[j] = seen.get(j, 0) + 1
    if n == 0:
        return {}, 0
    return {j: seen[j] / float(n) for j in seen}, n


def l1_half(p, q):
    """D = 0.5 · Σ|p−q|（逐门占用率的总变差距离），取值 0~1。

    这是「校准需求百分比」的底层度量。**只作诊断数字**：噪声本底可达 14.6%，
    足以跨过任何想当然的阈值，故不设语义线（见模块头 #4）。
    """
    n = max(len(p), len(q))
    s = 0.0
    for i in range(n):
        a = p[i] if i < len(p) else 0.0
        b = q[i] if i < len(q) else 0.0
        s += abs(float(a) - float(b))
    return 0.5 * s


def make_record(board, occ, n_frames, pitch=None, fps=None,
                dropped=0, source='live', note=None):
    """把逐门占用率整理成可落盘的基线记录。`b_j` 按门索引补零成定长数组。"""
    pitch = float(pitch if pitch is not None else rs.range_res(board))
    bins = int(rs.profile_for(board)['bins'])
    b_j = [round(float(occ.get(j, 0.0)), 4) for j in range(bins)]
    return {
        'schema': SCHEMA,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'board': board,
        'bins': bins,
        'bin_pitch_m': round(pitch, 4),
        'leak_range_m': round(leak_range_for(board), 4),
        'frames': int(n_frames),
        'dropped_head_frames': int(dropped),
        'fps': (round(float(fps), 2) if fps else None),
        'occ': {str(j): round(float(occ[j]), 4) for j in sorted(occ)},
        'b_j': b_j,
        'source': source,
        'note': note or '环境底噪基线（空场实测）。只用于诊断/减底噪参考，不设红/黄/绿语义线。',
    }


def save(record, path=DEFAULT_PATH):
    """原子写：先写临时文件再替换，避免断电/中断留下半截 JSON。"""
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def load(path=DEFAULT_PATH):
    """读基线；不存在/损坏/版本不符一律返回 None（不猜、不抛）。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get('schema') != SCHEMA:
        return None
    if not isinstance(rec.get('b_j'), list) or not rec['b_j']:
        return None
    return rec


def diag_lines(rec, prev=None):
    """机械印出「本底行」+ 可选的 D 诊断行。UI 直接照抄，不做任何颜色语义。"""
    pitch = float(rec.get('bin_pitch_m') or 0.6813)
    parts = []
    for j, v in enumerate(rec.get('b_j') or []):
        parts.append('j%d %.2fm %d%%' % (j, bin_center(j, pitch), round(100 * float(v))))
    out = ['本底行 ' + ' | '.join(parts)]
    if prev and prev.get('b_j'):
        d = l1_half(rec['b_j'], prev['b_j'])
        out.append('D = %.1f%%（与上次基线的逐门占用率差异；诊断数字，不设语义线）'
                   % (100.0 * d))
        if prev.get('created_at'):
            out.append('上次基线：%s' % prev['created_at'])
    else:
        out.append('D = —（首次校准，无参考基线）')
    return out


class BgCollector:
    """在线收集空场帧 → 逐门占用率。线程安全。

    `add()` 由采集线程每帧调用一次；`state` 由 HTTP 线程读取。
    收满 `want_frames`（计入的帧数）后自动 `finish()` 并在采集线程内落盘；
    落盘失败会记为 `phase='failed'` 并把原因回传，**不静默吞掉**。
    """

    def __init__(self, board, want_frames=DEFAULT_FRAMES, head_frames=HEAD_FRAMES,
                 path=DEFAULT_PATH, log=None):
        self.board = board
        self.pitch = rs.range_res(board)
        self.bins = int(rs.profile_for(board)['bins'])
        self.want = max(1, int(want_frames))
        self.head = max(0, int(head_frames))
        self.path = path
        self.log = log

        self._lock = threading.Lock()
        self._frames = []          # 已计入的帧（点列表）
        self._dropped = 0          # 头部丢弃计数
        self._n_raw = 0            # 收到的总帧数
        self._t0 = None
        self._t_last = None
        self._phase = 'collecting'
        self._msg = None
        self._result = None
        self._prev = load(path)    # 校准前先读旧基线，供 D 对比

    # ---------------------------------------------------------------- 写入侧
    def add(self, pts):
        """采集线程每帧调用。达到目标帧数时自动收尾。任何异常都转成 phase='failed'。"""
        try:
            with self._lock:
                if self._phase != 'collecting':
                    return
                now = time.time()
                if self._t0 is None:
                    self._t0 = now
                self._t_last = now
                self._n_raw += 1
                if self._n_raw <= self.head:
                    self._dropped += 1
                    return
                self._frames.append(pts)
                done = len(self._frames) >= self.want
            if done:
                self.finish()
        except Exception as e:                      # 采集线程不能因校准而中断
            with self._lock:
                self._phase = 'failed'
                self._msg = '%s: %s' % (type(e).__name__, e)

    # ---------------------------------------------------------------- 收尾
    def finish(self):
        """计算 B_j、落盘、生成诊断行。可被手动提前调用（提前结束也有效）。"""
        with self._lock:
            if self._phase not in ('collecting',):
                return self._result
            frames = list(self._frames)
            dropped = self._dropped
            n_raw = self._n_raw
            t0, t_last = self._t0, self._t_last
            prev = self._prev
            self._phase = 'finishing'

        try:
            occ, n = occ_from_point_frames(frames, self.pitch)
            if n <= 0:
                raise ValueError('未收到任何有效帧（want=%d），请确认采集正在运行'
                                 % self.want)
            fps = (n / (t_last - t0)) if (t0 and t_last and t_last > t0) else None
            rec = make_record(self.board, occ, n, pitch=self.pitch, fps=fps,
                              dropped=dropped, source='live')
            rec['raw_frames'] = n_raw
            rec['hit_target'] = (n >= self.want)
            rec['diag'] = diag_lines(rec, prev)
            path = save(rec, self.path)
            rec['saved_to'] = path
            if self.log:
                self.log('[bgcalib] 完成：%d 帧（丢弃头部 %d / 共收 %d）→ %s'
                         % (n, dropped, n_raw, path))
                for ln in rec['diag']:
                    self.log('[bgcalib] ' + ln)
            with self._lock:
                self._phase = 'done'
                self._result = rec
            return rec
        except Exception as e:
            with self._lock:
                self._phase = 'failed'
                self._msg = '%s: %s' % (type(e).__name__, e)
            return None

    # ---------------------------------------------------------------- 读取侧
    @property
    def active(self):
        with self._lock:
            return self._phase == 'collecting'

    @property
    def state(self):
        """给 /status 与页面用的快照。字段命名与页面文案一一对应。"""
        with self._lock:
            n = len(self._frames)
            phase = self._phase
            msg = self._msg
            rec = self._result
            t0, t_last = self._t0, self._t_last
            dropped = self._dropped
        elapsed = (time.time() - t0) if t0 else 0.0
        fps = (n / elapsed) if (n > 0 and elapsed > 0.5) else None
        left = None
        if phase == 'collecting':
            if fps:
                left = max(0.0, (self.want - n) / fps)
            else:
                left = (self.want - n) * rs.frame_period(self.board)
        return {
            'phase': phase,
            'running': phase == 'collecting',
            'frames': n,
            'want': self.want,
            'dropped': dropped,
            'elapsed_s': round(elapsed, 1),
            'left_s': (round(left, 1) if left is not None else None),
            'fps': (round(fps, 2) if fps else None),
            'board': self.board,
            'path': self.path,
            'msg': msg,
            'result': ({
                'frames': rec.get('frames'),
                'b_j': rec.get('b_j'),
                'bin_pitch_m': rec.get('bin_pitch_m'),
                'created_at': rec.get('created_at'),
                'saved_to': rec.get('saved_to'),
                'diag': rec.get('diag'),
            } if rec else None),
        }


# ---------------------------------------------------------------------- 自检
def _selftest_occ_parity():
    """占用率口径一致性自检：本模块 vs `radar_calibrate.mode_band`。

    用合成点云（确定性的、含多门/漏门/空帧）对比两边的 occ。任何一侧口径漂移
    都会在这里被打出来 —— 这是「与离线验证结论可比」这条声明的最小防线。
    返回 (ok, msg)。
    """
    pitch = rs.range_res('1t2r')
    # 造 4 帧：j=0 每帧都有；j=2 只在第 1、3 帧；第 4 帧全空
    mk = lambda r: [{'r': r, 'v': 0.0, 'pa': 1.0}]
    frames = [
        mk(bin_center(0, pitch)) + mk(bin_center(2, pitch)),
        mk(bin_center(0, pitch)),
        mk(bin_center(0, pitch)) + mk(bin_center(2, pitch)),
        [],
    ]
    mine, n = occ_from_point_frames(frames, pitch)
    try:
        import radar_calibrate as rc
    except Exception as e:                                  # 依赖缺失不算失败
        return None, '跳过（无法 import radar_calibrate: %s）' % e
    rc.set_board('1t2r')
    # mode_band 返回 dict：{'n_frames','bins','leak_bins','recommended_band'}
    res = rc.mode_band([(i + 1, (i + 1) * 129.5, f)
                        for i, f in enumerate(frames)],
                       (0.70, 1.35), log=lambda m: None)
    t_occ = {r['bin']: r['occ'] for r in res['bins']}
    diffs = []
    for j in sorted(set(list(mine.keys()) + list(t_occ.keys()))):
        a = round(float(mine.get(j, 0.0)), 3)
        b = round(float(t_occ.get(j, 0.0)), 3)
        if a != b:
            diffs.append('j%d 本模块=%.3f mode_band=%.3f' % (j, a, b))
    if diffs:
        return False, '占用率口径不一致：' + '；'.join(diffs)
    return True, '占用率口径一致（j=%s，n=%d 帧）' % (sorted(mine.keys()), n)


if __name__ == '__main__':
    ok, msg = _selftest_occ_parity()
    print('occ 口径自检：%s —— %s' % ('通过' if ok else ('跳过' if ok is None else '失败'), msg))
    raise SystemExit(0 if ok in (True, None) else 1)
