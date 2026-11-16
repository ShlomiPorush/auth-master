// Passkeys in the portal: the support check and error words (js/ui.js), the sign-in
// button's flow (js/auth-pages.js), the passkey check before Copy token / Copy key,
// the passkey address save and the Activity Log details (js/dashboard.js). The
// browser's WebAuthn calls and the API are fakes.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadScripts } = require("./_load.cjs");

const PAGE = {
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    readyState: undefined,
};

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function settle() {
    for (let i = 0; i < 10; i++) await tick();
}

function browser(extra) {
    function PublicKeyCredential() {}
    PublicKeyCredential.prototype.toJSON = function () { return {}; };
    PublicKeyCredential.parseRequestOptionsFromJSON = (json) => ({ parsed: "request", json });
    PublicKeyCredential.parseCreationOptionsFromJSON = (json) => ({ parsed: "creation", json });
    return Object.assign({ isSecureContext: true, PublicKeyCredential, navigator: { credentials: {} } }, extra || {});
}

function domError(name) {
    const e = new Error(name);
    e.name = name;
    return e;
}

// --- js/ui.js ---------------------------------------------------------------

const ui = loadScripts(["js/text.js", "js/common.js", "js/ui.js"]);

test("passkeys are offered only in a secure context with WebAuthn and its JSON helpers", () => {
    assert.equal(ui.uiPasskeyBlocked(browser(), false), "");
    assert.equal(ui.uiPasskeyBlocked(browser(), true), "");
    assert.equal(ui.uiPasskeyBlocked(browser({ isSecureContext: false }), false), "insecure");
    // Outside a secure context the browser hides WebAuthn too: HTTPS is the reason given
    assert.equal(ui.uiPasskeyBlocked(browser({ isSecureContext: false, PublicKeyCredential: undefined }), false), "insecure");
    assert.equal(ui.uiPasskeyBlocked(browser({ PublicKeyCredential: undefined }), false), "browser");
    const old = browser();
    delete old.PublicKeyCredential.parseRequestOptionsFromJSON;
    assert.equal(ui.uiPasskeyBlocked(old, false), "browser");
    const noJson = browser();
    delete noJson.PublicKeyCredential.prototype.toJSON;
    assert.equal(ui.uiPasskeyBlocked(noJson, false), "browser");
    const noCreate = browser();
    delete noCreate.PublicKeyCredential.parseCreationOptionsFromJSON;
    assert.equal(ui.uiPasskeyBlocked(noCreate, false), "", "signing in needs no creation helper");
    assert.equal(ui.uiPasskeyBlocked(noCreate, true), "browser");
    assert.equal(ui.uiPasskeyBlocked(null, false), "insecure");
});

test("a closed or cancelled browser window says nothing; other errors say what to do", () => {
    assert.equal(ui.uiPasskeyErrorText(domError("NotAllowedError"), false), "");
    assert.equal(ui.uiPasskeyErrorText(domError("NotAllowedError"), true), "");
    assert.equal(ui.uiPasskeyErrorText(domError("AbortError"), true), "");
    assert.match(ui.uiPasskeyErrorText(domError("InvalidStateError"), true), /already has a passkey here/);
    assert.match(ui.uiPasskeyErrorText(domError("SecurityError"), false), /passkey address/);
    assert.match(ui.uiPasskeyErrorText(domError("NotSupportedError"), true), /cannot create/);
    assert.match(ui.uiPasskeyErrorText(new TypeError("x"), false), /could not be used\. Try again\./);
    assert.match(ui.uiPasskeyErrorText(undefined, true), /could not be created\. Try again\./);
});

test("passkey names: spaces collapsed, 1 to 64 characters counted as the server counts them", () => {
    assert.equal(ui.uiPasskeyName("  My   phone \t key "), "My phone key");
    assert.equal(ui.uiPasskeyNameProblem("   "), "Give the passkey a name.");
    assert.equal(ui.uiPasskeyNameProblem("x".repeat(64)), "");
    assert.match(ui.uiPasskeyNameProblem("x".repeat(65)), /at most 64/);
    // Emoji are one character each for the server (code points), not two
    assert.equal(ui.uiPasskeyNameProblem("🔑".repeat(64)), "");
    assert.equal(ui.uiPasskeyNameProblem("a" + " ".repeat(80) + "b"), "");
});

