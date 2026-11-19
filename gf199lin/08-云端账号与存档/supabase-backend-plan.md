# Frame by Frame / Bear Run —— 接入 Supabase 真实后端方案

> 目标：把「本地伪账号 + localStorage 存档」升级为「真实账号 + 云端存档 + 跨设备历史 + 排行榜」，同时不破坏现有的「双击 index.html 即玩」离线能力。

---

## 1. 现状诊断（基于代码，不是猜测）

| 能力 | 现状 | 位置 |
| --- | --- | --- |
| 账号注册/登录 | 纯前端，用户名 + SHA-256 加盐哈希，存 `localStorage` 的 `bear-run:users:v1` | `radar-runner/js/auth.js` L20、L143-L178 |
| 登录态 | `localStorage` 的 `bear-run:session:v1`，只存用户名 | `radar-runner/js/auth.js` L44-L45 |
| 存档隔离 | 存储 key 加账号前缀 `bear-run:acct:<用户名>:<原始key>` | `radar-runner/js/auth.js` L206-L209 |
| 个人纪录 | 单条 JSON：`{best, bestDistance, bestCombo, bestNearMiss, plays, history[]}` | `radar-runner/js/Game.js` L904-L959 |
| 历史记录 | 最多 5 条，只存在那个 JSON 的 `history` 数组里 | `radar-runner/js/config.js` L167-L170、`Game.js` L962-L979 |
| 游客模式 | 全部只进内存，关页面即清空 | `radar-runner/js/auth.js` L25-L26、L211-L222 |
| 读写入口 | **全部收敛到 4 个函数**：`RR.Auth.get/set/remove/key` + `storeGet/storeSet` | `auth.js` L211-L222、`Game.js` L19-L32、`index.html` L11663-L11680 |
| 实际运行入口 | `radar-runner/index.html` 是**完全内联产物**（无任何 `<script src>`），`js/*.js` 是同一份代码的模块化源副本 | 已验证：`index.html` 内联脚本数 = 0 |

**这是好消息**：业务逻辑没有到处裸写 `localStorage`，只要在 `RR.Auth.get/set` 这一层做替换，`Game.js`、`Hud.js` 等几乎零改动。这是天然的重构接缝（seam）。

**这是坏消息 / 真正的难点**：`localStorage` 是**同步** API，Supabase 是**异步** API。而 `_loadRecords()` 是同步返回的，结算流程依赖它同步拿到数据。全链路改 `await` 会污染十几处调用点，风险高、回滚难。

**结论：不要改成全异步。用「内存缓存 + 本地持久层 + 异步回写云端」的三层结构。**

```
业务代码（同步，完全不变）
        ↓  RR.Auth.get / .set
内存缓存（Map，同步读写）
        ↓
localStorage（同步落盘，离线兜底 / 缓存）
        ↓  异步、可失败、可重试
Supabase（真源，跨设备）
```

启动时先从 Supabase 把数据拉进内存和 localStorage，之后运行时照旧同步读写，写入时"先写本地（保证永不丢）→ 再异步推云端（失败就排队重试）"。**这样你既拿到了真实数据，又保留了 `file://` 双击即玩。**

---

## 2. 为什么 Supabase 是对的选择

- **纯静态托管友好**：你们是 GitHub Pages，没有 Node 服务器。Supabase 提供「前端直连数据库 + RLS 行级权限」，正是为这种场景设计的，不需要自己写一套后端 API。
- **认证开箱即用**：邮箱密码、邮箱验证码、OAuth（GitHub/Google）、匿名登录都内置，密码哈希、会话刷新、跨设备登录全部由它负责，你不再自己碰密码。
- **Postgres**：历史记录天然是表，统计（最高分、总局数）用视图/聚合，排行榜一条 SQL。
- **免费层够用**：500MB 数据库、5 万月活、5GB 带宽。你们这个体量远远不够用满。

**必须知道的三个坑（真会踩）：**

