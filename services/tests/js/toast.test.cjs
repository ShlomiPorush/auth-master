// FB-2 in js/ui.js: one toast at a time, four seconds unless its duration says
// otherwise, waiting while the pointer or the focus is on it, errors as alerts,
// and one optional action that closes the toast, or holds it while it runs.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadScripts } = require("./_load.cjs");

// Just enough of a page for showToast: elements in a tree, attributes, classes,
// listeners, focus and the pointer resting on an element
function fakePage() {
    const doc = { activeElement: null };
    class El {
        constructor(tag) {
            this.tagName = tag.toUpperCase();
            this.children = [];
            this.parentNode = null;
            this.attrs = {};
            this.listeners = {};
            this.classes = new Set();
            this.text = "";
            this.hovered = false;
            this.style = {};
        }
        set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
        get className() { return [...this.classes].join(" "); }
        get classList() {
            const s = this.classes;
            return { add: (...c) => c.forEach((x) => s.add(x)), remove: (...c) => c.forEach((x) => s.delete(x)), contains: (c) => s.has(c) };
        }
        set id(v) { this.attrs.id = String(v); }
        get id() { return this.attrs.id || ""; }
        set textContent(v) { this.text = String(v); this.children = []; }
        get textContent() { return this.text + this.children.map((c) => c.textContent).join(""); }
        set innerHTML(v) { this.html = v; }
        setAttribute(k, v) { this.attrs[k] = String(v); }
        getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
        removeAttribute(k) { delete this.attrs[k]; }
        appendChild(c) { c.remove(); c.parentNode = this; this.children.push(c); return c; }
        remove() {
            if (!this.parentNode) return;
            this.parentNode.children = this.parentNode.children.filter((x) => x !== this);
            this.parentNode = null;
        }
        contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
        get isConnected() { return doc.body.contains(this); }
        addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
        fire(type) { for (let x = this; x; x = x.parentNode) (x.listeners[type] || []).forEach((fn) => fn({ type, target: this })); }
        matches(sel) { return sel === ":hover" ? this.hovered : false; }
        focus() { doc.activeElement = this; }
        click() { this.fire("click"); }
        all() { return [this, ...this.children.flatMap((c) => c.all())]; }
    }
    doc.body = new El("body");
    doc.createElement = (tag) => new El(tag);
    doc.getElementById = (id) => doc.body.all().find((e) => e.id === id) || null;
    doc.addEventListener = () => {};
    return doc;
}

function load(t) {
    t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 0 });
    const document = fakePage();
    const ctx = loadScripts(["js/text.js", "js/ui.js"], { document });
    const toasts = () => document.body.children.filter((c) => c.classes.has("ui-toast"));
    const buttons = (toast) => toast.children.filter((c) => c.tagName === "BUTTON");
    return { ctx, document, toasts, buttons };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("one toast at a time: a new one replaces the current one", (t) => {
    const { ctx, toasts } = load(t);
    ctx.showToast("first", "success");
    ctx.showToast("second", "info");
    assert.equal(toasts().length, 1);
    assert.match(toasts()[0].textContent, /second/);
});

test("it fades after four seconds and is gone 300ms later", (t) => {
    const { ctx, toasts } = load(t);
    const toast = ctx.showToast("saved", "success");
    t.mock.timers.tick(3999);
    assert.ok(!toast.classes.has("is-fading"));
    t.mock.timers.tick(1);
    assert.ok(toast.classes.has("is-fading"));
    t.mock.timers.tick(300);
    assert.equal(toasts().length, 0);
});

test("errors are alerts, everything else a status", (t) => {
    const { ctx } = load(t);
    assert.equal(ctx.showToast("x", "error").getAttribute("role"), "alert");
    assert.equal(ctx.showToast("x", "success").getAttribute("role"), "status");
    assert.equal(ctx.showToast("x", "unknown-type").getAttribute("role"), "status");
});

