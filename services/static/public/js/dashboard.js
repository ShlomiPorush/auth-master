/*
 * The admin dashboard: the shell (navigation, Needs attention, preferences) and
 * its pages: Tokens, Zones, API Keys, Activity Log, Access Logs, Usage, Settings
 * and Account (the admin's passkeys).
 *
 * Builds on js/ui.js (builders, router, refresh scheduler, date helpers) and
 * js/common.js (requests). Everything from the server is escaped with esc() or
 * set as text; handlers are attached here, never written inline.
 */

// The router's pages: every one registers a refresh below (services/tests/test_ui_guards.py)
var PAGES = {
    tokens: { path: "/admin/tokens", title: "shell.tokens" },
    zones: { path: "/admin/zones", title: "shell.zones" },
    apikeys: { path: "/admin/api-keys", title: "shell.api-keys" },
    activity: { path: "/admin/logs/activity", title: "shell.activity-log" },
    access: { path: "/admin/logs/access", title: "shell.access-logs" },
    usage: { path: "/admin/usage", title: "shell.usage" },
    settings: { path: "/admin/settings", title: "shell.settings" },
    account: { path: "/admin/account", title: "shell.account" },
};

var TOKEN_FLAGS = ["read", "write", "delete"];
var TOKEN_STATUS = {
    active: { label: "tokens.active", tone: "ok" },
    disabled: { label: "tokens.disabled", tone: "" },
    expired: { label: "tokens.expired", tone: "warn" },
};
var TOKEN_EXPIRY_SOON_MS = 7 * 24 * 3600 * 1000;
var TOKEN_LOCKED_REASON = "tokens.created-before-stored-tokens-were-encrypted";
var API_KEY_LOCKED_REASON = "apikeys.created-before-stored-keys-were-encrypted";
var API_KEY_SCOPES = [
    { value: "validate", text: "apikeys.validate-tokens" },
    { value: "tokens:read", text: "apikeys.list-tokens-zones" },
    { value: "tokens:write", text: "apikeys.create-edit-delete-tokens" },
    { value: "zones:read", text: "apikeys.list-zones" },
    { value: "zones:write", text: "apikeys.create-zones" },
];
var API_KEY_DEFAULT_SCOPES = ["validate", "tokens:read", "zones:read"];
var LOG_PAGE_SIZE = 30;
var LOG_SINCE_LIMIT = 500;
var LOG_REFRESH_MAX_PAGES = 20;
var SESSION_CHANGED_TEXT = "shell.your-session-changed-reload-the-page";

var dash = {
    csrf: "",
    user: "",
    zoneAsked: "",
    allowedAreas: [],
    zones: [],
    areas: [],
    zonesLoaded: false,
    zonesFailed: "",
    expired: false,
};

// ===========================================================================
// Requests: the dashboard's data layer. A 401 is an expired session; a 403
// CSRF answer means the session changed in another tab.
// ===========================================================================

function api(method, path, body) {
    var opts = { method: method };
    if (method !== "GET") opts.csrf = dash.csrf;
    if (body !== undefined) opts.body = body;
    return adminJson(path, opts).then(function (res) {
        if (res.status === 401) dashSessionExpired();
        else if (res.status === 403 && res.data && res.data.detail === "CSRF") showToast(t(SESSION_CHANGED_TEXT), "error");
        return res;
    });
}

// The server's reason, else the fallback the old UI showed
function apiError(res, fallback) {
    // The server caps request bodies; its 413 reply may be plain text
    if (res && res.status === 413) return t("shell.this-is-too-large-to-save");
    var d = res && res.data;
    if (d && typeof d.detail === "string" && d.detail) return d.detail;
    if (d && Array.isArray(d.detail)) {
        return d.detail.map(function (e) {
            var loc = Array.isArray(e.loc) ? e.loc.filter(function (x) { return x !== "body"; }).join(".") : "";
            return (loc ? loc + ": " : "") + (e.msg || "");
        }).join("; ");
    }
    if (d && typeof d.error === "string" && d.error) return d.error;
    if (res && res.error === "timeout") return t("shell.the-server-did-not-answer-in");
    if (res && res.error === "network") return t("shell.could-not-reach-the-server-check");
    return fallback;
}

// The page the user is on, for coming back after signing in (relative to the base path)
function dashHerePath() {
    var p = window.location.pathname;
    var b = basePath();
    if (b && p.indexOf(b) === 0) p = p.slice(b.length);
    return p + window.location.search;
}

function dashLoginUrl() {
    var here = dashHerePath();
    return basePath() + "/login" + (here && here !== "/admin/tokens" ? "?next=" + encodeURIComponent(here) : "");
}

// The session ended: edits cannot be saved without one, so the page says so and
// goes to sign in without asking about them (also from an open dialog)
function dashSessionExpired() {
    if (dash.expired) return;
    dash.expired = true;
    uiUnsavedRelease();
    showToast(t("shell.your-session-expired-sign-in-again"), "error");
    window.location.replace(dashLoginUrl());
}

// ===========================================================================
// Shared data: zones (and the areas tokens can use) and one token cache that
// the Tokens page and Needs attention share
// ===========================================================================

var dashTokenStore = { rows: null, at: 0, pending: null, pendingMark: -1, failed: "" };

function dashTokensLoad(force) {
    var store = dashTokenStore;
    var mark = uiMutationMark();
    if (store.pending && (!force || store.pendingMark === mark)) return store.pending;
    if (!force && store.rows && Date.now() - store.at < 25000) return Promise.resolve(store.rows);
    store.pendingMark = mark;
    var p = api("GET", "/admin/api/tokens").then(function (res) {
        if (store.pending === p) store.pending = null;
        if (uiStaleSince(mark)) return store.rows;
        if (res.ok && Array.isArray(res.data)) {
            store.rows = res.data;
            store.at = Date.now();
            store.failed = "";
        } else {
            store.failed = apiError(res, t("tokens.failed"));
        }
        // Needs attention counts from the same rows
        if (dashAttn.checked) dashAttnUpdate();
        return store.rows;
    });
    store.pending = p;
    return p;
}

function dashZonesLoad() {
    var mark = uiMutationMark();
    return api("GET", "/admin/api/zones").then(function (res) {
        if (uiStaleSince(mark)) return dash.zones;
        if (!res.ok || !Array.isArray(res.data)) {
            dash.zonesFailed = apiError(res, t("zones.failed"));
            return dash.zones;
        }
        dash.zonesFailed = "";
        dash.zones = res.data;
        dash.zonesLoaded = true;
        if (res.data.length) {
            dash.areas = res.data.map(function (z) { return z.name; });
            return dash.zones;
        }
        // No zones: the areas the server allows, read again from /me
        return api("GET", "/admin/api/me").then(function (me) {
            if (uiStaleSince(mark)) return dash.zones;
            if (me.ok && me.data && Array.isArray(me.data.allowedAreas)) dash.allowedAreas = me.data.allowedAreas;
            dash.areas = dash.allowedAreas && dash.allowedAreas.length ? dash.allowedAreas.slice() : ["default"];
            return dash.zones;
        });
    });
}

// ===========================================================================
// Start
// ===========================================================================

function dashBoot() {
    uiInstallGlobalListeners();
    uiLoadDatePrefs();
    dashBindShell();
    adminJson("/admin/api/setup/status").then(function (st) {
        if (st.ok && st.data && st.data.needsSetup) {
            window.location.replace(basePath() + "/setup");
            return null;
        }
        return adminJson("/admin/api/me").then(function (me) {
            if (me.ok && me.data && !me.data.user) {
                window.location.replace(dashLoginUrl());
                return;
            }
            if (!me.ok || !me.data) {
                dashBootFailed(apiError(me, t("shell.failed")));
                return;
            }
            dash.csrf = me.data.csrf || readCsrf();
            writeCsrf(dash.csrf);
            dash.user = me.data.user.username || "";
            dash.allowedAreas = me.data.allowedAreas || [];
            dash.zoneAsked = me.data.timeZone || "";
            uiSetAppZone(dash.zoneAsked);
            document.querySelectorAll("[data-user]").forEach(function (el) { el.textContent = dash.user; });
            document.getElementById("ui-shell").classList.remove("hidden");
            document.getElementById("boot").classList.add("hidden");
            uiRouterStart(PAGES, dashShowPage);
            uiRefreshStart();
            uiRefreshNow("attention");
        });
    });
}

function dashBootFailed(reason) {
    var box = document.getElementById("boot");
    box.innerHTML = '<div class="ui-failed" role="alert"><b>' + esc(t("shell.could-not-load-the-portal")) + '</b><p class="ui-muted">' + esc(reason) +
        '</p><button type="button" class="ui-btn" id="boot-retry">' + esc(t("shell.try-again")) + '</button></div>';
    document.getElementById("boot-retry").addEventListener("click", function () { window.location.reload(); });
}

// ===========================================================================
// Shell: navigation, theme, preferences, sign out
// ===========================================================================

var DASH_PAGE_IMPL = {};

function dashShowPage(id, info) {
    dashMoreClose();
    document.querySelectorAll(".ui-page[data-page]").forEach(function (el) {
        el.classList.toggle("hidden", el.getAttribute("data-page") !== id);
    });
    document.querySelectorAll("[data-page-link]").forEach(function (a) {
        if (a.getAttribute("data-page-link") === id) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
    });
    var more = document.getElementById("tab-more");
    if (more) {
        if (id === "activity" || id === "access" || id === "usage" || id === "settings" || id === "account") more.setAttribute("aria-current", "page");
        else more.removeAttribute("aria-current");
    }
    var title = document.getElementById("mtop-page");
    if (title) title.textContent = t(PAGES[id].title);
    if (DASH_PAGE_IMPL[id]) DASH_PAGE_IMPL[id](info || {});
}

function dashBindShell() {
    document.querySelectorAll("[data-page-link]").forEach(function (a) {
        var page = PAGES[a.getAttribute("data-page-link")];
        a.setAttribute("href", basePath() + page.path);
        // The rail hides the labels; the name stays for screen readers and as a tooltip
        if (a.classList.contains("ui-nav-item")) {
            a.setAttribute("aria-label", t(page.title));
            a.setAttribute("title", t(page.title));
        }
    });
    document.querySelectorAll("a[data-go]").forEach(function (a) {
        var page = PAGES[a.getAttribute("data-go")];
        if (!page) return;
        // Rebuild the query from encoded pairs so the attribute text never reaches the URL as is
        var pairs = [];
        new URLSearchParams(a.getAttribute("data-query") || "").forEach(function (value, key) {
            pairs.push(encodeURIComponent(key) + "=" + encodeURIComponent(value));
        });
        a.setAttribute("href", basePath() + page.path + (pairs.length ? "?" + pairs.join("&") : ""));
    });
    // Navigation inside the app: one history step; a modified click opens a tab
    document.addEventListener("click", function (event) {
        var a = event.target instanceof Element ? event.target.closest("a[data-page-link], a[data-go]") : null;
        if (!a || event.defaultPrevented) return;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
        event.preventDefault();
        var id = a.getAttribute("data-page-link") || a.getAttribute("data-go");
        uiGo(id, a.getAttribute("data-query") || "");
    });
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) { b.addEventListener("click", dashThemeCycle); });
    document.querySelectorAll("[data-prefs]").forEach(function (b) { b.addEventListener("click", dashPrefsOpen); });
    document.querySelectorAll("[data-logout]").forEach(function (b) { b.addEventListener("click", dashLogout); });
    document.querySelectorAll("[data-bell]").forEach(function (b) { b.addEventListener("click", function () { dashAttnOpen(b); }); });
    var more = document.getElementById("tab-more");
    if (more) more.addEventListener("click", dashMoreOpen);
    dashThemeLabel();
    document.addEventListener("ui-theme", dashThemeLabel);
}

var DASH_THEMES = [["system", "shell.system"], ["light", "shell.light"], ["dark", "shell.dark"]];

function dashThemeLabel() {
    var pref = typeof window.uiThemeGet === "function" ? window.uiThemeGet() : "system";
    var name = t(DASH_THEMES.filter(function (theme) { return theme[0] === pref; })[0][1]);
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
        var label = b.querySelector(".ui-tool-label");
        if (label) label.textContent = t("shell.theme", { name: name });
        var fly = b.querySelector(".ui-fly");
        if (fly) fly.textContent = t("shell.theme", { name: name });
        b.setAttribute("aria-label", t("shell.theme-click", { name: name }));
    });
}

function dashThemeCycle() {
    var pref = window.uiThemeGet();
    var i = DASH_THEMES.map(function (theme) { return theme[0]; }).indexOf(pref);
    window.uiThemeSet(DASH_THEMES[(i + 1) % DASH_THEMES.length][0]);
}

function dashLogout() {
    var go = function () {
        api("POST", "/admin/api/logout").then(function (res) {
            if (!res.ok && res.status !== 401) {
                showToast(t("shell.could-not-sign-out-try-again"), "error");
                return;
            }
            uiUnsavedRelease();
            writeCsrf("");
            window.location.href = basePath() + "/login";
        });
    };
    if (uiUnsavedCount()) uiConfirmLeave().then(function (ok) { if (ok) go(); });
    else go();
}

// The phone's More sheet: every page not on the tab bar and the tools (NL-9)
var dashMore = null;
function dashMoreClose() {
    if (dashMore) dashMore.backdrop.remove();
    dashMore = null;
}
function dashMoreOpen() {
    if (dashMore && dashMore.backdrop.isConnected) { dashMoreClose(); return; }
    var d = null;
    var close = dashMoreClose;
    var link = function (id) {
        return '<a class="ui-nav-item" data-page-link="' + id + '" href="' + esc(basePath() + PAGES[id].path) + '"' +
            (uiPageCurrent === id ? ' aria-current="page"' : "") + '><span class="ui-nav-label">' + esc(t(PAGES[id].title)) + "</span></a>";
    };
    d = uiDialog({
        title: t("shell.more"), size: "sm", backdropClass: "ui-more-layer", close: close, backdropClose: close,
        body: '<nav class="ui-more-list" aria-label="' + esc(t("shell.all-pages")) + '">' + link("tokens") + link("zones") + link("apikeys") + link("activity") + link("access") + link("usage") + link("settings") + link("account") +
            '<button type="button" class="ui-tool" data-more-prefs><span class="ui-tool-label">' + esc(t("shell.preferences")) + '</span></button>' +
            '<button type="button" class="ui-tool" data-more-theme><span class="ui-tool-label"></span></button>' +
            '<p class="ui-side-user">' + tHtml("shell.signed-in-as", { user: tRaw('<bdi dir="auto">' + esc(dash.user) + "</bdi>") }) + "</p>" +
            '<button type="button" class="ui-tool" data-more-logout><span class="ui-tool-label">' + esc(t("shell.log-out")) + '</span></button></nav>',
    });
    dashMore = d;
    var themeBtn = d.body.querySelector("[data-more-theme]");
    var themeText = function () {
        var pref = window.uiThemeGet();
        themeBtn.firstChild.textContent = t("shell.theme", { name: t(DASH_THEMES.filter(function (theme) { return theme[0] === pref; })[0][1]) });
    };
    themeText();
    themeBtn.addEventListener("click", function () { dashThemeCycle(); themeText(); });
    d.body.querySelector("[data-more-prefs]").addEventListener("click", function () { close(); dashPrefsOpen(); });
    d.body.querySelector("[data-more-logout]").addEventListener("click", function () { close(); dashLogout(); });
    // A page picked here: the sheet goes, the page takes over its history entry
    d.body.addEventListener("click", function (event) {
        var a = event.target.closest("a[data-page-link]");
        if (a) close();
    }, true);
}

// Preferences: theme, Date Format and Time Format, kept in this browser (FW-9)
function dashPrefsOpen() {
    var d = null;
    var close = function () { if (d) d.backdrop.remove(); };
    var zoneNote = dash.zoneAsked && dash.zoneAsked !== uiAppZone
        ? t("shell.the-server-s-time-zone", { zone: dash.zoneAsked })
        : t("shell.times-are-shown-in", { zone: uiAppZone });
    var seg = function (name, items, current) {
        return '<div class="ui-seg" role="group" aria-label="' + esc(name) + '">' + items.map(function (it) {
            return '<button type="button" data-value="' + esc(it[0]) + '" aria-pressed="' + (it[0] === current) + '">' + esc(t(it[1])) + "</button>";
        }).join("") + "</div>";
    };
    d = uiDialog({
        title: t("shell.preferences"), size: "sm", close: close, backdropClose: close,
        body: '<div class="ui-form">' +
            '<div class="ui-field"><span class="ui-label">' + esc(t("shell.theme-2")) + '</span>' + seg(t("shell.theme-2"), DASH_THEMES, window.uiThemeGet()) + "</div>" +
            '<div class="ui-field"><label class="ui-label" for="pref-date">' + esc(t("shell.date-format")) + '</label><select id="pref-date" class="ui-select">' +
            UI_DATE_FORMATS.map(function (f) { return '<option value="' + f[0] + '"' + (uiDatePrefs.date === f[0] ? " selected" : "") + ">" + esc(t(f[1])) + "</option>"; }).join("") +
            '</select><p class="ui-hint">' + esc(t("shell.select-your-date-format-default-browser")) + '</p></div>' +
            '<div class="ui-field"><span class="ui-label">' + esc(t("shell.time-format")) + '</span>' + seg(t("shell.time-format"), UI_TIME_FORMATS, uiDatePrefs.time) +
            '<p class="ui-hint">' + esc(t("shell.select-your-time-format-default-24")) + '</p></div>' +
            '<p class="ui-hint" id="pref-example" aria-live="polite"></p>' +
            '<p class="ui-hint">' + esc(zoneNote) + "</p></div>",
        foot: '<button type="button" class="ui-btn" data-cancel>' + esc(t("shell.close")) + '</button>',
    });
    d.foot.querySelector("[data-cancel]").addEventListener("click", close);
    var dateSel = d.body.querySelector("#pref-date");
    uiPicker(dateSel, { label: t("shell.date-format"), field: true });
    var segs = d.body.querySelectorAll(".ui-seg");
    var example = function () { d.body.querySelector("#pref-example").textContent = t("shell.example", { time: formatTime(Date.now(), { seconds: false }) }); };
    var applyDates = function (date, time) {
        uiSaveDatePrefs(date, time);
        example();
        dashRedrawDates();
    };
    segs[0].addEventListener("click", function (event) {
        var b = event.target.closest("button[data-value]");
        if (!b) return;
        window.uiThemeSet(b.getAttribute("data-value"));
        segs[0].querySelectorAll("button").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); });
    });
    segs[1].addEventListener("click", function (event) {
        var b = event.target.closest("button[data-value]");
        if (!b) return;
        segs[1].querySelectorAll("button").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); });
        applyDates(uiDatePrefs.date, b.getAttribute("data-value"));
    });
    dateSel.addEventListener("change", function () { applyDates(dateSel.value, uiDatePrefs.time); });
    example();
}

// Dates are drawn again in the new format
function dashRedrawDates() {
    dashTokDraw(true);
    dashZonesDraw(true);
    dashKeysDraw(true);
    dashLogs.activity.drawn = "";
    dashLogs.access.drawn = "";
    dashLogDraw(dashLogs.activity, true);
    dashLogDraw(dashLogs.access, true);
    dashPk.drawn = "";
    dashPkDraw(true);
}

// ===========================================================================
// Needs attention (NL-19): the bell on every page, its own refresher
// ===========================================================================

var dashAttn = { health: null, healthFailed: false, checked: false, dialog: null, bell: null, drawn: "" };

function dashAttnItems() {
    var items = [];
    var h = dashAttn.health;
    if (h && h.db === false) items.push({ tone: "fail", text: t("attention.database-is-not-responding"), sub: t("attention.tokens-cannot-be-checked-or-changed") });
    if (h && h.redis === false) items.push({ tone: "fail", text: t("attention.redis-is-not-responding"), sub: t("attention.sessions-and-the-token-cache-need") });
    var n = dashExpiringCount();
    if (n > 0) {
        items.push({
            tone: "warn", text: tn("attention.tokens-expire-within-7-days", n),
            sub: t("attention.replace-or-extend-them-before-they"), go: "tokens", query: "status=expiring", action: t("attention.open-tokens"),
        });
    }
    return items;
}

function dashExpiringCount() {
    var rows = dashTokenStore.rows || [];
    var now = Date.now();
    return rows.filter(function (tok) { return dashTokStatus(tok) === "active" && dashTokExpiring(tok, now); }).length;
}

function dashAttnRefresh() {
    var health = adminJson("/health", { timeout: 10000 }).then(function (res) {
        if (res.json && res.data && typeof res.data.db === "boolean" && typeof res.data.redis === "boolean") {
            dashAttn.health = res.data;
            dashAttn.healthFailed = false;
        } else {
            dashAttn.healthFailed = true;
        }
        dashAttn.checked = true;
    });
    return Promise.all([health, dashTokensLoad(false)]).then(dashAttnUpdate);
}

function dashAttnUpdate() {
    var items = dashAttnItems();
    var unknown = dashAttn.healthFailed || !!dashTokenStore.failed;
    var fail = items.some(function (i) { return i.tone === "fail"; });
    var label;
    if (items.length) label = tn("attention.needs-attention", items.length);
    else if (unknown) label = t("attention.needs-attention-some-checks-could-not");
    else if (!dashAttn.checked) label = t("attention.needs-attention-not-checked-yet");
    else label = t("attention.needs-attention-nothing-open");
    document.querySelectorAll("[data-bell]").forEach(function (b) {
        b.setAttribute("aria-label", label);
        b.setAttribute("title", label);
        var badge = b.querySelector(".ui-bell-badge");
        badge.className = "ui-bell-badge" + (items.length ? (fail ? " is-fail" : " is-warn") : unknown ? " is-unknown" : " hidden");
        badge.textContent = items.length ? (items.length > 99 ? "99+" : String(items.length)) : "";
    });
    // The status card: what the last answer said, kept when a check fails
    var h = dashAttn.health;
    var word = function (v) {
        if (!h) return dashAttn.healthFailed ? [t("attention.unknown"), ""] : [t("attention.not-checked-yet"), ""];
        return v ? [t("attention.connected"), "ok"] : [t("attention.not-responding"), "fail"];
    };
    [["db", h ? h.db : null], ["redis", h ? h.redis : null]].forEach(function (pair) {
        var w = word(pair[1]);
        document.querySelectorAll('[data-status="' + pair[0] + '"]').forEach(function (row) {
            row.querySelector(".ui-dot").className = "ui-dot" + (w[1] ? " ui-dot-" + w[1] : "");
            row.querySelector("b").textContent = w[0];
            row.setAttribute("title", (pair[0] === "db" ? t("attention.database") : t("attention.redis")) + ": " + w[0]);
        });
    });
    if (dashAttn.dialog) dashAttnDraw();
}

function dashAttnContent() {
    var items = dashAttnItems();
    var html = '<div class="ui-attn-list" role="list">' + items.map(function (it, i) {
        return '<div class="ui-alert is-' + it.tone + '" role="listitem"><span class="ui-alert-bar" aria-hidden="true"></span>' +
            "<div><p>" + esc(it.text) + "</p><small>" + esc(it.sub) + "</small></div>" +
            (it.go ? '<button type="button" class="ui-btn ui-btn-sm" data-attn-go="' + i + '">' + esc(it.action) + "</button>" : "<span></span>") + "</div>";
    }).join("") + "</div>";
    var note = "";
    if (dashAttn.healthFailed || dashTokenStore.failed) {
        note = '<p class="ui-attn-note" role="status">' + esc(t("attention.could-not-check-the-service")) + ' <button type="button" class="ui-btn-link" data-attn-retry>' + esc(t("attention.retry")) + '</button></p>';
    } else if (!dashAttn.checked) {
        note = '<p class="ui-attn-note" role="status">' + esc(t("attention.not-checked-yet")) + '</p>';
    } else if (!items.length) {
        note = '<p class="ui-attn-note"><b>' + esc(t("attention.nothing-needs-attention-right-now")) + '</b></p>';
    }
    return html + note;
}

function dashAttnDraw() {
    var d = dashAttn.dialog;
    if (!d) return;
    var html = dashAttnContent();
    if (html === dashAttn.drawn || uiTextHeld(d.body)) return;
    dashAttn.drawn = html;
    uiKeepFocus(d.body, function () { d.body.innerHTML = html; });
}

function dashAttnOpen(bell) {
    if (dashAttn.dialog && dashAttn.dialog.backdrop.isConnected) return;
    var close = function () {
        if (!dashAttn.dialog) return;
        dashAttn.dialog.backdrop.remove();
        dashAttn.dialog = null;
        dashAttn.drawn = "";
        document.querySelectorAll("[data-bell]").forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
    };
    var d = uiDialog({ title: t("attention.needs-attention-2"), size: "sm", backdropClass: "ui-attn-layer", close: close, backdropClose: close });
    dashAttn.dialog = d;
    dashAttn.bell = bell;
    bell.setAttribute("aria-expanded", "true");
    // Under the bell on a wide screen, its start edge in line with the bell's
    var r = bell.getBoundingClientRect();
    var rtl = getComputedStyle(bell).direction === "rtl";
    var start = rtl ? window.innerWidth - r.right : r.left;
    d.backdrop.style.setProperty("--ui-attn-top", Math.round(r.bottom + 8) + "px");
    d.backdrop.style.setProperty("--ui-attn-start", Math.max(16, Math.round(start)) + "px");
    dashAttnDraw();
    d.body.addEventListener("click", function (event) {
        var go = event.target.closest("[data-attn-go]");
        if (go) {
            var it = dashAttnItems()[Number(go.getAttribute("data-attn-go"))];
            close();
            if (it) uiGo(it.go, it.query);
            return;
        }
        if (event.target.closest("[data-attn-retry]")) {
            dashTokenStore.at = 0;
            uiRefreshNow("attention").then(dashAttnDraw);
        }
    });
    // It closes when the width crosses 760px, since its bell changes place
    var mq = window.matchMedia("(max-width: 760px)");
    var onCross = function () { close(); mq.removeEventListener("change", onCross); };
    mq.addEventListener("change", onCross);
}

registerBackgroundRefresh("attention", 30000, dashAttnRefresh);

// ===========================================================================
// Shared pieces of the list pages
// ===========================================================================

function dashLoading(text) {
    return '<div class="ui-loading" role="status"><span class="ui-spin" aria-hidden="true"></span><p>' + esc(text) + "</p></div>";
}

function dashFailed(title, reason, retryAttr) {
    return '<div class="ui-failed" role="alert"><b>' + esc(title) + '</b><p class="ui-muted">' + esc(reason) + "</p>" +
        '<button type="button" class="ui-btn ui-btn-sm" ' + retryAttr + ">" + esc(t("list.try-again")) + "</button></div>";
}

function dashEmpty(title, line) {
    return '<div class="ui-empty"><b>' + esc(title) + "</b>" + (line ? "<p>" + line + "</p>" : "") + "</div>";
}

function dashMenuButton(name) {
    return '<button type="button" class="ui-icon-btn" data-row-menu aria-haspopup="menu" aria-expanded="false" aria-label="' + esc(t("list.actions-for", { name: name })) +
        '" title="' + esc(t("list.actions")) + '">' + UI_DOTS_ICON + "</button>";
}

function dashTag(text, tone) {
    return '<span class="ui-tag' + (tone ? " ui-tag-" + tone : "") + '">' + esc(text) + "</span>";
}

// LD-12: after a background read failed, a line under the list's count says so,
// with the server's reason and how long ago the rows shown were loaded (the exact
// time on hover). One shared timer keeps every such line current.
function dashFreshText(at, reason, now) {
    return t("list.fresh-failed", { reason: reason, ago: formatAgo(at, now) });
}

function dashFreshFill(el) {
    var at = Number(el.getAttribute("data-fresh-at"));
    var text = el.querySelector("[data-fresh-text]");
    if (!text || !at) return;
    text.textContent = dashFreshText(at, el.getAttribute("data-fresh-reason") || "");
    text.setAttribute("title", formatTime(at));
}

var dashFreshTimer = null;

// reason "" (or no time of a good read) hides the line
function dashFreshSet(id, at, reason) {
    var el = document.getElementById(id);
    if (!el) return;
    var on = !!(reason && at);
    el.classList.toggle("hidden", !on);
    if (!on) {
        el.removeAttribute("data-fresh-at");
        return;
    }
    el.setAttribute("data-fresh-at", String(at));
    el.setAttribute("data-fresh-reason", reason);
    dashFreshFill(el);
    if (!dashFreshTimer) {
        dashFreshTimer = setInterval(function () {
            document.querySelectorAll(".ui-fresh[data-fresh-at]").forEach(dashFreshFill);
        }, 30000);
    }
}

// Its Try again reads the list again
function dashFreshBind(id, retry) {
    var el = document.getElementById(id);
    if (el) el.addEventListener("click", function (event) { if (event.target.closest("[data-fresh-retry]")) retry(); });
}

// A header label that sorts (TB-1): the label is a button that says its order in
// hidden words; the cell's data-sort-state draws the arrow. def: the list's own
// column and order, for a list that has one.
function dashSortHead(state, col, def) {
    var s = uiSortAria(state, col.key, def);
    var words = s === "ascending" ? t("sort.sorted-ascending") : s === "descending" ? t("sort.sorted-descending") : "";
    return '<div class="ui-td' + (col.kind === "number" ? " ui-num" : "") + '" role="columnheader" data-sort-state="' + s + '"><button type="button" class="ui-sort" data-sort="' +
        esc(col.key) + '">' + esc(t(col.label)) + (words ? '<span class="ui-sr">' + esc(words) + "</span>" : "") + "</button></div>";
}

// Wire a list's header buttons, Sort button and rows (open on click, Enter, Space)
function dashBindList(cfg) {
    var head = document.getElementById(cfg.head);
    var rows = document.getElementById(cfg.rows);
    var sortBtn = document.getElementById(cfg.sortButton);
    head.addEventListener("click", function (event) {
        var b = event.target.closest("[data-sort]");
        if (!b) return;
        var col = cfg.cols.filter(function (c) { return c.key === b.getAttribute("data-sort"); })[0];
        uiSortToggle(cfg.state, col, cfg.def);
        cfg.redraw();
        var again = head.querySelector('[data-sort="' + col.key + '"]');
        if (again) again.focus({ preventScroll: true });
    });
    if (sortBtn) {
        sortBtn.addEventListener("click", function () {
            uiSortMenu(sortBtn, cfg.name, cfg.cols, cfg.state, cfg.redraw, cfg.def);
        });
    }
    rows.addEventListener("click", function (event) {
        var row = event.target.closest("[data-key]");
        if (!row) return;
        var menuBtn = event.target.closest("[data-row-menu]");
        if (menuBtn) {
            cfg.menu(row.getAttribute("data-key"), menuBtn);
            return;
        }
        if (event.target.closest("button, a, input, select, label")) return;
        if (uiRowHeld(event, row)) return;
        cfg.open(row.getAttribute("data-key"));
    });
    rows.addEventListener("keydown", function (event) {
        var row = event.target;
        if (!(row instanceof Element) || !row.hasAttribute("data-key") || (event.key !== "Enter" && event.key !== " ")) return;
        event.preventDefault();
        cfg.open(row.getAttribute("data-key"));
    });
}

