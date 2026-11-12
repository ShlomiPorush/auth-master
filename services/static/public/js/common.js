/* Request helpers shared by every page: the admin API with its CSRF header,
   the base path the server runs under, the CSRF value kept for this tab and the
   validated return path after signing in. */
(function (w) {
  var B = w.__BASE_PATH__ || "";

  // opts: method, body (sent as JSON), headers, csrf, signal
  w.adminApi = async function (path, opts) {
    opts = opts || {};
    var headers = new Headers();
    if (opts.headers) {
      for (var k in opts.headers) {
        if (Object.prototype.hasOwnProperty.call(opts.headers, k)) headers.set(k, opts.headers[k]);
      }
    }
    if (opts.csrf) headers.set("X-CSRF-Token", opts.csrf);
    if (opts.body !== undefined) headers.set("Content-Type", "application/json");
    return fetch(B + path, {
      method: opts.method || "GET",
      headers: headers,
      credentials: "include",
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,
    });
  };

  // The same request, its body read as JSON under one timer, so a request that
  // hangs (headers or body) gives up. Reads default to 15 seconds; writes have no
  // timer, since a write cut short may still have been applied.
  // Resolves { ok, status, data, error } and never rejects; error is "timeout"
  // or "network" when no answer arrived.
  w.adminJson = function (path, opts) {
    opts = opts || {};
    var method = opts.method || "GET";
    var ms = opts.timeout !== undefined ? opts.timeout : (method === "GET" ? 15000 : 0);
    var ctrl = typeof AbortController !== "undefined" && ms ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, ms) : null;
    var sent = {};
    for (var k in opts) { if (Object.prototype.hasOwnProperty.call(opts, k)) sent[k] = opts[k]; }
    if (ctrl) sent.signal = ctrl.signal;
    function done(result) { if (timer) clearTimeout(timer); return result; }
    return w.adminApi(path, sent).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        var isJson = true;
        if (text) {
          try { data = JSON.parse(text); } catch (e) { isJson = false; }
        }
        return done({ ok: res.ok, status: res.status, data: data, json: isJson, error: "" });
      });
    }).catch(function (err) {
      return done({ ok: false, status: 0, data: null, json: false, error: err && err.name === "AbortError" ? "timeout" : "network" });
    });
  };

  w.basePath = function () { return B; };

  // A return path for after signing in, relative to the base path and always a
  // page under /admin/ of this site; anything else gives "". Decoded first, so an
  // encoded control character, backslash or second slash cannot slip through.
  w.cleanNext = function (value, base, origin) {
    if (typeof value !== "string" || !value || value.length > 2048) return "";
    var seen = value;
    for (var i = 0; i < 4; i++) {
      if (/[\u0000-\u001f\u007f\\]/.test(seen)) return "";
      var next;
      try { next = decodeURIComponent(seen); } catch (e) { return ""; }
      if (next === seen) break;
      seen = next;
    }
    if (/[\u0000-\u001f\u007f\\]/.test(seen)) return "";
    if (value.charAt(0) !== "/" || value.charAt(1) === "/" || seen.charAt(1) === "/") return "";
    var url;
    try { url = new URL((base || "") + value, origin); } catch (e) { return ""; }
    if (url.origin !== origin) return "";
    var root = (base || "") + "/admin/";
    if (url.pathname.indexOf(root) !== 0) return "";
    return url.pathname.slice((base || "").length) + url.search + url.hash;
  };

  // The ?next= return path of this page, cleaned (see cleanNext)
  w.safeNext = function () {
    var v = "";
    try { v = new URLSearchParams(w.location.search).get("next") || ""; } catch (e) { v = ""; }
    return w.cleanNext(v, B, w.location.origin);
  };

  w.readCsrf = function () {
    try { return w.sessionStorage.getItem("csrf") || ""; } catch (e) { return ""; }
  };
  w.writeCsrf = function (v) {
    try {
      if (v) w.sessionStorage.setItem("csrf", v);
      else w.sessionStorage.removeItem("csrf");
    } catch (e) { /* storage blocked: the dashboard reads the value from /me again */ }
  };
})(window);
