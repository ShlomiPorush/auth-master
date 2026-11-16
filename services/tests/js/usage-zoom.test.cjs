// CH-1, CH-3, CH-9, CH-10 and FB-8 on the Usage page (js/dashboard.js) without
// markup: a picked bar, the span shown in words, zoom steps and snapping at the
// bar resolution the API returns, the overview strip's sliders, the zoom in the
// address, and "-" for a share without calls.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadScripts } = require("./_load.cjs");

const PAGE = { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {} };

function load() {
    const ctx = loadScripts(["js/text.js", "js/common.js", "js/ui.js", "js/dashboard.js"], { document: PAGE });
    ctx.showToast = () => {};
    ctx.uiSetAppZone("UTC");
    ctx.uiSetDatePrefs("DD/MM/YYYY", "24h");
    // The writer has not filled the new words yet: stand-ins to read
    Object.assign(ctx.UI_TEXT, {
        "usage.span": "{from} to {to}", "usage.chart.clear": "Clear", "usage.zoom.group": "Zoom",
        "usage.zoom.earlier": "Earlier", "usage.zoom.out": "Zoom out", "usage.zoom.in": "Zoom in", "usage.zoom.later": "Later", "usage.zoom.reset": "Reset zoom",
        "usage.strip.label": "Whole period", "usage.strip.start": "Start", "usage.strip.end": "End",
    });
    return ctx;
}

// 24 hourly bars from 2026-11-15 00:00 UTC, i calls in bar i
const H0 = Date.UTC(2026, 10, 15, 0);
const HOURS = Array.from({ length: 24 }, (_, i) => ({
    start: new Date(H0 + i * 3600000).toISOString(), end: new Date(H0 + (i + 1) * 3600000).toISOString(), allowed: i, denied: 0, coverage: "full",
}));
const DAY = { range: "24h", retentionDays: 30, partial: false, timeZone: "UTC", series: HOURS };
const zoomOf = (a, b) => ({ from: H0 + a * 3600000, to: H0 + (b + 1) * 3600000 });

test("a zoom snaps to the bars there are; too little left, or everything, is no zoom", () => {
    const ctx = load();
    const span = (z) => ({ ...ctx.dashUseSpan(HOURS, z) });
    assert.deepEqual(span(null), { a: 0, b: 23, zoomed: false });
    assert.deepEqual(span(zoomOf(4, 9)), { a: 4, b: 9, zoomed: true });
    // Edges inside a bar take in the bars they touch
    assert.deepEqual(span({ from: H0 + 4.5 * 3600000, to: H0 + 9.2 * 3600000 }), { a: 4, b: 9, zoomed: true });
    // After a refresh the hours move on: what is left of the zoom stays
    const later = HOURS.slice(6).concat(HOURS.slice(0, 6).map((b, i) => ({ ...b, start: new Date(H0 + (24 + i) * 3600000).toISOString(), end: new Date(H0 + (25 + i) * 3600000).toISOString() })));
    assert.deepEqual({ ...ctx.dashUseSpan(later, zoomOf(4, 9)) }, { a: 0, b: 3, zoomed: true });
    assert.deepEqual({ ...ctx.dashUseSpan(later, zoomOf(4, 6)) }, { a: 0, b: 23, zoomed: false }, "one bar left is less than the shortest span");
    assert.deepEqual(span(zoomOf(0, 23)), { a: 0, b: 23, zoomed: false });
});

test("the zoom buttons step by whole bars and stay inside the period (CH-9)", () => {
    const ctx = load();
    const step = (a, b, kind) => ({ ...ctx.dashUseZoomStep({ a, b }, 24, kind) });
    assert.deepEqual(step(0, 23, "in"), { a: 6, b: 17 }, "the middle half");
    assert.deepEqual(step(6, 17, "out"), { a: 0, b: 23 });
    assert.deepEqual(step(10, 13, "out"), { a: 8, b: 15 }, "twice the span around its middle");
    assert.deepEqual(step(6, 17, "earlier"), { a: 0, b: 11 });
    assert.deepEqual(step(2, 5, "earlier"), { a: 0, b: 3 }, "kept inside the period");
    assert.deepEqual(step(16, 21, "later"), { a: 18, b: 23 });
    assert.deepEqual(step(10, 11, "in"), { a: 10, b: 11 }, "never under the shortest span");
    assert.deepEqual(step(10, 11, "reset"), { a: 0, b: 23 });
    const can = ctx.dashUseZoomCan({ a: 0, b: 23 }, 24);
    assert.deepEqual({ ...can }, { earlier: false, out: false, in: true, later: false, reset: false });
});