test("the pointer on the toast holds it; leaving gives back the rest, at least 1.5 seconds", (t) => {
    const { ctx } = load(t);
    const toast = ctx.showToast("held", "info");
    t.mock.timers.tick(3000);
    toast.hovered = true;
    toast.fire("mouseenter");
    t.mock.timers.tick(60000);
    assert.ok(toast.isConnected && !toast.classes.has("is-fading"));
    toast.hovered = false;
    toast.fire("mouseleave");
    t.mock.timers.tick(0);
    t.mock.timers.tick(1499);
    assert.ok(!toast.classes.has("is-fading"));
    t.mock.timers.tick(1);
    assert.ok(toast.classes.has("is-fading"));
    // Caught while fading: it comes back for 1.5 seconds
    toast.fire("mouseenter");
    assert.ok(!toast.classes.has("is-fading"));
    t.mock.timers.tick(1000);
    toast.fire("mouseleave");
    t.mock.timers.tick(0);
    t.mock.timers.tick(1500);
    assert.ok(toast.classes.has("is-fading"));
});

test("a toast without an action has only its close button; a label without run adds none", (t) => {
    const { ctx, buttons } = load(t);
    assert.equal(buttons(ctx.showToast("x", "info")).length, 1);
    assert.equal(buttons(ctx.showToast("x", "info", { action: { label: "Retry" } })).length, 1);
    assert.equal(buttons(ctx.showToast("x", "info", { action: { run: () => {} } })).length, 1);
});

test("an action is one button with its label; a click runs it and closes the toast", (t) => {
    const { ctx, toasts, buttons } = load(t);
    let runs = 0;
    const toast = ctx.showToast("Could not load", "error", { action: { label: "Retry", ariaLabel: "Retry loading", run: () => { runs += 1; } } });
    const [action, x] = buttons(toast);
    assert.equal(action.textContent, "Retry");
    assert.ok(action.classes.has("ui-toast-action"));
    assert.equal(action.getAttribute("aria-label"), "Retry loading");
    assert.equal(x.getAttribute("aria-label"), ctx.t("common.close"));
    action.click();
    assert.equal(runs, 1);
    assert.equal(toasts().length, 0);
});

test("an action that returns a promise holds the toast until it settles, and ignores more clicks", async (t) => {
    const { ctx, toasts, buttons } = load(t);
    let runs = 0;
    let settle;
    const toast = ctx.showToast("Could not load", "error", {
        action: { label: "Retry", run: () => { runs += 1; return new Promise((resolve) => { settle = resolve; }); } },
    });
    const action = buttons(toast)[0];
    action.click();
    assert.equal(action.getAttribute("aria-disabled"), "true");
    t.mock.timers.tick(60000);
    assert.ok(toast.isConnected && !toast.classes.has("is-fading"), "it stays while the action runs");
    action.click();
    assert.equal(runs, 1, "a second click does nothing");
    settle();
    await flush();
    assert.equal(toasts().length, 0);
});

test("the message that follows a running action takes the toast's place", async (t) => {
    const { ctx, toasts, buttons } = load(t);
    let fail;
    const first = ctx.showToast("Could not load", "error", {
        action: { label: "Retry", run: () => new Promise((resolve, reject) => { fail = reject; }) },
    });
    buttons(first)[0].click();
    fail(new Error("down"));
    const next = ctx.showToast("Still down", "error");
    await flush();
    assert.deepEqual(toasts(), [next]);
});

test("the keyboard in a replaced toast moves to the new one: its action, else its close button", (t) => {
    const { ctx, document, buttons } = load(t);
    const first = ctx.showToast("a", "info");
    document.activeElement = buttons(first)[0];
    const second = ctx.showToast("b", "error", { action: { label: "Retry", run: () => {} } });
    assert.equal(document.activeElement, buttons(second)[0]);
    const third = ctx.showToast("c", "info");
    assert.equal(document.activeElement, buttons(third)[0]);
    assert.equal(buttons(third)[0].getAttribute("aria-label"), ctx.t("common.close"));
});

test("a duration of Infinity keeps the toast until it is closed", (t) => {
    const { ctx, toasts, buttons } = load(t);
    const toast = ctx.showToast("x", "info", { duration: Infinity });
    t.mock.timers.tick(10 * 60000);
    assert.ok(toast.isConnected && !toast.classes.has("is-fading"));
    buttons(toast)[0].click();
    assert.equal(toasts().length, 0);
});

test("the message is text, never markup", (t) => {
    const { ctx } = load(t);
    const toast = ctx.showToast('<img src=x onerror="alert(1)">', "error");
    const p = toast.children.find((c) => c.tagName === "P");
    assert.equal(p.textContent, '<img src=x onerror="alert(1)">');
    assert.equal(p.html, undefined);
});
