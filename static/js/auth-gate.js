/**
 * TRON-X Identity Layer
 * ──────────────────────
 * TRON-X has no login system. Every browser gets its own random, anonymous
 * `user_id` the first time it visits, stored in localStorage. This id is
 * sent as the `X-User-Id` header on every same-origin /api/* fetch() call,
 * and the server uses it to scope chat sessions/history — so different
 * visitors never see each other's conversations or memory.
 *
 * Admin access (Vishnu): open the app once with `?admin_key=YOUR_KEY` in
 * the URL (the key configured as API_KEYS in .env on the server). This
 * script stores it in localStorage and attaches it as `X-API-Key` on every
 * request, which the server treats as full cross-user admin access
 * (see /api/admin/* and the user_id filter on /api/chat/sessions).
 * The key is stripped from the URL bar immediately so it isn't shared by
 * accident (e.g. copy/pasting the link).
 *
 * Must be loaded FIRST, before any other script that calls fetch().
 */
(function () {
  "use strict";

  var USER_KEY  = "tronx_user_id";
  var ADMIN_KEY = "tronx_admin_key";

  function uuid() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    // Fallback RFC4122-ish v4 UUID
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      var v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function getOrCreateUserId() {
    try {
      var id = localStorage.getItem(USER_KEY);
      if (!id) {
        id = uuid();
        localStorage.setItem(USER_KEY, id);
      }
      return id;
    } catch (e) {
      // localStorage unavailable (private mode) — use a per-tab id
      if (!window.__tronxUserId) window.__tronxUserId = uuid();
      return window.__tronxUserId;
    }
  }

  function getAdminKey() {
    try {
      return localStorage.getItem(ADMIN_KEY) || "";
    } catch (e) {
      return "";
    }
  }

  // Pick up ?admin_key=... from the URL once, persist it, then clean the URL.
  (function captureAdminKey() {
    try {
      var params = new URLSearchParams(location.search);
      var k = params.get("admin_key");
      if (k && k.trim()) {
        localStorage.setItem(ADMIN_KEY, k.trim());
        params.delete("admin_key");
        var qs = params.toString();
        var newUrl = location.pathname + (qs ? "?" + qs : "") + location.hash;
        history.replaceState({}, "", newUrl);
      }
    } catch (e) { /* ignore */ }
  })();

  var userId = getOrCreateUserId();

  function isApiUrl(url) {
    if (!url) return false;
    if (url.indexOf("/api/") === 0) return true; // relative "/api/..."
    try {
      var u = new URL(url, location.href);
      return u.origin === location.origin && u.pathname.indexOf("/api/") === 0;
    } catch (e) {
      return false;
    }
  }

  var _fetch = window.fetch.bind(window);

  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || "";

    if (!isApiUrl(url)) {
      return _fetch(input, init);
    }

    init = init || {};
    var headers = new Headers(init.headers || {});
    headers.set("X-User-Id", userId);
    var adminKey = getAdminKey();
    if (adminKey) headers.set("X-API-Key", adminKey);
    var newInit = Object.assign({}, init, { headers: headers });

    return _fetch(input, newInit);
  };

  // Expose for other scripts (e.g. proactive.js SSE, admin pages)
  window.TRONX_USER_ID = userId;
  window.TRONX_ADMIN_KEY = getAdminKey();
})();
