// The text catalog (js/text.js): t, tn, tHtml, tRaw and uiApplyText; and no
// catalog value or server value ever becomes markup, even a hostile one.
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

function text(extra) {
    return loadScripts(["js/text.js"], extra);
}

test("t fills placeholders with plain text and keeps unknown ones", () => {
    const ctx = text();
    ctx.UI_TEXT["x.a"] = "Hello {name}, {missing}";
    assert.equal(ctx.t("x.a", { name: "<b>Ann</b>" }), "Hello <b>Ann</b>, {missing}");
    assert.equal(ctx.t("x.a"), "Hello {name}, {missing}");
});

test("a missing key gives an empty string and an error naming it", () => {
    const errors = [];
    const ctx = text({ console: { ...console, error: (m) => errors.push(m) } });
    assert.equal(ctx.t("no.such.key"), "");
    assert.equal(ctx.tn("no.such.key", 2), "");
    assert.equal(ctx.tHtml("no.such.key"), "");
    assert.deepEqual(errors.map((m) => m.includes("no.such.key")), [true, true, true]);
});

test("tn picks the English one or other form and formats {n} like uiNum", () => {
    const ctx = loadScripts(["js/text.js", "js/common.js", "js/ui.js"]);
    ctx.UI_TEXT["x.n"] = { one: "{n} entry", other: "{n} entries" };
    assert.equal(ctx.tn("x.n", 1), "1 entry");
    assert.equal(ctx.tn("x.n", 0), "0 entries");
    assert.equal(ctx.tn("x.n", 2), "2 entries");
    assert.equal(ctx.tn("x.n", 1920), ctx.uiNum(1920) + " entries");
    assert.equal(ctx.tn("x.n", "1"), "1 entry");
    assert.equal(ctx.uiCountLabel(3, "x.n"), "3 entries");
});

test("tHtml escapes the whole value, escapes variables, and passes only tRaw markup", () => {
    const ctx = text();
    ctx.UI_TEXT["x.h"] = "<i>{a}</i> & {b} \"q\" 'q'";
    const out = ctx.tHtml("x.h", { a: "<script>1</script>", b: ctx.tRaw("<code>ok</code>") });
    assert.equal(out, "&lt;i&gt;&lt;script&gt;1&lt;/script&gt;&lt;/i&gt; &amp; <code>ok</code> &quot;q&quot; &#39;q&#39;");
});

test("uiApplyText sets data-t text, data-t-attr attributes and data-t-tpl slots", () => {
    const ctx = text();
    ctx.UI_TEXT["x.t"] = "A & B";
    ctx.UI_TEXT["x.l"] = "Label";
    ctx.UI_TEXT["x.tpl"] = "Signed in as {user}.";
    const attrs = (map) => ({ getAttribute: (k) => (k in map ? map[k] : null), setAttribute: (k, v) => { map[k] = v; } });
    const plain = { ...attrs({ "data-t": "x.t" }), textContent: "" };
    const labelled = attrs({ "data-t-attr": "aria-label:x.l; title:x.l" });
    const slot = { ...attrs({ "data-t-slot": "user" }), kind: "slot" };
    const nodes = [slot];
    const tpl = {
        ...attrs({ "data-t-tpl": "x.tpl" }),
        children: [slot],
        get firstChild() { return nodes[0] || null; },
        removeChild: (n) => nodes.splice(nodes.indexOf(n), 1),
        appendChild: (n) => nodes.push(n),
    };
    const root = {
        querySelectorAll: (sel) => (sel === "[data-t]" ? [plain] : sel === "[data-t-attr]" ? [labelled] : sel === "[data-t-tpl]" ? [tpl] : []),
    };
    ctx.document = { createTextNode: (s) => ({ text: s }) };
    ctx.uiApplyText(root);
    assert.equal(plain.textContent, "A & B");
    assert.equal(labelled.getAttribute("aria-label"), "Label");
    assert.equal(labelled.getAttribute("title"), "Label");
    assert.deepEqual(nodes.map((n) => n.text || n.kind), ["Signed in as ", "slot", "."]);
});

// --- hostile values -----------------------------------------------------------

const EVIL = "<img src=x onerror=alert(1)>\"'&amp;";

