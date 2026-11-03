# 05 · 验收与证据

本目录只放**能被截图直接证明**的东西。文字结论在 [`../03-雷达评估与校准/`](../03-雷达评估与校准/)。

| 图片 | 证明什么 |
|---|---|
| `images/01-menu.png` | 主菜单可用，输入源入口可见 |
| `images/02-play-settlement.png` | 结算页显示本局分数、最高分、纪录 |
| `images/03-action-jump.png` | 跳跃动作与角色状态正确 |
| `images/04-action-slide.png` | 滑铲动作正确，碰撞判定生效 |
| `images/05-radar-replay-run.png` | 雷达回放数据驱动真游戏跑起来 |
| `images/06-radar-replay-evidence.png` | 回放过程中判定生效的现场证据 |
| `images/07-calibration-wizard.png` | 开局校准向导 v2 的实际界面 |
| `images/08-jump-truth.png` | 跳跃真值通道与判定结果的对照 |

## 这些截图不能证明什么

必须说清楚，避免过度解读：

- **不能证明跳/蹲在真机上稳定可用**。截图来自回放数据或模拟源，
  真机结论要看 `radar-dataset-evaluation-2.md` 的误报率与帧率。
- **不能证明所有站位都准**。校准是"每人每次现场做一次"，换站位要重来。
- **不能代替验收用例**。逐条 PASS/FAIL 在仓库 `docs/acceptance.md`。

## 复现截图

```bash
# 真跑一局并在横幅可见瞬间截图
python ../04-工具与脚本/scripts/cdp_shot.py
```

截图输出到脚本内配置的目录，需要先把 `<PROJECT_ROOT>`、`<TEMP>` 换成你的实际路径。
