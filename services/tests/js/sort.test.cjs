// TB-1 and TB-28 in js/ui.js and js/dashboard.js: a list that arrives in an order
// of its own (Usage: Calls, high to low) shows that order in its headers and its
// Sort button without a "Default order" choice; a sort that lands on it puts the
// arrival order back; div list headers say their order in hidden words.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadScripts } = require("./_load.cjs");

const PAGE = { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {} };

function load() {
    const ctx = loadScripts(["js/text.js", "js/common.js", "js/ui.js", "js/dashboard.js"], { document: PAGE });
    // Stand-ins for the words the catalog does not have yet; the tests read them
    // through the catalog, so they hold whatever the words are
    if (!ctx.UI_TEXT["sort.sorted-ascending"]) ctx.UI_TEXT["sort.sorted-ascending"] = "sorted ascending";
    if (!ctx.UI_TEXT["sort.sorted-descending"]) ctx.UI_TEXT["sort.sorted-descending"] = "sorted descending";
    return ctx;
}

const CALLS = { key: "calls", label: "usage.calls", kind: "number" };
const NAME = { key: "name", label: "usage.token", kind: "text" };

test("with a default order, a first click on its column turns it around", () => {
    const ctx = load();
    const def = ctx.DASH_USE_DEFAULT;
    const state = { key: "", dir: "" };
    assert.equal(ctx.uiSortAria(state, "calls", def), "descending", "the arrival order shows in the header");
    ctx.uiSortToggle(state, CALLS, def);
    assert.deepEqual({ ...state }, { key: "calls", dir: "asc" });
    // Back on Calls, high to low: the list's own order again, exactly
    ctx.uiSortToggle(state, CALLS, def);
    assert.deepEqual({ ...state }, { key: "", dir: "" });
    // Another column starts in its own first order
    ctx.uiSortToggle(state, NAME, def);
    assert.deepEqual({ ...state }, { key: "name", dir: "asc" });
    assert.equal(ctx.uiSortAria(state, "calls", def), "none");
});

test("without a default order, nothing is sorted until a column is picked", () => {
    const ctx = load();
    const state = { key: "", dir: "" };
    assert.equal(ctx.uiSortAria(state, "name"), "none");
    ctx.uiSortToggle(state, { key: "name", kind: "text" });
    ctx.uiSortToggle(state, { key: "name", kind: "text" });
    assert.deepEqual({ ...state }, { key: "name", dir: "desc" }, "no reset without a default");
});

test("the Sort button names the order shown: the default column, or Default order", () => {
    const ctx = load();
    assert.equal(ctx.dashSortLabel({ key: "", dir: "" }, ctx.DASH_USE_COLS, ctx.DASH_USE_DEFAULT),
        ctx.t("list.sort", { column: ctx.t("usage.calls"), order: ctx.t("sort.high-to-low") }));
    assert.equal(ctx.dashSortLabel({ key: "", dir: "" }, ctx.DASH_TOK_COLS), ctx.t("list.sort-default-order"));
    assert.equal(ctx.dashSortLabel({ key: "lastUsed", dir: "desc" }, ctx.DASH_TOK_COLS),
        ctx.t("list.sort", { column: ctx.t("tokens.last-used"), order: ctx.t("sort.newest-first") }));
    // The dot shows only for an order other than the list's own
    ctx.dashUse.sort = { key: "", dir: "" };
    assert.doesNotMatch(ctx.dashUseSortButton(), /is-set/);
    ctx.dashUse.sort = { key: "name", dir: "asc" };
    assert.match(ctx.dashUseSortButton(), /ui-sort-menu-btn is-set/);
});

test("a header says its order in hidden words and draws its arrow from data-sort-state", () => {
    const ctx = load();
    const sorted = ctx.dashSortHead({ key: "", dir: "" }, CALLS, ctx.DASH_USE_DEFAULT);
    assert.match(sorted, /data-sort-state="descending"/);
    assert.ok(sorted.includes('<span class="ui-sr">' + ctx.esc(ctx.t("sort.sorted-descending")) + "</span>"));
    assert.doesNotMatch(sorted, /aria-sort/, "aria-sort belongs on a real th only");
    assert.match(sorted, /class="ui-td ui-num"/, "a number column lines up at its end");
    const plain = ctx.dashSortHead({ key: "calls", dir: "asc" }, NAME, ctx.DASH_USE_DEFAULT);
    assert.match(plain, /data-sort-state="none"/);
    assert.doesNotMatch(plain, /ui-sr/);
});

test("the Usage token list sorts; its zone and address tables opt out", () => {
    const ctx = load();
    const d = {
        range: "7d", retentionDays: 30, partial: false, timeZone: "UTC", series: [],
        totals: { calls: 0, allowed: 0, denied: 0, grantsChecks: 0, ipCount: 0, lastCallAt: null },
        token: { id: "t1", name: "n", status: "active" }, areasCount: 1, areas: [{ area: "a", truncated: false, calls: 1, allowed: 1, denied: 0, levels: {} }],
        addresses: [{ address: "1.2.3.4", truncated: false, calls: 1, denied: 0, lastCallAt: null }],
    };
    const html = ctx.dashUseTokenHtml(d);
    assert.match(html, /id="usage-areas" role="table" data-nosort/);
    assert.match(html, /id="usage-addrs" role="table" data-nosort/);
});
