// The Usage page (js/dashboard.js) without markup: the axis scale, the chart's bars and
// labels, stale answers, and the token filter Usage hands to Access Logs.
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
    ctx.uiSetAppZone("UTC");
    ctx.uiSetDatePrefs("DD/MM/YYYY", "24h");
    return ctx;
}

function fakeApi(ctx, handler) {
    const calls = [];
    ctx.adminJson = (path) => {
        const url = new URL(path, "http://x");
        calls.push({ path: url.pathname, params: Object.fromEntries(url.searchParams) });
        return handler(url).then((res) => ({ ok: res.status < 400, status: res.status, data: res.data, json: true, error: "" }));
    };
    return calls;
}

function day(i, allowed, denied, coverage) {
    const start = new Date(Date.UTC(2026, 10, 7 + i)).toISOString();
    const end = new Date(Date.UTC(2026, 10, 8 + i)).toISOString();
    return { start, end, allowed, denied, coverage: coverage || "full" };
}

const WEEK = {
    range: "7d", retentionDays: 3, dataFrom: "2026-11-10T12:00:00+00:00", partial: true,
    series: [day(0, 0, 0, "none"), day(1, 0, 0, "none"), day(2, 0, 0, "none"), day(3, 5, 1, "partial"), day(4, 30, 0), day(5, 0, 0), day(6, 117, 3)],
};

test("the value axis tops out at a round, even number", () => {
    const ctx = load();
    assert.deepEqual([0, 1, 3, 7, 12, 120, 999, 1000, 4100].map(ctx.dashUseNiceMax), [2, 2, 6, 10, 20, 200, 1000, 1000, 5000]);
});

test("shares under one percent are not rounded down to zero, and no calls is no share (FB-8)", () => {
    const ctx = load();
    assert.equal(ctx.dashUsePct(0, 0), "-");
    assert.equal(ctx.dashUsePct(1, 1000), "under 1%");
    assert.equal(ctx.dashUsePct(3, 120), "3%");
});

