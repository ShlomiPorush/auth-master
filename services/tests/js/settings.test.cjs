// The Settings page's data layer (js/dashboard.js) against a fake API whose answers
// arrive in an order the test chooses: a refresh never draws over a save's result.
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

function load() {
    const ctx = loadScripts(["js/text.js", "js/common.js", "js/ui.js", "js/dashboard.js"], { document: PAGE });
    ctx.showToast = () => {};
    return ctx;
}

function snap(version, maxReq) {
    return {
        version: version, savedBy: version ? "admin" : null, savedAt: null, savedFrom: null,
        fields: {
            maxReq: { value: maxReq, source: version ? "stored" : "default", locked: false, envName: "RATE_LIMIT_MAX_REQ", min: 1, max: 1000000 },
            windowMs: { value: 60000, source: "default", locked: false, envName: "RATE_LIMIT_WINDOW_MS", minSeconds: 1, maxSeconds: 3600 },
            exempt: { value: [], source: "default", locked: false, max: 100 },
        },
        applied: { version: version, lastReload: null, lastReloadError: null },
        yourAddress: "10.0.0.1",
    };
}

// Each request waits until the test answers it
function deferredApi(ctx) {
    const pending = [];
    ctx.adminJson = (path, opts) => new Promise((resolve) => {
        pending.push({ method: (opts && opts.method) || "GET", body: opts && opts.body, answer: (status, data) => resolve({ ok: status < 400, status, data, json: true, error: "" }) });
    });
    return pending;
}

test("a refresh that started during a save cannot undo the save's result", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const s = ctx.dashSet;
    s.snap = s.baseSnap = snap(0, 120);
    s.draft = { maxReq: "5", windowSeconds: "60", exempt: "" };
    s.dirty = true;

    const saving = ctx.dashSettingsSave();
    const refreshing = ctx.dashSettingsLoad();
    const [put, get] = pending;
    assert.equal(put.method, "PUT");
    assert.equal(get.method, "GET");
    assert.equal(put.body.maxReq, 5);
    assert.equal(put.body.baseVersion, 0);

    put.answer(200, snap(1, 5));
    await saving;
    get.answer(200, snap(0, 120)); // read before the save committed, answered after it
    await refreshing;

    assert.equal(s.baseSnap.version, 1);
    assert.equal(s.snap.version, 1);
    assert.equal(s.draft.maxReq, "5");
    assert.equal(s.dirty, false);
});

test("a conflict keeps the draft and its base until the admin chooses", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const s = ctx.dashSet;
    s.snap = s.baseSnap = snap(0, 120);
    s.draft = { maxReq: "5", windowSeconds: "60", exempt: "" };
    s.dirty = true;

    const saving = ctx.dashSettingsSave();
    pending[0].answer(409, { detail: "Someone else changed these settings after you opened the page.", code: "conflict", current: snap(1, 300) });
    await saving;
    assert.equal(s.conflict.version, 1);
    assert.equal(s.baseSnap.version, 0);
    assert.equal(s.draft.maxReq, "5");

    // A refresh while the conflict is open shows the new version but keeps the draft and its base
    const refreshing = ctx.dashSettingsLoad();
    pending[1].answer(200, snap(1, 300));
    await refreshing;
    assert.equal(s.baseSnap.version, 0);
    assert.equal(s.draft.maxReq, "5");
});

