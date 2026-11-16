// The Settings page's data layer (js/dashboard.js) against a fake API whose answers
// arrive in an order the test chooses: the save units, the one save bar over them,
// the questions the server asks, secrets on demand, and the database connection
// change with its lock, its lost answers and the database context change.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { loadScripts } = require("./_load.cjs");

const PAGE = {
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    readyState: undefined,
};

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function settle(n) {
    for (let i = 0; i < (n || 8); i++) await tick();
}

// The page's scripts; questions answer from `answers` (true = confirm), toasts are kept
function load(extra) {
    const ctx = loadScripts(["js/text.js", "js/common.js", "js/ui.js", "js/dashboard.js"], Object.assign({ document: PAGE }, extra || {}));
    // Words the writer has not filled yet read as their key here, so a message can be told apart
    for (const [k, v] of Object.entries(ctx.UI_TEXT)) {
        if (v === "") ctx.UI_TEXT[k] = k;
        else if (typeof v === "object" && v.one === "") ctx.UI_TEXT[k] = { one: k, other: k };
    }
    ctx.toasts = [];
    ctx.showToast = (message, type) => { ctx.toasts.push([message, type]); };
    ctx.questions = [];
    ctx.answers = [];
    ctx.uiConfirm = (o) => { ctx.questions.push(o); return Promise.resolve(ctx.answers.length ? ctx.answers.shift() : true); };
    return ctx;
}

// Each request waits until the test answers it
function deferredApi(ctx) {
    const pending = [];
    ctx.adminJson = (path, opts) => new Promise((resolve) => {
        pending.push({
            path, method: (opts && opts.method) || "GET", body: opts && opts.body,
            answer: (status, data, error) => resolve({ ok: status >= 200 && status < 400, status, data, json: true, error: error || "" }),
        });
    });
    return pending;
}

// Answer the next request that matches; fails when none is waiting
async function answerNext(pending, method, path, status, data, error) {
    await settle();
    const i = pending.findIndex((p) => !p.done && p.method === method && (!path || p.path === path || (path instanceof RegExp && path.test(p.path))));
    assert.ok(i >= 0, "no " + method + " " + path + " waiting: " + JSON.stringify(pending.filter((p) => !p.done).map((p) => p.method + " " + p.path)));
    pending[i].done = true;
    pending[i].answer(status, data, error);
    await settle();
    return pending[i];
}

function open(pending) {
    return pending.filter((p) => !p.done);
}

// Objects made inside the page's context compare by value only after a copy
function plain(v) {
    return JSON.parse(JSON.stringify(v));
}

// ---------------------------------------------------------------------------
// Server views
// ---------------------------------------------------------------------------

function rate(version, maxReq) {
    return {
        version: version, savedBy: version ? "admin" : null, savedAt: null, savedFrom: null,
        fields: {
            maxReq: { value: maxReq, source: version ? "stored" : "default", locked: false, envName: "RATE_LIMIT_MAX_REQ", min: 1, max: 1000000, default: 120 },
            windowMs: { value: 60000, source: "default", locked: false, envName: "RATE_LIMIT_WINDOW_MS", minSeconds: 1, maxSeconds: 3600, default: 60000 },
            exempt: { value: [], source: "default", locked: false, max: 100 },
        },
        applied: { version: version, lastReload: null, lastReloadError: null },
        yourAddress: "10.0.0.1",
    };
}

function general(version, over) {
    const v = Object.assign({ timeZone: "UTC", cookieSecure: false, activity: 30, access: 30 }, over || {});
    const src = version ? "stored" : "default";
    return {
        version: version, savedBy: version ? "admin" : null, savedAt: null, savedFrom: null,
        fields: {
            timeZone: { value: v.timeZone, source: src, locked: false, envName: "TZ", envInvalid: false, default: "UTC" },
            cookieSecure: { value: v.cookieSecure, source: src, locked: false, envName: "COOKIE_SECURE", default: false },
            activityLogRetentionDays: { value: v.activity, source: src, locked: false, envName: "ACTIVITY_LOG_RETENTION_DAYS", min: 1, max: 3650, default: 30 },
            accessLogRetentionDays: { value: v.access, source: src, locked: false, envName: "ACCESS_LOG_RETENTION_DAYS", min: 1, max: 3650, default: 30 },
        },
        timeZones: ["Asia/Jerusalem", "Europe/London", "UTC"],
        applied: { version: version, lastReload: null, lastReloadError: null },
        purge: { activity: { state: "ok", reason: null, lastRun: null }, access: { state: "ok", reason: null, lastRun: null } },
        rootPath: { envName: "ROOT_PATH", value: null },
    };
}

function display(version, date, time, over) {
    const f = (name, value, def, env, options) => Object.assign({ value, source: version ? "stored" : "default", locked: false, envName: env, envInvalid: false, default: def, options }, (over || {})[name] || {});
    return {
        version, savedBy: version ? "admin" : null, savedAt: null, storedInvalid: false,
        fields: {
            dateFormat: f("dateFormat", date || "browser", "browser", "DATE_FORMAT", ["browser", "DD/MM/YYYY", "MM/DD/YYYY", "DD.MM.YYYY", "YYYY-MM-DD"]),
            timeFormat: f("timeFormat", time || "24h", "24h", "TIME_FORMAT", ["24h", "12h", "browser"]),
        },
        applied: { version, lastReload: null, lastReloadError: null },
    };
}

function redisView(version, over) {
    return Object.assign({
        version: version, savedBy: version ? "admin" : null, savedAt: null, savedFrom: null,
        url: { connected: "redis://****@redis:6379/0", saved: version ? "redis://****@cache:6379/0" : null, source: version ? "stored" : "default",
            locked: false, envName: "REDIS_URL", syncPending: false },
        restartNeeded: !!version,
    }, over || {});
}

function pkAddress(version, origin, locked) {
    return { origin, rpId: origin ? new URL(origin).hostname : null, version, locked: !!locked, envName: "PASSKEY_ORIGIN", savedBy: "admin", savedAt: null };
}

function dbView(job) {
    return {
        source: "file", locked: false, envName: "DATABASE_URL", job: job || null, sslmodes: ["prefer"], editable: true,
        connection: { backend: "postgres", host: "db", port: 5432, database: "auth", user: "auth", sslmode: "prefer", caPem: false },
    };
}