1. **免费项目 7 天无活动会被自动暂停**。比赛/演示前一天务必打开控制台点一下，或跑一次查询唤醒。
2. **`anon` key 放前端是正确且公开的**，安全性 100% 依赖 RLS。**漏写一条策略 = 全表裸奔**。`service_role` key 绝对不能出现在 `index.html` 里（它也永远不该进 git）。
3. **GitHub Pages 域名必须加进 Supabase 的 Redirect URLs**，否则邮箱验证链接会跳失败。

---

## 3. 数据模型（可直接执行）

### 3.1 表结构

```sql
-- 玩家资料：与 auth.users 一对一
create table public.profiles (
  id         uuid primary key references auth.users(id) on delete cascade,
  username   text unique not null
             check (username ~ '^[A-Za-z0-9_\u4e00-\u9fa5]{2,20}$'),
  created_at timestamptz not null default now()
);

-- 每一局一条：这是"真实历史记录"的来源
create table public.game_records (
  id          bigserial primary key,
  user_id     uuid not null references public.profiles(id) on delete cascade,
  score       int  not null default 0 check (score      between 0 and 1000000),
  distance    int  not null default 0 check (distance   between 0 and 1000000),
  coins       int  not null default 0 check (coins      between 0 and 100000),
  combo       int  not null default 0 check (combo      between 0 and 100000),
  near_miss   int  not null default 0 check (near_miss  between 0 and 100000),
  won         boolean not null default false,
  duration_ms int,
  created_at  timestamptz not null default now()   -- 用服务端时间，不信客户端
);

create index game_records_user_time_idx
  on public.game_records (user_id, created_at desc);

-- 玩家偏好（输入源、角色），取代 <账号>:bear-run:input-source 这类 key
create table public.user_settings (
  user_id      uuid primary key references public.profiles(id) on delete cascade,
  input_source text,
  character    text,
  updated_at   timestamptz not null default now()
);
```

### 3.2 注册时自动建 profile

```sql
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, username)
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'username', split_part(new.email, '@', 1))
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();
```

### 3.3 统计视图（替代原来那个 JSON 汇总）

```sql
create or replace view public.player_stats
with (security_invoker = true) as
select
  p.id as user_id,
  p.username,
  coalesce(max(r.score), 0)      as best,
  coalesce(max(r.distance), 0)   as best_distance,
  coalesce(max(r.combo), 0)      as best_combo,
  coalesce(max(r.near_miss), 0)  as best_near_miss,
  count(r.id)                    as plays
from public.profiles p
left join public.game_records r on r.user_id = p.id
group by p.id, p.username;
```

> `security_invoker = true` 让视图尊重调用者的 RLS，不会越权。

### 3.4 公开排行榜（只暴露两列，不暴露 user_id）

```sql
-- 用 definer 视图绕过 RLS，但只 SELECT 最小字段集
create or replace view public.leaderboard as
select username, best, plays
from public.player_stats
order by best desc
limit 100;

grant select on public.leaderboard to anon, authenticated;
```

### 3.5 RLS 策略（最关键的一步）

```sql
alter table public.profiles      enable row level security;
alter table public.game_records  enable row level security;
alter table public.user_settings enable row level security;

-- profiles：只能读写自己
create policy "profiles_select_own" on public.profiles
  for select using (auth.uid() = id);
create policy "profiles_update_own" on public.profiles
  for update using (auth.uid() = id) with check (auth.uid() = id);
create policy "profiles_insert_own" on public.profiles
  for insert with check (auth.uid() = id);

-- game_records：只能读自己的、只能以自己的身份插入
create policy "records_select_own" on public.game_records
  for select using (auth.uid() = user_id);
create policy "records_insert_own" on public.game_records
  for insert with check (auth.uid() = user_id);
-- 故意不给 update / delete：历史记录是事实，不允许改

-- settings：读写自己
create policy "settings_all_own" on public.user_settings
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
```

### 3.6 用户名查重（不泄露整张表）

```sql
create or replace function public.username_available(uname text)
returns boolean
language sql
security definer
set search_path = public
as $$
  select not exists (select 1 from public.profiles where lower(username) = lower(uname));
$$;

grant execute on function public.username_available(text) to anon;
```

---

## 4. 认证方案：怎么兼容你们现在的「用户名 + 密码」UI

