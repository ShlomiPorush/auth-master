// WP4 list behaviors in js/dashboard.js without markup: count hints on the token
// pickers, values that copy in the logs, values the server cut, and empty states.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadScripts } = require("./_load.cjs");

const PAGE = { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {} };

function load() {
    const ctx = loadScripts(["js/text.js", "js/common.js", "js/ui.js", "js/dashboard.js"], { document: PAGE });
    ctx.showToast = () => {};
    return ctx;
}

const tok = (name, grants, extra) => ({ id: name, name, isActive: true, isExpired: false, grants, ...(extra || {}) });

test("each token picker option counts the rows its pick shows, ignoring its own filter (TB-18)", () => {
    const ctx = load();
    const now = Date.now();
    const rows = [
        tok("a", [{ area: "billing", level: "read" }]),
        tok("b", [{ area: "billing", level: "read,write,delete" }]),
        tok("c", [{ area: "orders", level: "write" }], { isActive: false }),
        tok("d", [{ area: "orders", level: "read" }]),
    ];
    // Zone set to billing, status Active: the zone counts ignore the zone filter
    const f = { q: "", status: "active", zone: "billing", level: "" };
    assert.deepEqual([...ctx.dashTokHints(rows, f, "zone", ["", "billing", "orders"], now)], [3, 2, 1]);
    // The level counts keep the zone and status filters
    assert.deepEqual([...ctx.dashTokHints(rows, f, "level", ["", "read", "write", "delete", "all"], now)], [2, 2, 1, 1, 1]);
    // The status counts keep the zone filter and ignore their own
    assert.deepEqual([...ctx.dashTokHints(rows, f, "status", ["all", "active", "disabled"], now)], [2, 2, 0]);
    // A count is what choosing it lists
    for (const zone of ["", "billing", "orders"]) {
        const shown = rows.filter((r) => ctx.dashTokMatch(r, { ...f, zone }, "", now)).length;
        assert.equal(ctx.dashTokHints(rows, f, "zone", [zone], now)[0], shown);
    }
});

test("the Activity Log actor copies when it is a user name; a system actor is a word", () => {
    const ctx = load();
    assert.match(ctx.dashLogActor("alice"), /class="ui-copy is-text ui-td-name" data-copy="alice"/);
    const system = ctx.dashLogActor("setup");
    assert.doesNotMatch(system, /data-copy/);
    assert.match(system, new RegExp(ctx.t("logs.actor.setup")));
});

test("Access Logs rows copy the token name, the zone and the address", () => {
    const ctx = load();
    const html = ctx.dashLogRowHtml(ctx.dashLogs.access, {
        id: "1", createdAt: "2026-11-01T10:00:00Z", tokenName: "ci <deploy>", area: "billing", level: "read", result: true, ipAddress: "10.0.0.1",
    });
    assert.match(html, /data-copy="ci &lt;deploy&gt;"/);
    assert.match(html, /data-copy="billing"/);
    assert.match(html, /data-copy="10\.0\.0\.1"/);
});

