// P27 in js/dashboard.js: after ui:db-changed the lists and Usage drop what they
// hold, an answer to a read that started before the change is ignored when it
// arrives late, and the page shown reads its data again.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadScripts } = require("./_load.cjs");

const PAGE = { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {} };

function load() {
    const listeners = {};
    const ctx = loadScripts(["js/text.js", "js/common.js", "js/ui.js", "js/dashboard.js"], {
        document: PAGE,
        addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    });
    ctx.showToast = () => {};
    // Needs attention reads again too; recorded here, not run
    ctx.refreshed = [];
    ctx.uiRefreshNow = (id) => { ctx.refreshed.push(id); return Promise.resolve(true); };
    ctx.dbChanged = () => (listeners["ui:db-changed"] || []).forEach((fn) => fn(new ctx.CustomEvent("ui:db-changed")));
    ctx.listeners = listeners;
    return ctx;
}

// Every request waits until the test answers it
function heldApi(ctx) {
    const pending = [];
    ctx.adminJson = (path) => new Promise((resolve) => {
        const url = new URL(path, "http://x");
        pending.push({ path: url.pathname, params: Object.fromEntries(url.searchParams), answer: (status, data) => resolve({ ok: status < 400, status, data, json: true, error: "" }) });
    });
    return pending;
}

const logPage = (ids) => ({
    logs: ids.map((id, i) => ({ id, createdAt: new Date(Date.UTC(2026, 0, 1, 12) - i * 1000).toISOString(), actor: "a", action: "login" })),
    total: ids.length, headCursor: ids.length ? "h:" + ids[0] : null, nextCursor: ids.length ? "n:" + ids[ids.length - 1] : null,
});

test("the dashboard listens for the database change event", () => {
    const ctx = load();
    assert.equal((ctx.listeners["ui:db-changed"] || []).length, 1);
});

test("a token list read before the change answers late and is ignored; the next read is the new database's", async () => {
    const ctx = load();
    const pending = heldApi(ctx);
    ctx.dashTokenStore.rows = [{ id: "old-shown", name: "old" }];
    const before = ctx.dashTokensLoad(true);
    ctx.dbChanged();
    assert.equal(ctx.dashTokenStore.rows, null, "the old database's tokens are no longer shown");
    pending.find((p) => p.path === "/admin/api/tokens").answer(200, [{ id: "old-late", name: "old" }]);
    await before;
    assert.equal(ctx.dashTokenStore.rows, null, "the late answer from the old database is dropped");
    const after = ctx.dashTokensLoad(false);
    const reads = pending.filter((p) => p.path === "/admin/api/tokens");
    assert.equal(reads.length, 2, "a new read starts instead of joining the old one");
    reads[1].answer(200, [{ id: "new", name: "new" }]);
    await after;
    assert.deepEqual([...ctx.dashTokenStore.rows.map((r) => r.id)], ["new"]);
});

test("zones and API keys drop their rows and ignore reads from before", async () => {
    const ctx = load();
    const pending = heldApi(ctx);
    ctx.dash.zones = [{ id: "z-old", name: "old" }];
    ctx.dash.zonesLoaded = true;
    ctx.dashZones.loaded = true;
    ctx.dashKeys.rows = [{ id: "k-old", name: "old" }];
    const zones = ctx.dashZonesLoad();
    const keys = ctx.dashKeysLoad(true);
    ctx.dbChanged();
    assert.deepEqual([...ctx.dash.zones], []);
    assert.equal(ctx.dashZones.loaded, false);
    assert.equal(ctx.dashKeys.rows, null);
    for (const p of pending) p.answer(200, p.path === "/admin/api/api-keys/master" ? { active: false } : [{ id: "late", name: "late" }]);
    await Promise.all([zones, keys]);
    assert.deepEqual([...ctx.dash.zones], []);
    assert.equal(ctx.dashKeys.rows, null);
});

test("a log list's first load, next page or refresh from before publishes nothing and leaves no load stuck", async () => {
    const ctx = load();
    const pending = heldApi(ctx);
    const s = ctx.dashLogs.activity;
    s.applied = { search: "", filter: "", token: "" };
    const first = ctx.dashLogFirst(s, false);
    ctx.dbChanged();
    assert.equal(s.loading, false, "a new load may start at once");
    pending[0].answer(200, logPage(["old1", "old2"]));
    await first;
    assert.equal(s.rows, null, "the old database's rows are not shown");
    // A new first load belongs to the new database
    const again = ctx.dashLogFirst(s, false);
    pending[1].answer(200, logPage(["new1"]));
    await again;
    assert.deepEqual([...s.rows.map((r) => r.id)], ["new1"]);
    // A next page asked before a change lands nowhere
    s.hasMore = true;
    ctx.dashLogMore(s);
    ctx.dbChanged();
    pending[2].answer(200, logPage(["old-next"]));
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(s.rows, null);
});

test("Usage ignores an answer for the old database and shows nothing of it", async () => {
    const ctx = load();
    const pending = heldApi(ctx);
    ctx.dashUse.data = { range: "7d", series: [], totals: {}, tokens: [] };
    ctx.dashUse.dataFor = ctx.dashUseKey();
    const late = ctx.dashUseLoad();
    ctx.dbChanged();
    assert.equal(ctx.dashUse.data, null);
    assert.equal(ctx.dashUse.dataFor, "");
    pending[0].answer(200, { range: "7d", series: [{ start: "x" }], totals: {}, tokens: [] });
    await late;
    assert.equal(ctx.dashUse.data, null);
});

test("the list shown reads again at once; another page waits until it is opened", () => {
    const ctx = load();
    heldApi(ctx);
    const shown = [];
    for (const page of ["tokens", "zones", "apikeys", "activity", "access", "usage", "account", "settings"]) {
        ctx.DASH_PAGE_IMPL[page] = (info) => shown.push([page, info.fresh]);
    }
    ctx.uiPageCurrent = "access";
    ctx.dbChanged();
    assert.deepEqual(shown, [["access", true]]);
    assert.deepEqual(ctx.refreshed, ["attention"], "the bell counts from the new token list");
    shown.length = 0;
    ctx.uiPageCurrent = "settings";
    ctx.dbChanged();
    assert.deepEqual(shown, [], "Settings reads its sections itself");
});
