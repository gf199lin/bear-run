# 团队分工与贡献记录

> 项目：熊二快跑 · Bear Run（体感跑酷 MVP）
> 仓库：https://github.com/gf199lin/bear-run
> 版本：v1.0.0-mvp
> 更新：2026-10-30

## 一、分工原则

- 团队 4 人，**每人 25%，合计 100%**。
- 按模块边界划分，每人都有明确、可验收的交付边界；雷达组两人按采集/校准链路分工，不重复计功。
- 贡献以 GitHub 为准：本人 commit、Issue 评论、PR 描述中的 `@mention` 三者都算记录。
- 任何一人缺席，其模块的验收物就无法交付，因此 25% 是责任比例，不是挂名比例。

## 二、分工表

| 成员 | 占比 | 负责模块 | 具体职责 | 主要交付物 |
|---|---|---|---|---|
| [@gf199lin](https://github.com/gf199lin) | 25% | 游戏引擎与集成发布 | 主循环、跑道生成、碰撞与结算；雷达判定接入；仓库管理、PR 合并、Release | 可运行游戏、`v1.0.0-mvp`、Release 页 |
| [@hwx-666](https://github.com/hwx-666) | 25% | 账号体系与界面 | 登录/注册/游客模式；最高分与纪录按账号隔离；主菜单、HUD、结算页样式 | 登录页、游客模式、UI 截图 |
| [@jqz070803](https://github.com/jqz070803) | 25% | 雷达数据采集与帧解析 | 采集环境与串口配置；原始帧解包/拼帧验证；左右与跳蹲动作标注；采集批次记录 | `docs/capture-log.md`、采集数据集说明 |
| [@chawaicha](https://github.com/chawaicha) | 25% | 雷达校准与数据评估 | 校准向导 5 步；中心/身高代理标定；左右与跳蹲阈值；复测命中率与数据质量评估 | `docs/calibration-record.md`、数据评估结论 |

> **雷达组**：`@jqz070803` 与 `@chawaicha` 共同承担“雷达数据采集与校准”（合计 50%），
> 前者负责采集链路，后者负责校准与评估链路。
> **测试、文档与演示**由全组共同完成，`@gf199lin` 统筹。

## 三、各模块验收口径

### @gf199lin — 游戏引擎与集成（25%）

- [ ] `index.html` 双击可进入主菜单，无控制台报错
- [ ] 键盘 `←` `→` `↑` `↓` 分别触发换道、跳跃、滑铲
- [ ] 撞障碍后结算页显示本局分数、最高分、纪录
- [ ] 雷达 WebSocket 断线后 1.2s 内给出提示并可重连
- [ ] `v1.0.0-mvp` tag 与 Release 指向同一提交

### @hwx-666 — 账号体系与界面（25%）

- [ ] 首次进入可注册、登录、选择游客模式
- [ ] 游客模式不落盘存档，退出后不污染账号数据
- [ ] 两个账号的最高分与纪录互相隔离
- [ ] 主菜单、HUD、结算页在 1366×768 与手机宽度下不重叠

### @jqz070803 — 雷达数据采集与帧解析（25%）

- [ ] 记录采集环境：板子数量、串口、固件/配置、采集日期
- [ ] 原始帧可稳定解包、拼帧，丢帧率有记录
- [ ] 左/右/跳/蹲四类动作各完成不少于 20 次标注
- [ ] `docs/capture-log.md` 记录采集批次、数据规模、异常与处理
- [ ] 不提交原始回波抓包与个人身份信息

### @chawaicha — 雷达校准与数据评估（25%）

- [ ] 校准向导 5 步（站中 → 左 → 右 → 跳 → 蹲）可完整走通
- [ ] 校准后左/右换道命中率有实测记录（≥ 80%）
- [ ] 跳/蹲标注为实验功能，给出当前误判率与原因
- [ ] `docs/calibration-record.md` 记录中心位置、身高代理、阈值、复测结果
- [ ] 对采集数据给出可用性结论（哪些批次可用于调参、哪些需重采）

### 全组共同 — 测试、文档与演示（@gf199lin 统筹）

- [ ] `docs/acceptance.md` 含不少于 8 条验收用例及通过/失败结论
- [ ] 每个失败项登记为 Issue，注明复现步骤与负责人
- [ ] README 的“3 分钟验收路径”与实际操作一致
- [ ] 提供 2 分钟演示视频或 GIF，覆盖键盘、体感、雷达三种输入
- [ ] Release 说明中的数据授权段落与 `docs/README.md` 一致

## 四、贡献记录（截至 2026-10-30）

| 成员 | 仓库 commit 数 | 说明 |
|---|---|---|
| @gf199lin | 28 | 引擎、雷达接入、整合与发布 |
| @hwx-666 | 152 | 登录/游客模式、UI；含早期逐文件整理提交 |
| @jqz070803 | 0 | **待补**：雷达组，采集与帧解析；今日需本人提交采集日志 |
| @chawaicha | 0 | **待补**：雷达组，校准与数据评估；今日需本人提交校准记录 |

> 说明：commit 数不等于贡献质量。`hwx-666` 的 152 次中有大量逐文件“删除冗余/上传整合”提交，
> 实际模块贡献以第二节职责与第三节验收物为准。`jqz070803`、`chawaicha` 同属雷达组，
> 共同完成采集与校准，但目前无提交记录，必须由本人补提交后，这 50% 才有 GitHub 证据。

## 五、今日补齐要求（每人 20 分钟内可完成）

每位成员至少完成：

1. **1 个本人 commit**：只改自己负责的文件，commit message 用 `type(scope): 描述`。
2. **1 条 Issue 评论**：在自己负责的 Issue 下写“负责模块 + 今日完成 + 待办”。
3. **1 次 PR 参与**：在 PR 描述或 review 中 `@` 至少一名组员。

### 最小提交建议

| 成员 | 今日最小提交 | 命令 |
|---|---|---|
| @gf199lin | 合并分工文档、发 Release | `git add docs/team-contribution.md README.md && git commit -m "docs: add 4-person ownership (25% each)"` |
| @hwx-666 | 补一张登录/游客模式截图到 `docs/images/` | `git add docs/images/05-login.png && git commit -m "docs(ui): add login and guest mode screenshot"` |
| @jqz070803 | 新建 `docs/capture-log.md`，记录采集环境、批次、帧解析校验与动作标注 | `git add docs/capture-log.md && git commit -m "docs(radar): add capture log and frame parsing checks"` |
| @chawaicha | 新建 `docs/calibration-record.md`，记录校准参数、阈值与复测命中率 | `git add docs/calibration-record.md && git commit -m "docs(radar): add calibration record and data evaluation"` |

## 六、Issue 与 PR 对应关系

| Issue | 负责人 | 关联 PR |
|---|---|---|
| `[MVP] 游戏本体与玩法闭环验收` | @gf199lin | 直接提交 / Release |
| `[前端] 登录/游客模式与本地存档隔离` | @hwx-666 | `feat/auth-guest-mode` |
| `[雷达] 2T4R 串口接入与左右换道` | @jqz070803 | `docs/capture-log` |
| `[雷达] 数据采集与帧解析` | @jqz070803 | `docs/capture-log` |
| `[雷达] 跳/蹲检测（实验性，未真机验收）` | @chawaicha | `docs/calibration-record` |
| `[交付] MVP 验收、文档与 Release` | @gf199lin（全组共同） | `docs/mvp-acceptance` |

> PR 合并后由 @gf199lin 统一打 tag，确保 Release 与 `main` 一致。