test("an address the server cut is shown cut and does not copy (P13)", () => {
    const ctx = load();
    const d = {
        range: "7d", retentionDays: 30, partial: false, timeZone: "UTC", series: [],
        totals: { calls: 2, allowed: 2, denied: 0, grantsChecks: 0, ipCount: 2, lastCallAt: null },
        token: { id: "t1", name: "n", status: "active" }, areasCount: 0, areas: [],
        addresses: [
            { address: "x".repeat(20), truncated: true, calls: 1, denied: 0, lastCallAt: null },
            { address: "10.0.0.2", truncated: false, calls: 1, denied: 0, lastCallAt: null },
        ],
    };
    const html = ctx.dashUseTokenHtml(d);
    assert.match(html, new RegExp("x{20}…"));
    assert.doesNotMatch(html, /data-copy="x+/);
    assert.match(html, /data-copy="10\.0\.0\.2"/);
});

test("an empty log list without filters has a second line; with filters, the filter hint", () => {
    const ctx = load();
    for (const s of [ctx.dashLogs.activity, ctx.dashLogs.access]) {
        assert.ok(s.emptyLine in ctx.UI_TEXT, s.id);
    }
});

// The dashboard's log tests use pages of rows numbered from the newest
const T0 = Date.UTC(2026, 0, 1, 12, 0, 0);
const logRow = (i) => ({ id: "r" + String(i).padStart(6, "0"), createdAt: new Date(T0 - i * 1000).toISOString(), actor: "a", action: "login" });
const logPage = (rows) => ({ logs: rows, total: rows.length, headCursor: rows.length ? "h:" + rows[0].id : null, nextCursor: rows.length ? "n:" + rows[rows.length - 1].id : null });

function api(ctx, handler) {
    const calls = [];
    ctx.adminJson = (path, opts) => {
        const url = new URL(path, "http://x");
        calls.push({ path: url.pathname, params: Object.fromEntries(url.searchParams), method: (opts && opts.method) || "GET" });
        const res = handler(url) || { status: 200, data: {} };
        return Promise.resolve({ ok: res.status < 400, status: res.status, data: res.data, json: true, error: "" });
    };
    return calls;
}

test("the freshness line says the reason and how long ago the shown rows were read (LD-12)", () => {
    const ctx = load();
    ctx.UI_TEXT["list.fresh-failed"] = "Could not update: {reason} Shown: {ago}";
    const now = Date.UTC(2026, 0, 1, 12, 0, 0);
    assert.equal(ctx.dashFreshText(now - 3 * 60000, "Service Unavailable", now), "Could not update: Service Unavailable Shown: 3 min ago");
});

test("a failed log update keeps the rows and its toast offers Try again, which reads again (FB-2)", async () => {
    const ctx = load();
    const toasts = [];
    ctx.showToast = (message, type, opts) => { toasts.push({ message, type, opts }); };
    const s = ctx.dashLogs.activity;
    s.applied = { search: "", filter: "", token: "" };
    api(ctx, () => ({ status: 200, data: logPage(Array.from({ length: 30 }, (_, i) => logRow(i))) }));
    await ctx.dashLogFirst(s, false);
    const loadedAt = s.at;
    assert.ok(loadedAt > 0);
    api(ctx, () => ({ status: 503, data: { detail: "down" } }));
    await ctx.dashLogRefresh(s);
    assert.equal(s.rows.length, 30);
    assert.equal(s.refreshFailed, "down");
    assert.equal(s.at, loadedAt, "the time of the last good read stays");
    assert.equal(toasts.length, 1);
    assert.equal(toasts[0].opts.action.label, "Try again");
    const calls = api(ctx, () => ({ status: 200, data: logPage([logRow(0)]) }));
    await toasts[0].opts.action.run();
    assert.equal(calls.length, 1, "Try again reads the list again");
    assert.equal(s.refreshFailed, "");
});

test("the sentinel loads the next page, but never after a failed page until Try again (TB-6)", async () => {
    const ctx = load();
    const s = ctx.dashLogs.activity;
    s.applied = { search: "", filter: "", token: "" };
    api(ctx, () => ({ status: 200, data: logPage(Array.from({ length: 30 }, (_, i) => logRow(i))) }));
    await ctx.dashLogFirst(s, false);
    ctx.uiPageCurrent = "activity";
    let calls = api(ctx, () => ({ status: 503, data: { detail: "down" } }));
    ctx.dashLogNearEnd(s);
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(calls.length, 1);
    assert.equal(s.moreFailed, "down");
    calls = api(ctx, () => ({ status: 200, data: logPage([logRow(30)]) }));
    ctx.dashLogNearEnd(s);
    assert.equal(calls.length, 0, "a failed page waits for Try again");
    // Try again is dashLogMore itself
    ctx.dashLogMore(s);
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(calls.length, 1);
    assert.equal(s.rows.length, 31);
    // Another page, or no more pages: nothing is asked while the page is not shown
    ctx.uiPageCurrent = "tokens";
    s.hasMore = true;
    ctx.dashLogNearEnd(s);
    assert.equal(calls.length, 1);
});