// The Sort button's name: the order the list shows (TB-28)
function dashSortLabel(state, cols, def) {
    var cur = uiSortEffective(state, def);
    var col = cols.filter(function (c) { return c.key === cur.key; })[0];
    return col ? t("list.sort", { column: t(col.label), order: uiSortWords(col.kind, cur.dir) }) : t("list.sort-default-order");
}

// The Sort button's dot: an order other than the default (TB-28)
function dashSortButtonSync(id, state, name, cols, def) {
    var b = document.getElementById(id);
    if (!b) return;
    var text = dashSortLabel(state, cols, def);
    b.classList.toggle("is-set", !!state.key);
    b.setAttribute("aria-label", text);
    b.setAttribute("title", text);
    b.setAttribute("data-sort-name", name);
}

// ===========================================================================
// Tokens
// ===========================================================================

var dashTok = {
    filters: { q: "", status: "all", zone: "", level: "" },
    sort: { key: "", dir: "" },
    drawn: "",
    bound: false,
};
var DASH_TOK_COLS = [
    { key: "name", label: "tokens.name", kind: "text" },
    { key: "status", label: "tokens.status", kind: "text" },
    { key: "lastUsed", label: "tokens.last-used", kind: "date" },
];

function dashTokStatus(tok) {
    if (!tok.isActive) return "disabled";
    if (tok.isExpired) return "expired";
    return "active";
}

function dashTokExpiring(tok, now) {
    var ms = uiParseInstant(tok.expiresAt);
    return !isNaN(ms) && ms > now && ms - now <= TOKEN_EXPIRY_SOON_MS;
}

// One filter function for the list and every count (each count ignores its own filter)
function dashTokMatch(tok, f, skip, now) {
    if (skip !== "q" && f.q) {
        var q = f.q.toLowerCase();
        if (String(tok.name || "").toLowerCase().indexOf(q) === -1 && String(tok.description || "").toLowerCase().indexOf(q) === -1) return false;
    }
    if (skip !== "status" && f.status !== "all") {
        var s = dashTokStatus(tok);
        if (f.status === "expiring") { if (s !== "active" || !dashTokExpiring(tok, now)) return false; }
        else if (s !== f.status) return false;
    }
    var grants = tok.grants || [];
    if (skip !== "zone" && f.zone && !grants.some(function (g) { return g.area === f.zone; })) return false;
    if (skip !== "level" && f.level) {
        var hit = grants.some(function (g) {
            var flags = String(g.level || "").split(",");
            if (f.level === "all") return TOKEN_FLAGS.every(function (x) { return flags.indexOf(x) !== -1; });
            return flags.indexOf(f.level) !== -1;
        });
        if (!hit) return false;
    }
    return true;
}

function dashGrantTags(grants) {
    if (!grants || !grants.length) return dashTag(t("tokens.no-grants"), "");
    return '<div class="ui-tags">' + grants.map(function (g) {
        var flags = String(g.level || "").split(",");
        var all = TOKEN_FLAGS.every(function (x) { return flags.indexOf(x) !== -1; });
        return '<span class="ui-tag ui-tag-line"><bdi dir="auto">' + esc(g.area) + "</bdi>: " + esc(all ? t("tokens.level.all") : flags.map(function (f) { return TOKEN_FLAGS.indexOf(f) !== -1 ? t("tokens.level." + f) : f; }).join("+")) + "</span>";
    }).join("") + "</div>";
}

function dashTokRow(tok, now) {
    var s = dashTokStatus(tok);
    var st = TOKEN_STATUS[s];
    var soon = s === "active" && dashTokExpiring(tok, now);
    var exp = tok.expiresAt ? '<span class="ui-td-sub' + (soon ? " ui-text-warn" : "") + '">' + tHtml("tokens.exp", { date: tRaw(uiTime(tok.expiresAt)) }) + "</span>" : "";
    return '<div class="ui-tr' + (s === "disabled" ? " is-off" : "") + '" role="row" tabindex="0" data-key="' + esc(tok.id) + '" aria-label="' + esc(tok.name) + '">' +
        '<div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + uiUserText(tok.name) + "</span>" +
        (tok.description ? '<span class="ui-td-sub">' + uiUserText(tok.description) + "</span>" : "") + "</div>" +
        '<div class="ui-td ui-td-full ui-td-wrap" role="cell">' + dashGrantTags(tok.grants) + "</div>" +
        '<div class="ui-td ui-td-status" role="cell">' + dashTag(t(st.label), st.tone) + (soon ? " " + dashTag(t("tokens.expiring-soon"), "warn") : "") + "</div>" +
        '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("tokens.last-used-2")) + ' </span>' + (tok.lastUsedAt ? uiTime(tok.lastUsedAt) : '<span class="ui-muted">-</span>') + exp + "</div>" +
        '<div class="ui-td ui-td-end" role="cell">' + dashMenuButton(tok.name) + "</div></div>";
}

function dashTokFind(id) {
    return (dashTokenStore.rows || []).filter(function (tok) { return tok.id === id; })[0] || null;
}

function dashTokReadQuery() {
    var q = uiPageQuery();
    var status = q.get("status") || "all";
    dashTok.filters = {
        q: q.get("q") || "",
        status: ["all", "active", "disabled", "expired", "expiring"].indexOf(status) !== -1 ? status : "all",
        zone: q.get("zone") || "",
        level: ["read", "write", "delete", "all"].indexOf(q.get("level") || "") !== -1 ? q.get("level") : "",
    };
}

function dashTokWriteQuery() {
    var f = dashTok.filters;
    var p = new URLSearchParams();
    if (f.q) p.set("q", f.q);
    if (f.status !== "all") p.set("status", f.status);
    if (f.zone) p.set("zone", f.zone);
    if (f.level) p.set("level", f.level);
    uiSetPageQuery(p);
}

function dashTokSyncControls() {
    var f = dashTok.filters;
    var search = document.getElementById("tok-search");
    if (document.activeElement !== search) search.value = f.q;
    var statusSel = document.getElementById("tok-status");
    statusSel.value = f.status;
    uiPickerSync(statusSel);
    var zoneSel = document.getElementById("tok-zone");
    var areas = dash.areas.slice();
    // A zone in the address that is not (or no longer) a zone stays picked (FS-17)
    if (f.zone && areas.indexOf(f.zone) === -1) areas.push(f.zone);
    var want = '<option value="">' + esc(t("tokens.all-zones")) + '</option>' + areas.map(function (a) { return '<option value="' + esc(a) + '">' + esc(a) + "</option>"; }).join("");
    if (zoneSel.getAttribute("data-drawn") !== want) {
        zoneSel.innerHTML = want;
        zoneSel.setAttribute("data-drawn", want);
    }
    zoneSel.value = f.zone;
    uiPickerSync(zoneSel);
    var levelSel = document.getElementById("tok-level");
    levelSel.value = f.level;
    uiPickerSync(levelSel);
}

// Draw the list. A background refresh (quiet) leaves a list the user works in alone
function dashTokDraw(quiet) {
    if (!dashTok.bound) return;
    var rowsEl = document.getElementById("tok-rows");
    dashFreshSet("tok-fresh", dashTokenStore.at, dashTokenStore.rows ? dashTokenStore.failed : "");
    if (quiet && uiAreaHeld(rowsEl)) return;
    var rows = dashTokenStore.rows;
    var html;
    var now = Date.now();
    var f = dashTok.filters;
    dashTokSyncControls();
    if (!rows) {
        html = dashTokenStore.failed ? dashFailed(t("tokens.could-not-load-the-tokens"), dashTokenStore.failed, "data-tok-retry") : dashLoading(t("tokens.loading-tokens"));
        document.getElementById("tok-count").textContent = "";
    } else {
        var shown = uiSortRows(rows.filter(function (tok) { return dashTokMatch(tok, f, "", now); }), dashTok.sort, dashTokSortValue);
        document.getElementById("tok-count").textContent = tn("tokens.count-tokens", shown.length);
        if (!rows.length) html = dashEmpty(t("tokens.no-tokens-yet-click-new-token"), esc(t("tokens.a-token-lets-a-service-call")));
        else if (!shown.length) html = dashEmpty(t("tokens.no-tokens-match-your-search"), esc(t("tokens.change-the-search-or-clear-the")));
        else html = shown.map(function (tok) { return dashTokRow(tok, now); }).join("");
        // Each option of a picker names how many tokens it lists with the other filters (MP-1 hint)
        [["tok-status", "status"], ["tok-zone", "zone"], ["tok-level", "level"]].forEach(function (pair) {
            var opts = document.getElementById(pair[0]).options;
            var values = Array.prototype.map.call(opts, function (opt) { return opt.value; });
            dashTokHints(rows, f, pair[1], values, now).forEach(function (n, i) { opts[i].setAttribute("data-hint", uiNum(n)); });
        });
    }
    var filtered = f.q || f.status !== "all" || f.zone || f.level;
    document.getElementById("tok-clear").classList.toggle("hidden", !filtered);
    var head = document.getElementById("tok-head");
    var headHtml = DASH_TOK_COLS.slice(0, 1).map(function (c) { return dashSortHead(dashTok.sort, c); }).join("") +
        '<div class="ui-td" role="columnheader">' + esc(t("tokens.grants")) + '</div>' +
        DASH_TOK_COLS.slice(1).map(function (c) { return dashSortHead(dashTok.sort, c); }).join("") +
        '<div class="ui-td" role="columnheader"><span class="ui-sr">' + esc(t("tokens.actions")) + '</span></div>';
    if (head.innerHTML !== headHtml) head.innerHTML = headHtml;
    dashSortButtonSync("tok-sort", dashTok.sort, t("list.name-tokens"), DASH_TOK_COLS);
    if (html === dashTok.drawn) return;
    dashTok.drawn = html;
    uiKeepFocus(rowsEl, function () { rowsEl.innerHTML = html; });
}

// How many tokens each value of one filter lists, the other filters as they are
// (the count ignores its own filter, so it is the rows a pick of it shows)
function dashTokHints(rows, f, field, values, now) {
    return values.map(function (v) {
        var g = { q: f.q, status: f.status, zone: f.zone, level: f.level };
        g[field] = v;
        return rows.filter(function (tok) { return dashTokMatch(tok, g, "", now); }).length;
    });
}

function dashTokSortValue(tok, key) {
    if (key === "name") return String(tok.name || "");
    if (key === "status") return t(TOKEN_STATUS[dashTokStatus(tok)].label);
    if (key === "lastUsed") return uiParseInstant(tok.lastUsedAt);
    return null;
}

function dashTokLoad(force) {
    return Promise.all([dashTokensLoad(force), dashZonesLoad()]).then(function () { dashTokDraw(!force); });
}

function dashTokBind() {
    if (dashTok.bound) return;
    dashTok.bound = true;
    uiPicker(document.getElementById("tok-status"), { label: t("tokens.status") });
    uiPicker(document.getElementById("tok-zone"), { label: t("tokens.zone") });
    uiPicker(document.getElementById("tok-level"), { label: t("tokens.level") });
    var search = document.getElementById("tok-search");
    search.addEventListener("input", function () {
        dashTok.filters.q = search.value.trim();
        dashTokWriteQuery();
        dashTokDraw(false);
    });
    document.getElementById("tok-status").addEventListener("change", function (event) {
        dashTok.filters.status = event.target.value;
        dashTokWriteQuery();
        dashTokDraw(false);
    });
    document.getElementById("tok-zone").addEventListener("change", function (event) {
        dashTok.filters.zone = event.target.value;
        dashTokWriteQuery();
        dashTokDraw(false);
    });
    document.getElementById("tok-level").addEventListener("change", function (event) {
        dashTok.filters.level = event.target.value;
        dashTokWriteQuery();
        dashTokDraw(false);
    });
    document.getElementById("tok-clear").addEventListener("click", function () {
        dashTok.filters = { q: "", status: "all", zone: "", level: "" };
        dashTokWriteQuery();
        dashTokDraw(false);
        search.focus({ preventScroll: true });
    });
    document.getElementById("tok-new").addEventListener("click", function () {
        var btn = document.getElementById("tok-new");
        btn.disabled = true;
        dashZonesLoad().then(function () {
            btn.disabled = false;
            if (!dash.areas.length) {
                showToast(t("tokens.could-not-load-the-zones", { reason: dash.zonesFailed || t("tokens.failed") }), "error");
                return;
            }
            dashTokenDialog(null);
        });
    });
    document.getElementById("tok-created-dismiss").addEventListener("click", function () {
        document.getElementById("tok-created").classList.add("hidden");
        document.getElementById("tok-created-value").textContent = "";
    });
    document.getElementById("tok-created-copy").addEventListener("click", function () {
        var token = document.getElementById("tok-created-value").textContent || "";
        if (!token) return;
        uiCopyText(token).then(function (ok) {
            showToast(ok ? t("tokens.token-copied-to-clipboard") : t("tokens.failed-to-copy-token"), ok ? "success" : "error");
        });
    });
    document.getElementById("tok-rows").addEventListener("click", function (event) {
        if (event.target.closest("[data-tok-retry]")) dashTokLoad(true);
    });
    dashFreshBind("tok-fresh", function () { dashTokLoad(true); });
    dashBindList({
        head: "tok-head", rows: "tok-rows", sortButton: "tok-sort", name: t("list.name-tokens"), cols: DASH_TOK_COLS, state: dashTok.sort,
        redraw: function () { dashTokDraw(false); },
        open: function (id) { var tok = dashTokFind(id); if (tok) dashTokenDialog(tok); },
        menu: dashTokMenu,
    });
}

function dashTokMenu(id, button) {
    var tok = dashTokFind(id);
    if (!tok) return;
    uiMenu(button, [
        { label: t("tokens.edit"), action: "token-edit", run: function () { dashTokenDialog(tok); } },
        { label: t("tokens.usage"), action: "token-usage", run: function () { uiGo("usage", "token=" + encodeURIComponent(tok.id)); } },
        { label: t("tokens.copy-token"), action: "token-copy", disabled: !tok.canReveal, reason: t(TOKEN_LOCKED_REASON), run: function () { dashMfaDialog("token", tok); } },
        { label: tok.isActive ? t("tokens.disable") : t("tokens.enable"), action: "token-toggle", run: function () { dashTokToggle(tok, button); } },
        { label: t("tokens.delete"), action: "token-delete", danger: true, run: function () { dashTokDelete(tok); } },
    ], t("tokens.actions-for", { name: tok.name }));
}

// One click, no question and no Undo (owner decision, DS-20): the row's button
// waits while it runs and the toast names the result
function dashTokToggle(tok, button) {
    var next = !tok.isActive;
    if (button) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
    }
    uiMutated();
    api("PATCH", "/admin/api/tokens/" + encodeURIComponent(tok.id), { isActive: next }).then(function (res) {
        if (button) {
            button.disabled = false;
            button.removeAttribute("aria-busy");
        }
        if (!res.ok) {
            if (res.status !== 401) showToast(t("tokens.could-not", { action: next ? t("tokens.enable-2") : t("tokens.disable-2"), name: tok.name, reason: apiError(res, t("tokens.save-failed")) }), "error");
            return;
        }
        showToast(next ? t("tokens.is-enabled", { name: tok.name }) : t("tokens.is-disabled", { name: tok.name }), "success");
        uiMutated();
        dashTokLoad(true);
    });
}

function dashTokDelete(tok) {
    uiConfirm({ title: t("tokens.delete-token"), message: t("tokens.delete-this-token-permanently"), confirmText: t("tokens.delete"), danger: true }).then(function (ok) {
        if (!ok) return;
        uiMutated();
        api("DELETE", "/admin/api/tokens/" + encodeURIComponent(tok.id)).then(function (res) {
            if (!res.ok) {
                if (res.status !== 401) showToast(t("tokens.delete-failed"), "error");
                return;
            }
            showToast(t("tokens.deleted", { name: tok.name }), "success");
            uiMutated();
            dashTokLoad(true);
        });
    });
}

// The flags of a stored grant level ("read,delete", or the shortcuts "all" and "readwrite")
function dashLevelFlags(level) {
    var v = String(level || "");
    if (v === "all") v = "read,write,delete";
    else if (v === "readwrite") v = "read,write";
    return v.split(",").map(function (s) { return s.trim(); }).filter(function (fl) { return TOKEN_FLAGS.indexOf(fl) !== -1; });
}

// Printable ASCII without spaces: where JavaScript's toLowerCase() and the server's
// lower() agree and there is nothing to trim. The server folds every area with Python's
// strip().lower(), which differs from trim().toLowerCase() elsewhere (U+FEFF, other
// Unicode), so other spellings are matched only exactly.
var DASH_PLAIN_AREA = /^[\x21-\x7e]+$/;

// The token's stored grants per zone row of the token dialog: a grant belongs to the
// zone with its exact name, else to the one zone whose name differs from it only in
// case (as /validate compares). Each row gets the flags it holds and the grants it
// came from (more than one when the token holds the zone in several spellings).
function dashGrantRows(areas, grants) {
    var rows = Object.create(null); // keyed by zone name: "constructor" is a valid one
    (grants || []).forEach(function (g) {
        if (!g || typeof g.area !== "string") return;
        var target = areas.indexOf(g.area) !== -1 ? g.area : null;
        if (target === null && DASH_PLAIN_AREA.test(g.area)) {
            var key = g.area.toLowerCase();
            var hits = areas.filter(function (a) { return DASH_PLAIN_AREA.test(a) && a.toLowerCase() === key; });
            if (hits.length === 1) target = hits[0];
        }
        if (target === null) return;
        var row = rows[target] || (rows[target] = { flags: [], grants: [] });
        dashLevelFlags(g.level).forEach(function (fl) { if (row.flags.indexOf(fl) === -1) row.flags.push(fl); });
        row.grants.push({ area: g.area, level: String(g.level || "") });
    });
    return rows;
}

// The notice on a row that holds the zone in more than one spelling
function dashPermNote(area, row) {
    if (!row || row.grants.length < 2) return "";
    return t("tokens.this-token-holds", { area: area, grants: row.grants.map(function (g) { return g.area + ": " + g.level; }).join("; ") });
}

// The zone permission rows of the token dialog
function dashPermRows(rows) {
    return dash.areas.map(function (a) {
        var flags = rows[a] ? rows[a].flags : [];
        var note = dashPermNote(a, rows[a]);
        var all = TOKEN_FLAGS.every(function (x) { return flags.indexOf(x) !== -1; });
        return '<div class="ui-perm" data-area="' + esc(String(a).toLowerCase()) + '" data-area-name="' + esc(a) + '"><b><bdi dir="auto">' + esc(a) + "</bdi></b>" +
            TOKEN_FLAGS.map(function (fl) {
                return '<label><input type="checkbox" class="ui-check" data-flag="' + fl + '"' + (flags.indexOf(fl) !== -1 ? " checked" : "") +
                    ' aria-label="' + esc(t("tokens.flag-for-zone", { flag: t("tokens.flag." + fl), zone: a })) + '"> ' + esc(t("tokens.flag." + fl)) + "</label>";
            }).join("") +
            '<label><input type="checkbox" class="ui-check" data-area-all' + (all ? " checked" : "") + ' aria-label="' + esc(t("tokens.all", { zone: a })) + '"> ' + esc(t("tokens.all-2")) + '</label>' +
            (note ? '<p class="ui-perm-note" role="note"><bdi dir="auto">' + esc(note) + "</bdi></p>" : "") + "</div>";
    }).join("");
}

function dashPermGrants(root) {
    var grants = [];
    root.querySelectorAll(".ui-perm").forEach(function (row) {
        var flags = TOKEN_FLAGS.filter(function (fl) { return row.querySelector('[data-flag="' + fl + '"]').checked; });
        if (flags.length) grants.push({ area: row.getAttribute("data-area-name"), level: flags.join(",") });
    });
    return grants;
}

// Create (t null) or edit a token. Every field of the old dialogs is kept.
function dashTokenDialog(type) {
    var editing = !!tok;
    var d = null;
    var close = function () { if (d) d.backdrop.remove(); };
    var guardedClose = function () { uiCloseGuarded(d, close); };
    var selected = dashGrantRows(dash.areas, editing ? tok.grants : []);
    var expiresInitial = editing && tok.expiresAt ? uiToZonedInput(tok.expiresAt) : "";
    var formId = "tok-form";
    var body = '<div class="ui-form">' +
        '<div class="ui-field"><label class="ui-label" for="tf-name">' + esc(t("tokens.name")) + '</label><input id="tf-name" class="ui-input" name="name" required dir="auto" autocomplete="off" placeholder="' + esc(t("tokens.my-api-token")) + '" data-inventory="tokens.form.name"></div>' +
        '<div class="ui-field"><label class="ui-label" for="tf-desc">' + esc(t("tokens.description")) + ' <small>' + esc(t("tokens.optional")) + '</small></label><input id="tf-desc" class="ui-input" name="description" dir="auto" autocomplete="off" placeholder="' +
        (editing ? esc(t("tokens.token-description")) : esc(t("tokens.what-this-token-is-used-for"))) + '" data-inventory="tokens.form.description"></div>' +
        (editing
            ? '<div class="ui-field"><label class="ui-label" for="tf-token">' + esc(t("tokens.replace-token-optional")) + '</label><input id="tf-token" class="ui-input ui-mono" name="token" autocomplete="off" spellcheck="false" placeholder="' + esc(t("tokens.leave-empty-to-keep-current-token")) + '" data-inventory="tokens.form.replace"></div>'
            : '<div class="ui-field"><label class="ui-label" for="tf-token">' + esc(t("tokens.existing-token-optional")) + '</label><input id="tf-token" class="ui-input ui-mono" name="token" autocomplete="off" spellcheck="false" placeholder="' + esc(t("tokens.leave-empty-to-auto-generate")) + '" data-inventory="tokens.form.existing"></div>') +
        '<div class="ui-field"><span class="ui-label" id="tf-perm-label">' + esc(t("tokens.zone-permissions")) + '</span>' +
        '<div class="ui-perm-tools"><label class="ui-search">' + DASH_SEARCH_ICON + '<input type="search" id="tf-filter" placeholder="' + esc(t("tokens.filter-zones")) + '" aria-label="' + esc(t("tokens.filter-zones-2")) + '" data-inventory="tokens.form.zonefilter"></label>' +
        '<div class="ui-chips" role="group" aria-label="' + esc(t("tokens.set-every-zone-to")) + '">' +
        '<button type="button" class="ui-btn ui-btn-sm" data-bulk="none">' + esc(t("tokens.none")) + '</button>' +
        '<button type="button" class="ui-btn ui-btn-sm" data-bulk="read">' + esc(t("tokens.read")) + '</button>' +
        '<button type="button" class="ui-btn ui-btn-sm" data-bulk="write">' + esc(t("tokens.write")) + '</button>' +
        '<button type="button" class="ui-btn ui-btn-sm" data-bulk="delete">' + esc(t("tokens.delete")) + '</button>' +
        '<button type="button" class="ui-btn ui-btn-sm" data-bulk="all">' + esc(t("tokens.all-3")) + '</button></div></div>' +
        '<div class="ui-perms" role="group" aria-labelledby="tf-perm-label">' + dashPermRows(selected) + "</div>" +
        '<p class="ui-picker-none hidden" id="tf-nomatch" role="status">' + esc(t("tokens.no-zones-match-the-filter")) + '</p></div>' +
        '<div class="ui-field"><label class="ui-label" for="tf-exp">' + esc(t("tokens.expires-optional")) + '</label><input id="tf-exp" class="ui-input" type="datetime-local" name="expires" data-inventory="tokens.form.expires">' +
        '<p class="ui-hint">' + tHtml("tokens.in", { zone: uiZoneName(), clear: tRaw('<button type="button" class="ui-btn-link" id="tf-exp-clear">' + esc(t("tokens.clear")) + "</button>") }) +
        "</p></div></div>";
    var foot = '<p class="ui-error hidden" id="tf-err" role="alert"></p>' +
        '<button type="button" class="ui-btn" data-cancel>' + esc(t("tokens.cancel")) + '</button>' +
        (editing ? '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">' + esc(t("tokens.save")) + '</button>'
            : '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">' + esc(t("tokens.create")) + '</button>');
    d = uiDialog({
        title: editing ? t("tokens.edit-token") : t("tokens.new-token"), size: "md", close: guardedClose, body: body, foot: foot,
        form: { id: formId, onSubmit: function () { submit(); } },
    });
    var f = d.body;
    var nameEl = f.querySelector("#tf-name");
    var descEl = f.querySelector("#tf-desc");
    var tokEl = f.querySelector("#tf-token");
    var expEl = f.querySelector("#tf-exp");
    var err = d.foot.querySelector("#tf-err");
    if (editing) {
        nameEl.value = tok.name || "";
        descEl.value = tok.description || "";
        expEl.value = expiresInitial;
    }
    uiGuardDialog(d);
    d.foot.querySelector("[data-cancel]").addEventListener("click", guardedClose);
    f.querySelector("#tf-exp-clear").addEventListener("click", function () { expEl.value = ""; expEl.focus(); });
    f.querySelector("#tf-filter").addEventListener("input", function (event) {
        var q = event.target.value.trim().toLowerCase();
        var any = false;
        f.querySelectorAll(".ui-perm").forEach(function (row) {
            var hit = !q || row.getAttribute("data-area").indexOf(q) !== -1;
            row.classList.toggle("hidden", !hit);
            if (hit) any = true;
        });
        f.querySelector("#tf-nomatch").classList.toggle("hidden", any);
    });
    f.addEventListener("click", function (event) {
        var b = event.target.closest("[data-bulk]");
        if (!b) return;
        var level = b.getAttribute("data-bulk");
        var want = level === "all" ? TOKEN_FLAGS : level === "none" ? [] : [level];
        f.querySelectorAll(".ui-perm").forEach(function (row) {
            TOKEN_FLAGS.forEach(function (fl) { row.querySelector('[data-flag="' + fl + '"]').checked = want.indexOf(fl) !== -1; });
            row.querySelector("[data-area-all]").checked = want.length === TOKEN_FLAGS.length;
        });
    });
    f.addEventListener("change", function (event) {
        var row = event.target.closest(".ui-perm");
        if (!row) return;
        if (event.target.hasAttribute("data-area-all")) {
            TOKEN_FLAGS.forEach(function (fl) { row.querySelector('[data-flag="' + fl + '"]').checked = event.target.checked; });
        } else {
            row.querySelector("[data-area-all]").checked = TOKEN_FLAGS.every(function (fl) { return row.querySelector('[data-flag="' + fl + '"]').checked; });
        }
    });
    var showErr = function (text, field) {
        err.textContent = text;
        err.classList.remove("hidden");
        if (field) {
            field.setAttribute("aria-invalid", "true");
            field.focus();
        }
    };
    var busy = false;
    function submit() {
        if (busy) return;
        err.classList.add("hidden");
        nameEl.removeAttribute("aria-invalid");
        var name = nameEl.value.trim();
        if (!name) { showErr(t("tokens.name-is-required"), nameEl); return; }
        var grants = dashPermGrants(f);
        if (!grants.length) { showErr(t("tokens.select-at-least-one-area"), null); return; }
        var payload = { name: name, description: descEl.value.trim(), grants: grants };
        var tokenValue = tokEl.value.trim();
        if (tokenValue) payload.token = tokenValue;
        if (editing) {
            // Sent only when changed, so the stored instant stays exactly as it was
            if (expEl.value !== expiresInitial) payload.expiresAt = expEl.value ? uiFromZonedInput(expEl.value) : "";
        } else if (expEl.value) {
            payload.expiresAt = uiFromZonedInput(expEl.value);
        }
        if (payload.expiresAt === null) { showErr(t("tokens.expires-enter-a-date-and-time"), expEl); return; }
        busy = true;
        var submitBtn = d.foot.querySelector("[type=submit]");
        submitBtn.disabled = true;
        uiMutated();
        var req = editing
            ? api("PATCH", "/admin/api/tokens/" + encodeURIComponent(tok.id), payload)
            : api("POST", "/admin/api/tokens", payload);
        req.then(function (res) {
            busy = false;
            submitBtn.disabled = false;
            if (!res.ok) {
                if (res.status !== 401) showErr(editing ? apiError(res, t("tokens.save-failed")) : apiError(res, t("tokens.failed")), null);
                return;
            }
            close();
            uiMutated();
            if (editing) {
                showToast(t("tokens.token-updated"), "success");
            } else {
                document.getElementById("tok-created-value").textContent = (res.data && res.data.token) || "";
                document.getElementById("tok-created").classList.remove("hidden");
            }
            dashTokLoad(true);
        });
    }
}

// The authenticator code, then the token or key is copied (MFA). A reply for a
// closed, dropped or replaced dialog is dropped and never copied.
var dashRevealSeq = 0;
function dashRevealCurrent(d, closed, seq) {
    return !closed && seq === dashRevealSeq && !!d && d.backdrop.isConnected;
}
function dashMfaDialog(kind, item) {
    var d = null;
    var seq = ++dashRevealSeq;
    var closed = false;
    var close = function () {
        closed = true;
        if (d) d.backdrop.remove();
    };
    var formId = "mfa-form";
    var what = kind === "token" ? t("reveal.the-token") : t("reveal.the-key");
    d = uiDialog({
        title: t("reveal.mfa-verification"), size: "sm", close: close,
        body: '<div class="ui-form"><p id="mfa-text">' + (kind === "token" ? esc(t("reveal.enter-your-6-digit-authenticator-code")) : esc(t("reveal.enter-your-6-digit-authenticator-code-2"))) + "</p>" +
            '<p class="ui-hint">' + tHtml(kind === "token" ? "reveal.token" : "reveal.api-key", { name: tRaw('<bdi dir="auto">' + esc(item.name) + "</bdi>") }) + "</p>" +
            '<div class="ui-field hidden" id="mfa-pk"><button type="button" class="ui-btn ui-btn-block" id="mfa-pk-btn" data-inventory="mfa.passkey">' + esc(t("reveal.use-a-passkey")) + '</button>' +
            '<p class="ui-hint">' + esc(t("reveal.or-enter-your-6-digit-code")) + '</p></div>' +
            '<div class="ui-field"><label class="ui-label" for="mfa-code">' + esc(t("reveal.6-digit-code")) + '</label>' +
            '<input id="mfa-code" class="ui-input ui-code" inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="' + esc(t("reveal.000000")) + '" required data-inventory="mfa.code"></div></div>',
        foot: '<p class="ui-error hidden" id="mfa-err" role="alert"></p><button type="button" class="ui-btn" data-cancel>' + esc(t("reveal.cancel")) + '</button>' +
            '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">' + esc(t("reveal.verify-copy")) + '</button>',
        form: { id: formId, onSubmit: function () { submit(); } },
    });
    d.foot.querySelector("[data-cancel]").addEventListener("click", close);
    d.backdrop.addEventListener("ui-drop", function () { closed = true; });
    var code = d.body.querySelector("#mfa-code");
    var err = d.foot.querySelector("#mfa-err");
    var btn = d.foot.querySelector("[type=submit]");
    var pkBtn = d.body.querySelector("#mfa-pk-btn");
    var busy = false;
    var verified = false;
    var setBusy = function (on) {
        busy = on;
        btn.disabled = on;
        pkBtn.disabled = on;
    };
    var fail = function (text) {
        if (!text) return;
        err.textContent = text;
        err.classList.remove("hidden");
    };
    // "Use a passkey" when this browser can and the admin has a passkey for this address
    if (!uiPasskeyBlocked(window, false)) {
        dashPkLoad(false).then(function () {
            if (!dashRevealCurrent(d, closed, seq) || verified || !dashPkUsable()) return;
            d.body.querySelector("#mfa-pk").classList.remove("hidden");
            d.body.querySelector("#mfa-text").textContent = t("reveal.use-a-passkey-or-your-6", { what: what });
        });
    }
    pkBtn.addEventListener("click", function () {
        if (busy || verified) return;
        err.classList.add("hidden");
        setBusy(true);
        dashPkStepUp().then(function (r) {
            // The browser's window may have stayed open after this dialog closed
            if (!dashRevealCurrent(d, closed, seq)) return;
            if (!r.passkey) {
                setBusy(false);
                fail(r.error);
                return;
            }
            reveal({ passkey: r.passkey, ceremony: r.ceremony }, false);
        });
    });
    function submit() {
        if (busy || verified) return;
        var value = code.value.trim();
        if (!value) { err.textContent = t("reveal.enter-the-code-from-your-authenticator"); err.classList.remove("hidden"); code.focus(); return; }
        err.classList.add("hidden");
        setBusy(true);
        reveal({ code: value }, true);
    }
    function reveal(body, withCode) {
        var path = kind === "token"
            ? api("POST", "/admin/api/tokens/" + encodeURIComponent(item.id) + "/reveal", body)
            : api("POST", "/admin/api/api-keys/" + encodeURIComponent(item.id) + "/reveal", body);
        path.then(function (res) {
            if (!dashRevealCurrent(d, closed, seq)) return;
            setBusy(false);
            if (!res.ok) {
                if (res.status === 401) return;
                fail(apiError(res, t("reveal.verification-failed")));
                if (withCode) code.select();
                return;
            }
            verified = true;
            var secret = kind === "token" ? (res.data && res.data.token) || "" : (res.data && res.data.key) || "";
            uiCopyText(secret).then(function (ok) {
                if (ok) {
                    close();
                    showToast(kind === "token" ? t("reveal.token-copied-to-clipboard") : t("reveal.api-key-copied-to-clipboard"), "success");
                    return;
                }
                if (!dashRevealCurrent(d, closed, seq)) return;
                dashMfaCopyStep(d, kind, secret, close);
            });
        });
    }
}

