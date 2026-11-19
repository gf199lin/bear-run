# Supabase 接入：你需要做的事（按顺序）

> 代码已经改完并通过本地冒烟测试（17 项全过）。
> 剩下的都是**只有你能做**的账号侧操作，大约 20 分钟。

---

## 第 0 步：先确认现在的状态

```powershell
# 在项目根目录跑
node radar-runner/tools/sync-inline.js --check
node .deepworks/tmp/smoke.js
```

期望输出：`OK: index.html is in sync.` 和 `failed=0`。

没配置 Supabase 之前，游戏**照常能玩**，只是仍然存本地。改完代码不会让你现在的页面变白。

---

## 第 1 步：建 Supabase 项目（约 3 分钟）

1. 打开 <https://supabase.com/dashboard>，用 GitHub 账号登录。
2. **New project**：
   - Name：`bear-run`
   - Database Password：随便生成一个，**存到密码管理器**（这个不是玩家密码，是数据库管理员密码）
   - Region：选 **Southeast Asia (Singapore)** 或 **Northeast Asia (Tokyo)**，国内访问延迟最低
3. 等 2 分钟创建完成。

> ⚠️ 免费项目 **7 天没有活动会被自动暂停**。演示/比赛前一天，打开控制台点一下让它醒过来，否则登录会直接超时。

---

## 第 2 步：跑建表脚本（约 2 分钟）

1. 左侧 **SQL Editor** → **New query**
2. 打开项目里的 `outputs/supabase/schema.sql`，**整段复制**粘贴进去
3. 点 **Run**（或 Ctrl+Enter）

期望：没有红色报错。如果提示某条策略已存在，忽略即可（脚本可重复执行）。

跑完顺手验证一下：

```sql
select tablename, rowsecurity from pg_tables
where schemaname = 'public'
  and tablename in ('profiles','game_records','user_settings');
```

三行 `rowsecurity` 必须都是 `true`。**这是安全底线，有一行是 false 就别往下走。**

---

## 第 3 步：配置认证（约 3 分钟，最容易漏）

左侧 **Authentication**：

### 3.1 关闭邮箱验证

- **Sign In / Providers** → **Email** → 把 **Confirm email** 关掉 → Save

> 为什么必须关：你们用的是「用户名 + 密码」，前端会把用户名拼成 `用户名@bearrun.local` 这种伪邮箱。伪邮箱收不到验证信，开着确认就是注册完直接卡死。

### 3.2 填站点地址

- **URL Configuration**：
  - **Site URL**：`https://gf199lin.github.io/bear-run/`
  - **Redirect URLs** 追加两条：
    - `https://gf199lin.github.io/bear-run/**`
    - `http://127.0.0.1:8801/**`（本地调试用）

### 3.3 密码长度（可选）

- **Sign In / Providers** → Email → **Minimum password length** 默认 6。
  代码已经按 6 位校验，不用改。

---

## 第 4 步：把两个值填进页面（约 1 分钟）

左侧 **Project Settings** → **API**，复制两个值：

| 复制什么 | 填到哪 |
| --- | --- |
| **Project URL**（`https://xxxx.supabase.co`） | `url` |
| **anon / public** key（`eyJhbGci...` 很长一串） | `key` |

打开 `radar-runner/index.html`，搜索 `RR_CLOUD_CONFIG`，改成：

```js
window.RR_CLOUD_CONFIG = {
  url: 'https://你的项目ref.supabase.co',
  key: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9....',
  domain: 'bearrun.local'
};
```

> ✅ **anon key 是设计成公开的**，写进 HTML、提交到 git 都没问题。真正的权限由数据库 RLS 管。
> ❌ **service_role key 绝对不要放这里**。它绕过所有 RLS，泄露 = 数据库任人读写。

---

## 第 5 步：部署 + 验证（约 5 分钟）

```powershell
# 1. 本地先试
node .deepworks/tmp/serve.js radar-runner 8801
# 浏览器打开 http://127.0.0.1:8801/index.html
```

本地验证清单：

- [ ] 页面正常渲染，不是白屏
- [ ] 控制台没有红色报错
- [ ] 在控制台跑 `RR.Cloud.status()`，应该是 `{enabled: true, configured: true, active: false, ...}`
- [ ] 注册一个账号 → 提示成功 → 自动进游戏
- [ ] 打开 Supabase **Table Editor → profiles**，能看到刚注册的用户名和一行数据
- [ ] 打一局 → **game_records** 里出现一行

跨设备验证（这是核心）：

- [ ] 换一个浏览器（或隐身窗口）打开同一地址，用同一个用户名密码登录
- [ ] 登录后最高分和游玩次数与第一台设备一致

跑通后再推到 GitHub Pages。

---

## 日常改动流程（重要）

`index.html` 是「所有 JS 内联成一个文件」的产物。**改 `js/` 下的源文件后，必须重新内联**，否则线上跑的还是旧代码：

```powershell
node radar-runner/tools/sync-inline.js          # 同步
node radar-runner/tools/sync-inline.js --check  # 提交前检查
```

---

## 排错对照表

| 现象 | 原因 | 怎么办 |
| --- | --- | --- |
| 注册后停在原地，提示「注册成功，但需要在 Supabase 关闭 Confirm email」 | 邮箱验证没关 | 回到第 3.1 步 |
| 提示「云端服务加载失败，请检查网络」 | CDN 被墙 / 5 秒超时 | 换网络；或把 supabase-js 下载到本地 `vendor/` 再引用 |
| 登录提示「用户名或密码错误」，但密码确定对 | 用户名大小写或空格不一致 | 登录时用户名会被转小写去匹配邮箱，检查注册时是否多打了空格 |
| 注册提示「该用户名已被注册」但表里没有 | 上次注册中断留了孤儿 auth 用户 | 控制台 Authentication → Users 里删掉对应用户，再注册 |
| 登录成功但最高分是 0 | `player_stats` 视图没建成功 | 重跑 schema.sql 第 6 节 |
| 排行榜能看但永远空 | 还没人打过局 | 正常；或者 `leaderboard` 视图没 grant |
| 第二天打开就登录失效 | 免费项目被暂停 | 控制台唤醒后重新登录 |
| 换了设备数据不同步 | 用的是 file:// 双击版 | file:// 永远走本地模式，云功能只在 http(s) 下生效 |

---

## 你现在还需要补的一件事（代码层面，非必须）

**P2：把每局战绩写进云端。**

现在登录已经是真的了，但「打一局 → 上云」还差一行调用。位置在 `radar-runner/js/Game.js` 的 `_pushRecord()` 里，本地写完加：

```js
if (RR.Cloud && RR.Cloud.user) {
  RR.Cloud.pushRecord({
    score: score,
    distance: Math.floor(this.distance),
    coins: this.coins,
    combo: this.maxCombo,
    nearMiss: this.nearMiss,
    won: false
  });
}
```

`RR.Cloud.pushRecord` 已经写好了（含离线队列和失败重试）。要我现在接上，说一声。