test("the proposed name names the system when the browser tells it", () => {
    assert.equal(ui.uiPasskeyDefaultName({ userAgentData: { platform: "macOS" } }), "Passkey on Mac");
    assert.equal(ui.uiPasskeyDefaultName({ userAgentData: { platform: "Windows" } }), "Passkey on Windows");
    assert.equal(ui.uiPasskeyDefaultName({ userAgent: "Mozilla/5.0 (Linux; Android 14; Pixel 8)" }), "Passkey on Android");
    assert.equal(ui.uiPasskeyDefaultName({ userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)" }), "Passkey on iPhone");
    assert.equal(ui.uiPasskeyDefaultName({ userAgent: "Something" }), "Passkey");
    assert.equal(ui.uiPasskeyDefaultName(undefined), "Passkey");
});

// --- js/auth-pages.js: Sign in with a passkey -------------------------------------

function el() {
    const classes = new Set(["hidden"]);
    const node = {
        textContent: "", innerHTML: "", disabled: false, attrs: {},
        classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c), toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)) },
        setAttribute(k, v) { this.attrs[k] = v; },
        getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
        addEventListener(type, fn) { this["on" + type] = fn; },
    };
    return node;
}

function loginPage(answers, get) {
    const els = { "pk-login": el(), "pk-note": el(), "pk-signin": el(), err: el() };
    const doc = { ...PAGE, getElementById: (id) => els[id] || null };
    const signals = [];
    const calls = [];
    const win = browser();
    win.PublicKeyCredential.signalUnknownCredential = (o) => { signals.push(o); return Promise.resolve(); };
    win.navigator = { credentials: { get: (o) => { calls.push(["get", o]); return get(o); } } };
    const ctx = loadScripts(["js/text.js", "js/common.js", "js/ui.js", "js/auth-pages.js"], {
        document: doc, location: { href: "", search: "?next=%2Fadmin%2Fzones", origin: "https://auth.example.test" },
        isSecureContext: win.isSecureContext, PublicKeyCredential: win.PublicKeyCredential, navigator: win.navigator,
    });
    ctx.adminApi = (path, opts) => {
        calls.push([path, opts && opts.body]);
        const a = answers(path, opts);
        return Promise.resolve({ ok: a.status < 400, status: a.status, json: () => Promise.resolve(a.data) });
    };
    return { ctx, els, signals, calls };
}

const OPTIONS = { publicKey: { challenge: "c1", rpId: "example.test", allowCredentials: [] }, ceremony: "cer-1" };
const assertion = { toJSON: () => ({ id: "cred-abc", rawId: "cred-abc", type: "public-key", response: {} }) };

test("sign-in: hidden when the service has no passkey address (409)", async () => {
    const { ctx, els } = loginPage(() => ({ status: 409, data: { detail: "Passkeys are not set up" } }), () => Promise.reject(new Error("no")));
    ctx.authPasskeyOffer();
    await settle();
    assert.ok(els["pk-login"].classList.contains("hidden"));
    assert.ok(els["pk-note"].classList.contains("hidden"));
});

test("sign-in: shown when set up; outside HTTPS a note says why instead", async () => {
    const shown = loginPage(() => ({ status: 200, data: OPTIONS }), () => Promise.reject(new Error("no")));
    shown.ctx.authPasskeyOffer();
    await settle();
    assert.ok(!shown.els["pk-login"].classList.contains("hidden"));

    const insecure = loginPage(() => ({ status: 200, data: OPTIONS }), () => Promise.reject(new Error("no")));
    insecure.ctx.isSecureContext = false;
    insecure.ctx.authPasskeyOffer();
    await settle();
    assert.ok(insecure.els["pk-login"].classList.contains("hidden"));
    assert.equal(insecure.els["pk-note"].textContent, "Passkeys need HTTPS (or localhost).");
});

test("sign-in: new options on the click, the JSON credential with its ceremony, then the safe next page", async () => {
    const { ctx, calls } = loginPage((path) => (path.endsWith("/options") ? { status: 200, data: OPTIONS } : { status: 200, data: { ok: true, csrf: "csrf-1" } }), () => Promise.resolve(assertion));
    ctx.authPasskeySignIn();
    await settle();
    assert.deepEqual(calls.map((c) => c[0]), ["/admin/api/login/passkey/options", "get", "/admin/api/login/passkey"]);
    assert.deepEqual(JSON.parse(JSON.stringify(calls[1][1].publicKey)), { parsed: "request", json: OPTIONS.publicKey });
    assert.deepEqual(JSON.parse(JSON.stringify(calls[2][1])), { credential: { id: "cred-abc", rawId: "cred-abc", type: "public-key", response: {} }, ceremony: "cer-1" });
    assert.equal(ctx.location.href, "/admin/zones");
});