function hostile() {
    const ctx = loadScripts(["js/text.js", "js/common.js", "js/ui.js", "js/dashboard.js"], { document: PAGE, location: { origin: "https://a.example" } });
    for (const [k, v] of Object.entries(ctx.UI_TEXT)) {
        ctx.UI_TEXT[k] = typeof v === "string" ? EVIL + v : { one: EVIL + v.one, other: EVIL + v.other };
    }
    ctx.showToast = () => {};
    ctx.uiSetAppZone("UTC");
    return ctx;
}

function clean(html, what) {
    assert.ok(html.length > 0, what + " drew nothing");
    assert.ok(!/<img/i.test(html), what + " put markup from a value into the page: " + html.slice(0, 300));
    assert.ok(!/<script/i.test(html), what);
}

const NOW = Date.UTC(2026, 10, 16, 12);
const iso = (ms) => new Date(ms).toISOString();

test("hostile catalog and server values never become markup: tokens, zones, keys", () => {
    const ctx = hostile();
    const tok = { id: "1", name: EVIL, description: EVIL, isActive: true, canReveal: true, expiresAt: iso(NOW + 86400000), lastUsedAt: iso(NOW - 60000),
        grants: [{ area: EVIL, level: "read,write" }, { area: "b", level: "read,write,delete" }] };
    clean(ctx.dashTokRow(tok, NOW), "dashTokRow");
    clean(ctx.dashGrantTags([]), "dashGrantTags");
    clean(ctx.dashGrantTags(tok.grants), "dashGrantTags");
    ctx.dash.areas = [EVIL];
    clean(ctx.dashPermRows({ [EVIL]: { flags: ["read"], grants: [{ area: EVIL, level: "read" }, { area: EVIL + "X", level: "write" }] } }), "dashPermRows");
    clean(ctx.dashSortHead({ key: "", dir: "" }, { key: "name", label: "tokens.name", kind: "text" }), "dashSortHead");
    clean(ctx.dashMenuButton(EVIL), "dashMenuButton");
    clean(ctx.dashZoneCaseNotice([[{ name: EVIL }, { name: EVIL + "x" }]]), "dashZoneCaseNotice");
    ctx.dashKeys.master = { active: true, envName: EVIL };
    clean(ctx.dashKeyMasterRow(), "dashKeyMasterRow");
    clean(ctx.dashEmpty(ctx.t("tokens.no-tokens-yet-click-new-token"), ctx.esc(ctx.t("tokens.a-token-lets-a-service-call"))), "dashEmpty");
    clean(ctx.dashFailed(ctx.t("tokens.could-not-load-the-tokens"), EVIL, "data-x"), "dashFailed");
});

test("hostile catalog and server values never become markup: logs", () => {
    const ctx = hostile();
    const activity = ctx.dashLogs.activity;
    const access = ctx.dashLogs.access;
    const log = (action, details, extra) => ({ id: "1", createdAt: iso(NOW), actor: EVIL, action, entityType: EVIL, entityName: EVIL, ipAddress: "1.2.3.4", details, ...extra });
    const rows = [
        log("token_create", { grants: [{ area: EVIL, level: "read" }], expiresAt: iso(NOW) }),
        log("token_create", { grants: [] }),
        log("token_patch", { updates: { name: EVIL, description: EVIL, isActive: false, grants: [{ area: EVIL, level: "all" }], expiresAt: null, scopes: [EVIL] } }),
        log("token_patch", { updates: {} }),
        log("zone_create", { description: EVIL }),
        log("api_key_create", { scopes: EVIL }),
        log("settings.update", { version: 2, from: { timeZone: EVIL, activityLogRetentionDays: 30, cookieSecure: false }, to: { timeZone: null, activityLogRetentionDays: 7, cookieSecure: true } }, { entityName: "general" }),
        log("settings.update", { from: { origin: EVIL }, to: { origin: EVIL + "2" } }, { entityName: "passkeys" }),
        log("settings.update", { from: { origin: null }, to: { origin: EVIL } }, { entityName: "passkeys" }),
        log("settings.update", { from: { origin: EVIL }, to: { origin: null } }, { entityName: "passkeys" }),
        log("login", { method: "passkey", passkey: EVIL }),
        log("token_reveal", { method: "passkey" }),
        log("api_key_reveal", { method: EVIL }),
        log("passkey_added", { rpId: EVIL }),
        log("passkey_renamed", { from: EVIL }),
        log("passkey_failed", { where: "login", reason: "invalid" }),
        log("passkey_failed", { where: EVIL, reason: EVIL }),
    ];
    for (const r of rows) clean(ctx.dashLogRowHtml(activity, r), "activity " + r.action);
    clean(ctx.dashLogRowHtml(access, { id: "2", createdAt: iso(NOW), tokenName: EVIL, area: EVIL, level: EVIL, result: false, ipAddress: EVIL }), "access row");
    clean(ctx.dashLogRowHtml(access, { id: "3", createdAt: iso(NOW), tokenName: EVIL, area: EVIL, level: "read", result: true, ipAddress: "::1" }), "access row");
    ctx.dashLimit.failed = EVIL;
    clean(ctx.dashLimitHtml(), "dashLimitHtml without data");
    ctx.dashLimit.data = { windowMs: 60000, limit: 120, since: iso(NOW), rejectedTotal: 3, lastRejectedAt: iso(NOW), untrackedRejected: 2, maxTrackedAddresses: 100,
        topAddresses: [{ address: EVIL, rejected: 3 }] };
    ctx.dashLimit.at = NOW;
    clean(ctx.dashLimitHtml(), "dashLimitHtml");
    ctx.dashLimit.failed = "";
    ctx.dashLimit.data = { windowMs: 30000, limit: 1, since: iso(NOW), rejectedTotal: 0 };
    clean(ctx.dashLimitHtml(), "dashLimitHtml with no rejections");
});

