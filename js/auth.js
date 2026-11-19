/**
 * 账号系统 —— 双模式（云端 / 本地降级）。
 *
 * 【云模式】http(s) 打开且已配置 Supabase 时自动启用：
 *   - 注册 / 登录交给 Supabase Auth：密码哈希、会话刷新、跨设备登录、找回密码
 *     全部由服务端负责，前端不再自己存密码；
 *   - 存档读写经 RR.Cloud（见 js/cloud.js）：内存缓存 + 本地落盘 + 异步上云。
 *
 * 【本地模式】file:// 双击打开、或没配 SUPABASE_URL 时启用：
 *   - 退回原来的本地伪账号（localStorage + SHA-256 加盐），仅用于同机演示，
 *     换设备 / 清缓存不同步，也防不了懂技术的人改本地数据。
 *   - 这是为了保住"双击 index.html 即玩"的分发方式，不是给正式环境用的。
 *
 * 外部契约（UI 一行不用改）：register / login 仍然返回 Promise<{ok, msg}>。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};

  var USERS_KEY   = 'bear-run:users:v1';
  var SESSION_KEY = 'bear-run:session:v1';
  var MIGRATED_KEY = 'bear-run:migrated:v1';
  var GUEST_KEY   = 'bear-run:guest:v1';

  // 游客模式：所有存档/偏好只写进内存，不落盘 -> 关掉页面就什么都没了，无历史
  var guestStore = {};

  var usersCache = null;

  function getLS(key) { try { return global.localStorage.getItem(key); } catch (e) { return null; } }
  function setLS(key, v) { try { global.localStorage.setItem(key, v); } catch (e) { /* 忽略 */ } }
  function removeLS(key) { try { global.localStorage.removeItem(key); } catch (e) { /* 忽略 */ } }

  /** 云端是否可用（已配置 + 当前是 http(s) 环境） */
  function cloudOn() {
    return !!(RR.Cloud && RR.Cloud.enabled && RR.Cloud.configured);
  }
  /** 当前是否真的登录在云端（有有效用户），决定存档走云还是走本地 */
  function cloudActive() {
    return !!(RR.Cloud && RR.Cloud.active);
  }

  /** 用户名 -> Supabase Auth 的登录邮箱（Supabase 只认 email / phone） */
  function emailOf(username) {
    var env = (global.RR_CLOUD_CONFIG || {});
    var domain = env.domain || (RR.CFG && RR.CFG.SUPABASE_ACCOUNT_DOMAIN) || 'bearrun.local';
    return String(username).trim().toLowerCase().replace(/\s+/g, '') + '@' + domain;
  }

  function mapAuthError(msg) {
    msg = String(msg || '');
    if (/already registered|already exists|duplicate|user already/i.test(msg)) return '该用户名已被注册';
    if (/password.*(6|least|short)|weak.?password/i.test(msg)) return '密码至少 6 位';
    if (/rate limit|too many|over_email_send/i.test(msg)) return '操作太频繁，请稍后再试';
    if (/invalid.*(email|credentials)|invalid login/i.test(msg)) return '用户名或密码错误';
    if (/network|fetch|Failed to fetch/i.test(msg)) return '网络异常，请检查连接后重试';
    return msg || '操作失败，请重试';
  }

  function loadUsers() {
    if (usersCache) return usersCache;
    var out = {};
    var raw = getLS(USERS_KEY);
    if (raw) { try { var d = JSON.parse(raw); if (d && typeof d === 'object') out = d; } catch (e) { out = {}; } }
    usersCache = out;
    return out;
  }
  function saveUsers() { setLS(USERS_KEY, JSON.stringify(usersCache || {})); }

  function currentUser() { return getLS(SESSION_KEY) || null; }

  /**
   * 写入登录态。
   * 注意：必须同时清掉游客标记 —— 否则在游客状态下注册/登录后，
   * mode() 会一直返回 'guest'，存档只进内存不落盘（关页面就丢）。
   */
  function saveSession(u) {
    if (u) {
      setLS(SESSION_KEY, u);
      clearGuestMarker();
    } else {
      removeLS(SESSION_KEY);
    }
  }

  // 游客标记：优先存 sessionStorage（关标签页即清空），无可降级到 localStorage
  function getGuestMarker() {
    try { if ('sessionStorage' in global) return global.sessionStorage.getItem(GUEST_KEY); } catch (e) { /* 忽略 */ }
    return getLS(GUEST_KEY);
  }
  function setGuestMarker() {
    try { if ('sessionStorage' in global) { global.sessionStorage.setItem(GUEST_KEY, '1'); return; } } catch (e) { /* 忽略 */ }
    setLS(GUEST_KEY, '1');
  }
  function clearGuestMarker() {
    try { if ('sessionStorage' in global) { global.sessionStorage.removeItem(GUEST_KEY); } } catch (e) { /* 忽略 */ }
    removeLS(GUEST_KEY);
  }
  function isGuest() { return !!getGuestMarker(); }

  // ---- 以下哈希仅本地降级模式使用（file:// 双击版），云端模式完全不碰密码 ----

  function weakHash(str) {
    var h = 5381;
    for (var i = 0; i < str.length; i++) { h = ((h << 5) + h + str.charCodeAt(i)) | 0; }
    return 'w' + (h >>> 0).toString(16);
  }

  function sha256(text) {
    if (global.crypto && global.crypto.subtle && global.crypto.subtle.digest) {
      return global.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)).then(function (buf) {
        return Array.prototype.map.call(new Uint8Array(buf), function (b) {
          return ('0' + b.toString(16)).slice(-2);
        }).join('');
      });
    }
    return Promise.resolve(weakHash(text));
  }

  function randSalt() {
    var bytes = (global.crypto && global.crypto.getRandomValues)
      ? global.crypto.getRandomValues(new Uint8Array(12))
      : null;
    if (bytes) {
      return Array.prototype.map.call(bytes, function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
    }
    return String(Date.now()) + Math.random().toString(16).slice(2);
  }

  /**
   * 一键把旧的"无账号前缀"存档迁进某个账号（全站只迁一次）。
   * 老玩家里已有最高分/纪录的，注册第一个账号时自动接过来，不丢纪录。
   */
  function migrateLegacyOnce(username) {
    if (getLS(MIGRATED_KEY)) return;
    var cfg = RR.CFG || {};
    var legacy = [
      cfg.STORAGE_KEY_RECORDS,
      cfg.STORAGE_KEY
    ];
    var i, raw, v, target;
    for (i = 0; i < legacy.length; i++) {
      raw = legacy[i];
      if (!raw) continue;
      v = getLS(raw);
      if (v == null) continue;
      target = 'bear-run:acct:' + username + ':' + raw;
      if (getLS(target) == null) setLS(target, v);
      removeLS(raw);
    }
    // 输入源 / 角色 这类偏好也跟首个账号走
    ['bear-run:input-source', 'bear-run:character'].forEach(function (k) {
      v = getLS(k);
      if (v == null) return;
      target = 'bear-run:acct:' + username + ':' + k;
      if (getLS(target) == null) setLS(target, v);
      removeLS(k);
    });
    setLS(MIGRATED_KEY, '1');
  }

  // ---------------------------------------------------------------- 本地降级实现

  function localRegister(username, password) {
    var users = loadUsers();
    var lower = username.toLowerCase();
    for (var u in users) {
      if (u.toLowerCase() === lower) return Promise.resolve({ ok: false, msg: '该用户名已被注册' });
    }
    var salt = randSalt();
    return sha256(salt + '::' + password).then(function (hash) {
      users[username] = { s: salt, h: hash, created: Date.now() };
      saveUsers();
      if (Object.keys(users).length === 1) migrateLegacyOnce(username);
      saveSession(username);
      return { ok: true, msg: '注册成功（本地模式，仅本机有效）' };
    });
  }

  function localLogin(username, password) {
    var users = loadUsers();
    var user = null, key = null;
    for (var u in users) { if (u.toLowerCase() === username.toLowerCase()) { user = users[u]; key = u; break; } }
    if (!user || !user.s) return Promise.resolve({ ok: false, msg: '用户名或密码错误' });
    return sha256(user.s + '::' + password).then(function (hash) {
      if (hash !== user.h) return { ok: false, msg: '用户名或密码错误' };
      saveSession(key);
      return { ok: true, msg: '登录成功' };
    });
  }

  // ---------------------------------------------------------------- 云端实现

  function cloudRegister(username, password) {
    var C = RR.Cloud;
    return C.ensureClient().then(function (sb) {
      return sb.rpc('username_available', { uname: username });
    }).then(function (r) {
      if (r && r.error) throw new Error('用户名校验失败，请稍后重试');
      if (r && r.data === false) throw new Error('该用户名已被注册');
      return C.ensureClient();
    }).then(function (sb) {
      return sb.auth.signUp({
        email: emailOf(username),
        password: password,
        options: { data: { username: username } }   // 触发器用它写入 profiles.username
      });
    }).then(function (r) {
      if (r.error) throw new Error(mapAuthError(r.error.message));
      if (!r.data || !r.data.session) {
        // Supabase 默认开启邮箱确认；伪邮箱收不到邮件，会卡在这里
        throw new Error('注册成功，但需要在 Supabase 关闭「Confirm email」才能直接登录');
      }
      saveSession(username);
      C.user = r.data.session.user;
      C.username = username;
      C.active = true;
      return { ok: true, msg: '注册成功' };
    }).catch(function (e) {
      return { ok: false, msg: (e && e.message) || '注册失败，请重试' };
    });
  }

  function cloudLogin(username, password) {
    var C = RR.Cloud;
    return C.ensureClient().then(function (sb) {
      return sb.auth.signInWithPassword({ email: emailOf(username), password: password });
    }).then(function (r) {
      if (r.error) throw new Error(mapAuthError(r.error.message));
      saveSession(username);
      C.user = r.data.user;
      C.username = username;
      C.active = true;
      return { ok: true, msg: '登录成功' };
    }).catch(function (e) {
      return { ok: false, msg: (e && e.message) || '登录失败，请重试' };
    });
  }

  // ---------------------------------------------------------------- 公开 API

  var Auth = {
    USERS_KEY: USERS_KEY,
    SESSION_KEY: SESSION_KEY,

    /** 当前登录的用户名；未登录返回 null */
    current: function () { return currentUser(); },

    /** 是否已登录 */
    loggedIn: function () { return !!currentUser(); },

    /** 当前是否运行在云端模式（能跨设备同步） */
    isCloud: function () { return cloudActive(); },

    /**
     * 用户名是否已被占用。
     * 本地模式返回 boolean；云端模式返回 Promise<boolean>（因为要查服务端）。
     */
    hasAccount: function (username) {
      username = (username || '').trim();
      if (cloudOn()) {
        return RR.Cloud.ensureClient().then(function (sb) {
          return sb.rpc('username_available', { uname: username });
        }).then(function (r) {
          return !(r && r.error) && r.data === false;
        }).catch(function () { return false; });
      }
      var users = loadUsers();
      var lower = username.toLowerCase();
      for (var u in users) { if (u.toLowerCase() === lower) return true; }
      return false;
    },

    /**
     * 注册：用户名唯一，云端要求密码 ≥6 位（Supabase 默认），本地 ≥4 位。
     * @returns Promise<{ok:boolean, msg:string}>
     */
    register: function (username, password, confirm) {
      username = (username || '').trim();
      if (!/^[A-Za-z0-9_\u4e00-\u9fa5]{2,20}$/.test(username)) {
        return Promise.resolve({ ok: false, msg: '用户名需 2-20 位（中英文/数字/下划线）' });
      }
      var minLen = cloudOn() ? 6 : 4;
      if (!password || password.length < minLen) {
        return Promise.resolve({ ok: false, msg: '密码至少 ' + minLen + ' 位' });
      }
      if (confirm !== undefined && password !== confirm) {
        return Promise.resolve({ ok: false, msg: '两次输入的密码不一致' });
      }
      if (cloudOn()) return cloudRegister(username, password);
      return localRegister(username, password);
    },

    login: function (username, password) {
      username = (username || '').trim();
      if (cloudOn() && cloudActive()) {
        // 已经登录过（比如会话刚恢复），直接放行
        if (currentUser() === username) return Promise.resolve({ ok: true, msg: '登录成功' });
      }
      if (cloudOn()) return cloudLogin(username, password);
      return localLogin(username, password);
    },

    logout: function () {
      if (RR.Cloud) RR.Cloud.signOut();
      saveSession(null);
      clearGuestMarker();
      guestStore = {};
    },

    /** 进入游客模式：不注册不登录，直接玩，但没有任何历史记录 */
    enterGuest: function () {
      saveSession(null);
      guestStore = {};
      setGuestMarker();
    },

    /** 是否处于游客模式 */
    isGuest: function () { return isGuest(); },

    /** 当前模式：'user'（已登录账号）| 'guest'（游客）| 'none'（未登录） */
    mode: function () {
      if (isGuest()) return 'guest';
      return currentUser() ? 'user' : 'none';
    },

    /** 是否可以直接进游戏（已登录 或 游客） */
    canPlay: function () { return this.mode() !== 'none'; },

    /** 当前账号下某业务 key 的实际存储名；未登录/游客时退回原名 */
    key: function (raw) {
      var u = currentUser();
      return u ? ('bear-run:acct:' + u + ':' + raw) : raw;
    },

    /**
     * 同步读存档。
     * 云端模式优先读内存缓存（启动时已由 RR.Cloud.pull 灌入），
     * 再退回 localStorage 的账号前缀 key —— 双保险，云端掉线也不丢。
     */
    get: function (raw) {
      if (isGuest()) return (raw in guestStore) ? guestStore[raw] : null;
      if (cloudActive()) {
        var v = RR.Cloud.get(raw);
        if (v != null) return v;
      }
      return getLS(Auth.key(raw));
    },

    set: function (raw, val) {
      if (isGuest()) { guestStore[raw] = String(val); return; }
      if (cloudActive()) RR.Cloud.set(raw, val);   // 内存 + 异步上云
      setLS(Auth.key(raw), val);                   // 本地落盘始终保留
    },

    remove: function (raw) {
      if (isGuest()) { delete guestStore[raw]; return; }
      if (cloudActive()) RR.Cloud.remove(raw);
      removeLS(Auth.key(raw));
    }
  };

  RR.Auth = Auth;
})(window);