// The Settings page drawn into a stand-in for its container, with the proxy setting
// the server reported
function drawWithProxy(value) {
    const body = { innerHTML: "" };
    const page = { ...PAGE, getElementById: (id) => (id === "settings-body" ? body : null) };
    const ctx = loadScripts(["js/text.js", "js/common.js", "js/ui.js", "js/dashboard.js"], { document: page });
    const s = ctx.dashSet;
    s.snap = s.baseSnap = snap(0, 120);
    s.snap.proxyNetwork = { envName: "FORWARDED_ALLOW_IPS", value: value };
    s.draft = { maxReq: "120", windowSeconds: "60", exempt: "" };
    ctx.dashSettingsDrawForm();
    // Catalog text is escaped as a whole in markup: read an apostrophe as the page shows it
    return body.innerHTML.replace(/&#39;/g, "'");
}

const WARNING = 'id="set-exempt-warn"';

test("a loopback-only FORWARDED_ALLOW_IPS keeps the proxy hint and the exempt warning", () => {
    // The bundled compose file passes uvicorn's default when the variable is not set
    for (const value of ["127.0.0.1,::1", "::1, 127.0.0.1", " 127.0.0.1", "::1", "127.0.0.1,,::1"]) {
        const html = drawWithProxy(value);
        assert.ok(html.includes("loopback addresses only"), value);
        assert.ok(html.includes("set it to the proxy's address or network"), value);
        assert.ok(html.includes(WARNING), value);
    }
});

test("a proxy address hides the exempt warning; unset, empty and * keep their messages", () => {
    const proxy = drawWithProxy("172.18.0.0/16");
    assert.ok(proxy.includes("<code>172.18.0.0/16</code>"));
    assert.ok(!proxy.includes("loopback addresses only"));
    assert.ok(!proxy.includes(WARNING));

    const mixed = drawWithProxy("127.0.0.1,172.18.0.5");
    assert.ok(!mixed.includes("loopback addresses only"));
    assert.ok(!mixed.includes(WARNING));

    const star = drawWithProxy("*");
    assert.ok(star.includes("<code>*</code>"));
    assert.ok(!star.includes("loopback addresses only"));
    assert.ok(!star.includes(WARNING));

    const unset = drawWithProxy(null);
    assert.ok(unset.includes("is not set"));
    assert.ok(unset.includes(WARNING));

    const empty = drawWithProxy("");
    assert.ok(empty.includes("to an empty value"));
    assert.ok(empty.includes(WARNING));
});

// ---------------------------------------------------------------------------
// General: time zone, log retention, the Secure cookie
// ---------------------------------------------------------------------------

function general(version, over) {
    const v = Object.assign({ timeZone: "UTC", cookieSecure: false, activity: 30, access: 30 }, over || {});
    const src = version ? "stored" : "default";
    return {
        version: version, savedBy: version ? "admin" : null, savedAt: null, savedFrom: null,
        fields: {
            timeZone: { value: v.timeZone, source: src, locked: false, envName: "TZ", envInvalid: false },
            cookieSecure: { value: v.cookieSecure, source: src, locked: false, envName: "COOKIE_SECURE" },
            activityLogRetentionDays: { value: v.activity, source: src, locked: false, envName: "ACTIVITY_LOG_RETENTION_DAYS", min: 1, max: 3650 },
            accessLogRetentionDays: { value: v.access, source: src, locked: false, envName: "ACCESS_LOG_RETENTION_DAYS", min: 1, max: 3650 },
        },
        timeZones: ["Asia/Jerusalem", "Europe/London", "UTC"],
        applied: { version: version, lastReload: null, lastReloadError: null },
        purge: { activity: { state: "ok", reason: null, lastRun: null }, access: { state: "ok", reason: null, lastRun: null } },
        rootPath: { envName: "ROOT_PATH", value: null },
    };
}

function editGeneral(ctx, changes) {
    Object.assign(ctx.dashGen.draft, changes);
    ctx.dashGenDraftChanged();
}

function startGeneral(ctx, snapshot) {
    const g = ctx.dashGen;
    g.snap = g.baseSnap = snapshot;
    g.draft = ctx.dashGenDraftFrom(snapshot);
    return g;
}

test("General sends only the changed fields, as numbers and a boolean", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = startGeneral(ctx, general(3));
    editGeneral(ctx, { timeZone: "Europe/London", accessLogRetentionDays: " 60 " });
    assert.equal(g.dirty, true);
    assert.deepEqual(Object.keys(ctx.dashGenChanges()).sort(), ["accessLogRetentionDays", "timeZone"]);
    const saving = ctx.dashGenSave();
    assert.deepEqual(plain(pending[0].body), { baseVersion: 3, timeZone: "Europe/London", accessLogRetentionDays: 60 });
    pending[0].answer(200, general(4, { timeZone: "Europe/London", access: 60 }));
    await saving;
    assert.equal(g.dirty, false);
    assert.equal(g.baseSnap.version, 4);
    // Going back to the saved value is not a change
    editGeneral(ctx, { accessLogRetentionDays: "60" });
    assert.equal(g.dirty, false);
});

test("General refuses a retention that is not a whole number before asking the server", () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = startGeneral(ctx, general(1));
    editGeneral(ctx, { activityLogRetentionDays: "7.5" });
    ctx.dashGenSave();
    assert.equal(pending.length, 0);
    assert.equal(g.errorField, "activityLogRetentionDays");
});

