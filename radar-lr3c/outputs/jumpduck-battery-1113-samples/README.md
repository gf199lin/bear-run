# 跳蹲电池数据样本集（2026-11-13）

> 用途：给验收和复现提供**最小但有代表性**的原始雷达数据，不需要 46 MB 全量采集。
> 全量原始数据在本地 `data/jumpduck_battery/`，按 `.gitignore` 不入库。

## 一、为什么抽样

2026-11-13 共采集 6 个 session，其中 4 个有效（96 个跳/蹲样本）、2 个作废（雷达歪斜/掉线）。
全量约 46 MB。入库只需覆盖**每条结论的边界**：

| 结论 | 需要的样本 |
|---|---|
| 静息与动作可分（`nz_p90` 0.037 vs 0.52–0.94） | `rest0` |
| 中跳 / 中蹲可判 | `jump_m`、`duck_m` |
| 轻跳、深蹲 24/24（最好的一档） | `jump_k`、`duck_l` |
| 大跳蓄力 = 蹲（物理不可分） | `jump_l` |
| 浅蹲负段仅 2 帧（失败边界） | `duck_s` |
| 负样本误报（弯腰/转身/踏步） | `bend`、`turn`、`walk` |

## 二、目录与来源

每个 session 保留 `meta.json`（采集参数）、`labels.json`（真值标注）、`metrics.csv`（逐帧指标），
`blocks/` 下只放抽样块。

### `20261113-133307_S01`（37 块，全类别）
band `[1,4)`，7.73 fps，127.7 s。

| 块 | 真值 | 在结论中的角色 |
|---|---|---|
| `001_rest0.npz` | 静息 | 负样本；`nz_p90 = 0.037` |
| `002_jump_m.npz` | 中跳 | 跳 1–2 帧负段 |
| `006_duck_m.npz` | 中蹲 | 蹲 3–5 帧负段 |
| `010_walk.npz` | 踏步 | 负样本，谱宽落在切向/径向之间 |
| `020_jump_l.npz` | 大幅跳 | 蓄力下蹲达 3 帧，被判 DUCK |
| `022_duck_s.npz` | 浅蹲 | 负段仅 2 帧，原规则判 JUMP |
| `026_bend.npz` | 弯腰 | 负样本，径向速度形态与蹲同构 |
| `029_turn.npz` | 转身 | 负样本，方位角无差异 |

### `20261113-140614_S01`（33 块，边界标定）
band `[1,3)`，range 1.70 m；刻意拆成不预蹲/自然蓄力/中蹲/浅蹲四组。

| 块 | 真值 | 角色 |
|---|---|---|
| `002_jump_m.npz` | 中跳（明确不预蹲） | `nb = 1–2` |
| `018_jump_l.npz` | 自然大跳（会蓄力） | `nb = 2–3`，与浅蹲重叠 |
| `026_duck_s.npz` | 浅蹲慢蹲 | EMA 前 `nb = 0`，EMA 后 3–4 |

### `20261113-142228_S01`（25 块，轻跳 + 深蹲专项）
| 块 | 真值 | 角色 |
|---|---|---|
| `002_jump_k.npz` | 轻跳 | 专项 12/12 之一 |
| `014_duck_l.npz` | 深蹲 | 专项 12/12 之一 |

## 三、怎么用

```bat
python tools\eval_battery.py outputs\jumpduck-battery-1113-samples\20261113-133307_S01 --csv outputs\jumpduck-battery-1113-samples\blockfeat.csv
python tools\eval_vr_rule.py outputs\jumpduck-battery-1113-samples\20261113-133307_S01
```

逐块全量特征仍在 `outputs/jumpduck-battery-1113-blockfeat.csv`（37 块）与
`outputs/jumpduck-battery-1113-rule-eval.csv`（逐块判定）。

## 四、边界

- 数据为不含图像的人体雷达回波，仅课程内部使用，对外分享前按课程保密要求确认。
- 抽样不等于全量：本文所列准确率仍以 `docs/jumpduck-decision-spec.md` 的四批 96 样本为准。
- 所有阈值均为**提案，待小组确认**。