test("hostile catalog and server values never become markup: usage", () => {
    const ctx = hostile();
    const series = [0, 1, 2].map((i) => ({ start: iso(NOW + i * 86400000), end: iso(NOW + (i + 1) * 86400000), allowed: i, denied: 1, coverage: ["none", "partial", "full"][i] }));
    const totals = { calls: 3, denied: 1, tokensUsed: 1, tokensTotal: 2, unknownCalls: 1, ipCount: 2, lastCallAt: iso(NOW), grantsChecks: 2 };
    const base = { range: "7d", retentionDays: 3, dataFrom: iso(NOW), dataFromReason: "retention", partial: true, timeZone: "Not/AZone", series, totals, areasIncomplete: true };
    clean(ctx.dashUseChartHtml(base), "dashUseChartHtml");
    clean(ctx.dashUseNotices(base), "dashUseNotices");
    clean(ctx.dashUseNotices({ ...base, dataFromReason: "purge", timeZone: "Asia/Jerusalem" }), "dashUseNotices");
    ctx.dashUse.failed = EVIL;
    clean(ctx.dashUseOverviewHtml({ ...base, tokens: [{ tokenId: "1", name: EVIL, status: "deleted", calls: 1, denied: 1, ipCount: 1, lastCallAt: iso(NOW), grantsChecks: 1, topAreas: [{ area: EVIL, calls: 1, truncated: true }] },
        { tokenId: null, name: EVIL, status: "unknown", calls: 1, denied: 0, ipCount: 1, lastCallAt: null, topAreas: [] }] }), "dashUseOverviewHtml");
    clean(ctx.dashUseOverviewHtml({ ...base, tokens: [] }), "dashUseOverviewHtml empty");
    clean(ctx.dashUseTokenHtml({ ...base, token: { id: "1", name: EVIL, status: "active" }, areasCount: 5,
        areas: [{ area: EVIL, calls: 2, allowed: 1, denied: 1, truncated: true, levels: { [EVIL]: { allowed: 1, denied: 1 } } }], addresses: [{ address: EVIL, calls: 2, denied: 1 }], levels: { [EVIL]: { allowed: 1, denied: 1 } } }), "dashUseTokenHtml");
    clean(ctx.dashUseNotFoundHtml(), "dashUseNotFoundHtml");
    clean(ctx.dashUseNote(), "dashUseNote");
});

