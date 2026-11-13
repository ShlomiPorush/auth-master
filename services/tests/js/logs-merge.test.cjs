// Log rows from several answers are merged in server order (created_at DESC,
// id DESC), deduplicated by id, never reordered by arrival (plan section 4).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadScripts } = require("./_load.cjs");

const ui = loadScripts(["js/common.js", "js/ui.js"]);
const ids = (rows) => Array.from(rows, (r) => r.id);

test("rows sort by time, then id, both newest first", () => {
    const rows = ui.uiLogMerge([], [
        { id: "a", createdAt: "2026-01-01T10:00:00+00:00" },
        { id: "c", createdAt: "2026-01-01T10:00:00+00:00" },
        { id: "b", createdAt: "2026-01-01T10:00:01+00:00" },
    ]);
    assert.deepEqual(ids(rows), ["b", "c", "a"]);
});

test("instants are compared across zones and to the microsecond", () => {
    const rows = ui.uiLogMerge([], [
        { id: "1", createdAt: "2026-01-01T12:00:00.000001+02:00" },
        { id: "2", createdAt: "2026-01-01T10:00:00.000002+00:00" },
        { id: "3", createdAt: "2026-01-01T10:00:00+00:00" },
    ]);
    assert.deepEqual(ids(rows), ["2", "1", "3"]);
});

test("a refresh adds rows into their place, never wipes and never repeats", () => {
    const shown = ui.uiLogMerge([], [
        { id: "x9", createdAt: "2026-01-01T10:00:05Z" },
        { id: "x5", createdAt: "2026-01-01T10:00:03Z" },
    ]);
    // a row that committed late with an older time, a new top row and a repeat
    const merged = ui.uiLogMerge(shown, [
        { id: "x7", createdAt: "2026-01-01T10:00:04Z" },
        { id: "y1", createdAt: "2026-01-01T10:00:06Z" },
        { id: "x9", createdAt: "2026-01-01T10:00:05Z" },
    ]);
    assert.deepEqual(ids(merged), ["y1", "x9", "x7", "x5"]);
    assert.deepEqual(ids(ui.uiLogMerge(merged, [])), ids(merged));
});

test("Load more appends older rows; an answer with only known rows changes nothing", () => {
    const first = ui.uiLogMerge([], [{ id: "b", createdAt: "2026-01-02T00:00:00Z" }]);
    const more = ui.uiLogMerge(first, [{ id: "a", createdAt: "2026-01-01T00:00:00Z" }]);
    assert.deepEqual(ids(more), ["b", "a"]);
    assert.deepEqual(ids(ui.uiLogMerge(more, [{ id: "a", createdAt: "2026-01-01T00:00:00Z" }])), ["b", "a"]);
});

test("a refresh offers only rows above the last shown row while older rows remain", () => {
    const shown = ui.uiLogMerge([], [
        { id: "s2", createdAt: "2026-01-01T10:00:20Z" },
        { id: "s1", createdAt: "2026-01-01T10:00:10Z" },
    ]);
    const collected = [
        { id: "n1", createdAt: "2026-01-01T10:00:30Z" },
        { id: "s2", createdAt: "2026-01-01T10:00:20Z" },
        { id: "late", createdAt: "2026-01-01T10:00:15Z" },
        { id: "older", createdAt: "2026-01-01T10:00:05Z" },
    ];
    assert.deepEqual(ids(ui.uiLogFresh(shown, collected, true)), ["n1", "late"]);
    // The whole list is loaded: every row not shown is new
    assert.deepEqual(ids(ui.uiLogFresh(shown, collected, false)), ["n1", "late", "older"]);
});
