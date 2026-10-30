# 团队分工与贡献记录

> 项目：熊二快跑 · Bear Run（体感跑酷 MVP）
> 仓库：https://github.com/gf199lin/bear-run
> 版本：v1.0.0-mvp
> 更新：2026-10-30

## 一、分工原则

- 团队 4 人，**每人 25%，合计 100%**。
- 按模块边界划分，每人都有独立、可验收的交付物，避免职责重叠。
- 贡献以 GitHub 为准：本人 commit、Issue 评论、PR 描述中的 `@mention` 三者都算记录。
- 任何一人缺席，其模块的验收物就无法交付，因此 25% 是责任比例，不是挂名比例。

## 二、分工表

| 成员 | 占比 | 负责模块 | 具体职责 | 主要交付物 |
|---|---|---|---|---|
| [@gf199lin](https://github.com/gf199lin) | 25% | 游戏引擎与集成发布 | 主循环、跑道生成、碰撞与结算；雷达判定接入；仓库管理、PR 合并、Release | 可运行游戏、`v1.0.0-mvp`、Release 页 |
| [@hwx-666](https://github.com/hwx-666) | 25% | 账号体系与界面 | 登录/注册/游客模式；最高分与纪录按账号隔离；主菜单、HUD、结算页样式 | 登录页、游客模式、UI 截图 |
| [@jqz070803](https://github.com/jqz070803) | 25% | 雷达数据与校准 | 原始帧解析验证；左/右/跳/蹲校准流程；采集数据质量评估与阈值标定 | 校准向导、`docs/calibration-record.md`、数据评估结论 |
| [@chawaicha](https://github.com/chawaicha) | 25% | 测试、文档与演示 | 验收用例与真机联调记录；缺陷登记；README/PRD 维护；演示截图与视频 | `docs/acceptance.md`、验收结果、2 分钟演示 |

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

### @jqz070803 — 雷达数据与校准（25%）

- [ ] 校准向导 5 步（站中 → 左 → 右 → 跳 → 蹲）可完整走通
- [ ] 校准后左/右换道命中率有实测记录（≥ 80%）
- [ ] 跳/蹲标注为实验功能，给出当前误判率与原因
- [ ] `docs/calibration-record.md` 记录串口、中心位置、身高代理、阈值、复测结果
- [ ] 不提交原始回波抓包与个人身份信息

### @chawaicha — 测试、文档与演示（25%）

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
| @jqz070803 | 0 | **待补**：今日需本人提交校准记录或数据评估 |
| @chawaicha | 0 | **待补**：今日需本人提交验收用例或演示材料 |

> 说明：commit 数不等于贡献质量。`hwx-666` 的 152 次中有大量逐文件“删除冗余/上传整合”提交，
> 实际模块贡献以第二节职责与第三节验收物为准。`jqz070803`、`chawaicha` 目前无提交记录，
> 必须由本人补提交后，25% 才有 GitHub 证据。

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
| @jqz070803 | 新建 `docs/calibration-record.md`，记录校准参数与复测结果 | `git add docs/calibration-record.md && git commit -m "docs(radar): add calibration record and retest result"` |
| @chawaicha | 新建 `docs/acceptance.md`，登记 8 条验收用例 | `git add docs/acceptance.md && git commit -m "docs(test): add MVP acceptance cases and results"` |

## 六、Issue 与 PR 对应关系

| Issue | 负责人 | 关联 PR |
|---|---|---|
| `[MVP] 游戏本体与玩法闭环验收` | @gf199lin | 直接提交 / Release |
| `[前端] 登录/游客模式与本地存档隔离` | @hwx-666 | `feat/auth-guest-mode` |
| `[雷达] 2T4R 串口接入与左右换道` | @jqz070803 | `feat/radar-calibration` |
| `[雷达] 跳/蹲检测（实验性，未真机验收）` | @jqz070803 | `feat/radar-calibration` |
| `[交付] MVP 提交检查：.gitignore、保密、Release` | @chawaicha | `docs/mvp-acceptance` |

> PR 合并后由 @gf199lin 统一打 tag，确保 Release 与 `main` 一致。
