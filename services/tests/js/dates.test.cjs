// Date and time helpers of js/ui.js: formats per the Date Format and Time Format
// preferences, in the app time zone, and datetime-local wall times back to
// instants across daylight saving changes (FW-9, FW-2).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadScripts } = require("./_load.cjs");

const ui = loadScripts(["js/common.js", "js/ui.js"]);
const H = 3600 * 1000;

// Probe a zone hour by hour for an offset change in one direction (no dates
// hard-coded from memory); narrows it to the minute
function findTransition(tz, fromMs, toMs, forward) {
    let prev = ui.uiZoneOffset(fromMs, tz);
    for (let t = fromMs + H; t <= toMs; t += H) {
        const off = ui.uiZoneOffset(t, tz);
        if (off !== prev && (off > prev) === forward) {
            let lo = t - H;
            let hi = t;
            while (hi - lo > 60000) {
                const mid = lo + Math.floor((hi - lo) / 120000) * 60000;
                if (ui.uiZoneOffset(mid, tz) === prev) lo = mid;
                else hi = mid;
            }
            return { at: hi, before: prev, after: off };
        }
        prev = off;
    }
    return null;
}

const wall = (ms, tz) => ui.uiToZonedInput(ms, tz);
const prefs = (date, time) => ({ date, time });

test("formats follow the Date Format and Time Format preferences", () => {
    const t = Date.UTC(2026, 9, 23, 13, 5, 9);
    assert.equal(ui.formatTime(t, { zone: "UTC", prefs: prefs("dmy", "24") }), "23/10/2026, 13:05:09");
    assert.equal(ui.formatTime(t, { zone: "UTC", prefs: prefs("mdy", "24") }), "10/23/2026, 13:05:09");
    assert.equal(ui.formatTime(t, { zone: "UTC", prefs: prefs("ymd", "12"), seconds: false }), "2026-10-23, 1:05 PM");
    assert.equal(ui.formatDay(t, { zone: "UTC", prefs: prefs("ymd", "24") }), "2026-10-23");
    assert.equal(ui.formatClock(Date.UTC(2026, 0, 1, 0, 7), { zone: "UTC", prefs: prefs("dmy", "12") }), "12:07 AM");
    assert.equal(ui.formatClock(Date.UTC(2026, 0, 1, 12, 7), { zone: "UTC", prefs: prefs("dmy", "12") }), "12:07 PM");
    // Browser order: numbers only, never month names
    const browser = ui.formatDay(t, { zone: "UTC", prefs: prefs("browser", "24") });
    assert.match(browser, /^[\d./\- ]+$/);
    assert.ok(browser.includes("2026") && browser.includes("23") && browser.includes("10"));
});

test("the app time zone decides the day and the clock", () => {
    const t = Date.UTC(2026, 0, 15, 22, 30);
    const p = prefs("dmy", "24");
    assert.equal(ui.formatTime(t, { zone: "Asia/Jerusalem", prefs: p, seconds: false }), "16/01/2026, 00:30");
    assert.equal(ui.formatTime(t, { zone: "UTC", prefs: p, seconds: false }), "15/01/2026, 22:30");
    assert.equal(ui.formatTime(t, { zone: "Asia/Kolkata", prefs: p, seconds: false }), "16/01/2026, 04:00");
});

test("list times: clock today, day and month this year, full date before", () => {
    const now = Date.UTC(2026, 5, 10, 12, 0);
    const p = prefs("dmy", "24");
    assert.equal(ui.formatListTime(Date.UTC(2026, 5, 10, 8, 15), { zone: "UTC", prefs: p, now }), "08:15");
    assert.equal(ui.formatListTime(Date.UTC(2026, 2, 4, 8, 15), { zone: "UTC", prefs: p, now }), "04/03");
    assert.equal(ui.formatListTime(Date.UTC(2025, 2, 4, 8, 15), { zone: "UTC", prefs: p, now }), "04/03/2025");
});