// A unit with its view loaded and its draft taken from it
function start(ctx, name, view) {
    const p = ctx.DASH_SET_PANELS[name];
    p.st.snap = p.st.baseSnap = view;
    p.st.draft = p.draftFrom(view);
    p.st.dirty = false;
    return p.st;
}

function editGeneral(ctx, changes) {
    Object.assign(ctx.dashGen.draft, changes);
    ctx.dashGenDraftChanged();
}

function newUrl() {
    // A password made for this run only
    return "redis://:" + crypto.randomBytes(24).toString("hex") + "@cache:6379/0";
}

// ---------------------------------------------------------------------------
// One unit: refresh, conflict, fields, questions
// ---------------------------------------------------------------------------

test("a refresh that started during a save cannot undo the save's result", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const s = start(ctx, "rate", rate(0, 120));
    s.draft.maxReq = "5";
    const saving = ctx.dashSettingsSave();
    const refreshing = ctx.dashSettingsLoad();
    const [put, get] = pending;
    assert.equal(put.method, "PUT");
    assert.equal(get.method, "GET");
    assert.equal(put.body.maxReq, 5);
    assert.equal(put.body.baseVersion, 0);
    put.answer(200, rate(1, 5));
    assert.equal(await saving, "saved");
    get.answer(200, rate(0, 120)); // read before the save committed, answered after it
    await refreshing;
    assert.equal(s.baseSnap.version, 1);
    assert.equal(s.snap.version, 1);
    assert.equal(s.draft.maxReq, "5");
    assert.equal(s.dirty, false);
});

test("a conflict keeps the draft and its base until the admin chooses", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const s = start(ctx, "rate", rate(0, 120));
    s.draft.maxReq = "5";
    const saving = ctx.dashSettingsSave();
    pending[0].answer(409, { detail: "Someone else changed these settings after you opened the page.", code: "conflict", current: rate(1, 300) });
    assert.equal(await saving, "conflict");
    assert.equal(s.conflict.version, 1);
    assert.equal(s.baseSnap.version, 0);
    assert.equal(s.draft.maxReq, "5");
    // A refresh while the conflict is open shows the new version but keeps the draft and its base
    const refreshing = ctx.dashSettingsLoad();
    pending[1].answer(200, rate(1, 300));
    await refreshing;
    assert.equal(s.baseSnap.version, 0);
    assert.equal(s.draft.maxReq, "5");
});

// The rate limit form drawn into a stand-in for its container
function drawWithProxy(value) {
    const body = { innerHTML: "" };
    const page = { ...PAGE, getElementById: (id) => (id === "settings-body" ? body : null) };
    const ctx = load({ document: page });
    const s = start(ctx, "rate", rate(0, 120));
    s.snap.proxyNetwork = { envName: "FORWARDED_ALLOW_IPS", value: value };
    ctx.dashSettingsDrawForm();
    // Catalog text is escaped as a whole in markup: read an apostrophe as the page shows it
    return body.innerHTML.replace(/&#39;/g, "'");
}

const WARNING = 'id="set-exempt-warn"';

test("a loopback-only FORWARDED_ALLOW_IPS keeps the proxy hint and the exempt warning", () => {
    for (const value of ["127.0.0.1,::1", "::1, 127.0.0.1", " 127.0.0.1", "::1", "127.0.0.1,,::1"]) {
        const html = drawWithProxy(value);
        assert.ok(html.includes("loopback addresses only"), value);
        assert.ok(html.includes(WARNING), value);
    }
});

test("a proxy address hides the exempt warning; unset, empty and * keep their messages", () => {
    const proxy = drawWithProxy("172.18.0.0/16");
    assert.ok(proxy.includes("<code>172.18.0.0/16</code>"));
    assert.ok(!proxy.includes(WARNING));
    assert.ok(!drawWithProxy("127.0.0.1,172.18.0.5").includes(WARNING));
    assert.ok(drawWithProxy("*").includes("<code>*</code>"));
    const unset = drawWithProxy(null);
    assert.ok(unset.includes("is not set") && unset.includes(WARNING));
    const empty = drawWithProxy("");
    assert.ok(empty.includes("to an empty value") && empty.includes(WARNING));
});

test("General sends only the changed fields, as numbers and a boolean", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = start(ctx, "general", general(3));
    editGeneral(ctx, { timeZone: "Europe/London", accessLogRetentionDays: " 60 " });
    assert.equal(g.dirty, true);
    assert.equal(ctx.dashSettingsCount(), 2);
    const saving = ctx.dashGenSave();
    assert.deepEqual(plain(pending[0].body), { baseVersion: 3, timeZone: "Europe/London", accessLogRetentionDays: 60 });
    pending[0].answer(200, general(4, { timeZone: "Europe/London", access: 60 }));
    assert.equal(await saving, "saved");
    assert.equal(g.dirty, false);
    assert.equal(g.baseSnap.version, 4);
    editGeneral(ctx, { accessLogRetentionDays: "60" });
    assert.equal(g.dirty, false, "the saved value is not a change");
});

test("a lower retention is confirmed in the shared question with exactly the values the server named", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = start(ctx, "general", general(2));
    editGeneral(ctx, { activityLogRetentionDays: "7" });
    const confirm = { activityLogRetentionDays: 7, accessLogRetentionDays: 30 };
    const saving = ctx.dashGenSave();
    assert.equal(pending[0].body.confirmPurge, undefined);
    pending[0].answer(409, { code: "confirm_purge", detail: "This deletes older log entries.", estimate: { activityLogs: 412, accessLogs: 0 }, confirm });
    await settle();
    assert.equal(ctx.questions.length, 1);
    assert.match(ctx.questions[0].message, /412 activity log entries/);
    assert.doesNotMatch(ctx.questions[0].message, /access log entr/);
    assert.equal(ctx.questions[0].danger, true);
    assert.deepEqual(plain(pending[1].body.confirmPurge), confirm);
    assert.equal(pending[1].body.activityLogRetentionDays, 7);
    pending[1].answer(200, general(3, { activity: 7 }));
    assert.equal(await saving, "saved");
    assert.equal(g.askPurge, null);
});

test("Cancel on a question sends nothing more, and the draft stays", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = start(ctx, "general", general(2));
    editGeneral(ctx, { activityLogRetentionDays: "7" });
    ctx.answers.push(false);
    const saving = ctx.dashGenSave();
    pending[0].answer(409, { code: "confirm_purge", detail: "x", estimate: {}, confirm: { activityLogRetentionDays: 7, accessLogRetentionDays: 30 } });
    assert.equal(await saving, "cancelled");
    await settle();
    assert.equal(pending.length, 1, "nothing was sent after Cancel");
    assert.equal(g.askPurge, null);
    assert.equal(g.draft.activityLogRetentionDays, "7");
    assert.equal(g.dirty, true);
});

