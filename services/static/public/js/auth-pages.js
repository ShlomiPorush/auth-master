/*
 * Sign in (login, login-mfa) and first setup (setup, setup-mfa). Each page names
 * itself in <body data-auth-page="...">. Same endpoints and CSRF flow as before;
 * every link and redirect is built from basePath(). Uses js/common.js and js/ui.js.
 */

var AUTH_EYE_ICON = '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>';
var AUTH_NETWORK_TEXT = "Unable to connect. Check your connection and try again.";

// POST with JSON; resolves { ok, status, data } and never rejects
function authPost(promise) {
    return promise.then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, data: j || {} }; });
    }, function () {
        return { ok: false, status: 0, data: {}, network: true };
    });
}

function authErrorText(res, fallback) {
    if (res.network) return AUTH_NETWORK_TEXT;
    var j = res.data || {};
    if (typeof j.detail === "string" && j.detail) return j.detail;
    if (typeof j.error === "string" && j.error) return j.error;
    return fallback;
}

function authShowError(text) {
    var el = document.getElementById("err");
    el.textContent = text;
    el.classList.remove("hidden");
}

function authHideError() {
    document.getElementById("err").classList.add("hidden");
}

// The submit button waits with a spinner and says what is happening, then comes back (FB-7)
function authBusy(button, on, text) {
    if (on) {
        button.setAttribute("data-label", button.textContent);
        button.disabled = true;
        button.innerHTML = '<span class="ui-spin" aria-hidden="true"></span>' + esc(text);
    } else {
        button.disabled = false;
        button.textContent = button.getAttribute("data-label") || button.textContent;
    }
}

// Show-password buttons and the Caps Lock note (FS-23)
function authPasswordHelpers() {
    document.querySelectorAll("[data-show-password]").forEach(function (b) {
        var input = b.parentElement.querySelector("input");
        b.innerHTML = AUTH_EYE_ICON;
        // The field keeps the focus
        b.addEventListener("mousedown", function (event) { event.preventDefault(); });
        b.addEventListener("click", function () {
            var show = input.type === "password";
            input.type = show ? "text" : "password";
            b.setAttribute("aria-pressed", String(show));
            b.setAttribute("aria-label", show ? "Hide password" : "Show password");
            b.setAttribute("title", show ? "Hide password" : "Show password");
        });
    });
    document.querySelectorAll("[data-caps]").forEach(function (note) {
        var input = document.getElementById(note.getAttribute("data-caps"));
        var check = function (event) {
            if (!event.getModifierState) return;
            note.classList.toggle("hidden", !event.getModifierState("CapsLock"));
        };
        input.addEventListener("keydown", check);
        input.addEventListener("keyup", check);
        input.addEventListener("mousedown", check);
        input.addEventListener("blur", function () { note.classList.add("hidden"); });
    });
}

function authFieldError(input, text) {
    input.setAttribute("aria-invalid", "true");
    authShowError(text);
    input.focus();
}

// --- login --------------------------------------------------------------

function authLogin() {
    var form = document.getElementById("f");
    form.addEventListener("submit", function (event) {
        event.preventDefault();
        authHideError();
        var user = form.querySelector('[name="username"]');
        var pass = form.querySelector('[name="password"]');
        user.removeAttribute("aria-invalid");
        pass.removeAttribute("aria-invalid");
        if (!user.value.trim()) { authFieldError(user, "Username: enter your username"); return; }
        if (!pass.value) { authFieldError(pass, "Password: enter your password"); return; }
        var button = form.querySelector("[type=submit]");
        authBusy(button, true, "Signing in...");
        authPost(adminApi("/admin/api/login", { method: "POST", body: { username: user.value, password: pass.value } })).then(function (res) {
            authBusy(button, false);
            if (!res.ok) {
                pass.value = "";
                authShowError(authErrorText(res, "Failed"));
                pass.focus();
                return;
            }
            writeCsrf(res.data.csrf || "");
            var next = safeNext();
            window.location.href = basePath() + "/login-mfa" + (next ? "?next=" + encodeURIComponent(next) : "");
        });
    });
}

// --- login-mfa ----------------------------------------------------------