test("hostile catalog and server values never become markup: settings, database, passkeys, shared builders", () => {
    const ctx = hostile();
    clean(ctx.dashSetLocked({ envName: EVIL }, EVIL), "dashSetLocked");
    clean(ctx.dashSetSavedLine({ version: 2, savedBy: EVIL, savedFrom: EVIL, savedAt: iso(NOW) }, ""), "dashSetSavedLine");
    clean(ctx.dashSettingsConflictText({ fields: { maxReq: { value: 3 }, windowMs: { value: 60000 }, exempt: { value: [EVIL] } } }), "dashSettingsConflictText");
    clean(ctx.dashSettingsConflictText({ fields: { maxReq: { value: 1 }, windowMs: { value: 1000 }, exempt: { value: [] } } }), "dashSettingsConflictText");
    ctx.dashGen.askPurge = { detail: EVIL, estimate: { activityLogs: 3, accessLogs: null }, fields: {} };
    ctx.dashGen.askCookie = { detail: EVIL };
    ctx.dashGenLowered = () => true;
    clean(ctx.dashGenAsksHtml(), "dashGenAsksHtml");
    clean(ctx.dashDbSummary({ backend: "postgres", user: EVIL, host: EVIL, port: 5432, database: EVIL, sslmode: EVIL, caPem: true }), "dashDbSummary");
    clean(ctx.dashDbSummary({ backend: "sqlite", path: EVIL }), "dashDbSummary");
    for (const blocked of ["insecure", "browser", ""]) {
        if (blocked) clean(ctx.dashPkInfoHtml({ address: null }, blocked, EVIL), "dashPkInfoHtml " + blocked);
        clean(ctx.dashPkInfoHtml({ address: { origin: "" } }, blocked, EVIL), "dashPkInfoHtml no address " + blocked);
        clean(ctx.dashPkInfoHtml({ address: { origin: EVIL, locked: true, envName: EVIL } }, blocked, EVIL), "dashPkInfoHtml locked " + blocked);
        clean(ctx.dashPkInfoHtml({ address: { origin: EVIL } }, blocked, EVIL + "2"), "dashPkInfoHtml other " + blocked);
    }
    clean(ctx.dashPkRowHtml({ id: "1", name: EVIL, usable: false, rpId: EVIL, backedUp: false, createdAt: iso(NOW), lastUsedAt: null }), "dashPkRowHtml");
    clean(ctx.uiDbFieldsHtml("db-", { host: EVIL, port: 1, database: EVIL, user: EVIL, sslmode: "verify-full", caSaved: true }, true), "uiDbFieldsHtml");
    for (const r of [{ ok: false, code: "timeout" }, { ok: true, problems: [EVIL] }, { ok: true, state: "new" }, { ok: true, state: "empty" },
        { ok: true, state: "foreign", others: [EVIL] }, { ok: true, state: "incomplete", others: [EVIL] }, { ok: true, state: "older" }, { ok: true, pending: true },
        { ok: true, identity: "same" }, { ok: true, identity: "different_copy" }, { ok: true }]) {
        for (const where of ["setup", "settings", "recovery"]) clean(ctx.uiDbNoteHtml(ctx.uiDbCheckNote(r, where)), "uiDbCheckNote " + JSON.stringify(r));
    }
    clean(ctx.uiCopyable(EVIL), "uiCopyable");
    clean(ctx.uiTime(iso(NOW)), "uiTime");
});

test("hostile catalog and server values never become markup: sign-in and setup pages", () => {
    const els = {};
    const el = (id) => els[id] || (els[id] = {
        id, innerHTML: "", textContent: "", value: "", classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
        setAttribute() {}, getAttribute: () => null, removeAttribute() {},
    });
    const ctx = loadScripts(["js/text.js", "js/common.js", "js/ui.js", "js/auth-pages.js"], { document: { ...PAGE, getElementById: el }, QRCode: function () {} });
    for (const [k, v] of Object.entries(ctx.UI_TEXT)) {
        ctx.UI_TEXT[k] = typeof v === "string" ? EVIL + v : { one: EVIL + v.one, other: EVIL + v.other };
    }
    ctx.authSetupExpired(EVIL);
    clean(els["setup-mfa-box"].innerHTML, "authSetupExpired");
    ctx.authSetupMfaShow({ secretBase32: EVIL, otpauthUrl: "otpauth://x", issuer: EVIL, issuerLocked: true, issuerEnvName: EVIL });
    clean(els["issuer-locked"].innerHTML, "authSetupMfaShow locked note");
    clean(els.sec.innerHTML, "authSetupMfaShow secret");
});