test("a value change or a conflict clears a confirmation", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = start(ctx, "general", general(2));
    editGeneral(ctx, { activityLogRetentionDays: "7" });
    // The question is open while the admin changes the value it names: nothing is sent
    let release;
    ctx.uiConfirm = (o) => { ctx.questions.push(o); return new Promise((r) => { release = r; }); };
    const saving = ctx.dashGenSave();
    await answerNext(pending, "PUT", null, 409, { code: "confirm_purge", detail: "x", estimate: {}, confirm: { activityLogRetentionDays: 7, accessLogRetentionDays: 30 } });
    editGeneral(ctx, { accessLogRetentionDays: "20" });
    assert.equal(g.askPurge, null);
    release(true);
    assert.equal(await saving, "cancelled");
    assert.equal(pending.length, 1);

    // A conflict drops what was confirmed
    ctx.uiConfirm = (o) => { ctx.questions.push(o); return Promise.resolve(true); };
    const again = ctx.dashGenSave();
    await answerNext(pending, "PUT", "/admin/api/settings/general", 409, { code: "confirm_purge", detail: "x", estimate: {},
        confirm: { activityLogRetentionDays: 7, accessLogRetentionDays: 20 } });
    assert.equal(open(pending)[0].body.confirmPurge.accessLogRetentionDays, 20);
    await answerNext(pending, "PUT", "/admin/api/settings/general", 409, { code: "conflict", current: general(3, { activity: 90 }) });
    assert.equal(await again, "conflict");
    assert.equal(g.askPurge, null);
    ctx.dashPanelKeep(ctx.DASH_SET_PANELS.general);
    assert.equal(g.baseSnap.version, 3);
    ctx.dashGenSave();
    assert.equal(open(pending)[0].body.confirmPurge, undefined);
    assert.equal(open(pending)[0].body.baseVersion, 3);
});

test("two questions in a row are saved together", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    start(ctx, "general", general(1));
    editGeneral(ctx, { cookieSecure: true, accessLogRetentionDays: "10" });
    const confirm = { activityLogRetentionDays: 30, accessLogRetentionDays: 10 };
    const saving = ctx.dashGenSave();
    await answerNext(pending, "PUT", null, 409, { code: "confirm_purge", detail: "x", estimate: { activityLogs: 0, accessLogs: 5 }, confirm });
    assert.deepEqual(plain(open(pending)[0].body.confirmPurge), confirm);
    assert.equal(open(pending)[0].body.confirmCookieSecure, undefined);
    await answerNext(pending, "PUT", null, 409, { code: "confirm_cookie_secure", detail: "Browsers send a Secure cookie only over HTTPS." });
    assert.equal(ctx.questions.length, 2);
    assert.equal(ctx.questions[1].message, "Browsers send a Secure cookie only over HTTPS.");
    const last = open(pending)[0];
    assert.deepEqual(plain(last.body.confirmPurge), confirm);
    assert.equal(last.body.confirmCookieSecure, true);
    await answerNext(pending, "PUT", null, 200, general(2, { cookieSecure: true, access: 10 }));
    assert.equal(await saving, "saved");
});

test("the Secure cookie question is bound to turning it on", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = start(ctx, "general", general(1));
    editGeneral(ctx, { cookieSecure: true });
    ctx.answers.push(false);
    let saving = ctx.dashGenSave();
    pending[0].answer(409, { code: "confirm_cookie_secure", detail: "x" });
    assert.equal(await saving, "cancelled");
    editGeneral(ctx, { cookieSecure: false });
    editGeneral(ctx, { cookieSecure: true });
    saving = ctx.dashGenSave();
    assert.equal(pending[1].body.confirmCookieSecure, undefined);
    assert.equal(g.askCookie, null);
});

test("Discard after a conflict takes the other admin's version", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = start(ctx, "general", general(2));
    editGeneral(ctx, { timeZone: "Asia/Jerusalem" });
    const saving = ctx.dashGenSave();
    pending[0].answer(409, { code: "conflict", current: general(3, { timeZone: "Europe/London" }) });
    await saving;
    ctx.dashPanelDiscard(ctx.DASH_SET_PANELS.general);
    assert.equal(g.baseSnap.version, 3);
    assert.equal(g.draft.timeZone, "Europe/London");
    assert.equal(g.dirty, false);
});

test("a saved time zone becomes this tab's zone; a refresh does not change it", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    ctx.uiSetAppZone("UTC");
    start(ctx, "general", general(1));
    const refreshing = ctx.dashGenLoad();
    pending[0].answer(200, general(2, { timeZone: "Europe/London" }));
    await refreshing;
    assert.equal(ctx.uiAppZone, "UTC");
    editGeneral(ctx, { timeZone: "Asia/Jerusalem" });
    const saving = ctx.dashGenSave();
    pending[1].answer(200, general(3, { timeZone: "Asia/Jerusalem" }));
    await saving;
    assert.equal(ctx.uiAppZone, "Asia/Jerusalem");
    assert.equal(ctx.dash.zoneAsked, "Asia/Jerusalem");
});

test("a locked field is never a change, and an invalid TZ is called out", () => {
    const ctx = load();
    const s = general(1);
    s.fields.timeZone = { value: "UTC", source: "env", locked: true, envName: "TZ", envInvalid: true, default: "UTC" };
    start(ctx, "general", s);
    editGeneral(ctx, { timeZone: "Europe/London" });
    assert.equal(ctx.dashGen.dirty, false);
    assert.equal(ctx.dashSettingsCount(), 0);
    const alerts = [];
    ctx.dashGenAlerts(s, alerts);
    assert.match(alerts.join(""), /TZ is not a time zone/);
});

test("a skipped purge shows its reason", () => {
    const ctx = load();
    const s = general(1);
    s.purge.access = { state: "skipped", reason: "This process uses 30 days, but 60 are saved.", lastRun: null };
    const alerts = [];
    ctx.dashGenAlerts(s, alerts);
    const html = alerts.join("");
    assert.match(html, /Old access log entries were not deleted/);
    assert.match(html, /but 60 are saved\./);
});