// The browser refused to copy without a fresh click (likely after a passkey
// window): the dialog offers a Copy button instead of only saying it failed
function dashMfaCopyStep(d, kind, secret, close) {
    var label = kind === "token" ? t("reveal.copy-token") : t("reveal.copy-key");
    d.body.innerHTML = '<div class="ui-form"><p>' + esc(t("reveal.verified-the-browser-did-not-let", { what: kind === "token" ? t("reveal.the-token") : t("reveal.the-key"), button: label })) + "</p></div>";
    d.foot.innerHTML = '<button type="button" class="ui-btn" data-cancel>' + esc(t("reveal.close")) + '</button>' +
        '<button type="button" class="ui-btn ui-btn-primary" data-copy-now>' + esc(label) + "</button>";
    d.foot.querySelector("[data-cancel]").addEventListener("click", close);
    var copyBtn = d.foot.querySelector("[data-copy-now]");
    copyBtn.addEventListener("click", function () {
        uiCopyText(secret).then(function (ok) {
            if (ok) close();
            if (kind === "token") showToast(ok ? t("reveal.token-copied-to-clipboard") : t("reveal.failed-to-copy-token"), ok ? "success" : "error");
            else showToast(ok ? t("reveal.api-key-copied-to-clipboard") : t("reveal.failed-to-copy"), ok ? "success" : "error");
        });
    });
    copyBtn.focus({ preventScroll: true });
}

DASH_PAGE_IMPL.tokens = function (info) {
    dashTokBind();
    dashTokReadQuery();
    dashTok.drawn = "";
    dashTokDraw(false);
    dashTokLoad(!!info.fresh || !!info.initial).then(function () { uiRefreshLoaded("tokens"); });
};

registerPageRefresh("tokens", 30000, function () {
    return Promise.all([dashTokensLoad(false), dashZonesLoad()]).then(function () { dashTokDraw(true); });
});

// ===========================================================================
// Zones
// ===========================================================================

var dashZones = { q: "", sort: { key: "", dir: "" }, drawn: "", bound: false, loaded: false, at: 0 };
var DASH_ZONE_COLS = [{ key: "name", label: "zones.name", kind: "text" }, { key: "createdAt", label: "zones.created", kind: "date" }];

function dashZoneFind(id) {
    return dash.zones.filter(function (z) { return z.id === id; })[0] || null;
}

// Zones whose names differ only in case, from before names had to be unique that way
// (zone names are ASCII, so toLowerCase() compares them as the server does)
function dashZoneCaseGroups(zones) {
    var groups = Object.create(null); // keyed by zone name: "constructor" is a valid one
    var order = [];
    (zones || []).forEach(function (z) {
        var key = String(z.name).toLowerCase();
        if (!groups[key]) { groups[key] = []; order.push(key); }
        groups[key].push(String(z.name));
    });
    return order.filter(function (k) { return groups[k].length > 1; }).map(function (k) { return groups[k]; });
}

function dashZoneCaseNotice(groups) {
    if (!groups.length) return "";
    return '<div class="ui-banner ui-banner-warn"><div><b>' + esc(t("zones.zones-whose-names-differ-only-in")) + '</b>' +
        tHtml("zones.a-token-for-one-of-them", { groups: tRaw(groups.map(function (g) { return '<bdi dir="auto">' + esc(g.join(", ")) + "</bdi>"; }).join("; ")) }) + "</div></div>";
}

function dashZonesDraw(quiet) {
    if (!dashZones.bound) return;
    var dupEl = document.getElementById("zone-dupes");
    if (dupEl) {
        var dupHtml = dashZoneCaseNotice(dashZones.loaded ? dashZoneCaseGroups(dash.zones) : []);
        if (dupEl.innerHTML !== dupHtml) dupEl.innerHTML = dupHtml;
        dupEl.classList.toggle("hidden", !dupHtml);
    }
    var rowsEl = document.getElementById("zone-rows");
    dashFreshSet("zone-fresh", dashZones.at, dashZones.loaded ? dash.zonesFailed : "");
    if (quiet && uiAreaHeld(rowsEl)) return;
    var html;
    var q = dashZones.q.toLowerCase();
    if (!dashZones.loaded) {
        html = dash.zonesFailed ? dashFailed(t("zones.could-not-load-the-zones"), dash.zonesFailed, "data-zone-retry") : dashLoading(t("zones.loading-zones"));
        document.getElementById("zone-count").textContent = "";
    } else {
        var shown = uiSortRows(dash.zones.filter(function (z) {
            return !q || String(z.name).toLowerCase().indexOf(q) !== -1 || String(z.description || "").toLowerCase().indexOf(q) !== -1;
        }), dashZones.sort, function (z, key) { return key === "name" ? String(z.name) : uiParseInstant(z.createdAt); });
        document.getElementById("zone-count").textContent = tn("zones.count-zones", shown.length);
        if (!dash.zones.length) html = dashEmpty(t("zones.no-zones-yet"), esc(t("zones.add-a-zone-so-tokens-can")));
        else if (!shown.length) html = dashEmpty(t("zones.no-zones-match-your-search"), esc(t("zones.no-zones-match-line")));
        else html = shown.map(function (z) {
            return '<div class="ui-tr" role="row" tabindex="0" data-key="' + esc(z.id) + '" aria-label="' + esc(z.name) + '">' +
                '<div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + uiUserText(z.name) + "</span></div>" +
                '<div class="ui-td ui-td-full" role="cell">' + (z.description ? uiUserText(z.description) : '<span class="ui-muted">-</span>') + "</div>" +
                '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("zones.created-2")) + ' </span>' + uiTime(z.createdAt) + "</div>" +
                '<div class="ui-td ui-td-end" role="cell">' + dashMenuButton(z.name) + "</div></div>";
        }).join("");
    }
    document.getElementById("zone-clear").classList.toggle("hidden", !dashZones.q);
    var head = document.getElementById("zone-head");
    var headHtml = dashSortHead(dashZones.sort, DASH_ZONE_COLS[0]) + '<div class="ui-td" role="columnheader">' + esc(t("zones.description")) + '</div>' +
        dashSortHead(dashZones.sort, DASH_ZONE_COLS[1]) + '<div class="ui-td" role="columnheader"><span class="ui-sr">' + esc(t("zones.actions")) + '</span></div>';
    if (head.innerHTML !== headHtml) head.innerHTML = headHtml;
    dashSortButtonSync("zone-sort", dashZones.sort, t("list.name-zones"), DASH_ZONE_COLS);
    if (html === dashZones.drawn) return;
    dashZones.drawn = html;
    uiKeepFocus(rowsEl, function () { rowsEl.innerHTML = html; });
}

function dashZonesLoadPage(quiet) {
    return dashZonesLoad().then(function () {
        if (dash.zonesLoaded) dashZones.loaded = true;
        if (dash.zonesLoaded && !dash.zonesFailed) dashZones.at = Date.now();
        dashZonesDraw(quiet);
    });
}

function dashZonesBind() {
    if (dashZones.bound) return;
    dashZones.bound = true;
    var search = document.getElementById("zone-search");
    search.addEventListener("input", function () {
        dashZones.q = search.value.trim();
        var p = new URLSearchParams();
        if (dashZones.q) p.set("q", dashZones.q);
        uiSetPageQuery(p);
        dashZonesDraw(false);
    });
    document.getElementById("zone-clear").addEventListener("click", function () {
        dashZones.q = "";
        search.value = "";
        uiSetPageQuery(new URLSearchParams());
        dashZonesDraw(false);
        search.focus();
    });
    var form = document.getElementById("zone-add");
    var err = document.getElementById("zone-err");
    form.addEventListener("submit", function (event) {
        event.preventDefault();
        err.classList.add("hidden");
        var nameEl = form.querySelector('[name="name"]');
        var name = nameEl.value.trim();
        if (!name) {
            err.textContent = t("zones.zone-name-enter-a-name");
            err.classList.remove("hidden");
            nameEl.focus();
            return;
        }
        var btn = form.querySelector("[type=submit]");
        btn.disabled = true;
        uiMutated();
        api("POST", "/admin/api/zones", { name: name, description: form.querySelector('[name="description"]').value.trim() }).then(function (res) {
            btn.disabled = false;
            if (!res.ok) {
                if (res.status !== 401) {
                    err.textContent = apiError(res, t("zones.failed-to-add-zone"));
                    err.classList.remove("hidden");
                }
                return;
            }
            form.reset();
            showToast(t("zones.added", { name: name }), "success");
            uiMutated();
            dashZonesLoadPage(false);
        });
    });
    document.getElementById("zone-rows").addEventListener("click", function (event) {
        if (event.target.closest("[data-zone-retry]")) dashZonesLoadPage(false);
    });
    dashFreshBind("zone-fresh", function () { dashZonesLoadPage(false); });
    dashBindList({
        head: "zone-head", rows: "zone-rows", sortButton: "zone-sort", name: t("list.name-zones"), cols: DASH_ZONE_COLS, state: dashZones.sort,
        redraw: function () { dashZonesDraw(false); },
        open: function (id) { var z = dashZoneFind(id); if (z) dashZoneDialog(z); },
        menu: function (id, button) {
            var z = dashZoneFind(id);
            if (!z) return;
            uiMenu(button, [
                { label: t("zones.edit"), action: "zone-edit", run: function () { dashZoneDialog(z); } },
                { label: t("zones.delete"), action: "zone-delete", danger: true, run: function () { dashZoneDelete(z); } },
            ], t("zones.actions-for", { name: z.name }));
        },
    });
    // Text typed into the Add form is an unsaved change
    uiUnsavedRegister(function () {
        if (uiPageCurrent !== "zones") return 0;
        return Array.prototype.slice.call(form.querySelectorAll("input")).filter(function (i) { return i.value.trim(); }).length;
    }, function () {
        form.reset();
        err.classList.add("hidden");
    });
}

function dashZoneDelete(z) {
    uiConfirm({ title: t("zones.delete-zone"), message: t("zones.delete-this-zone-tokens-referencing-it"), confirmText: t("zones.delete"), danger: true }).then(function (ok) {
        if (!ok) return;
        uiMutated();
        api("DELETE", "/admin/api/zones/" + encodeURIComponent(z.id)).then(function (res) {
            if (!res.ok) {
                if (res.status !== 401) showToast(apiError(res, t("zones.delete-failed")), "error");
                return;
            }
            showToast(t("zones.deleted", { name: z.name }), "success");
            uiMutated();
            dashZonesLoadPage(false);
            dashTokenStore.at = 0;
        });
    });
}

function dashZoneDialog(z) {
    var d = null;
    var close = function () { if (d) d.backdrop.remove(); };
    var guardedClose = function () { uiCloseGuarded(d, close); };
    var formId = "zone-edit-form";
    d = uiDialog({
        title: t("zones.edit-zone"), size: "sm", close: guardedClose,
        body: '<div class="ui-form"><div class="ui-field"><label class="ui-label" for="ze-name">' + esc(t("zones.name")) + '</label><input id="ze-name" class="ui-input" required autocomplete="off" data-inventory="zones.edit.name"></div>' +
            '<div class="ui-field"><label class="ui-label" for="ze-desc">' + esc(t("zones.description")) + '</label><input id="ze-desc" class="ui-input" dir="auto" autocomplete="off" placeholder="' + esc(t("zones.what-this-zone-controls")) + '" data-inventory="zones.edit.description"></div></div>',
        foot: '<p class="ui-error hidden" id="ze-err" role="alert"></p><button type="button" class="ui-btn" data-cancel>' + esc(t("zones.cancel")) + '</button>' +
            '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">' + esc(t("zones.save")) + '</button>',
        form: { id: formId, onSubmit: function () { submit(); } },
    });
    var nameEl = d.body.querySelector("#ze-name");
    var descEl = d.body.querySelector("#ze-desc");
    var err = d.foot.querySelector("#ze-err");
    nameEl.value = z.name || "";
    descEl.value = z.description || "";
    uiGuardDialog(d);
    d.foot.querySelector("[data-cancel]").addEventListener("click", guardedClose);
    function submit() {
        err.classList.add("hidden");
        var name = nameEl.value.trim();
        if (!name) { err.textContent = t("zones.name-is-required"); err.classList.remove("hidden"); nameEl.focus(); return; }
        var btn = d.foot.querySelector("[type=submit]");
        btn.disabled = true;
        uiMutated();
        api("PATCH", "/admin/api/zones/" + encodeURIComponent(z.id), { name: name, description: descEl.value.trim() }).then(function (res) {
            btn.disabled = false;
            if (!res.ok) {
                if (res.status !== 401) { err.textContent = apiError(res, t("zones.save-failed")); err.classList.remove("hidden"); }
                return;
            }
            close();
            uiMutated();
            showToast(t("zones.zone-updated"), "success");
            dashZonesLoadPage(false);
            dashTokenStore.at = 0;
        });
    }
}

DASH_PAGE_IMPL.zones = function () {
    dashZonesBind();
    dashZones.q = uiPageQuery().get("q") || "";
    document.getElementById("zone-search").value = dashZones.q;
    dashZones.drawn = "";
    dashZonesDraw(false);
    dashZonesLoadPage(false).then(function () { uiRefreshLoaded("zones"); });
};

registerPageRefresh("zones", 30000, function () { return dashZonesLoadPage(true); });

// ===========================================================================
// API keys
// ===========================================================================

var dashKeys = { rows: null, failed: "", master: null, sort: { key: "", dir: "" }, drawn: "", bound: false, at: 0 };
var DASH_KEY_COLS = [{ key: "name", label: "apikeys.name", kind: "text" }, { key: "createdAt", label: "apikeys.created", kind: "date" }];

function dashKeyFind(id) {
    return (dashKeys.rows || []).filter(function (k) { return k.id === id; })[0] || null;
}

function dashKeysLoad(quiet) {
    var mark = uiMutationMark();
    return Promise.all([api("GET", "/admin/api/api-keys"), api("GET", "/admin/api/api-keys/master")]).then(function (both) {
        var res = both[0];
        var master = both[1];
        if (uiStaleSince(mark)) return;
        if (res.ok && Array.isArray(res.data)) {
            dashKeys.rows = res.data;
            dashKeys.at = Date.now();
            dashKeys.failed = "";
        } else {
            dashKeys.failed = apiError(res, t("apikeys.failed"));
        }
        // The master key row follows its own answer; a failed check keeps what was shown
        if (master.ok && master.data && typeof master.data.active === "boolean") dashKeys.master = master.data;
        dashKeysDraw(quiet);
    });
}

// ADMIN_API_KEY in the environment: a row that cannot be opened, edited or deleted here
function dashKeyMasterRow() {
    var m = dashKeys.master;
    if (!m || !m.active) return "";
    return '<div class="ui-tr" role="row" aria-label="' + esc(t("apikeys.master-key")) + '">' +
        '<div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + esc(t("apikeys.master-key")) + '</span></div>' +
        '<div class="ui-td ui-td-full ui-td-wrap" role="cell"><div class="ui-tags"><span class="ui-tag ui-tag-line">' + esc(t("apikeys.all-scopes")) + '</span></div>' +
        '<p class="ui-locked-note">' + DASH_LOCK_ICON + "<span>" + tHtml("apikeys.set-by-the", { env: tRaw("<code>" + esc(m.envName || "ADMIN_API_KEY") + "</code>") }) + "</span></p></div>" +
        '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("apikeys.created-2")) + ' </span><span class="ui-muted">' + '-' + '</span></div>' +
        '<div class="ui-td ui-td-end" role="cell"></div></div>';
}

function dashKeysDraw(quiet) {
    if (!dashKeys.bound) return;
    var rowsEl = document.getElementById("key-rows");
    dashFreshSet("key-fresh", dashKeys.at, dashKeys.rows ? dashKeys.failed : "");
    if (quiet && uiAreaHeld(rowsEl)) return;
    var html;
    if (!dashKeys.rows) {
        html = dashKeys.failed ? dashFailed(t("apikeys.could-not-load-the-api-keys"), dashKeys.failed, "data-key-retry") : dashLoading(t("apikeys.loading-api-keys"));
        document.getElementById("key-count").textContent = "";
    } else {
        var rows = uiSortRows(dashKeys.rows, dashKeys.sort, function (k, key) { return key === "name" ? String(k.name) : uiParseInstant(k.createdAt); });
        // The Master key row is a row of the list, so it counts (TB-17)
        document.getElementById("key-count").textContent = tn("apikeys.count-api-keys", rows.length + (dashKeys.master && dashKeys.master.active ? 1 : 0));
        if (!rows.length) html = dashKeyMasterRow() + dashEmpty(t("apikeys.no-api-keys-yet"), esc(t("apikeys.create-one-with-new-key")));
        else html = dashKeyMasterRow() + rows.map(function (k) {
            var scopes = String(k.scopes || "").split(",").filter(Boolean);
            return '<div class="ui-tr" role="row" tabindex="0" data-key="' + esc(k.id) + '" aria-label="' + esc(k.name) + '">' +
                '<div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + uiUserText(k.name) + "</span></div>" +
                '<div class="ui-td ui-td-full ui-td-wrap" role="cell"><div class="ui-tags">' +
                scopes.map(function (s) { return '<span class="ui-tag ui-tag-line ui-mono">' + esc(s) + "</span>"; }).join("") + "</div></div>" +
                '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("apikeys.created-2")) + ' </span>' + uiTime(k.createdAt) + "</div>" +
                '<div class="ui-td ui-td-end" role="cell">' + dashMenuButton(k.name) + "</div></div>";
        }).join("");
    }
    var head = document.getElementById("key-head");
    var headHtml = dashSortHead(dashKeys.sort, DASH_KEY_COLS[0]) + '<div class="ui-td" role="columnheader">' + esc(t("apikeys.scopes")) + '</div>' +
        dashSortHead(dashKeys.sort, DASH_KEY_COLS[1]) + '<div class="ui-td" role="columnheader"><span class="ui-sr">' + esc(t("apikeys.actions")) + '</span></div>';
    if (head.innerHTML !== headHtml) head.innerHTML = headHtml;
    dashSortButtonSync("key-sort", dashKeys.sort, t("apikeys.api-keys"), DASH_KEY_COLS);
    if (html === dashKeys.drawn) return;
    dashKeys.drawn = html;
    uiKeepFocus(rowsEl, function () { rowsEl.innerHTML = html; });
}

function dashKeysBind() {
    if (dashKeys.bound) return;
    dashKeys.bound = true;
    document.getElementById("key-new").addEventListener("click", function () { dashKeyDialog(null); });
    document.getElementById("key-rows").addEventListener("click", function (event) {
        if (event.target.closest("[data-key-retry]")) dashKeysLoad(false);
    });
    dashFreshBind("key-fresh", function () { dashKeysLoad(false); });
    dashBindList({
        head: "key-head", rows: "key-rows", sortButton: "key-sort", name: t("apikeys.api-keys"), cols: DASH_KEY_COLS, state: dashKeys.sort,
        redraw: function () { dashKeysDraw(false); },
        open: function (id) { var k = dashKeyFind(id); if (k) dashKeyDialog(k); },
        menu: function (id, button) {
            var k = dashKeyFind(id);
            if (!k) return;
            uiMenu(button, [
                { label: t("apikeys.edit"), action: "apikey-edit", run: function () { dashKeyDialog(k); } },
                { label: t("apikeys.copy-key"), action: "apikey-copy", disabled: k.canReveal === false, reason: t(API_KEY_LOCKED_REASON), run: function () { dashMfaDialog("apikey", k); } },
                { label: t("apikeys.delete"), action: "apikey-delete", danger: true, run: function () { dashKeyDelete(k); } },
            ], t("apikeys.actions-for", { name: k.name }));
        },
    });
}

function dashKeyDelete(k) {
    uiConfirm({ title: t("apikeys.delete-api-key"), message: t("apikeys.delete-api-key-2", { name: k.name }), confirmText: t("apikeys.delete"), danger: true }).then(function (ok) {
        if (!ok) return;
        uiMutated();
        api("DELETE", "/admin/api/api-keys/" + encodeURIComponent(k.id)).then(function (res) {
            if (res.ok) {
                showToast(t("apikeys.api-key-deleted"), "success");
                uiMutated();
                dashKeysLoad(false);
            } else if (res.status !== 401) {
                showToast(t("apikeys.failed-to-delete-key"), "error");
            }
        });
    });
}

// Create (k null) or edit an API key: name and the five scopes as switch rows
function dashKeyDialog(k) {
    var editing = !!k;
    var d = null;
    var close = function () { if (d) d.backdrop.remove(); };
    var guardedClose = function () { uiCloseGuarded(d, close); };
    var has = editing ? String(k.scopes || "").split(",") : API_KEY_DEFAULT_SCOPES;
    var formId = "key-form";
    d = uiDialog({
        title: editing ? t("apikeys.edit-api-key") : t("apikeys.new-api-key"), size: "sm", close: guardedClose,
        body: '<div class="ui-form"><div class="ui-field"><label class="ui-label" for="kf-name">' + esc(t("apikeys.name")) + '</label><input id="kf-name" class="ui-input" required dir="auto" autocomplete="off" placeholder="' + esc(t("apikeys.e-g-n8n-integration")) + '" data-inventory="apikeys.form.name"></div>' +
            '<div class="ui-field"><span class="ui-label" id="kf-scopes-label">' + esc(t("apikeys.scopes")) + '</span><div class="ui-switch-list" role="group" aria-labelledby="kf-scopes-label">' +
            API_KEY_SCOPES.map(function (s, i) {
                return '<label class="ui-switch-row" for="kf-scope-' + i + '"><span><b class="ui-mono">' + esc(s.value) + "</b><small>" + esc(t(s.text)) + "</small></span>" +
                    '<input type="checkbox" role="switch" class="ui-switch" id="kf-scope-' + i + '" value="' + esc(s.value) + '"' + (has.indexOf(s.value) !== -1 ? " checked" : "") + "></label>";
            }).join("") + "</div></div></div>",
        foot: '<p class="ui-error hidden" id="kf-err" role="alert"></p><button type="button" class="ui-btn" data-cancel>' + esc(t("apikeys.cancel")) + '</button>' +
            (editing ? '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">' + esc(t("apikeys.save")) + '</button>'
                : '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">' + esc(t("apikeys.create")) + '</button>'),
        form: { id: formId, onSubmit: function () { submit(); } },
    });
    var nameEl = d.body.querySelector("#kf-name");
    var err = d.foot.querySelector("#kf-err");
    if (editing) nameEl.value = k.name || "";
    uiGuardDialog(d);
    d.foot.querySelector("[data-cancel]").addEventListener("click", guardedClose);
    function submit() {
        err.classList.add("hidden");
        var name = nameEl.value.trim();
        if (!name) { err.textContent = t("apikeys.name-is-required"); err.classList.remove("hidden"); nameEl.focus(); return; }
        var scopes = Array.prototype.slice.call(d.body.querySelectorAll(".ui-switch:checked")).map(function (c) { return c.value; });
        if (!scopes.length) { err.textContent = t("apikeys.select-at-least-one-scope"); err.classList.remove("hidden"); return; }
        var btn = d.foot.querySelector("[type=submit]");
        btn.disabled = true;
        uiMutated();
        var req = editing
            ? api("PATCH", "/admin/api/api-keys/" + encodeURIComponent(k.id), { name: name, scopes: scopes })
            : api("POST", "/admin/api/api-keys", { name: name, scopes: scopes });
        req.then(function (res) {
            btn.disabled = false;
            if (!res.ok) {
                if (res.status !== 401) { err.textContent = apiError(res, editing ? t("apikeys.save-failed") : t("apikeys.failed")); err.classList.remove("hidden"); }
                return;
            }
            close();
            uiMutated();
            if (editing) showToast(t("apikeys.api-key-updated"), "success");
            else dashKeyCreated((res.data && res.data.key) || "");
            dashKeysLoad(false);
        });
    }
}

function dashKeyCreated(key) {
    var d = null;
    var close = function () { if (d) d.backdrop.remove(); };
    d = uiDialog({
        title: t("apikeys.api-key-created"), size: "sm", close: close,
        body: '<div class="ui-form"><p>' + esc(t("apikeys.copy-the-key-you-can-also")) + '</p><code class="ui-secret" id="kc-key"></code></div>',
        foot: '<button type="button" class="ui-btn" data-cancel>' + esc(t("apikeys.close")) + '</button><button type="button" class="ui-btn ui-btn-primary" data-inventory="apikeys.created.copy">' + esc(t("apikeys.copy-to-clipboard")) + '</button>',
    });
    d.body.querySelector("#kc-key").textContent = key;
    d.foot.querySelector("[data-cancel]").addEventListener("click", close);
    d.foot.querySelector(".ui-btn-primary").addEventListener("click", function () {
        uiCopyText(key).then(function (ok) { showToast(ok ? t("apikeys.copied-to-clipboard") : t("apikeys.failed-to-copy"), ok ? "success" : "error"); });
    });
}

DASH_PAGE_IMPL.apikeys = function () {
    dashKeysBind();
    dashKeys.drawn = "";
    dashKeysDraw(false);
    dashKeysLoad(false).then(function () { uiRefreshLoaded("apikeys"); });
};

registerPageRefresh("apikeys", 30000, function () { return dashKeysLoad(true); });

// ===========================================================================
// Activity Log and Access Logs: 30 rows per load by cursor, new rows taken in
// by a refresh that follows `since`, merged in server order, never wiped
// ===========================================================================

var dashLogs = {
    activity: {
        id: "activity", endpoint: "/admin/api/logs/activity", filterParam: "action", queryKey: "action",
        loadingText: "logs.loading-activity", emptyText: "logs.no-activity-logs-found", emptyLine: "logs.no-activity-line", failedText: "logs.failed-to-load-activity-logs",
    },
    access: {
        id: "access", endpoint: "/admin/api/logs/access", filterParam: "result", queryKey: "result", tokenParam: "token_id",
        loadingText: "logs.loading-access-logs", emptyText: "logs.no-access-logs-found", emptyLine: "logs.no-access-line", failedText: "logs.failed-to-load-access-logs",
    },
};
Object.keys(dashLogs).forEach(function (k) {
    var s = dashLogs[k];
    s.rows = null;
    s.heads = {};
    s.nexts = {};
    s.total = 0;
    s.hasMore = false;
    s.gen = 0;
    s.loading = false;
    s.moreFailed = "";
    s.failed = "";
    s.rowsFor = null;
    s.refreshFailed = "";
    s.capped = false;
    s.loadSeq = 0;
    s.pending = [];
    s.drawn = "";
    s.bound = false;
    s.applied = { search: "", filter: "", token: "" };
    s.at = 0;
    s.observer = null;
});

function dashLogQuery(s, extra) {
    var p = new URLSearchParams();
    if (s.applied.search) p.set("search", s.applied.search);
    if (s.applied.filter) p.set(s.filterParam, s.applied.filter);
    if (s.applied.token && s.tokenParam) p.set(s.tokenParam, s.applied.token);
    Object.keys(extra).forEach(function (k) { p.set(k, extra[k]); });
    return s.endpoint + "?" + p.toString();
}

// Keep the cursors the server gave: headCursor for a page's first row, nextCursor for its last
function dashLogCursors(s, data) {
    var logs = data.logs || [];
    if (!logs.length) return;
    if (data.headCursor) s.heads[logs[0].id] = data.headCursor;
    if (data.nextCursor) s.nexts[logs[logs.length - 1].id] = data.nextCursor;
}

// The cursor after the last row shown; when that row has none, the lowest row that has one
function dashLogTailCursor(s) {
    for (var i = s.rows.length - 1; i >= 0; i--) {
        if (s.nexts[s.rows[i].id]) return s.nexts[s.rows[i].id];
    }
    return "";
}

// The filters a list was loaded with; rows, cursors and the pill belong to them
function dashLogKey(s) {
    return JSON.stringify([s.applied.search, s.applied.filter, s.applied.token]);
}

// The rows shown were loaded with the filters applied now: only then may a refresh
// follow their top or Load more follow their end
function dashLogReady(s) {
    return s.rows !== null && s.rowsFor === dashLogKey(s);
}

// A load takes the loading flag with its own token; its answer gives back only
// that token, so a late answer never leaves the list stuck nor frees a newer load
function dashLogTake(s) {
    s.loadSeq += 1;
    s.loading = s.loadSeq;
    return s.loadSeq;
}
function dashLogRelease(s, token) {
    if (s.loading !== token) return false;
    s.loading = false;
    dashLogDim(s, false);
    return true;
}

function dashLogFirst(s, quiet, isCurrent) {
    var gen = ++s.gen;
    var key = dashLogKey(s);
    var token = dashLogTake(s);
    // A first load starts a new list: an earlier cap offer no longer applies
    s.capped = false;
    s.moreFailed = "";
    if (!quiet) dashLogDim(s, true);
    return api("GET", dashLogQuery(s, { limit: String(LOG_PAGE_SIZE), page: "1" })).then(function (res) {
        dashLogRelease(s, token);
        if (gen !== s.gen || (isCurrent && !isCurrent())) return;
        if (!res.ok || !res.data) {
            var reason = apiError(res, t(s.failedText));
            if (s.rowsFor !== key) {
                // Rows of other filters never stay under these ones: the first load failed
                s.rows = null;
                s.rowsFor = null;
                s.heads = {};
                s.nexts = {};
                s.pending = [];
                s.hasMore = false;
                s.failed = reason;
            } else {
                dashLogRefreshFailed(s, reason);
            }
            dashLogDraw(s, quiet);
            return;
        }
        s.failed = "";
        s.refreshFailed = "";
        s.capped = false;
        s.at = Date.now();
        s.heads = {};
        s.nexts = {};
        dashLogCursors(s, res.data);
        s.rows = uiLogMerge([], res.data.logs || []);
        s.rowsFor = key;
        s.total = res.data.total || 0;
        s.hasMore = (res.data.logs || []).length >= LOG_PAGE_SIZE;
        s.pending = [];
        dashLogDraw(s, false);
        dashLogEndCheck(s);
    });
}

