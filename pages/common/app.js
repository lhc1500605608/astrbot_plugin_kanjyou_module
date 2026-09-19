(function () {
  'use strict';

  function getSdk() { return window.AstrBotPluginPage || null; }

  var _i18nDict = {};
  var _i18nReady = false;

  function _loadI18n() {
    if (_i18nReady) return;
    var sdk = getSdk();
    var locale = (sdk && sdk.locale) || (sdk && sdk.getLocale && sdk.getLocale()) || 'zh-CN';
    if (sdk && typeof sdk.t === 'function') {
      _i18nReady = true;
      return;
    }
    var base = '../../.astrbot-plugin/i18n/';
    var url = base + locale + '.json';
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('GET', url, false);
      xhr.send(null);
      if (xhr.status === 200 || xhr.status === 0) {
        _i18nDict = JSON.parse(xhr.responseText);
      }
    } catch (e) {
      console.warn('[kanjyou] i18n load failed:', e);
    }
    _i18nReady = true;
  }

  function _resolveKey(key) {
    var parts = key.split('.');
    var obj = _i18nDict;
    for (var i = 0; i < parts.length; i++) {
      if (obj && typeof obj === 'object' && parts[i] in obj) {
        obj = obj[parts[i]];
      } else {
        return undefined;
      }
    }
    return obj;
  }

  function _interpolate(str, params) {
    if (!params || typeof str !== 'string') return str;
    return str.replace(/\{(\w+)\}/g, function (_, k) {
      return params[k] != null ? params[k] : '{' + k + '}';
    });
  }

  function t(key, params) {
    _loadI18n();
    var sdk = getSdk();
    if (sdk && typeof sdk.t === 'function') {
      var val = sdk.t(key, params);
      if (val !== undefined && val !== key) return _interpolate(val, params);
    }
    var local = _resolveKey(key);
    if (local !== undefined) return _interpolate(local, params);
    return key;
  }

  function applyI18n(root) {
    var el = root || document;
    var nodes = el.querySelectorAll('[data-i18n]');
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var key = node.getAttribute('data-i18n');
      if (!key) continue;
      var val = t(key);
      if (val !== key) node.textContent = val;
    }
    var placeholders = el.querySelectorAll('[data-i18n-placeholder]');
    for (var j = 0; j < placeholders.length; j++) {
      var pNode = placeholders[j];
      var pKey = pNode.getAttribute('data-i18n-placeholder');
      if (!pKey) continue;
      var pVal = t(pKey);
      if (pVal !== pKey) pNode.placeholder = pVal;
    }
    var titles = el.querySelectorAll('[data-i18n-title]');
    for (var k = 0; k < titles.length; k++) {
      var tNode = titles[k];
      var tKey = tNode.getAttribute('data-i18n-title');
      if (!tKey) continue;
      var tVal = t(tKey);
      if (tVal !== tKey) tNode.title = tVal;
    }
  }

  function getTheme() {
    var attr = document.documentElement.getAttribute('data-theme');
    if (attr === 'light' || attr === 'dark') return attr;
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
      return 'light';
    }
    return 'dark';
  }

  function onThemeChange(cb) {
    if (window.matchMedia) {
      window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', function () {
        if (!document.documentElement.hasAttribute('data-theme')) {
          cb(getTheme());
        }
      });
    }
    var observer = new MutationObserver(function () {
      cb(getTheme());
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  }

  var KANJYOU = {
    get sdk() { return getSdk(); },
    t: t,
    applyI18n: applyI18n,
    getTheme: getTheme,
    onThemeChange: onThemeChange,

    async apiGet(path, params) {
      var sdk = getSdk();
      if (!sdk) return null;
      var ep = path.charAt(0) === '/' ? path.slice(1) : path;
      try {
        return await sdk.apiGet(ep, params || {});
      } catch (e) {
        console.error('[kanjyou] apiGet error:', ep, e);
        return null;
      }
    },

    async apiPost(path, body) {
      var sdk = getSdk();
      if (!sdk) return null;
      var ep = path.charAt(0) === '/' ? path.slice(1) : path;
      try {
        return await sdk.apiPost(ep, body || {});
      } catch (e) {
        console.error('[kanjyou] apiPost error:', ep, e);
        return null;
      }
    },

    subscribeSSE(path, onEvent, onStatus) {
      var sdk = getSdk();
      if (!sdk) return { close() {} };
      var ep = path.charAt(0) === '/' ? path.slice(1) : path;
      var closed = false;
      var handlers = {
        onMessage: function(info) {
          if (!closed) onEvent(info);
        },
        onOpen: function() {
          if (onStatus) onStatus('connected');
        },
        onError: function() {
          if (onStatus) onStatus('disconnected');
        }
      };
      try {
        sdk.subscribeSSE(ep, handlers);
      } catch (e) {
        console.error('[kanjyou] subscribeSSE error:', ep, e);
      }
      return {
        close() { closed = true; },
      };
    },

    connectSSEWithReconnect(path, onEvent, onStatus, opts) {
      var self = this;
      opts = opts || {};
      var maxDelay = opts.maxDelay || 30000;
      var baseDelay = opts.baseDelay || 1000;
      var heartbeatTimeout = opts.heartbeatTimeout || 60000;
      var handle = null;
      var delay = baseDelay;
      var timer = null;
      var heartbeatTimer = null;
      var stopped = false;

      function resetHeartbeat() {
        if (heartbeatTimer) clearTimeout(heartbeatTimer);
        heartbeatTimer = setTimeout(function() {
          if (stopped) return;
          scheduleReconnect();
        }, heartbeatTimeout);
      }

      function connect() {
        if (stopped) return;
        if (handle) { try { handle.close(); } catch(e) {} }
        if (onStatus) onStatus('connecting');
        handle = self.subscribeSSE(path, function(data) {
          delay = baseDelay;
          resetHeartbeat();
          onEvent(data);
        }, onStatus);
        resetHeartbeat();
      }

      function scheduleReconnect() {
        if (stopped) return;
        if (handle) { try { handle.close(); } catch(e) {} }
        if (heartbeatTimer) clearTimeout(heartbeatTimer);
        if (onStatus) onStatus('disconnected');
        timer = setTimeout(function() {
          delay = Math.min(delay * 2, maxDelay);
          connect();
        }, delay);
      }

      connect();

      return {
        close() {
          stopped = true;
          if (timer) clearTimeout(timer);
          if (heartbeatTimer) clearTimeout(heartbeatTimer);
          if (handle) { try { handle.close(); } catch(e) {} }
        },
        reconnect() {
          if (timer) clearTimeout(timer);
          delay = baseDelay;
          connect();
        },
      };
    },

    formatDuration(seconds) {
      var s = Math.max(0, Math.floor(seconds));
      if (s < 60) return s + 's';
      if (s < 3600) return Math.floor(s / 60) + 'm' + (s % 60 ? ' ' + (s % 60) + 's' : '');
      var h = Math.floor(s / 3600);
      var m = Math.floor((s % 3600) / 60);
      return h + 'h' + (m ? ' ' + m + 'm' : '');
    },

    formatTime(ts) {
      if (!ts || ts === '-' || ts === '0' || ts === 0) return '-';
      var d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
      if (isNaN(d.getTime())) return '-';
      var pad = function(n) { return String(n).padStart(2, '0'); };
      return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    },

    formatDateTime(ts) {
      if (!ts || ts === '-' || ts === '0' || ts === 0) return '-';
      var d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
      if (isNaN(d.getTime())) return '-';
      var pad = function(n) { return String(n).padStart(2, '0'); };
      return (
        d.getFullYear() + '-' +
        pad(d.getMonth() + 1) + '-' +
        pad(d.getDate()) + ' ' +
        pad(d.getHours()) + ':' +
        pad(d.getMinutes()) + ':' +
        pad(d.getSeconds())
      );
    },

    sessionLabel(key) {
      if (!key) return '-';
      if (key.startsWith('private:')) return 'P:' + key.slice(8);
      if (key.startsWith('group:')) return 'G:' + key.slice(6);
      return key;
    },

    sessionType(key) {
      if (!key) return '';
      if (key.startsWith('private:')) return 'private';
      if (key.startsWith('group:')) return 'group';
      return '';
    },

    moodColor(value) {
      var v = Number(value) || 0;
      if (v >= 70) return 'var(--accent2)';
      if (v >= 35) return 'var(--warn)';
      return 'var(--danger)';
    },

    moodPercent(value) {
      return Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
    },

    escapeHtml(text) {
      var el = document.createElement('span');
      el.textContent = text || '';
      return el.innerHTML;
    },

    showToast(message, type) {
      var container = document.querySelector('.toast-container');
      if (!container) {
        container = document.createElement('div');
        container.className = 'toast-container';
        document.body.appendChild(container);
      }
      var toast = document.createElement('div');
      toast.className = 'toast' + (type === 'ok' ? ' toast-ok' : type === 'err' ? ' toast-err' : '');
      toast.textContent = message;
      container.appendChild(toast);
      setTimeout(function() {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity .2s';
        setTimeout(function() { toast.remove(); }, 200);
      }, 3000);
    },

    now() {
      return Math.floor(Date.now() / 1000);
    },
  };

  window.KANJYOU = KANJYOU;
})();