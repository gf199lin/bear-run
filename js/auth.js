/**
 * 本地多账号（伪账号）系统 —— 纯前端，适配 GitHub Pages 静态托管。
 *
 * 目标：进入游戏先登录/注册，不同账号彼此独立存档（最高分、连击、历史等），
 *       而不是"这个浏览器就默认一个身份"。
 *
 * 边界（务必让产品/使用的人知道）：
 *  - 账号与存档都只存在"当前浏览器"的 localStorage，没有后端，
 *    换设备 / 清缓存 / 换浏览器 不会同步，也防不了懂技术的人直接改本地数据。
 *  - 密码用 SHA-256+盐 哈希存储（crypto.subtle）：
 *        https / localhost 下是真哈希；
 *        file:// 或非安全上下文下降级为简易哈希（仅演示）。
 *  - 这只用于"本地多账号演示"，不具备真正的账号安全 / 异地登录能力。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};

  var USERS_KEY = 'bear-run:users:v1';
  var SESSION_KEY = 'bear-run:session:v1';
  var MIGRATED_KEY = 'bear-run:migrated:v1';

  var usersCache = null;

  function getLS(key) { try { return global.localStorage.getItem(key); } catch (e) { return null; } }
  function setLS(key, v) { try { global.localStorage.setItem(key, v); } catch (e) { /* 忽略 */ } }
  function removeLS(key) { try { global.localStorage.removeItem(key); } catch (e) { /* 忽略 */ } }

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
  function saveSession(u) { if (u) setLS(SESSION_KEY, u); else removeLS(SESSION_KEY); }

  // crypto.subtle 不可用时的降级（仅演示用）
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

  var Auth = {
    USERS_KEY: USERS_KEY,
    SESSION_KEY: SESSION_KEY,

    /** 当前登录的用户名；未登录返回 null */
    current: function () { return currentUser(); },

    /** 是否已登录 */
    loggedIn: function () { return !!currentUser(); },

    hasAccount: function (username) {
      var users = loadUsers();
      var lower = (username || '').toLowerCase();
      for (var u in users) { if (u.toLowerCase() === lower) return true; }
      return false;
    },

    /**
     * 注册：用户名唯一，密码至少 4 位。
     * @returns Promise<{ok:boolean, msg:string}>
     */
    register: function (username, password, confirm) {
      username = (username || '').trim();
      if (!/^[A-Za-z0-9_\u4e00-\u9fa5]{2,20}$/.test(username)) {
        return Promise.resolve({ ok: false, msg: '用户名需 2-20 位（中英文/数字/下划线）' });
      }
      if (!password || password.length < 4) {
        return Promise.resolve({ ok: false, msg: '密码至少 4 位' });
      }
      if (confirm !== undefined && password !== confirm) {
        return Promise.resolve({ ok: false, msg: '两次输入的密码不一致' });
      }
      var users = loadUsers();
      var lower = username.toLowerCase();
      for (var u in users) { if (u.toLowerCase() === lower) { return Promise.resolve({ ok: false, msg: '该用户名已被注册' }); } }
      var salt = randSalt();
      return sha256(salt + '::' + password).then(function (hash) {
        users[username] = { s: salt, h: hash, created: Date.now() };
        saveUsers();
        if (Object.keys(users).length === 1) migrateLegacyOnce(username);
        saveSession(username);
        return { ok: true, msg: '注册成功' };
      });
    },

    login: function (username, password) {
      username = (username || '').trim();
      var users = loadUsers();
      var user = null, key = null;
      for (var u in users) { if (u.toLowerCase() === username.toLowerCase()) { user = users[u]; key = u; break; } }
      if (!user || !user.s) return Promise.resolve({ ok: false, msg: '用户名或密码错误' });
      return sha256(user.s + '::' + password).then(function (hash) {
        if (hash !== user.h) return { ok: false, msg: '用户名或密码错误' };
        saveSession(key);
        return { ok: true, msg: '登录成功' };
      });
    },

    logout: function () { saveSession(null); },

    /** 当前账号下某业务 key 的实际存储名；未登录时退回原名 */
    key: function (raw) {
      var u = currentUser();
      return u ? ('bear-run:acct:' + u + ':' + raw) : raw;
    },

    get: function (raw) { return getLS(Auth.key(raw)); },
    set: function (raw, val) { setLS(Auth.key(raw), val); },
    remove: function (raw) { removeLS(Auth.key(raw)); }
  };

  RR.Auth = Auth;
})(window);
