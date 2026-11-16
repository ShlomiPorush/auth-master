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
