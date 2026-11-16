/*
 * Sign in (login, login-mfa), first setup (setup, setup-mfa) and the recovery page
 * (recovery). Each page names itself in <body data-auth-page="...">. Same endpoints and CSRF flow as before;
 * every link and redirect is built from basePath(). Uses js/common.js and js/ui.js.
 */

var AUTH_EYE_ICON = '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>';
var AUTH_NETWORK_TEXT = "auth.unable-to-connect-check-your-connection";

// POST with JSON; resolves { ok, status, data } and never rejects
function authPost(promise) {
    return promise.then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, data: j || {} }; });
    }, function () {
        return { ok: false, status: 0, data: {}, network: true };
    });
}

function authErrorText(res, fallback) {
    if (res.network) return t(AUTH_NETWORK_TEXT);
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
            b.setAttribute("aria-label", show ? t("auth.hide-password") : t("auth.show-password"));
            b.setAttribute("title", show ? t("auth.hide-password") : t("auth.show-password"));
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
        if (!user.value.trim()) { authFieldError(user, t("login.username-enter-your-username")); return; }
        if (!pass.value) { authFieldError(pass, t("login.password-enter-your-password")); return; }
        var button = form.querySelector("[type=submit]");
        authBusy(button, true, t("login.signing-in"));
        authPost(adminApi("/admin/api/login", { method: "POST", body: { username: user.value, password: pass.value } })).then(function (res) {
            authBusy(button, false);
            if (!res.ok) {
                pass.value = "";
                authShowError(authErrorText(res, t("login.failed")));
                pass.focus();
                return;
            }
            writeCsrf(res.data.csrf || "");
            var next = safeNext();
            window.location.href = basePath() + "/login-mfa" + (next ? "?next=" + encodeURIComponent(next) : "");
        });
    });
    authPasskeyOffer();
}

// --- passkey sign-in (login) ----------------------------------------------

var authPasskeyRunning = false;

// The passkey button hides when the service has no passkey address: the probe
// answers 409. It stores nothing; a click asks for new options and reports any
// other failure then.
function authPasskeyOffer() {
    var box = document.getElementById("pk-login");
    var note = document.getElementById("pk-note");
    if (!box || !note) return;
    document.getElementById("pk-signin").addEventListener("click", authPasskeySignIn);
    authPost(adminApi("/admin/api/login/passkey/options", { method: "POST" })).then(function (res) {
        if (res.status === 409) return;
        var blocked = uiPasskeyBlocked(window, false);
        if (blocked) {
            note.textContent = blocked === "insecure" ? t(UI_PASSKEY_INSECURE) : t("login.passkey-old-browser");
            note.classList.remove("hidden");
            return;
        }
        box.classList.remove("hidden");
    });
}

// Tell the password manager that the server does not know this passkey (sign-in only)
function authPasskeyForget(rpId, credentialId) {
    var pkc = window.PublicKeyCredential;
    if (!pkc || typeof pkc.signalUnknownCredential !== "function" || !rpId || !credentialId) return;
    try {
        Promise.resolve(pkc.signalUnknownCredential({ rpId: rpId, credentialId: credentialId })).catch(function () {});
    } catch (e) { /* a hint to the password manager only */ }
}