test("changed fields carry the Changed mark and Reset names the default; locked ones a lock naming the variable", () => {
    const ctx = load();
    assert.equal(ctx.dashSetMarks("x", false, "30 days"), "");
    const marks = ctx.dashSetMarks("activityLogRetentionDays", true, "30 days");
    assert.match(marks, /data-set-reset="activityLogRetentionDays"/);
    assert.match(marks, /ui-set-pill is-changed/);
    const lock = ctx.dashSetLockPill({ envName: "TZ" }, "Asia/Jerusalem");
    assert.match(lock, /TZ/);
    assert.match(lock, /environment variable \(Asia\/Jerusalem\)\. Remove it there to change the value here\./);
    // Reset puts the default into the draft
    start(ctx, "general", general(1, { activity: 90 }));
    ctx.dashGenReset("activityLogRetentionDays");
    assert.equal(ctx.dashGen.draft.activityLogRetentionDays, "30");
    assert.equal(ctx.dashGen.dirty, true);
});

test("the Settings page refresh reads every save unit", async () => {
    const ctx = load();
    const paths = [];
    ctx.adminJson = (path) => { paths.push(path); return Promise.resolve({ ok: false, status: 500, data: null, json: false, error: "" }); };
    await ctx.dashSettingsLoadAll();
    assert.deepEqual(paths.sort(), ["/admin/api/settings/display", "/admin/api/settings/general", "/admin/api/settings/passkeys",
        "/admin/api/settings/rate-limit", "/admin/api/settings/redis"]);
});

// ---------------------------------------------------------------------------
// Display (FW-9): its own unit, its own version
// ---------------------------------------------------------------------------

test("Display sends only its changed format and applies it to this tab", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    ctx.uiSetDatePrefs("browser", "24h");
    const d = start(ctx, "display", display(2));
    d.draft.dateFormat = "DD.MM.YYYY";
    ctx.dashDispInput();
    assert.equal(d.dirty, true);
    const saving = ctx.dashDispSave();
    assert.deepEqual(plain(pending[0].body), { baseVersion: 2, dateFormat: "DD.MM.YYYY" });
    assert.equal(pending[0].path, "/admin/api/settings/display");
    pending[0].answer(200, display(3, "DD.MM.YYYY"));
    assert.equal(await saving, "saved");
    assert.equal(ctx.uiDatePrefs.date, "DD.MM.YYYY");
    assert.equal(ctx.formatDay(Date.UTC(2026, 9, 23, 12), { zone: "UTC" }), "23.10.2026");
});

test("a locked format is not a change; an unknown environment value is called out", () => {
    const ctx = load();
    const view = display(1, "browser", "24h", { timeFormat: { locked: true, source: "env", envInvalid: true } });
    const d = start(ctx, "display", view);
    ctx.dashDispPickTime("12h");
    assert.equal(d.dirty, false);
    const alerts = [];
    ctx.dashDispAlerts(view, alerts);
    assert.equal(alerts.length, 2);
    assert.match(alerts[0], /is-warn/);
});

// ---------------------------------------------------------------------------
// The save bar: every unit in a fixed order, each reports for itself
// ---------------------------------------------------------------------------

test("partial save: General saved and Display refused (409); each keeps only its own draft", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = start(ctx, "general", general(1));
    const d = start(ctx, "display", display(1));
    editGeneral(ctx, { timeZone: "Europe/London" });
    d.draft.timeFormat = "12h";
    ctx.dashDispInput();
    assert.equal(ctx.dashSettingsCount(), 2);
    const run = ctx.dashSettingsSaveAll();
    // General first; Display only after General answered
    await settle();
    assert.equal(open(pending).length, 1);
    await answerNext(pending, "PUT", "/admin/api/settings/general", 200, general(2, { timeZone: "Europe/London" }));
    await answerNext(pending, "PUT", "/admin/api/settings/display", 409, { code: "conflict", detail: "changed", current: display(2, "YYYY-MM-DD") });
    const results = plain(await run);
    assert.deepEqual(results, { general: "saved", display: "conflict" });
    assert.equal(g.dirty, false);
    assert.equal(d.draft.timeFormat, "12h");
    assert.equal(d.conflict.version, 2);
    assert.equal(ctx.dashSettingsCount(), 1);
    assert.equal(ctx.toasts.length, 1);
    assert.equal(ctx.toasts[0][1], "error");
    assert.equal(ctx.toasts[0][0], ctx.t("settings.savebar.saved-and-failed", { saved: ctx.t("settings.general"), failed: ctx.t("settings.display") }));
});

test("partial save the other way: Display saved and General refused; General keeps its draft", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = start(ctx, "general", general(1));
    const d = start(ctx, "display", display(1));
    editGeneral(ctx, { accessLogRetentionDays: "45" });
    d.draft.dateFormat = "YYYY-MM-DD";
    ctx.dashDispInput();
    const run = ctx.dashSettingsSaveAll();
    await answerNext(pending, "PUT", "/admin/api/settings/general", 409, { code: "conflict", current: general(2, { access: 60 }) });
    await answerNext(pending, "PUT", "/admin/api/settings/display", 200, display(2, "YYYY-MM-DD"));
    assert.deepEqual(plain(await run), { general: "conflict", display: "saved" });
    assert.equal(d.dirty, false);
    assert.equal(g.draft.accessLogRetentionDays, "45");
    assert.equal(g.baseSnap.version, 1);
    assert.equal(ctx.dashSettingsCount(), 1);
});

test("the save bar saves the units in order: General, Display, Rate limit, Passkeys, Redis", async () => {
    const ctx = load({ location: { origin: "https://auth.example.test", pathname: "/admin/settings", search: "", hash: "" } });
    const pending = deferredApi(ctx);
    start(ctx, "general", general(1));
    start(ctx, "display", display(1));
    start(ctx, "rate", rate(1, 120));
    start(ctx, "passkeys", pkAddress(1, "https://auth.example.test"));
    start(ctx, "redis", redisView(1));
    editGeneral(ctx, { timeZone: "Europe/London" });
    ctx.dashDisp.draft.timeFormat = "12h";
    ctx.dashSet.draft.maxReq = "50";
    ctx.dashPkSet.draft.origin = "https://auth.example.test:8443";
    ctx.dashRedis.replacing = true;
    ctx.dashRedis.draft.url = newUrl();
    ctx.dashRedisDraftChanged();
    assert.equal(ctx.dashSettingsCount(), 5);
    const run = ctx.dashSettingsSaveAll();
    const order = [];
    const answers = {
        "/admin/api/settings/general": general(2, { timeZone: "Europe/London" }),
        "/admin/api/settings/display": display(2, "browser", "12h"),
        "/admin/api/settings/rate-limit": rate(2, 50),
        "/admin/api/settings/passkeys": pkAddress(2, "https://auth.example.test:8443"),
        "/admin/api/settings/redis": redisView(2),
    };
    for (let i = 0; i < 5; i++) {
        await settle();
        const now = open(pending);
        assert.equal(now.length, 1, "one save at a time");
        order.push(now[0].path);
        await answerNext(pending, "PUT", now[0].path, 200, answers[now[0].path]);
    }
    await run;
    assert.deepEqual(order, Object.keys(answers));
    assert.equal(ctx.dashSettingsCount(), 0);
    assert.equal(ctx.toasts.at(-1)[1], "success");
});

