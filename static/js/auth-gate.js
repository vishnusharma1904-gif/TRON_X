/**
 * TRON-X Access Gate
 * ──────────────────
 * TRON-X has no per-user accounts — it's a single personal assistant.
 * When AUTH_ENABLED=true on the server, every /api/* call must carry an
 * X-API-Key header or the server rejects it with 401.
 *
 * This script asks the visitor for that key once, remembers it in this
 * browser's localStorage, and silently attaches it to every same-origin
 * /api/* fetch() call made by the rest of the app (chat.js, panels.js, etc).
 *
 * If the server rejects the stored key (401), the visitor is asked again.
 * This means anyone opening the public ngrok URL without the key cannot
 * read sessions, chat history, or memory.
 *
 * Must be loaded FIRST, before any other script that calls fetch().
 */
(function () {
  "use strict";

  var STORAGE_KEY = "tronx_access_key";

  function getKey() {
    try {
      return localStorage.getItem(STORAGE_KEY) || "";
    } catch (e) {
      return "";
    }
  }

  function setKey(key) {
    try {
      if (key) localStorage.setItem(STORAGE_KEY, key);
      else localStorage.removeItem(STORAGE_KEY);
    } catch (e) {
      /* ignore (private mode etc.) */
    }
  }

  function askForKey(message) {
    var key = window.prompt(message);
    if (key && key.trim()) {
      setKey(key.trim());
      return key.trim();
    }
    return "";
  }

  // Ask immediately on first visit so the rest of the app boots with a key.
  if (!getKey()) {
    askForKey("TRON-X access key required:");
  }

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
  var _retried = false;

  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || "";

    if (!isApiUrl(url)) {
      return _fetch(input, init);
    }

    init = init || {};
    var headers = new Headers(init.headers || {});
    var key = getKey();
    if (key) headers.set("X-API-Key", key);
    var newInit = Object.assign({}, init, { headers: headers });

    return _fetch(input, newInit).then(function (response) {
      if (response.status === 401) {
        setKey("");
        var retryKey = askForKey("Access key invalid or missing. Enter the TRON-X access key:");
        if (retryKey) {
          var retryHeaders = new Headers(init.headers || {});
          retryHeaders.set("X-API-Key", retryKey);
          return _fetch(input, Object.assign({}, init, { headers: retryHeaders }));
        }
      }
      return response;
    });
  };
})();