// An update that failed keeps the rows, says so under the list, and tells once
// per run of failures with the old wording, offering to try again (FB-2)
function dashLogRefreshFailed(s, reason) {
    if (!s.refreshFailed) {
        showToast(t(s.failedText), "error", { action: { label: t("logs.try-again"), run: function () { return dashLogRefresh(s); } } });
    }
    s.refreshFailed = reason || t(s.failedText);
}

// TB-6: the next page loads from a sentinel under the list before its end comes
// into view; a failed page waits for Try again and never asks again by itself
function dashLogWatch(s) {
    var line = document.getElementById(s.id + "-more");
    if (!line || typeof IntersectionObserver !== "function") return;
    s.observer = new IntersectionObserver(function (entries) {
        if (entries.some(function (e) { return e.isIntersecting; })) dashLogNearEnd(s);
    }, { root: uiMainScroller(), rootMargin: "0px 0px 400px 0px" });
    s.observer.observe(line);
}

function dashLogNearEnd(s) {
    if (uiPageCurrent !== s.id || s.moreFailed || s.loading || !s.hasMore || !dashLogReady(s)) return;
    dashLogMore(s);
}

// A short page can leave the end in view, and the observer reports only changes
function dashLogEndCheck(s) {
    if (!s.observer || typeof requestAnimationFrame !== "function") return;
    requestAnimationFrame(function () {
        var line = document.getElementById(s.id + "-more");
        var main = uiMainScroller();
        if (!line || !main || !line.getClientRects().length) return;
        if (line.getBoundingClientRect().top < main.getBoundingClientRect().bottom + 400) dashLogNearEnd(s);
    });
}

function dashLogMore(s) {
    if (s.loading || !dashLogReady(s) || !s.rows.length) return;
    var cursor = dashLogTailCursor(s);
    if (!cursor) return;
    var gen = s.gen;
    var token = dashLogTake(s);
    s.moreFailed = "";
    dashLogDraw(s, false);
    api("GET", dashLogQuery(s, { limit: String(LOG_PAGE_SIZE), before: cursor })).then(function (res) {
        dashLogRelease(s, token);
        if (gen !== s.gen) return;
        if (!res.ok || !res.data) {
            s.moreFailed = apiError(res, t(s.failedText));
            dashLogDraw(s, false);
            return;
        }
        dashLogCursors(s, res.data);
        var logs = res.data.logs || [];
        s.rows = uiLogMerge(s.rows, logs);
        s.hasMore = logs.length >= LOG_PAGE_SIZE;
        dashLogDraw(s, false);
        dashLogEndCheck(s);
    });
}

// New rows since the top row: `since` its cursor (the server looks 60 seconds
// further back), page by page with `before` while pages are full. While older
// rows remain to load, the pages stop once they pass the last row shown (what
// lies below belongs to Load more). More than 20 full pages of new rows: the
// newest rows become the list, and Load more goes on from there.
function dashLogRefresh(s, isCurrent) {
    if (s.loading || s.capped) return Promise.resolve();
    if (!dashLogReady(s) || !s.rows.length) return dashLogFirst(s, true, isCurrent);
    var since = s.heads[s.rows[0].id];
    if (!since) return dashLogFirst(s, true, isCurrent);
    var gen = s.gen;
    var last = s.rows[s.rows.length - 1];
    var hasMore = s.hasMore;
    var collected = [];
    var cursors = { heads: {}, nexts: {} };
    var step = function (before, pages) {
        var extra = { limit: String(LOG_SINCE_LIMIT), since: since };
        if (before) extra.before = before;
        return api("GET", dashLogQuery(s, extra)).then(function (res) {
            if (gen !== s.gen || (isCurrent && !isCurrent())) return null;
            if (!res.ok || !res.data) return { failed: apiError(res, t(s.failedText)) };
            var logs = res.data.logs || [];
            if (logs.length) {
                if (res.data.headCursor) cursors.heads[logs[0].id] = res.data.headCursor;
                if (res.data.nextCursor) cursors.nexts[logs[logs.length - 1].id] = res.data.nextCursor;
            }
            collected = collected.concat(logs);
            if (logs.length < LOG_SINCE_LIMIT || !res.data.nextCursor) return true;
            if (hasMore && uiLogCompare(logs[logs.length - 1], last) > 0) return true;
            if (pages + 1 >= LOG_REFRESH_MAX_PAGES) return false;
            return step(res.data.nextCursor, pages + 1);
        });
    };
    return step("", 0).then(function (complete) {
        if (complete === null || gen !== s.gen || (isCurrent && !isCurrent())) return;
        if (complete && complete.failed) {
            dashLogRefreshFailed(s, complete.failed);
            dashLogDraw(s, true);
            return;
        }
        s.refreshFailed = "";
        s.at = Date.now();
        if (complete === false) {
            s.capped = true;
            s.pending = [];
            dashLogDraw(s, true);
            return;
        }
        var fresh = uiLogFresh(s.rows, collected, s.hasMore);
        Object.keys(cursors.heads).forEach(function (id) { s.heads[id] = cursors.heads[id]; });
        Object.keys(cursors.nexts).forEach(function (id) { s.nexts[id] = cursors.nexts[id]; });
        if (!fresh.length) { s.pending = []; dashLogDraw(s, true); return; }
        var main = uiMainScroller();
        var list = document.getElementById(s.id + "-panel");
        var atTop = main && list ? list.getBoundingClientRect().top >= main.getBoundingClientRect().top - 8 && main.scrollTop <= list.offsetTop + 8 : true;
        if (atTop && !uiAreaHeld(document.getElementById(s.id + "-rows"))) {
            dashLogTakeIn(s, fresh);
        } else {
            // Scrolled away or busy: the list stays, the new rows wait behind the pill (LD-15)
            s.pending = fresh;
            dashLogDraw(s, true);
        }
    });
}

// The offer at the top edge, taken: the waiting rows, or after a cap the newest rows as a new list
function dashLogTakeNew(s) {
    if (s.capped) {
        s.capped = false;
        return dashLogFirst(s, false);
    }
    dashLogTakeIn(s, s.pending);
    return Promise.resolve();
}

function dashLogTakeIn(s, fresh) {
    s.rows = uiLogMerge(s.rows, fresh);
    s.total += fresh.length;
    s.pending = [];
    dashLogDraw(s, false);
}

function dashLogDim(s, on) {
    var el = document.getElementById(s.id + "-rows");
    if (!el) return;
    clearTimeout(s.dimTimer);
    if (on) {
        el.setAttribute("aria-busy", "true");
        s.dimTimer = setTimeout(function () { el.classList.add("is-dim"); }, 200);
    } else {
        el.removeAttribute("aria-busy");
        el.classList.remove("is-dim");
    }
}

function dashLogRowHtml(s, log) {
    if (s.id === "activity") {
        return '<div class="ui-tr" role="row" data-key="' + esc(log.id) + '">' +
            '<div class="ui-td" role="cell">' + uiTime(log.createdAt) + "</div>" +
            '<div class="ui-td ui-td-main" role="cell">' + dashLogActor(log.actor) + "</div>" +
            '<div class="ui-td" role="cell">' + dashTag(uiIdWord("logs.action.", log.action), "") + "</div>" +
            '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("logs.entity-type")) + ' </span><span class="ui-muted">' + esc(log.entityType ? uiIdWord("logs.entity.", log.entityType) : "-") + "</span></div>" +
            '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("logs.entity-name")) + ' </span>' + (log.entityName ? uiUserText(log.entityName) : '<span class="ui-muted">-</span>') + "</div>" +
            '<div class="ui-td ui-td-full ui-td-wrap" role="cell">' + dashLogDetails(log) + "</div>" +
            '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("logs.ip-address")) + ' </span>' + uiCopyable(log.ipAddress) + "</div></div>";
    }
    return '<div class="ui-tr" role="row" data-key="' + esc(log.id) + '">' +
        '<div class="ui-td" role="cell">' + uiTime(log.createdAt) + "</div>" +
        '<div class="ui-td ui-td-main" role="cell">' + uiCopyable(log.tokenName, "is-text ui-td-name") + "</div>" +
        '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("logs.area-zone")) + ' </span>' + uiCopyable(log.area, "is-text") + "</div>" +
        '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("logs.level")) + ' </span><span class="ui-mono">' + esc(uiIdWord("logs.level.", log.level)) + "</span></div>" +
        '<div class="ui-td" role="cell">' + (log.result ? dashTag(t("logs.allowed"), "ok") : dashTag(t("logs.denied"), "fail")) + "</div>" +
        '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("logs.ip-address")) + ' </span>' + uiCopyable(log.ipAddress) + "</div></div>";
}

// Who did it: a user name copies on a click (FB-12); a system actor (setup, the
// master API key...) is a word, not a value to copy
function dashLogActor(actor) {
    if (Object.prototype.hasOwnProperty.call(UI_TEXT, "logs.actor." + String(actor))) {
        return '<span class="ui-td-name">' + uiUserText(uiIdWord("logs.actor.", actor)) + "</span>";
    }
    return uiCopyable(actor, "is-text ui-td-name");
}

// What an activity changed, in words; every value escaped
function dashLogDetails(log) {
    var det = log.details;
    if (!det) return '<span class="ui-muted">-</span>';
    var act = log.action;
    var grantsText = function (grants) {
        if (!grants || !grants.length) return esc(t("logs.none"));
        return grants.map(function (g) { return esc(g.area) + ": " + esc(String(g.level || "").split(",").map(function (f) { return f ? uiIdWord("logs.level.", f) : f; }).join("+")); }).join(", ");
    };
    var updated = function (items) {
        if (!items.length) return '<span class="ui-muted">' + esc(t("logs.no-modifications")) + '</span>';
        return "<b>" + esc(t("logs.updated")) + "</b>" + " " + items.join(", ");
    };
    try {
        if (act === "token_create") {
            return "<b>" + esc(t("logs.grants")) + "</b>" + " " + grantsText(det.grants) + "<br><b>" + esc(t("logs.expires")) + "</b>" + " " + (det.expiresAt ? esc(formatTime(det.expiresAt, { seconds: false })) : esc(t("logs.never")));
        }
        if (act === "token_patch" || act === "zone_patch" || act === "api_key_patch") {
            var u = det.updates || {};
            var items = [];
            if (u.name !== undefined && u.name !== null) items.push(esc(t("logs.updated-name", { name: u.name })));
            if (u.description !== undefined && u.description !== null) items.push(esc(t("logs.updated-description", { description: u.description })));
            if (u.isActive !== undefined && u.isActive !== null) items.push(esc(t("logs.status", { status: u.isActive ? t("logs.enabled") : t("logs.disabled") })));
            if (u.grants !== undefined && u.grants !== null) items.push(tHtml("logs.grants-2", { grants: tRaw(grantsText(u.grants)) }));
            if (u.expiresAt !== undefined && u.expiresAt !== null) items.push(esc(t("logs.expires-2", { date: u.expiresAt ? formatTime(u.expiresAt, { seconds: false }) : t("logs.never") })));
            if (u.scopes !== undefined && u.scopes !== null) items.push(esc(t("logs.scopes", { scopes: [].concat(u.scopes).join(", ") })));
            return updated(items);
        }
        if (act === "zone_create") return det.description ? "<b>" + esc(t("logs.description")) + "</b>" + " " + esc(det.description) : '<span class="ui-muted">-</span>';
        if (act === "api_key_create") return det.scopes ? "<b>" + esc(t("logs.scopes-2")) + "</b>" + " " + esc(det.scopes) : '<span class="ui-muted">-</span>';
        if (act === "settings.update" && DASH_LOG_SETTINGS[log.entityName]) return dashLogSettings(DASH_LOG_SETTINGS[log.entityName], det);
        var passkey = dashLogPasskeyDetails(log, det);
        if (passkey !== null) return passkey;
    } catch (e) { /* shown as data below */ }
    return '<code class="ui-mono">' + esc(JSON.stringify(det)) + "</code>";
}

// settings.update: the fields a version changed, from the old value to the new one
var DASH_LOG_SETTINGS = {
    general: [
        ["timeZone", "logs.time-zone", ""],
        ["activityLogRetentionDays", "logs.activity-log-retention", "days"],
        ["accessLogRetentionDays", "logs.access-log-retention", "days"],
        ["cookieSecure", "logs.secure-cookie", "onoff"],
    ],
    redis: [["url", "logs.redis-url", ""]],
};

function dashLogSettingValue(v, kind) {
    if (v === null || v === undefined) return t("logs.default");
    if (kind === "onoff") return v ? t("logs.on") : t("logs.off");
    if (kind === "days") return tn("logs.count-days", v);
    return String(v);
}

function dashLogSettings(fields, det) {
    // The Redis entry may carry the masked URLs themselves or an object with a url field
    var side = function (v) { return v !== null && typeof v === "object" ? v : { url: v }; };
    var from = fields[0][0] === "url" ? side(det.from) : det.from || {};
    var to = fields[0][0] === "url" ? side(det.to) : det.to || {};
    var items = fields.filter(function (f) { return JSON.stringify(from[f[0]]) !== JSON.stringify(to[f[0]]); }).map(function (f) {
        return esc(t("logs.setting-change", { name: t(f[1]), from: dashLogSettingValue(from[f[0]], f[2]), to: dashLogSettingValue(to[f[0]], f[2]) }));
    });
    var version = det.version ? "<b>" + esc(t("logs.version", { version: uiNum(det.version) })) + "</b> " : "";
    return version + (items.length ? items.join(", ") : '<span class="ui-muted">' + esc(t("logs.no-modifications")) + '</span>');
}

var DASH_STEP_UP_METHODS = { passkey: "logs.passkey", code: "logs.authenticator-code" };
var DASH_PASSKEY_WHERE = { login: "logs.sign-in", token_reveal: "logs.copy-token", api_key_reveal: "logs.copy-key", passkey_add: "logs.add-a-passkey" };
var DASH_PASSKEY_REASONS = { invalid: "logs.the-passkey-could-not-be-verified", user_handle: "logs.the-passkey-does-not-match-its" };

// The passkey parts of the Activity Log in words; null when the row is not one of them
function dashLogPasskeyDetails(log, det) {
    var act = log.action;
    var word = function (table, value) {
        return Object.prototype.hasOwnProperty.call(table, value) ? esc(t(table[value])) : esc(value);
    };
    if (act === "login" && det.method === "passkey") {
        return "<b>" + esc(t("logs.signed-in-with")) + "</b> " + (det.passkey ? tHtml("logs.passkey-named", { name: tRaw(uiUserText(det.passkey)) }) : esc(t("logs.passkey-2")));
    }
    if ((act === "token_reveal" || act === "api_key_reveal") && det.method) {
        return "<b>" + esc(t("logs.confirmed-with")) + "</b>" + " " + word(DASH_STEP_UP_METHODS, det.method);
    }
    if (act === "passkey_added") return det.rpId ? "<b>" + esc(t("logs.made-for")) + "</b>" + " " + esc(det.rpId) : '<span class="ui-muted">-</span>';
    if (act === "passkey_renamed") return typeof det.from === "string" ? "<b>" + esc(t("logs.renamed-from")) + "</b>" + " " + uiUserText(det.from) : '<span class="ui-muted">-</span>';
    if (act === "passkey_failed") {
        return "<b>" + esc(t("logs.where")) + "</b>" + " " + word(DASH_PASSKEY_WHERE, det.where) + "<br><b>" + esc(t("logs.reason")) + "</b>" + " " + word(DASH_PASSKEY_REASONS, det.reason);
    }
    if (act === "settings.update" && log.entityName === "passkeys" && det.from && det.to) {
        var from = det.from.origin;
        var to = det.to.origin;
        if (!from && to) return "<b>" + esc(t("logs.passkey-address-set-to")) + "</b>" + " " + esc(to);
        if (from && !to) return "<b>" + esc(t("logs.passkey-address-removed")) + "</b> " + esc(t("logs.it-was", { origin: from }));
        return "<b>" + esc(t("logs.passkey-address-changed")) + "</b> " + esc(t("logs.from", { from: from, to: to }));
    }
    return null;
}

function dashLogDraw(s, quiet) {
    if (!s.bound) return;
    var rowsEl = document.getElementById(s.id + "-rows");
    var moreEl = document.getElementById(s.id + "-more");
    var pillEl = document.getElementById(s.id + "-new");
    var countEl = document.getElementById(s.id + "-count");
    // The pill and the line under the list always follow; the rows only when free
    var n = s.pending.length;
    pillEl.classList.toggle("hidden", !n && !s.capped);
    pillEl.querySelector("[data-pill-text]").textContent = s.capped ? t("logs.more-new-entries-than-can-be")
        : tn("logs.new-entries", n);
    var moreHtml = "";
    if (s.moreFailed) {
        moreHtml = '<span role="alert">' + esc(t("logs.could-not-load-more-entries", { reason: s.moreFailed })) + '</span>' + ' ' + '<button type="button" class="ui-btn-link" data-log-more>' + esc(t("logs.try-again")) + '</button>';
    } else if (s.rows && s.rows.length && s.hasMore) {
        // The sentinel loads the next page by itself; without one, a Load more button
        moreHtml = s.loading || s.observer ? '<span class="ui-spin" aria-hidden="true"></span><span role="status">' + esc(t("logs.loading-more")) + '</span>'
            : '<button type="button" class="ui-btn ui-btn-sm" data-log-more data-inventory="' + s.id + '.loadmore">' + esc(t("logs.load-more")) + '</button>';
    } else if (s.rows && s.rows.length > LOG_PAGE_SIZE) {
        moreHtml = '<span role="status">' + esc(t("logs.all", { n: uiNum(s.rows.length) })) + "</span>";
    }
    // A failed update keeps the rows and says so under the count (LD-12)
    dashFreshSet(s.id + "-fresh", s.at, s.rows ? s.refreshFailed : "");
    if (moreEl.innerHTML !== moreHtml) moreEl.innerHTML = moreHtml;
    moreEl.classList.toggle("hidden", !moreHtml);
    var filtered = s.applied.search || s.applied.filter;
    if (s.rows) countEl.textContent = filtered ? tn("logs.match-the-filters", s.total) : tn("logs.count-entries", s.total);
    if (quiet && uiAreaHeld(rowsEl)) return;
    var html;
    if (s.rows === null) html = s.failed ? dashFailed(t(s.failedText), s.failed, "data-log-retry") : dashLoading(t(s.loadingText));
    else if (!s.rows.length) html = dashEmpty(t(s.emptyText), filtered ? esc(t("logs.change-the-filters-or-reset-them")) : esc(t(s.emptyLine)));
    else html = s.rows.map(function (log) { return dashLogRowHtml(s, log); }).join("");
    if (html === s.drawn) return;
    s.drawn = html;
    uiKeepFocus(rowsEl, function () { rowsEl.innerHTML = html; });
}

function dashLogApply(s, fromInputs) {
    var search = document.getElementById(s.id + "-search");
    var sel = document.getElementById(s.id + "-filter");
    if (fromInputs) s.applied = { search: search.value.trim(), filter: sel.value, token: s.applied.token };
    var p = new URLSearchParams();
    if (s.applied.search) p.set("q", s.applied.search);
    if (s.applied.filter) p.set(s.queryKey, s.applied.filter);
    if (s.applied.token) p.set("token", s.applied.token);
    uiSetPageQuery(p);
    dashLogFilterMarks(s);
    var main = uiMainScroller();
    if (main) main.scrollTop = 0;
    return dashLogFirst(s, false);
}

function dashLogBind(s) {
    if (s.bound) return;
    s.bound = true;
    var search = document.getElementById(s.id + "-search");
    var sel = document.getElementById(s.id + "-filter");
    uiPicker(sel, { label: s.id === "activity" ? t("logs.action") : t("logs.result") });
    // The search applies as you type (after a short pause) and at once on Enter
    var typing = null;
    var applySearch = function () {
        clearTimeout(typing);
        typing = null;
        if (search.value.trim() !== s.applied.search) dashLogApply(s, true);
    };
    search.addEventListener("input", function () {
        clearTimeout(typing);
        typing = setTimeout(applySearch, 350);
    });
    search.addEventListener("keydown", function (event) {
        if (event.key === "Enter") { event.preventDefault(); applySearch(); }
    });
    sel.addEventListener("change", function () { dashLogApply(s, true); });
    document.getElementById(s.id + "-reset").addEventListener("click", function () {
        search.value = "";
        sel.value = "";
        uiPickerSync(sel);
        s.applied.token = "";
        dashLogApply(s, true);
    });
    var tokenChip = document.getElementById(s.id + "-token");
    if (tokenChip) {
        tokenChip.addEventListener("click", function () {
            s.applied.token = "";
            dashLogApply(s, false);
        });
    }
    document.getElementById(s.id + "-new").addEventListener("click", function () {
        dashLogTakeNew(s);
        var main = uiMainScroller();
        var panel = document.getElementById(s.id + "-panel");
        if (main && panel) main.scrollTop = Math.max(0, panel.offsetTop - 16);
    });
    var panel = document.getElementById(s.id + "-panel");
    panel.addEventListener("click", function (event) {
        if (event.target.closest("[data-log-more]")) dashLogMore(s);
        else if (event.target.closest("[data-log-retry]")) dashLogFirst(s, false);
        else if (event.target.closest("[data-fresh-retry]")) dashLogRefresh(s);
    });
    dashLogWatch(s);
}

// Reset shows while any filter is set; the token filter (from Usage) is a chip that removes it
function dashLogFilterMarks(s) {
    var reset = document.getElementById(s.id + "-reset");
    if (reset) reset.classList.toggle("hidden", !s.applied.search && !s.applied.filter && !s.applied.token);
    var chip = document.getElementById(s.id + "-token");
    if (!chip) return;
    var id = s.applied.token;
    chip.classList.toggle("hidden", !id);
    if (!id) return;
    var draw = function () {
        if (s.applied.token !== id) return;
        var tok = (dashTokenStore.rows || []).filter(function (r) { return r.id === id; })[0];
        chip.innerHTML = '<span class="ui-picker-name">' + esc(t("logs.token")) + '</span><span>' + (tok ? uiUserText(tok.name) : esc(t("logs.deleted-token"))) + "</span>" +
            '<span aria-hidden="true">' + esc(t("logs.text")) + '</span>';
    };
    draw();
    dashTokensLoad().then(draw);
}

function dashLogShow(s) {
    dashLogBind(s);
    var q = uiPageQuery();
    var applied = { search: q.get("q") || "", filter: q.get(s.queryKey) || "", token: s.tokenParam ? q.get("token") || "" : "" };
    var sel = document.getElementById(s.id + "-filter");
    var known = Array.prototype.some.call(sel.options, function (o) { return o.value === applied.filter; });
    if (!known) applied.filter = "";
    document.getElementById(s.id + "-search").value = applied.search;
    sel.value = applied.filter;
    uiPickerSync(sel);
    var same = s.rows !== null && applied.search === s.applied.search && applied.filter === s.applied.filter && applied.token === s.applied.token;
    s.applied = applied;
    dashLogFilterMarks(s);
    s.drawn = "";
    if (same) {
        dashLogDraw(s, false);
        return dashLogRefresh(s).then(function () { uiRefreshLoaded(s.id); });
    }
    s.rows = null;
    s.rowsFor = null;
    s.failed = "";
    s.refreshFailed = "";
    s.capped = false;
    dashLogDraw(s, false);
    return dashLogFirst(s, false).then(function () { uiRefreshLoaded(s.id); });
}

DASH_PAGE_IMPL.activity = function () { dashLogShow(dashLogs.activity); };
DASH_PAGE_IMPL.access = function () { dashLogShow(dashLogs.access); dashLimitLoad(); };

registerPageRefresh("activity", 30000, function (isCurrent) { return dashLogRefresh(dashLogs.activity, isCurrent); });
registerPageRefresh("access", 30000, function (isCurrent) {
    return Promise.all([dashLogRefresh(dashLogs.access, isCurrent), dashLimitLoad(isCurrent)]);
});

// ===========================================================================
// Rate limit card on Access Logs: requests the limit rejected are not in the
// log, so the card says how many there were and from which addresses
// ===========================================================================

var dashLimit = { data: null, at: 0, failed: "", seq: 0, drawn: "" };

function dashLimitLoad(isCurrent) {
    var seq = ++dashLimit.seq;
    return api("GET", "/admin/api/rate-limit").then(function (res) {
        // A newer load or a newer refresh cycle owns the card
        if (seq !== dashLimit.seq || (isCurrent && !isCurrent())) return;
        var d = res.data;
        if (res.ok && d && typeof d.rejectedTotal === "number" && typeof d.limit === "number") {
            dashLimit.data = d;
            dashLimit.at = Date.now();
            dashLimit.failed = "";
        } else {
            dashLimit.failed = apiError(res, t("access.ratelimit.the-server-did-not-answer"));
        }
        dashLimitDraw();
    });
}

function dashLimitHtml() {
    var d = dashLimit.data;
    var stale = dashLimit.failed ? esc(t("access.ratelimit.could-not-refresh-these-counts", { reason: dashLimit.failed })) : "";
    if (!d) return dashLimitAlert(" is-fail", t("access.ratelimit.could-not-load-the-rate-limit"), [stale]);
    if (stale) stale = tHtml("access.ratelimit.the-counts-below-are-from", { reason: dashLimit.failed, time: tRaw(uiTime(new Date(dashLimit.at).toISOString())) });
    var seconds = d.windowMs / 1000;
    var rule = esc(t("access.ratelimit.limit", { requests: tn("access.ratelimit.count-requests", d.limit),
        window: seconds === 60 ? t("access.ratelimit.minute") : tn("access.ratelimit.count-seconds", seconds) }));
    var since = tHtml("access.ratelimit.counted-by-this-server-process-since", { time: tRaw(uiTime(d.since)) });
    var lines = stale ? [stale] : [];
    if (!d.rejectedTotal) {
        return dashLimitAlert(stale ? " is-fail" : "", t("access.ratelimit.no-requests-were-rejected-by-the"), lines.concat([rule, since]));
    }
    var tops = (d.topAddresses || []).map(function (a) {
        return uiCopyable(a.address) + " (" + esc(uiNum(a.rejected)) + ")";
    }).join(", ");
    lines.push(
        tHtml("access.ratelimit.they-were-answered-with", {
            status: tRaw("<code>" + esc(t("access.ratelimit.429-too-many-requests")) + "</code>"),
            header: tRaw("<code>" + esc(t("access.ratelimit.retry-after")) + "</code>"),
            time: tRaw(uiTime(d.lastRejectedAt)),
        }),
        d.untrackedRejected
            ? tHtml("access.ratelimit.most-rejected-among-the-first", { max: uiNum(d.maxTrackedAddresses), addresses: tRaw(tops),
                more: tn("access.ratelimit.count-more-rejections", d.untrackedRejected) })
            : tHtml("access.ratelimit.most-rejected", { addresses: tRaw(tops) }),
        rule,
        since
    );
    var title = tn("access.ratelimit.count-requests-were", d.rejectedTotal);
    return dashLimitAlert(stale ? " is-fail" : " is-warn", title, lines);
}

// `lines` are HTML: every value in them is escaped where it is put in
function dashLimitAlert(tone, title, lines) {
    return '<div class="ui-alert' + tone + '"><span class="ui-alert-bar" aria-hidden="true"></span><div><p>' + esc(title) + "</p>" +
        lines.map(function (l) { return "<small>" + l + "</small>"; }).join("") + "</div><span></span></div>";
}

function dashLimitDraw() {
    var box = document.getElementById("access-limit");
    if (!box || (!dashLimit.data && !dashLimit.failed)) return;
    var html = dashLimitHtml();
    box.classList.remove("hidden");
    if (html === dashLimit.drawn) return;
    dashLimit.drawn = html;
    box.innerHTML = html;
}

// ===========================================================================
// Usage: calls per token from the access log, and the calls over time
// (services/app/routers/admin_usage.py). ?range= picks the period; ?token= opens
// one token (its id, or "unknown" for calls with a token the service did not know).
// ===========================================================================

var USAGE_RANGES = { "24h": "usage.the-last-24-hours", "7d": "usage.the-last-7-days", "30d": "usage.the-last-30-days" };
var USAGE_STATUS = {
    active: { label: "usage.active", tone: "ok" },
    disabled: { label: "usage.disabled", tone: "" },
    expired: { label: "usage.expired", tone: "warn" },
    deleted: { label: "usage.deleted", tone: "" },
};
var DASH_USE_COLS = [
    { key: "name", label: "usage.token", kind: "text" },
    { key: "calls", label: "usage.calls", kind: "number" },
    { key: "denied", label: "usage.denied", kind: "number" },
    { key: "ipCount", label: "usage.addresses", kind: "number" },
    { key: "lastCallAt", label: "usage.last-call", kind: "date" },
];
// The token list arrives busiest first: Calls, high to low (TB-1, TB-28)
var DASH_USE_DEFAULT = { key: "calls", dir: "desc" };
var USAGE_NOTE = "usage.note";

// data: the last answer; shown: the answer the page now draws (a refresh skipped while the
// user works in the page leaves the older one on screen, and the chart reads that one)
var dashUse = { range: "7d", token: "", data: null, dataFor: "", notFound: false, failed: "", seq: 0, at: 0,
    sort: { key: "", dir: "" }, sel: -1, drawn: "", shown: null, bound: false };
// The note under the usage tables (HTML)
function dashUseNote() {
    return tHtml(USAGE_NOTE, { path: tRaw("<code>" + esc(t("usage.validate-grants")) + "</code>") });
}