test("a cancelled question stops the save; later units are not sent", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    start(ctx, "general", general(1));
    start(ctx, "rate", rate(1, 120));
    editGeneral(ctx, { activityLogRetentionDays: "3" });
    ctx.dashSet.draft.maxReq = "50";
    ctx.answers.push(false);
    const run = ctx.dashSettingsSaveAll();
    await answerNext(pending, "PUT", "/admin/api/settings/general", 409, { code: "confirm_purge", detail: "x", estimate: {},
        confirm: { activityLogRetentionDays: 3, accessLogRetentionDays: 30 } });
    assert.deepEqual(plain(await run), { general: "cancelled" });
    assert.equal(open(pending).length, 0);
    assert.equal(pending.length, 1);
    assert.equal(ctx.dashSettingsCount(), 2);
});

test("a save without an answer is not reported as nothing saved, and nothing is sent again by itself", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const r = start(ctx, "redis", redisView(1));
    r.replacing = true;
    const url = newUrl();
    r.draft.url = url;
    ctx.dashRedisDraftChanged();
    const run = ctx.dashSettingsSaveAll();
    await answerNext(pending, "PUT", "/admin/api/settings/redis", 0, null, "network");
    assert.deepEqual(plain(await run), { redis: "unknown" });
    // The page reads the saved values again (GET), never the PUT
    await answerNext(pending, "GET", "/admin/api/settings/redis", 200, redisView(1));
    await settle();
    assert.equal(pending.filter((p) => p.method === "PUT").length, 1, "the secret was sent once");
    assert.equal(r.unknown, true);
    assert.equal(r.draft.url, url, "the draft stays");
    assert.doesNotMatch(ctx.toasts.at(-1)[0], /^Nothing was saved/);
    assert.equal(ctx.toasts.at(-1)[0], ctx.t("settings.savebar.result-unknown", { parts: ctx.t("settings.redis-connection") }));
});

test("a field that fails the check is named, and nothing is sent", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    start(ctx, "general", general(1));
    start(ctx, "rate", rate(1, 120));
    editGeneral(ctx, { activityLogRetentionDays: "7.5" });
    ctx.dashSet.draft.maxReq = "0";
    assert.equal(await ctx.dashSettingsSaveAll(), null);
    await settle();
    assert.equal(pending.length, 0);
    assert.equal(ctx.toasts.length, 1);
    assert.equal(ctx.toasts[0][0], ctx.tn("settings.check.fields-problem", 2, { label: ctx.t("settings.general.activity-log-retention-in-days") }));
    assert.equal(ctx.toasts[0][1], "error");
});

test("Discard drops every unit's unsaved fields", () => {
    const ctx = load();
    start(ctx, "general", general(1));
    start(ctx, "rate", rate(1, 120));
    editGeneral(ctx, { timeZone: "Asia/Jerusalem" });
    ctx.dashSet.draft.maxReq = "5";
    assert.equal(ctx.dashSettingsCount(), 2);
    ctx.dashSettingsDiscardAll();
    assert.equal(ctx.dashSettingsCount(), 0);
});

// ---------------------------------------------------------------------------
// Secrets on demand (FS-3)
// ---------------------------------------------------------------------------

test("Redis: typed text is not a change until Replace; the restart question is bound to the URL", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const r = start(ctx, "redis", redisView(0));
    r.draft.url = newUrl();
    ctx.dashRedisDraftChanged();
    assert.equal(r.dirty, false, "not replacing: nothing to send");
    r.replacing = true;
    const url = newUrl();
    r.draft.url = " " + url + " ";
    ctx.dashRedisDraftChanged();
    assert.equal(r.dirty, true);
    const saving = ctx.dashRedisSave();
    assert.deepEqual(plain(pending[0].body), { baseVersion: 0, url: url });
    await answerNext(pending, "PUT", null, 409, { code: "confirm_restart", detail: "Takes effect when every instance of the service restarts." });
    assert.equal(open(pending)[0].body.confirmRestart, true);
    await answerNext(pending, "PUT", null, 200, redisView(1));
    assert.equal(await saving, "saved");
    assert.equal(r.draft.url, "");
    assert.equal(r.replacing, false);
    assert.equal(r.dirty, false);
});

test("Redis: a failed check says why in plain words and keeps the draft", async () => {
    const ctx = load();
    const r = start(ctx, "redis", redisView(0));
    r.replacing = true;
    const expected = { unreachable: /Could not connect/, auth: /refused the user name or password/, no_write: /cannot write/, other: /check of this Redis failed/ };
    for (const [category, words] of Object.entries(expected)) {
        const pending = deferredApi(ctx);
        const url = newUrl();
        r.draft.url = url;
        ctx.dashRedisDraftChanged();
        const saving = ctx.dashRedisSave();
        pending[0].answer(422, { code: "redis_check_failed", category, detail: "server words" });
        assert.equal(await saving, "failed");
        assert.match(r.error, words, category);
        assert.equal(r.draft.url, url);
    }
});

test("a locked Redis URL is not a change, and the notes for a pending sync and a restart show", () => {
    const ctx = load();
    const view = redisView(1, { restartNeeded: true });
    view.url = Object.assign({}, view.url, { locked: true, source: "env", syncPending: true });
    const r = start(ctx, "redis", view);
    r.replacing = true;
    r.draft.url = newUrl();
    ctx.dashRedisDraftChanged();
    assert.equal(r.dirty, false);
    const alerts = [];
    ctx.dashRedisAlerts(view, alerts);
    assert.match(alerts.join(""), /keep REDIS_URL set until this note goes away/);
});

// The database fields as the page reads them, from stand-in inputs
function dbPage(values) {
    const els = {};
    for (const [id, value] of Object.entries(values)) els["dbs-" + id] = { value };
    return { ...PAGE, getElementById: (id) => els[id] || null };
}