Supabase Auth **只认 email 或 phone，不认裸用户名**。三种走法：

| 方案 | 做法 | 优点 | 缺点 |
| --- | --- | --- | --- |
| **A. 伪邮箱（推荐，改动最小）** | 注册时前端拼 `username@bearrun.local`，关闭邮箱确认 | UI 完全不用改，用户仍然只输用户名+密码 | 用户无法找回密码（没有真邮箱）；丢弃邮箱就等于丢弃找回能力 |
| **B. 真邮箱** | 表单加一个邮箱输入框，用户名存 `profiles.username` | 能找回密码、能发验证 | UI 要加一栏，注册多一步 |
| **C. 邮箱验证码 / 免密** | 邮箱 OTP 或 Magic Link | 不用记密码，最安全 | 要配 SMTP（免费层自带额度有限），体验变化大 |

**建议**：比赛演示用 **A**（最快），产品化后迁到 **B**。无论哪种，密码都交给 Supabase，**云端路径下前端不再做 `SHA-256 + salt`**。

> 实现时补充：`auth.js` 里的哈希逻辑**保留了**，只作为 `file://` 双击离线版的降级路径（保住「双击 index.html 即玩」的分发方式）。云端模式下这段代码不会被执行，密码只经过 Supabase Auth。

方案 A 的关键配置：
- Supabase 控制台 → Authentication → Providers → Email → **关闭 "Confirm email"**
- Authentication → URL Configuration → Site URL 填 `https://<你们的 GitHub Pages 域名>`
- 前端注册时：

```js
const email = `${username.toLowerCase()}@bearrun.local`;
const { data, error } = await supabase.auth.signUp({
  email,
  password,
  options: { data: { username } }   // 触发 handle_new_user 用它建 profile
});
```

> `@bearrun.local` 必须是你们自己的假域，别用 `@gmail.com` 之类，避免误发邮件。

---

## 5. 前端改造：4 个文件、1 个新文件

### 5.1 新增 `js/cloud.js`（核心适配层）

这层负责：初始化 Supabase、缓存、离线队列、把云端数据映射成本地 key。

