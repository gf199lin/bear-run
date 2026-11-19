# 08 · 云端账号与存档（Supabase 接入）

> 负责人：[@gf199lin](https://github.com/gf199lin)
> 时间：2026-11-19
> 对应提交：`198e32f`（功能）、`c8cc46c`（后台证据归档）
> 线上：https://gf199lin.github.io/bear-run/

---

## 1. 这一册解决什么问题

前七册里，账号和战绩都是**纯前端 localStorage 伪账号**：换设备就丢、清缓存就没了、
懂技术的人可以直接改本地数据，也无法证明"这一局真的是这个玩家打的"。

这一册把账号和战绩搬到 Supabase（Postgres + Auth），做到：

- 真实注册/登录，密码由服务端保管，前端拿不到哈希；
- 战绩进 `game_records` 表，跨设备、跨浏览器跟着账号走；
- 权限由数据库 RLS 兜底，前端只持 publishable key，泄露也写不了别人的数据。

**同时保住的老能力**：直接双击 `index.html`（`file://`）或未配置密钥时，
整个云端模块静默降级，仍然是原本地伪账号，单文件分发不受影响。

---

## 2. 架构：三层存储

`localStorage` 是同步的，`Supabase` 是异步的，而游戏的存档读取是同步调用。
所以没有把业务代码改成 async，而是在中间加了一层缓冲：

```
业务代码（同步读写，一行未改）
      ↓ RR.Auth.get / .set
内存缓存（Map，同步命中）
      ↓
localStorage（同步落盘，离线兜底）
      ↓ 异步、可失败、可排队重试
Supabase（真源，跨设备）
```

三个关键设计：

| 设计 | 原因 |
| --- | --- |
| 启动时同步解析会话 | 避免刷新后先闪一下登录页 |
| 写失败进 `outbox`，下次启动补传 | 现场断网不丢这局战绩（最多缓存 200 局） |
| `file://` / 无配置直接降级 | 保住"双击即玩"的单文件分发能力 |

用户名映射伪邮箱 `用户名@bearrun.local`（Supabase Auth 只认 email），
玩家看到的仍是用户名，源码里由 `SUPABASE_ACCOUNT_DOMAIN` 统一控制。

---

## 3. 交付物索引

| 文件 | 内容 |
| --- | --- |
| [`schema.sql`](schema.sql) | 三张表 + 触发器 + `player_stats`/`leaderboard` 视图 + RLS 策略 + 验证清单，可一键重建整个后台 |
| [`supabase-backend-plan.md`](supabase-backend-plan.md) | 架构方案、威胁模型、分阶段路线、为什么不用服务端 |
| [`supabase-setup-checklist.md`](supabase-setup-checklist.md) | 控制台逐步操作与排错对照（含"哪一步漏了会怎样"） |
| [`supabase-deployment-record.md`](supabase-deployment-record.md) | 2026-11-19 实际执行记录与端到端验证结果 |
| 前端实现 | [`js/cloud.js`](../../js/cloud.js)、[`js/auth.js`](../../js/auth.js)、[`js/Game.js`](../../js/Game.js) |
| 同步工具 | [`tools/sync-inline.js`](../../tools/sync-inline.js) |

---

## 4. 验证证据（全部为实测，非推测）

| 检查项 | 结果 |
| --- | --- |
| `GET /auth/v1/health` | `200`，`GoTrue v2.198.0` |
| 匿名读 `profiles` | `200 []`，表可读但为空（RLS 允许公开读榜单维度） |
| 注册 | 返回 `access_token` + `user`，无邮件确认环节 |
| 注册后 `profiles` / `player_stats` | 由触发器**自动生成**对应行 |
| 匿名伪造写入 `game_records` | `401` + `42501`，RLS 拒绝 |
| 前端 SDK（jsDelivr） | `200`，约 216 KB |
| 线上落地复查 | 页面含 `RR_CLOUD_CONFIG` / `RR.Cloud` / `cloudRegister` / `RR.Cloud.pushRecord` |

本地回归：内联 22 个脚本块语法全过；Node 模拟浏览器环境冒烟测试 17/17 通过；
`sync-inline.js --check` 幂等校验通过。

过程中冒烟测试抓出并修掉两个真实缺陷：

1. `_bootstrap` 调用了不存在的 `this._ensureClient`（应为 `ensureClient`），云端初始化直接抛错；
2. 游客态注册/登录后没清除游客标记，导致新账号的存档只进内存、刷新即丢。

---

## 5. 复现步骤

```bash
# 1. 在 Supabase 控制台新建项目，SQL Editor 里整段执行 schema.sql
# 2. Authentication → Sign In / Providers → Email → 关闭 Confirm email
# 3. Authentication → URL Configuration 填 Pages 地址与本地调试地址
# 4. 把项目 URL 与 publishable key 填进 index.html 的 RR_CLOUD_CONFIG
# 5. 改完 js/ 源文件后重新内联：
node tools/sync-inline.js          # 写入 index.html
node tools/sync-inline.js --check   # 校验是否一致
```

`service_role` key 与数据库密码不进入本仓库、前端和任何截图。

---

## 6. 已知限制（不粉饰）

- **旧本地账号不会自动迁移**：部署前浏览器里的伪账号没有 Supabase 会话，
  需在线上退出后用同一用户名重新注册，旧战绩不随之上云。
- **游客模式不写云端**：游客没有 `auth.uid()`，RLS 下无法写入，属预期行为。
- **测试期探针账号**需手动清理（记录见 `supabase-deployment-record.md` §5）。
- 密码找回依赖 Supabase 邮件服务，当前未接入自定义 SMTP，演示期按"忘记就重注册"处理。