test("database: the password and CA certificate go only when replaced; Remove sends keepCa false", () => {
    const ctx = load({ document: dbPage({ host: "db", port: "5432", database: "auth", user: "auth", password: "typed", sslmode: "verify-full", ca: "" }) });
    ctx.dashDb.snap = dbView();
    ctx.dashDb.snap.connection.caPem = true;
    let body = plain(ctx.dashDbRead());
    assert.equal(body.password, undefined, "not replacing: the saved password is kept");
    assert.equal(body.keepCa, true);
    assert.equal(body.caPem, undefined);
    ctx.dashDb.replacing.password = true;
    assert.equal(ctx.dashDbRead().password, "typed");
    ctx.dashDb.caRemove = true;
    body = plain(ctx.dashDbRead());
    assert.equal(body.keepCa, false);
    assert.equal(body.caPem, undefined);
});

test("database: each refused check is its own whole sentence, never a lower-cased title", () => {
    const ctx = load();
    const cases = [
        [{ ok: false, code: "timeout" }, "settings.database.nothing-saved-connection-failed"],
        [{ ok: true, problems: ["x"] }, "settings.database.nothing-saved-user-unusable"],
        [{ ok: true, state: "foreign", others: ["t"] }, "settings.database.nothing-saved-other-data"],
        [{ ok: true, state: "incomplete", others: ["t"] }, "settings.database.nothing-saved-tables-missing"],
        [{ ok: true, pending: true }, "settings.database.nothing-saved-unfinished-copy"],
        [{ ok: true, identity: "another" }, "settings.database.nothing-saved-another-installation"],
    ];
    for (const [r, key] of cases) {
        const note = ctx.uiDbCheckNote(r, "settings");
        assert.equal(note.tone, " is-fail", JSON.stringify(r));
        assert.equal(ctx.DASH_DB_REFUSED[note.kind], key, JSON.stringify(r));
    }
    assert.ok(!("settings.database.nothing-was-saved" in ctx.UI_TEXT));
});

// ---------------------------------------------------------------------------
// The database change: never together with a save, locked before it is sent,
// followed to its end, recovered when its answer is lost
// ---------------------------------------------------------------------------

// The change's own button needs the DOM; the flow is driven through dashDbSend
function dbReady(ctx) {
    ctx.dashDb.snap = dbView();
}

test("the database change cannot start with unsaved changes or while a save runs", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    dbReady(ctx);
    start(ctx, "general", general(1));
    editGeneral(ctx, { timeZone: "Asia/Jerusalem" });
    assert.notEqual(ctx.dashDbBlockReason(), "");
    ctx.dashDbSave();
    await settle();
    assert.equal(pending.length, 0, "no check, no change");

    // A save that is slow: while it runs the change stays blocked
    const run = ctx.dashSettingsSaveAll();
    await settle();
    assert.equal(ctx.dashSettings.saving, true);
    assert.equal(ctx.dashDbBlockReason(), ctx.t("settings.database.wait-for-the-save"));
    ctx.dashDbSave();
    await settle();
    assert.equal(open(pending).length, 1);
    await answerNext(pending, "PUT", "/admin/api/settings/general", 200, general(2, { timeZone: "Asia/Jerusalem" }));
    await run;
    assert.equal(ctx.dashDbBlockReason(), "");
});

test("no section save is sent after a database change started", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    dbReady(ctx);
    start(ctx, "general", general(1));
    start(ctx, "rate", rate(1, 120));
    ctx.dashDbSend({ postgres: {}, code: "123456" });
    assert.equal(ctx.dashSettings.lock, "sending", "locked before the answer");
    // Edits and Save are refused while it runs
    editGeneral(ctx, { timeZone: "Asia/Jerusalem" });
    assert.equal(await ctx.dashSettingsSaveAll(), null);
    await settle();
    assert.equal(pending.filter((p) => p.path.indexOf("/admin/api/settings/database") !== 0).length, 0);
});

test("a save already running stops before the next unit when a change locks the page", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    start(ctx, "general", general(1));
    start(ctx, "rate", rate(1, 120));
    editGeneral(ctx, { timeZone: "Asia/Jerusalem" });
    ctx.dashSet.draft.maxReq = "9";
    const run = ctx.dashSettingsSaveAll();
    await settle();
    // Another tab's change found by a refresh locks this page mid-save
    ctx.dashSettingsLockFor("running");
    await answerNext(pending, "PUT", "/admin/api/settings/general", 200, general(2, { timeZone: "Asia/Jerusalem" }));
    await run;
    assert.equal(pending.filter((p) => p.path === "/admin/api/settings/rate-limit").length, 0);
});

function me(zone, date, time) {
    return { user: { username: "admin" }, csrf: "c", allowedAreas: [], timeZone: zone, dateFormat: date, timeFormat: time };
}

test("a finished change reads every section again, applies the new zone and formats, then tells the pages", async () => {
    const events = [];
    const ctx = load();
    ctx.dispatchEvent = (e) => { events.push([e.type, ctx.uiAppZone, ctx.uiDatePrefs.date, ctx.uiDatePrefs.time]); return true; };
    const pending = deferredApi(ctx);
    dbReady(ctx);
    ctx.uiSetAppZone("UTC");
    ctx.uiSetDatePrefs("DD/MM/YYYY", "12h");
    start(ctx, "general", general(1));
    ctx.dashDbSend({ postgres: {}, code: "123456" });
    await answerNext(pending, "PUT", "/admin/api/settings/database", 202, { job: { id: "j1", state: "running" } });
    assert.equal(ctx.dashSettings.lock, "running");
    await answerNext(pending, "GET", "/admin/api/settings/database/jobs/j1", 200, { job: { id: "j1", state: "done", result: {} } });
    assert.equal(ctx.dashSettings.lock, "");
    // Every section is read again from the database now in use
    for (const path of ["/admin/api/settings/general", "/admin/api/settings/display", "/admin/api/settings/rate-limit",
        "/admin/api/settings/passkeys", "/admin/api/settings/redis"]) {
        assert.ok(open(pending).some((p) => p.path === path), path);
    }
    // A copy with another zone and no display key: the defaults
    await answerNext(pending, "GET", "/admin/api/me", 200, me("Asia/Jerusalem", "browser", "24h"));
    assert.deepEqual(events, [["ui:db-changed", "Asia/Jerusalem", "browser", "24h"]]);
    // A date outside Settings follows the new database without a reload
    const t = Date.UTC(2026, 0, 15, 22, 30);
    assert.equal(ctx.formatClock(t), "00:30");
    assert.equal(ctx.dashGen.snap, null, "General is read again");
});