test("the chart draws one bar per bucket, denied above allowed, and marks deleted logs", () => {
    const ctx = load();
    const html = ctx.dashUseChartHtml(WEEK);
    assert.equal((html.match(/data-chart-i="/g) || []).length, 7);
    assert.equal((html.match(/ui-chart-col is-none/g) || []).length, 3);
    assert.equal((html.match(/ui-chart-col is-partial/g) || []).length, 1);
    // The busiest day: 3 denied on top of 117 allowed, on an axis of 200
    const last = html.slice(html.indexOf('data-chart-i="6"'));
    assert.match(last, /is-fail" style="height:1\.50%"[^]*is-base" style="height:58\.50%"/);
    // An empty bucket has no segments at all (a zero-height segment would still show its gap)
    const empty = html.slice(html.indexOf('data-chart-i="5"'), html.indexOf('data-chart-i="6"'));
    assert.doesNotMatch(empty, /ui-chart-seg/);
    assert.match(html, /<span style="top:0%">200<\/span>/);
    // Every day of a week is named, in the user's date order
    assert.equal((html.match(/<span style="grid-column:\d+">/g) || []).length, 7);
    assert.match(html, /grid-column:7">13\/11</);
    // The bar's name says what a bucket without data means
    assert.match(html, /07\/11\/2026: No data: access logs older than 3 days are deleted/);
    assert.match(html, /10\/11\/2026: 6 calls, 5 allowed, 1 denied \(counted from 10\/11\/2026, 12:00; older logs are deleted\)/);
    assert.match(html, /Logs deleted/);
});

test("thirty days name every fifth day, counted back from today", () => {
    const ctx = load();
    const series = Array.from({ length: 30 }, (_, i) => day(i - 23, 1, 0));
    const html = ctx.dashUseChartHtml({ range: "30d", retentionDays: 30, partial: false, series });
    const cols = [...html.matchAll(/grid-column:(\d+)"/g)].map((m) => Number(m[1]));
    assert.deepEqual(cols, [5, 10, 15, 20, 25, 30]);
    assert.doesNotMatch(html, /Logs deleted/);
});

test("an answer for a period the user already left is dropped", async () => {
    const ctx = load();
    let release;
    const slow = new Promise((r) => { release = r; });
    const calls = fakeApi(ctx, (url) => (url.searchParams.get("range") === "24h" ? slow : Promise.resolve({ status: 200, data: { ...WEEK, totals: {}, tokens: [] } })));
    ctx.dashUse.range = "24h";
    const first = ctx.dashUseLoad();
    ctx.dashUse.range = "7d";
    await ctx.dashUseLoad();
    release({ status: 200, data: { range: "24h", series: [], totals: {}, tokens: [] } });
    await first;
    assert.equal(ctx.dashUse.data.range, "7d");
    assert.equal(ctx.dashUse.dataFor, "|7d");
    assert.deepEqual(calls.map((c) => c.params.range), ["24h", "7d"]);
});

test("a token page asks for that token, and a missing token is not a failure", async () => {
    const ctx = load();
    const calls = fakeApi(ctx, () => Promise.resolve({ status: 404, data: { detail: "Token not found" } }));
    ctx.dashUse.token = "unknown";
    ctx.dashUse.range = "30d";
    await ctx.dashUseLoad();
    assert.equal(calls[0].path, "/admin/api/usage/tokens/unknown");
    assert.equal(ctx.dashUse.notFound, true);
    assert.equal(ctx.dashUse.failed, "");
});

test("the Access Logs token filter reaches the API and belongs to the list's state", () => {
    const ctx = load();
    const s = ctx.dashLogs.access;
    s.applied = { search: "", filter: "", token: "" };
    const before = ctx.dashLogKey(s);
    s.applied = { search: "", filter: "false", token: "5b0c6f0e-4d0e-4c64-9d0b-2f3c1d3f9a11" };
    assert.notEqual(ctx.dashLogKey(s), before);
    const q = new URL(ctx.dashLogQuery(s, { limit: 30 }), "http://x").searchParams;
    assert.equal(q.get("token_id"), "5b0c6f0e-4d0e-4c64-9d0b-2f3c1d3f9a11");
    assert.equal(q.get("result"), "false");
    // The activity log has no token filter
    const a = ctx.dashLogs.activity;
    a.applied = { search: "", filter: "", token: "x" };
    assert.equal(new URL(ctx.dashLogQuery(a, {}), "http://x").searchParams.get("token_id"), null);
});

test("a token address that is not a token id is not sent to the API", async () => {
    const ctx = load();
    for (const bad of [".", "..", "%2e", "abc", "5B0C6F0E-4D0E-4C64-9D0B-2F3C1D3F9A11"]) {
        const calls = fakeApi(ctx, () => Promise.resolve({ status: 200, data: { series: [], totals: {}, tokens: [] } }));
        ctx.dashUse.token = bad;
        await ctx.dashUseLoad();
        assert.equal(calls.length, 0, bad);
        assert.equal(ctx.dashUse.notFound, true, bad);
    }
});

test("the report names its hours and days in the zone it was counted in, not the tab's zone", () => {
    const ctx = load(); // the tab shows UTC
    // Days that start at midnight in Jerusalem (UTC+2 in November)
    const series = Array.from({ length: 7 }, (_, i) => ({
        start: new Date(Date.UTC(2026, 10, 6 + i, 22)).toISOString(),
        end: new Date(Date.UTC(2026, 10, 7 + i, 22)).toISOString(),
        allowed: 1, denied: 0, coverage: "full",
    }));
    const d = { range: "7d", retentionDays: 30, partial: false, timeZone: "Asia/Jerusalem", series };
    const html = ctx.dashUseChartHtml(d);
    // In UTC the first bucket would be named 06/11; in the report's zone it is 07/11
    assert.match(html, /aria-label="07\/11\/2026: /);
    assert.doesNotMatch(html, /06\/11/);
    assert.match(html, /grid-column:7">13\/11</);
    assert.match(ctx.dashUseNotices(d), /This report uses another time zone[^]*Asia\/Jerusalem/);

    // Hours too
    const hours = [{ start: "2026-11-13T10:00:00Z", end: "2026-11-13T11:00:00Z", allowed: 1, denied: 0, coverage: "full" }];
    const hourly = ctx.dashUseChartHtml({ range: "24h", retentionDays: 30, partial: false, timeZone: "Asia/Jerusalem", series: hours });
    assert.match(hourly, /13\/11\/2026, 12:00/);
    assert.match(hourly, />12:00</);
});

test("a report in the tab's own zone, or one without a zone, shows no zone note", () => {
    const ctx = load();
    assert.equal(ctx.dashUseNotices({ ...WEEK, partial: false, timeZone: "UTC" }), "");
    assert.equal(ctx.dashUseNotices({ ...WEEK, partial: false }), "");
    assert.equal(ctx.dashUseZone({ timeZone: "Not/AZone" }), "UTC");
});

test("a report zone this browser cannot format is named, not hidden", () => {
    const ctx = load();
    const notes = ctx.dashUseNotices({ ...WEEK, partial: false, timeZone: "Not/AZone" });
    // Catalog text is escaped as a whole in markup: read an apostrophe as the page shows it
    assert.match(notes.replace(/&#39;/g, "'"), /This browser cannot show this report's time zone[^]*Not\/AZone[^]*UTC/);
    assert.doesNotMatch(notes, /This report uses another time zone/);
});

test("data that an earlier, shorter retention deleted is called that", () => {
    const ctx = load();
    const purged = { ...WEEK, dataFromReason: "purge", timeZone: "UTC" };
    assert.match(ctx.dashUseNotices(purged), /Older calls were deleted under an earlier, shorter retention, so these figures start at/);
    assert.match(ctx.dashUseChartHtml(purged), /07\/11\/2026: No data: these access logs were deleted under an earlier, shorter retention/);
    const kept = { ...WEEK, dataFromReason: "retention", timeZone: "UTC" };
    assert.match(ctx.dashUseNotices(kept), /Access logs are kept for 3 days/);
});
