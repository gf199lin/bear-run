# -*- coding: utf-8 -*-
"""修 Calibration.js 的极性假设缺陷：第 2/3 步不能按 x 的符号判定左右。"""
import re
from pathlib import Path

p = Path(r"<PROJECT_ROOT>\radar-runner\js\Calibration.js")
src = p.read_text(encoding="utf-8")
orig_len = len(src)
applied = []


def rep(old, new, tag, count=1):
    global src
    n = src.count(old)
    assert n == count, "[%s] 期望 %d 处，实际 %d 处" % (tag, count, n)
    src = src.replace(old, new, count)
    applied.append("%s x%d" % (tag, count))


# ---- 1) open() 初始化：sideA/sideB + 上次校准的极性 ----
rep(
    "      this.x0 = this.xLeft = this.xRight = null;",
    "      this.x0 = null;\n"
    "      this.sideA = null;          // 第 2 步测到的原始 x（玩家在左侧）\n"
    "      this.sideB = null;          // 第 3 步测到的原始 x（玩家在右侧）\n"
    "      this.sideKeyboard = false;  // 左右步是否被键盘兜底走过（则极性无法标定）\n"
    "      var prior = Calibration.load();\n"
    "      this.priorSign = prior ? prior.sign : null;",
    "open-init",
)

# ---- 2) 分发：which 替代 dir ----
rep("        case 'left': this._stepSide(frame, -1); break;",
    "        case 'left': this._stepSide(frame, 0); break;", "dispatch-left")
rep("        case 'right': this._stepSide(frame, +1); break;",
    "        case 'right': this._stepSide(frame, 1); break;", "dispatch-right")

# ---- 3) _stepSide 整体重写（极性无关） ----
new_side = '''    /**
     * 第 2/3 步：等一个相对基线够大的位移，并保持 holdFrames 帧。
     *
     * 关键：**不假设雷达极性**。数据结论（docs/radar-calibration-plan.md）里
     * 右移 15 次有 12 次的 px_delta 是负的 —— "往右走 x 一定变大"这个前提本身
     * 就是错的，所以这里只要求：
     *   which=0（第 2 步）：离基线足够远，落在哪一侧都算 -> sideA
     *   which=1（第 3 步）：离基线足够远，且落在与 sideA 相反的一侧 -> sideB
     *   sign = (sideB - sideA) > 0 ? +1 : -1
     * 这样无论雷达装正还是装反，校准后"玩家往右走 => x' 变大"都成立。
     */
    _stepSide(frame, which) {
      var label = which === 0 ? '左' : '右';
      var keyed = which === 0 ? this.keys.left : this.keys.right;

      // 键盘兜底：键盘无法告诉我们雷达极性（退回上次校准的极性）
      if (keyed) {
        this.usedKeyboard = true;
        this.sideKeyboard = true;
        this.message = '用键盘走了这步 —— 雷达极性没法标定了。';
        this.messageKind = 'warn';
        this._nextStep();
        return;
      }

      var x = this._validX(frame);
      if (x === null) {
        this.message = this.radarOffline
          ? '雷达没数据（按 ' + (which === 0 ? '←' : '→') + ' 可用键盘代替）'
          : '没看到你，站回雷达前面。';
        this.messageKind = this.radarOffline ? 'warn' : '';
        return;
      }

      var dx = x - this.x0;
      var far = Math.abs(dx) >= this.opts.moveDelta;
      var sameSide = (which === 1 && this.sideA !== null) &&
        ((dx > 0) === ((this.sideA - this.x0) > 0));
      var ok = far && !sameSide;

      if (ok) {
        this.holdBuf.push(x);
        this.message = '检测到' + label + '移 ' + x.toFixed(2) +
          '（保持 ' + this.holdBuf.length + '/' + this.opts.holdFrames + '）';
        this.messageKind = 'ok';
        if (this.holdBuf.length >= this.opts.holdFrames) {
          var val = median(this.holdBuf);
          if (which === 0) this.sideA = val; else this.sideB = val;
          this.holdBuf = [];
          this._nextStep();
        }
        return;
      }

      this.holdBuf = [];
      if (sameSide) {
        this.message = '你现在还在同一侧，走到另一边去。';
      } else {
        this.message = '当前 x = ' + x.toFixed(2) + '，要离基线 ' +
          this.x0.toFixed(2) + ' 再远 ' + this.opts.moveDelta.toFixed(2) + '（幅度大一点）';
      }
      this.messageKind = '';
    }'''

pat = re.compile(r"    /\*\*\n     \* 第 2/3 步：等一个相对基线够大的位移.*?void mean;\n    \}", re.S)
assert len(pat.findall(src)) == 1, "找到 _stepSide 的次数不对"
src = pat.sub(lambda m: new_side, src, count=1)
applied.append("_stepSide-rewrite x1")

# ---- 4) 全局改名 xLeft/xRight -> sideA/sideB ----
n1 = src.count("this.xLeft")
n2 = src.count("this.xRight")
assert n1 >= 1 and n2 >= 1, "没找到 xLeft/xRight"
src = src.replace("this.xLeft", "this.sideA").replace("this.xRight", "this.sideB")
applied.append("rename xLeft/xRight -> sideA/sideB (%d+%d)" % (n1, n2))

# ---- 5) debug 键名 & 退化到上次极性 ----
rep("xLeft: this.sideA, xRight: this.sideB, spread: spread",
    "left: this.sideA, right: this.sideB, spread: spread", "debug-keys")

rep("""        if (this.sideA === null || this.sideB === null) {
          unreliable = true;
          sign = 1;
        }""",
    """        if (this.sideA === null || this.sideB === null) {
          // 没测到两侧：退回上次校准的极性（若有），否则只能用默认正向
          unreliable = true;
          sign = (this.priorSign === -1) ? -1 : 1;
        }""", "finish-fallback")

# ---- 6) 键盘走过左右步 => 结果不可靠 ----
rep("""        if (!unreliable) Calibration.save(this.result);""",
    """        if (this.sideKeyboard) unreliable = true;
        this.result.unreliable = unreliable;
        if (!unreliable) Calibration.save(this.result);""", "sideKeyboard")

# ---- 7) _stepReady：数据中断时清采样缓冲 ----
rep("""      if (x === null) {
        this.message = this.radarOffline
          ? '没收到雷达数据…可以按 ← → 空格 ↓ 用键盘完成。'
          : '暂时没看到你，站到雷达正前方试试。';
        this.messageKind = 'warn';
        return;
      }
      this.baseBuf.push(x);""",
    """      if (x === null) {
        if (this.now - (this._lastValidAt || 0) > 1200) this.baseBuf = [];
        this.message = this.radarOffline
          ? '没收到雷达数据…可以按 ← → 空格 ↓ 用键盘完成。'
          : '暂时没看到你，站到雷达正前方试试。';
        this.messageKind = 'warn';
        return;
      }
      this._lastValidAt = this.now;
      this.baseBuf.push(x);""", "ready-stale")

p.write_text(src, encoding="utf-8", newline="\n")
print("已应用补丁：")
for a in applied:
    print("  -", a)
print("字节数 %d -> %d" % (orig_len, len(src)))