```js
(function (global) {
  'use strict';
  var RR = global.RR = global.RR || {};

  // 只在 http(s) 下启用；file:// 双击即玩时自动降级为纯本地
  var ENABLED = /^https?:$/.test(global.location.protocol);

  var Cloud = {
    enabled: ENABLED,
    ready: false,
    user: null,
    sb: null,
    _mem: Object.create(null),      // raw key -> string
    _pending: [],                   // 待同步的写操作

    async init() {
      if (!ENABLED || !global.supabase) return false;
      this.sb = global.supabase.createClient(RR.CFG.SUPABASE_URL, RR.CFG.SUPABASE_ANON_KEY);
      var s = await this.sb.auth.getSession();
      this.user = s.data.session ? s.data.session.user : null;
      if (this.user) await this.pull();
      this.ready = true;
      this._flush();
      return true;
    },

    // 启动时把云端数据灌进内存 + localStorage，业务代码照旧同步读
    async pull() {
      var uid = this.user.id;

      var st = await this.sb.from('user_settings').select('*').eq('user_id', uid).maybeSingle();
      if (st.data) {
        if (st.data.input_source != null) this._put(RR.CFG.STORAGE_KEY_INPUT, st.data.input_source);
        if (st.data.character   != null) this._put(RR.CFG.STORAGE_KEY_CHAR,  st.data.character);
      }

      var rs = await this.sb.from('game_records')
        .select('score,distance,coins,combo,near_miss,won,created_at')
        .eq('user_id', uid)
        .order('created_at', { ascending: false })
        .limit(RR.CFG.RECORDS.historyMax);

      var stats = await this.sb.from('player_stats').select('*').eq('user_id', uid).maybeSingle();
      if (stats.data) {
        var rec = {
          best:           stats.data.best,
          bestDistance:   stats.data.best_distance,
          bestCombo:      stats.data.best_combo,
          bestNearMiss:   stats.data.best_near_miss,
          plays:          stats.data.plays,
          history: (rs.data || []).map(function (r) {
            return {
              score: r.score, distance: r.distance, coins: r.coins,
              combo: r.combo, nearMiss: r.near_miss, won: r.won,
              at: Date.parse(r.created_at)
            };
          })
        };
        this._put(RR.CFG.STORAGE_KEY_RECORDS, JSON.stringify(rec));
        this._put(RR.CFG.STORAGE_KEY, String(rec.best));
      }
    },

    _put(raw, val) {
      this._mem[raw] = String(val);
      try { global.localStorage.setItem(raw, String(val)); } catch (e) {}
    },

    get(raw) {
      return (raw in this._mem) ? this._mem[raw] : null;
    },

    set(raw, val) {
      this._mem[raw] = String(val);
      try { global.localStorage.setItem(raw, String(val)); } catch (e) {}
      this._queue(raw, val);
    },

    // 只同步这两类偏好；纪录走专门的 pushRecord（因为是 append-only 的局记录）
    _queue(raw, val) {
      var C = RR.CFG;
      if (!this.user) return;
      if (raw === C.STORAGE_KEY_INPUT || raw === C.STORAGE_KEY_CHAR) {
        this._pending.push({ type: 'settings', raw: raw, val: val });
        this._flush();
      }
    },

    async _flush() {
      if (!this.ready || !this.user || !this._pending.length) return;
      var batch = this._pending.splice(0, this._pending.length);
      var uid = this.user.id;
      var patch = { user_id: uid, updated_at: new Date().toISOString() };
      batch.forEach(function (op) {
        if (op.raw === RR.CFG.STORAGE_KEY_INPUT) patch.input_source = op.val;
        if (op.raw === RR.CFG.STORAGE_KEY_CHAR)  patch.character    = op.val;
      });
      var res = await this.sb.from('user_settings').upsert(patch, { onConflict: 'user_id' });
      if (res.error) this._pending = this._pending.concat(batch); // 失败留着重试
    },

    // 结算时调用：一局一条，服务端生成 created_at
    async pushRecord(rec) {
      if (!this.user) return;
      var res = await this.sb.from('game_records').insert({
        user_id:   this.user.id,
        score:     rec.score,
        distance:  rec.distance,
        coins:     rec.coins,
        combo:     rec.combo,
        near_miss: rec.nearMiss,
        won:       !!rec.won,
        duration_ms: rec.durationMs || null
      });
      if (res.error) {
        // 离线/失败：塞进本地队列，下次启动补传
        var q = JSON.parse(global.localStorage.getItem('bear-run:outbox') || '[]');
        q.push(rec);
        global.localStorage.setItem('bear-run:outbox', JSON.stringify(q));
      }
    },

    async flushOutbox() {
      if (!this.user) return;
      var q = JSON.parse(global.localStorage.getItem('bear-run:outbox') || '[]');
      if (!q.length) return;
      for (var i = 0; i < q.length; i++) {
        var r = q[i];
        var res = await this.sb.from('game_records').insert({
          user_id: this.user.id, score: r.score, distance: r.distance,
          coins: r.coins, combo: r.combo, near_miss: r.nearMiss, won: !!r.won
        });
        if (!res.error) q[i] = null;
      }
      global.localStorage.setItem('bear-run:outbox', JSON.stringify(q.filter(Boolean)));
    }
  };

  RR.Cloud = Cloud;
})(window);
```

### 5.2 改 `js/auth.js`：把存储委托给 Cloud

只改 `get/set/remove` 三个方法（L211-L222），其余保持不变：

```js
get: function (raw) {
  if (isGuest()) return (raw in guestStore) ? guestStore[raw] : null;
  if (RR.Cloud && RR.Cloud.enabled && RR.Cloud.user) {
    var v = RR.Cloud.get(raw);
    if (v != null) return v;
  }
  return getLS(Auth.key(raw));
},
set: function (raw, val) {
  if (isGuest()) { guestStore[raw] = String(val); return; }
  if (RR.Cloud && RR.Cloud.enabled && RR.Cloud.user) RR.Cloud.set(raw, val);
  setLS(Auth.key(raw), val);
},
remove: function (raw) {
  if (isGuest()) { delete guestStore[raw]; return; }
  removeLS(Auth.key(raw));
}
```