test("the answer is lost while the job still runs: the page finds the job and follows it, never sends again", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    dbReady(ctx);
    ctx.dashDbSend({ postgres: {}, code: "123456" });
    await answerNext(pending, "PUT", "/admin/api/settings/database", 0, null, "network");
    assert.equal(ctx.dashSettings.lock, "recovering");
    await answerNext(pending, "GET", "/admin/api/settings/database", 200, dbView({ id: "j9", state: "running" }));
    assert.equal(ctx.dashSettings.lock, "running");
    await answerNext(pending, "GET", "/admin/api/settings/database/jobs/j9", 200, { job: { id: "j9", state: "running" } });
    assert.equal(pending.filter((p) => p.method === "PUT").length, 1, "the change and its code went once");
});

test("the answer is lost and the switch then fails after its save point: the read meets recovery mode", async () => {
    const loc = { origin: "https://a.example", pathname: "/admin/settings/database", search: "", hash: "", href: "" };
    const ctx = load({ location: loc });
    const pending = deferredApi(ctx);
    dbReady(ctx);
    ctx.dashDbSend({ postgres: {}, code: "123456" });
    await answerNext(pending, "PUT", "/admin/api/settings/database", 0, null, "network");
    await answerNext(pending, "GET", "/admin/api/settings/database", 503, { detail: "The database is not available.", code: "auth_failed" });
    assert.equal(loc.href, "/recovery");
});

test("a pending change is waited for by reading the settings; no job at all ends in a locked unknown result", async () => {
    const ctx = load();
    ctx.dispatchEvent = () => true;
    const pending = deferredApi(ctx);
    dbReady(ctx);
    ctx.setTimeout = (fn) => { fn(); return 0; };
    ctx.dashDbSend({ postgres: {}, code: "123456" });
    await answerNext(pending, "PUT", "/admin/api/settings/database", 0, null, "network");
    await answerNext(pending, "GET", "/admin/api/settings/database", 200, dbView({ state: "pending" }));
    // Still waiting: read again, never sent again
    await answerNext(pending, "GET", "/admin/api/settings/database", 503, { detail: "switching", code: "database_switching" });
    await answerNext(pending, "GET", "/admin/api/settings/database", 0, null, "network");
    await answerNext(pending, "GET", "/admin/api/settings/database", 200, dbView(null));
    assert.equal(ctx.dashSettings.lock, "unknown");
    // The zone and formats are read again in case the database changed
    await answerNext(pending, "GET", "/admin/api/me", 200, me("UTC", "browser", "24h"));
    await settle();
    assert.equal(open(pending).length, 0, "nothing more: no retry, no polling");
    assert.equal(pending.filter((p) => p.method === "PUT").length, 1);
    // The page stays locked: no save, no refresh read
    start(ctx, "general", general(1));
    editGeneral(ctx, { timeZone: "Asia/Jerusalem" });
    assert.equal(await ctx.dashSettingsSaveAll(), null);
});

test("a read refused for another reason keeps the lock and offers to read again, only the read", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    dbReady(ctx);
    ctx.dashDbSend({ postgres: {}, code: "123456" });
    await answerNext(pending, "PUT", "/admin/api/settings/database", 0, null, "network");
    await answerNext(pending, "GET", "/admin/api/settings/database", 500, { detail: "Internal Server Error" });
    assert.equal(ctx.dashSettings.lock, "read-failed");
    await settle();
    assert.equal(open(pending).length, 0);
    ctx.dashDbRecover(); // Try again
    await answerNext(pending, "GET", "/admin/api/settings/database", 200, dbView({ id: "j3", state: "running" }));
    assert.equal(ctx.dashSettings.lock, "running");
    assert.equal(pending.filter((p) => p.method === "PUT").length, 1);
});

test("a job the server no longer knows (404) is looked for through the settings read", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    dbReady(ctx);
    ctx.dashDbFollow("gone");
    await answerNext(pending, "GET", "/admin/api/settings/database/jobs/gone", 404, { code: "not_found", detail: "No such operation." });
    assert.equal(ctx.dashSettings.lock, "recovering");
    assert.ok(open(pending).some((p) => p.path === "/admin/api/settings/database"));
});

test("a change refused before it started unlocks the page; nothing changed", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    dbReady(ctx);
    const sent = ctx.dashDbSend({ postgres: {}, code: "000000" });
    await answerNext(pending, "PUT", "/admin/api/settings/database", 400, { detail: "Invalid code" });
    const res = await sent;
    assert.equal(res.status, 400);
    assert.equal(ctx.dashSettings.lock, "");
});

test("the step-up dialog closed while its request ran: the page follows the job, the dialog sends nothing again", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    dbReady(ctx);
    // A stand-in for the dialog builder: its parts and their handlers
    const handlers = {};
    const el = (name) => ({ name, value: "", disabled: false, textContent: "", classList: { add() {}, remove() {}, toggle() {} },
        addEventListener: (type, fn) => { handlers[name + ":" + type] = fn; }, focus() {}, select() {} });
    const parts = { "#stepup-code": el("code"), "#stepup-err": el("err"), "[type=submit]": el("submit"), "#stepup-pk-btn": el("pk"),
        "[data-cancel]": el("cancel"), "#stepup-pk": el("pkbox"), "#stepup-text": el("text") };
    let connected = true;
    let dialog = null;
    ctx.uiDialog = (o) => {
        dialog = { o, backdrop: { isConnected: true, remove() { connected = false; this.isConnected = false; }, addEventListener() {} },
            body: { querySelector: (s) => parts[s], querySelectorAll: () => [] }, foot: { querySelector: (s) => parts[s] } };
        return dialog;
    };
    ctx.uiGuardDialog = () => {};
    ctx.uiPasskeyBlocked = () => "insecure";
    ctx.dashStepUpDialog({ title: "t", text: "x", pkText: "y", submitText: "z", send: (proof) => ctx.dashDbSend(Object.assign({ postgres: {} }, proof)) });
    parts["#stepup-code"].value = "123456";
    dialog.o.form.onSubmit();
    await settle();
    // The admin closes the dialog before the answer comes
    dialog.backdrop.remove();
    assert.equal(connected, false);
    await answerNext(pending, "PUT", "/admin/api/settings/database", 202, { job: { id: "late", state: "running" } });
    assert.ok(open(pending).some((p) => p.path === "/admin/api/settings/database/jobs/late"), "the page follows the job");
    dialog.o.form.onSubmit();
    await settle();
    assert.equal(pending.filter((p) => p.method === "PUT").length, 1, "the proof was sent once");
});

