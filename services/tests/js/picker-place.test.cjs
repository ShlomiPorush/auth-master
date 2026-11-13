// MP-5 in js/ui.js: a picker panel hangs from its button on every screen, inside
// the room between the phone top bar and tab bar and inside every clipping box;
// it opens upward only when there is more room above, scrolls inside when taller
// than its side, and is shifted back inside the edges. Row menus use the same room.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadScripts } = require("./_load.cjs");

const rect = (top, left, width, height) => ({ top, left, width, height, bottom: top + height, right: left + width });

function page({ mtop, tabbar, boxes }) {
    const shown = (r) => ({ getClientRects: () => (r ? [r] : []), getBoundingClientRect: () => r });
    const styles = new Map();
    const body = { tag: "body" };
    let parent = body;
    for (const b of (boxes || []).slice().reverse()) {
        const el = { parentElement: parent, getBoundingClientRect: () => b.rect };
        styles.set(el, { overflowX: b.overflow, overflowY: b.overflow });
        parent = el;
    }
    const host = { parentElement: parent };
    const ctx = loadScripts(["js/common.js", "js/ui.js"], {
        innerWidth: 390,
        innerHeight: 844,
        Node: class Node {},
        requestAnimationFrame: (fn) => fn(),
        getComputedStyle: (el) => styles.get(el) || { overflowX: "visible", overflowY: "visible", direction: "ltr" },
        document: {
            body,
            querySelector: (sel) => (sel === ".ui-mtop" ? shown(mtop) : sel === ".ui-tabbar" ? shown(tabbar) : null),
        },
    });
    return { ctx, host };
}

function panelAt(left, width, height) {
    const classes = new Set();
    return {
        style: {},
        offsetHeight: height,
        classList: { remove: (...c) => c.forEach((x) => classes.delete(x)), toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), has: (c) => classes.has(c) },
        getBoundingClientRect() {
            const dx = this.style.transform ? Number(/-?\d+/.exec(this.style.transform)[0]) : 0;
            return rect(0, left + dx, width, height);
        },
    };
}

test("on a phone the panel opens under its button, inside the room above the tab bar", () => {
    const { ctx, host } = page({ mtop: rect(0, 0, 390, 56), tabbar: rect(780, 0, 390, 64) });
    const panel = panelAt(200, 260, 300);
    ctx.uiPickerPlace({ panel, host, button: { getBoundingClientRect: () => rect(300, 200, 130, 30) } });
    assert.equal(panel.classList.has("is-up"), false);
    // 780 - 8 (above the tab bar) - 330 (button bottom) - 6
    assert.equal(panel.style.maxHeight, "436px");
    // 200 + 260 = 460 passes 390 - 8: shifted back by 78px
    assert.equal(panel.style.transform, "translateX(-78px)");
});

test("it opens upward only when the room above is larger, and scrolls within it", () => {
    const { ctx, host } = page({ mtop: rect(0, 0, 390, 56), tabbar: rect(780, 0, 390, 64) });
    const panel = panelAt(20, 260, 400);
    ctx.uiPickerPlace({ panel, host, button: { getBoundingClientRect: () => rect(650, 20, 130, 30) } });
    assert.equal(panel.classList.has("is-up"), true);
    // 650 - (56 + 8) - 6
    assert.equal(panel.style.maxHeight, "580px");
    assert.equal(panel.style.transform, "");
});

test("a clipping box around it (a dialog body) cuts the room", () => {
    const { ctx, host } = page({ boxes: [{ rect: rect(200, 0, 390, 400), overflow: "auto" }] });
    const panel = panelAt(16, 200, 500);
    ctx.uiPickerPlace({ panel, host, button: { getBoundingClientRect: () => rect(260, 16, 130, 30) } });
    // below: 600 - 8 - 290 - 6 = 296; above: 260 - 208 - 6 = 46 -> stays below
    assert.equal(panel.classList.has("is-up"), false);
    assert.equal(panel.style.maxHeight, "296px");
});