同时把 `register/login/logout` 改成走 Supabase（保留同一个 `Promise<{ok,msg}>` 契约，UI 一行不用改）：

```js
register: function (username, password, confirm) {
  username = (username || '').trim();
  if (!/^[A-Za-z0-9_\u4e00-\u9fa5]{2,20}$/.test(username))
    return Promise.resolve({ ok: false, msg: '用户名需 2-20 位（中英文/数字/下划线）' });
  if (!password || password.length < 4)
    return Promise.resolve({ ok: false, msg: '密码至少 4 位' });
  if (confirm !== undefined && password !== confirm)
    return Promise.resolve({ ok: false, msg: '两次输入的密码不一致' });

  var sb = RR.Cloud.sb;
  return sb.rpc('username_available', { uname: username }).then(function (r) {
    if (r.data === false) throw new Error('该用户名已被注册');
    return sb.auth.signUp({
      email: username.toLowerCase() + '@bearrun.local',
      password: password,
      options: { data: { username: username } }
    });
  }).then(function (r) {
    if (r.error) throw new Error(/already/i.test(r.error.message) ? '该用户名已被注册' : r.error.message);
    return { ok: true, msg: '注册成功' };
  }).catch(function (e) {
    return { ok: false, msg: e.message || '注册失败，请重试' };
  });
},

login: function (username, password) {
  var sb = RR.Cloud.sb;
  return sb.auth.signInWithPassword({
    email: username.trim().toLowerCase() + '@bearrun.local',
    password: password
  }).then(function (r) {
    if (r.error) return { ok: false, msg: '用户名或密码错误' };
    saveSession(username);
    return { ok: true, msg: '登录成功' };
  });
},

logout: function () {
  if (RR.Cloud && RR.Cloud.sb) RR.Cloud.sb.auth.signOut();
  saveSession(null);
  clearGuestMarker();
  guestStore = {};
}
```

### 5.3 改结算：多写一行

在 `Game.js` `_pushRecord()`（L962 附近）末尾，本地写完之后加：

```js
if (RR.Cloud && RR.Cloud.user) {
  RR.Cloud.pushRecord({
    score: score,
    distance: Math.floor(this.distance),
    coins: this.coins,
    combo: this.maxCombo,
    nearMiss: this.nearMiss,
    won: !!(challenge && challenge.done),
    durationMs: this.durationMs
  });
}
```

**注意**：本地 `history` 仍然照旧维护并截断到 5 条，不要删这段逻辑——它是离线兜底。云端才是完整历史，页面上想看全量历史时再单独查表。

### 5.4 改 `index.html`：引入 SDK + 初始化

```html
<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
<script>
  RR.CFG.SUPABASE_URL = 'https://xxxx.supabase.co';
  RR.CFG.SUPABASE_ANON_KEY = 'eyJhbGciOi...';   // 公开，可进 git
  RR.Cloud.init().then(function () { RR.Cloud.flushOutbox(); });
</script>
```

> **前提**：`index.html` 现在是全内联、无外链脚本的产物。加一个 `<script src>` 会破坏"双击即玩"。建议保留两套构建：
> - `index.html`（内联版，GitHub Pages 用 ✅ 云功能）
> - `outputs/bear-run-file/index.html`（纯本地版，发给别人双击玩 ❌ 无云）
> 用 `.deepworks/tmp/build-standalone.ps1` 的分支逻辑生成：内联时把 SDK 也内联或直接去掉云配置。

---

## 6. 本地老数据迁移

老玩家第一次登录新系统时，把 `bear-run:acct:<用户名>:*` 里的数据搬上去，只搬一次：

