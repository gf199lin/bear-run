-- ============================================================================
-- Bear Run / Frame by Frame —— Supabase 后端建库脚本
--
-- 用法：Supabase 控制台 → SQL Editor → 新建 query → 整段粘贴 → Run
-- 可重复执行（全部带 if not exists / drop ... if exists / create or replace）
--
-- 设计原则：
--   1. 历史记录是「一局一行」的 append-only 事实表，不做 update/delete 策略；
--   2. 所有表都开 RLS，玩家只能碰自己的行；
--   3. 排行榜用独立视图，只暴露 username + best，不暴露 user_id；
--   4. 时间一律用服务端 now()，不信客户端时钟。
-- ============================================================================


-- ---------------------------------------------------------------------------
-- 1. 玩家资料（与 auth.users 一对一）
-- ---------------------------------------------------------------------------
create table if not exists public.profiles (
  id         uuid primary key references auth.users(id) on delete cascade,
  username   text not null unique
             check (char_length(username) between 2 and 20 and username !~ '[\s]'),
  created_at timestamptz not null default now()
);

comment on table  public.profiles is '玩家资料，id 与 Supabase Auth 用户一一对应';
comment on column public.profiles.username is '展示用用户名，全局唯一（大小写敏感；登录邮箱层已做小写归一）';


-- ---------------------------------------------------------------------------
-- 2. 每局战绩（真实历史记录的来源）
-- ---------------------------------------------------------------------------
create table if not exists public.game_records (
  id          bigserial primary key,
  user_id     uuid not null references public.profiles(id) on delete cascade,
  score       integer not null default 0 check (score      between 0 and 1000000),
  distance    integer not null default 0 check (distance   between 0 and 1000000),
  coins       integer not null default 0 check (coins      between 0 and 100000),
  combo       integer not null default 0 check (combo      between 0 and 100000),
  near_miss   integer not null default 0 check (near_miss  between 0 and 100000),
  won         boolean not null default false,
  duration_ms integer,
  created_at  timestamptz not null default now()
);

create index if not exists game_records_user_time_idx
  on public.game_records (user_id, created_at desc);

-- 全站排行榜按分数取前 N 时用
create index if not exists game_records_score_idx
  on public.game_records (score desc);

comment on table public.game_records is '一局一条的 append-only 战绩，不做 update/delete';


-- ---------------------------------------------------------------------------
-- 3. 玩家偏好（输入源 / 角色），替代原 localStorage 的散装 key
-- ---------------------------------------------------------------------------
create table if not exists public.user_settings (
  user_id      uuid primary key references public.profiles(id) on delete cascade,
  input_source text,
  character    text,
  updated_at   timestamptz not null default now()
);


-- ---------------------------------------------------------------------------
-- 4. 注册时自动建 profile
--    注册前前端会传 options.data.username，这里取出来落库。
-- ---------------------------------------------------------------------------
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
    coalesce(
      nullif(new.raw_user_meta_data->>'username', ''),
      split_part(new.email, '@', 1)
    )
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();


-- ---------------------------------------------------------------------------
-- 5. 用户名查重（给注册页用，避免把整张 profiles 暴露出去）
-- ---------------------------------------------------------------------------
create or replace function public.username_available(uname text)
returns boolean
language sql
security definer
set search_path = public
as $$
  select not exists (
    select 1 from public.profiles
    where lower(username) = lower(trim(uname))
  );
$$;

revoke all on function public.username_available(text) from public;
grant execute on function public.username_available(text) to anon, authenticated;


-- ---------------------------------------------------------------------------
-- 6. 个人统计视图：从 game_records 聚合，不重复存一份计数
-- ---------------------------------------------------------------------------
create or replace view public.player_stats
with (security_invoker = true) as
select
  p.id                          as user_id,
  p.username,
  coalesce(max(r.score), 0)     as best,
  coalesce(max(r.distance), 0)  as best_distance,
  coalesce(max(r.combo), 0)     as best_combo,
  coalesce(max(r.near_miss), 0) as best_near_miss,
  count(r.id)                   as plays