test("a row menu stays between the bars", () => {
    const { ctx } = page({ mtop: rect(0, 0, 390, 56), tabbar: rect(780, 0, 390, 64) });
    const menu = { style: {}, offsetWidth: 190, offsetHeight: 120 };
    const anchor = { getBoundingClientRect: () => rect(700, 340, 34, 34) };
    ctx.getComputedStyle = () => ({ direction: "ltr" });
    ctx.uiMenuPlace(menu, anchor);
    // no room under the button above the tab bar: it opens above it
    assert.equal(menu.style.top, 700 - 120 - 4 + "px");
    assert.equal(menu.style.left, 340 + 34 - 190 + "px");
});

// A picker as uiPicker builds it, open, with a button that can move
function openPicker(ctx, host, buttonRect, panel) {
    const picker = {
        host, panel,
        button: { getBoundingClientRect: () => buttonRect.value, setAttribute() {}, removeAttribute() {}, isConnected: true, focus() {} },
    };
    panel.remove = () => {};
    panel.contains = () => false;
    ctx.uiPickerState.open = picker;
    return picker;
}

test("F1: an open picker is placed again when the page scrolls", () => {
    const { ctx, host } = page({ mtop: rect(0, 0, 700, 56), tabbar: null });
    ctx.innerWidth = 700;
    ctx.innerHeight = 390;
    const button = { value: rect(300, 20, 130, 30) };
    const panel = panelAt(20, 260, 200);
    const picker = openPicker(ctx, host, button, panel);
    ctx.uiPickerPlace(picker);
    assert.equal(panel.classList.has("is-up"), true);
    assert.equal(panel.style.maxHeight, 300 - 64 - 6 + "px");
    // The page scrolls 70px: the button moves up and the room above shrinks
    button.value = rect(230, 20, 130, 30);
    ctx.uiPickerOnScroll({ target: ctx.document });
    assert.equal(panel.style.maxHeight, 230 - 64 - 6 + "px", "placed again");
});

test("F1: the picker closes once its button leaves the room; other scrolls leave it alone", () => {
    const { ctx, host } = page({ mtop: rect(0, 0, 700, 56), tabbar: null });
    ctx.innerHeight = 390;
    const button = { value: rect(300, 20, 130, 30) };
    const panel = panelAt(20, 260, 200);
    openPicker(ctx, host, button, panel);
    // A box that does not hold the picker scrolls: nothing happens
    const other = new ctx.Node();
    other.contains = () => false;
    button.value = rect(10, 20, 130, 30);
    ctx.uiPickerOnScroll({ target: other });
    assert.ok(ctx.uiPickerState.open, "still open");
    // The page scrolls the button under the top bar: it closes
    ctx.uiPickerOnScroll({ target: ctx.document });
    assert.equal(ctx.uiPickerState.open, null);
});

test("F2: a menu that does not fit below opens above when there is more room there, never over its button", () => {
    const { ctx } = page({ mtop: rect(0, 0, 390, 56), tabbar: rect(536, 0, 390, 64) });
    ctx.innerHeight = 600;
    ctx.getComputedStyle = () => ({ direction: "ltr" });
    const menu = { style: {}, offsetWidth: 190, offsetHeight: 168 };
    ctx.uiMenuPlace(menu, { getBoundingClientRect: () => rect(350, 300, 34, 34) });
    assert.equal(menu.style.top, 350 - 4 - 168 + "px");
    assert.ok(parseInt(menu.style.top, 10) + 168 <= 350, "ends above the button");
});

test("F2: with too little room on both sides it takes the larger one, cut to it", () => {
    const { ctx } = page({ mtop: null, tabbar: null });
    ctx.innerHeight = 300;
    ctx.getComputedStyle = () => ({ direction: "ltr" });
    const menu = { style: {}, offsetWidth: 190, offsetHeight: 400 };
    ctx.uiMenuPlace(menu, { getBoundingClientRect: () => rect(100, 300, 34, 34) });
    // below: 292 - 138 = 154; above: 96 - 8 = 88 -> below, cut to 154 and scrolling
    assert.equal(menu.style.top, "138px");
    assert.equal(menu.style.maxHeight, "154px");
});
