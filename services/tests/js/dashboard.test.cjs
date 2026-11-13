// The dashboard's data layer (js/dashboard.js) run against a fake API in a page
// without markup: the log lists (filters, refresh, Load more), stale answers,
// the reveal guard, unsaved changes and the token status rule.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadScripts } = require("./_load.cjs");

// A page without elements: the data layer runs, drawing finds nothing to draw into
const PAGE = {
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    readyState: undefined,
};

function load() {
    const ctx = loadScripts(["js/common.js", "js/ui.js", "js/dashboard.js"], { document: PAGE });
    ctx.toasts = [];
    ctx.showToast = (message, type) => { ctx.toasts.push([message, type]); };
    return ctx;
}

const T0 = Date.UTC(2026, 0, 1, 12, 0, 0);
const row = (i, ms) => ({ id: "r" + String(i).padStart(6, "0"), createdAt: new Date(ms).toISOString(), actor: "a", action: "login" });

// Rows numbered from the newest; cursors are opaque strings that carry the row
function page(rows) {
    return {
        logs: rows, total: rows.length, page: 1, limit: rows.length,
        headCursor: rows.length ? "h:" + rows[0].id : null,
        nextCursor: rows.length ? "n:" + rows[rows.length - 1].id : null,
    };
}

// The fake API: handler(params) gives { status, data }; every request is kept
function fakeApi(ctx, handler) {
    const calls = [];
    ctx.adminJson = (path, opts) => {
        const url = new URL(path, "http://x");
        const params = Object.fromEntries(url.searchParams);
        calls.push({ path: url.pathname, params, method: (opts && opts.method) || "GET" });
        const res = handler(params, url.pathname) || { status: 200, data: {} };
        return Promise.resolve({ ok: res.status < 400, status: res.status, data: res.data, json: true, error: res.error || "" });
    };
    return calls;
}

async function loaded(ctx, count) {
    const s = ctx.dashLogs.activity;
    s.applied = { search: "", filter: "" };
    const rows = Array.from({ length: count }, (_, i) => row(i, T0 - i * 1000));
    fakeApi(ctx, () => ({ status: 200, data: page(rows.slice(0, 30)) }));
    await ctx.dashLogFirst(s, false);
    return s;
}

test("R1: a new filter whose first load fails shows no rows of the old filter, and nothing follows their cursors", async () => {
    const ctx = load();
    const s = await loaded(ctx, 60);
    assert.equal(s.rows.length, 30);
    s.applied = { search: "", filter: "login" };
    const calls = fakeApi(ctx, () => ({ status: 503, data: { detail: "Service Unavailable" } }));
    await ctx.dashLogFirst(s, false);
    assert.equal(s.rows, null, "the old filter's rows are gone");
    assert.equal(s.failed, "Service Unavailable");
    assert.equal(Object.keys(s.heads).length + Object.keys(s.nexts).length, 0);
    // Load more does nothing; the refresh retries the first load instead of `since`
    calls.length = 0;
    ctx.dashLogMore(s);
    assert.equal(calls.length, 0);
    await ctx.dashLogRefresh(s);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].params.since, undefined);
    assert.equal(calls[0].params.action, "login");
});

test("R1: a failed reload of the same filter keeps its rows", async () => {
    const ctx = load();
    const s = await loaded(ctx, 60);
    fakeApi(ctx, () => ({ status: 503, data: {} }));
    await ctx.dashLogFirst(s, false);
    assert.equal(s.rows.length, 30);
    assert.ok(s.refreshFailed);
});

test("R2: rows already present below the last shown row end the refresh at once", async () => {
    const ctx = load();
    const s = await loaded(ctx, 30 + 5000);
    assert.equal(s.hasMore, true);
    // The lookback window holds thousands of older rows the page already had
    const older = Array.from({ length: 500 }, (_, i) => row(100 + i, T0 - 30000 - i * 10));
    const calls = fakeApi(ctx, () => ({ status: 200, data: page(older) }));
    await ctx.dashLogRefresh(s);
    assert.equal(calls.length, 1, "one page, no 20-page run");
    assert.equal(s.rows.length, 30);
    assert.equal(s.hasMore, true, "Load more stays");
});

