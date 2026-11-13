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
