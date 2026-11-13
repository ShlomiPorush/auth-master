// Small shared helpers of js/ui.js: escaping, user text, copyable values,
// list sorting, the dialog builder's required options and the refresh scheduler.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadScripts } = require("./_load.cjs");

const ui = loadScripts(["js/common.js", "js/ui.js"]);

test("esc escapes the five markup characters", () => {
    assert.equal(ui.esc(`<a href="x" onclick='y'>&</a>`), "&lt;a href=&quot;x&quot; onclick=&#39;y&#39;&gt;&amp;&lt;/a&gt;");
    assert.equal(ui.esc(null), "");
    assert.equal(ui.esc(5), "5");
});

test("user text keeps its own direction", () => {
    assert.equal(ui.uiUserText("<b>שלום</b>"), '<bdi dir="auto">&lt;b&gt;שלום&lt;/b&gt;</bdi>');
});

test("copyable values are escaped in the attribute and the text", () => {
    const html = ui.uiCopyable(`1.2.3.4"><img src=x>`);
    assert.ok(!html.includes("<img"));
    assert.ok(html.includes('data-copy="1.2.3.4&quot;&gt;&lt;img src=x&gt;"'));
    assert.equal(ui.uiCopyable(""), '<span class="ui-muted">-</span>');
});

test("sorting: text A to Z first, dates newest first, empty values last both ways", () => {
    const rows = [{ n: "b", t: 2 }, { n: "A", t: NaN }, { n: "c", t: 5 }];
    const state = { key: "", dir: "" };
    const value = (r, k) => r[k];
    ui.uiSortToggle(state, { key: "n", kind: "text" });
    assert.deepEqual(Array.from(ui.uiSortRows(rows, state, value), (r) => r.n), ["A", "b", "c"]);
    ui.uiSortToggle(state, { key: "t", kind: "date" });
    assert.equal(state.dir, "desc");
    assert.deepEqual(Array.from(ui.uiSortRows(rows, state, value), (r) => r.n), ["c", "b", "A"]);
    ui.uiSortToggle(state, { key: "t", kind: "date" });
    assert.deepEqual(Array.from(ui.uiSortRows(rows, state, value), (r) => r.n), ["b", "c", "A"]);
    assert.equal(ui.uiSortAria(state, "t"), "ascending");
    assert.equal(ui.uiSortAria(state, "n"), "none");
});

test("a dialog needs a size and, unless it is a question, a close function", () => {
    assert.throws(() => ui.uiDialog({ title: "x", close() {} }), /size/);
    assert.throws(() => ui.uiDialog({ title: "x", size: "sm" }), /close/);
});

test("the refresh scheduler runs on demand and a mutation makes older reads stale", async () => {
    let runs = 0;
    ui.registerPageRefresh("p", 30000, () => { runs++; return Promise.resolve(); });
    await ui.uiRefreshNow("p");
    await ui.uiRefreshNow("p");
    assert.equal(runs, 2);
    const mark = ui.uiMutationMark();
    ui.uiMutated();
    assert.ok(ui.uiStaleSince(mark));
});