test("sign-in: an unknown passkey (404) is signalled with the server's rpId and the base64url id", async () => {
    const { ctx, els, signals } = loginPage((path) => (path.endsWith("/options") ? { status: 200, data: OPTIONS }
        : { status: 404, data: { detail: "This passkey is not registered here." } }), () => Promise.resolve(assertion));
    ctx.authPasskeySignIn();
    await settle();
    assert.deepEqual(JSON.parse(JSON.stringify(signals)), [{ rpId: "example.test", credentialId: "cred-abc" }]);
    assert.equal(els.err.textContent, "This passkey is not registered here.");
    assert.equal(ctx.location.href, "");
});

test("sign-in: other refusals show the server's words and signal nothing", async () => {
    for (const status of [400, 401, 409, 429]) {
        const { ctx, els, signals } = loginPage((path) => (path.endsWith("/options") ? { status: 200, data: OPTIONS }
            : { status, data: { detail: "reason " + status } }), () => Promise.resolve(assertion));
        ctx.authPasskeySignIn();
        await settle();
        assert.equal(signals.length, 0, String(status));
        assert.equal(els.err.textContent, "reason " + status);
        assert.ok(!els["pk-signin"].disabled, "the button comes back");
    }
});

test("sign-in: a closed browser window shows nothing and sends nothing", async () => {
    const { ctx, els, calls } = loginPage(() => ({ status: 200, data: OPTIONS }), () => Promise.reject(domError("NotAllowedError")));
    ctx.authPasskeySignIn();
    await settle();
    assert.ok(els.err.classList.contains("hidden"));
    assert.deepEqual(calls.map((c) => c[0]), ["/admin/api/login/passkey/options", "get"]);
    assert.ok(!els["pk-signin"].disabled);
});

// --- js/dashboard.js -------------------------------------------------------

function dashboard(handler, win) {
    const w = win || browser();
    const ctx = loadScripts(["js/text.js", "js/common.js", "js/ui.js", "js/dashboard.js"], {
        document: PAGE, isSecureContext: w.isSecureContext, PublicKeyCredential: w.PublicKeyCredential, navigator: w.navigator,
        location: { origin: "https://auth.example.test", pathname: "/admin/settings", search: "" },
    });
    ctx.toasts = [];
    ctx.showToast = (message, type) => { ctx.toasts.push([message, type]); };
    const calls = [];
    ctx.adminJson = (path, opts) => {
        calls.push({ path, method: (opts && opts.method) || "GET", body: opts && opts.body, csrf: opts && opts.csrf });
        const a = handler(path, opts) || { status: 200, data: {} };
        return Promise.resolve({ ok: a.status < 400, status: a.status, data: a.data, json: true, error: "" });
    };
    return { ctx, calls };
}

test("the passkey check before a reveal: options, the browser, then { passkey, ceremony } and no code", async () => {
    const w = browser();
    w.navigator = { credentials: { get: () => Promise.resolve(assertion) } };
    const { ctx, calls } = dashboard(() => ({ status: 200, data: OPTIONS }), w);
    ctx.dash.csrf = "csrf-x";
    const r = await ctx.dashPkStepUp();
    assert.deepEqual(JSON.parse(JSON.stringify(r)), { passkey: { id: "cred-abc", rawId: "cred-abc", type: "public-key", response: {} }, ceremony: "cer-1" });
    assert.equal(calls[0].path, "/admin/api/passkeys/stepup/options");
    assert.equal(calls[0].method, "POST");
    assert.equal(calls[0].csrf, "csrf-x");
});

test("the passkey check: a closed window is quiet, no usable passkey (409) shows the reason", async () => {
    const w = browser();
    w.navigator = { credentials: { get: () => Promise.reject(domError("NotAllowedError")) } };
    const quiet = dashboard(() => ({ status: 200, data: OPTIONS }), w);
    assert.deepEqual(JSON.parse(JSON.stringify(await quiet.ctx.dashPkStepUp())), { error: "" });

    const none = dashboard(() => ({ status: 409, data: { detail: "You have no passkey for this address. Use your authenticator code." } }), w);
    const r = await none.ctx.dashPkStepUp();
    assert.equal(r.error, "You have no passkey for this address. Use your authenticator code.");
});

