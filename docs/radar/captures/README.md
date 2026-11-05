# 采集与分析记录索引

> 本目录按「一次实验 = 一份记录」归档。
> 原始帧（`.bin` / `.npz`）与日志不入库，只保留结论与可复现的评估结果。

## 记录清单

| 日期 | 记录 | 实验内容 | 核心结论 |
|---|---|---|---|
| 2026-10-22 | [lr3-series](lr3-series-20261022.md) | 1.0 m 站位、1T2R、固定带三轮 | RIGHT 0/8；根因是固定带只覆盖一个距离 bin |
| 2026-10-29 20:08–22:07 | [vertical](vertical-20261029.md) | 2T4R 俯仰/高度 11 轮 | 8 轮里仅 1 轮 `ok`；跳与蹲俯仰响应同向，分不开 |
| 2026-10-29 22:43 | [rxpair](rxpair-20261029.md) | 4 种通道对诊断 | z0-z1 偏左、z0-z3 偏右，单对分不出左右 |
| 2026-10-29 22:40 | [horizontal](horizontal-20261029.md) | 水平方位角 2 轮 | 中心自身漂移 30°，左右重叠，方位角法不可用 |
| 2026-10-29 18:40/19:03 | [calibration-fisher](calibration-fisher-20261029.md) | Fisher 可分性 2 次 | 2T4R 通道 0-1 达 18.8，是 1T2R 的 4.2 倍；首轮作废 |
| 2026-10-29 18:04–18:16 | [dual-interference](dual-interference-20261029.md) | 双板共存 4 次 | 均未见显著周期性干扰；1T2R 帧率不受影响 |
| 2026-10-29 | [jumpduck-offline-eval](jumpduck-offline-eval.md) | 跳蹲离线可分性 | 58.8% 但 CI 下界 42.2% → 判「不可分」 |
| 2026-10-22 起 | [lane-eval-summary](lane-eval-summary.md) | 左右移判据结果集 | 93.3% 是离线可分性；实时滑窗最好 48.2%；ML 路线不可用 |

## 阅读顺序建议

1. 先看 [lane-eval-summary](lane-eval-summary.md)：理解「离线可分性 ≠ 实时准确率」，
   以及为什么最终选规则法。
2. 再看 [lr3-series](lr3-series-20261022.md) 与 [rxpair](rxpair-20261029.md)：
   理解左右不对称的物理根源。
3. 然后看 [vertical](vertical-20261029.md) 与 [jumpduck-offline-eval](jumpduck-offline-eval.md)：
   理解跳/蹲为什么仍停在验证门内。
4. 最后看 [horizontal](horizontal-20261029.md) 与 [dual-interference](dual-interference-20261029.md)：
   理解哪些路线被实测否定、哪些前提被实测支持。

## 配套评估结果

`outputs/radar-eval/` 下按实验分目录保存原始评估 JSON，与上面的记录一一对应：

```
outputs/radar-eval/
├── lane-eval/               # 左右移：基线、扫参、可分性、ML
├── vertical/                # 11 轮俯仰标定
├── rxpair/                  # 通道对诊断
├── horizontal/              # 方位角
├── horizontal_guide/        # 通道对法
├── calibration-analysis/    # Fisher 分析
├── dual-interference/       # 双板干扰
├── jumpduck-offline-eval/   # 跳蹲离线评估
├── lr3b-compare-all.txt
└── lr3c-compare-all.txt
```

## 证据纪律

- 每份记录都标注了样本量、采集条件与判定阈值来源；
- 负面结论（不可用、不可分、unsupported）与正面结论同样保留，不做选择性收录；
- 计划中的检查与已执行的证据分开写，阈值类提案标注「待小组确认」。