test("R2: more than 20 full pages of new rows make the newest rows the list, with Load more", async () => {
    const ctx = load();
    const s = await loaded(ctx, 60);
    let n = 0;
    const calls = fakeApi(ctx, (params) => {
        if (params.since) {
            const rows = Array.from({ length: 500 }, () => { n += 1; return row(900000 - n, T0 + 3600000 - n); });
            return { status: 200, data: page(rows) };
        }
        const fresh = Array.from({ length: 30 }, (_, i) => row(990000 - i, T0 + 7200000 - i * 1000));
        return { status: 200, data: page(fresh) };
    });
    await ctx.dashLogRefresh(s);
    assert.equal(calls.filter((c) => c.params.since).length, 20);
    assert.equal(calls[calls.length - 1].params.page, "1", "then the newest page is loaded");
    assert.equal(s.rows[0].id, "r990000");
    assert.equal(s.hasMore, true);
    // Load more follows the new list's end
    const more = fakeApi(ctx, () => ({ status: 200, data: page([row(1, T0)]) }));
    ctx.dashLogMore(s);
    assert.equal(more.length, 1);
    assert.equal(more[0].params.before, "n:" + s.rows[s.rows.length - 1].id);
});

test("R7: a failed update keeps the rows, says so, and tells once per run of failures", async () => {
    const ctx = load();
    const s = await loaded(ctx, 60);
    ctx.toasts.length = 0;
    fakeApi(ctx, () => ({ status: 503, data: { detail: "down" } }));
    await ctx.dashLogRefresh(s);
    await ctx.dashLogRefresh(s);
    assert.equal(s.rows.length, 30);
    assert.equal(s.refreshFailed, "down");
    assert.deepEqual(ctx.toasts.map((t) => t[0]), ["Failed to load activity logs"]);
    fakeApi(ctx, () => ({ status: 200, data: page([]) }));
    await ctx.dashLogRefresh(s);
    assert.equal(s.refreshFailed, "");
});

test("R5: a refresh run that timed out or was replaced publishes nothing", async () => {
    const ctx = load();
    ctx.PAGE_REFRESH_TIMEOUT = 20;
    const seen = [];
    let release;
    ctx.registerPageRefresh("slow", 30000, (isCurrent) => new Promise((resolve) => {
        release = () => { seen.push(isCurrent()); resolve(); };
    }));
    await ctx.uiRefreshNow("slow");
    release();
    await new Promise((r) => setTimeout(r, 5));
    assert.deepEqual(seen, [false], "the late answer is told it is stale");
    // A log refresh told it is stale leaves the list alone
    const s = await loaded(ctx, 60);
    const before = s.rows.map((r) => r.id).join();
    fakeApi(ctx, () => ({ status: 200, data: page([row(999999, T0 + 5000)]) }));
    await ctx.dashLogRefresh(s, () => false);
    assert.equal(s.rows.map((r) => r.id).join(), before);
    assert.equal(s.pending.length, 0);
});

test("R4: a zones or API keys read that started before a save publishes nothing", async () => {
    const ctx = load();
    let answer;
    fakeApi(ctx, () => null);
    ctx.adminJson = () => new Promise((resolve) => { answer = resolve; });
    ctx.dash.zones = [{ id: "new", name: "after-save" }];
    const zones = ctx.dashZonesLoad();
    ctx.uiMutated();
    answer({ ok: true, status: 200, data: [{ id: "old", name: "before-save" }], json: true, error: "" });
    await zones;
    assert.equal(ctx.dash.zones[0].name, "after-save");

    ctx.dashKeys.rows = [{ id: "k2", name: "after-save" }];
    const keys = ctx.dashKeysLoad(false);
    ctx.uiMutated();
    answer({ ok: true, status: 200, data: [{ id: "k1", name: "before-save" }], json: true, error: "" });
    await keys;
    assert.equal(ctx.dashKeys.rows[0].name, "after-save");
});