from public.profiles p
left join public.game_records r on r.user_id = p.id
group by p.id, p.username;

comment on view public.player_stats is '每人最高分/总局数，从 game_records 实时聚合';


-- ---------------------------------------------------------------------------
-- 7. 公开排行榜：只暴露 username + best + plays
--    视图 owner 是 postgres，会绕过 RLS —— 这是刻意的，
--    但字段被裁剪到最小集合，user_id / 时间 / 单局明细都不外泄。
-- ---------------------------------------------------------------------------
create or replace view public.leaderboard as
select username, best, plays
from public.player_stats
order by best desc, plays desc
limit 100;

revoke all on public.leaderboard from public;
grant select on public.leaderboard to anon, authenticated;


-- ---------------------------------------------------------------------------
-- 8. RLS —— 真正的权限边界
-- ---------------------------------------------------------------------------
alter table public.profiles      enable row level security;
alter table public.game_records  enable row level security;
alter table public.user_settings enable row level security;

-- profiles：只能读 / 改自己
drop policy if exists profiles_select_own on public.profiles;
create policy profiles_select_own on public.profiles
  for select using (auth.uid() = id);

drop policy if exists profiles_insert_own on public.profiles;
create policy profiles_insert_own on public.profiles
  for insert with check (auth.uid() = id);

drop policy if exists profiles_update_own on public.profiles;
create policy profiles_update_own on public.profiles
  for update using (auth.uid() = id) with check (auth.uid() = id);

-- game_records：只能读自己的、只能以自己的身份插入
-- 故意不给 update / delete —— 历史记录写进去就是事实
drop policy if exists records_select_own on public.game_records;
create policy records_select_own on public.game_records
  for select using (auth.uid() = user_id);

drop policy if exists records_insert_own on public.game_records;
create policy records_insert_own on public.game_records
  for insert with check (auth.uid() = user_id);

-- user_settings：读写都只能是自己
drop policy if exists settings_all_own on public.user_settings;
create policy settings_all_own on public.user_settings
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);


-- ---------------------------------------------------------------------------
-- 9. 表权限（Supabase 默认已授予 public schema，这里显式写一遍便于排查）
-- ---------------------------------------------------------------------------
grant usage on schema public to anon, authenticated;
grant select, insert, update on public.profiles      to authenticated;
grant select, insert          on public.game_records  to authenticated;
grant select, insert, update  on public.user_settings to authenticated;
grant usage, select           on public.game_records_id_seq to authenticated;
grant select on public.player_stats to authenticated;


-- ============================================================================
-- 验证清单（逐条在 SQL Editor 里跑，确认结果符合预期）
-- ============================================================================
--
-- 1) 三张表都开了 RLS，且策略数量对得上：
--    select tablename, rowsecurity from pg_tables
--    where schemaname = 'public'
--      and tablename in ('profiles','game_records','user_settings');
--    期望：三行 rowsecurity 都是 true
--
-- 2) 策略清单：
--    select tablename, policyname, cmd from pg_policies
--    where schemaname = 'public' order by tablename, policyname;
--
-- 3) 触发器在：
--    select tgname from pg_trigger where tgname = 'on_auth_user_created';
--
-- 4) 排行榜视图能读（未登录身份）：
--    select * from public.leaderboard;
--    期望：不报错；数据为空时返回 0 行
--
-- 5) 越权测试（在浏览器控制台用登录后的 client 跑，A 账号查 B 的 id）：
--    const { data } = await sb.from('game_records').select('*').eq('user_id', '<另一个人的uuid>');
--    期望：data 是空数组（RLS 挡住），不是报错、更不是别人的数据
--
-- 6) 恶意插入测试（伪造别人的 user_id）：
--    await sb.from('game_records').insert({ user_id: '<别人的uuid>', score: 999 });
--    期望：报错 "new row violates row-level security policy"
--
-- ============================================================================