test("a lower retention is confirmed with exactly the values the server named", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = startGeneral(ctx, general(2));
    editGeneral(ctx, { activityLogRetentionDays: "7" });

    let saving = ctx.dashGenSave();
    assert.equal(pending[0].body.confirmPurge, undefined);
    const confirm = { activityLogRetentionDays: 7, accessLogRetentionDays: 30 };
    pending[0].answer(409, { code: "confirm_purge", detail: "This deletes older log entries.", estimate: { activityLogs: 412, accessLogs: 0 }, confirm });
    await saving;
    assert.equal(g.askPurge.ok, false);
    assert.match(ctx.dashGenAsksHtml(), /412 activity log entries/);

    // Not ticked: the save goes without a confirmation, and the server asks again
    saving = ctx.dashGenSave();
    assert.equal(pending[1].body.confirmPurge, undefined);
    pending[1].answer(409, { code: "confirm_purge", detail: "x", estimate: { activityLogs: 412, accessLogs: 0 }, confirm });
    await saving;

    ctx.dashGenConfirm("purge", true);
    saving = ctx.dashGenSave();
    assert.deepEqual(plain(pending[2].body.confirmPurge), confirm);
    assert.equal(pending[2].body.activityLogRetentionDays, 7);
    pending[2].answer(200, general(3, { activity: 7 }));
    await saving;
    assert.equal(g.askPurge, null);
});

test("changing a retention after the question drops the confirmation; other fields keep it", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = startGeneral(ctx, general(2));
    editGeneral(ctx, { activityLogRetentionDays: "7" });
    const saving = ctx.dashGenSave();
    pending[0].answer(409, { code: "confirm_purge", detail: "x", estimate: { activityLogs: null, accessLogs: null },
        confirm: { activityLogRetentionDays: 7, accessLogRetentionDays: 30 } });
    await saving;
    assert.match(ctx.dashGenAsksHtml(), /an unknown number of activity log entries/);
    // Only the lowered log is named.
    assert.doesNotMatch(ctx.dashGenAsksHtml(), /access log entr/);
    ctx.dashGenConfirm("purge", true);

    editGeneral(ctx, { timeZone: "Asia/Jerusalem" });
    assert.equal(g.askPurge.ok, true);
    editGeneral(ctx, { accessLogRetentionDays: "20" });
    assert.equal(g.askPurge, null);

    ctx.dashGenSave();
    assert.equal(pending[1].body.confirmPurge, undefined);
});

test("the Secure cookie question is bound to turning it on", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = startGeneral(ctx, general(1));
    editGeneral(ctx, { cookieSecure: true });
    let saving = ctx.dashGenSave();
    assert.equal(pending[0].body.cookieSecure, true);
    pending[0].answer(409, { code: "confirm_cookie_secure", detail: "Browsers send a Secure cookie only over HTTPS." });
    await saving;
    assert.match(ctx.dashGenAsksHtml(), /Browsers send a Secure cookie only over HTTPS\./);
    ctx.dashGenConfirm("cookie", true);
    saving = ctx.dashGenSave();
    assert.equal(pending[1].body.confirmCookieSecure, true);
    pending[1].answer(409, { code: "confirm_cookie_secure", detail: "again" });
    await saving;
    assert.equal(g.askCookie.ok, false, "a new question starts unticked");

    ctx.dashGenConfirm("cookie", true);
    editGeneral(ctx, { cookieSecure: false });
    assert.equal(g.askCookie, null);
    editGeneral(ctx, { cookieSecure: true });
    ctx.dashGenSave();
    assert.equal(pending[2].body.confirmCookieSecure, undefined);
});

test("both confirmations go together when the server asks for one after the other", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    startGeneral(ctx, general(1));
    editGeneral(ctx, { cookieSecure: true, accessLogRetentionDays: "10" });
    const confirm = { activityLogRetentionDays: 30, accessLogRetentionDays: 10 };
    let saving = ctx.dashGenSave();
    pending[0].answer(409, { code: "confirm_purge", detail: "x", estimate: { activityLogs: 0, accessLogs: 5 }, confirm });
    await saving;
    ctx.dashGenConfirm("purge", true);
    saving = ctx.dashGenSave();
    pending[1].answer(409, { code: "confirm_cookie_secure", detail: "y" });
    await saving;
    ctx.dashGenConfirm("cookie", true);
    ctx.dashGenSave();
    assert.deepEqual(plain(pending[2].body.confirmPurge), confirm);
    assert.equal(pending[2].body.confirmCookieSecure, true);
});