function authLoginMfa() {
    var form = document.getElementById("f");
    var code = form.querySelector('[name="code"]');
    var back = document.getElementById("back-to-login");
    var next = safeNext();
    back.setAttribute("href", basePath() + "/login" + (next ? "?next=" + encodeURIComponent(next) : ""));
    code.focus();
    code.select();
    form.addEventListener("submit", function (event) {
        event.preventDefault();
        authHideError();
        code.removeAttribute("aria-invalid");
        if (!code.value.trim()) { authFieldError(code, "Enter the code from your authenticator app."); return; }
        var button = form.querySelector("[type=submit]");
        authBusy(button, true, "Signing in...");
        authPost(adminApi("/admin/api/login/mfa", { method: "POST", body: { code: code.value }, csrf: readCsrf() })).then(function (res) {
            authBusy(button, false);
            if (!res.ok) {
                authShowError(authErrorText(res, "Failed"));
                code.select();
                return;
            }
            writeCsrf(res.data.csrf || "");
            window.location.href = basePath() + (safeNext() || "/");
        });
    });
}

// --- setup --------------------------------------------------------------

function authSetup() {
    var form = document.getElementById("f");
    form.addEventListener("submit", function (event) {
        event.preventDefault();
        authHideError();
        var user = form.querySelector('[name="username"]');
        var pass = form.querySelector('[name="password"]');
        var boot = form.querySelector('[name="bootstrap"]');
        user.removeAttribute("aria-invalid");
        pass.removeAttribute("aria-invalid");
        if (user.value.trim().length < 2) { authFieldError(user, "Username: use at least 2 characters"); return; }
        if (pass.value.length < 8) { authFieldError(pass, "Password: use at least 8 characters"); return; }
        var headers = {};
        if (boot.value) headers["X-Bootstrap-Token"] = boot.value;
        var button = form.querySelector("[type=submit]");
        authBusy(button, true, "Creating the account...");
        authPost(adminApi("/admin/api/bootstrap/register", { method: "POST", body: { username: user.value, password: pass.value }, headers: headers })).then(function (res) {
            authBusy(button, false);
            if (!res.ok) {
                authShowError(authErrorText(res, "Failed"));
                return;
            }
            writeCsrf(res.data.csrf || "");
            window.location.href = basePath() + "/setup-mfa";
        });
    });
}

// --- setup-mfa ----------------------------------------------------------

// The setup session ended: start the setup again
function authSetupExpired(reason) {
    var box = document.getElementById("setup-mfa-box");
    box.innerHTML = '<div class="ui-banner ui-banner-warn" role="alert"><div><b>Session expired.</b><p class="ui-muted">' + esc(reason || "") +
        '</p></div><a class="ui-btn ui-btn-sm" id="start-over">Start over</a></div>';
    document.getElementById("start-over").setAttribute("href", basePath() + "/setup");
}

function authSetupMfa() {
    var form = document.getElementById("f");
    var code = form.querySelector('[name="code"]');
    adminJson("/admin/api/bootstrap/mfa-enroll").then(function (res) {
        if (!res.ok || !res.data) {
            authSetupExpired(res.error ? AUTH_NETWORK_TEXT : authErrorText(res, ""));
            return;
        }
        var secret = res.data.secretBase32 || "";
        document.getElementById("sec").innerHTML = uiCopyable(secret);
        document.getElementById("qr").innerHTML = "";
        new QRCode(document.getElementById("qr"), { width: 180, height: 180, text: res.data.otpauthUrl });
        document.getElementById("setup-mfa-loading").classList.add("hidden");
        document.getElementById("setup-mfa-ready").classList.remove("hidden");
        code.focus();
    });
    form.addEventListener("submit", function (event) {
        event.preventDefault();
        authHideError();
        code.removeAttribute("aria-invalid");
        if (!code.value.trim()) { authFieldError(code, "Enter the code from your authenticator app."); return; }
        var button = form.querySelector("[type=submit]");
        authBusy(button, true, "Checking...");
        authPost(adminApi("/admin/api/bootstrap/mfa-verify", { method: "POST", body: { code: code.value }, csrf: readCsrf() })).then(function (res) {
            authBusy(button, false);
            if (res.status === 401) {
                authSetupExpired(authErrorText(res, ""));
                return;
            }
            if (!res.ok) {
                authShowError(authErrorText(res, "Failed"));
                code.select();
                return;
            }
            writeCsrf(res.data.csrf || "");
            window.location.href = basePath() + "/";
        });
    });
}

function authStart() {
    uiInstallGlobalListeners();
    authPasswordHelpers();
    var page = document.body.getAttribute("data-auth-page");
    if (page === "login") authLogin();
    else if (page === "login-mfa") authLoginMfa();
    else if (page === "setup") authSetup();
    else if (page === "setup-mfa") authSetupMfa();
}

if (typeof document !== "undefined" && document.readyState !== undefined) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", authStart);
    else authStart();
}
