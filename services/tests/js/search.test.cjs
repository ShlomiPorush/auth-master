// The global search (js/dashboard.js) and the shared list data (js/ui.js) run
// against a fake API in a page without markup: stale answers, cache
// invalidation after a change and after a database change, hostile and long
// names, the Settings index, the nav counter and the router's sub-addresses.
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

function load(extra) {
    const ctx = loadScripts(["js/text.js", "js/common.js", "js/ui.js", "js/dashboard.js"], Object.assign({ document: PAGE }, extra || {}));
    ctx.showToast = () => {};
    return ctx;
}

// The fake API: handler(path) gives { status, data } or a promise of it
function fakeApi(ctx, handler) {
    const calls = [];
    ctx.adminJson = (path, opts) => {
        calls.push({ path, method: (opts && opts.method) || "GET" });
        return Promise.resolve(handler(path)).then(res => {
            const r = res || { status: 200, data: [] };
            return { ok: r.status < 400, status: r.status, data: r.data, json: true, error: "" };
        });
    };
    return calls;
}

const tok = (id, name, extra) => Object.assign({ id, name, description: "", isActive: true, isExpired: false, grants: [] }, extra || {});
const names = (r) => Array.from(r.items, i => i.title);
const settle = () => new Promise(r => setImmediate(r));

test("search reads the shared token rows once, then not again within 30 seconds", async () => {
    const ctx = load();
    const calls = fakeApi(ctx, p => (p === "/admin/api/tokens" ? { status: 200, data: [tok("1", "Orders service")] } : { status: 200, data: [] }));
    await ctx.dashSearchRead("tokens");
    assert.deepEqual(names(ctx.dashSearchResults("ord").tokens), ["Orders service"]);
    // the search shares the pages' store
    assert.equal(ctx.dashTokenStore.rows.length, 1);
    await ctx.dashSearchRead("tokens");
    assert.equal(calls.filter(c => c.path === "/admin/api/tokens").length, 1);
});

test("a change marks the data stale: the next search reads it again without a reload", async () => {
    const ctx = load();
    let rows = [tok("1", "Orders service")];
    const calls = fakeApi(ctx, p => (p === "/admin/api/tokens" ? { status: 200, data: rows } : { status: 200, data: [] }));
    await ctx.dashSearchRead("tokens");
    assert.deepEqual(names(ctx.dashSearchResults("orders").tokens), ["Orders service"]);
    // The token dialog renames it and marks tokens stale
    rows = [tok("1", "Invoices service")];
    ctx.uiDataStale("tokens");
    await ctx.dashSearchRead("tokens");
    assert.equal(calls.filter(c => c.path === "/admin/api/tokens").length, 2);
    assert.deepEqual(names(ctx.dashSearchResults("orders").tokens), []);
    assert.deepEqual(names(ctx.dashSearchResults("invoices").tokens), ["Invoices service"]);
});

test("data read more than 30 seconds ago is read again when the search opens or the query changes", async () => {
    const ctx = load();
    const calls = fakeApi(ctx, p => (p === "/admin/api/zones" ? { status: 200, data: [{ id: "z", name: "orders", description: "" }] } : { status: 200, data: [] }));
    await ctx.dashSearchRead("zones");
    ctx.uiDataState.zones.readAt = Date.now() - 31000;
    await ctx.dashSearchRead("zones");
    assert.equal(calls.filter(c => c.path === "/admin/api/zones").length, 2);
});

test("a read that a change overtook does not count as fresh", async () => {
    const ctx = load();
    let release;
    const held = new Promise(r => { release = r; });
    let first = true;
    const calls = fakeApi(ctx, p => {
        if (p !== "/admin/api/api-keys") return { status: 200, data: { active: false } };
        if (first) { first = false; return held; }
        return { status: 200, data: [{ id: "k", name: "renamed", scopes: "validate" }] };
    });
    const reading = ctx.dashSearchRead("apikeys");
    ctx.uiDataStale("apikeys");
    release({ status: 200, data: [{ id: "k", name: "old", scopes: "validate" }] });
    await reading;
    await settle();
    assert.ok(ctx.uiDataNeedsRead("apikeys", 30000), "still stale after the overtaken read");
    await ctx.dashSearchRead("apikeys");
    assert.deepEqual(names(ctx.dashSearchResults("renamed").apikeys), ["renamed"]);
    assert.equal(calls.filter(c => c.path === "/admin/api/api-keys").length, 2);
});