test("a General conflict keeps the draft, drops the confirmations, and Keep rebases the draft", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = startGeneral(ctx, general(2));
    editGeneral(ctx, { activityLogRetentionDays: "7" });
    let saving = ctx.dashGenSave();
    pending[0].answer(409, { code: "confirm_purge", detail: "x", estimate: {}, confirm: { activityLogRetentionDays: 7, accessLogRetentionDays: 30 } });
    await saving;
    ctx.dashGenConfirm("purge", true);

    saving = ctx.dashGenSave();
    pending[1].answer(409, { code: "conflict", detail: "changed", current: general(3, { activity: 90 }) });
    await saving;
    assert.equal(g.conflict.version, 3);
    assert.equal(g.baseSnap.version, 2);
    assert.equal(g.draft.activityLogRetentionDays, "7");
    assert.equal(g.askPurge, null);

    ctx.dashPanelKeep(ctx.DASH_SET_PANELS.general);
    assert.equal(g.conflict, null);
    assert.equal(g.baseSnap.version, 3);
    assert.equal(g.dirty, true);
    ctx.dashGenSave();
    assert.equal(pending[2].body.baseVersion, 3);
    assert.equal(pending[2].body.activityLogRetentionDays, 7);
    assert.equal(pending[2].body.confirmPurge, undefined);
});

test("Discard after a General conflict takes the other admin's version", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const g = startGeneral(ctx, general(2));
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
    ctx.dash.zoneAsked = "UTC";
    startGeneral(ctx, general(1));

    // Another tab saved a zone: this tab keeps its zone until reloaded
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

test("a locked field is never sent, and an invalid TZ is called out", () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const s = general(1);
    s.fields.timeZone = { value: "UTC", source: "env", locked: true, envName: "TZ", envInvalid: true };
    startGeneral(ctx, s);
    editGeneral(ctx, { timeZone: "Europe/London" });
    assert.equal(ctx.dashGen.dirty, false);
    ctx.dashGenSave();
    assert.deepEqual(plain(pending[0].body), { baseVersion: 1 });
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
    assert.doesNotMatch(html, /Old activity log entries/);
});

test("the Settings page refresh loads every panel", async () => {
    const ctx = load();
    const paths = [];
    ctx.adminJson = (path) => { paths.push(path); return Promise.resolve({ ok: false, status: 500, data: null, json: false, error: "" }); };
    await ctx.dashSettingsLoadAll();
    assert.deepEqual(paths.sort(), ["/admin/api/settings/general", "/admin/api/settings/rate-limit", "/admin/api/settings/redis"]);
});

// ---------------------------------------------------------------------------
// Redis connection
// ---------------------------------------------------------------------------

const crypto = require("node:crypto");

function redisView(version, over) {
    return Object.assign({
        version: version, savedBy: version ? "admin" : null, savedAt: null, savedFrom: null,
        url: { connected: "redis://****@redis:6379/0", saved: version ? "redis://****@cache:6379/0" : null, source: version ? "stored" : "default",
            locked: false, envName: "REDIS_URL", syncPending: false },
        restartNeeded: !!version,
    }, over || {});
}

function newUrl() {
    // A password made for this run only
    return "redis://:" + crypto.randomBytes(24).toString("hex") + "@cache:6379/0";
}

function startRedis(ctx, view) {
    const r = ctx.dashRedis;
    r.snap = r.baseSnap = view;
    r.draft = ctx.dashRedisDraftFrom(view);
    return r;
}

test("Redis asks to confirm the restart, bound to the URL it was sent", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const r = startRedis(ctx, redisView(0));
    const url = newUrl();
    r.draft.url = " " + url + " ";
    ctx.dashRedisDraftChanged();
    assert.equal(r.dirty, true);

    let saving = ctx.dashRedisSave();
    assert.deepEqual(plain(pending[0].body), { baseVersion: 0, url: url });
    pending[0].answer(409, { code: "confirm_restart", detail: "Takes effect when every instance of the service restarts." });
    await saving;
    assert.equal(r.askRestart.ok, false);

    ctx.dashRedisConfirm(true);
    saving = ctx.dashRedisSave();
    assert.equal(pending[1].body.confirmRestart, true);
    pending[1].answer(200, redisView(1));
    await saving;
    // The typed URL, with its password, is gone from the page's state
    assert.equal(r.draft.url, "");
    assert.equal(r.askRestart, null);
    assert.equal(r.dirty, false);
    assert.doesNotMatch(JSON.stringify({ draft: r.draft, snap: r.snap, base: r.baseSnap }), new RegExp(url.split(":")[2].split("@")[0]));
});