function authPasskeySignIn() {
    if (authPasskeyRunning) return;
    var button = document.getElementById("pk-signin");
    authHideError();
    authPasskeyRunning = true;
    authBusy(button, true, t("login.waiting-for-your-passkey"));
    var done = function (text) {
        authPasskeyRunning = false;
        authBusy(button, false);
        if (text) authShowError(text);
    };
    // New options on every click: a new challenge each time
    authPost(adminApi("/admin/api/login/passkey/options", { method: "POST" })).then(function (opt) {
        if (!opt.ok) {
            if (opt.status === 409) document.getElementById("pk-login").classList.add("hidden");
            done(authErrorText(opt, t("login.could-not-start-the-passkey-sign")));
            return;
        }
        var json = opt.data.publicKey || {};
        var publicKey;
        try {
            publicKey = PublicKeyCredential.parseRequestOptionsFromJSON(json);
        } catch (e) {
            done(t("login.this-browser-could-not-read-the"));
            return;
        }
        navigator.credentials.get({ publicKey: publicKey }).then(function (credential) {
            if (!credential) { done(""); return; }
            var encoded = credential.toJSON();
            authPost(adminApi("/admin/api/login/passkey", { method: "POST", body: { credential: encoded, ceremony: opt.data.ceremony } })).then(function (res) {
                if (res.ok) {
                    writeCsrf(res.data.csrf || "");
                    window.location.href = basePath() + (safeNext() || "/");
                    return;
                }
                // The server has no such passkey: the password manager may drop it
                if (res.status === 404) authPasskeyForget(json.rpId, encoded.id);
                done(authErrorText(res, t("login.could-not-sign-in-with-the")));
            });
        }, function (err) {
            done(uiPasskeyErrorText(err, false));
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
        if (!code.value.trim()) { authFieldError(code, t("loginmfa.enter-the-code-from-your-authenticator")); return; }
        var button = form.querySelector("[type=submit]");
        authBusy(button, true, t("loginmfa.signing-in"));
        authPost(adminApi("/admin/api/login/mfa", { method: "POST", body: { code: code.value }, csrf: readCsrf() })).then(function (res) {
            authBusy(button, false);
            if (!res.ok) {
                authShowError(authErrorText(res, t("loginmfa.failed")));
                code.select();
                return;
            }
            writeCsrf(res.data.csrf || "");
            window.location.href = basePath() + (safeNext() || "/");
        });
    });
}

// --- setup --------------------------------------------------------------

// Poll a database operation until it ends; resolves the job (or null when the server cannot be reached)
function authWaitJob(path, headers) {
    return new Promise(function (resolve) {
        var misses = 0;
        var poll = function () {
            adminJson(path, { headers: headers || {} }).then(function (res) {
                var job = res.ok && res.data && res.data.job;
                if (job && job.state !== "running") { resolve(job); return; }
                if (!job && ++misses > 20) { resolve(null); return; }
                setTimeout(poll, 500);
            });
        };
        poll();
    });
}

// A choice between SQLite and PostgreSQL (ui-seg buttons with data-db-kind)
function authKindSwitch(form, onChange) {
    form.querySelectorAll("[data-db-kind]").forEach(function (b) {
        b.addEventListener("click", function () {
            form.querySelectorAll("[data-db-kind]").forEach(function (o) { o.setAttribute("aria-pressed", String(o === b)); });
            onChange(b.getAttribute("data-db-kind"));
        });
    });
}

function authSetError(id, text) {
    var el = document.getElementById(id);
    el.textContent = text || "";
    el.classList.toggle("hidden", !text);
}

// The first step of setup while no database is configured: choose and check it
function authSetupDatabase(showRegister) {
    var form = document.getElementById("db-form");
    var pg = document.getElementById("db-pg-fields");
    var result = document.getElementById("db-result");
    var adoptRow = document.getElementById("db-adopt-row");
    var adopt = document.getElementById("db-adopt");
    var kind = "sqlite";
    var busy = false;
    document.getElementById("setup-sub").textContent = t("setup.choose-where-the-service-keeps-its");
    pg.innerHTML = '<div class="ui-form">' + uiDbFieldsHtml("db-", { port: 5432 }, false) + "</div>";
    form.classList.remove("hidden");
    var reset = function () {
        result.innerHTML = "";
        adoptRow.classList.add("hidden");
        adopt.checked = false;
        authSetError("db-err", "");
    };
    authKindSwitch(form, function (k) {
        kind = k;
        pg.classList.toggle("hidden", k !== "postgres");
        document.getElementById("db-sqlite-note").classList.toggle("hidden", k !== "sqlite");
        reset();
    });
    form.addEventListener("input", function (event) {
        if (event.target.id === "db-adopt") return;
        if (event.target.id === "db-sslmode") uiDbFieldsSync("db-");
        result.innerHTML = "";
    });
    form.addEventListener("change", function (event) { if (event.target.id === "db-sslmode") uiDbFieldsSync("db-"); });
    var body = function () {
        var b = { backend: kind };
        if (kind === "postgres") b.postgres = uiDbFieldsRead("db-");
        return b;
    };
    var showCheck = function (r) {
        result.innerHTML = uiDbNoteHtml(uiDbCheckNote(r, "setup"));
        adoptRow.classList.toggle("hidden", !(r && r.ok && r.state === "older"));
    };
    var setBusy = function (on, button, text) {
        busy = on;
        authBusy(button, on, text);
        document.getElementById(button.id === "db-check" ? "db-continue" : "db-check").disabled = on;
    };
    document.getElementById("db-check").addEventListener("click", function () {
        if (busy) return;
        authSetError("db-err", "");
        var button = this;
        setBusy(true, button, t("setup.checking"));
        authPost(adminApi("/admin/api/setup/database/check", { method: "POST", body: body() })).then(function (res) {
            setBusy(false, button);
            if (!res.ok) {
                authSetError("db-err", authErrorText(res, t("setup.the-check-failed")));
                if (res.data && res.data.field) uiDbFieldError("db-", res.data.field);
                return;
            }
            showCheck(res.data);
        });
    });
    form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (busy) return;
        authSetError("db-err", "");
        var button = document.getElementById("db-continue");
        var b = body();
        if (adopt.checked) b.adopt = true;
        setBusy(true, button, t("setup.setting-up-the-database"));
        authPost(adminApi("/admin/api/setup/database", { method: "POST", body: b })).then(function (res) {
            if (!res.ok || !res.data.job) {
                setBusy(false, button);
                authSetError("db-err", authErrorText(res, t("setup.could-not-start-the-setup")));
                if (res.data && res.data.field) uiDbFieldError("db-", res.data.field);
                return;
            }
            return authWaitJob("/admin/api/setup/database/jobs/" + encodeURIComponent(res.data.job.id)).then(function (job) {
                setBusy(false, button);
                if (!job) { authSetError("db-err", t("setup.the-server-stopped-answering-reload-the")); return; }
                if (job.state !== "done") {
                    if (job.code === "attach_failed") {
                        // Saved, but the service could not start on it: recovery mode
                        window.location.href = basePath() + "/recovery";
                        return;
                    }
                    if (job.code === "confirm_adopt") {
                        adoptRow.classList.remove("hidden");
                        authSetError("db-err", t("setup.this-database-holds-this-service-s"));
                        return;
                    }
                    authSetError("db-err", uiDbCodeText(job.code, job.error));
                    return;
                }
                if (job.result && job.result.next === "login") {
                    window.location.href = basePath() + "/login";
                    return;
                }
                form.classList.add("hidden");
                showRegister();
            });
        });
    });
}