// ---------------------------------------------------------------------------
// The page: search index, sections, sign out
// ---------------------------------------------------------------------------

test("SETTINGS_INDEX names every section and real fields with catalog keys", () => {
    const fs = require("node:fs");
    const path = require("node:path");
    const ctx = load();
    const code = ["js/dashboard.js", "js/ui.js"].map((f) => fs.readFileSync(path.join(__dirname, "..", "..", "static", "public", f), "utf8")).join("\n");
    const sections = ctx.SETTINGS_SECTIONS.map((s) => s.id);
    for (const s of sections) assert.ok(ctx.SETTINGS_INDEX.some((e) => e.section === s && e.field === ""), s);
    for (const e of ctx.SETTINGS_INDEX) {
        assert.ok(sections.includes(e.section), e.section);
        assert.ok(e.key in ctx.UI_TEXT, e.key);
        if (e.field) {
            const id = e.field.replace(/^dbs-/, "");
            assert.ok(code.includes('id="' + e.field + '"') || code.includes('"' + e.field + '"') || code.includes('p + "' + id + '"') ||
                code.includes('input("' + id + '"') || code.includes("'" + id + "'") || code.includes('"' + id + '"'), e.field);
        }
    }
});

test("Settings sections have their own addresses under /admin/settings", () => {
    const loc = { origin: "https://a.example", pathname: "/admin/settings/rate-limit", search: "", hash: "" };
    const ctx = load({ location: loc });
    ctx.uiPages = ctx.PAGES;
    assert.equal(ctx.uiPageFromLocation(), "settings");
    assert.equal(ctx.uiPageSection("settings"), "rate-limit");
    loc.pathname = "/admin/settings";
    assert.equal(ctx.uiPageSection("settings"), "");
    loc.pathname = "/admin/settings/Bad.Name";
    assert.equal(ctx.uiPageSection("settings"), "");
    loc.pathname = "/admin/tokens/x";
    assert.equal(ctx.uiPageFromLocation(), "", "only a page with sections owns the addresses under it");
});

test("signing out drops unsaved changes only once the sign-out worked", async () => {
    const loc = { origin: "https://a.example", pathname: "/admin/settings/general", search: "", hash: "", href: "" };
    const ctx = load({ location: loc, sessionStorage: { getItem: () => "", setItem() {}, removeItem() {} } });
    const pending = deferredApi(ctx);
    start(ctx, "general", general(1));
    ctx.uiUnsavedRegister(() => ctx.dashSettingsCount(), () => { throw new Error("must not drop"); });
    editGeneral(ctx, { timeZone: "Asia/Jerusalem" });
    let out = ctx.dashLogout();
    await answerNext(pending, "POST", "/admin/api/logout", 500, { detail: "busy" });
    assert.equal(await out, false);
    assert.equal(ctx.uiUnsavedCount(), 1, "still guarded");
    assert.equal(ctx.dashGen.draft.timeZone, "Asia/Jerusalem");
    assert.match(ctx.toasts.at(-1)[0], /sign out/i);
    out = ctx.dashLogout();
    await answerNext(pending, "POST", "/admin/api/logout", 200, { ok: true });
    assert.equal(await out, true);
    assert.equal(ctx.uiUnsavedCount(), 0);
    assert.equal(loc.href, "/login");
    // Stay keeps everything and sends nothing
    const ctx2 = load({ location: loc });
    const pending2 = deferredApi(ctx2);
    start(ctx2, "general", general(1));
    ctx2.uiUnsavedRegister(() => ctx2.dashSettingsCount(), () => {});
    Object.assign(ctx2.dashGen.draft, { timeZone: "Asia/Jerusalem" });
    ctx2.answers.push(false);
    assert.equal(await ctx2.dashLogout(), false);
    assert.equal(pending2.length, 0);
});

// ---------------------------------------------------------------------------
// Other pages that read settings words
// ---------------------------------------------------------------------------

test("the Activity Log names what a General, Display or Redis save changed", () => {
    const ctx = load();
    const gen = ctx.dashLogDetails({ action: "settings.update", entityName: "general", details: {
        version: 4,
        from: { timeZone: null, cookieSecure: false, activityLogRetentionDays: 30, accessLogRetentionDays: 30 },
        to: { timeZone: "Asia/Jerusalem", cookieSecure: true, activityLogRetentionDays: 30, accessLogRetentionDays: 7 },
    } });
    assert.match(gen, /Version 4:/);
    assert.match(gen, /Time zone: default to Asia\/Jerusalem/);
    assert.match(gen, /Access log retention: 30 days to 7 days/);
    const disp = ctx.dashLogDetails({ action: "settings.update", entityName: "display", details: {
        version: 2, from: { dateFormat: "browser", timeFormat: "24h" }, to: { dateFormat: "DD.MM.YYYY", timeFormat: "24h" } } });
    assert.match(disp, /Date Format: browser to DD\.MM\.YYYY/);
    assert.doesNotMatch(disp, /Time Format/);
    for (const det of [{ version: 2, from: null, to: "redis://****@cache:6379/0" },
        { version: 2, from: { url: null }, to: { url: "redis://****@cache:6379/0" } }]) {
        assert.match(ctx.dashLogDetails({ action: "settings.update", entityName: "redis", details: det }), /Redis URL: default to redis:\/\/\*\*\*\*@cache:6379\/0/);
    }
    const evil = ctx.dashLogDetails({ action: "settings.update", entityName: "general", details: { version: 1, from: {}, to: { timeZone: "<img>" } } });
    assert.match(evil, /&lt;img&gt;/);
});

test("an active ADMIN_API_KEY is a locked row on API Keys", () => {
    const ctx = load();
    ctx.dashKeys.master = { active: false, envName: "ADMIN_API_KEY" };
    assert.equal(ctx.dashKeyMasterRow(), "");
    ctx.dashKeys.master = { active: true, envName: "ADMIN_API_KEY" };
    const row = ctx.dashKeyMasterRow();
    assert.match(row, /Master key/);
    assert.match(row, /remove it from the environment to revoke/);
    assert.doesNotMatch(row, /data-key|tabindex|data-row-menu/);
});