test("a changed Redis URL needs a new restart confirmation", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const r = startRedis(ctx, redisView(0));
    r.draft.url = newUrl();
    ctx.dashRedisDraftChanged();
    const saving = ctx.dashRedisSave();
    pending[0].answer(409, { code: "confirm_restart", detail: "x" });
    await saving;
    ctx.dashRedisConfirm(true);
    r.draft.url = newUrl();
    ctx.dashRedisDraftChanged();
    assert.equal(r.askRestart, null);
    ctx.dashRedisSave();
    assert.equal(pending[1].body.confirmRestart, undefined);
});

test("a failed Redis check says why in plain words and keeps the draft", async () => {
    const ctx = load();
    const r = startRedis(ctx, redisView(0));
    const expected = { unreachable: /Could not connect/, auth: /refused the user name or password/, no_write: /cannot write/, other: /check of this Redis failed/, surprise: /check of this Redis failed/ };
    for (const [category, words] of Object.entries(expected)) {
        const pending = deferredApi(ctx);
        const url = newUrl();
        r.draft.url = url;
        ctx.dashRedisDraftChanged();
        const saving = ctx.dashRedisSave();
        pending[0].answer(422, { code: "redis_check_failed", category, detail: "server words" });
        await saving;
        assert.match(r.error, words, category);
        assert.match(r.error, /Nothing was saved\./);
        assert.equal(r.errorField, "url");
        assert.equal(r.draft.url, url);
    }
});

test("a Redis conflict drops the restart confirmation and keeps the typed URL", async () => {
    const ctx = load();
    const pending = deferredApi(ctx);
    const r = startRedis(ctx, redisView(0));
    const url = newUrl();
    r.draft.url = url;
    ctx.dashRedisDraftChanged();
    let saving = ctx.dashRedisSave();
    pending[0].answer(409, { code: "confirm_restart", detail: "x" });
    await saving;
    ctx.dashRedisConfirm(true);
    saving = ctx.dashRedisSave();
    pending[1].answer(409, { code: "conflict", current: redisView(1) });
    await saving;
    assert.equal(r.askRestart, null);
    assert.equal(r.conflict.version, 1);
    assert.equal(r.draft.url, url);
    assert.match(ctx.dashRedisConflictText(r.conflict), /redis:\/\/\*\*\*\*@cache:6379\/0/);
});

test("a locked Redis URL is not a change, and the notes for a pending sync and a restart show", () => {
    const ctx = load();
    const view = redisView(1, { restartNeeded: true });
    view.url = Object.assign({}, view.url, { locked: true, source: "env", syncPending: true });
    const r = startRedis(ctx, view);
    r.draft.url = newUrl();
    ctx.dashRedisDraftChanged();
    assert.equal(r.dirty, false);
    const alerts = [];
    ctx.dashRedisAlerts(view, alerts);
    const html = alerts.join("");
    assert.match(html, /The environment value is not saved yet; keep REDIS_URL set until this note goes away\./);
    assert.match(html, /Restart every instance of the service together/);
});

// Objects made inside the page's context compare by value only after a copy
function plain(v) {
    return JSON.parse(JSON.stringify(v));
}

test("the Activity Log names what a General or Redis save changed", () => {
    const ctx = load();
    const gen = ctx.dashLogDetails({ action: "settings.update", entityName: "general", details: {
        version: 4,
        from: { timeZone: null, cookieSecure: false, activityLogRetentionDays: 30, accessLogRetentionDays: 30 },
        to: { timeZone: "Asia/Jerusalem", cookieSecure: true, activityLogRetentionDays: 30, accessLogRetentionDays: 7 },
    } });
    assert.match(gen, /Version 4:/);
    assert.match(gen, /Time zone: default to Asia\/Jerusalem/);
    assert.match(gen, /Access log retention: 30 days to 7 days/);
    assert.match(gen, /Secure cookie: off to on/);
    assert.doesNotMatch(gen, /Activity log retention/);
    for (const det of [{ version: 2, from: null, to: "redis://****@cache:6379/0" },
        { version: 2, from: { url: null }, to: { url: "redis://****@cache:6379/0" } }]) {
        assert.match(ctx.dashLogDetails({ action: "settings.update", entityName: "redis", details: det }), /Redis URL: default to redis:\/\/\*\*\*\*@cache:6379\/0/);
    }
    // Text from the log is escaped
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
    // Nothing opens it: no row key, no keyboard stop, no menu
    assert.doesNotMatch(row, /data-key|tabindex|data-row-menu/);
});