test("a zoomed chart draws only its bars, scales to them, and offers Reset", () => {
    const ctx = load();
    ctx.dashUse.zoom = zoomOf(4, 9);
    ctx.dashUse.pick = "";
    const html = ctx.dashUseChartHtml(DAY);
    const bars = [...html.matchAll(/data-chart-i="(\d+)"/g)].map((m) => Number(m[1]));
    assert.deepEqual(bars, [4, 5, 6, 7, 8, 9]);
    assert.match(html, /<span style="top:0%">10<\/span>/, "the axis tops out over the bars shown");
    assert.match(html, /data-zoom-step="reset"[^>]*aria-label="Reset zoom"/);
    assert.doesNotMatch(html, /class="ui-icon-btn is-gone" data-zoom-step="reset"/);
    // The line above the chart names the span and its calls
    assert.match(html, /<b>15\/11\/2026, 04:00 to 15\/11\/2026, 10:00<\/b> 39 calls, 39 allowed, 0 denied/);
    // The strip's window covers bars 4 to 9 of 24; its edges are sliders with bounds
    assert.match(html, /class="ui-strip-win" aria-hidden="true" style="left:16\.667%;width:25\.000%"/);
    const start = /<span class="ui-strip-edge is-start"[^>]*/.exec(html)[0];
    assert.match(start, /role="slider" tabindex="0"/);
    assert.match(start, new RegExp(`aria-valuemin="${H0}" aria-valuemax="${H0 + 8 * 3600000}" aria-valuenow="${H0 + 4 * 3600000}"`));
    assert.match(start, /aria-valuetext="15\/11\/2026, 04:00"/);
    const end = /<span class="ui-strip-edge is-end"[^>]*/.exec(html)[0];
    assert.match(end, new RegExp(`aria-valuemin="${H0 + 6 * 3600000}" aria-valuemax="${H0 + 24 * 3600000}" aria-valuenow="${H0 + 10 * 3600000}"`));
});

test("unzoomed, Reset keeps its place hidden and the buttons that cannot act are disabled", () => {
    const ctx = load();
    ctx.dashUse.zoom = null;
    const html = ctx.dashUseChartHtml(DAY);
    assert.match(html, /class="ui-icon-btn is-gone" data-zoom-step="reset"[^>]* disabled/);
    assert.match(html, /data-zoom-step="earlier"[^>]* disabled/);
    assert.match(html, /data-zoom-step="in"[^>]*"><svg/);
    assert.match(html, /role="group" aria-label="Zoom"/);
});

test("a picked bar is pressed, the others fade, and the line names it with Clear (CH-1)", () => {
    const ctx = load();
    ctx.dashUse.zoom = null;
    ctx.dashUse.pick = HOURS[7].start;
    const html = ctx.dashUseChartHtml(DAY);
    assert.match(html, /ui-chart-bars has-pick/);
    assert.equal((html.match(/aria-pressed="true"/g) || []).length, 1);
    assert.match(html, /data-chart-i="7"[^>]*aria-pressed="true" tabindex="0"/, "the picked bar is the Tab stop");
    assert.match(html, /<b>15\/11\/2026, 07:00<\/b> 7 calls, 7 allowed, 0 denied <button type="button" class="ui-btn-link" data-chart-clear>Clear<\/button>/);
    // Every bar has its numbers as a tooltip, the same words as its name
    assert.match(html, /aria-label="15\/11\/2026, 07:00: 7 calls, 7 allowed, 0 denied" data-tip="15\/11\/2026, 07:00: 7 calls, 7 allowed, 0 denied"/);
    // A zoom drops the pick
    ctx.uiSetPageQuery = () => {};
    ctx.dashUse.shown = DAY;
    ctx.dashUseZoomTo({ a: 2, b: 9 });
    assert.equal(ctx.dashUse.pick, "");
    assert.equal(ctx.dashUse.zoom.from, H0 + 2 * 3600000);
    assert.equal(ctx.dashUse.zoom.to, H0 + 10 * 3600000);
    ctx.dashUseZoomTo({ a: 0, b: 23 });
    assert.equal(ctx.dashUse.zoom, null, "every bar shown is no zoom");
});

test("the zoom is in the address with the period, and read back from it", () => {
    const ctx = load();
    const z = zoomOf(4, 9);
    const q = ctx.dashUseQuery("", "24h", z);
    assert.equal(q, `range=24h&zoom=${(H0 + 4 * 3600000) / 1000}-${(H0 + 10 * 3600000) / 1000}`);
    ctx.location = { search: "?" + q, pathname: "/admin/usage" };
    ctx.dashUseReadQuery();
    assert.equal(ctx.dashUse.range, "24h");
    assert.equal(ctx.dashUse.zoom.from, z.from);
    assert.equal(ctx.dashUse.zoom.to, z.to);
    for (const bad of ["abc", "5-3", "1-", "-1-5", "1e3-2e3"]) {
        ctx.location = { search: "?zoom=" + bad, pathname: "/admin/usage" };
        ctx.dashUseReadQuery();
        assert.equal(ctx.dashUse.zoom, null, bad);
    }
});

test("bars read left to right in every language", () => {
    const ctx = load();
    const css = require("node:fs").readFileSync(require("node:path").join(__dirname, "../../static/public/css/ui.css"), "utf8");
    assert.match(css, /\.ui-chart-plot, \.ui-strip \{ direction: ltr; \}/);
    // The arrow keys no longer swap under right to left
    assert.doesNotMatch(String(ctx.dashUseBind), /dir === "rtl"/);
});

test("a denied share with no calls is a muted -, never 0% (FB-8)", () => {
    const ctx = load();
    assert.equal(ctx.dashUseDeniedSub({ calls: 0, denied: 0 }), '<span class="ui-muted">-</span>');
    assert.equal(ctx.dashUseDeniedSub({ calls: 200, denied: 3 }), "2% of calls");
    // The figure stands over its label
    assert.match(ctx.dashUseKpi("Calls", "5", ""), /^<div class="ui-kpi"><b>5<\/b><small>Calls<\/small>/);
});