test("a database change drops the rows and ignores a late answer from the previous database", async () => {
    const ctx = load();
    let releaseOld;
    const oldAnswer = new Promise(r => { releaseOld = r; });
    let db = "old";
    fakeApi(ctx, p => {
        if (p === "/health") return { status: 200, data: { db: true, redis: true } };
        if (p !== "/admin/api/tokens") return { status: 200, data: [] };
        return db === "old" ? oldAnswer : { status: 200, data: [tok("n", "New database token")] };
    });
    ctx.dashSearch.open = true;
    const oldRead = ctx.dashSearchRead("tokens");
    // WP2 dispatches ui:db-changed once the switch completed
    db = "new";
    ctx.dashSearchDbChanged();
    assert.equal(ctx.dashSearchResults("token").tokens.loading, true, "nothing from before the change is shown");
    await ctx.dashSearchRead("tokens");
    await settle();
    assert.deepEqual(names(ctx.dashSearchResults("token").tokens), ["New database token"]);
    // The previous database answers late: neither the search nor the shared store take it
    releaseOld({ status: 200, data: [tok("o", "Old database token")] });
    await oldRead;
    await settle();
    assert.deepEqual(names(ctx.dashSearchResults("token").tokens), ["New database token"]);
    assert.deepEqual(Array.from(ctx.dashTokenStore.rows, t => t.name), ["New database token"]);
});

test("rows held from before a database change are never shown, even without a read", () => {
    const ctx = load();
    const slot = ctx.dashSearchSlot("zones");
    slot.rows = [{ id: "z", name: "orders" }];
    slot.gen = ctx.uiDataGen;
    assert.equal(ctx.dashSearchResults("ord").zones.count, 1);
    ctx.uiDataReset();
    assert.equal(ctx.dashSearchResults("ord").zones.count, 0);
});

test("hostile and long names are escaped, marked and kept whole", () => {
    const ctx = load();
    const hostile = '<img src=x onerror="alert(1)">&amp;';
    const long = "Token " + "x".repeat(2000);
    const slot = ctx.dashSearchSlot("tokens");
    slot.rows = [tok("h", hostile, { description: "<b>bold</b>" }), tok("l", long)];
    slot.gen = ctx.uiDataGen;
    const r = ctx.dashSearchResults("<img");
    assert.deepEqual(names(r.tokens), [hostile]);
    const html = ctx.dashSearchItemHtml(r.tokens.items[0], "<img", 0, true);
    assert.ok(!/<img/i.test(html), "no markup from the name");
    assert.ok(html.includes("<mark>&lt;img</mark>"), "the match is marked on the escaped text");
    assert.ok(html.includes("&amp;amp;"), "an entity in the name stays text");
    assert.ok(!html.includes("<b>bold"), "the description is escaped too");
    const longHit = ctx.dashSearchResults("xxxx").tokens.items[0];
    assert.equal(longHit.title, long);
    assert.ok(ctx.dashSearchItemHtml(longHit, "xxxx", 1, false).includes("x".repeat(1990)));
    // A name whose lower case has another length is shown unmarked, never cut
    assert.equal(ctx.dashSearchMark("İstanbul", "i"), "İstanbul");
});

test("Settings results come from SETTINGS_INDEX, by name first, then by field id, and lead to their address", () => {
    const ctx = load();
    const index = ctx.SETTINGS_INDEX;
    ctx.SETTINGS_INDEX = undefined;
    assert.equal(ctx.dashSearchResults("zone").settings.count, 0, "no index: no Settings results and no error");
    ctx.SETTINGS_INDEX = index;
    ctx.UI_TEXT["test.general"] = "General";
    ctx.UI_TEXT["test.tz"] = "Time zone";
    ctx.UI_TEXT["test.retention"] = "Keep logs for";
    ctx.SETTINGS_INDEX = [
        { section: "general", field: "", key: "test.general" },
        { section: "general", field: "timezone", key: "test.tz" },
        { section: "general", field: "zone_retention_days", key: "test.retention" },
        { section: "Bad/Section", field: "x", key: "test.tz" },
    ];
    const r = ctx.dashSearchResults("zone").settings;
    assert.deepEqual(Array.from(r.items, i => [i.title, i.sub, i.section, i.field]), [
        ["Time zone", "General", "general", "timezone"],
        ["Keep logs for", "zone_retention_days", "general", "zone_retention_days"],
    ]);
});