test("R3: a reveal answer is used only while its dialog is open; a dropped layer is told", () => {
    const ctx = load();
    const open = { backdrop: { isConnected: true } };
    const gone = { backdrop: { isConnected: false } };
    assert.equal(ctx.dashRevealCurrent(open, false, ctx.dashRevealSeq), true);
    assert.equal(ctx.dashRevealCurrent(gone, false, ctx.dashRevealSeq), false);
    assert.equal(ctx.dashRevealCurrent(open, true, ctx.dashRevealSeq), false);
    const events = [];
    let removed = false;
    ctx.uiLayerDrop({ dispatchEvent: (e) => events.push(e.type), remove: () => { removed = true; } });
    assert.deepEqual(events, ["ui-drop"]);
    assert.ok(removed);
});

test("R6 and R9: an open dirty dialog counts as unsaved; Leave drops dialogs and resets forms", () => {
    const ctx = load();
    const field = { type: "text", value: "a" };
    let dropped = false;
    const d = {
        backdrop: { isConnected: true, dispatchEvent: () => {}, remove: () => { dropped = true; d.backdrop.isConnected = false; } },
        body: { querySelectorAll: () => [field] },
    };
    ctx.uiGuardDialog(d);
    assert.equal(ctx.uiUnsavedCount(), 0);
    field.value = "changed";
    assert.equal(ctx.uiUnsavedCount(), 1);
    let draft = "typed";
    ctx.uiUnsavedRegister(() => (draft ? 1 : 0), () => { draft = ""; });
    assert.equal(ctx.uiUnsavedCount(), 2);
    ctx.uiUnsavedDiscard();
    assert.ok(dropped);
    assert.equal(draft, "");
    assert.equal(ctx.uiUnsavedCount(), 0);
});

test("one status rule: a token both disabled and expired is Disabled in the tag and the chips", () => {
    const ctx = load();
    const t = { isActive: false, isExpired: true, grants: [] };
    const f = (status) => ({ q: "", status, zone: "", level: "" });
    assert.equal(ctx.dashTokStatus(t), "disabled");
    assert.equal(ctx.dashTokMatch(t, f("disabled"), "", Date.now()), true);
    assert.equal(ctx.dashTokMatch(t, f("expired"), "", Date.now()), false);
    assert.equal(ctx.dashTokMatch({ isActive: true, isExpired: true, grants: [] }, f("expired"), "", Date.now()), true);
});

test("R8: with storage blocked, a Light or Dark choice holds for this visit", () => {
    const toggles = [];
    const blocked = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() { throw new Error("blocked"); } };
    const ctx = loadScripts(["js/theme.js"], {
        localStorage: undefined,
        matchMedia: () => ({ matches: false, addEventListener() {} }),
        addEventListener() {},
        document: {
            documentElement: { classList: { toggle: (c, on) => toggles.push(on) }, setAttribute() {} },
            dispatchEvent() {},
        },
    });
    Object.defineProperty(ctx, "localStorage", { get: () => blocked });
    ctx.uiThemeSet("dark");
    assert.equal(ctx.uiThemeGet(), "dark");
    assert.equal(toggles[toggles.length - 1], true);
    ctx.uiThemeSet("system");
    assert.equal(ctx.uiThemeGet(), "system");
});

test("R11: the toast takes no action, busy state or duration", () => {
    const src = require("node:fs").readFileSync(require("node:path").join(__dirname, "../../static/public/js/ui.js"), "utf8");
    assert.ok(!/opts\.(action|duration)|onAction|ui-toast-action/.test(src));
});