```js
async function migrateLocalToCloud(username) {
  var flag = 'bear-run:migrated:v2';
  if (localStorage.getItem(flag)) return;
  var prefix = 'bear-run:acct:' + username + ':';
  var recKey = prefix + RR.CFG.STORAGE_KEY_RECORDS;
  var raw = localStorage.getItem(recKey);
  if (raw) {
    try {
      var d = JSON.parse(raw);
      // 历史是 append-only，逐条补传，保留原始时间
      for (var i = (d.history || []).length - 1; i >= 0; i--) {
        var h = d.history[i];
        await RR.Cloud.sb.from('game_records').insert({
          user_id: RR.Cloud.user.id,
          score: h.score, distance: h.distance, coins: h.coins,
          combo: h.combo, near_miss: h.nearMiss, won: !!h.won,
          created_at: new Date(h.at || Date.now()).toISOString()
        });
      }
    } catch (e) {}
  }
  localStorage.setItem(flag, '1');
}
```

> 只有存了历史的 5 局能补传。更早的局在旧系统里本来就没存，**这正是换 Supabase 的意义：以后每一局都不会丢**。

---

## 7. 分阶段落地顺序（含验收标准）

| 阶段 | 内容 | 工作量 | 验收标准 |
| --- | --- | --- | --- |
| **P0** | 建 Supabase 项目、跑 §3 全部 SQL、配 Auth（关邮箱确认、加站点 URL） | 1-2 小时 | 控制台 Table Editor 能看到 3 张表；`username_available` 能调通 |
| **P1** | 接 `js/cloud.js` + 改造 `auth.js` 的 register/login/logout | 半天 | 两台设备/两个浏览器分别登录同一账号，登录态都在；A 设备注册的账号在 B 设备能登 |
| **P2** | 结算写入 `game_records`，接 `player_stats` 做首页纪录展示 | 半天 | A 设备打一局，B 设备刷新后 `best`/`plays` 变了；数据库里每局一条 |
| **P3** | 偏好同步 + 离线队列 + 老数据迁移 | 半天 | 断网打一局，恢复网络后刷新，那一局出现在云端 |
| **P4** | 排行榜页面 + 防刷 | 1 天 | 排行榜只显示 username/best；伪造 `user_id` 的插入被 RLS 拒绝 |

**先做 P0+P1**。这两步做完，"登录是真的、账号是跨设备的"这个核心痛点就解决了，后面的都是增量。

---

## 8. 安全红线（逐条核对）

- [ ] `service_role` key **没有**出现在任何前端文件、`git log`、截图里。
- [ ] 三张表全部 `enable row level security`，每条策略都验证过：用 A 账号的 token 读 B 账号数据必须返回空。
- [ ] 前端不再计算/存储密码哈希（删掉 `auth.js` L63-L89）。
- [ ] `anon` key 可以公开；但**不要**给 `anon` 任何表的直接 `select` 权限，只给 `leaderboard` 视图。
- [ ] `game_records` 不给 `update/delete` 策略——历史一旦写入不可篡改。
- [ ] Supabase 项目设置里 **关闭 "Confirm email"**（否则伪邮箱收不到验证邮件，注册直接卡死）。

---

## 9. 一个必须说清楚的产品限制

客户端游戏的数据**天然不可信**。玩家改内存、改请求，就能提交 999999 分。RLS 只能保证"他改不了别人的数据"，保证不了"他自己提交的分数是真的"。

要做到真防作弊，只有两条路：
1. **服务端判定**（你们有 `radar-agent` / `radar-service`，可以让它持有 `service_role` 或 Edge Function，由它校验并写分）；
2. **加权信任**：分数超过阈值时进 `suspicious` 标记，人工复核后再上排行榜。

比赛/演示阶段可以先不管，但**别把排行榜当竞技排名的依据**。

---

## 10. 备选方案对比（如果不选 Supabase）

| 方案 | 适合 | 代价 |
| --- | --- | --- |
| **Supabase** | 纯静态站 + 要 Auth + 要 SQL | 免费层 7 天不活跃暂停 |
| Firebase | 要实时同步、要推送 | 强绑定 Google，查询能力弱 |
| Cloudflare D1 + Workers | 已经很熟 CF | Auth 要自己写，等于重造一遍 |
| 自建 Node + Postgres | 有服务器、要完全掌控 | 运维成本，黑客松不划算 |

你们是 GitHub Pages + 无服务器，**Supabase 是最省事且唯一不需要自己写后端 API 的方案**。
