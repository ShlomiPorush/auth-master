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
    const ctx = loadScripts(["js/common.js", "js/ui.js", "js/dashboard.js"], { document: PAGE });
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