function authSetup() {
    var form = document.getElementById("f");
    var showRegister = function () {
        document.getElementById("setup-sub").textContent = t("setup.create-the-first-administrator-account");
        form.classList.remove("hidden");
        form.querySelector('[name="username"]').focus();
    };
    adminJson("/admin/api/setup/status").then(function (res) {
        document.getElementById("setup-loading").classList.add("hidden");
        var d = res.ok && res.data;
        if (d && d.needsDatabase) authSetupDatabase(showRegister);
        else if (d && d.needsSetup === false) window.location.href = basePath() + "/login";
        else showRegister();
    });
    form.addEventListener("submit", function (event) {
        event.preventDefault();
        authHideError();
        var user = form.querySelector('[name="username"]');
        var pass = form.querySelector('[name="password"]');
        user.removeAttribute("aria-invalid");
        pass.removeAttribute("aria-invalid");
        if (user.value.trim().length < 2) { authFieldError(user, t("setup.username-use-at-least-2-characters")); return; }
        if (pass.value.length < 8) { authFieldError(pass, t("setup.password-use-at-least-8-characters")); return; }
        if (new TextEncoder().encode(pass.value).length > 72) { authFieldError(pass, t("setup.password-use-at-most-72-bytes")); return; }
        var button = form.querySelector("[type=submit]");
        authBusy(button, true, t("setup.creating-the-account"));
        authPost(adminApi("/admin/api/bootstrap/register", { method: "POST", body: { username: user.value, password: pass.value } })).then(function (res) {
            authBusy(button, false);
            if (!res.ok) {
                authShowError(authErrorText(res, t("setup.failed")));
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
    box.innerHTML = '<div class="ui-banner ui-banner-warn" role="alert"><div><b>' + esc(t("setupmfa.session-expired")) + '</b><p class="ui-muted">' + esc(reason || "") +
        '</p></div><a class="ui-btn ui-btn-sm" id="start-over">' + esc(t("setupmfa.start-over")) + '</a></div>';
    document.getElementById("start-over").setAttribute("href", basePath() + "/setup");
}

// The QR code, the secret and the issuer name from an enrollment answer
function authSetupMfaShow(data) {
    var secret = data.secretBase32 || "";
    document.getElementById("sec").innerHTML = uiCopyable(secret);
    document.getElementById("qr").innerHTML = "";
    new QRCode(document.getElementById("qr"), { width: 180, height: 180, text: data.otpauthUrl });
    var input = document.getElementById("issuer");
    var locked = document.getElementById("issuer-locked");
    var issuer = data.issuer || "";
    input.value = issuer;
    input.setAttribute("data-saved", issuer);
    if (data.issuerMaxLength) input.maxLength = data.issuerMaxLength;
    input.classList.toggle("hidden", !!data.issuerLocked);
    document.getElementById("issuer-hint").classList.toggle("hidden", !!data.issuerLocked);
    locked.classList.toggle("hidden", !data.issuerLocked);
    locked.innerHTML = data.issuerLocked
        ? UI_LOCK_ICON + "<span>" + tHtml("setupmfa.issuer-locked", { env: tRaw("<code>" + esc(data.issuerEnvName || "TOTP_ISSUER") + "</code>"), value: issuer }) + "</span>"
        : "";
}

// A new issuer name: the server keeps it for this setup only and answers with a new QR code
var authIssuerPending = null;
function authIssuerSave() {
    var input = document.getElementById("issuer");
    var err = document.getElementById("issuer-err");
    var name = input.value.trim();
    if (authIssuerPending || input.classList.contains("hidden") || name === input.getAttribute("data-saved")) return authIssuerPending;
    err.classList.add("hidden");
    input.removeAttribute("aria-invalid");
    if (!name) {
        input.setAttribute("aria-invalid", "true");
        err.textContent = t("setupmfa.name-in-authenticator-apps-enter-a");
        err.classList.remove("hidden");
        return null;
    }
    input.disabled = true;
    authIssuerPending = authPost(adminApi("/admin/api/bootstrap/issuer", { method: "POST", body: { issuer: name }, csrf: readCsrf() })).then(function (res) {
        authIssuerPending = null;
        input.disabled = false;
        if (res.status === 401 || res.status === 400) {
            authSetupExpired(authErrorText(res, ""));
            return;
        }
        if (!res.ok || !res.data || !res.data.otpauthUrl) {
            if (res.status === 422) input.setAttribute("aria-invalid", "true");
            err.textContent = authErrorText(res, t("setupmfa.could-not-change-the-name-try"));
            err.classList.remove("hidden");
            return;
        }
        authSetupMfaShow(res.data);
    });
    return authIssuerPending;
}

function authSetupMfa() {
    var form = document.getElementById("f");
    var code = form.querySelector('[name="code"]');
    adminJson("/admin/api/bootstrap/mfa-enroll").then(function (res) {
        if (!res.ok || !res.data) {
            authSetupExpired(res.error ? t(AUTH_NETWORK_TEXT) : authErrorText(res, ""));
            return;
        }
        authSetupMfaShow(res.data);
        document.getElementById("setup-mfa-loading").classList.add("hidden");
        document.getElementById("setup-mfa-ready").classList.remove("hidden");
        code.focus();
    });
    document.getElementById("issuer").addEventListener("change", authIssuerSave);
    document.getElementById("issuer-form").addEventListener("submit", function (event) {
        event.preventDefault();
        authIssuerSave();
    });
    form.addEventListener("submit", function (event) {
        event.preventDefault();
        // A name change still on its way (the field lost focus to this button) finishes first
        var pending = authIssuerPending;
        if (pending) {
            pending.then(function () { if (document.getElementById("f")) form.requestSubmit(); });
            return;
        }
        authHideError();
        code.removeAttribute("aria-invalid");
        if (!code.value.trim()) { authFieldError(code, t("setupmfa.enter-the-code-from-your-authenticator")); return; }
        var button = form.querySelector("[type=submit]");
        authBusy(button, true, t("setupmfa.checking"));
        authPost(adminApi("/admin/api/bootstrap/mfa-verify", { method: "POST", body: { code: code.value }, csrf: readCsrf() })).then(function (res) {
            authBusy(button, false);
            if (res.status === 401) {
                authSetupExpired(authErrorText(res, ""));
                return;
            }
            if (!res.ok) {
                authShowError(authErrorText(res, t("setupmfa.failed")));
                code.select();
                return;
            }
            writeCsrf(res.data.csrf || "");
            window.location.href = basePath() + "/";
        });
    });
}

// --- recovery ------------------------------------------------------------

// The database cannot be used: the master key, kept in this page's memory only,
// opens the saved connection for editing. Every call sends it again.
function authRecovery() {
    var key = "";
    var kind = "postgres";
    var busy = false;
    var keyForm = document.getElementById("rec-key-form");
    var form = document.getElementById("rec-form");
    var result = document.getElementById("rec-result");
    var call = function (method, path, body) {
        var opts = { method: method, headers: { Authorization: "Bearer " + key } };
        if (body !== undefined) opts.body = body;
        return adminJson(path, opts);
    };
    var running = function () {
        document.getElementById("rec-signin").setAttribute("href", basePath() + "/login");
        keyForm.classList.add("hidden");
        form.classList.add("hidden");
        document.getElementById("rec-running").classList.remove("hidden");
    };
    var fail = function (res, fallback) {
        if (res.status === 429) return t("recovery.too-many-wrong-keys-from-this", { minutes: Math.ceil(((res.data && res.data.retryAfter) || 60) / 60) });
        if (res.error) return res.error === "timeout" ? t("recovery.the-server-did-not-answer-in") : t(AUTH_NETWORK_TEXT);
        return (res.data && res.data.detail) || fallback;
    };
    var showForm = function (d) {
        keyForm.classList.add("hidden");
        form.classList.remove("hidden");
        document.getElementById("rec-why").textContent = " " + (d.message || "");
        var c = d.configured;
        var saved = "";
        if (c && c.backend === "postgres") saved = t("recovery.saved-connection", { user: c.user, host: c.host, port: c.port, database: c.database, sslmode: uiIdWord("db.sslmode.", c.sslmode) });
        else if (c && c.backend === "sqlite") saved = t("recovery.saved-database-the-sqlite-file", { path: c.path });
        document.getElementById("rec-saved").textContent = saved;
        var pgValues = c && c.backend === "postgres" ? c : { port: 5432 };
        var keep = !!(c && c.backend === "postgres" && d.code !== "config_unreadable");
        document.getElementById("rec-pg-fields").innerHTML = '<div class="ui-form">' + uiDbFieldsHtml("rec-", pgValues, keep) + "</div>";
        if (c && c.backend === "sqlite") form.querySelector('[data-db-kind="sqlite"]').click();
    };
    var open = function () {
        call("GET", "/admin/api/recovery/status").then(function (res) {
            var button = keyForm.querySelector("[type=submit]");
            authBusy(button, false);
            if (!res.ok) {
                authShowError(fail(res, t("recovery.the-master-key-is-not-correct")));
                return;
            }
            if (res.data.mode !== "recovery") { running(); return; }
            showForm(res.data);
        });
    };
    adminJson("/admin/api/recovery/status").then(function (res) {
        document.getElementById("rec-loading").classList.add("hidden");
        var d = res.ok && res.data;
        if (d && d.mode !== "recovery") running();
        else if (d && !d.masterKeyConfigured) document.getElementById("rec-no-key").classList.remove("hidden");
        else {
            keyForm.classList.remove("hidden");
            document.getElementById("rec-key").focus();
        }
    });
    keyForm.addEventListener("submit", function (event) {
        event.preventDefault();
        authHideError();
        key = document.getElementById("rec-key").value.trim();
        if (!key) { authShowError(t("recovery.enter-the-master-key")); return; }
        authBusy(keyForm.querySelector("[type=submit]"), true, t("recovery.checking"));
        open();
    });
    authKindSwitch(form, function (k) {
        kind = k;
        document.getElementById("rec-pg-fields").classList.toggle("hidden", k !== "postgres");
        document.getElementById("rec-sqlite-note").classList.toggle("hidden", k !== "sqlite");
        result.innerHTML = "";
        authSetError("rec-err", "");
    });
    form.addEventListener("input", function (event) {
        if (event.target.id === "rec-sslmode") uiDbFieldsSync("rec-");
        if (event.target.id !== "rec-confirm") result.innerHTML = "";
    });
    form.addEventListener("change", function (event) { if (event.target.id === "rec-sslmode") uiDbFieldsSync("rec-"); });
    var body = function () {
        var b = { backend: kind };
        if (kind === "postgres") b.postgres = uiDbFieldsRead("rec-");
        return b;
    };
    var setBusy = function (on, button, text) {
        busy = on;
        authBusy(button, on, text);
        document.getElementById(button.id === "rec-check" ? "rec-save" : "rec-check").disabled = on;
    };
    document.getElementById("rec-check").addEventListener("click", function () {
        if (busy) return;
        authSetError("rec-err", "");
        var button = this;
        setBusy(true, button, t("recovery.checking"));
        call("POST", "/admin/api/recovery/check", body()).then(function (res) {
            setBusy(false, button);
            if (!res.ok) {
                authSetError("rec-err", fail(res, t("recovery.the-check-failed")));
                if (res.data && res.data.field) uiDbFieldError("rec-", res.data.field);
                return;
            }
            result.innerHTML = uiDbNoteHtml(uiDbCheckNote(res.data, "recovery"));
        });
    });
    form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (busy) return;
        authSetError("rec-err", "");
        var confirm = document.getElementById("rec-confirm");
        if (!confirm.checked) {
            authSetError("rec-err", t("recovery.tick-the-box-to-confirm-that"));
            confirm.focus();
            return;
        }
        var b = body();
        b.confirm = true;
        var button = document.getElementById("rec-save");
        setBusy(true, button, t("recovery.connecting"));
        call("POST", "/admin/api/recovery/save", b).then(function (res) {
            if (!res.ok || !res.data.job) {
                setBusy(false, button);
                authSetError("rec-err", fail(res, t("recovery.could-not-start")));
                if (res.data && res.data.field) uiDbFieldError("rec-", res.data.field);
                return;
            }
            return authWaitJob("/admin/api/recovery/jobs/" + encodeURIComponent(res.data.job.id), { Authorization: "Bearer " + key }).then(function (job) {
                setBusy(false, button);
                if (!job) { authSetError("rec-err", t("recovery.the-server-stopped-answering-reload-the")); return; }
                if (job.state !== "done") { authSetError("rec-err", uiDbCodeText(job.code, job.error)); return; }
                key = "";
                running();
            });
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
    else if (page === "recovery") authRecovery();
}

// The last script of the page: every word of the parsed page is in place (js/text.js)
if (typeof document !== "undefined" && document && typeof document.querySelectorAll === "function") uiApplyText(document, true);

if (typeof document !== "undefined" && document.readyState !== undefined) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", authStart);
    else authStart();
}