var USAGE_TOKEN_ID = /^(?:unknown|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/;

function dashUseKey() {
    return dashUse.token + "|" + dashUse.range;
}

function dashUseReadQuery() {
    var q = uiPageQuery();
    var r = q.get("range") || "7d";
    dashUse.range = USAGE_RANGES[r] ? r : "7d";
    dashUse.token = q.get("token") || "";
}

function dashUseQuery(token, range) {
    var p = new URLSearchParams();
    if (token) p.set("token", token);
    if (range !== "7d") p.set("range", range);
    return p.toString();
}

function dashUseLoad(isCurrent) {
    var key = dashUseKey();
    var seq = ++dashUse.seq;
    // Only a token id or "unknown" goes into the path: "." or ".." would name another address
    if (dashUse.token && !USAGE_TOKEN_ID.test(dashUse.token)) {
        dashUse.data = null;
        dashUse.dataFor = key;
        dashUse.notFound = true;
        dashUse.failed = "";
        dashUseDraw(false);
        return Promise.resolve();
    }
    var path = "/admin/api/usage/tokens" + (dashUse.token ? "/" + encodeURIComponent(dashUse.token) : "");
    return api("GET", path + "?range=" + encodeURIComponent(dashUse.range)).then(function (res) {
        // A newer load, a newer refresh cycle or another period owns the page
        if (seq !== dashUse.seq || (isCurrent && !isCurrent()) || key !== dashUseKey()) return;
        if (res.ok && res.data && Array.isArray(res.data.series)) {
            dashUse.data = res.data;
            dashUse.dataFor = key;
            dashUse.at = Date.now();
            dashUse.notFound = false;
            dashUse.failed = "";
        } else if (res.status === 404 && dashUse.token) {
            dashUse.data = null;
            dashUse.dataFor = key;
            dashUse.notFound = true;
            dashUse.failed = "";
        } else {
            dashUse.failed = apiError(res, t("usage.the-server-did-not-answer"));
        }
        dashUseDraw(!!isCurrent);
    });
}

// A round top for the value axis: 1, 2 or 5 times a power of ten, even so its half is whole
function dashUseNiceMax(v) {
    if (!(v > 0)) return 2;
    var p = Math.pow(10, Math.floor(Math.log10(v)));
    var m = v / p;
    var top = (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
    if (top < 2) top = 2;
    if (top % 2) top += 1;
    return top;
}

function dashUsePct(part, whole) {
    if (!whole) return "0%";
    var p = (part / whole) * 100;
    if (p > 0 && p < 1) return t("usage.under-1");
    return Math.round(p) + "%";
}

// The report counts hours and days in the time zone the server computed it in, and names
// them in that zone too, so a zone saved in another tab cannot shift the labels
function dashUseZone(d) {
    return d && uiZoneValid(d.timeZone) ? d.timeZone : uiAppZone;
}

function dashUseBucketName(d, b) {
    var z = dashUseZone(d);
    return d.range === "24h" ? formatTime(b.start, { seconds: false, zone: z }) : formatDay(b.start, { zone: z });
}

// The data starts at the purge boundary: an earlier, shorter retention already deleted older calls
function dashUsePurged(d) {
    return d.dataFromReason === "purge";
}

// One bucket in words: the chart's readout, each bar's name and the table behind the chart
function dashUseBucketWords(d, b) {
    if (b.coverage === "none") {
        return dashUsePurged(d) ? t("usage.no-data-these-access-logs-were")
            : t("usage.no-data-access-logs-older-than", { days: tn("usage.count-days", d.retentionDays) });
    }
    var counts = { calls: tn("usage.count-calls", b.allowed + b.denied), allowed: uiNum(b.allowed), denied: uiNum(b.denied) };
    if (b.coverage !== "partial") return t("usage.bucket", counts);
    counts.time = formatTime(d.dataFrom, { seconds: false, zone: dashUseZone(d) });
    return t("usage.bucket-partial", counts);
}

// Allowed and denied calls per hour or day: bars from the series, a readout line that
// follows the pointer and the keyboard, and the same numbers as a table for screen readers
function dashUseChartHtml(d) {
    var series = d.series;
    var n = series.length;
    var max = 0;
    series.forEach(function (b) { max = Math.max(max, b.allowed + b.denied); });
    var top = dashUseNiceMax(max);
    var hourly = d.range === "24h";
    var zone = { zone: dashUseZone(d) };
    var every = hourly ? 3 : n > 10 ? 5 : 1;
    var gaps = false;
    var cols = series.map(function (b, i) {
        var cls = b.coverage === "none" ? " is-none" : b.coverage === "partial" ? " is-partial" : "";
        if (cls) gaps = true;
        var seg = function (v, tone) {
            return v ? '<span class="ui-chart-seg is-' + tone + '" style="height:' + ((v / top) * 100).toFixed(2) + '%"></span>' : "";
        };
        return '<button type="button" class="ui-chart-col' + cls + '" data-chart-i="' + i + '" data-action="chart-' + i + '" tabindex="-1" aria-label="' +
            esc(dashUseBucketName(d, b) + ": " + dashUseBucketWords(d, b)) + '">' + seg(b.denied, "fail") + seg(b.allowed, "base") + "</button>";
    }).join("");
    var ticks = series.map(function (b, i) {
        // Count the labels back from the newest bucket, so the last one is always named
        var back = n - 1 - i;
        if (back % every) return "";
        // On a phone every other hour label goes, so the rest do not touch
        var minor = hourly && (back / every) % 2 ? ' class="is-minor"' : "";
        return "<span" + minor + ' style="grid-column:' + (i + 1) + '">' + esc(hourly ? formatClock(b.start, zone) : formatShortDay(b.start, zone)) + "</span>";
    }).join("");
    var rows = series.map(function (b) {
        return "<tr><th scope=\"row\">" + esc(dashUseBucketName(d, b)) + "</th><td>" +
            (b.coverage === "none" ? esc(t("usage.no-data")) : esc(uiNum(b.allowed))) + "</td><td>" + (b.coverage === "none" ? esc(t("usage.no-data")) : esc(uiNum(b.denied))) + "</td></tr>";
    }).join("");
    return '<div class="ui-chart">' +
        '<p class="ui-chart-read" id="usage-read" aria-live="polite"></p>' +
        '<div class="ui-chart-plot">' +
        '<div class="ui-chart-y" aria-hidden="true"><span style="top:0%">' + esc(uiNum(top)) + '</span><span style="top:50%">' + esc(uiNum(top / 2)) +
        '</span><span style="top:100%">0</span></div>' +
        '<div class="ui-chart-bars" role="group" aria-label="' + esc(t("usage.chart-label", { title: hourly ? t("usage.calls-per-hour") : t("usage.calls-per-day") })) + '" style="--ui-chart-n:' + n + '">' + cols + "</div>" +
        '<div class="ui-chart-x" aria-hidden="true" style="--ui-chart-n:' + n + '">' + ticks + "</div></div>" +
        '<p class="ui-chart-key"><span><i class="is-base"></i>' + esc(t("usage.allowed-2")) + '</span><span><i class="is-fail"></i>' + esc(t("usage.denied")) + '</span>' +
        (gaps ? '<span><i class="is-none"></i>' + esc(t("usage.logs-deleted")) + '</span>' : "") + "</p>" +
        '<table class="ui-sr"><caption>' + (hourly ? esc(t("usage.calls-per-hour")) : esc(t("usage.calls-per-day"))) + "</caption><thead><tr><th scope=\"col\">" +
        (hourly ? esc(t("usage.hour")) : esc(t("usage.day"))) + '</th><th scope="col">' + esc(t("usage.allowed-2")) + '</th><th scope="col">' + esc(t("usage.denied")) + '</th></tr></thead><tbody>' + rows + "</tbody></table></div>";
}

// Show one bar's numbers; the newest bar when none was picked
function dashUseSelect(i, focus) {
    var body = document.getElementById("usage-body");
    var d = dashUse.shown;
    if (!body || !d) return;
    var cols = body.querySelectorAll("[data-chart-i]");
    if (!cols.length) return;
    if (i < 0 || i >= cols.length) i = cols.length - 1;
    dashUse.sel = i;
    Array.prototype.forEach.call(cols, function (c, k) {
        c.classList.toggle("is-on", k === i);
        c.setAttribute("tabindex", k === i ? "0" : "-1");
    });
    var b = d.series[i];
    var read = document.getElementById("usage-read");
    if (read && b) read.innerHTML = "<b>" + esc(dashUseBucketName(d, b)) + "</b> " + esc(dashUseBucketWords(d, b));
    if (focus) cols[i].focus({ preventScroll: true });
}

function dashUseKpi(label, value, sub) {
    return '<div class="ui-kpi"><small>' + esc(label) + "</small><b>" + value + "</b>" + (sub ? "<span>" + sub + "</span>" : "") + "</div>";
}

function dashUseNotices(d) {
    var out = "";
    var zone = dashUseZone(d);
    if (d.partial) {
        var from = uiTime(d.dataFrom, formatTime(d.dataFrom, { seconds: false, zone: zone }), zone);
        out += '<div class="ui-banner ui-banner-warn" role="status"><div><b>' + esc(t("usage.part-of-this-period-is-no")) + '</b>' +
            (dashUsePurged(d) ? tHtml("usage.older-calls-were-deleted-under-an", { time: tRaw(from) })
                : tHtml("usage.access-logs-are-kept-for", { days: tn("usage.count-days", d.retentionDays), time: tRaw(from) })) + "</div></div>";
    }
    if (d.timeZone && !uiZoneValid(d.timeZone)) {
        out += '<div class="ui-banner ui-banner-warn" role="status"><div><b>' + esc(t("usage.this-browser-cannot-show-this-report")) + "</b>" +
            tHtml("usage.the-hours-and-days-are-counted", { zone: tRaw("<code>" + esc(d.timeZone) + "</code>"), shown: tRaw("<code>" + esc(zone) + "</code>") }) + "</div></div>";
    } else if (zone !== uiAppZone) {
        out += '<div class="ui-banner" role="status"><div><b>' + esc(t("usage.this-report-uses-another-time-zone")) + '</b>' + tHtml("usage.its-hours-and-days-are-counted", { zone: tRaw("<code>" + esc(zone) + "</code>") }) + "</div></div>";
    }
    if (d.areasIncomplete) {
        out += '<div class="ui-banner ui-banner-warn" role="status"><div><b>' + esc(t("usage.area-figures-are-incomplete-for-this")) + '</b>' +
            esc(t("usage.the-calls-name-more-different-zones")) + "</div></div>";
    }
    return out;
}

function dashUseChartPanel(d) {
    var totals = d.totals;
    return '<div class="ui-panel"><div class="ui-panel-head">' + (d.range === "24h" ? esc(t("usage.calls-per-hour")) : esc(t("usage.calls-per-day"))) +
        ' <span class="ui-count">' + esc(tn("usage.count-calls", totals.calls)) + "</span></div>" + dashUseChartHtml(d) + "</div>";
}

function dashUseStatusTag(status) {
    if (status === "unknown") return "";
    var st = USAGE_STATUS[status] || USAGE_STATUS.active;
    return dashTag(t(st.label), st.tone);
}

function dashUseAreaTags(list) {
    if (list === null) return '<span class="ui-muted">-</span>';
    if (!list.length) return '<span class="ui-muted">' + esc(t("usage.no-calls")) + '</span>';
    return '<div class="ui-tags">' + list.map(function (a) {
        return '<span class="ui-tag ui-tag-line">' + uiUserText(a.area + (a.truncated ? "…" : "")) + " " + esc(uiNum(a.calls)) + "</span>";
    }).join("") + "</div>";
}

function dashUseSortValue(tok, key) {
    if (key === "name") return String(tok.name || "");
    if (key === "lastCallAt") return uiParseInstant(tok.lastCallAt);
    return tok[key];
}

function dashUseOverviewHtml(d) {
    var totals = d.totals;
    var html = dashUseNotices(d) + '<div class="ui-kpis">' +
        dashUseKpi(t("usage.calls"), esc(uiNum(totals.calls)), esc(t("usage.in", { range: t(USAGE_RANGES[d.range]) }))) +
        dashUseKpi(t("usage.denied"), esc(uiNum(totals.denied)), esc(t("usage.of-calls", { pct: dashUsePct(totals.denied, totals.calls) }))) +
        dashUseKpi(t("usage.tokens-with-calls"), esc(uiNum(totals.tokensUsed)) + ' ' + '<span class="ui-muted">' + esc(t("usage.of", { total: uiNum(totals.tokensTotal) })) + "</span>", esc(t("usage.tokens-that-called-in-this-period"))) +
        dashUseKpi(t("usage.unknown-token"), esc(uiNum(totals.unknownCalls)), esc(t("usage.calls-with-a-token-the-service"))) +
        "</div>" + dashUseChartPanel(d);
    // Busiest first; the header buttons and the Sort order re-sort from here
    var base = d.tokens.slice().sort(function (a, b) {
        return b.calls - a.calls || b.grantsChecks - a.grantsChecks || String(a.name).localeCompare(String(b.name), undefined, { sensitivity: "base" });
    });
    var rows = uiSortRows(base, dashUse.sort, dashUseSortValue);
    var head = DASH_USE_COLS.slice(0, 3).map(function (c) { return dashSortHead(dashUse.sort, c, DASH_USE_DEFAULT); }).join("") +
        '<div class="ui-td" role="columnheader">' + esc(t("usage.top-zones")) + '</div>' +
        DASH_USE_COLS.slice(3).map(function (c) { return dashSortHead(dashUse.sort, c, DASH_USE_DEFAULT); }).join("");
    var body = rows.length ? rows.map(function (r) {
        var key = r.tokenId || "unknown";
        return '<div class="ui-tr' + (r.calls ? "" : " is-off") + '" role="row" tabindex="0" data-key="' + esc(key) + '" aria-label="' + esc(r.name) + '">' +
            '<div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + uiUserText(r.name) + "</span>" +
            (r.status !== "active" ? " " + dashUseStatusTag(r.status) : "") +
            (r.grantsChecks ? '<span class="ui-td-sub">' + esc(tn("usage.count-grants-checks", r.grantsChecks)) + "</span>" : "") + "</div>" +
            '<div class="ui-td ui-num" role="cell"><span class="ui-unit">' + esc(t("usage.calls-2")) + ' </span>' + esc(uiNum(r.calls)) + "</div>" +
            '<div class="ui-td ui-num" role="cell"><span class="ui-unit">' + esc(t("usage.denied-3")) + ' </span><span' + (r.denied ? ' class="ui-text-fail"' : "") + ">" + esc(uiNum(r.denied)) + "</span>" +
            (r.calls ? '<span class="ui-td-sub">' + esc(dashUsePct(r.denied, r.calls)) + "</span>" : "") + "</div>" +
            '<div class="ui-td ui-td-full ui-td-wrap" role="cell">' + (r.calls ? dashUseAreaTags(r.topAreas) : '<span class="ui-muted">' + esc(t("usage.no-calls-in-this-period")) + '</span>') + "</div>" +
            '<div class="ui-td ui-num" role="cell"><span class="ui-unit">' + esc(t("usage.addresses-2")) + ' </span>' + esc(uiNum(r.ipCount)) + "</div>" +
            '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("usage.last-call-2")) + ' </span>' + (r.lastCallAt ? uiTime(r.lastCallAt) : '<span class="ui-muted">-</span>') + "</div></div>";
    }).join("") : dashEmpty(t("usage.no-tokens-yet"), esc(t("usage.create-a-token-on-the-tokens")));
    html += '<div class="ui-panel"><div class="ui-panel-head">' + esc(t("usage.tokens")) + ' <span class="ui-count">' + esc(tn("usage.count-rows", rows.length)) + "</span>" +
        dashUseSortButton() + "</div>" +
        '<div class="ui-table ui-stack" id="usage-table" role="table" aria-label="' + esc(t("usage.calls-per-token")) + '">' +
        '<div class="ui-tr ui-tr-head" role="row" id="usage-head">' + head + "</div>" +
        '<div class="ui-table-body" role="rowgroup">' + body + "</div></div></div>" +
        '<p class="ui-usage-note">' + dashUseNote() + (totals.grantsChecks ? " " + esc(t("usage.this-period-has", { checks: tn("usage.count-grants-checks", totals.grantsChecks) })) : "") + "</p>";
    return html;
}

// The token list's Sort button sits at the end of its panel head (TB-28)
function dashUseSortButton() {
    var label = dashSortLabel(dashUse.sort, DASH_USE_COLS, DASH_USE_DEFAULT);
    return '<button type="button" class="ui-icon-btn ui-sort-menu-btn' + (dashUse.sort.key ? " is-set" : "") + '" id="usage-sort" data-action="usage-sort" aria-haspopup="dialog" aria-expanded="false" data-sort-name="' +
        esc(t("list.name-tokens")) + '" aria-label="' + esc(label) + '" title="' + esc(label) + '">' + UI_SORT_ICON + "</button>";
}

function dashUseBackLink() {
    var q = dashUseQuery("", dashUse.range);
    return '<a data-go="usage" data-query="' + esc(q) + '" href="' + esc(basePath() + PAGES.usage.path + (q ? "?" + q : "")) + '">' + esc(t("usage.all-tokens")) + '</a>';
}

function dashUseLevels(levels) {
    return '<div class="ui-tags">' + Object.keys(levels).map(function (lv) {
        var v = levels[lv];
        return (v.allowed ? '<span class="ui-tag ui-tag-line">' + esc(t("usage.level-calls", { level: uiIdWord("logs.level.", lv), n: uiNum(v.allowed) })) + "</span>" : "") +
            (v.denied ? '<span class="ui-tag ui-tag-fail">' + esc(t("usage.level-denied", { level: uiIdWord("logs.level.", lv), n: uiNum(v.denied) })) + "</span>" : "");
    }).join("") + "</div>";
}

function dashUseTokenHtml(d) {
    var totals = d.totals;
    var tok = d.token;
    var logs = tok.id ? '<a class="ui-btn ui-btn-sm" data-go="access" data-query="token=' + esc(encodeURIComponent(tok.id)) + '" href="' +
        esc(basePath() + PAGES.access.path + "?token=" + encodeURIComponent(tok.id)) + '">' + esc(t("usage.open-in-access-logs")) + '</a>' : "";
    var html = '<div class="ui-usage-title">' + dashUseBackLink() + "<h2>" + uiUserText(tok.name) + " " + dashUseStatusTag(tok.status) + "</h2>" + logs + "</div>" +
        dashUseNotices(d) + '<div class="ui-kpis">' +
        dashUseKpi(t("usage.calls"), esc(uiNum(totals.calls)), esc(t("usage.in", { range: t(USAGE_RANGES[d.range]) }))) +
        dashUseKpi(t("usage.denied"), esc(uiNum(totals.denied)), esc(t("usage.of-calls", { pct: dashUsePct(totals.denied, totals.calls) }))) +
        dashUseKpi(t("usage.zones"), d.areasCount === null ? "-" : esc(uiNum(d.areasCount)), esc(t("usage.called-at-least-once"))) +
        dashUseKpi(t("usage.addresses"), esc(uiNum(totals.ipCount)), esc(t("usage.the-calls-came-from"))) +
        dashUseKpi(t("usage.last-call"), totals.lastCallAt ? uiTime(totals.lastCallAt) : '<span class="ui-muted">-</span>', totals.grantsChecks ? esc(tn("usage.count-grants-checks", totals.grantsChecks)) : "") +
        "</div>" + dashUseChartPanel(d);

    var areas = d.areas;
    var areasBody;
    if (areas === null) areasBody = dashEmpty(t("usage.area-figures-are-incomplete-for-this-2"), esc(t("usage.call-counts-above-are-complete")));
    else if (!areas.length) areasBody = dashEmpty(t("usage.no-calls-in-this-period-2"), esc(t("usage.no-calls-line")));
    else {
        var most = areas[0].calls;
        areasBody = '<div class="ui-table ui-stack" id="usage-areas" role="table" data-nosort aria-label="' + esc(t("usage.calls-per-zone")) + '">' +
            '<div class="ui-tr ui-tr-head" role="row"><div class="ui-td" role="columnheader">' + esc(t("usage.zone")) + '</div><div class="ui-td" role="columnheader">' + esc(t("usage.calls")) + '</div>' +
            '<div class="ui-td" role="columnheader">' + esc(t("usage.allowed-and-denied")) + '</div><div class="ui-td" role="columnheader">' + esc(t("usage.levels")) + '</div></div>' +
            '<div class="ui-table-body" role="rowgroup">' + areas.map(function (a) {
                var width = Math.max(2, (a.calls / most) * 100).toFixed(1);
                return '<div class="ui-tr" role="row"><div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + uiUserText(a.area + (a.truncated ? "…" : "")) + "</span></div>" +
                    '<div class="ui-td ui-num" role="cell"><span class="ui-unit">' + esc(t("usage.calls-2")) + ' </span>' + esc(uiNum(a.calls)) +
                    (a.denied ? '<span class="ui-td-sub"><span class="ui-text-fail">' + esc(t("usage.denied-2", { n: uiNum(a.denied) })) + "</span></span>" : "") + "</div>" +
                    '<div class="ui-td ui-td-full" role="cell"><div class="ui-bar" style="width:' + width + '%" aria-hidden="true">' +
                    (a.allowed ? '<i class="is-base" style="flex:' + a.allowed + '"></i>' : "") + (a.denied ? '<i class="is-fail" style="flex:' + a.denied + '"></i>' : "") +
                    '</div><span class="ui-sr">' + esc(t("usage.allowed", { allowed: uiNum(a.allowed), denied: uiNum(a.denied) })) + "</span></div>" +
                    '<div class="ui-td ui-td-full ui-td-wrap" role="cell">' + dashUseLevels(a.levels) + "</div></div>";
            }).join("") + "</div></div>";
    }
    var shownAreas = areas ? areas.length : 0;
    html += '<div class="ui-panel"><div class="ui-panel-head">' + esc(t("usage.zones")) +
        (d.areasCount !== null ? ' <span class="ui-count">' + esc(d.areasCount > shownAreas ? t("usage.busiest-of", { shown: uiNum(shownAreas), total: uiNum(d.areasCount) }) : tn("usage.count-zones", d.areasCount)) + "</span>" : "") +
        "</div>" + areasBody + "</div>";

    var addrs = d.addresses;
    var addrBody = !addrs.length ? dashEmpty(t("usage.no-calls-in-this-period-2"), esc(t("usage.no-calls-line"))) :
        '<div class="ui-table ui-stack" id="usage-addrs" role="table" data-nosort aria-label="' + esc(t("usage.calls-per-address")) + '">' +
        '<div class="ui-tr ui-tr-head" role="row"><div class="ui-td" role="columnheader">' + esc(t("usage.address")) + '</div><div class="ui-td" role="columnheader">' + esc(t("usage.calls")) + '</div>' +
        '<div class="ui-td" role="columnheader">' + esc(t("usage.denied")) + '</div><div class="ui-td" role="columnheader">' + esc(t("usage.last-call")) + '</div></div>' +
        '<div class="ui-table-body" role="rowgroup">' + addrs.map(function (a) {
            return '<div class="ui-tr" role="row"><div class="ui-td ui-td-main" role="cell">' + (a.truncated ? uiUserText(a.address + "…", "ui-mono") : uiCopyable(a.address)) + "</div>" +
                '<div class="ui-td ui-num" role="cell"><span class="ui-unit">' + esc(t("usage.calls-2")) + ' </span>' + esc(uiNum(a.calls)) + "</div>" +
                '<div class="ui-td ui-num" role="cell"><span class="ui-unit">' + esc(t("usage.denied-3")) + ' </span><span' + (a.denied ? ' class="ui-text-fail"' : "") + ">" + esc(uiNum(a.denied)) + "</span></div>" +
                '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("usage.last-call-2")) + ' </span>' + uiTime(a.lastCallAt) + "</div></div>";
        }).join("") + "</div></div>";
    html += '<div class="ui-panel"><div class="ui-panel-head">' + esc(t("usage.addresses-2")) + ' <span class="ui-count">' +
        esc(totals.ipCount > addrs.length ? t("usage.busiest-of", { shown: uiNum(addrs.length), total: uiNum(totals.ipCount) }) : tn("usage.count-addresses", totals.ipCount)) +
        "</span></div>" + addrBody + "</div>";

    html += '<p class="ui-usage-note">' + dashUseNote() + "</p>";
    return html;
}

function dashUseNotFoundHtml() {
    return '<div class="ui-usage-title">' + dashUseBackLink() + "</div>" +
        '<div class="ui-panel">' + dashEmpty(t("usage.no-usage-to-show-for-this"), esc(t("usage.it-is-not-a-token-any"))) + "</div>";
}

function dashUseSyncRange() {
    document.querySelectorAll("#usage-range [data-range]").forEach(function (b) {
        b.setAttribute("aria-pressed", b.getAttribute("data-range") === dashUse.range ? "true" : "false");
    });
}

// A background refresh (quiet) leaves the page alone while the user works in it
function dashUseDraw(quiet) {
    var body = document.getElementById("usage-body");
    if (!body) return;
    dashUseSyncRange();
    var mine = dashUse.dataFor === dashUseKey();
    // A failed refresh keeps the figures shown and says so above them (LD-12)
    dashFreshSet("usage-fresh", dashUse.at, mine && dashUse.data ? dashUse.failed : "");
    var html;
    if (mine && dashUse.notFound) html = dashUseNotFoundHtml();
    else if (mine && dashUse.data) html = dashUse.token ? dashUseTokenHtml(dashUse.data) : dashUseOverviewHtml(dashUse.data);
    else html = '<div class="ui-panel">' + (dashUse.failed ? dashFailed(t("usage.could-not-load-the-usage"), dashUse.failed, "data-usage-retry") : dashLoading(t("usage.loading-usage"))) + "</div>";
    if (quiet && uiAreaHeld(body)) return;
    dashUse.shown = mine ? dashUse.data : null;
    if (html !== dashUse.drawn) {
        dashUse.drawn = html;
        uiKeepFocus(body, function () { body.innerHTML = html; });
    }
    if (dashUse.shown) dashUseSelect(dashUse.sel, false);
}

function dashUseOpen(key) {
    uiGo("usage", dashUseQuery(key, dashUse.range));
}

function dashUseBind() {
    if (dashUse.bound) return;
    dashUse.bound = true;
    document.getElementById("usage-range").addEventListener("click", function (event) {
        var b = event.target.closest("[data-range]");
        if (!b || b.getAttribute("data-range") === dashUse.range) return;
        dashUse.range = b.getAttribute("data-range");
        dashUse.sel = -1;
        dashUse.failed = "";
        uiSetPageQuery(new URLSearchParams(dashUseQuery(dashUse.token, dashUse.range)));
        dashUseDraw(false);
        dashUseLoad().then(function () { uiRefreshLoaded("usage"); });
    });
    dashFreshBind("usage-fresh", function () { dashUseLoad(); });
    var body = document.getElementById("usage-body");
    body.addEventListener("click", function (event) {
        var target = event.target;
        if (target.closest("[data-usage-retry]")) {
            dashUse.failed = "";
            dashUseDraw(false);
            dashUseLoad();
            return;
        }
        var sortBtn = target.closest("#usage-sort");
        if (sortBtn) {
            uiSortMenu(sortBtn, t("list.name-tokens"), DASH_USE_COLS, dashUse.sort, function () {
                dashUseDraw(false);
                // The list was drawn again with its button; the menu stays open over it
                var again = document.getElementById("usage-sort");
                if (again) again.setAttribute("aria-expanded", "true");
            }, DASH_USE_DEFAULT);
            return;
        }
        var sort = target.closest("#usage-head [data-sort]");
        if (sort) {
            var col = DASH_USE_COLS.filter(function (c) { return c.key === sort.getAttribute("data-sort"); })[0];
            uiSortToggle(dashUse.sort, col, DASH_USE_DEFAULT);
            dashUseDraw(false);
            var again = body.querySelector('#usage-head [data-sort="' + col.key + '"]');
            if (again) again.focus({ preventScroll: true });
            return;
        }
        var col2 = target.closest("[data-chart-i]");
        if (col2) {
            dashUseSelect(Number(col2.getAttribute("data-chart-i")), false);
            return;
        }
        var row = target.closest("#usage-table [data-key]");
        if (!row || target.closest("button, a, input, select, label") || uiRowHeld(event, row)) return;
        dashUseOpen(row.getAttribute("data-key"));
    });
    body.addEventListener("keydown", function (event) {
        var target = event.target;
        if (!(target instanceof Element)) return;
        var c = target.closest("[data-chart-i]");
        if (c) {
            var i = Number(c.getAttribute("data-chart-i"));
            var last = body.querySelectorAll("[data-chart-i]").length - 1;
            var to = event.key === "ArrowRight" ? i + 1 : event.key === "ArrowLeft" ? i - 1 : event.key === "Home" ? 0 : event.key === "End" ? last : null;
            if (to === null) return;
            event.preventDefault();
            // The bars follow the reading order of the page
            if (document.documentElement.dir === "rtl" && (event.key === "ArrowRight" || event.key === "ArrowLeft")) to = 2 * i - to;
            dashUseSelect(Math.max(0, Math.min(last, to)), true);
            return;
        }
        if (target.matches("#usage-table [data-key]") && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            dashUseOpen(target.getAttribute("data-key"));
        }
    });
    // Pointing at a bar shows its numbers; the pick stays when the pointer leaves
    body.addEventListener("pointerover", function (event) {
        var c = event.target instanceof Element ? event.target.closest("[data-chart-i]") : null;
        if (c && event.pointerType !== "touch") dashUseSelect(Number(c.getAttribute("data-chart-i")), false);
    });
    body.addEventListener("focusin", function (event) {
        var c = event.target instanceof Element ? event.target.closest("[data-chart-i]") : null;
        if (c) dashUseSelect(Number(c.getAttribute("data-chart-i")), false);
    });
}

DASH_PAGE_IMPL.usage = function () {
    dashUseBind();
    var before = dashUseKey();
    dashUseReadQuery();
    if (dashUseKey() !== before) {
        dashUse.sel = -1;
        dashUse.failed = "";
    }
    dashUseDraw(false);
    dashUseLoad().then(function () { uiRefreshLoaded("usage"); });
};

registerPageRefresh("usage", 60000, function (isCurrent) { return dashUseLoad(isCurrent); });

// ===========================================================================
// Settings: three panels, each with its own saved versions and Save. General
// (time zone, log retention, the session cookie), the rate limit and the
// addresses exempt from it, and the Redis connection. Each form edits a draft
// based on one saved version (base); a refresh never touches a changed draft,
// and the base only moves on a save or an explicit choice after a conflict.
// ===========================================================================

var DASH_LOCK_ICON = '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z"/></svg>';

function dashSetPanelState() {
    return { snap: null, baseSnap: null, draft: null, dirty: false, saving: false, failed: "", refreshFailed: "",
        conflict: null, error: "", errorField: "", seq: 0 };
}

var dashSet = {
    snap: null, baseSnap: null, draft: null, dirty: false, saving: false, failed: "", refreshFailed: "",
    conflict: null, error: "", errorField: "", askOwn: false, seq: 0, bound: false,
};
// askPurge / askCookie / askRestart: the server asked to confirm the values it was sent
// ({ ..., ok }); they go as soon as one of those values changes
var dashGen = dashSetPanelState();
dashGen.askPurge = null;
dashGen.askCookie = null;
var dashRedis = dashSetPanelState();
dashRedis.askRestart = null;

// ---------------------------------------------------------------------------
// Shared by the panels
// ---------------------------------------------------------------------------

function dashSetSourceHint(field) {
    if (field.source === "stored") return t("settings.saved-here");
    if (field.source === "default") return t("settings.default-value");
    return "";
}

function dashSetLocked(field, shown) {
    return '<p class="ui-locked-note">' + DASH_LOCK_ICON + "<span>" + tHtml("settings.set-by-the", { env: tRaw("<code>" + esc(field.envName) + "</code>"), value: shown }) + "</span></p>";
}

function dashSetAlert(tone, title, sub, actions) {
    return '<div class="ui-alert' + tone + '"><span class="ui-alert-bar" aria-hidden="true"></span><div><p>' + esc(title) + "</p><small>" + sub +
        "</small></div>" + (actions || "<span></span>") + "</div>";
}

// A value in a sentence of tHtml, set as code
function dashCode(value) {
    return tRaw("<code>" + esc(value) + "</code>");
}

function dashSetSavedLine(s, none) {
    return s.version
        ? tHtml(s.savedFrom ? "settings.version-from" : "settings.version", { version: uiNum(s.version), user: tRaw(uiUserText(s.savedBy || "?")),
            address: s.savedFrom ? dashCode(s.savedFrom) : "", time: tRaw(uiTime(s.savedAt)) })
        : none;
}

// The version this server process uses, for panels that reload every 15 seconds
function dashSetAppliedAlerts(s, alerts) {
    var a = s.applied || {};
    if (a.lastReloadError) {
        alerts.push(dashSetAlert(" is-fail", t("settings.this-server-process-could-not-reload"),
            esc(t("settings.it-keeps-using-version", { reason: a.lastReloadError, version: uiNum(a.version) }))));
    } else if (a.version < s.version) {
        alerts.push(dashSetAlert(" is-warn", t("settings.this-server-process-still-uses-version", { version: uiNum(a.version) }), esc(t("settings.it-reloads-the-settings-every-15"))));
    }
    return a.lastReload ? tHtml("settings.checked", { version: uiNum(a.version || 0), time: tRaw(uiTime(a.lastReload)) })
        : esc(t("settings.this-server-process-enforces-version", { version: uiNum(a.version || 0) }));
}

function dashPanelLoad(p, isCurrent) {
    var st = p.st;
    var seq = ++st.seq;
    return p.get().then(function (res) {
        // A newer load, a save in flight, or a save that finished after this load began owns the panel
        if (seq !== st.seq || st.saving || (isCurrent && !isCurrent())) return;
        var d = res.data;
        if (!res.ok || !d || !p.valid(d)) {
            var why = apiError(res, t("settings.the-server-did-not-answer"));
            if (st.snap) st.refreshFailed = why;
            else st.failed = why;
            p.draw();
            return;
        }
        st.failed = "";
        st.refreshFailed = "";
        st.snap = d;
        if (!st.dirty && !st.saving && !st.conflict) {
            // A panel with a key keeps its form while the saved values stay the same (an open
            // picker or a focused field survives the 30-second refresh)
            var same = p.formKey && st.baseSnap && p.formKey(st.baseSnap) === p.formKey(d);
            st.baseSnap = d;
            if (!same) {
                st.draft = p.draftFrom(d);
                st.error = "";
                st.errorField = "";
                p.clearAsks(st);
                p.drawForm();
            }
        }
        p.draw();
    });
}

function dashPanelDraw(p) {
    var st = p.st;
    var info = document.getElementById(p.ids.info);
    var body = document.getElementById(p.ids.body);
    if (!info || !body) return;
    if (!st.snap) {
        info.innerHTML = "";
        body.innerHTML = st.failed ? dashFailed(t(p.failedTitle), st.failed, 'data-settings-retry="' + p.name + '"') : dashLoading(t(p.loadingText));
        return;
    }
    var alerts = [];
    if (st.conflict) {
        alerts.push(dashSetAlert(" is-warn", t("settings.someone-else-saved-these-settings-after"),
            tHtml("settings.your-changes-are-still-in-the", { saved: tRaw(p.conflictText(st.conflict)) }),
            '<div class="ui-settings-actions"><button type="button" class="ui-btn ui-btn-sm" data-set-discard="' + p.name + '">' + esc(t("settings.discard-my-changes")) + '</button>' +
            '<button type="button" class="ui-btn ui-btn-sm" data-set-keep="' + p.name + '">' + esc(t("settings.keep-my-changes")) + '</button></div>'));
    }
    if (st.refreshFailed) {
        alerts.push(dashSetAlert(" is-fail", t("settings.could-not-refresh-the-settings"), esc(t("settings.they-are-checked-again-every-30", { reason: st.refreshFailed }))));
    }
    p.alerts(st.snap, alerts);
    var html = alerts.join("");
    if (!uiTextHeld(info)) uiKeepFocus(info, function () { info.innerHTML = html; });
    if (!document.getElementById(p.ids.form)) p.drawForm();
    p.sync();
}

// A save that went through: the answer is the new base
function dashPanelSaved(p, d, message) {
    var st = p.st;
    st.snap = d;
    st.baseSnap = d;
    st.draft = p.draftFrom(d);
    st.dirty = false;
    st.conflict = null;
    p.clearAsks(st);
    p.drawForm();
    p.draw();
    showToast(d.warning || message, d.warning ? "error" : "success");
}

// Another admin saved first: the draft and its base stay until the admin chooses
function dashPanelConflict(p, current) {
    var st = p.st;
    st.conflict = current;
    st.snap = current;
    p.draw();
}

function dashPanelFail(p, message, field) {
    p.st.error = message;
    p.st.errorField = field;
    p.sync();
}

function dashPanelDiscard(p) {
    var st = p.st;
    var to = st.conflict || st.snap;
    st.baseSnap = to;
    st.snap = to;
    st.draft = p.draftFrom(to);
    st.dirty = false;
    st.conflict = null;
    p.clearAsks(st);
    st.error = "";
    st.errorField = "";
    p.drawForm();
    p.draw();
}

// An explicit choice: the draft now changes the version saved by the other admin. In panels
// whose confirmations are bound to values (asksBound), the confirmations go too
function dashPanelKeep(p) {
    var st = p.st;
    st.baseSnap = st.conflict;
    st.conflict = null;
    if (p.asksBound) p.clearAsks(st);
    p.input();
    p.draw();
}

// The error line, the invalid field, the busy state and Discard follow the state
function dashPanelSyncCommon(p, pairs) {
    var st = p.st;
    var err = document.getElementById(p.ids.err);
    err.textContent = st.error;
    err.classList.toggle("hidden", !st.error);
    pairs.forEach(function (pair) {
        var el = document.getElementById(pair[0]);
        if (!el) return;
        if (st.errorField === pair[1]) el.setAttribute("aria-invalid", "true");
        else el.removeAttribute("aria-invalid");
    });
    document.getElementById(p.ids.fields).disabled = st.saving;
    var save = document.getElementById(p.ids.save);
    if (save) save.textContent = st.saving ? t("settings.saving") : t("settings.save");
    var discard = document.getElementById(p.ids.discard);
    if (discard) discard.classList.toggle("hidden", !st.dirty);
}

// A confirmation box inside a form: the server's words and a checkbox, drawn again only when it changes
function dashSetAskHtml(id, title, sub, label, ok) {
    return '<div class="ui-alert is-warn"><span class="ui-alert-bar" aria-hidden="true"></span><div><p>' + esc(title) + "</p><small>" + sub + "</small>" +
        '<label class="ui-check-label"><input type="checkbox" class="ui-check" id="' + id + '"' + (ok ? " checked" : "") + " /> <span>" + esc(label) +
        "</span></label></div><span></span></div>";
}

function dashSetAsksDraw(id, html) {
    var box = document.getElementById(id);
    if (!box || box.getAttribute("data-drawn") === html) return;
    box.setAttribute("data-drawn", html);
    uiKeepFocus(box, function () { box.innerHTML = html; });
}

// ---------------------------------------------------------------------------
// Rate limit
// ---------------------------------------------------------------------------

function dashSetDraftFrom(s) {
    var f = s.fields;
    return {
        maxReq: String(f.maxReq.value),
        windowSeconds: String(f.windowMs.value / 1000),
        exempt: f.exempt.value.join("\n"),
    };
}

function dashSetExemptList(text) {
    return text.split(/\r?\n/).map(function (l) { return l.trim(); }).filter(function (l) { return l; });
}

function dashSetChanges() {
    var base = dashSetDraftFrom(dashSet.baseSnap);
    var d = dashSet.draft;
    var f = dashSet.baseSnap.fields;
    var out = {};
    if (!f.maxReq.locked && d.maxReq.trim() !== base.maxReq) out.maxReq = d.maxReq.trim();
    if (!f.windowMs.locked && d.windowSeconds.trim() !== base.windowSeconds) out.windowSeconds = d.windowSeconds.trim();
    if (dashSetExemptList(d.exempt).join("\n") !== dashSetExemptList(base.exempt).join("\n")) out.exempt = dashSetExemptList(d.exempt);
    return out;
}

function dashSettingsLoad(isCurrent) {
    return dashPanelLoad(DASH_SET_PANELS.rate, isCurrent);
}

function dashSettingsLoadAll(isCurrent) {
    return Promise.all([dashGenLoad(isCurrent), dashSettingsLoad(isCurrent), dashRedisLoad(isCurrent)]);
}

// True when FORWARDED_ALLOW_IPS lists only loopback addresses, as uvicorn reads it
// (comma-separated, spaces around items ignored). The bundled compose file passes
// uvicorn's default "127.0.0.1,::1", so such a value names no proxy at another address.
function dashProxyLoopbackOnly(value) {
    var items = String(value).split(",").map(function (item) { return item.trim(); }).filter(Boolean);
    return items.length > 0 && items.every(function (item) { return item === "127.0.0.1" || item === "::1"; });
}

function dashSettingsDrawForm() {
    var body = document.getElementById("settings-body");
    if (!body || !dashSet.baseSnap) return;
    var f = dashSet.baseSnap.fields;
    var d = dashSet.draft;
    var own = dashSet.snap.yourAddress;
    var proxy = dashSet.snap.proxyNetwork || { envName: "FORWARDED_ALLOW_IPS", value: null };
    var proxySet = proxy.value !== null && proxy.value !== undefined;
    var proxyLoopback = proxySet && dashProxyLoopbackOnly(proxy.value);
    var proxyField = !proxySet
        ? '<p class="ui-hint">' + tHtml("settings.ratelimit.is-not-set-so-uvicorn-trusts", { env: dashCode(proxy.envName), header: dashCode(t("settings.ratelimit.x-forwarded-for")),
            a: dashCode("127.0.0.1"), b: dashCode("::1") }) + "</p>"
        : proxyLoopback
        ? '<p class="ui-locked-note">' + DASH_LOCK_ICON + "<span>" + tHtml("settings.ratelimit.loopback-addresses-only-uvicorn-trusts", { env: dashCode(proxy.envName),
            value: dashCode(proxy.value), header: dashCode(t("settings.ratelimit.x-forwarded-for")) }) + "</span></p>"
        : proxy.value === ""
        ? '<p class="ui-locked-note">' + DASH_LOCK_ICON + "<span>" + tHtml("settings.ratelimit.environment-variable-to-an-empty-value", { env: dashCode(proxy.envName),
            header: dashCode(t("settings.ratelimit.x-forwarded-for")) }) + "</span></p>"
        : proxy.value
        ? '<p class="ui-locked-note">' + DASH_LOCK_ICON + "<span>" + tHtml("settings.ratelimit.uvicorn-takes-the-caller-s-address", { env: dashCode(proxy.envName),
            value: dashCode(proxy.value), header: dashCode(t("settings.ratelimit.x-forwarded-for")) }) + "</span></p>"
        : "";
    // Shown whether the field is locked or not.
    var maxScope = esc(t("settings.ratelimit.an-ipv6-64-network-counts-as"));
    var maxField = f.maxReq.locked
        ? dashSetLocked(f.maxReq, tn("settings.ratelimit.count-requests", f.maxReq.value)) + '<span class="ui-hint">' + maxScope + "</span>"
        : '<input class="ui-input" id="set-max" type="number" inputmode="numeric" min="' + esc(f.maxReq.min) + '" max="' + esc(f.maxReq.max) +
          '" step="1" required value="' + esc(d.maxReq) + '" aria-describedby="set-max-hint" />' +
          '<span class="ui-hint" id="set-max-hint">' + esc(t("settings.ratelimit.from-1-to", { source: dashSetSourceHint(f.maxReq), max: uiNum(f.maxReq.max),
            scope: t("settings.ratelimit.an-ipv6-64-network-counts-as") })) + "</span>";
    var winField = f.windowMs.locked
        ? dashSetLocked(f.windowMs, t("settings.ratelimit.ms", { n: uiNum(f.windowMs.value) }))
        : '<input class="ui-input" id="set-window" type="number" inputmode="numeric" min="' + esc(f.windowMs.minSeconds) + '" max="' +
          esc(f.windowMs.maxSeconds) + '" step="1" required value="' + esc(d.windowSeconds) + '" aria-describedby="set-window-hint" />' +
          '<span class="ui-hint" id="set-window-hint">' + esc(t("settings.ratelimit.whole-seconds-from-1-to", { source: dashSetSourceHint(f.windowMs), max: uiNum(f.windowMs.maxSeconds) })) + "</span>";
    body.innerHTML =
        '<form class="ui-form" id="settings-form" novalidate><fieldset class="ui-settings-fields" id="settings-fields">' +
        '<div class="ui-form-row">' +
        '<div class="ui-field"><label class="ui-label" for="set-max">' + esc(t("settings.ratelimit.requests-per-window-from-one-address")) + '</label>' + maxField + "</div>" +
        '<div class="ui-field"><label class="ui-label" for="set-window">' + esc(t("settings.ratelimit.window-in-seconds")) + '</label>' + winField + "</div>" +
        "</div>" +
        '<div class="ui-field"><span class="ui-label">' + esc(t("settings.ratelimit.reverse-proxy-addresses")) + '</span>' + proxyField + "</div>" +
        '<div class="ui-field"><label class="ui-label" for="set-exempt">' + esc(t("settings.ratelimit.exempt-addresses-and-networks")) + ' <small>' + esc(t("settings.ratelimit.one-per-line-up-to", { max: uiNum(f.exempt.max) })) + "</small></label>" +
        '<textarea class="ui-textarea" id="set-exempt" spellcheck="false" autocapitalize="off" autocomplete="off" dir="ltr" aria-describedby="set-exempt-hint" placeholder="' + esc(t("settings.ratelimit.203-0-113-10-172-18")) + '">' +
        esc(d.exempt) + "</textarea>" +
        '<span class="ui-hint" id="set-exempt-hint">' + (own
            ? tHtml("settings.ratelimit.your-request-to-this-page-came", { example: dashCode("172.18.0.0/16"), own: dashCode(own) })
            : tHtml("settings.ratelimit.ipv4-or-ipv6-addresses-or-networks", { example: dashCode("172.18.0.0/16") })) + "</span>" +
        (proxySet && proxy.value && !proxyLoopback ? "" :
            '<div class="ui-alert is-warn" id="set-exempt-warn"><span class="ui-alert-bar" aria-hidden="true"></span><div><p>' + esc(t("settings.ratelimit.an-exempt-address-is-not-limited")) + '</p>' +
            "<small>" + tHtml("settings.ratelimit.if-a-reverse-proxy-sits-in", { env: dashCode(t("settings.ratelimit.forwarded-allow-ips")) }) + "</small></div><span></span></div>") +
        '<label class="ui-check-label hidden" id="set-own-wrap"><input type="checkbox" class="ui-check" id="set-own" />' + ' ' + '<span>' + tHtml("settings.ratelimit.exempt", { own: dashCode(own || "") }) + "</span></label>" +
        "</div>" +
        '<p class="ui-error hidden" id="set-err" role="alert"></p>' +
        '<div class="ui-settings-actions"><button type="submit" class="ui-btn ui-btn-primary" id="set-save">' + esc(t("settings.ratelimit.save")) + '</button>' +
        '<button type="button" class="ui-btn hidden" id="set-discard" data-set-discard="rate">' + esc(t("settings.ratelimit.discard-changes")) + '</button></div>' +
        "</fieldset></form>";
    dashSettingsSyncForm();
}

// Parts of the form that follow the draft and the last answer, without redrawing the inputs
function dashSettingsSyncForm() {
    var form = document.getElementById("settings-form");
    if (!form) return;

    var wrap = document.getElementById("set-own-wrap");
    // Shown when the server answered that the change exempts the admin's own address
    wrap.classList.toggle("hidden", !dashSet.askOwn);
    if (!dashSet.askOwn) document.getElementById("set-own").checked = false;
    dashPanelSyncCommon(DASH_SET_PANELS.rate, [["set-max", "maxReq"], ["set-window", "windowMs"], ["set-exempt", "exempt"]]);
}

function dashSettingsDraw() {
    dashPanelDraw(DASH_SET_PANELS.rate);
}

function dashSettingsAlerts(s, alerts) {
    var applied = dashSetAppliedAlerts(s, alerts);
    var saved = dashSetSavedLine(s, esc(t("settings.ratelimit.nothing-saved-here-yet-the-values")));
    alerts.push(dashSetAlert("", t("settings.ratelimit.saved-settings"), tHtml("settings.ratelimit.changes-apply-at-once-in-this", { saved: tRaw(saved), applied: tRaw(applied) })));
}

function dashSettingsConflictText(c) {
    var f = c.fields;
    return tHtml("settings.ratelimit.saved-now", { requests: tn("settings.ratelimit.count-requests", f.maxReq.value), seconds: uiNum(f.windowMs.value / 1000),
        exempt: tRaw(f.exempt.value.length ? f.exempt.value.map(function (a) { return uiUserText(a); }).join(", ") : esc(t("settings.ratelimit.none"))) });
}

function dashSettingsInput() {
    var d = dashSet.draft;
    var max = document.getElementById("set-max");
    var win = document.getElementById("set-window");
    if (max) d.maxReq = max.value;
    if (win) d.windowSeconds = win.value;
    d.exempt = document.getElementById("set-exempt").value;
    var changes = dashSetChanges();
    dashSet.dirty = Object.keys(changes).length > 0;
    dashSettingsSyncForm();
}

function dashSettingsSave() {
    if (dashSet.saving) return;
    var changes = dashSetChanges();
    var body = { baseVersion: dashSet.baseSnap.version };
    var whole = /^\d+$/;
    if ("maxReq" in changes) {
        if (!whole.test(changes.maxReq)) return dashSettingsFail(t("settings.ratelimit.requests-per-window-must-be-a"), "maxReq");
        body.maxReq = Number(changes.maxReq);
    }
    if ("windowSeconds" in changes) {
        if (!whole.test(changes.windowSeconds)) return dashSettingsFail(t("settings.ratelimit.the-window-must-be-a-whole"), "windowMs");
        body.windowSeconds = Number(changes.windowSeconds);
    }
    if ("exempt" in changes) body.exempt = changes.exempt;
    var ownBox = document.getElementById("set-own");
    if (ownBox && ownBox.checked) body.confirmOwnAddress = true;
    dashSet.saving = true;
    dashSet.error = "";
    dashSet.errorField = "";
    dashSettingsSyncForm();
    dashSet.seq += 1; // a load that started before this save must not draw over its result
    uiMutated();
    return api("PUT", "/admin/api/settings/rate-limit", body).then(function (res) {
        dashSet.saving = false;
        dashSet.seq += 1; // and so must a load that started while it ran
        var d = res.data || {};
        if (res.ok && d.fields) {
            dashPanelSaved(DASH_SET_PANELS.rate, d, t("settings.ratelimit.settings-saved"));
            return;
        }
        if (res.status === 409 && d.code === "conflict" && d.current) {
            dashPanelConflict(DASH_SET_PANELS.rate, d.current);
            return;
        }
        if (res.status === 409 && d.code === "own_address") dashSet.askOwn = true;
        dashSettingsFail(apiError(res, t("settings.ratelimit.could-not-save-the-settings")), d.field || "");
        if (d.code === "locked") dashSettingsLoad();
    });
}

function dashSettingsFail(message, field) {
    dashPanelFail(DASH_SET_PANELS.rate, message, field);
}

// ---------------------------------------------------------------------------
// General: time zone, log retention, the session cookie, and ROOT_PATH (read only)
// ---------------------------------------------------------------------------

var DASH_GEN_RETENTION = [
    ["activityLogRetentionDays", "gen-activity", "settings.general.activity-log-retention-in-days"],
    ["accessLogRetentionDays", "gen-access", "settings.general.access-log-retention-in-days"],
];

function dashGenLoad(isCurrent) {
    return dashPanelLoad(DASH_SET_PANELS.general, isCurrent);
}

function dashGenDraftFrom(s) {
    var f = s.fields;
    return {
        timeZone: f.timeZone.value,
        cookieSecure: f.cookieSecure.value === true,
        activityLogRetentionDays: String(f.activityLogRetentionDays.value),
        accessLogRetentionDays: String(f.accessLogRetentionDays.value),
    };
}

function dashGenChanges() {
    var f = dashGen.baseSnap.fields;
    var d = dashGen.draft;
    var out = {};
    if (!f.timeZone.locked && d.timeZone !== f.timeZone.value) out.timeZone = d.timeZone;
    if (!f.cookieSecure.locked && d.cookieSecure !== (f.cookieSecure.value === true)) out.cookieSecure = d.cookieSecure;
    DASH_GEN_RETENTION.forEach(function (r) {
        var v = d[r[0]].trim();
        if (!f[r[0]].locked && v !== String(f[r[0]].value)) out[r[0]] = v;
    });
    return out;
}

// After the draft changed: a confirmation goes when a value it was asked for changes
function dashGenDraftChanged() {
    var st = dashGen;
    var d = st.draft;
    if (st.askPurge && (st.askPurge.activityLogRetentionDays !== d.activityLogRetentionDays.trim() ||
        st.askPurge.accessLogRetentionDays !== d.accessLogRetentionDays.trim())) st.askPurge = null;
    if (st.askCookie && st.askCookie.cookieSecure !== d.cookieSecure) st.askCookie = null;
    st.dirty = Object.keys(dashGenChanges()).length > 0;
}

function dashGenClearAsks(st) {
    st.askPurge = null;
    st.askCookie = null;
}

function dashGenInput() {
    var d = dashGen.draft;
    if (!d) return;
    var tz = document.getElementById("gen-tz");
    var cookie = document.getElementById("gen-cookie");
    if (tz) d.timeZone = tz.value;
    if (cookie) d.cookieSecure = cookie.checked;
    DASH_GEN_RETENTION.forEach(function (r) {
        var el = document.getElementById(r[1]);
        if (el) d[r[0]] = el.value;
    });
    dashGenDraftChanged();
    dashGenSyncForm();
}

// The admin ticked or cleared a confirmation box
function dashGenConfirm(kind, ok) {
    var ask = kind === "purge" ? dashGen.askPurge : dashGen.askCookie;
    if (ask) ask.ok = !!ok;
}

function dashGenRetentionField(f, r, d) {
    var field = f[r[0]];
    if (field.locked) return dashSetLocked(field, tn("settings.general.count-days", field.value));
    return '<input class="ui-input" id="' + r[1] + '" type="number" inputmode="numeric" min="' + esc(field.min) + '" max="' + esc(field.max) +
        '" step="1" required value="' + esc(d[r[0]]) + '" aria-describedby="' + r[1] + '-hint" />' +
        '<span class="ui-hint" id="' + r[1] + '-hint">' + esc(t("settings.general.whole-days-from", { source: dashSetSourceHint(field), min: uiNum(field.min), max: uiNum(field.max) })) + "</span>";
}

function dashGenDrawForm() {
    var body = document.getElementById("general-body");
    if (!body || !dashGen.baseSnap) return;
    var s = dashGen.baseSnap;
    var f = s.fields;
    var d = dashGen.draft;
    // Only zones this browser can show dates in; the current value stays so it is not changed silently
    var zones = (Array.isArray(s.timeZones) ? s.timeZones : []).filter(function (z) { return uiZoneValid(z); });
    if (zones.indexOf(d.timeZone) === -1) zones.unshift(d.timeZone);
    var tzField = f.timeZone.locked
        ? dashSetLocked(f.timeZone, f.timeZone.envInvalid ? t("settings.general.not-a-time-zone-so-utc") : f.timeZone.value)
        : '<select class="ui-select" id="gen-tz" aria-describedby="gen-tz-hint">' + zones.map(function (z) {
            return '<option value="' + esc(z) + '"' + (z === d.timeZone ? " selected" : "") + ">" + esc(z) + "</option>";
        }).join("") + "</select>" +
          '<span class="ui-hint" id="gen-tz-hint">' + esc(t("settings.general.dates-on-every-page-and-the", { source: dashSetSourceHint(f.timeZone) })) + "</span>";
    var cookieField = f.cookieSecure.locked
        ? dashSetLocked(f.cookieSecure, f.cookieSecure.value ? "true" : "false")
        : '<label class="ui-check-label"><input type="checkbox" class="ui-check" id="gen-cookie"' + (d.cookieSecure ? " checked" : "") +
          ' aria-describedby="gen-cookie-hint" />' + ' ' + '<span>' + esc(t("settings.general.send-the-session-cookie-over-https")) + '</span></label>' +
          '<span class="ui-hint" id="gen-cookie-hint">' + esc(t("settings.general.turn-it-on-when-admins-reach", { source: dashSetSourceHint(f.cookieSecure) })) + "</span>";
    var root = s.rootPath || { envName: "ROOT_PATH", value: null };
    var rootField = root.value === null || root.value === undefined
        ? '<p class="ui-hint">' + tHtml("settings.general.is-not-set-so-the-portal", { env: dashCode(root.envName) }) + "</p>"
        : '<p class="ui-locked-note">' + DASH_LOCK_ICON + "<span>" + (root.value
            ? tHtml("settings.general.environment-variable", { env: dashCode(root.envName), value: dashCode(root.value) })
            : tHtml("settings.general.to-an-empty-value-so-the", { env: dashCode(root.envName) })) + "</span></p>";
    var locked = f.timeZone.locked && f.cookieSecure.locked && f.activityLogRetentionDays.locked && f.accessLogRetentionDays.locked;
    body.innerHTML =
        '<form class="ui-form" id="general-form" novalidate autocomplete="off"><fieldset class="ui-settings-fields" id="general-fields">' +
        '<div class="ui-field"><label class="ui-label" for="gen-tz">' + esc(t("settings.general.time-zone")) + '</label>' + tzField + "</div>" +
        '<div class="ui-form-row">' + DASH_GEN_RETENTION.map(function (r) {
            return '<div class="ui-field"><label class="ui-label" for="' + r[1] + '">' + esc(t(r[2])) + "</label>" + dashGenRetentionField(f, r, d) + "</div>";
        }).join("") + "</div>" +
        '<div class="ui-field"><span class="ui-label">' + esc(t("settings.general.secure-session-cookie")) + '</span>' + cookieField + "</div>" +
        '<div class="ui-field"><span class="ui-label">' + esc(t("settings.general.url-path-prefix")) + '</span>' + rootField + "</div>" +
        '<div class="ui-settings-asks" id="gen-asks" aria-live="polite"></div>' +
        '<p class="ui-error hidden" id="gen-err" role="alert"></p>' +
        (locked ? "" : '<div class="ui-settings-actions"><button type="submit" class="ui-btn ui-btn-primary" id="gen-save">' + esc(t("settings.general.save")) + '</button>' +
            '<button type="button" class="ui-btn hidden" id="gen-discard" data-set-discard="general">' + esc(t("settings.general.discard-changes")) + '</button></div>') +
        "</fieldset></form>";
    var sel = document.getElementById("gen-tz");
    if (sel) uiPicker(sel, { label: t("settings.general.time-zone"), field: true });
    dashGenSyncForm();
}

// True when the confirmation lowers this retention below the value the page was showing.
function dashGenLowered(ask, name) {
    var snap = dashGen.snap;
    var cur = snap && snap.fields && snap.fields[name] ? snap.fields[name].value : null;
    return !!ask.confirm && typeof cur === "number" && ask.confirm[name] < cur;
}

function dashGenAsksHtml() {
    var st = dashGen;
    var html = "";
    if (st.askPurge) {
        var e = st.askPurge.estimate || {};
        var rows = function (n, unknownKey, countKey) { return n === null || n === undefined ? t(unknownKey) : t("settings.general.about", { count: tn(countKey, n) }); };
        // Only the logs whose retention goes down; the server leaves the other estimate out.
        var parts = [];
        if (dashGenLowered(st.askPurge, "activityLogRetentionDays")) parts.push(rows(e.activityLogs, "settings.general.unknown-activity-log-entries", "settings.general.count-activity-log-entries"));
        if (dashGenLowered(st.askPurge, "accessLogRetentionDays")) parts.push(rows(e.accessLogs, "settings.general.unknown-access-log-entries", "settings.general.count-access-log-entries"));
        html += dashSetAskHtml("gen-purge-ok", t("settings.general.lowering-the-retention-deletes-older-entries"),
            parts.length ? esc(t("settings.general.older-than-the-new-limit", { detail: st.askPurge.detail || "",
                parts: parts.length > 1 ? t("settings.general.and", { first: parts[0], second: parts[1] }) : parts[0] })) : esc(st.askPurge.detail || ""),
            t("settings.general.delete-them-when-i-save"), st.askPurge.ok);
    }
    if (st.askCookie) {
        html += dashSetAskHtml("gen-cookie-ok", t("settings.general.this-page-was-opened-over-plain"), esc(st.askCookie.detail || ""),
            t("settings.general.turn-on-the-secure-cookie-anyway"), st.askCookie.ok);
    }
    return html;
}

function dashGenSyncForm() {
    var form = document.getElementById("general-form");
    if (!form) return;
    dashSetAsksDraw("gen-asks", dashGenAsksHtml());
    dashPanelSyncCommon(DASH_SET_PANELS.general, [["gen-tz-pick", "timeZone"], ["gen-cookie", "cookieSecure"],
        ["gen-activity", "activityLogRetentionDays"], ["gen-access", "accessLogRetentionDays"]]);
    var pick = document.getElementById("gen-tz");
    if (pick && pick.uiPicker) uiPickerSync(pick);
}

function dashGenDraw() {
    dashPanelDraw(DASH_SET_PANELS.general);
}

var DASH_GEN_PURGE = [["activity", "settings.general.activity-log"], ["access", "settings.general.access-log"]];

function dashGenAlerts(s, alerts) {
    var tz = s.fields.timeZone;
    if (tz.envInvalid) {
        alerts.push(dashSetAlert(" is-warn", t("settings.general.tz-is-not-a-time-zone"),
            tHtml("settings.general.environment-variable-is-empty-or-not", { env: dashCode(tz.envName), example: dashCode(t("settings.general.europe-london")) })));
    }
    var purge = s.purge || {};
    DASH_GEN_PURGE.forEach(function (x) {
        var p = purge[x[0]];
        if (p && p.state === "skipped") {
            alerts.push(dashSetAlert(" is-warn", t("settings.general.old", { log: t(x[1]) }),
                p.lastRun
                    ? tHtml("settings.general.last-attempt", { reason: p.reason || t("settings.general.this-server-process-skipped-the-cleanup"), time: tRaw(uiTime(p.lastRun)) })
                    : esc(t("settings.general.the-cleanup-runs-again-every-minute", { reason: p.reason || t("settings.general.this-server-process-skipped-the-cleanup") }))));
        }
    });
    var applied = dashSetAppliedAlerts(s, alerts);
    var saved = dashSetSavedLine(s, esc(t("settings.general.nothing-saved-here-yet-the-values")));
    alerts.push(dashSetAlert("", t("settings.general.saved-settings"), tHtml("settings.general.changes-apply-at-once-in-this", { saved: tRaw(saved), applied: tRaw(applied) })));
}

function dashGenConflictText(c) {
    var f = c.fields;
    return esc(t("settings.general.saved-now-time-zone", { zone: f.timeZone.value, activity: tn("settings.general.count-days", f.activityLogRetentionDays.value),
        access: tn("settings.general.count-days", f.accessLogRetentionDays.value), cookie: f.cookieSecure.value ? t("settings.general.on") : t("settings.general.off") }));
}

function dashGenSave() {
    var st = dashGen;
    if (st.saving || !st.baseSnap) return;
    var changes = dashGenChanges();
    var body = { baseVersion: st.baseSnap.version };
    var whole = /^\d+$/;
    if ("timeZone" in changes) body.timeZone = changes.timeZone;
    if ("cookieSecure" in changes) body.cookieSecure = changes.cookieSecure;
    for (var i = 0; i < DASH_GEN_RETENTION.length; i++) {
        var key = DASH_GEN_RETENTION[i][0];
        if (!(key in changes)) continue;
        if (!whole.test(changes[key])) return dashPanelFail(DASH_SET_PANELS.general, t("settings.general.retention-must-be-a-whole-number"), key);
        body[key] = Number(changes[key]);
    }
    if (st.askPurge && st.askPurge.ok) body.confirmPurge = st.askPurge.confirm;
    if (st.askCookie && st.askCookie.ok) body.confirmCookieSecure = true;
    // The values this request asks about; a confirmation the server asks for is bound to them
    var sent = { activityLogRetentionDays: st.draft.activityLogRetentionDays.trim(), accessLogRetentionDays: st.draft.accessLogRetentionDays.trim(),
        cookieSecure: st.draft.cookieSecure };
    var zoneBefore = st.baseSnap.fields.timeZone.value;
    st.saving = true;
    st.error = "";
    st.errorField = "";
    dashGenSyncForm();
    st.seq += 1;
    uiMutated();
    return api("PUT", "/admin/api/settings/general", body).then(function (res) {
        st.saving = false;
        st.seq += 1;
        var d = res.data || {};
        if (res.ok && d.fields) {
            dashPanelSaved(DASH_SET_PANELS.general, d, t("settings.general.settings-saved"));
            var zone = d.fields.timeZone && d.fields.timeZone.value;
            if (zone && zone !== zoneBefore) dashGenZoneSaved(zone);
            return;
        }
        if (res.status === 409 && d.code === "conflict" && d.current) {
            dashGenClearAsks(st);
            dashPanelConflict(DASH_SET_PANELS.general, d.current);
            dashGenSyncForm();
            return;
        }
        if (res.status === 409 && d.code === "confirm_purge" && d.confirm) {
            st.askPurge = { confirm: d.confirm, estimate: d.estimate || {}, detail: d.detail || "", ok: false,
                activityLogRetentionDays: sent.activityLogRetentionDays, accessLogRetentionDays: sent.accessLogRetentionDays };
            dashGenSyncForm();
            return;
        }
        if (res.status === 409 && d.code === "confirm_cookie_secure") {
            st.askCookie = { detail: d.detail || "", ok: false, cookieSecure: sent.cookieSecure };
            dashGenSyncForm();
            return;
        }
        dashPanelFail(DASH_SET_PANELS.general, apiError(res, t("settings.general.could-not-save-the-settings")), d.field || "");
        if (d.code === "locked") dashGenLoad();
    });
}

// This tab takes the new zone at once; other open tabs keep the old one until reloaded
function dashGenZoneSaved(zone) {
    dash.zoneAsked = zone;
    uiSetAppZone(zone);
    dashRedrawDates();
    dashSettingsDraw();
    dashGenDraw();
    dashRedisDraw();
}

// ---------------------------------------------------------------------------
// Redis connection: saved only after the server checked it; used after a restart
// ---------------------------------------------------------------------------

var DASH_REDIS_SOURCE = {
    env: ["settings.redis.source-env", "settings.redis.redis-url"],
    stored: ["settings.redis.the-value-saved-here", ""],
    "default": ["settings.redis.source-default", "settings.redis.redis-default-url"],
};
var DASH_REDIS_CHECK = {
    unreachable: "settings.redis.could-not-connect-to-this-redis",
    auth: "settings.redis.redis-refused-the-user-name-or",
    no_write: "settings.redis.this-connection-cannot-write-the-keys",
    other: "settings.redis.the-check-of-this-redis-failed",
};

function dashRedisLoad(isCurrent) {
    return dashPanelLoad(DASH_SET_PANELS.redis, isCurrent);
}

// The field is write-only: the draft is only what the admin typed
function dashRedisDraftFrom() {
    return { url: "" };
}

function dashRedisClearAsks(st) {
    st.askRestart = null;
}

function dashRedisDraftChanged() {
    var st = dashRedis;
    if (st.askRestart && st.askRestart.url !== st.draft.url.trim()) st.askRestart = null;
    st.dirty = !!st.baseSnap && !st.baseSnap.url.locked && st.draft.url.trim() !== "";
}

function dashRedisInput() {
    if (!dashRedis.draft) return;
    var el = document.getElementById("redis-url");
    if (el) dashRedis.draft.url = el.value;
    dashRedisDraftChanged();
    dashRedisSyncForm();
}

function dashRedisConfirm(ok) {
    if (dashRedis.askRestart) dashRedis.askRestart.ok = !!ok;
}

function dashRedisDrawForm() {
    var body = document.getElementById("redis-body");
    if (!body || !dashRedis.baseSnap) return;
    var u = dashRedis.baseSnap.url;
    var source = tRaw(DASH_REDIS_SOURCE[u.source] ? tHtml(DASH_REDIS_SOURCE[u.source][0], { env: tRaw(DASH_REDIS_SOURCE[u.source][1] ? "<code>" + esc(t(DASH_REDIS_SOURCE[u.source][1])) + "</code>" : "") }) : esc(u.source));
    var now = '<p class="ui-hint">' + (u.saved
        ? tHtml("settings.redis.saved-here", { connected: dashCode(u.connected), saved: dashCode(u.saved), source: source })
        : tHtml("settings.redis.this-server-process-uses", { connected: dashCode(u.connected), source: source })) + "</p>";
    var field = u.locked
        ? dashSetLocked(u, u.connected)
        : '<input class="ui-input" id="redis-url" type="text" autocomplete="off" spellcheck="false" autocapitalize="off" dir="ltr" ' +
          'placeholder="' + esc(t("settings.redis.redis-password-host-6379-0")) + '" aria-describedby="redis-url-hint" />' +
          '<span class="ui-hint" id="redis-url-hint">' + tHtml("settings.redis.the-whole-url-with-its-password", { scheme: dashCode(t("settings.redis.rediss")) }) + "</span>";
    body.innerHTML =
        '<form class="ui-form" id="redis-form" novalidate autocomplete="off"><fieldset class="ui-settings-fields" id="redis-fields">' +
        '<div class="ui-field"><span class="ui-label">' + esc(t("settings.redis.current-connection")) + '</span>' + now + "</div>" +
        '<div class="ui-field"><label class="ui-label" for="redis-url">' + esc(t("settings.redis.new-connection-url")) + '</label>' + field + "</div>" +
        '<div class="ui-settings-asks" id="redis-asks" aria-live="polite"></div>' +
        '<p class="ui-error hidden" id="redis-err" role="alert"></p>' +
        (u.locked ? "" : '<div class="ui-settings-actions"><button type="submit" class="ui-btn ui-btn-primary" id="redis-save">' + esc(t("settings.redis.save")) + '</button>' +
            '<button type="button" class="ui-btn hidden" id="redis-discard" data-set-discard="redis">' + esc(t("settings.redis.discard-changes")) + '</button></div>') +
        "</fieldset></form>";
    dashRedisSyncForm();
}

function dashRedisSyncForm() {
    var form = document.getElementById("redis-form");
    if (!form) return;
    var ask = dashRedis.askRestart;
    dashSetAsksDraw("redis-asks", ask ? dashSetAskHtml("redis-restart-ok", t("settings.redis.saving-takes-effect-only-after-a"), esc(ask.detail || ""),
        t("settings.redis.save-it-i-will-restart-every"), ask.ok) : "");
    dashPanelSyncCommon(DASH_SET_PANELS.redis, [["redis-url", "url"]]);
}

function dashRedisDraw() {
    dashPanelDraw(DASH_SET_PANELS.redis);
}

function dashRedisAlerts(s, alerts) {
    var u = s.url;
    if (u.syncPending) {
        alerts.push(dashSetAlert(" is-warn", t("settings.redis.the-environment-value-is-not-saved"),
            tHtml("settings.redis.the-service-saves", { env: dashCode(u.envName) })));
    }
    if (s.restartNeeded) {
        alerts.push(dashSetAlert(" is-warn", t("settings.redis.restart-the-service-to-use-the"),
            tHtml("settings.redis.this-server-process-still-uses", { connected: dashCode(u.connected), command: dashCode(t("settings.redis.docker-compose-restart-auth-service")) })));
    }
    var saved = dashSetSavedLine(s, esc(t("settings.redis.nothing-saved-here-yet")));
    alerts.push(dashSetAlert("", t("settings.redis.saved-connection"), tHtml("settings.redis.a-saved-connection-takes-effect-when", { saved: tRaw(saved) })));
}

function dashRedisConflictText(c) {
    return tHtml("settings.redis.saved-now", { url: c.url.saved ? dashCode(c.url.saved) : t("settings.redis.nothing") });
}

function dashRedisSave() {
    var st = dashRedis;
    if (st.saving || !st.baseSnap) return;
    var url = st.draft.url.trim();
    if (!url) return dashPanelFail(DASH_SET_PANELS.redis, t("settings.redis.enter-the-new-connection-url"), "url");
    var body = { baseVersion: st.baseSnap.version, url: url };
    if (st.askRestart && st.askRestart.ok) body.confirmRestart = true;
    st.saving = true;
    st.error = "";
    st.errorField = "";
    dashRedisSyncForm();
    st.seq += 1;
    uiMutated();
    return api("PUT", "/admin/api/settings/redis", body).then(function (res) {
        st.saving = false;
        st.seq += 1;
        var d = res.data || {};
        if (res.ok && d.url) {
            var el = document.getElementById("redis-url");
            if (el) el.value = "";
            dashPanelSaved(DASH_SET_PANELS.redis, d, t("settings.redis.redis-connection-saved-restart-the-service"));
            return;
        }
        if (res.status === 409 && d.code === "conflict" && d.current) {
            dashRedisClearAsks(st);
            dashPanelConflict(DASH_SET_PANELS.redis, d.current);
            dashRedisSyncForm();
            return;
        }
        if (res.status === 409 && d.code === "confirm_restart") {
            st.askRestart = { detail: d.detail || "", ok: false, url: url };
            dashRedisSyncForm();
            return;
        }
        if (d.code === "redis_check_failed") {
            dashPanelFail(DASH_SET_PANELS.redis, t("settings.redis.nothing-was-saved", { reason: t(DASH_REDIS_CHECK[d.category] || DASH_REDIS_CHECK.other) }), "url");
            return;
        }
        dashPanelFail(DASH_SET_PANELS.redis, apiError(res, t("settings.redis.could-not-save-the-redis-connection")), d.field || "");
        if (d.code === "locked") dashRedisLoad();
    });
}

// ---------------------------------------------------------------------------
// The panels, the page and its events
// ---------------------------------------------------------------------------

var DASH_SET_PANELS = {
    rate: {
        name: "rate", st: dashSet, failedTitle: "settings.could-not-load-the-settings", loadingText: "settings.loading-settings",
        ids: { info: "settings-info", body: "settings-body", form: "settings-form", fields: "settings-fields", err: "set-err", save: "set-save", discard: "set-discard" },
        get: function () { return api("GET", "/admin/api/settings/rate-limit"); },
        valid: function (d) { return !!d.fields; },
        draftFrom: dashSetDraftFrom, drawForm: dashSettingsDrawForm, draw: dashSettingsDraw, sync: dashSettingsSyncForm, input: dashSettingsInput,
        save: dashSettingsSave, alerts: dashSettingsAlerts, conflictText: dashSettingsConflictText,
        clearAsks: function (st) { st.askOwn = false; },
    },
    general: {
        name: "general", st: dashGen, failedTitle: "settings.could-not-load-the-general-settings", loadingText: "settings.loading-the-general-settings",
        ids: { info: "general-info", body: "general-body", form: "general-form", fields: "general-fields", err: "gen-err", save: "gen-save", discard: "gen-discard" },
        get: function () { return api("GET", "/admin/api/settings/general"); },
        valid: function (d) { return !!(d.fields && d.fields.timeZone); },
        formKey: function (d) { return JSON.stringify([d.version, d.fields, d.rootPath, (d.timeZones || []).length]); },
        draftFrom: dashGenDraftFrom, drawForm: dashGenDrawForm, draw: dashGenDraw, sync: dashGenSyncForm, input: dashGenInput,
        save: dashGenSave, alerts: dashGenAlerts, conflictText: dashGenConflictText, clearAsks: dashGenClearAsks, asksBound: true,
    },
    redis: {
        name: "redis", st: dashRedis, failedTitle: "settings.could-not-load-the-redis-connection", loadingText: "settings.loading-the-redis-connection",
        ids: { info: "redis-info", body: "redis-body", form: "redis-form", fields: "redis-fields", err: "redis-err", save: "redis-save", discard: "redis-discard" },
        get: function () { return api("GET", "/admin/api/settings/redis"); },
        valid: function (d) { return !!d.url; },
        formKey: function (d) { return JSON.stringify([d.version, d.url]); },
        draftFrom: dashRedisDraftFrom, drawForm: dashRedisDrawForm, draw: dashRedisDraw, sync: dashRedisSyncForm, input: dashRedisInput,
        save: dashRedisSave, alerts: dashRedisAlerts, conflictText: dashRedisConflictText, clearAsks: dashRedisClearAsks, asksBound: true,
    },
};
var DASH_SET_FORMS = { "settings-form": "rate", "general-form": "general", "redis-form": "redis" };
var DASH_SET_CONFIRMS = {
    "gen-purge-ok": function (ok) { dashGenConfirm("purge", ok); },
    "gen-cookie-ok": function (ok) { dashGenConfirm("cookie", ok); },
    "redis-restart-ok": dashRedisConfirm,
};

function dashSettingsBind() {
    if (dashSet.bound) return;
    dashSet.bound = true;
    var page = document.querySelector('[data-page="settings"]');
    // Typing, a picked time zone (change) and a ticked box (both) update the draft
    var edited = function (event) {
        var target = event.target;
        if (DASH_SET_CONFIRMS[target.id]) {
            DASH_SET_CONFIRMS[target.id](target.checked);
            return;
        }
        if (target.id === "set-own") return;
        var form = target.closest("form");
        var p = form && DASH_SET_PANELS[DASH_SET_FORMS[form.id]];
        if (p) p.input();
    };
    page.addEventListener("input", edited);
    page.addEventListener("change", edited);
    page.addEventListener("submit", function (event) {
        var p = DASH_SET_PANELS[DASH_SET_FORMS[event.target.id]];
        if (!p) return;
        event.preventDefault();
        p.save();
    });
    page.addEventListener("click", function (event) {
        var target = event.target;
        var retry = target.closest("[data-settings-retry]");
        var discard = target.closest("[data-set-discard]");
        var keep = target.closest("[data-set-keep]");
        var p;
        if (retry && (p = DASH_SET_PANELS[retry.getAttribute("data-settings-retry")])) {
            p.st.failed = "";
            p.draw();
            dashPanelLoad(p);
        } else if (discard && (p = DASH_SET_PANELS[discard.getAttribute("data-set-discard")])) {
            dashPanelDiscard(p);
        } else if (keep && (p = DASH_SET_PANELS[keep.getAttribute("data-set-keep")])) {
            dashPanelKeep(p);
        }
    });
    // A changed draft is an unsaved change, in each panel
    Object.keys(DASH_SET_PANELS).forEach(function (name) {
        var p = DASH_SET_PANELS[name];
        uiUnsavedRegister(function () {
            return p.st.dirty ? 1 : 0;
        }, function () {
            if (!p.st.baseSnap) return;
            p.st.draft = p.draftFrom(p.st.baseSnap);
            p.st.dirty = false;
            p.st.conflict = null;
            if (p.asksBound) p.clearAsks(p.st);
            p.drawForm();
        });
    });
}

// The passkey address on the Settings page: one field, saved on top of the version
// it was read at (baseVersion). A refresh never touches a changed draft.
var dashPkSet = { snap: null, base: null, draft: "", dirty: false, saving: false, failed: "", refreshFailed: "", error: "", stale: false, seq: 0, bound: false };

function dashPkSetLoad(isCurrent) {
    var seq = ++dashPkSet.seq;
    return api("GET", "/admin/api/settings/passkeys").then(function (res) {
        if (seq !== dashPkSet.seq || dashPkSet.saving || (isCurrent && !isCurrent())) return;
        var d = res.data;
        if (!res.ok || !d || typeof d.version !== "number") {
            var why = apiError(res, t("settings.passkeys.the-server-did-not-answer"));
            if (dashPkSet.snap) dashPkSet.refreshFailed = why;
            else dashPkSet.failed = why;
            dashPkSetDraw(false);
            return;
        }
        var changed = !dashPkSet.snap || dashPkSet.snap.version !== d.version || dashPkSet.snap.locked !== d.locked;
        dashPkSet.failed = "";
        dashPkSet.refreshFailed = "";
        dashPkSet.snap = d;
        if (!dashPkSet.dirty) {
            dashPkSet.base = d;
            dashPkSet.draft = d.origin || "";
            dashPkSet.stale = false;
            if (changed) dashPkSet.error = "";
        }
        dashPkSetDraw(changed && !dashPkSet.dirty);
    });
}

// The host name of an address, as the passkeys' rpId is; "" when it is not one
function dashPkHost(value) {
    try { return new URL(value).hostname.toLowerCase(); } catch (e) { return ""; }
}

function dashPkSetFormHtml() {
    var b = dashPkSet.base;
    var here = window.location.origin;
    var field;
    if (b.locked) {
        field = dashSetLocked({ envName: b.envName || "PASSKEY_ORIGIN" }, b.origin || "");
    } else {
        field = '<input class="ui-input" id="set-pk-origin" type="url" inputmode="url" spellcheck="false" autocapitalize="off" autocomplete="off" dir="ltr" ' +
            'placeholder="' + esc(here) + '" value="' + esc(dashPkSet.draft) + '" aria-describedby="set-pk-hint" data-inventory="settings.passkeys.origin" />';
    }
    var hint = !b.locked && !b.origin ? tHtml("settings.passkeys.not-set-yet-the-first-passkey", { here: dashCode(here) })
        : !b.locked && b.origin && b.savedBy ? tHtml("settings.passkeys.saved-by", { here: dashCode(here), user: tRaw(uiUserText(b.savedBy)), time: tRaw(uiTime(b.savedAt)) })
        : tHtml("settings.passkeys.the-address-admins-open-this-service", { here: dashCode(here) });
    return '<form class="ui-form" id="pk-set-form" novalidate><fieldset class="ui-settings-fields" id="pk-set-fields">' +
        '<div class="ui-field"><label class="ui-label" for="set-pk-origin">' + esc(t("settings.passkeys.passkey-address")) + '</label>' + field +
        '<span class="ui-hint" id="set-pk-hint">' + hint + "</span></div>" +
        '<div class="ui-alert is-warn"><span class="ui-alert-bar" aria-hidden="true"></span><div><p>' + esc(t("settings.passkeys.passkeys-made-for-another-address-stop")) + '</p>' +
        "<small>" + esc(t("settings.passkeys.a-new-host-name-or-no")) + "</small></div><span></span></div>" +
        '<p class="ui-error hidden" id="set-pk-err" role="alert"></p>' +
        (b.locked ? "" : '<div class="ui-settings-actions"><button type="submit" class="ui-btn ui-btn-primary" id="set-pk-save">' + esc(t("settings.passkeys.save")) + '</button>' +
            '<button type="button" class="ui-btn hidden" id="set-pk-discard">' + esc(t("settings.passkeys.discard-changes")) + '</button>' +
            '<button type="button" class="ui-btn hidden" id="set-pk-reload">' + esc(t("settings.passkeys.reload")) + '</button></div>') +
        "</fieldset></form>";
}

// Draw the section; the form itself only when it is new or redraw is set
function dashPkSetDraw(redraw) {
    var body = document.getElementById("settings-pk-body");
    if (!body) return;
    if (!dashPkSet.snap) {
        body.innerHTML = dashPkSet.failed ? dashFailed(t("settings.passkeys.could-not-load-the-passkey-address"), dashPkSet.failed, "data-pk-set-retry") : dashLoading(t("settings.passkeys.loading-the-passkey-address"));
        return;
    }
    if (redraw || !document.getElementById("pk-set-form")) body.innerHTML = dashPkSetFormHtml();
    var err = document.getElementById("set-pk-err");
    var text = dashPkSet.error || (dashPkSet.refreshFailed ? t("settings.passkeys.could-not-refresh-the-passkey-address", { reason: dashPkSet.refreshFailed }) : "");
    err.textContent = text;
    err.classList.toggle("hidden", !text);
    var input = document.getElementById("set-pk-origin");
    if (input) {
        if (dashPkSet.error && !dashPkSet.stale) input.setAttribute("aria-invalid", "true");
        else input.removeAttribute("aria-invalid");
    }
    document.getElementById("pk-set-fields").disabled = dashPkSet.saving;
    var save = document.getElementById("set-pk-save");
    if (save) save.textContent = dashPkSet.saving ? t("settings.passkeys.saving") : t("settings.passkeys.save");
    var discard = document.getElementById("set-pk-discard");
    if (discard) discard.classList.toggle("hidden", !dashPkSet.dirty);
    var reload = document.getElementById("set-pk-reload");
    if (reload) reload.classList.toggle("hidden", !dashPkSet.stale);
}

function dashPkSetReset() {
    var to = dashPkSet.snap;
    dashPkSet.base = to;
    dashPkSet.draft = (to && to.origin) || "";
    dashPkSet.dirty = false;
    dashPkSet.stale = false;
    dashPkSet.error = "";
}

function dashPkSetSave() {
    if (dashPkSet.saving || !dashPkSet.base || dashPkSet.base.locked) return;
    var b = dashPkSet.base;
    var value = dashPkSet.draft.trim();
    var current = b.origin || "";
    if (value === current) {
        dashPkSet.dirty = false;
        dashPkSetDraw(false);
        return;
    }
    var ask = null;
    if (!value) {
        ask = { title: t("settings.passkeys.remove-the-passkey-address"), message: t("settings.passkeys.no-passkey-works-until-an-address"), confirmText: t("settings.passkeys.remove") };
    } else if (current && b.rpId && dashPkHost(value) && dashPkHost(value) !== b.rpId) {
        ask = { title: t("settings.passkeys.change-the-passkey-address"), message: t("settings.passkeys.passkeys-made-for", { address: b.rpId }), confirmText: t("settings.passkeys.change") };
    }
    if (ask) ask.danger = true;
    (ask ? uiConfirm(ask) : Promise.resolve(true)).then(function (ok) {
        if (!ok || dashPkSet.saving) return;
        dashPkSet.saving = true;
        dashPkSet.error = "";
        dashPkSet.seq += 1; // a load that started before this save must not draw over it
        dashPkSetDraw(false);
        uiMutated();
        api("PUT", "/admin/api/settings/passkeys", { baseVersion: b.version, origin: value }).then(function (res) {
            dashPkSet.saving = false;
            dashPkSet.seq += 1;
            var d = res.data;
            if (res.ok && d && typeof d.version === "number") {
                dashPkSet.snap = d;
                dashPkSetReset();
                dashPkSetDraw(true);
                dashPk.at = 0; // the Account page and Copy token read the list again
                uiMutated();
                showToast(d.origin ? t("settings.passkeys.passkey-address-saved") : t("settings.passkeys.passkey-address-removed"), "success");
                return;
            }
            if (res.status === 401) return;
            dashPkSet.error = apiError(res, t("settings.passkeys.could-not-save-the-passkey-address"));
            // Locked by the environment, or saved by someone else: the admin reloads
            dashPkSet.stale = res.status === 409;
            dashPkSetDraw(false);
        });
    });
}

function dashPkSetBind() {
    if (dashPkSet.bound) return;
    dashPkSet.bound = true;
    var panel = document.getElementById("settings-pk-panel");
    panel.addEventListener("input", function (event) {
        if (event.target.id !== "set-pk-origin") return;
        dashPkSet.draft = event.target.value;
        dashPkSet.dirty = dashPkSet.draft.trim() !== ((dashPkSet.base && dashPkSet.base.origin) || "");
        dashPkSetDraw(false);
    });
    panel.addEventListener("submit", function (event) {
        if (event.target.id !== "pk-set-form") return;
        event.preventDefault();
        dashPkSetSave();
    });
    panel.addEventListener("click", function (event) {
        var target = event.target;
        if (target.closest("[data-pk-set-retry]")) {
            dashPkSet.failed = "";
            dashPkSetDraw(false);
            dashPkSetLoad();
        } else if (target.closest("#set-pk-discard")) {
            dashPkSetReset();
            dashPkSetDraw(true);
        } else if (target.closest("#set-pk-reload")) {
            dashPkSet.dirty = false;
            dashPkSet.stale = false;
            dashPkSet.error = "";
            dashPkSet.snap = null;
            dashPkSetDraw(true);
            dashPkSetLoad();
        }
    });
    uiUnsavedRegister(function () {
        return dashPkSet.dirty ? 1 : 0;
    }, function () {
        if (!dashPkSet.snap) return;
        dashPkSetReset();
        dashPkSetDraw(true);
    });
}

// ---------------------------------------------------------------------------
// Database connection: shown here; a PostgreSQL connection saved by setup can be
// changed (host, user, password, TLS) for the same installation. The server checks
// the new connection, switches while requests wait a moment, and keeps the old one
// on any failure. Saving needs an authenticator code or a passkey.
// ---------------------------------------------------------------------------

var dashDb = { snap: null, failed: "", refreshFailed: "", check: null, checkedFor: "", error: "", confirm: false, askConfirm: "",
    busy: "", dirty: false, seq: 0, bound: false };

function dashDbLoad(isCurrent) {
    var seq = ++dashDb.seq;
    return api("GET", "/admin/api/settings/database").then(function (res) {
        if (seq !== dashDb.seq || dashDb.busy || (isCurrent && !isCurrent())) return;
        var d = res.data;
        if (!res.ok || !d || !d.connection) {
            var why = apiError(res, t("settings.database.the-server-did-not-answer"));
            if (dashDb.snap) dashDb.refreshFailed = why;
            else dashDb.failed = why;
            dashDbDraw(false);
            return;
        }
        var changed = !dashDb.snap || JSON.stringify(dashDb.snap.connection) !== JSON.stringify(d.connection) || dashDb.snap.editable !== d.editable;
        dashDb.failed = "";
        dashDb.refreshFailed = "";
        dashDb.snap = d;
        dashDbDraw(changed && !dashDb.dirty);
    });
}

function dashDbSummary(c) {
    if (!c) return "";
    if (c.backend === "sqlite") return tHtml("settings.database.the-sqlite-file", { path: dashCode(c.path || "") });
    var where = dashCode((c.user || "?") + "@" + (c.host || "?") + ":" + (c.port || "") + "/" + (c.database || "?"));
    if (!c.sslmode) return tHtml("settings.database.postgresql", { where: where });
    return tHtml(c.caPem ? "settings.database.with-a-ca-certificate" : "settings.database.tls", { where: where, mode: uiIdWord("db.sslmode.", c.sslmode) });
}

function dashDbFormHtml() {
    var s = dashDb.snap;
    var c = s.connection;
    var now = '<p class="ui-hint">' + tHtml("settings.database.passwords-are-never-shown", { summary: tRaw(dashDbSummary(c)) }) + "</p>";
    if (s.locked) {
        return '<div class="ui-form"><div class="ui-field"><span class="ui-label">' + esc(t("settings.database.current-connection")) + '</span>' + now +
            dashSetLocked({ envName: s.envName }, c.backend === "sqlite" ? t("settings.database.sqlite") : t("settings.database.postgresql-2")) + "</div></div>";
    }
    if (!s.editable) {
        return '<div class="ui-form"><div class="ui-field"><span class="ui-label">' + esc(t("settings.database.current-connection")) + '</span>' + now +
            '<p class="ui-hint">' + esc(t("settings.database.chosen-in-setup-back-up-the")) + '</p></div></div>';
    }
    return '<form class="ui-form" id="db-form" novalidate autocomplete="off"><fieldset class="ui-settings-fields" id="db-fields">' +
        '<div class="ui-field"><span class="ui-label">' + esc(t("settings.database.current-connection")) + '</span>' + now +
        '<p class="ui-hint">' + esc(t("settings.database.change-it-for-a-new-password")) + "</p></div>" +
        uiDbFieldsHtml("dbs-", { host: c.host, port: c.port, database: c.database, user: c.user, sslmode: c.sslmode, caSaved: c.caPem }, true) +
        '<div id="dbs-result" aria-live="polite"></div>' +
        '<label class="ui-check-label hidden" id="dbs-confirm-row"><input type="checkbox" class="ui-check" id="dbs-confirm" /> <span id="dbs-confirm-text"></span></label>' +
        '<p class="ui-error hidden" id="dbs-err" role="alert"></p>' +
        '<div class="ui-settings-actions"><button type="button" class="ui-btn" id="dbs-check" data-inventory="settings.database.check">' + esc(t("settings.database.check-connection")) + '</button>' +
        '<button type="submit" class="ui-btn ui-btn-primary" id="dbs-save" data-inventory="settings.database.save">' + esc(t("settings.database.save")) + '</button>' +
        '<button type="button" class="ui-btn hidden" id="dbs-discard">' + esc(t("settings.database.discard-changes")) + '</button></div>' +
        "</fieldset></form>";
}

function dashDbDraw(redraw) {
    var body = document.getElementById("db-body");
    if (!body) return;
    if (!dashDb.snap) {
        body.innerHTML = dashDb.failed ? dashFailed(t("settings.database.could-not-load-the-database-connection"), dashDb.failed, "data-db-retry") : dashLoading(t("settings.database.loading-the-database-connection"));
        return;
    }
    if (redraw || !body.firstChild || body.querySelector(".ui-loading, .ui-failed")) body.innerHTML = dashDbFormHtml();
    var form = document.getElementById("db-form");
    if (!form) return;
    document.getElementById("db-fields").disabled = !!dashDb.busy;
    document.getElementById("dbs-check").textContent = dashDb.busy === "check" ? t("settings.database.checking") : t("settings.database.check-connection");
    document.getElementById("dbs-save").textContent = dashDb.busy === "save" ? t("settings.database.saving") : t("settings.database.save");
    document.getElementById("dbs-discard").classList.toggle("hidden", !dashDb.dirty || !!dashDb.busy);
    document.getElementById("dbs-result").innerHTML = uiDbNoteHtml(uiDbCheckNote(dashDb.check, "settings"));
    var row = document.getElementById("dbs-confirm-row");
    row.classList.toggle("hidden", !dashDb.askConfirm);
    document.getElementById("dbs-confirm-text").textContent = dashDb.askConfirm;
    document.getElementById("dbs-confirm").checked = dashDb.confirm;
    var err = document.getElementById("dbs-err");
    var text = dashDb.error || (dashDb.refreshFailed ? t("settings.database.could-not-refresh-the-database-connection", { reason: dashDb.refreshFailed }) : "");
    err.textContent = text;
    err.classList.toggle("hidden", !text);
}

// What the confirmation box says for a check result ("" when none is needed)
function dashDbConfirmText(r) {
    if (r && r.ok && r.identity === "different_copy") return t("settings.database.use-this-copy-changes-made-after");
    if (r && r.ok && r.state === "older") return t("settings.database.adopt-this-database-changes-made-after");
    return "";
}

function dashDbFail(text, field) {
    dashDb.error = text;
    dashDbDraw(false);
    if (field) uiDbFieldError("dbs-", field);
}

function dashDbCheck() {
    var fields = uiDbFieldsRead("dbs-");
    var key = JSON.stringify(fields) + "|" + !!document.getElementById("dbs-password").value;
    dashDb.busy = "check";
    dashDb.error = "";
    dashDbDraw(false);
    return api("POST", "/admin/api/settings/database/check", { postgres: fields }).then(function (res) {
        dashDb.busy = "";
        if (!res.ok) {
            dashDb.check = null;
            if (res.status !== 401) dashDbFail(apiError(res, t("settings.database.the-check-failed")), res.data && res.data.field);
            return null;
        }
        dashDb.check = res.data;
        dashDb.checkedFor = key;
        var ask = dashDbConfirmText(res.data);
        if (ask !== dashDb.askConfirm) dashDb.confirm = false;
        dashDb.askConfirm = ask;
        dashDbDraw(false);
        return res.data;
    });
}

function dashDbSave() {
    if (dashDb.busy || !dashDb.snap || !dashDb.snap.editable) return;
    var fields = uiDbFieldsRead("dbs-");
    var key = JSON.stringify(fields) + "|" + !!document.getElementById("dbs-password").value;
    var checked = dashDb.check && dashDb.checkedFor === key ? Promise.resolve(dashDb.check) : dashDbCheck();
    checked.then(function (r) {
        if (!r) return;
        var note = uiDbCheckNote(r, "settings");
        if (!r.ok || (note && note.tone === " is-fail")) {
            dashDbFail(t("settings.database.nothing-was-saved", { reason: note ? note.title.toLowerCase() : t("settings.database.the-check-failed-2") }));
            return;
        }
        if (dashDb.askConfirm && !dashDb.confirm) {
            dashDbFail(t("settings.database.tick-the-box-to-confirm-then"));
            document.getElementById("dbs-confirm").focus();
            return;
        }
        var body = { postgres: fields };
        if (dashDb.askConfirm) body.confirmCopy = true;
        dashStepUpDialog({
            title: t("settings.database.confirm-the-change"),
            text: t("settings.database.enter-your-6-digit-authenticator-code"),
            pkText: t("settings.database.use-a-passkey-or-your-6"),
            submitText: t("settings.database.verify-save"),
            send: function (proof) {
                var b = {};
                Object.keys(body).forEach(function (k) { b[k] = body[k]; });
                Object.keys(proof).forEach(function (k) { b[k] = proof[k]; });
                return api("PUT", "/admin/api/settings/database", b);
            },
            done: function (res) { dashDbRun(res.data.job); },
        });
    });
}

// The operation runs on the server; follow it until it ends
function dashDbRun(job) {
    dashDb.busy = "save";
    dashDb.error = "";
    dashDbDraw(false);
    uiMutated();
    var poll = function () {
        api("GET", "/admin/api/settings/database/jobs/" + encodeURIComponent(job.id)).then(function (res) {
            var j = res.ok && res.data && res.data.job;
            // Saved, but the service could not start on the new connection: recovery mode
            if (res.status === 503 && res.data && res.data.code && res.data.code !== "database_switching") {
                window.location.href = basePath() + "/recovery";
                return;
            }
            if (!j || j.state === "running") {
                if (res.status === 401) { dashDb.busy = ""; return; }
                setTimeout(poll, 500);
                return;
            }
            dashDb.busy = "";
            if (j.state === "done") {
                dashDb.dirty = false;
                dashDb.check = null;
                dashDb.askConfirm = "";
                dashDb.confirm = false;
                dashDb.snap = null;
                dashDbDraw(true);
                dashDbLoad();
                showToast(j.result && j.result.warning ? j.result.warning : t("settings.database.database-connection-saved"), j.result && j.result.warning ? "warning" : "success");
                return;
            }
            if (j.code === "confirm_copy") {
                dashDb.askConfirm = t("settings.database.use-it-anyway-changes-made-after");
                dashDb.confirm = false;
            }
            dashDbFail(t("settings.database.nothing-was-changed", { reason: uiDbCodeText(j.code, j.error) }));
        });
    };
    poll();
}

// The second check before a sensitive change: an authenticator code or a passkey.
// o: title, text, pkText, submitText, send(proof) -> api promise, done(res)
function dashStepUpDialog(o) {
    var d = null;
    var closed = false;
    var close = function () {
        closed = true;
        if (d) d.backdrop.remove();
    };
    var formId = "stepup-form";
    d = uiDialog({
        title: o.title, size: "sm", close: close,
        body: '<div class="ui-form"><p id="stepup-text">' + esc(o.text) + "</p>" +
            '<div class="ui-field hidden" id="stepup-pk"><button type="button" class="ui-btn ui-btn-block" id="stepup-pk-btn">' + esc(t("settings.database.use-a-passkey")) + '</button>' +
            '<p class="ui-hint">' + esc(t("settings.database.or-enter-your-6-digit-code")) + '</p></div>' +
            '<div class="ui-field"><label class="ui-label" for="stepup-code">' + esc(t("settings.database.6-digit-code")) + '</label>' +
            '<input id="stepup-code" class="ui-input ui-code" inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="' + esc(t("settings.database.000000")) + '" required></div></div>',
        foot: '<p class="ui-error hidden" id="stepup-err" role="alert"></p><button type="button" class="ui-btn" data-cancel>' + esc(t("settings.database.cancel")) + '</button>' +
            '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">' + esc(o.submitText) + "</button>",
        form: { id: formId, onSubmit: function () { submit(); } },
    });
    d.foot.querySelector("[data-cancel]").addEventListener("click", close);
    d.backdrop.addEventListener("ui-drop", function () { closed = true; });
    var code = d.body.querySelector("#stepup-code");
    var err = d.foot.querySelector("#stepup-err");
    var btn = d.foot.querySelector("[type=submit]");
    var pkBtn = d.body.querySelector("#stepup-pk-btn");
    var busy = false;
    var setBusy = function (on) {
        busy = on;
        btn.disabled = on;
        pkBtn.disabled = on;
    };
    var fail = function (text) {
        if (!text) return;
        err.textContent = text;
        err.classList.remove("hidden");
    };
    var live = function () { return !closed && d.backdrop.isConnected; };
    if (!uiPasskeyBlocked(window, false)) {
        dashPkLoad(false).then(function () {
            if (!live() || !dashPkUsable()) return;
            d.body.querySelector("#stepup-pk").classList.remove("hidden");
            d.body.querySelector("#stepup-text").textContent = o.pkText;
        });
    }
    var send = function (proof, withCode) {
        o.send(proof).then(function (res) {
            if (!live()) return;
            setBusy(false);
            if (!res.ok) {
                if (res.status === 401) return;
                fail(apiError(res, t("settings.database.verification-failed")));
                if (withCode) code.select();
                return;
            }
            close();
            o.done(res);
        });
    };
    pkBtn.addEventListener("click", function () {
        if (busy) return;
        err.classList.add("hidden");
        setBusy(true);
        dashPkStepUp().then(function (r) {
            if (!live()) return;
            if (!r.passkey) {
                setBusy(false);
                fail(r.error);
                return;
            }
            send({ passkey: r.passkey, ceremony: r.ceremony }, false);
        });
    });
    function submit() {
        if (busy) return;
        var value = code.value.trim();
        if (!value) { fail(t("settings.database.enter-the-code-from-your-authenticator")); code.focus(); return; }
        err.classList.add("hidden");
        setBusy(true);
        send({ code: value }, true);
    }
}

function dashDbBind() {
    if (dashDb.bound) return;
    dashDb.bound = true;
    var panel = document.getElementById("db-panel");
    var edited = function (event) {
        var target = event.target;
        if (target.id === "dbs-confirm") {
            dashDb.confirm = target.checked;
            return;
        }
        if (!target.closest("#db-form")) return;
        if (target.id === "dbs-sslmode") uiDbFieldsSync("dbs-");
        dashDb.dirty = true;
        dashDb.error = "";
        if (dashDb.check) {
            dashDb.check = null;
            dashDb.askConfirm = "";
            dashDb.confirm = false;
        }
        dashDbDraw(false);
    };
    panel.addEventListener("input", edited);
    panel.addEventListener("change", edited);
    panel.addEventListener("submit", function (event) {
        if (event.target.id !== "db-form") return;
        event.preventDefault();
        dashDbSave();
    });
    panel.addEventListener("click", function (event) {
        var target = event.target;
        if (target.closest("[data-db-retry]")) {
            dashDb.failed = "";
            dashDbDraw(false);
            dashDbLoad();
        } else if (target.closest("#dbs-check")) {
            if (!dashDb.busy) dashDbCheck();
        } else if (target.closest("#dbs-discard")) {
            dashDb.dirty = false;
            dashDb.check = null;
            dashDb.askConfirm = "";
            dashDb.confirm = false;
            dashDb.error = "";
            dashDbDraw(true);
        }
    });
    uiUnsavedRegister(function () {
        return dashDb.dirty ? 1 : 0;
    }, function () {
        if (!dashDb.snap) return;
        dashDb.dirty = false;
        dashDb.check = null;
        dashDb.askConfirm = "";
        dashDbDraw(true);
    });
}

DASH_PAGE_IMPL.settings = function () {
    dashSettingsBind();
    dashGenDraw();
    dashSettingsDraw();
    dashDbBind();
    dashDbDraw(false);
    dashRedisDraw();
    dashPkSetBind();
    dashPkSetDraw(false);
    Promise.all([dashSettingsLoadAll(), dashDbLoad(), dashPkSetLoad()]).then(function () { uiRefreshLoaded("settings"); });
};

registerPageRefresh("settings", 30000, function (isCurrent) {
    return Promise.all([dashSettingsLoadAll(isCurrent), dashDbLoad(isCurrent), dashPkSetLoad(isCurrent)]);
});

// ===========================================================================
// Passkeys: the signed-in admin's list (Account page, Copy token and Copy key),
// a check with one of them before a sensitive step, and the Account page
// ===========================================================================

var dashPk = { data: null, failed: "", pending: null, at: 0, seq: 0, drawn: "", bound: false };

// GET /admin/api/passkeys, kept 25 seconds; force reads it again
function dashPkLoad(force) {
    if (dashPk.pending && !force) return dashPk.pending;
    if (!force && dashPk.data && Date.now() - dashPk.at < 25000) return Promise.resolve(dashPk.data);
    var seq = ++dashPk.seq;
    var mark = uiMutationMark();
    var p = api("GET", "/admin/api/passkeys").then(function (res) {
        if (dashPk.pending === p) dashPk.pending = null;
        if (seq !== dashPk.seq || uiStaleSince(mark)) return dashPk.data;
        if (res.ok && res.data && res.data.address && Array.isArray(res.data.passkeys)) {
            dashPk.data = res.data;
            dashPk.at = Date.now();
            dashPk.failed = "";
        } else {
            dashPk.failed = apiError(res, t("account.passkeys.failed"));
        }
        return dashPk.data;
    });
    dashPk.pending = p;
    return p;
}

// The admin has a passkey made for the current passkey address
function dashPkUsable() {
    var list = dashPk.data && dashPk.data.passkeys;
    return !!list && list.some(function (p) { return p.usable; });
}

// Confirm with one of the admin's own passkeys. Resolves { passkey, ceremony } for
// the request that needs it, or { error } with the words to show ("" when the
// admin closed the browser's window).
function dashPkStepUp() {
    return api("POST", "/admin/api/passkeys/stepup/options").then(function (res) {
        if (!res.ok) {
            if (res.status === 409) dashPk.at = 0; // the list changed: read it again next time
            return { error: res.status === 401 ? "" : apiError(res, t("account.passkeys.could-not-start-the-passkey-check")) };
        }
        var publicKey;
        try {
            publicKey = PublicKeyCredential.parseRequestOptionsFromJSON(res.data.publicKey);
        } catch (e) {
            return { error: t("account.passkeys.this-browser-could-not-read-the") };
        }
        return navigator.credentials.get({ publicKey: publicKey }).then(function (credential) {
            if (!credential) return { error: "" };
            return { passkey: credential.toJSON(), ceremony: res.data.ceremony };
        }, function (err) {
            return { error: uiPasskeyErrorText(err, false) };
        });
    });
}

// The Account page's notes: the passkey address, and what this browser cannot do
function dashPkInfoHtml(data, blocked, here) {
    var alerts = [];
    var add = function (tone, title, sub) {
        alerts.push('<div class="ui-alert' + tone + '"><span class="ui-alert-bar" aria-hidden="true"></span><div><p>' + esc(title) + "</p><small>" + sub +
            "</small></div><span></span></div>");
    };
    var settings = '<a data-go="settings" href="' + esc(basePath() + PAGES.settings.path) + '">' + esc(t("account.passkeys.settings")) + '</a>';
    if (blocked === "insecure") {
        add(" is-warn", t(UI_PASSKEY_INSECURE), esc(t("account.passkeys.open-the-service-over-https-to")));
    } else if (blocked === "browser") {
        add(" is-warn", t(UI_PASSKEY_OLD_BROWSER), esc(t("account.passkeys.use-a-current-browser-to-add")));
    }
    var a = data && data.address;
    if (!a) return alerts.join("");
    if (!a.origin) {
        add("", t("account.passkeys.no-passkey-address-yet"), tHtml("account.passkeys.your-first-passkey-ties-passkeys-to", { here: dashCode(here), settings: tRaw(settings) }));
    } else if (a.origin !== here) {
        add(" is-warn", t("account.passkeys.this-page-is-open-at-another"), tHtml("account.passkeys.and-this-page-is-open-at", { origin: dashCode(a.origin), here: dashCode(here) }));
    } else {
        add("", t("account.passkeys.passkey-address"), a.locked
            ? tHtml("account.passkeys.environment-variable-sets-it", { origin: dashCode(a.origin), env: dashCode(a.envName || "PASSKEY_ORIGIN") })
            : tHtml("account.passkeys.you-can-change-it-on", { origin: dashCode(a.origin), settings: tRaw(settings) }));
    }
    return alerts.join("");
}

function dashPkRowHtml(p) {
    var tags = [];
    if (!p.usable) tags.push(dashTag(t("account.passkeys.not-usable-at-this-address-made", { address: p.rpId }), "warn"));
    tags.push(p.backedUp ? dashTag(t("account.passkeys.synced"), "ok") : dashTag(t("account.passkeys.not-backed-up"), ""));
    return '<div class="ui-tr" role="row" data-key="' + esc(p.id) + '" aria-label="' + esc(p.name) + '">' +
        '<div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + uiUserText(p.name) + "</span></div>" +
        '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("account.passkeys.created")) + ' </span>' + uiTime(p.createdAt) + "</div>" +
        '<div class="ui-td" role="cell"><span class="ui-unit">' + esc(t("account.passkeys.last-used")) + ' </span>' + (p.lastUsedAt ? uiTime(p.lastUsedAt) : '<span class="ui-muted">' + esc(t("account.passkeys.never")) + '</span>') + "</div>" +
        '<div class="ui-td ui-td-full ui-td-wrap" role="cell"><div class="ui-tags">' + tags.join("") + "</div></div>" +
        '<div class="ui-td ui-td-end" role="cell">' + dashMenuButton(p.name) + "</div></div>";
}

function dashPkDraw(quiet) {
    if (!dashPk.bound) return;
    var rowsEl = document.getElementById("pk-rows");
    var info = document.getElementById("pk-info");
    var blocked = uiPasskeyBlocked(window, true);
    var data = dashPk.data;
    var infoHtml = dashPkInfoHtml(data, blocked, window.location.origin);
    if (info.innerHTML !== infoHtml && !uiTextHeld(info)) uiKeepFocus(info, function () { info.innerHTML = infoHtml; });
    // Locked, not hidden, where this browser cannot add one: the reason is its tooltip
    // and the notes above say it too (FB-1)
    var add = document.getElementById("pk-add");
    add.disabled = !!blocked || !data;
    var lock = blocked === "insecure" ? t(UI_PASSKEY_INSECURE) : blocked ? t(UI_PASSKEY_OLD_BROWSER) : "";
    if (lock) add.setAttribute("title", lock);
    else add.removeAttribute("title");
    if (quiet && uiAreaHeld(rowsEl)) return;
    var html;
    if (!data) {
        html = dashPk.failed ? dashFailed(t("account.passkeys.could-not-load-your-passkeys"), dashPk.failed, "data-pk-retry") : dashLoading(t("account.passkeys.loading-passkeys"));
        document.getElementById("pk-count").textContent = "";
    } else {
        document.getElementById("pk-count").textContent = tn("account.passkeys.count-passkeys", data.passkeys.length);
        html = data.passkeys.length
            ? data.passkeys.map(dashPkRowHtml).join("")
            : dashEmpty(t("account.passkeys.no-passkeys-yet"), esc(t("account.passkeys.add-one-to-sign-in-with")));
    }
    if (html === dashPk.drawn) return;
    dashPk.drawn = html;
    uiKeepFocus(rowsEl, function () { rowsEl.innerHTML = html; });
}

function dashPkFind(id) {
    var list = (dashPk.data && dashPk.data.passkeys) || [];
    return list.filter(function (p) { return p.id === id; })[0] || null;
}

function dashPkReload(quiet) {
    return dashPkLoad(true).then(function () { dashPkDraw(quiet); });
}

function dashPkBind() {
    if (dashPk.bound) return;
    dashPk.bound = true;
    var add = document.getElementById("pk-add");
    add.addEventListener("click", function () {
        add.disabled = true;
        dashPkLoad(true).then(function (data) {
            dashPkDraw(false);
            if (!data) {
                showToast(t("account.passkeys.could-not-load-your-passkeys-2", { reason: dashPk.failed || t("account.passkeys.failed") }), "error");
                return;
            }
            dashPkAddDialog(data);
        });
    });
    document.getElementById("pk-rows").addEventListener("click", function (event) {
        if (event.target.closest("[data-pk-retry]")) {
            dashPk.failed = "";
            dashPkDraw(false);
            dashPkReload(false);
            return;
        }
        var menuBtn = event.target.closest("[data-row-menu]");
        var row = event.target.closest("[data-key]");
        if (!menuBtn || !row) return;
        var p = dashPkFind(row.getAttribute("data-key"));
        if (!p) return;
        uiMenu(menuBtn, [
            { label: t("account.passkeys.rename"), action: "passkey-rename", run: function () { dashPkRenameDialog(p); } },
            { label: t("account.passkeys.delete"), action: "passkey-delete", danger: true, run: function () { dashPkDelete(p); } },
        ], t("account.passkeys.actions-for", { name: p.name }));
    });
}

// Add a passkey, in one dialog: 1. the name, and the authenticator code or another
// passkey; 2. the browser creates the passkey (its own button press, since the
// browser may require one); 3. it is saved, or the name is fixed and saved again.
function dashPkAddDialog(data) {
    var d = null;
    var closed = false;
    var close = function () {
        closed = true;
        if (d) d.backdrop.remove();
    };
    var live = function () { return !closed && !!d && d.backdrop.isConnected; };
    var here = window.location.origin;
    var a = data.address || {};
    var withPasskey = dashPkUsable();
    var notice = "";
    if (!a.origin) {
        notice = '<div class="ui-alert is-warn"><span class="ui-alert-bar" aria-hidden="true"></span><div><p>' + esc(t("account.passkeys.this-passkey-sets-the-passkey-address")) + '</p><small>' +
            tHtml("account.passkeys.passkeys-will-work-at", { here: dashCode(here) }) + "</small></div><span></span></div>";
    } else if (a.origin !== here) {
        notice = '<div class="ui-alert is-warn"><span class="ui-alert-bar" aria-hidden="true"></span><div><p>' + esc(t("account.passkeys.this-page-is-open-at-another")) + '</p><small>' +
            tHtml("account.passkeys.so-the-browser-will-likely-refuse", { origin: dashCode(a.origin) }) + "</small></div><span></span></div>";
    }
    var formId = "pk-add-form";
    d = uiDialog({
        title: t("account.passkeys.add-a-passkey"), size: "sm", close: close,
        body: '<div class="ui-form">' +
            '<p class="ui-hint" id="pk-step" aria-live="polite"></p>' + notice +
            '<div class="ui-field"><label class="ui-label" for="pk-name">' + esc(t("account.passkeys.name")) + '</label>' +
            '<input id="pk-name" class="ui-input" required dir="auto" autocomplete="off" aria-describedby="pk-name-hint" data-inventory="account.passkey.name">' +
            '<span class="ui-hint" id="pk-name-hint">' + esc(t("account.passkeys.so-you-can-tell-your-passkeys")) + '</span></div>' +
            '<div class="ui-form" id="pk-s1">' +
            (withPasskey ? '<div class="ui-field"><button type="button" class="ui-btn ui-btn-block" id="pk-confirm-pk">' + esc(t("account.passkeys.confirm-with-a-passkey")) + '</button>' +
                '<span class="ui-hint">' + esc(t("account.passkeys.or-enter-your-6-digit-code")) + '</span></div>' : "") +
            '<div class="ui-field"><label class="ui-label" for="pk-code">' + esc(t("account.passkeys.6-digit-code")) + '</label>' +
            '<input id="pk-code" class="ui-input ui-code" inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="' + esc(t("account.passkeys.000000")) + '" data-inventory="account.passkey.code"></div></div>' +
            '<p class="hidden" id="pk-s2">' + esc(t("account.passkeys.press-create-passkey-then-follow-your")) + '</p>' +
            "</div>",
        foot: '<p class="ui-error hidden" id="pk-err" role="alert"></p><button type="button" class="ui-btn" data-cancel>' + esc(t("account.passkeys.cancel")) + '</button>' +
            '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '" id="pk-go">' + esc(t("account.passkeys.continue")) + '</button>',
        form: { id: formId, onSubmit: function () { next(); } },
    });
    d.foot.querySelector("[data-cancel]").addEventListener("click", close);
    d.backdrop.addEventListener("ui-drop", function () { closed = true; });
    var nameEl = d.body.querySelector("#pk-name");
    var codeEl = d.body.querySelector("#pk-code");
    var pkBtn = d.body.querySelector("#pk-confirm-pk");
    var go = d.foot.querySelector("#pk-go");
    var cancel = d.foot.querySelector("[data-cancel]");
    var err = d.foot.querySelector("#pk-err");
    var st = { step: 1, busy: false, publicKey: null, ceremony: "", credential: null };
    nameEl.value = uiPasskeyDefaultName(window.navigator);

    var fail = function (text, field) {
        if (!text) return;
        err.textContent = text;
        err.classList.remove("hidden");
        if (field) {
            field.setAttribute("aria-invalid", "true");
            field.focus();
        }
    };
    var clearFail = function () {
        err.classList.add("hidden");
        nameEl.removeAttribute("aria-invalid");
        codeEl.removeAttribute("aria-invalid");
    };
    var setBusy = function (on) {
        st.busy = on;
        go.disabled = on;
        if (pkBtn) pkBtn.disabled = on;
    };
    var show = function (step) {
        st.step = step;
        d.body.querySelector("#pk-step").textContent = step === 1 ? t("account.passkeys.step-1-of-2-confirm-it")
            : step === 2 ? t("account.passkeys.step-2-of-2-create-the") : t("account.passkeys.almost-done-save-the-passkey");
        d.body.querySelector("#pk-s1").classList.toggle("hidden", step !== 1);
        d.body.querySelector("#pk-s2").classList.toggle("hidden", step !== 2);
        nameEl.disabled = step === 2;
        go.textContent = step === 1 ? t("account.passkeys.continue") : step === 2 ? t("account.passkeys.create-passkey") : t("account.passkeys.save");
        go.classList.remove("hidden");
        cancel.textContent = t("account.passkeys.cancel");
    };
    var restart = function (text) {
        st.publicKey = null;
        st.ceremony = "";
        st.credential = null;
        codeEl.value = "";
        show(1);
        fail(text);
    };
    var nameOk = function () {
        var problem = uiPasskeyNameProblem(nameEl.value);
        if (problem) fail(problem, nameEl);
        return !problem;
    };

    // Step 1: the code or a passkey, then the options to create one
    var askOptions = function (body) {
        body.proposedOrigin = here;
        api("POST", "/admin/api/passkeys/options", body).then(function (res) {
            if (!live()) return;
            setBusy(false);
            if (!res.ok) {
                if (res.status === 401) return;
                fail(apiError(res, t("account.passkeys.could-not-start-adding-the-passkey")), "code" in body ? codeEl : null);
                if ("code" in body) codeEl.select();
                return;
            }
            try {
                st.publicKey = PublicKeyCredential.parseCreationOptionsFromJSON(res.data.publicKey);
            } catch (e) {
                fail(t("account.passkeys.this-browser-could-not-read-the-2"));
                return;
            }
            st.ceremony = res.data.ceremony;
            codeEl.value = "";
            show(2);
            go.focus({ preventScroll: true });
        });
    };
    if (pkBtn) {
        pkBtn.addEventListener("click", function () {
            if (st.busy || st.step !== 1) return;
            clearFail();
            if (!nameOk()) return;
            setBusy(true);
            dashPkStepUp().then(function (r) {
                if (!live()) return;
                if (!r.passkey) {
                    setBusy(false);
                    fail(r.error);
                    return;
                }
                askOptions({ passkey: r.passkey, ceremony: r.ceremony });
            });
        });
    }

    // Step 2: the browser creates the passkey
    var create = function () {
        setBusy(true);
        navigator.credentials.create({ publicKey: st.publicKey }).then(function (credential) {
            if (!live()) return;
            if (!credential) { setBusy(false); return; }
            st.credential = credential.toJSON();
            save();
        }, function (e) {
            if (!live()) return;
            setBusy(false);
            fail(uiPasskeyErrorText(e, true));
            if (e && e.name === "InvalidStateError") {
                // Nothing more to do here: this device already has one
                go.classList.add("hidden");
                cancel.textContent = t("account.passkeys.close");
            }
        });
    };

    // Step 3: save it under its name
    var save = function () {
        if (!nameOk()) { setBusy(false); show(3); return; }
        setBusy(true);
        uiMutated();
        api("POST", "/admin/api/passkeys", { name: uiPasskeyName(nameEl.value), credential: st.credential, ceremony: st.ceremony }).then(function (res) {
            if (!live()) {
                if (res.ok) dashPkReload(true);
                return;
            }
            setBusy(false);
            if (res.ok) {
                close();
                uiMutated();
                showToast(t("account.passkeys.passkey-added-you-can-now-sign"), "success");
                dashPkReload(false);
                return;
            }
            if (res.status === 401) return;
            if (res.status === 422 || res.status === 0 || res.status === 413) {
                // The browser made the passkey: fix the name or try again, then save it
                show(3);
                fail(apiError(res, t("account.passkeys.the-passkey-was-not-saved-try")), res.status === 422 ? nameEl : null);
                return;
            }
            restart(apiError(res, t("account.passkeys.the-passkey-was-not-saved-start")));
        });
    };

    function next() {
        if (st.busy) return;
        clearFail();
        if (st.step === 1) {
            if (!nameOk()) return;
            var value = codeEl.value.trim();
            if (!value) {
                fail(withPasskey ? t("account.passkeys.enter-the-code-from-your-authenticator") : t("account.passkeys.enter-the-code-from-your-authenticator-2"), codeEl);
                return;
            }
            setBusy(true);
            askOptions({ code: value });
        } else if (st.step === 2) {
            create();
        } else {
            save();
        }
    }
    show(1);
}

function dashPkRenameDialog(p) {
    var d = null;
    var close = function () { if (d) d.backdrop.remove(); };
    var guardedClose = function () { uiCloseGuarded(d, close); };
    var formId = "pk-rename-form";
    d = uiDialog({
        title: t("account.passkeys.rename-passkey"), size: "sm", close: guardedClose,
        body: '<div class="ui-form"><div class="ui-field"><label class="ui-label" for="pk-rename">' + esc(t("account.passkeys.name")) + '</label>' +
            '<input id="pk-rename" class="ui-input" required dir="auto" autocomplete="off" data-inventory="account.passkey.rename"></div></div>',
        foot: '<p class="ui-error hidden" id="pk-rename-err" role="alert"></p><button type="button" class="ui-btn" data-cancel>' + esc(t("account.passkeys.cancel")) + '</button>' +
            '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">' + esc(t("account.passkeys.save")) + '</button>',
        form: { id: formId, onSubmit: function () { submit(); } },
    });
    var nameEl = d.body.querySelector("#pk-rename");
    var err = d.foot.querySelector("#pk-rename-err");
    nameEl.value = p.name || "";
    uiGuardDialog(d);
    d.foot.querySelector("[data-cancel]").addEventListener("click", guardedClose);
    var busy = false;
    function submit() {
        if (busy) return;
        err.classList.add("hidden");
        nameEl.removeAttribute("aria-invalid");
        var problem = uiPasskeyNameProblem(nameEl.value);
        if (problem) {
            err.textContent = problem;
            err.classList.remove("hidden");
            nameEl.setAttribute("aria-invalid", "true");
            nameEl.focus();
            return;
        }
        busy = true;
        var btn = d.foot.querySelector("[type=submit]");
        btn.disabled = true;
        uiMutated();
        api("PATCH", "/admin/api/passkeys/" + encodeURIComponent(p.id), { name: uiPasskeyName(nameEl.value) }).then(function (res) {
            busy = false;
            btn.disabled = false;
            if (!res.ok) {
                if (res.status === 401) return;
                err.textContent = res.status === 404 ? t("account.passkeys.this-passkey-no-longer-exists-close") : apiError(res, t("account.passkeys.save-failed"));
                err.classList.remove("hidden");
                if (res.status === 404) dashPkReload(true);
                return;
            }
            close();
            uiMutated();
            showToast(t("account.passkeys.passkey-renamed"), "success");
            dashPkReload(false);
        });
    }
}

function dashPkDelete(p) {
    uiConfirm({
        title: t("account.passkeys.delete-passkey"),
        message: t("account.passkeys.delete-the-passkey", { name: p.name }),
        confirmText: t("account.passkeys.delete"), danger: true,
    }).then(function (ok) {
        if (!ok) return;
        uiMutated();
        api("DELETE", "/admin/api/passkeys/" + encodeURIComponent(p.id)).then(function (res) {
            if (!res.ok && res.status !== 404) {
                if (res.status !== 401) showToast(t("account.passkeys.could-not-delete-the-passkey", { reason: apiError(res, t("account.passkeys.failed")) }), "error");
                return;
            }
            showToast(t("account.passkeys.deleted", { name: p.name }), "success");
            uiMutated();
            dashPkReload(false);
        });
    });
}

DASH_PAGE_IMPL.account = function () {
    dashPkBind();
    dashPk.drawn = "";
    dashPkDraw(false);
    dashPkReload(false).then(function () { uiRefreshLoaded("account"); });
};

registerPageRefresh("account", 30000, function (isCurrent) {
    return dashPkLoad(true).then(function () { if (!isCurrent || isCurrent()) dashPkDraw(true); });
});

var DASH_SEARCH_ICON ='<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"/></svg>';

// The last script of the page: every word of the parsed page is in place (js/text.js)
if (typeof document !== "undefined" && document && typeof document.querySelectorAll === "function") uiApplyText(document, true);

if (typeof document !== "undefined" && document.readyState !== undefined) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", dashBoot);
    else dashBoot();
}
