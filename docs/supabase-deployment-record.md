# Supabase 后台部署记录

> 本文件用于留存「后台侧具体做了什么、结果如何」的可核对证据，配合
> `outputs/supabase/schema.sql` 与 `docs/supabase-setup-checklist.md` 使用。
> 仓库公开，本文件不含任何密钥：`service_role` / 数据库密码一律不落盘。

## 1. 环境

| 项 | 值 |
| --- | --- |
| 项目 ref | `pemtcluzsirzeubnqwzx` |
| API 地址 | `https://pemtcluzsirzeubnqwzx.supabase.co` |
| 前端使用密钥 | `sb_publishable_...`（publishable，可公开，安全性由 RLS 保证） |
| 执行时间 | 2026-11-19（UTC+08:00） |
| 前端仓库 | https://github.com/gf199lin/bear-run |
| 线上入口 | https://gf199lin.github.io/bear-run/（Pages 由 `main` 部署） |

## 2. 控制台操作

1. **SQL Editor** 执行 `outputs/supabase/schema.sql`：建 `profiles` / `game_records` /
   `user_settings` 三张表，加触发器与 `player_stats`、`leaderboard` 视图，开启 RLS 并写入策略。
2. **Authentication → Sign In / Providers → Email**：关闭 **Confirm email**，
   使注册后立即拿到 `access_token`（否则前端必须先收邮件激活）。
3. **Authentication → URL Configuration**：`Site URL` 填 Pages 地址，`Redirect URLs` 加本地调试地址。

## 3. 验证结果

| 检查 | 结果 |
| --- | --- |
| `GET /auth/v1/health` | `200`，`GoTrue v2.198.0` |
| `GET /rest/v1/profiles`（匿名） | `200 []`，表可读但为空 |
| `POST /auth/v1/signup` | 返回 `access_token` + `user`，无邮件确认环节 |
| 注册后 `profiles` / `player_stats` | 由触发器自动生成对应行 |
| 匿名伪造插入 `game_records` | `401` + `42501`（RLS 拒绝），策略生效 |
| 前端 CDN `@supabase/supabase-js@2` | `200`，约 216 KB，支持 publishable key |

## 4. 前端落地

- 提交：`feat(cloud): 接入 Supabase 真实账号与云端战绩`（main）
- 线上复查：页面已包含 `window.RR_CLOUD_CONFIG`、`RR.Cloud`、`cloudRegister`、`RR.Cloud.pushRecord`
- 降级行为：`file://` 双击版或未配置密钥时，自动退化为原本地伪账号，单文件分发能力不受影响

## 5. 已知限制

- 部署前已存在的**本地伪账号**（仅存于浏览器 localStorage）不会自动迁移到云端；
  需在线上页面退出后用同一用户名重新注册，之后战绩才写入 `game_records`。
- 云端为唯一真值来源，跨设备以 `auth.uid()` 隔离，RLS 保证只能读写自己的行。
- 测试期若建过探针账号，可在 SQL Editor 清理：
  `delete from auth.users where email = 'zz_probe_delete_me@bearrun.local';`
