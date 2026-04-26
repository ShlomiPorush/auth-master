(function (w) {
  w.adminApi = async function (path, opts) {
    opts = opts || {};
    var headers = new Headers();
    if (opts.headers) {
      for (var k in opts.headers) {
        if (opts.headers.hasOwnProperty(k)) headers.set(k, opts.headers[k]);
      }
    }
    if (opts.csrf) headers.set("X-CSRF-Token", opts.csrf);
    if (opts.body !== undefined) headers.set("Content-Type", "application/json");
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      credentials: "include",
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  };
  w.readCsrf = function () {
    return sessionStorage.getItem("csrf") || "";
  };
  w.writeCsrf = function (v) {
    if (v) sessionStorage.setItem("csrf", v);
    else sessionStorage.removeItem("csrf");
  };
})(window);