test("unknown zones and preferences fall back to UTC and the defaults", () => {
    assert.equal(ui.uiSetAppZone("localtime"), "UTC");
    assert.equal(ui.uiSetAppZone("Not/AZone"), "UTC");
    assert.equal(ui.uiSetAppZone("Asia/Jerusalem"), "Asia/Jerusalem");
    ui.uiSetAppZone("UTC");
    const p = ui.uiSetDatePrefs("nonsense", "13");
    assert.equal(p.date, "browser");
    assert.equal(p.time, "24");
});

test("UTC round trip", () => {
    const t = Date.UTC(2026, 3, 1, 9, 45);
    assert.equal(wall(t, "UTC"), "2026-04-01T09:45");
    assert.equal(ui.uiFromZonedInput("2026-04-01T09:45", "UTC"), new Date(t).toISOString());
    assert.equal(ui.uiFromZonedInput("not a date", "UTC"), null);
});

test("a 30-minute offset zone round trips (Asia/Kolkata)", () => {
    const t = Date.UTC(2026, 6, 1, 6, 0);
    assert.equal(ui.uiZoneOffset(t, "Asia/Kolkata"), 330);
    assert.equal(wall(t, "Asia/Kolkata"), "2026-07-01T11:30");
    assert.equal(ui.uiFromZonedInput("2026-07-01T11:30", "Asia/Kolkata"), new Date(t).toISOString());
});

test("a 30-minute zone with daylight saving round trips (Australia/Adelaide)", () => {
    for (const t of [Date.UTC(2026, 0, 10, 3, 0), Date.UTC(2026, 6, 10, 3, 0)]) {
        assert.equal(ui.uiFromZonedInput(wall(t, "Australia/Adelaide"), "Australia/Adelaide"), new Date(t).toISOString());
    }
});

test("Asia/Jerusalem: a wall time the clock skips moves forward by the gap", () => {
    const tz = "Asia/Jerusalem";
    const tr = findTransition(tz, Date.UTC(2026, 1, 1), Date.UTC(2026, 5, 1), true);
    assert.ok(tr, "found the spring change");
    const gapMin = tr.after - tr.before;
    // The wall clock jumps at the change: the wall time 30 minutes after the
    // last wall time before it is never shown
    const lastWall = Date.parse(wall(tr.at - 60000, tz) + ":00Z");
    const skipped = new Date(lastWall + 31 * 60000).toISOString().slice(0, 16);
    const back = Date.parse(ui.uiFromZonedInput(skipped, tz));
    const shownAgain = Date.parse(wall(back, tz) + ":00Z");
    assert.equal(shownAgain - Date.parse(skipped + ":00Z"), gapMin * 60000);
});

test("Asia/Jerusalem: a wall time the clock passes twice is the earlier instant", () => {
    const tz = "Asia/Jerusalem";
    const tr = findTransition(tz, Date.UTC(2026, 8, 1), Date.UTC(2026, 11, 1), false);
    assert.ok(tr, "found the autumn change");
    const early = tr.at - 30 * 60000;
    const later = early + (tr.before - tr.after) * 60000;
    assert.equal(wall(early, tz), wall(later, tz), "the same wall time twice");
    assert.equal(ui.uiFromZonedInput(wall(early, tz), tz), new Date(early).toISOString());
});

test("formatAgo and counts", () => {
    const now = Date.UTC(2026, 0, 1, 12);
    assert.equal(ui.formatAgo(now - 20 * 1000, now), "just now");
    assert.equal(ui.formatAgo(now - 5 * 60000, now), "5 min ago");
    assert.equal(ui.formatAgo(now + 60000, now), "just now");
    assert.equal(ui.uiCountLabel(1, "entry", "entries"), "1 entry");
    assert.match(ui.uiCountLabel(1920, "entry", "entries"), /^1.?920 entries$/);
});
