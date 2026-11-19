/**
 * 云端数据层（Supabase）—— 纯前端，适配静态托管（GitHub Pages）。
 *
 * 设计要点（为什么这么做）：
 *
 * 1) 同步 / 异步的桥接
 *    localStorage 是同步的，Supabase 是异步的，而游戏的存档读取（_loadRecords）
 *    是同步调用。所以这里不把业务代码改成 async，而是做三层：
 *
 *      业务代码（同步读写，一行不用改）
 *            ↓ RR.Auth.get / .set
 *      内存缓存（Map，同步，命中即返回）
 *            ↓
 *      localStorage（同步落盘，离线兜底、刷新不丢）
 *            ↓ 异步、可失败、可排队重试
 *      Supabase（真源，跨设备）
 *
 * 2) 启动时不阻塞
 *    init() 的同步部分先把 Supabase 存在 localStorage 的会话解析出来，
 *    回填 bear-run:session:v1 —— 这样后面同步执行的 RR.Auth.canPlay()
 *    立刻就能判断对，页面不会先闪一下登录页。
 *    真正的网络请求在后台跑，回来后再把云端数据灌进内存与 localStorage。
 *
 * 3) 离线 / 双击即玩
 *    file:// 协议下（直接双击 index.html）不加载 SDK、不联网，
 *    整个模块静默降级，行为和现在完全一致。
 *    未配置 SUPABASE_URL 时同理。
 *
 * 4) 安全
 *    这里只使用 anon key（公开、可进 git）。真正的权限边界是数据库的 RLS，
 *    见 outputs/supabase/schema.sql。service_role key 永远不进前端。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};
  var CFG = RR.CFG || (RR.CFG = {});

  var SESSION_KEY   = 'bear-run:session:v1';
  var OUTBOX_KEY    = 'bear-run:outbox:v1';
  var SDK_URL       = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2';
  var SDK_TIMEOUT   = 8000;

  function trim(v) { return (v == null ? '' : String(v)).trim(); }
  function isHttp() { return /^https?:$/.test(global.location.protocol); }

  // 配置优先级：window.RR_CLOUD_CONFIG（部署时只改这一处）> RR.CFG（模块化开发时用）
  function envCfg() { return global.RR_CLOUD_CONFIG || {}; }
  function cfgUrl() { return trim(envCfg().url || CFG.SUPABASE_URL); }
  function cfgKey() { return trim(envCfg().key || CFG.SUPABASE_ANON_KEY); }

  var Cloud = {
    /** 当前环境是否可能启用云端（http/https） */
    enabled: false,
    /** 是否填了 SUPABASE_URL / SUPABASE_ANON_KEY */
    configured: false,
    /** 是否已经有有效登录用户（决定 RR.Auth 走云端还是本地） */
    active: false,
    user: null,
    username: null,
    sb: null,
    readyPromise: null,

    _mem: Object.create(null),   // raw key -> string
    _pending: [],                // 待同步的偏好写入
    _flushing: false,
    _sdkPromise: null,

    // ---------------------------------------------------------------- 初始化

    init: function () {
      this.enabled = isHttp();
      this.configured =
        /^https?:\/\//i.test(cfgUrl()) &&
        cfgKey().length > 20;

      if (!this.enabled || !this.configured) return Promise.resolve(false);

      // 同步阶段：先把登录态救回来，保证同步调用方判断正确
      this._restoreSessionSync();

      var self = this;
      this.readyPromise = this._bootstrap().catch(function () { return false; });
      return this.readyPromise;
    },

    /**
     * 从 Supabase 自己写在 localStorage 的会话里，同步解析出用户名。
     *
     * supabase-js v2 默认把会话存在 key 形如 `sb-<project-ref>-auth-token` 的位置；
     * token 很大时会分片成 `...-auth-token.0`、`...-auth-token.1`。
     * 这里按 key 排序拼接，再解析 JSON。
     *
     * 解析失败一律返回，不抛错 —— 最坏情况就是退回"未登录"，不影响本地模式。
     */
    _restoreSessionSync: function () {
      var sess = null;
      try {
        var parts = [];
        var ls = global.localStorage;
        for (var i = 0; i < ls.length; i++) {
          var k = ls.key(i);
          if (k && k.indexOf('sb-') === 0 && k.indexOf('-auth-token') > 0) {
            parts.push({ k: k, v: ls.getItem(k) });
          }
        }
        if (!parts.length) return;

        parts.sort(function (a, b) { return a.k < b.k ? -1 : (a.k > b.k ? 1 : 0); });
        var raw = parts.map(function (p) { return p.v || ''; }).join('');
        var d = JSON.parse(raw);
        sess = (d && d.user) ? d : ((d && d.currentSession) ? d.currentSession : null);
      } catch (e) {
        sess = null;
      }
      if (!sess || !sess.user) return;

      var name = this._usernameOf(sess.user);
      if (!name) return;
      this.username = name;
      try { global.localStorage.setItem(SESSION_KEY, name); } catch (e) { /* 忽略 */ }
    },

    _usernameOf: function (u) {
      if (!u) return null;
      var meta = u.user_metadata || {};
      if (meta.username) return String(meta.username);
      if (u.email) return String(u.email).split('@')[0];
      return null;
    },

    /** 异步：建客户端 -> 校验会话 -> 拉数据 -> 补传离线队列 */
    _bootstrap: function () {
      var self = this;
      return this.ensureClient().then(function (sb) {
        return sb.auth.getSession();
      }).then(function (res) {
        var sess = res && res.data ? res.data.session : null;
        if (!sess || !sess.user) {
          self._clearSession();
          return false;
        }
        self.user = sess.user;
        self.username = self._usernameOf(sess.user) || self.username;
        self.active = true;
        if (self.username) {
          try { global.localStorage.setItem(SESSION_KEY, self.username); } catch (e) { /* 忽略 */ }
        }
        return self.pull();
      }).then(function () {
        return self.flushOutbox();
      }).then(function () {
        try {
          document.dispatchEvent(new global.CustomEvent('rr:cloud-ready'));
        } catch (e) { /* 老浏览器没有 CustomEvent 构造器 */ }
        return true;
      }).catch(function () {
        // 网络失败 / SDK 加载失败：保持 active=false，业务自动退回本地存档
        return false;
      });
    },

    // ---------------------------------------------------------------- 客户端

    _url: function () { return cfgUrl(); },
    _key: function () { return cfgKey(); },

    /** 返回 Promise<SupabaseClient>；未配置时明确 reject，便于 UI 提示 */
    ensureClient: function () {
      var self = this;
      if (this.sb) return Promise.resolve(this.sb);
      if (!this.enabled || !this.configured) {
        return Promise.reject(new Error('本地离线版没有云端账号服务'));
      }
      if (!this._sdkPromise) {
        this._sdkPromise = this._loadSDK();
        return this._sdkPromise.then(function (ok) {
          if (!ok) { self._sdkPromise = null; throw new Error('云端服务加载失败，请检查网络'); }
          self.sb = global.supabase.createClient(self._url(), self._key(), {
            auth: {
              persistSession: true,
              autoRefreshToken: true,
              detectSessionInUrl: false
            }
          });
          return self.sb;
        });
      }
      return this._sdkPromise.then(function (ok) {
        if (!ok) throw new Error('云端服务加载失败，请检查网络');
        if (!self.sb) {
          self.sb = global.supabase.createClient(self._url(), self._key(), {
            auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: false }
          });
        }
        return self.sb;
      });
    },

    /** 动态注入 SDK：内联单文件版不预先引 CDN，避免破坏"双击即玩" */
    _loadSDK: function () {
      if (global.supabase && global.supabase.createClient) return Promise.resolve(true);
      return new Promise(function (resolve) {
        var done = false;
        function finish(ok) { if (!done) { done = true; resolve(ok); } }

        var s = document.createElement('script');
        s.src = SDK_URL;
        s.async = true;
        s.onload = function () { finish(true); };
        s.onerror = function () { finish(false); };
        document.head.appendChild(s);

        setTimeout(function () { finish(false); }, SDK_TIMEOUT);
      });
    },

    // ---------------------------------------------------------------- 拉取

    /**
     * 把云端数据灌进内存缓存 + localStorage（账号前缀 key），
     * 让还没切换到云端读取的旧代码路径也能看到同一份数据。
     */
    pull: function () {
      var self = this;
      if (!this.sb || !this.user) return Promise.resolve();

      var uid = this.user.id;
      var uname = this.username || '';
      var historyMax = (CFG.RECORDS && CFG.RECORDS.historyMax) || 5;

      function cacheKey(raw) { return 'bear-run:acct:' + uname + ':' + raw; }

      return Promise.all([
        this.sb.from('user_settings')
          .select('input_source,character')
          .eq('user_id', uid).maybeSingle(),
        this.sb.from('game_records')
          .select('score,distance,coins,combo,near_miss,won,created_at')
          .eq('user_id', uid)
          .order('created_at', { ascending: false })
          .limit(historyMax),
        this.sb.from('player_stats').select('*').eq('user_id', uid).maybeSingle()
      ]).then(function (rows) {
        var st = rows[0] && rows[0].data;
        var rs = (rows[1] && rows[1].data) || [];
        var ps = rows[2] && rows[2].data;

        if (st) {
          if (st.input_source != null) self._put(CFG.STORAGE_KEY_INPUT, st.input_source, cacheKey);
          if (st.character != null)    self._put(CFG.STORAGE_KEY_CHAR,  st.character,  cacheKey);
        }

        if (ps) {
          var rec = {
            best: Number(ps.best) || 0,
            bestDistance: Number(ps.best_distance) || 0,
            bestCombo: Number(ps.best_combo) || 0,
            bestNearMiss: Number(ps.best_near_miss) || 0,
            plays: Number(ps.plays) || 0,
            history: rs.map(function (x) {
              return {
                score: x.score,
                distance: x.distance,
                coins: x.coins,
                combo: x.combo,
                nearMiss: x.near_miss,
                won: !!x.won,
                at: Date.parse(x.created_at) || 0
              };
            })
          };
          self._put(CFG.STORAGE_KEY_RECORDS, JSON.stringify(rec), cacheKey);
          self._put(CFG.STORAGE_KEY, String(rec.best), cacheKey);
        }

        // 云端没有记录但本地有未上传的旧存档：不要覆盖掉本地最高分
        try {
          var lk = cacheKey(CFG.STORAGE_KEY_RECORDS);
          var localRaw = global.localStorage.getItem(lk);
          if (localRaw && ps && !rec.plays) {
            global.localStorage.setItem(lk, localRaw);
            self._mem[CFG.STORAGE_KEY_RECORDS] = localRaw;
          }
        } catch (e) { /* 忽略 */ }
      });
    },

    _put: function (raw, val, cacheKey) {
      var s = String(val);
      this._mem[raw] = s;
      try { global.localStorage.setItem(cacheKey(raw), s); } catch (e) { /* 隐私模式 */ }
    },

    // ---------------------------------------------------------------- 同步读

    /** 同步读内存缓存；未命中返回 null，由调用方退回 localStorage */
    get: function (raw) {
      return (raw in this._mem) ? this._mem[raw] : null;
    },

    /** 同步写内存 + 本地，并把偏好类字段异步推给云端 */
    set: function (raw, val) {
      var s = String(val);
      this._mem[raw] = s;

      if (CFG.STORAGE_KEY_INPUT && raw === CFG.STORAGE_KEY_INPUT) this._queueSettings(raw, s);
      else if (CFG.STORAGE_KEY_CHAR && raw === CFG.STORAGE_KEY_CHAR) this._queueSettings(raw, s);
    },

    remove: function (raw) {
      delete this._mem[raw];
    },

    // ---------------------------------------------------------------- 偏好同步

    _queueSettings: function (raw, val) {
      for (var i = 0; i < this._pending.length; i++) {
        if (this._pending[i].raw === raw) { this._pending[i].val = val; return this._flushSettings(); }
      }
      this._pending.push({ raw: raw, val: val });
      this._flushSettings();
    },

    _flushSettings: function () {
      var self = this;
      if (!this.active || !this.sb || !this.user) return;
      if (this._flushing || !this._pending.length) return;

      this._flushing = true;
      var batch = this._pending.splice(0, this._pending.length);
      var patch = { user_id: this.user.id, updated_at: new Date().toISOString() };
      for (var i = 0; i < batch.length; i++) {
        if (batch[i].raw === CFG.STORAGE_KEY_INPUT) patch.input_source = batch[i].val;
        if (batch[i].raw === CFG.STORAGE_KEY_CHAR)  patch.character    = batch[i].val;
      }

      this.sb.from('user_settings').upsert(patch, { onConflict: 'user_id' }).then(function (res) {
        self._flushing = false;
        if (res && res.error) {
          // 失败不丢：放回队列，等下次写入或下次启动重试
          self._pending = batch.concat(self._pending);
        } else if (self._pending.length) {
          self._flushSettings();
        }
      }).catch(function () {
        self._flushing = false;
        self._pending = batch.concat(self._pending);
      });
    },

    // ---------------------------------------------------------------- 一局记录

    /**
     * 结算时调用：一局一条 append-only 记录（P2 阶段在 Game 里接）。
     * 失败进本地 outbox，下次启动补传 —— 保证"打过的一定记得住"。
     */
    pushRecord: function (rec) {
      var self = this;
      if (!this.active || !this.sb || !this.user || !rec) return Promise.resolve(false);

      var row = {
        user_id: this.user.id,
        score: Math.max(0, Math.floor(Number(rec.score) || 0)),
        distance: Math.max(0, Math.floor(Number(rec.distance) || 0)),
        coins: Math.max(0, Math.floor(Number(rec.coins) || 0)),
        combo: Math.max(0, Math.floor(Number(rec.combo) || 0)),
        near_miss: Math.max(0, Math.floor(Number(rec.nearMiss) || 0)),
        won: !!rec.won
      };
      if (rec.durationMs) row.duration_ms = Math.floor(Number(rec.durationMs) || 0);

      return this.sb.from('game_records').insert(row).then(function (res) {
        if (res && res.error) { self._pushOutbox(rec); return false; }
        return true;
      }).catch(function () {
        self._pushOutbox(rec);
        return false;
      });
    },

    _pushOutbox: function (rec) {
      try {
        var q = JSON.parse(global.localStorage.getItem(OUTBOX_KEY) || '[]');
        if (!Array.isArray(q)) q = [];
        q.push(rec);
        if (q.length > 200) q = q.slice(-200);
        global.localStorage.setItem(OUTBOX_KEY, JSON.stringify(q));
      } catch (e) { /* 忽略 */ }
    },

    /** 启动时补传上次离线打的局 */
    flushOutbox: function () {
      var self = this;
      if (!this.active || !this.sb || !this.user) return Promise.resolve();

      var q = [];
      try { q = JSON.parse(global.localStorage.getItem(OUTBOX_KEY) || '[]'); } catch (e) { q = []; }
      if (!Array.isArray(q) || !q.length) return Promise.resolve();

      var left = [];
      var chain = Promise.resolve();

      q.forEach(function (rec) {
        chain = chain.then(function () {
          var row = {
            user_id: self.user.id,
            score: Math.max(0, Math.floor(Number(rec.score) || 0)),
            distance: Math.max(0, Math.floor(Number(rec.distance) || 0)),
            coins: Math.max(0, Math.floor(Number(rec.coins) || 0)),
            combo: Math.max(0, Math.floor(Number(rec.combo) || 0)),
            near_miss: Math.max(0, Math.floor(Number(rec.nearMiss) || 0)),
            won: !!rec.won
          };
          return self.sb.from('game_records').insert(row).then(function (res) {
            if (res && res.error) left.push(rec);
          }).catch(function () { left.push(rec); });
        });
      });

      return chain.then(function () {
        try {
          if (left.length) global.localStorage.setItem(OUTBOX_KEY, JSON.stringify(left));
          else global.localStorage.removeItem(OUTBOX_KEY);
        } catch (e) { /* 忽略 */ }
      });
    },

    // ---------------------------------------------------------------- 会话

    signOut: function () {
      var self = this;
      this._clearSession();
      if (!this.sb) return Promise.resolve();
      return this.sb.auth.signOut().catch(function () { /* 离线也照样本地登出 */ });
    },

    _clearSession: function () {
      this.user = null;
      this.username = null;
      this.active = false;
      this._mem = Object.create(null);
      this._pending = [];
      try { global.localStorage.removeItem(SESSION_KEY); } catch (e) { /* 忽略 */ }
    },

    /** 给调试用的状态快照（不含 token） */
    status: function () {
      return {
        enabled: this.enabled,
        configured: this.configured,
        active: this.active,
        username: this.username,
        memKeys: Object.keys(this._mem).length,
        pending: this._pending.length
      };
    }
  };

  RR.Cloud = Cloud;

  // 自动启动：内联单文件版和 <script src> 版都能直接用。
  // file:// 双击打开、或没填 SUPABASE_URL 时，init() 内部会立即降级为纯本地，不联网。
  if (!Cloud._booted) {
    Cloud._booted = true;
    Cloud.init();
  }
})(window);