test("Use a passkey is offered only with a passkey made for the current address", async () => {
    const list = (usable) => ({ address: { origin: "https://auth.example.test", rpId: "auth.example.test", version: 1, locked: false },
        passkeys: [{ id: "p1", name: "Phone", usable, rpId: usable ? "auth.example.test" : "old.example.test", backedUp: true }] });
    const yes = dashboard(() => ({ status: 200, data: list(true) }));
    await yes.ctx.dashPkLoad(true);
    assert.equal(yes.ctx.dashPkUsable(), true);
    const no = dashboard(() => ({ status: 200, data: list(false) }));
    await no.ctx.dashPkLoad(true);
    assert.equal(no.ctx.dashPkUsable(), false);
    const html = no.ctx.dashPkRowHtml(no.ctx.dashPk.data.passkeys[0]);
    assert.ok(html.includes("Not usable at this address (made for old.example.test)"));
    assert.ok(html.includes("Synced"));
});

function address(version, origin, locked) {
    return { origin, rpId: origin ? new URL(origin).hostname : null, version, locked: !!locked, envName: "PASSKEY_ORIGIN", savedBy: "admin", savedAt: null };
}

test("passkey address: the save sends only baseVersion and origin, on top of the version read", async () => {
    const { ctx, calls } = dashboard((path, opts) => (opts && opts.method === "PUT" ? { status: 200, data: address(4, "https://auth.example.test:8443") } : null));
    const s = ctx.dashPkSet;
    s.snap = s.base = address(3, "https://auth.example.test");
    s.draft = "https://auth.example.test:8443";
    s.dirty = true;
    let asked = 0;
    ctx.uiConfirm = () => { asked++; return Promise.resolve(true); };
    ctx.dashPkSetSave();
    await settle();
    assert.equal(asked, 0, "the same host name keeps the passkeys usable: no question");
    const put = calls.find((c) => c.method === "PUT");
    assert.equal(put.path, "/admin/api/settings/passkeys");
    assert.deepEqual(JSON.parse(JSON.stringify(put.body)), { baseVersion: 3, origin: "https://auth.example.test:8443" });
    assert.equal(s.base.version, 4);
    assert.equal(s.dirty, false);
});

test("passkey address: removing it or moving to another host asks first; No sends nothing", async () => {
    for (const draft of ["", "https://other.example.test"]) {
        const { ctx, calls } = dashboard(() => null);
        const s = ctx.dashPkSet;
        s.snap = s.base = address(3, "https://auth.example.test");
        s.draft = draft;
        s.dirty = true;
        const questions = [];
        ctx.uiConfirm = (o) => { questions.push(o); return Promise.resolve(false); };
        ctx.dashPkSetSave();
        await settle();
        assert.equal(questions.length, 1, JSON.stringify(draft));
        assert.equal(questions[0].danger, true);
        assert.equal(calls.filter((c) => c.method === "PUT").length, 0);
    }
});

test("passkey address: a conflict (409) keeps the draft and offers Reload; a refresh does not overwrite it", async () => {
    const answers = [{ status: 409, data: { detail: "Someone else changed the passkey address after you opened the page. Reload and try again." } }];
    const { ctx } = dashboard((path, opts) => (opts && opts.method === "PUT" ? answers.shift() : { status: 200, data: address(5, "https://new.example.test") }));
    const s = ctx.dashPkSet;
    s.snap = s.base = address(3, null);
    s.draft = "https://auth.example.test";
    s.dirty = true;
    ctx.dashPkSetSave();
    await settle();
    assert.equal(s.stale, true);
    assert.match(s.error, /Reload and try again/);
    await ctx.dashPkSetLoad();
    assert.equal(s.draft, "https://auth.example.test");
    assert.equal(s.base.version, 3);
    assert.equal(s.snap.version, 5);
});

test("sign-in: a probe that fails for another reason keeps the button; the click reports the failure", async () => {
    const { ctx, els } = loginPage(() => ({ status: 503, data: { detail: "Service unavailable" } }), () => Promise.reject(new Error("no")));
    ctx.authPasskeyOffer();
    await settle();
    assert.ok(!els["pk-login"].classList.contains("hidden"));
    ctx.authPasskeySignIn();
    await settle();
    assert.equal(els.err.textContent, "Service unavailable");
});