test("the search lists at most 50 of a kind and counts them all", () => {
    const ctx = load();
    const slot = ctx.dashSearchSlot("zones");
    slot.rows = Array.from({ length: 120 }, (_, i) => ({ id: "z" + i, name: "zone" + i }));
    slot.gen = ctx.uiDataGen;
    const r = ctx.dashSearchResults("zone").zones;
    assert.equal(r.count, 120);
    assert.equal(r.items.length, 50);
});

test("the Tokens counter counts enabled tokens expiring within 7 days, red within a day", () => {
    const ctx = load();
    const now = Date.UTC(2026, 10, 16, 12);
    const at = h => new Date(now + h * 3600 * 1000).toISOString();
    const rows = [
        tok("a", "a", { expiresAt: at(72) }),
        tok("b", "b", { expiresAt: at(400) }),
        tok("c", "c", { expiresAt: at(10), isActive: false }),
        tok("d", "d", { expiresAt: at(-1), isExpired: true }),
    ];
    assert.deepEqual(JSON.parse(JSON.stringify(ctx.dashExpiryCount(rows, now))), { n: 1, tone: "warn" });
    rows.push(tok("e", "e", { expiresAt: at(5) }));
    assert.deepEqual(JSON.parse(JSON.stringify(ctx.dashExpiryCount(rows, now))), { n: 2, tone: "fail" });
    assert.deepEqual(JSON.parse(JSON.stringify(ctx.dashExpiryCount(null, now))), { n: 0, tone: "warn" });
});

test("the router reads a part of a page from the address and builds it back", () => {
    const ctx = load();
    ctx.uiPages = ctx.PAGES;
    const parse = p => JSON.parse(JSON.stringify(ctx.uiParsePath(p)));
    assert.deepEqual(parse("/admin/settings"), { id: "settings", sub: "" });
    assert.deepEqual(parse("/admin/settings/redis"), { id: "settings", sub: "redis" });
    assert.deepEqual(parse("/admin/settings/rate-limit/"), { id: "settings", sub: "rate-limit" });
    assert.deepEqual(parse("/admin/settings/%3Cscript%3E"), { id: "settings", sub: "" }, "an id that is not an id opens the first section");
    assert.deepEqual(parse("/admin/settingsx"), { id: "", sub: "" });
    assert.deepEqual(parse("/admin/logs/activity"), { id: "activity", sub: "" });
    assert.equal(ctx.uiPageUrl("settings", "redis", "", "redis_url"), "/admin/settings/redis#redis_url");
    assert.equal(ctx.uiPageUrl("tokens", "", "q=a%20b", ""), "/admin/tokens?q=a%20b");
    assert.equal(ctx.uiPageUrl("settings", "../x", "", ""), "/admin/settings");
});

test("every page has a help key, and its paragraphs are its lines", () => {
    const ctx = load();
    for (const id of Object.keys(ctx.PAGES)) {
        assert.ok(ctx.DASH_HELP[id], "help for " + id);
        assert.ok(Object.prototype.hasOwnProperty.call(ctx.UI_TEXT, ctx.DASH_HELP[id]), ctx.DASH_HELP[id] + " is in the catalog");
    }
    ctx.UI_TEXT["help.tokens"] = "First paragraph.\n\nSecond <b>one</b>.\n";
    assert.deepEqual(Array.from(ctx.dashHelpParagraphs("tokens")), ["First paragraph.", "Second <b>one</b>."]);
});

test("the browser tab names an opened level and falls back to the page name", () => {
    const ctx = load();
    ctx.uiPages = ctx.PAGES;
    ctx.uiPageCurrent = "usage";
    ctx.document.title = "";
    ctx.UI_TEXT["common.page-title-sub"] = "{sub} · {page} · {app}";
    const page = ctx.t("shell.usage");
    const app = ctx.t("common.auth-service");
    ctx.uiSetSubTitle("Orders service");
    assert.equal(ctx.document.title, "Orders service · " + page + " · " + app);
    ctx.UI_TEXT["common.page-title-sub"] = "";
    ctx.uiSetSubTitle("Other");
    assert.equal(ctx.document.title, ctx.t("common.page-title", { page, app }));
});

test("Needs attention opens the Database and Redis sections of Settings, and turns red with an urgent token", () => {
    const ctx = load();
    ctx.dashAttn.health = { db: false, redis: false };
    ctx.dashTokenStore.rows = [tok("e", "e", { expiresAt: new Date(Date.now() + 3600 * 1000).toISOString() })];
    const items = Array.from(ctx.dashAttnItems(), i => [i.go, i.sub_ || "", i.tone]);
    assert.deepEqual(items, [["settings", "database", "fail"], ["settings", "redis", "fail"], ["tokens", "", "fail"]]);
});