test("passkey address form: locked by PASSKEY_ORIGIN shows the variable and no field or Save; unlocked escapes the draft", () => {
    const { ctx } = dashboard(() => null);
    const s = ctx.dashPkSet;
    s.snap = s.base = address(2, "https://auth.example.test", true);
    s.draft = "https://auth.example.test";
    const locked = ctx.dashPkSetFormHtml();
    assert.ok(locked.includes("<code>PASSKEY_ORIGIN</code> environment variable (https://auth.example.test)"));
    assert.ok(!locked.includes('id="set-pk-origin"'));
    assert.ok(!locked.includes('id="set-pk-save"'));
    assert.ok(locked.includes("Passkeys made for another address stop working"));

    s.snap = s.base = address(0, null);
    s.draft = '"><img src=x>';
    const open = ctx.dashPkSetFormHtml();
    assert.ok(open.includes('id="set-pk-origin"'));
    assert.ok(open.includes('value="&quot;&gt;&lt;img src=x&gt;"'));
    assert.ok(!open.includes("<img"));
    assert.ok(open.includes("Not set yet"));
    assert.ok(open.includes('id="set-pk-save"'));
});

test("Account page notes: HTTPS, old browser, no address, another address, locked", () => {
    const { ctx } = dashboard(() => null);
    const here = "https://auth.example.test";
    const data = (origin, locked) => ({ address: address(1, origin, locked), passkeys: [] });
    assert.ok(ctx.dashPkInfoHtml(null, "insecure", here).includes("Passkeys need HTTPS (or localhost)."));
    assert.ok(ctx.dashPkInfoHtml(null, "browser", here).includes("This browser cannot use passkeys here."));
    const none = ctx.dashPkInfoHtml(data(null), "", here);
    assert.ok(none.includes("No passkey address yet"));
    assert.ok(none.includes("<code>https://auth.example.test</code>"));
    const other = ctx.dashPkInfoHtml(data("https://other.example.test"), "", here);
    assert.ok(other.includes("This page is open at another address"));
    assert.ok(other.includes("<code>https://other.example.test</code>"));
    const locked = ctx.dashPkInfoHtml(data(here, true), "", here);
    assert.ok(locked.includes("<code>PASSKEY_ORIGIN</code> environment variable sets it"));
    assert.ok(!locked.includes("is-warn"));
    const open = ctx.dashPkInfoHtml(data(here, false), "", here);
    assert.ok(open.includes('data-go="settings"'));
});

test("Activity Log: passkey sign-ins, reveals, passkey actions and the address in words, escaped", () => {
    const { ctx } = dashboard(() => null);
    const d = (action, details, entityName) => ctx.dashLogDetails({ action, details, entityName });
    assert.equal(d("login", { method: "passkey", passkey: "<b>Phone</b>" }), '<b>Signed in with:</b> passkey (<bdi dir="auto">&lt;b&gt;Phone&lt;/b&gt;</bdi>)');
    assert.equal(d("token_reveal", { method: "passkey" }), "<b>Confirmed with:</b> Passkey");
    assert.equal(d("api_key_reveal", { method: "code" }), "<b>Confirmed with:</b> Authenticator code");
    assert.equal(d("passkey_added", { rpId: "auth.example.test" }), "<b>Made for:</b> auth.example.test");
    assert.equal(d("passkey_renamed", { from: "Old" }), '<b>Renamed from:</b> <bdi dir="auto">Old</bdi>');
    assert.equal(d("passkey_failed", { where: "token_reveal", reason: "invalid" }), "<b>Where:</b> Copy token<br><b>Reason:</b> The passkey could not be verified");
    assert.equal(d("passkey_failed", { where: "<x>", reason: "constructor" }), "<b>Where:</b> &lt;x&gt;<br><b>Reason:</b> constructor");
    assert.equal(d("settings.update", { from: { origin: null }, to: { origin: "https://a.example.test" } }, "passkeys"), "<b>Passkey address set to:</b> https://a.example.test");
    assert.equal(d("settings.update", { from: { origin: "https://a.example.test" }, to: { origin: null } }, "passkeys"), "<b>Passkey address removed.</b> It was https://a.example.test");
    assert.equal(d("settings.update", { from: { origin: "https://a.example.test" }, to: { origin: "https://b.example.test" } }, "passkeys"),
        "<b>Passkey address changed</b> from https://a.example.test to https://b.example.test");
    // A password sign-in has no details; other settings rows stay as data
    assert.equal(d("login", null), '<span class="ui-muted">-</span>');
    assert.ok(d("settings.update", { from: { maxReq: 1 }, to: { maxReq: 2 } }, "rate_limit").startsWith('<code class="ui-mono">'));
});
