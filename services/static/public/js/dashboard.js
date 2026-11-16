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
    settings: { path: "/admin/settings", title: "shell.settings", sections: true },
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
            // Settings > General decides how dates are written (FW-9)
            uiSetDatePrefs(me.data.dateFormat, me.data.timeFormat);
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
    // Wide screens: this page's title and actions go up into the top bar (NL-6)
    uiPlaceTopbar(id);
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
    dashBindShellWp3();
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

// Signing out asks first when something is unsaved, and drops the unsaved changes only
// once the sign-out worked; a failed sign-out keeps them guarded (SV-1)
function dashLogout() {
    var go = function () {
        return api("POST", "/admin/api/logout").then(function (res) {
            if (!res.ok && res.status !== 401) {
                showToast(t("shell.could-not-sign-out-try-again"), "error");
                return false;
            }
            uiUnsavedRelease();
            writeCsrf("");
            window.location.href = basePath() + "/login";
            return true;
        });
    };
    var n = uiUnsavedCount();
    if (!n) return go();
    return uiConfirm({
        title: t("common.leave-without-saving"), message: tn("common.leave-unsaved", n),
        confirmText: t("common.leave"), cancelText: t("common.stay"), danger: true, focusCancel: true, noHistory: true,
    }).then(function (ok) { return ok ? go() : false; });
}

// The phone's More sheet: every page not on the tab bar and the tools (NL-9)
var dashMore = null;
function dashMoreClose() {
    if (dashMore) dashMore.backdrop.remove();
    dashMore = null;
    var tab = document.getElementById("tab-more");
    if (tab) {
        tab.setAttribute("aria-expanded", "false");
        tab.removeAttribute("aria-controls");
    }
}
function dashMoreOpen() {
    if (dashMore && dashMore.backdrop.isConnected) { dashMoreClose(); return; }
    var d = null;
    var close = dashMoreClose;
    var link = function (id) {
        // The same icon and counter as in the sidebar (NL-9, NL-10)
        var navLink = document.querySelector('.ui-nav [data-page-link="' + id + '"]');
        var icon = navLink ? navLink.querySelector("svg") : null;
        return '<a class="ui-nav-item" data-page-link="' + id + '" href="' + esc(basePath() + PAGES[id].path) + '"' +
            (uiPageCurrent === id ? ' aria-current="page"' : "") + ">" + (icon ? icon.outerHTML : "") + '<span class="ui-nav-label">' + esc(t(PAGES[id].title)) + "</span>" +
            dashNavCountHtml(id) + "</a>";
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
    d.backdrop.id = "ui-more";
    var tab = document.getElementById("tab-more");
    if (tab) {
        tab.setAttribute("aria-expanded", "true");
        tab.setAttribute("aria-controls", "ui-more");
    }
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

// Preferences: the theme of this browser, and the zone dates are shown in. The
// Date Format and Time Format are server settings in Settings > General (FW-9)
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
            '<p class="ui-hint">' + esc(zoneNote) + "</p>" +
            '<p class="ui-hint"><a href="' + esc(basePath() + PAGES.settings.path + "/general") + '" id="pref-formats">' + esc(t("shell.date-formats-in-settings")) + "</a></p></div>",
        foot: '<button type="button" class="ui-btn" data-cancel>' + esc(t("shell.close")) + '</button>',
    });
    d.foot.querySelector("[data-cancel]").addEventListener("click", close);
    var segs = d.body.querySelectorAll(".ui-seg");
    segs[0].addEventListener("click", function (event) {
        var b = event.target.closest("button[data-value]");
        if (!b) return;
        window.uiThemeSet(b.getAttribute("data-value"));
        segs[0].querySelectorAll("button").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); });
    });
    // The link opens Settings > General in place of the dialog
    d.body.querySelector("#pref-formats").addEventListener("click", function (event) {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
        event.preventDefault();
        event.stopPropagation();
        close();
        dashSettings.section = "general";
        uiGo("settings");
    });
}

// Dates are drawn again in the new format or zone
function dashRedrawDates() {
    // The Settings sections write their saved times, and the formats example
    if (typeof DASH_SET_ORDER !== "undefined") {
        DASH_SET_ORDER.forEach(function (name) { if (DASH_SET_PANELS[name].st.snap) DASH_SET_PANELS[name].draw(); });
    }
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

// Each item opens where it is handled (NL-12): Database and Redis their Settings sections
function dashAttnItems() {
    var items = [];
    var h = dashAttn.health;
    if (h && h.db === false) {
        items.push({ tone: "fail", text: t("attention.database-is-not-responding"), sub: t("attention.tokens-cannot-be-checked-or-changed"),
            go: "settings", sub_: DASH_SETTINGS_SECTION.database, action: t("attention.open-database-settings") });
    }
    if (h && h.redis === false) {
        items.push({ tone: "fail", text: t("attention.redis-is-not-responding"), sub: t("attention.sessions-and-the-token-cache-need"),
            go: "settings", sub_: DASH_SETTINGS_SECTION.redis, action: t("attention.open-redis-settings") });
    }
    var exp = dashExpiryCount(dashTokenStore.rows, Date.now());
    if (exp.n > 0) {
        items.push({
            tone: exp.tone, text: tn("attention.tokens-expire-within-7-days", exp.n),
            sub: t("attention.replace-or-extend-them-before-they"), go: "tokens", query: "status=expiring", action: t("attention.open-tokens"),
        });
    }
    return items;
}

function dashExpiringCount() {
    return dashExpiryCount(dashTokenStore.rows, Date.now()).n;
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
            // The rail shows only the dot: the row says its state in its name (AX)
            var name = t("attention.service-state", { service: pair[0] === "db" ? t("attention.database") : t("attention.redis"), state: w[0] });
            row.setAttribute("title", name);
            row.setAttribute("aria-label", name);
        });
    });
    dashNavCountsDraw();
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
        document.querySelectorAll("[data-bell], [data-status-card]").forEach(function (b) {
            b.setAttribute("aria-expanded", "false");
            b.removeAttribute("aria-controls");
        });
    };
    var d = uiDialog({ title: t("attention.needs-attention-2"), size: "sm", backdropClass: "ui-attn-layer", close: close, backdropClose: close });
    d.backdrop.id = "ui-attn";
    dashAttn.dialog = d;
    dashAttn.bell = bell;
    bell.setAttribute("aria-expanded", "true");
    bell.setAttribute("aria-controls", "ui-attn");
    // Under the bell on a wide screen, its end edge in line with the bell's (NL-19);
    // opened from the sidebar's status card, beside the card
    var r = bell.getBoundingClientRect();
    var rtl = getComputedStyle(bell).direction === "rtl";
    if (bell.hasAttribute("data-status-card")) {
        d.backdrop.style.setProperty("--ui-attn-top", Math.round(r.top) + "px");
        d.backdrop.style.setProperty("--ui-attn-start", Math.round(rtl ? window.innerWidth - r.left + 8 : r.right + 8) + "px");
    } else {
        d.backdrop.style.setProperty("--ui-attn-top", Math.round(r.bottom + 8) + "px");
        d.backdrop.style.setProperty("--ui-attn-end", Math.max(16, Math.round(rtl ? r.left : window.innerWidth - r.right)) + "px");
    }
    dashAttnDraw();
    d.body.addEventListener("click", function (event) {
        var go = event.target.closest("[data-attn-go]");
        if (go) {
            var it = dashAttnItems()[Number(go.getAttribute("data-attn-go"))];
            if (!it) return;
            // Closed once the move is made; a move the user cancels over unsaved changes leaves it open
            uiGo(it.go, it.query, { sub: it.sub_ }).then(function (moved) { if (moved) close(); });
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
// WP3: shell bindings, nav counters (NL-10), help (DS-5) and the global search
// (SR-1 to SR-8)
// ===========================================================================

// The Settings sections that Needs attention opens (NL-12); ids of SETTINGS_INDEX
var DASH_SETTINGS_SECTION = { database: "database", redis: "redis" };

// A token that expires within this time turns its counter red (fixed threshold)
var TOKEN_EXPIRY_URGENT_MS = 24 * 3600 * 1000;

// Enabled tokens that expire within 7 days, and whether one expires within a day
function dashExpiryCount(rows, now) {
    var n = 0;
    var urgent = false;
    (rows || []).forEach(function (tok) {
        if (dashTokStatus(tok) !== "active" || !dashTokExpiring(tok, now)) return;
        n += 1;
        if (uiParseInstant(tok.expiresAt) - now <= TOKEN_EXPIRY_URGENT_MS) urgent = true;
    });
    return { n: n, tone: urgent ? "fail" : "warn" };
}

// The counter of a page in the sidebar, the rail and the More sheet: shown only
// above zero, amber or red by severity, with what it counts as its tooltip
function dashNavCountHtml(id) {
    if (id !== "tokens") return "";
    var c = dashExpiryCount(dashTokenStore.rows, Date.now());
    if (!c.n) return '<span class="ui-nav-count hidden" data-nav-count="tokens"></span>';
    return '<span class="ui-nav-count' + (c.tone === "fail" ? " is-fail" : "") + '" data-nav-count="tokens" title="' +
        esc(tn("attention.tokens-expire-within-7-days", c.n)) + '">' + esc(uiNum(c.n)) + "</span>";
}

function dashNavCountsDraw() {
    if (!uiHasDom()) return;
    var c = dashExpiryCount(dashTokenStore.rows, Date.now());
    var label = c.n ? tn("attention.tokens-expire-within-7-days", c.n) : "";
    document.querySelectorAll('[data-nav-count="tokens"]').forEach(function (el) {
        el.classList.toggle("hidden", !c.n);
        el.classList.toggle("is-fail", c.tone === "fail");
        if (el.classList.contains("ui-nav-count")) el.textContent = c.n ? uiNum(c.n) : "";
        if (label) el.setAttribute("title", label);
        else el.removeAttribute("title");
    });
}

function dashBindShellWp3() {
    var toggle = document.getElementById("ui-nav-toggle");
    if (toggle) toggle.addEventListener("click", uiNavToggle);
    uiNavToggleSync();
    // The help button beside every page title (NL-15)
    document.querySelectorAll("[data-help]").forEach(function (b) {
        var id = b.getAttribute("data-help");
        if (!PAGES[id]) return;
        var label = t("help.title", { page: t(PAGES[id].title) });
        b.setAttribute("aria-label", label);
        b.setAttribute("title", label);
        b.addEventListener("click", function () { dashHelpOpen(id); });
    });
    // The status card opens Needs attention (NL-12)
    document.querySelectorAll("[data-status-card]").forEach(function (card) {
        card.setAttribute("title", t("shell.service-status"));
        card.setAttribute("aria-expanded", "false");
        card.addEventListener("click", function () { dashAttnOpen(card); });
    });
    // The top bar and the phone search follow the width across 760px (NL-6, SR-8)
    if (window.matchMedia) {
        var mq = window.matchMedia("(max-width: 760px)");
        var cross = function () {
            dashSearchClose(false);
            uiPlaceTopbar();
        };
        if (mq.addEventListener) mq.addEventListener("change", cross);
    }
    dashSearchBind();
    window.addEventListener("ui:db-changed", dashSearchDbChanged);
}

// ---------------------------------------------------------------------------
// Help: one sheet per page from the shared dialog, with the reading gauge
// ---------------------------------------------------------------------------

// Each page's help, one paragraph per line of its catalog value
var DASH_HELP = {
    tokens: "help.tokens",
    zones: "help.zones",
    apikeys: "help.apikeys",
    activity: "help.activity",
    access: "help.access",
    usage: "help.usage",
    settings: "help.settings",
    account: "help.account",
};

function dashHelpParagraphs(id) {
    return t(DASH_HELP[id]).split(/\n+/).map(function (p) { return p.trim(); }).filter(Boolean);
}

function dashHelpOpen(id) {
    if (!DASH_HELP[id]) return null;
    return uiInfoDialog({ title: t("help.title", { page: t(PAGES[id].title) }), paragraphs: dashHelpParagraphs(id) });
}

// ---------------------------------------------------------------------------
// Global search: pages, Settings sections and fields (SETTINGS_INDEX), tokens,
// zones and API keys. Local only: tokens, zones and API keys come from the same
// shared rows the pages use. A kind marked stale (uiDataStale) or read more than
// 30 seconds ago is read again when the search opens or the query changes; a
// database change (ui:db-changed) drops what was read before it.
// ---------------------------------------------------------------------------

var DASH_SEARCH_MAX_AGE = 30000;
var DASH_SEARCH_SUMMARY = 3;
var DASH_SEARCH_LIMIT = 50;
var DASH_SEARCH_KINDS = [
    { id: "pages", label: "search.pages" },
    { id: "settings", label: "shell.settings" },
    { id: "tokens", label: "shell.tokens" },
    { id: "zones", label: "shell.zones" },
    { id: "apikeys", label: "shell.api-keys" },
];
var DASH_SEARCH_DATA = ["tokens", "zones", "apikeys"];
var DASH_SEARCH_PAGE_ICON = '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6"/></svg>';

// Where each kind's rows live: the shared stores of the pages
var DASH_SEARCH_SOURCES = {
    tokens: {
        rows: function () { return dashTokenStore.rows; },
        failed: function () { return dashTokenStore.failed; },
        load: function () { return dashTokensLoad(true); },
    },
    zones: {
        rows: function () { return dash.zonesLoaded ? dash.zones : null; },
        failed: function () { return dash.zonesFailed; },
        load: function () { return dashZonesLoad(); },
    },
    apikeys: {
        rows: function () { return dashKeys.rows; },
        failed: function () { return dashKeys.failed; },
        load: function () { return dashKeysLoad(true); },
    },
};

var dashSearch = { q: "", tab: "all", active: 0, shown: [], open: false, bound: false, data: {} };

// What the search holds of a kind: rows read in data generation gen
function dashSearchSlot(kind) {
    if (!dashSearch.data[kind]) dashSearch.data[kind] = { rows: null, gen: -1, failed: "", loading: false, seq: 0, retries: 0, pending: null };
    return dashSearch.data[kind];
}

// Read a kind again when it is stale or older than 30 seconds. Only the newest
// read of a kind applies, and never one that started before a database change.
function dashSearchRead(kind) {
    var slot = dashSearchSlot(kind);
    var src = DASH_SEARCH_SOURCES[kind];
    if (slot.gen !== uiDataGen && slot.rows) {
        slot.rows = null;
        slot.failed = "";
    }
    if (slot.loading) return slot.pending;
    if (slot.rows && !uiDataNeedsRead(kind, DASH_SEARCH_MAX_AGE)) return Promise.resolve();
    var mark = uiDataMark(kind);
    var before = src.rows();
    var seq = ++slot.seq;
    slot.loading = true;
    var done = function () {
        if (seq !== slot.seq) return;
        slot.loading = false;
        slot.pending = null;
        if (uiDataOld(mark)) {
            // The database changed while this read ran: read from the one now in use
            if (dashSearch.open) dashSearchRead(kind);
            return;
        }
        var rows = src.rows();
        if (rows && rows !== before) {
            slot.rows = rows;
            slot.gen = mark.gen;
            slot.failed = "";
            slot.retries = 0;
            uiDataRead(kind, mark);
        } else if (src.failed()) {
            slot.failed = src.failed();
        } else if (slot.retries < 2) {
            // A save started meanwhile and its answer was dropped: read once more
            slot.retries += 1;
            dashSearchRender();
            return dashSearchRead(kind);
        }
        dashSearchRender();
    };
    slot.pending = Promise.resolve().then(src.load).then(done, function () {
        if (seq !== slot.seq) return;
        slot.loading = false;
        slot.pending = null;
        slot.failed = slot.failed || t("search.failed");
        dashSearchRender();
    });
    return slot.pending;
}

// The database may have changed: what the search read is dropped, every kind is
// stale, answers that started before are ignored, the counters read again
function dashSearchDbChanged() {
    // The generation step (uiDataReset) is taken once by the dispatcher, before any listener runs
    DASH_SEARCH_DATA.forEach(function (kind) {
        var slot = dashSearchSlot(kind);
        slot.rows = null;
        slot.failed = "";
        slot.loading = false;
        slot.pending = null;
        slot.retries = 0;
        slot.seq += 1;
    });
    // The shared token rows serve the counters and the bell: their next read starts anew
    dashTokenStore.pending = null;
    dashTokenStore.at = 0;
    if (dashSearch.open) dashSearchRefresh();
    dashSearchRender();
    uiRefreshNow("attention");
}

// Read what is stale for the open search
function dashSearchRefresh() {
    return Promise.all(DASH_SEARCH_DATA.map(dashSearchRead));
}

function dashSearchHas(text, q) {
    return String(text === null || text === undefined ? "" : text).toLowerCase().indexOf(q) !== -1;
}

function dashSearchList(items) {
    return { items: items.slice(0, DASH_SEARCH_LIMIT), count: items.length, loading: false, failed: "" };
}

// The results of a query per kind: { items, count, loading, failed }. Pure: it
// reads the catalog, PAGES, SETTINGS_INDEX and what the search holds.
function dashSearchResults(query) {
    var q = String(query || "").trim().toLowerCase();
    var out = {};
    if (!q) return out;
    out.pages = dashSearchList(Object.keys(PAGES).filter(function (id) { return dashSearchHas(t(PAGES[id].title), q); }).map(function (id) {
        return { kind: "pages", icon: id, title: t(PAGES[id].title), sub: "", page: id };
    }));
    // Settings by their shown name first, then by their field id (SR-1); the index
    // comes from the Settings page (SETTINGS_INDEX), absent until it defines one
    var index = typeof SETTINGS_INDEX !== "undefined" && Array.isArray(SETTINGS_INDEX) ? SETTINGS_INDEX : [];
    var sections = {};
    index.forEach(function (e) { if (e && !e.field && typeof e.key === "string") sections[e.section] = t(e.key); });
    var byName = [];
    var byId = [];
    index.forEach(function (e) {
        if (!e || typeof e.section !== "string" || !UI_SUB_ID.test(e.section)) return;
        var field = typeof e.field === "string" ? e.field : "";
        var name = typeof e.key === "string" && e.key ? t(e.key) : "";
        var item = { kind: "settings", icon: "settings", title: name || field || e.section, sub: field ? sections[e.section] || "" : "", section: e.section, field: field };
        if (dashSearchHas(name, q)) byName.push(item);
        else if (field && dashSearchHas(field, q)) {
            item.sub = field;
            byId.push(item);
        }
    });
    out.settings = dashSearchList(byName.concat(byId));
    DASH_SEARCH_DATA.forEach(function (kind) {
        var slot = dashSearchSlot(kind);
        var rows = slot.gen === uiDataGen ? slot.rows : null;
        if (!rows) {
            out[kind] = { items: [], count: 0, loading: !slot.failed, failed: slot.failed };
            return;
        }
        var hits = [];
        rows.forEach(function (r) {
            if (!r) return;
            if (kind === "tokens" && (dashSearchHas(r.name, q) || dashSearchHas(r.description, q))) {
                hits.push({ kind: kind, icon: kind, title: String(r.name || ""), sub: r.description ? String(r.description) : t(TOKEN_STATUS[dashTokStatus(r)].label), id: r.id });
            } else if (kind === "zones" && (dashSearchHas(r.name, q) || dashSearchHas(r.description, q))) {
                hits.push({ kind: kind, icon: kind, title: String(r.name || ""), sub: String(r.description || ""), id: r.id });
            } else if (kind === "apikeys" && dashSearchHas(r.name, q)) {
                hits.push({ kind: kind, icon: kind, title: String(r.name || ""), sub: String(r.scopes || "").split(",").filter(Boolean).join(", "), id: r.id });
            }
        });
        out[kind] = dashSearchList(hits);
    });
    return out;
}

// The text with the first case-insensitive match of q marked, escaped
function dashSearchMark(text, q) {
    var value = String(text === null || text === undefined ? "" : text);
    var lower = value.toLowerCase();
    var at = q && lower.length === value.length ? lower.indexOf(q) : -1;
    if (at < 0) return esc(value);
    return esc(value.slice(0, at)) + "<mark>" + esc(value.slice(at, at + q.length)) + "</mark>" + esc(value.slice(at + q.length));
}

function dashNavIcon(id) {
    if (!uiHasDom()) return "";
    var link = document.querySelector('.ui-nav [data-page-link="' + id + '"]');
    var icon = link ? link.querySelector("svg") : null;
    return icon ? icon.outerHTML : DASH_SEARCH_PAGE_ICON;
}

function dashSearchItemHtml(item, q, index, active) {
    return '<button type="button" class="ui-gs-item' + (active ? " is-active" : "") + '" id="ui-gs-opt-' + index + '" data-gs-index="' + index +
        '" role="option" tabindex="-1" aria-selected="' + active + '"><span class="ui-gs-ic">' + dashNavIcon(item.icon === "pages" ? item.page : item.icon) + "</span>" +
        '<b><bdi dir="auto">' + dashSearchMark(item.title, q) + "</bdi></b><small>" + (item.sub ? '<bdi dir="auto">' + dashSearchMark(item.sub, q) + "</bdi>" : "") + "</small></button>";
}

function dashSearchCountText(r) {
    if (!r) return "";
    if (r.loading) return "…";
    if (r.failed) return "!";
    return uiNum(r.count);
}

function dashSearchRender() {
    if (!uiHasDom()) return;
    var root = document.getElementById("ui-gs");
    var panel = document.getElementById("ui-gs-panel");
    var input = document.getElementById("ui-gs-input");
    if (!root || !panel || !input) return;
    var q = dashSearch.q;
    root.classList.toggle("has-q", !!q);
    dashSearch.shown = [];
    if (!q || !dashSearch.open) {
        panel.innerHTML = "";
        input.removeAttribute("aria-activedescendant");
        input.setAttribute("aria-expanded", "false");
        return;
    }
    var lq = q.trim().toLowerCase();
    var res = dashSearchResults(q);
    var loading = DASH_SEARCH_KINDS.some(function (k) { return res[k.id] && res[k.id].loading; });
    var total = DASH_SEARCH_KINDS.reduce(function (n, k) { return n + (res[k.id] ? res[k.id].count : 0); }, 0);
    if (dashSearch.tab !== "all" && !res[dashSearch.tab]) dashSearch.tab = "all";
    var item = function (it) {
        var index = dashSearch.shown.push(it) - 1;
        return dashSearchItemHtml(it, lq, index, index === dashSearch.active);
    };
    var tab = function (id, label, count, empty) {
        var on = dashSearch.tab === id;
        return '<button type="button" class="ui-gs-tab' + (empty ? " is-empty" : "") + '" role="tab" id="ui-gs-tab-' + id + '" data-gs-tab="' + id +
            '" aria-selected="' + on + '" tabindex="' + (on ? "0" : "-1") + '"><span>' + esc(label) + '</span><span class="ui-gs-n">' + esc(count) + "</span></button>";
    };
    var tabs = tab("all", t("search.all"), loading ? "…" : uiNum(total), false) + DASH_SEARCH_KINDS.map(function (k) {
        var r = res[k.id];
        return tab(k.id, t(k.label), dashSearchCountText(r), r && !r.loading && !r.count);
    }).join("");
    var body = "";
    if (dashSearch.tab === "all") {
        DASH_SEARCH_KINDS.forEach(function (k) {
            var r = res[k.id];
            if (!r || !r.count) return;
            body += '<div class="ui-gs-sec" role="presentation"><span>' + esc(t("search.section", { kind: t(k.label), n: uiNum(r.count) })) + "</span>" +
                (r.count > DASH_SEARCH_SUMMARY ? '<button type="button" class="ui-btn-link" data-gs-tab="' + k.id + '">' + esc(t("search.see-all")) + "</button>" : "") + "</div>" +
                r.items.slice(0, DASH_SEARCH_SUMMARY).map(item).join("");
        });
        if (!body) body = '<p class="ui-gs-empty" role="status">' + esc(loading ? t("search.searching") : t("search.nothing-found", { q: q.trim() })) + "</p>";
        else if (loading) body += '<p class="ui-gs-wait" role="status">' + esc(t("search.still-searching")) + "</p>";
    } else {
        var r = res[dashSearch.tab];
        if (r.loading) body = '<p class="ui-gs-empty" role="status">' + esc(t("search.searching")) + "</p>";
        else if (r.failed && !r.count) body = '<p class="ui-gs-empty" role="status">' + esc(t("search.failed")) + "</p>";
        else if (!r.count) body = '<p class="ui-gs-empty" role="status">' + esc(t("search.nothing-found", { q: q.trim() })) + "</p>";
        else {
            body = r.items.map(item).join("");
            if (r.count > r.items.length) body += '<p class="ui-gs-wait">' + esc(t("search.first-of", { shown: uiNum(r.items.length), n: uiNum(r.count) })) + "</p>";
        }
    }
    if (dashSearch.active >= dashSearch.shown.length) dashSearch.active = Math.max(0, dashSearch.shown.length - 1);
    var keys = '<div class="ui-gs-keys" aria-hidden="true">' +
        '<span><kbd class="is-up"></kbd><kbd class="is-down"></kbd><span>' + esc(t("search.hint-move")) + "</span></span>" +
        "<span><kbd>" + esc(t("search.key-tab")) + "</kbd><span>" + esc(t("search.hint-kind")) + "</span></span>" +
        "<span><kbd>" + esc(t("search.key-enter")) + "</kbd><span>" + esc(t("search.hint-open")) + "</span></span>" +
        "<span><kbd>" + esc(t("search.key-esc")) + "</kbd><span>" + esc(t("search.hint-close")) + "</span></span></div>";
    panel.innerHTML = '<div class="ui-gs-tabs" role="tablist" aria-label="' + esc(t("search.result-types")) + '">' + tabs + "</div>" +
        '<div class="ui-gs-body" role="listbox" id="ui-gs-list" aria-label="' + esc(t("search.results")) + '">' + body + "</div>" + keys;
    // The field keeps the focus while Up and Down move: a screen reader reads the active result
    input.setAttribute("aria-expanded", "true");
    var active = panel.querySelector(".ui-gs-item.is-active");
    if (active) {
        input.setAttribute("aria-activedescendant", active.id);
        var list = panel.querySelector(".ui-gs-body");
        if (active.offsetTop < list.scrollTop) list.scrollTop = active.offsetTop;
        else if (active.offsetTop + active.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = active.offsetTop + active.offsetHeight - list.clientHeight;
    } else {
        input.removeAttribute("aria-activedescendant");
    }
}

function dashSearchRun(text) {
    dashSearch.q = String(text || "");
    dashSearch.active = 0;
    if (!dashSearch.q.trim()) dashSearch.tab = "all";
    // Stale data is read again; each kind fills in when its answer arrives
    if (dashSearch.q.trim()) dashSearchRefresh();
    dashSearchRender();
}

// Phones: the search moves out of its bar and covers the screen (SR-8). The
// sheet is a layer of js/ui.js: Escape and Back close it through its Cancel.
function dashSearchSheet(on) {
    var root = document.getElementById("ui-gs");
    if (!root) return;
    if (on && root.parentElement !== document.body) {
        document.body.appendChild(root);
        root.classList.add("is-sheet");
        document.documentElement.classList.add("ui-gs-locked");
    } else if (!on && root.classList.contains("is-sheet")) {
        root.classList.remove("is-sheet");
        document.documentElement.classList.remove("ui-gs-locked");
        var bar = document.getElementById("ui-topbar");
        if (bar) bar.insertBefore(root, document.getElementById("ui-topbar-actions"));
    }
}

function dashSearchOpen() {
    var root = document.getElementById("ui-gs");
    if (!root) return;
    if (uiIsPhone()) dashSearchSheet(true);
    root.classList.add("is-open");
    dashSearch.open = true;
    dashSearchRefresh();
    dashSearchRender();
}

// clear: the field is emptied and the next search starts again on All
function dashSearchClose(clear) {
    var root = document.getElementById("ui-gs");
    var input = document.getElementById("ui-gs-input");
    if (!root || !input) return;
    if (clear) {
        input.value = "";
        dashSearch.tab = "all";
        dashSearch.q = "";
    }
    dashSearch.open = false;
    root.classList.remove("is-open");
    dashSearchRender();
    dashSearchSheet(false);
    if (document.activeElement === input) input.blur();
}

// A kind's tab starts on its first result
function dashSearchSetTab(id) {
    dashSearch.tab = id;
    dashSearch.active = 0;
    dashSearchRender();
}

function dashSearchStepTab(step) {
    var ids = ["all"].concat(DASH_SEARCH_KINDS.map(function (k) { return k.id; }));
    dashSearchSetTab(ids[(ids.indexOf(dashSearch.tab) + step + ids.length) % ids.length]);
}

// Opening a result goes to it and shows where it landed (SR-6). A move the user
// stops over unsaved changes keeps the search open.
function dashSearchChoose(index) {
    var item = dashSearch.shown[index];
    if (!item) return Promise.resolve(false);
    var go;
    var find = null;
    if (item.kind === "pages") {
        go = uiGo(item.page);
    } else if (item.kind === "settings") {
        go = uiGo("settings", "", { sub: item.section, hash: item.field });
        if (item.field) {
            find = function () {
                var el = document.getElementById(item.field) || document.querySelector('[data-page="settings"] [name="' + (window.CSS && CSS.escape ? CSS.escape(item.field) : item.field) + '"]');
                return el && el.getClientRects().length ? el : null;
            };
        }
    } else {
        // A drill-down starts clean (NL-13): the list searched for exactly this name
        var page = item.kind === "apikeys" ? "apikeys" : item.kind;
        var q = item.kind === "apikeys" ? "" : "q=" + encodeURIComponent(item.title);
        var rowsId = { tokens: "tok-rows", zones: "zone-rows", apikeys: "key-rows" }[item.kind];
        go = uiGo(page, q);
        find = function () {
            var rows = document.getElementById(rowsId);
            if (!rows) return null;
            return Array.prototype.slice.call(rows.querySelectorAll("[data-key]")).filter(function (r) { return r.getAttribute("data-key") === item.id; })[0] || null;
        };
    }
    return go.then(function (moved) {
        if (!moved) return false;
        dashSearchClose(true);
        if (!find) return true;
        return uiWaitFor(find).then(function (el) {
            if (!el) return true;
            var field = el.tabIndex >= 0 ? el : null;
            uiReveal(el.closest(".ui-field, .ui-switch-row") || el);
            if (field) field.focus({ preventScroll: true });
            return true;
        });
    });
}

function dashSearchBind() {
    var root = document.getElementById("ui-gs");
    var input = document.getElementById("ui-gs-input");
    var panel = document.getElementById("ui-gs-panel");
    if (!root || !input || !panel || dashSearch.bound) return;
    dashSearch.bound = true;
    var sheet = function () { return root.classList.contains("is-sheet"); };
    input.addEventListener("focus", dashSearchOpen);
    input.addEventListener("input", function () { dashSearchRun(input.value); });
    // The keys work from the field, a tab or a result (SR-4)
    root.addEventListener("keydown", function (event) {
        var onTab = event.target instanceof Element && !!event.target.closest("[data-gs-tab]");
        var inPanel = panel.contains(event.target);
        if (event.target !== input && !inPanel) return;
        var n = dashSearch.shown.length;
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            if (!n) return;
            dashSearch.active = (dashSearch.active + (event.key === "ArrowDown" ? 1 : -1) + n) % n;
            dashSearchRender();
            if (inPanel && !sheet()) input.focus({ preventScroll: true });
        } else if ((event.key === "ArrowLeft" || event.key === "ArrowRight") && onTab) {
            event.preventDefault();
            var rtl = getComputedStyle(root).direction === "rtl";
            dashSearchStepTab((event.key === "ArrowRight") !== rtl ? 1 : -1);
            var on = panel.querySelector('.ui-gs-tab[aria-selected="true"]');
            if (on) on.focus();
        } else if (event.key === "Enter" && !inPanel) {
            event.preventDefault();
            dashSearchChoose(dashSearch.active);
        } else if (event.key === "Tab" && dashSearch.q.trim()) {
            event.preventDefault();
            dashSearchStepTab(event.shiftKey ? -1 : 1);
            if (inPanel && !sheet()) input.focus({ preventScroll: true });
        } else if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            dashSearchClose(true);
        }
    });
    // A click on a result or a tab keeps the focus in the field; on a phone a tab
    // lets the keyboard go so the results get the room
    panel.addEventListener("mousedown", function (event) {
        if (event.target instanceof Element && event.target.closest("button") && !sheet()) event.preventDefault();
    });
    panel.addEventListener("click", function (event) {
        var tabBtn = event.target.closest("[data-gs-tab]");
        if (tabBtn) {
            dashSearchSetTab(tabBtn.getAttribute("data-gs-tab"));
            if (sheet()) input.blur();
            else input.focus({ preventScroll: true });
            return;
        }
        var it = event.target.closest("[data-gs-index]");
        if (it) dashSearchChoose(Number(it.getAttribute("data-gs-index")));
    });
    var cancel = document.getElementById("ui-gs-cancel");
    if (cancel) cancel.addEventListener("click", function () { dashSearchClose(true); });
    var phone = document.getElementById("ui-gs-phone");
    if (phone) {
        phone.addEventListener("click", function () {
            dashSearchOpen();
            input.focus();
        });
    }
    // A click outside closes it and keeps the text; nothing closes it under a dialog
    document.addEventListener("mousedown", function (event) {
        if (!dashSearch.open || sheet() || uiOpenLayers().length) return;
        if (event.target instanceof Node && !root.contains(event.target)) dashSearchClose(false);
    });
    input.addEventListener("blur", function () {
        setTimeout(function () {
            if (dashSearch.open && !root.contains(document.activeElement) && !input.value && !sheet() && !uiOpenLayers().length) dashSearchClose(false);
        }, 0);
    });
    // "/" from anywhere that is not a field opens the search, never over a dialog
    document.addEventListener("keydown", function (event) {
        if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented) return;
        var target = event.target;
        if (target instanceof Element && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
        if (uiOpenLayers().length || uiPickerState.open) return;
        event.preventDefault();
        if (uiIsPhone()) {
            if (phone) phone.click();
        } else {
            input.focus();
        }
    });
}

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
    uiDataStale("tokens");
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
        uiDataStale("tokens");
        dashTokLoad(true);
    });
}

function dashTokDelete(tok) {
    uiConfirm({ title: t("tokens.delete-token"), message: t("tokens.delete-this-token-permanently"), confirmText: t("tokens.delete"), danger: true }).then(function (ok) {
        if (!ok) return;
        uiMutated();
        uiDataStale("tokens");
        api("DELETE", "/admin/api/tokens/" + encodeURIComponent(tok.id)).then(function (res) {
            if (!res.ok) {
                if (res.status !== 401) showToast(t("tokens.delete-failed"), "error");
                return;
            }
            showToast(t("tokens.deleted", { name: tok.name }), "success");
            uiMutated();
            uiDataStale("tokens");
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
function dashTokenDialog(tok) {
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
            ? '<div class="ui-field"><label class="ui-label" for="tf-token">' + esc(t("tokens.replace-token-optional")) + '</label>' +
              '<div class="ui-set-secret" id="tf-token-stored"><span>' + esc(t("settings.secret.stored")) + '</span><button type="button" class="ui-btn ui-btn-sm" id="tf-token-replace">' +
              esc(t("tokens.form.replace-token")) + "</button></div>" +
              '<div class="ui-set-replace hidden" id="tf-token-new"><input id="tf-token" class="ui-input ui-mono" name="token" autocomplete="off" spellcheck="false" placeholder="' + esc(t("tokens.leave-empty-to-keep-current-token")) + '" data-inventory="tokens.form.replace">' +
              '<button type="button" class="ui-btn ui-btn-sm" id="tf-token-keep">' + esc(t("settings.secret.keep")) + "</button></div></div>"
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
    if (editing) {
        // Replace shows the field and takes the focus; Keep drops what was typed
        var tokReplace = function (on) {
            if (!on) tokEl.value = "";
            f.querySelector("#tf-token-stored").classList.toggle("hidden", on);
            f.querySelector("#tf-token-new").classList.toggle("hidden", !on);
            (on ? tokEl : f.querySelector("#tf-token-replace")).focus();
        };
        f.querySelector("#tf-token-replace").addEventListener("click", function () { tokReplace(true); });
        f.querySelector("#tf-token-keep").addEventListener("click", function () { tokReplace(false); });
    }
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
        uiDataStale("tokens");
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
            uiDataStale("tokens");
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
    // A typed code is asked about before it is dropped (DS-9)
    var guardedClose = function () { uiCloseGuarded(d, close); };
    var formId = "mfa-form";
    var what = kind === "token" ? t("reveal.the-token") : t("reveal.the-key");
    d = uiDialog({
        title: t("reveal.mfa-verification"), size: "sm", close: guardedClose,
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
    uiGuardDialog(d);
    d.foot.querySelector("[data-cancel]").addEventListener("click", guardedClose);
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
    uiGuardReset(d);
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
        uiDataStale("zones");
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
            uiDataStale("zones");
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
        uiDataStale("zones");
        uiDataStale("tokens");
        api("DELETE", "/admin/api/zones/" + encodeURIComponent(z.id)).then(function (res) {
            if (!res.ok) {
                if (res.status !== 401) showToast(apiError(res, t("zones.delete-failed")), "error");
                return;
            }
            showToast(t("zones.deleted", { name: z.name }), "success");
            uiMutated();
            uiDataStale("zones");
            uiDataStale("tokens");
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
        uiDataStale("zones");
        uiDataStale("tokens");
        api("PATCH", "/admin/api/zones/" + encodeURIComponent(z.id), { name: name, description: descEl.value.trim() }).then(function (res) {
            btn.disabled = false;
            if (!res.ok) {
                if (res.status !== 401) { err.textContent = apiError(res, t("zones.save-failed")); err.classList.remove("hidden"); }
                return;
            }
            close();
            uiMutated();
            uiDataStale("zones");
            uiDataStale("tokens");
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
        uiDataStale("apikeys");
        api("DELETE", "/admin/api/api-keys/" + encodeURIComponent(k.id)).then(function (res) {
            if (res.ok) {
                showToast(t("apikeys.api-key-deleted"), "success");
                uiMutated();
                uiDataStale("apikeys");
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
        uiDataStale("apikeys");
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
            uiDataStale("apikeys");
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
    // Settings > General, Date Format and Time Format (WP2): the values are the format names
    display: [["dateFormat", "shell.date-format", ""], ["timeFormat", "shell.time-format", ""]],
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
    return (items.length ? items.join(", ") : '<span class="ui-muted">' + esc(t("logs.no-modifications")) + '</span>');
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
    sort: { key: "", dir: "" }, pick: "", zoom: null, drawn: "", drawnKind: "", shown: null, bound: false,
    gesture: null, redrawAfter: false, swallowUntil: 0, wheel: null, keySpan: null, keyTimer: null, dimTimer: null };

// CH-9, CH-10: the chart zooms over the bars the API returns, never finer, and
// shows at least this many bars
var USAGE_ZOOM_MIN = 2;
var USAGE_ICONS = {
    earlier: '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7"/></svg>',
    out: '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607zM13.5 10.5h-6"/></svg>',
    "in": '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607zM10.5 7.5v6m3-3h-6"/></svg>',
    later: '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7"/></svg>',
    reset: '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9M3.75 20.25v-4.5m0 4.5h4.5m-4.5 0L9 15M20.25 3.75h-4.5m4.5 0v4.5m0-4.5L15 9m5.25 11.25h-4.5m4.5 0v-4.5m0 4.5L15 15"/></svg>',
};
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
    // ?zoom=<start>-<end> in Unix seconds: the span the chart shows
    var z = /^(\d{1,12})-(\d{1,12})$/.exec(q.get("zoom") || "");
    dashUse.zoom = z && Number(z[1]) < Number(z[2]) ? { from: Number(z[1]) * 1000, to: Number(z[2]) * 1000 } : null;
}

function dashUseQuery(token, range, zoom) {
    var p = new URLSearchParams();
    if (token) p.set("token", token);
    if (range !== "7d") p.set("range", range);
    if (zoom) p.set("zoom", Math.floor(zoom.from / 1000) + "-" + Math.ceil(zoom.to / 1000));
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
    if (!whole) return "-";
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

// The bars a zoom covers, snapped to the bars there are: { a, b } as indices,
// both included. Nothing zoomed, or less than the shortest span left in the
// data, is the whole series (CH-10).
function dashUseSpan(series, zoom) {
    var n = series.length;
    var all = { a: 0, b: n - 1, zoomed: false };
    if (!zoom || n <= USAGE_ZOOM_MIN) return all;
    var a = -1;
    var b = -1;
    series.forEach(function (bar, i) {
        if (a === -1 && uiParseInstant(bar.end) > zoom.from) a = i;
        if (uiParseInstant(bar.start) < zoom.to) b = i;
    });
    if (a === -1 || b - a + 1 < USAGE_ZOOM_MIN || (a === 0 && b === n - 1)) return all;
    return { a: a, b: b, zoomed: true };
}

// len bars from a, kept inside the series and at least the shortest span
function dashUseClamp(a, len, n) {
    var l = Math.max(Math.min(USAGE_ZOOM_MIN, n), Math.min(n, Math.round(len)));
    var s = Math.max(0, Math.min(n - l, Math.round(a)));
    return { a: s, b: s + l - 1 };
}

// The zoom buttons (CH-9): Zoom in keeps the middle half, Zoom out doubles, Earlier
// and Later move by half the span, Reset shows every bar; all by whole bars
function dashUseZoomStep(v, n, kind) {
    var len = v.b - v.a + 1;
    if (kind === "in") {
        var l = Math.max(USAGE_ZOOM_MIN, Math.round(len / 2));
        return dashUseClamp(v.a + Math.floor((len - l) / 2), l, n);
    }
    if (kind === "out") {
        var wide = Math.min(n, len * 2);
        return dashUseClamp(v.a - Math.floor((wide - len) / 2), wide, n);
    }
    if (kind === "earlier") return dashUseClamp(v.a - Math.max(1, Math.floor(len / 2)), len, n);
    if (kind === "later") return dashUseClamp(v.a + Math.max(1, Math.floor(len / 2)), len, n);
    return { a: 0, b: n - 1 };
}

function dashUseZoomCan(v, n) {
    var len = v.b - v.a + 1;
    return { earlier: v.a > 0, out: len < n, "in": len > USAGE_ZOOM_MIN, later: v.b < n - 1, reset: len < n };
}

// The span shown in words: hours with their times, days by their dates
function dashUseSpanName(d, first, last) {
    var z = { seconds: false, zone: dashUseZone(d) };
    var hourly = d.range === "24h";
    return t("usage.span", { from: hourly ? formatTime(first.start, z) : formatDay(first.start, z), to: hourly ? formatTime(last.end, z) : formatDay(last.start, z) });
}

function dashUseSumWords(bars) {
    var allowed = 0;
    var denied = 0;
    bars.forEach(function (b) { allowed += b.allowed; denied += b.denied; });
    return t("usage.bucket", { calls: tn("usage.count-calls", allowed + denied), allowed: uiNum(allowed), denied: uiNum(denied) });
}

// A strip edge as a slider: its time, and the bounds the other edge leaves it (CH-10)
function dashUseEdge(d, span, which) {
    var s = d.series;
    var n = s.length;
    var z = { seconds: false, zone: dashUseZone(d) };
    var now = which === "start" ? uiParseInstant(s[span.a].start) : uiParseInstant(s[span.b].end);
    var min = which === "start" ? uiParseInstant(s[0].start) : uiParseInstant(s[Math.min(n - 1, span.a + USAGE_ZOOM_MIN - 1)].end);
    var max = which === "start" ? uiParseInstant(s[Math.max(0, span.b - USAGE_ZOOM_MIN + 1)].start) : uiParseInstant(s[n - 1].end);
    return { now: now, min: min, max: max, text: formatTime(now, z), left: ((which === "start" ? span.a : span.b + 1) / n * 100).toFixed(3) + "%" };
}

// CH-10: the whole period in small bars, with a window over the span the chart shows
function dashUseStripHtml(d, v) {
    var s = d.series;
    var n = s.length;
    if (n <= USAGE_ZOOM_MIN) return "";
    var max = 0;
    s.forEach(function (b) { max = Math.max(max, b.allowed + b.denied); });
    var bars = s.map(function (b) {
        // Time before the logs kept is hatched over the whole height (FB-9)
        if (b.coverage === "none") return '<i class="is-none" style="height:100%"></i>';
        return '<i style="height:' + Math.max(6, max ? ((b.allowed + b.denied) / max) * 100 : 0).toFixed(1) + '%"></i>';
    }).join("");
    var edge = function (which, key) {
        var e = dashUseEdge(d, v, which);
        return '<span class="ui-strip-edge is-' + which + '" role="slider" tabindex="0" data-edge="' + which + '" data-action="strip-' + which + '" aria-label="' + esc(t(key)) +
            '" aria-valuemin="' + e.min + '" aria-valuemax="' + e.max + '" aria-valuenow="' + e.now + '" aria-valuetext="' + esc(e.text) + '" style="left:' + e.left + '"></span>';
    };
    return '<div class="ui-strip" role="group" aria-label="' + esc(t("usage.strip.label")) + '">' +
        '<div class="ui-strip-bars" aria-hidden="true" style="--ui-chart-n:' + n + '">' + bars + "</div>" +
        '<div class="ui-strip-win" aria-hidden="true" style="left:' + (v.a / n * 100).toFixed(3) + "%;width:" + ((v.b - v.a + 1) / n * 100).toFixed(3) + '%"></div>' +
        edge("start", "usage.strip.start") + edge("end", "usage.strip.end") + "</div>";
}

// Allowed and denied calls per hour or day over the span shown (CH-1, CH-3): a
// readout of the picked bar or of the span, bars that pick on a click and show a
// tooltip on hover and focus, the zoom buttons (CH-9), the overview strip
// (CH-10), and the same numbers as a table for screen readers. Time runs left to
// right in every language.
function dashUseChartHtml(d) {
    var series = d.series;
    var v = dashUseSpan(series, dashUse.zoom);
    var shown = series.slice(v.a, v.b + 1);
    var n = shown.length;
    var max = 0;
    shown.forEach(function (b) { max = Math.max(max, b.allowed + b.denied); });
    var top = dashUseNiceMax(max);
    var hourly = d.range === "24h";
    var zone = { zone: dashUseZone(d) };
    var every = hourly ? 3 : n > 10 ? 5 : 1;
    var gaps = false;
    var picked = -1;
    shown.forEach(function (b, k) { if (dashUse.pick && b.start === dashUse.pick) picked = k; });
    // One Tab stop among the bars: the picked one, else the newest shown
    var stop = picked !== -1 ? picked : n - 1;
    // Tooltips of the bars at the ends line up with the chart's edge
    var tipEdge = Math.max(1, Math.round(n / 6));
    var cols = shown.map(function (b, k) {
        var cls = b.coverage === "none" ? " is-none" : b.coverage === "partial" ? " is-partial" : "";
        if (cls) gaps = true;
        cls += k < tipEdge ? " is-tip-start" : k >= n - tipEdge ? " is-tip-end" : "";
        var seg = function (val, tone) {
            return val ? '<span class="ui-chart-seg is-' + tone + '" style="height:' + ((val / top) * 100).toFixed(2) + '%"></span>' : "";
        };
        var words = dashUseBucketName(d, b) + ": " + dashUseBucketWords(d, b);
        return '<button type="button" class="ui-chart-col' + cls + '" data-chart-i="' + (v.a + k) + '" data-action="bar-' + esc(b.start) + '" aria-pressed="' + (k === picked) +
            '" tabindex="' + (k === stop ? "0" : "-1") + '" aria-label="' + esc(words) + '" data-tip="' + esc(words) + '">' + seg(b.denied, "fail") + seg(b.allowed, "base") + "</button>";
    }).join("");
    var ticks = shown.map(function (b, i) {
        // Count the labels back from the newest bucket shown, so the last one is always named
        var back = n - 1 - i;
        if (back % every) return "";
        // On a phone every other hour label goes, so the rest do not touch
        var minor = hourly && (back / every) % 2 ? ' class="is-minor"' : "";
        return "<span" + minor + ' style="grid-column:' + (i + 1) + '">' + esc(hourly ? formatClock(b.start, zone) : formatShortDay(b.start, zone)) + "</span>";
    }).join("");
    var rows = shown.map(function (b) {
        return "<tr><th scope=\"row\">" + esc(dashUseBucketName(d, b)) + "</th><td>" +
            (b.coverage === "none" ? esc(t("usage.no-data")) : esc(uiNum(b.allowed))) + "</td><td>" + (b.coverage === "none" ? esc(t("usage.no-data")) : esc(uiNum(b.denied))) + "</td></tr>";
    }).join("");
    var read = "";
    if (picked !== -1) {
        read = "<b>" + esc(dashUseBucketName(d, shown[picked])) + "</b> " + esc(dashUseBucketWords(d, shown[picked])) +
            ' <button type="button" class="ui-btn-link" data-chart-clear>' + esc(t("usage.chart.clear")) + "</button>";
    } else if (n) {
        read = "<b>" + esc(dashUseSpanName(d, shown[0], shown[n - 1])) + "</b> " + esc(dashUseSumWords(shown));
    }
    var can = dashUseZoomCan(v, series.length);
    var tool = function (kind, key) {
        var label = t(key);
        return '<button type="button" class="ui-icon-btn' + (kind === "reset" && !v.zoomed ? " is-gone" : "") + '" data-zoom-step="' + kind + '" data-action="zoom-' + kind +
            '" aria-label="' + esc(label) + '" title="' + esc(label) + '"' + (can[kind] ? "" : " disabled") + ">" + USAGE_ICONS[kind] + "</button>";
    };
    var tools = series.length > USAGE_ZOOM_MIN ? '<div class="ui-chart-zoom" role="group" aria-label="' + esc(t("usage.zoom.group")) + '">' +
        tool("earlier", "usage.zoom.earlier") + tool("out", "usage.zoom.out") + tool("in", "usage.zoom.in") + tool("later", "usage.zoom.later") + tool("reset", "usage.zoom.reset") + "</div>" : "";
    return '<div class="ui-chart">' +
        '<p class="ui-chart-read" id="usage-read" aria-live="polite">' + read + "</p>" +
        '<div class="ui-chart-plot">' +
        '<div class="ui-chart-y" aria-hidden="true"><span style="top:0%">' + esc(uiNum(top)) + '</span><span style="top:50%">' + esc(uiNum(top / 2)) +
        '</span><span style="top:100%">0</span></div>' +
        '<div class="ui-chart-view"><div class="ui-chart-bars' + (picked !== -1 ? " has-pick" : "") + '" role="group" aria-label="' + esc(t("usage.chart-label", { title: hourly ? t("usage.calls-per-hour") : t("usage.calls-per-day") })) + '" style="--ui-chart-n:' + n + '">' + cols + "</div></div>" +
        '<div class="ui-chart-x" aria-hidden="true" style="--ui-chart-n:' + n + '">' + ticks + "</div></div>" +
        tools + dashUseStripHtml(d, v) +
        '<p class="ui-chart-key"><span><i class="is-base"></i>' + esc(t("usage.allowed-2")) + '</span><span><i class="is-fail"></i>' + esc(t("usage.denied")) + '</span>' +
        (gaps ? '<span><i class="is-none"></i>' + esc(t("usage.logs-deleted")) + '</span>' : "") + "</p>" +
        '<table class="ui-sr"><caption>' + (hourly ? esc(t("usage.calls-per-hour")) : esc(t("usage.calls-per-day"))) + "</caption><thead><tr><th scope=\"col\">" +
        (hourly ? esc(t("usage.hour")) : esc(t("usage.day"))) + '</th><th scope="col">' + esc(t("usage.allowed-2")) + '</th><th scope="col">' + esc(t("usage.denied")) + '</th></tr></thead><tbody>' + rows + "</tbody></table></div>";
}

// A click on a bar picks it (the others fade); the same bar again drops the pick
function dashUsePick(start) {
    dashUse.pick = dashUse.pick === start ? "" : start;
    dashUseDraw(false);
}

// Show another span: the zoom goes into the address with the period, and a picked
// bar is dropped (CH-1, CH-9)
function dashUseZoomTo(span) {
    var d = dashUse.shown;
    if (!d || !d.series.length) return;
    var s = d.series;
    var n = s.length;
    var whole = span.a <= 0 && span.b >= n - 1;
    dashUse.zoom = whole ? null : { from: uiParseInstant(s[span.a].start), to: uiParseInstant(s[span.b].end) };
    dashUse.pick = "";
    dashUse.redrawAfter = false;
    uiSetPageQuery(new URLSearchParams(dashUseQuery(dashUse.token, dashUse.range, dashUse.zoom)));
    dashUseDraw(false);
}

// A zoom button: after the redraw the keyboard stays on that button, else on the
// first one that can still act
function dashUseZoomButton(kind) {
    var d = dashUse.shown;
    if (!d) return;
    dashUseZoomTo(dashUseZoomStep(dashUseSpan(d.series, dashUse.zoom), d.series.length, kind));
    var body = document.getElementById("usage-body");
    var a = document.activeElement;
    if (a && a !== document.body && body.contains(a) && !a.disabled) return;
    var usable = function (b) { return b && !b.disabled && !b.classList.contains("is-gone"); };
    var same = body.querySelector('[data-zoom-step="' + kind + '"]');
    var first = Array.prototype.filter.call(body.querySelectorAll("[data-zoom-step]"), usable)[0];
    var to = usable(same) ? same : first || body.querySelector('.ui-chart-col[tabindex="0"]');
    if (to) to.focus({ preventScroll: true });
}

// Move the strip's window and edges to a span before it is applied (a drag, keys)
function dashUseStripPreview(span) {
    var d = dashUse.shown;
    var strip = document.querySelector("#usage-body .ui-strip");
    if (!d || !strip) return;
    var n = d.series.length;
    var win = strip.querySelector(".ui-strip-win");
    win.style.left = (span.a / n * 100).toFixed(3) + "%";
    win.style.width = ((span.b - span.a + 1) / n * 100).toFixed(3) + "%";
    ["start", "end"].forEach(function (which) {
        var el = strip.querySelector('[data-edge="' + which + '"]');
        var e = dashUseEdge(d, span, which);
        el.style.left = e.left;
        el.setAttribute("aria-valuenow", String(e.now));
        el.setAttribute("aria-valuemin", String(e.min));
        el.setAttribute("aria-valuemax", String(e.max));
        el.setAttribute("aria-valuetext", e.text);
    });
}

// The strip's edges by keyboard: arrows one bar (Left and Down earlier), Page Up
// and Page Down a bigger step, Home and End to the ends; applied once, 350ms
// after the last key (CH-10)
function dashUseEdgeKey(event, which) {
    var d = dashUse.shown;
    if (!d) return;
    var n = d.series.length;
    var v = dashUse.keySpan || dashUseSpan(d.series, dashUse.zoom);
    var big = d.range === "24h" ? 6 : d.range === "30d" ? 7 : 1;
    var steps = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1, PageDown: -big, PageUp: big };
    var step = Object.prototype.hasOwnProperty.call(steps, event.key) ? steps[event.key] : 0;
    if (!step && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    var span = { a: v.a, b: v.b };
    var lo = which === "start" ? 0 : v.a + USAGE_ZOOM_MIN - 1;
    var hi = which === "start" ? v.b - USAGE_ZOOM_MIN + 1 : n - 1;
    var cur = which === "start" ? v.a : v.b;
    var next = event.key === "Home" ? lo : event.key === "End" ? hi : Math.max(lo, Math.min(hi, cur + step));
    if (which === "start") span.a = next;
    else span.b = next;
    dashUse.keySpan = span;
    dashUse.gesture = "keys";
    dashUseStripPreview(span);
    clearTimeout(dashUse.keyTimer);
    dashUse.keyTimer = setTimeout(function () {
        dashUse.keySpan = null;
        dashUse.gesture = null;
        dashUseZoomTo(span);
    }, 350);
}

function dashUseKpi(label, value, sub) {
    return '<div class="ui-kpi"><b>' + value + "</b><small>" + esc(label) + "</small>" + (sub ? "<span>" + sub + "</span>" : "") + "</div>";
}

// The share of calls denied, or a muted "-" when there were no calls (FB-8)
function dashUseDeniedSub(totals) {
    return totals.calls ? esc(t("usage.of-calls", { pct: dashUsePct(totals.denied, totals.calls) })) : '<span class="ui-muted">-</span>';
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
        dashUseKpi(t("usage.denied"), esc(uiNum(totals.denied)), dashUseDeniedSub(totals)) +
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
        dashUseKpi(t("usage.denied"), esc(uiNum(totals.denied)), dashUseDeniedSub(totals)) +
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

// The page shown while another period or token loads: kept, dimmed after 200ms (TB-5)
function dashUseDim(on) {
    var body = document.getElementById("usage-body");
    if (!body) return;
    clearTimeout(dashUse.dimTimer);
    if (on) {
        body.setAttribute("aria-busy", "true");
        dashUse.dimTimer = setTimeout(function () { body.classList.add("is-dim"); }, 200);
    } else {
        body.removeAttribute("aria-busy");
        body.classList.remove("is-dim");
    }
}

// A background refresh (quiet) leaves the page alone while the user works in it,
// and waits for a zoom gesture or strip drag to end (CH-10)
function dashUseDraw(quiet) {
    var body = document.getElementById("usage-body");
    if (!body) return;
    dashUseSyncRange();
    var mine = dashUse.dataFor === dashUseKey();
    // A failed refresh keeps the figures shown and says so above them (LD-12)
    dashFreshSet("usage-fresh", dashUse.at, mine && dashUse.data ? dashUse.failed : "");
    if (quiet && dashUse.gesture) {
        dashUse.redrawAfter = true;
        return;
    }
    var html;
    var kind = "content";
    if (mine && dashUse.notFound) html = dashUseNotFoundHtml();
    else if (mine && dashUse.data) html = dashUse.token ? dashUseTokenHtml(dashUse.data) : dashUseOverviewHtml(dashUse.data);
    else if (dashUse.failed) {
        html = '<div class="ui-panel">' + dashFailed(t("usage.could-not-load-the-usage"), dashUse.failed, "data-usage-retry") + "</div>";
        kind = "failed";
    } else if (dashUse.drawn && dashUse.drawnKind === "content") {
        dashUseDim(true);
        return;
    } else {
        html = '<div class="ui-panel">' + dashLoading(t("usage.loading-usage")) + "</div>";
        kind = "loading";
    }
    if (quiet && uiAreaHeld(body)) return;
    dashUseDim(false);
    dashUse.shown = mine ? dashUse.data : null;
    dashUse.drawnKind = kind;
    // The browser tab names the token whose usage is open (NL-3, WP3)
    uiSetSubTitle(dashUse.token && dashUse.shown && dashUse.shown.token ? dashUse.shown.token.name : "");
    if (html !== dashUse.drawn) {
        dashUse.drawn = html;
        uiKeepFocus(body, function () { body.innerHTML = html; });
    }
}

// A token's page keeps the period and the span shown
function dashUseOpen(key) {
    uiGo("usage", dashUseQuery(key, dashUse.range, dashUse.zoom));
}

function dashUseBind() {
    if (dashUse.bound) return;
    dashUse.bound = true;
    document.getElementById("usage-range").addEventListener("click", function (event) {
        var b = event.target.closest("[data-range]");
        if (!b || b.getAttribute("data-range") === dashUse.range) return;
        dashUse.range = b.getAttribute("data-range");
        // Another period: no picked bar and no zoom
        dashUse.pick = "";
        dashUse.zoom = null;
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
        if (target.closest("[data-chart-clear]")) {
            dashUse.pick = "";
            dashUseDraw(false);
            var stop = body.querySelector('.ui-chart-col[tabindex="0"]');
            if (stop) stop.focus({ preventScroll: true });
            return;
        }
        var step = target.closest("[data-zoom-step]");
        if (step) {
            dashUseZoomButton(step.getAttribute("data-zoom-step"));
            return;
        }
        var col2 = target.closest("[data-chart-i]");
        if (col2) {
            // The click that ends a drag, a pinch or a strip move picks nothing (CH-9)
            if (Date.now() < dashUse.swallowUntil) return;
            var bar = dashUse.shown && dashUse.shown.series[Number(col2.getAttribute("data-chart-i"))];
            if (bar) dashUsePick(bar.start);
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
            // Arrows move between the bars, left to right in every language; Enter
            // and Space pick (the bar is a button)
            var bars = Array.prototype.slice.call(body.querySelectorAll("[data-chart-i]"));
            var i = bars.indexOf(c);
            var to = event.key === "ArrowRight" ? i + 1 : event.key === "ArrowLeft" ? i - 1 : event.key === "Home" ? 0 : event.key === "End" ? bars.length - 1 : null;
            if (to === null) return;
            event.preventDefault();
            to = Math.max(0, Math.min(bars.length - 1, to));
            bars.forEach(function (b, k) { b.setAttribute("tabindex", k === to ? "0" : "-1"); });
            bars[to].focus({ preventScroll: true });
            return;
        }
        var edge = target.closest("[data-edge]");
        if (edge) {
            dashUseEdgeKey(event, edge.getAttribute("data-edge"));
            return;
        }
        if (target.matches("#usage-table [data-key]") && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            dashUseOpen(target.getAttribute("data-key"));
        }
    });
    dashUseGestures(body);
}

// CH-9 and CH-10 by pointer: a drag across the bars of an unzoomed chart marks a
// band and zooms to its bars; on a zoomed chart the drag moves it; two fingers
// pinch; Ctrl+wheel (and a trackpad pinch) zooms around the pointer. On the strip
// the window moves, an edge resizes, and a press beside the window centres it
// there. A move under 6px is a click; a mostly vertical one is left to the page.
// While the bars stretch and slide nothing is drawn; the span is applied, snapped
// to whole bars, when the gesture ends.
function dashUseGestures(body) {
    var g = null;
    var view = function () { return body.querySelector(".ui-chart-view"); };
    var preview = function (len, a, L, width) {
        var bars = body.querySelector(".ui-chart-bars");
        if (!bars) return;
        bars.style.transformOrigin = "0 0";
        bars.style.transform = "translateX(" + (-(a) * width / L).toFixed(1) + "px) scaleX(" + (len / L).toFixed(4) + ")";
    };
    var clear = function () {
        var bars = body.querySelector(".ui-chart-bars");
        if (bars) bars.style.transform = "";
        var v = view();
        if (v) v.classList.remove("is-moving");
        var strip = body.querySelector(".ui-strip");
        if (strip) strip.classList.remove("is-held");
    };
    var end = function (commit, event) {
        var gg = g;
        g = null;
        dashUse.gesture = null;
        if (!gg) return;
        if (gg.band) gg.band.remove();
        clear();
        var span = commit ? gg.span || null : null;
        if (commit && gg.kind === "strip" && !gg.moved && gg.mode === "center" && event) {
            var r = gg.el.getBoundingClientRect();
            var at = Math.floor(((event.clientX - r.left) / r.width) * gg.n);
            var len = gg.v.b - gg.v.a + 1;
            span = dashUseClamp(at - Math.floor(len / 2), len, gg.n);
        }
        if (gg.moved) dashUse.swallowUntil = Date.now() + 400;
        if (span && (span.a !== gg.v.a || span.b !== gg.v.b)) {
            dashUseZoomTo(span);
            return;
        }
        if (gg.kind === "strip") dashUseStripPreview(gg.v);
        settle();
    };
    // A refresh that came during the gesture is drawn now, after the click that may
    // follow this press (drawing first would take that click's bar away)
    var settle = function () {
        setTimeout(function () {
            if (dashUse.gesture || !dashUse.redrawAfter) return;
            dashUse.redrawAfter = false;
            dashUseDraw(false);
        }, 0);
    };
    body.addEventListener("pointerdown", function (event) {
        if (event.pointerType === "mouse" && event.button !== 0) return;
        var d = dashUse.shown;
        var target = event.target instanceof Element ? event.target : null;
        if (!d || !target || d.series.length <= USAGE_ZOOM_MIN) return;
        var onView = target.closest(".ui-chart-view");
        if (g && g.kind === "bars" && onView && event.pointerType === "touch" && !g.pinch && g.pointers.size === 1) {
            // A second finger: a pinch, keeping the time under the fingers
            g.pointers.set(event.pointerId, event.clientX);
            var xs = Array.from(g.pointers.values());
            if (g.band) { g.band.remove(); g.band = null; }
            g.pinch = { d0: Math.max(1, Math.abs(xs[0] - xs[1])), m0: (xs[0] + xs[1]) / 2 };
            g.moved = true;
            return;
        }
        // A gesture whose release was never seen (outside the window) is called off
        if (g) end(false, null);
        var strip = target.closest(".ui-strip");
        if (!onView && !strip) return;
        var edge = target.closest("[data-edge]");
        g = {
            kind: onView ? "bars" : "strip", el: onView || strip, id: event.pointerId, x0: event.clientX, y0: event.clientY,
            v: dashUseSpan(d.series, dashUse.zoom), n: d.series.length, moved: false, span: null, band: null, pinch: null,
            pointers: new Map([[event.pointerId, event.clientX]]),
            mode: edge ? edge.getAttribute("data-edge") : target.closest(".ui-strip-win") ? "move" : "center",
        };
        dashUse.gesture = g;
    });
    window.addEventListener("pointermove", function (event) {
        if (!g || !g.pointers.has(event.pointerId)) return;
        g.pointers.set(event.pointerId, event.clientX);
        var r = g.el.getBoundingClientRect();
        var len = g.v.b - g.v.a + 1;
        if (g.pinch) {
            var xs = Array.from(g.pointers.values());
            var k = Math.max(1, Math.abs(xs[0] - xs[1])) / g.pinch.d0;
            var L = Math.max(Math.min(USAGE_ZOOM_MIN, g.n), Math.min(g.n, len / k));
            var p = g.v.a + ((g.pinch.m0 - r.left) / r.width) * len;
            var a = Math.max(0, Math.min(g.n - L, p - (((xs[0] + xs[1]) / 2 - r.left) / r.width) * L));
            g.span = dashUseClamp(a, L, g.n);
            preview(len, a - g.v.a, L, r.width);
            return;
        }
        if (event.pointerId !== g.id) return;
        var dx = event.clientX - g.x0;
        var dy = event.clientY - g.y0;
        if (!g.moved) {
            if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return;
            // Mostly vertical: the page scrolls, nothing here moves
            if (Math.abs(dy) > Math.abs(dx) || (g.kind === "strip" && g.mode === "center")) {
                g = null;
                dashUse.gesture = null;
                settle();
                return;
            }
            g.moved = true;
            try { g.el.setPointerCapture(event.pointerId); } catch (e) { /* the gesture goes on without it */ }
            if (g.kind === "bars") g.el.classList.add("is-moving");
            else g.el.classList.add("is-held");
        }
        var per = r.width / (g.kind === "bars" ? len : g.n);
        if (g.kind === "bars" && !g.v.zoomed) {
            // A band over the bars it covers
            var x1 = Math.max(0, Math.min(r.width, g.x0 - r.left));
            var x2 = Math.max(0, Math.min(r.width, event.clientX - r.left));
            if (!g.band) {
                g.band = document.createElement("div");
                g.band.className = "ui-chart-band";
                g.el.appendChild(g.band);
            }
            g.band.style.left = Math.min(x1, x2) + "px";
            g.band.style.width = Math.abs(x2 - x1) + "px";
            var i1 = Math.min(len - 1, Math.floor(Math.min(x1, x2) / per));
            var i2 = Math.min(len - 1, Math.floor(Math.max(x1, x2) / per));
            g.span = i2 - i1 + 1 >= USAGE_ZOOM_MIN ? { a: g.v.a + i1, b: g.v.a + i2 } : null;
        } else if (g.kind === "bars") {
            // A zoomed chart moves with the pointer
            g.span = dashUseClamp(g.v.a - Math.round(dx / per), len, g.n);
            preview(len, -dx / per, len, r.width);
        } else {
            var shift = Math.round(dx / per);
            if (g.mode === "move") g.span = dashUseClamp(g.v.a + shift, len, g.n);
            else if (g.mode === "start") g.span = { a: Math.max(0, Math.min(g.v.b - USAGE_ZOOM_MIN + 1, g.v.a + shift)), b: g.v.b };
            else g.span = { a: g.v.a, b: Math.max(g.v.a + USAGE_ZOOM_MIN - 1, Math.min(g.n - 1, g.v.b + shift)) };
            dashUseStripPreview(g.span);
        }
    });
    window.addEventListener("pointerup", function (event) {
        if (!g || !g.pointers.has(event.pointerId)) return;
        // A pinch ends when the first finger lifts
        if (g.pinch || event.pointerId === g.id) end(true, event);
        else g.pointers.delete(event.pointerId);
    });
    window.addEventListener("blur", function () { if (g) end(false, null); });
    window.addEventListener("pointercancel", function (event) {
        // Called off: the chart and the strip return as they were
        if (g && g.pointers.has(event.pointerId)) end(false, null);
    });
    body.addEventListener("wheel", function (event) {
        if (!event.ctrlKey) return;
        var v = event.target instanceof Element ? event.target.closest(".ui-chart-view") : null;
        var d = dashUse.shown;
        if (!v || !d || d.series.length <= USAGE_ZOOM_MIN || g) return;
        event.preventDefault();
        var n = d.series.length;
        var w = dashUse.wheel || (dashUse.wheel = { v: dashUseSpan(d.series, dashUse.zoom), factor: 1, timer: null, span: null });
        var unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1;
        w.factor *= Math.exp(event.deltaY * unit * 0.004);
        var r = v.getBoundingClientRect();
        var f = Math.max(0, Math.min(1, (event.clientX - r.left) / r.width));
        var len = w.v.b - w.v.a + 1;
        var L = Math.max(Math.min(USAGE_ZOOM_MIN, n), Math.min(n, len * w.factor));
        var a = Math.max(0, Math.min(n - L, w.v.a + f * len - f * L));
        w.span = dashUseClamp(a, L, n);
        dashUse.gesture = "wheel";
        preview(len, a - w.v.a, L, r.width);
        clearTimeout(w.timer);
        w.timer = setTimeout(function () {
            dashUse.wheel = null;
            dashUse.gesture = null;
            clear();
            if (w.span.a !== w.v.a || w.span.b !== w.v.b) dashUseZoomTo(w.span);
            else if (dashUse.redrawAfter) { dashUse.redrawAfter = false; dashUseDraw(false); }
        }, 220);
    }, { passive: false });
}

DASH_PAGE_IMPL.usage = function () {
    dashUseBind();
    var before = dashUseKey();
    dashUseReadQuery();
    if (dashUseKey() !== before) {
        dashUse.pick = "";
        dashUse.failed = "";
    }
    dashUseDraw(false);
    dashUseLoad().then(function () { uiRefreshLoaded("usage"); });
};

registerPageRefresh("usage", 60000, function (isCurrent) { return dashUseLoad(isCurrent); });

// ===========================================================================
// WP4: the database in use changed (WP2 dispatches ui:db-changed after a switch,
// or after a recovery that may have changed it). The lists and Usage drop what
// they hold, answers to reads that started before are ignored, and the page
// shown reads its data again from the database now in use (P27).
// ===========================================================================

function dashListsDbChanged() {
    // Token, zone, API key and passkey reads under way publish nothing: the dispatcher took the
    // generation step (uiDataReset) before this listener runs
    var store = dashTokenStore;
    store.rows = null;
    store.at = 0;
    store.pending = null;
    store.pendingMark = -1;
    store.failed = "";
    dash.zones = [];
    dash.areas = [];
    dash.zonesLoaded = false;
    dash.zonesFailed = "";
    dashTok.drawn = "";
    dashZones.loaded = false;
    dashZones.at = 0;
    dashZones.drawn = "";
    dashKeys.rows = null;
    dashKeys.master = null;
    dashKeys.failed = "";
    dashKeys.at = 0;
    dashKeys.drawn = "";
    Object.keys(dashLogs).forEach(function (k) {
        var s = dashLogs[k];
        // A first load, a next page or a refresh under way is of the old list
        s.gen += 1;
        s.loadSeq += 1;
        s.loading = false;
        dashLogDim(s, false);
        s.rows = null;
        s.rowsFor = null;
        s.heads = {};
        s.nexts = {};
        s.pending = [];
        s.total = 0;
        s.hasMore = false;
        s.capped = false;
        s.failed = "";
        s.refreshFailed = "";
        s.moreFailed = "";
        s.at = 0;
        s.drawn = "";
    });
    dashUse.seq += 1;
    dashUse.data = null;
    dashUse.dataFor = "";
    dashUse.notFound = false;
    dashUse.failed = "";
    dashUse.at = 0;
    dashUse.pick = "";
    dashUse.zoom = null;
    dashUse.drawn = "";
    dashUse.shown = null;
    dashLimit.seq += 1;
    dashLimit.data = null;
    dashLimit.failed = "";
    dashLimit.at = 0;
    dashLimit.drawn = "";
    dashPk.seq += 1;
    dashPk.data = null;
    dashPk.failed = "";
    dashPk.pending = null;
    dashPk.at = 0;
    dashPk.drawn = "";
    // The page shown reads again; Needs attention counts from the new token list
    if (DASH_DB_LISTS.indexOf(uiPageCurrent) !== -1 && DASH_PAGE_IMPL[uiPageCurrent]) DASH_PAGE_IMPL[uiPageCurrent]({ fresh: true });
    uiRefreshNow("attention");
}

var DASH_DB_LISTS = ["tokens", "zones", "apikeys", "activity", "access", "usage", "account"];

if (typeof window !== "undefined" && typeof window.addEventListener === "function") window.addEventListener("ui:db-changed", dashListsDbChanged);

// ===========================================================================
// Settings: three panels, each with its own saved versions and Save. General
// (time zone, log retention, the session cookie), the rate limit and the
// addresses exempt from it, and the Redis connection. Each form edits a draft
// based on one saved version (base); a refresh never touches a changed draft,
// and the base only moves on a save or an explicit choice after a conflict.
// Settings (FS-8, FS-10, SV-1): sections with their own address
// (/admin/settings/<section>), one save bar over the save units, and the
// database connection change as its own flow. Each save unit edits a draft
// based on one saved version (base); a refresh never touches a changed draft
// or a part the admin works in, and the base only moves on a save or an
// explicit choice after a conflict.
// ===========================================================================

var DASH_LOCK_ICON = '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z"/></svg>';

// The sections, in the order of the section list (NL-3: /admin/settings/<id>)
var SETTINGS_SECTIONS = [
    { id: "general", group: "settings.nav.group-service", label: "settings.general", intro: "settings.section.general-intro" },
    { id: "rate-limit", group: "settings.nav.group-service", label: "settings.rate-limit", intro: "settings.section.rate-limit-intro" },
    { id: "passkeys", group: "settings.nav.group-service", label: "settings.passkeys", intro: "settings.section.passkeys-intro" },
    { id: "database", group: "settings.nav.group-connections", label: "settings.database", intro: "settings.section.database-intro" },
    { id: "redis", group: "settings.nav.group-connections", label: "settings.redis-connection", intro: "settings.section.redis-intro" },
];

// Every Settings section and field for the global search: the catalog key of its
// label; the address of a field is /admin/settings/<section>#<field>
var SETTINGS_INDEX = [
    { section: "general", field: "", key: "settings.general" },
    { section: "general", field: "gen-tz", key: "settings.general.time-zone" },
    { section: "general", field: "gen-activity", key: "settings.general.activity-log-retention-in-days" },
    { section: "general", field: "gen-access", key: "settings.general.access-log-retention-in-days" },
    { section: "general", field: "gen-cookie", key: "settings.general.secure-session-cookie" },
    { section: "general", field: "gen-root", key: "settings.general.url-path-prefix" },
    { section: "general", field: "disp-date", key: "shell.date-format" },
    { section: "general", field: "disp-time", key: "shell.time-format" },
    { section: "rate-limit", field: "", key: "settings.rate-limit" },
    { section: "rate-limit", field: "set-max", key: "settings.ratelimit.requests-per-window-from-one-address" },
    { section: "rate-limit", field: "set-window", key: "settings.ratelimit.window-in-seconds" },
    { section: "rate-limit", field: "set-proxy", key: "settings.ratelimit.reverse-proxy-addresses" },
    { section: "rate-limit", field: "set-exempt", key: "settings.ratelimit.exempt-addresses-and-networks" },
    { section: "passkeys", field: "", key: "settings.passkeys" },
    { section: "passkeys", field: "set-pk-origin", key: "settings.passkeys.passkey-address" },
    { section: "database", field: "", key: "settings.database" },
    { section: "database", field: "dbs-host", key: "db.host" },
    { section: "database", field: "dbs-port", key: "db.port" },
    { section: "database", field: "dbs-database", key: "db.database" },
    { section: "database", field: "dbs-user", key: "db.user" },
    { section: "database", field: "dbs-password", key: "db.password" },
    { section: "database", field: "dbs-sslmode", key: "db.tls" },
    { section: "database", field: "dbs-ca", key: "db.ca-certificate-pem-optional" },
    { section: "redis", field: "", key: "settings.redis-connection" },
    { section: "redis", field: "redis-url", key: "settings.redis.new-connection-url" },
];

// The page: the open section, the save run, and the lock while the database changes
// lock: "" | "sending" | "running" | "recovering" | "read-failed" | "unknown" (DB change, P23 to P30)
var dashSettings = { section: "", bound: false, saving: false, lock: "", loadedOnce: false };

function dashSetPanelState() {
    return { snap: null, baseSnap: null, draft: null, dirty: false, saving: false, failed: "", refreshFailed: "",
        conflict: null, error: "", errorField: "", unknown: false, seq: 0 };
}

var dashSet = {
    snap: null, baseSnap: null, draft: null, dirty: false, saving: false, failed: "", refreshFailed: "",
    conflict: null, error: "", errorField: "", unknown: false, askOwn: null, seq: 0, bound: false,
};
// askPurge / askCookie / askRestart / askOwn: the server asked to confirm the values it
// was sent; the admin answered in the shared question, and the confirmation is bound
// to those values: it goes as soon as one of them changes, or on a conflict
var dashGen = dashSetPanelState();
dashGen.askPurge = null;
dashGen.askCookie = null;
var dashDisp = dashSetPanelState();
var dashRedis = dashSetPanelState();
dashRedis.askRestart = null;
dashRedis.replacing = false;
var dashPkSet = dashSetPanelState();
dashPkSet.bound = false;

// ---------------------------------------------------------------------------
// Shared by the panels: marks, locks, alerts
// ---------------------------------------------------------------------------

function dashSetSourceHint(field) {
    if (field.source === "stored") return t("settings.saved-here");
    if (field.source === "default") return t("settings.default-value");
    return "";
}

// A field the environment sets: a lock pill naming the variable (FS-6)
function dashSetLockPill(field, shown) {
    return '<span class="ui-set-pill is-lock" title="' + esc(t("settings.set-by-the", { env: field.envName, value: shown })) + '">' + DASH_LOCK_ICON +
        "<span>" + esc(t("settings.field.set-by-env", { env: field.envName })) + "</span></span>";
}

// The lock as a sentence, for parts that have no control of their own
function dashSetLocked(field, shown) {
    return '<p class="ui-locked-note">' + DASH_LOCK_ICON + "<span>" + tHtml("settings.set-by-the", { env: tRaw("<code>" + esc(field.envName) + "</code>"), value: shown }) + "</span></p>";
}

// Changed (differs from the default) and Reset to default, for a field that is not
// locked and not a secret (FS-6). shownDefault: the default as words
function dashSetMarks(name, changed, shownDefault) {
    if (!changed) return "";
    return '<span class="ui-set-pill is-changed" title="' + esc(t("settings.field.changed-title")) + '">' + esc(t("settings.field.changed")) + "</span>" +
        '<button type="button" class="ui-btn-link ui-set-reset" data-set-reset="' + esc(name) + '">' + esc(t("settings.field.reset-to-default", { value: shownDefault })) + "</button>";
}

// One setting: label and explanation at the start, the control at the end (FS-8)
function dashSetRow(o) {
    return '<div class="ui-set-row' + (o.locked ? " is-locked" : "") + '" id="' + esc(o.id) + '-row">' +
        '<div class="ui-set-text"><div class="ui-set-label">' + (o.forId ? '<label class="ui-label" id="' + esc(o.id) + '-label" for="' + esc(o.forId) + '">'
            : '<span class="ui-label" id="' + esc(o.id) + '-label">') +
        esc(o.label) + (o.forId ? "</label>" : "</span>") + '<span class="ui-set-marks" id="' + esc(o.id) + '-marks">' + (o.marks || "") + "</span></div>" +
        (o.hint ? '<p class="ui-hint" id="' + esc(o.id) + '-hint">' + o.hint + "</p>" : "") + "</div>" +
        '<div class="ui-set-control">' + o.control + '<p class="ui-set-note hidden" id="' + esc(o.id) + '-note" role="alert"></p></div></div>';
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

// Alerts when this server process failed to reload or still uses an older version
function dashSetAppliedAlerts(s, alerts) {
    var a = s.applied || {};
    if (a.lastReloadError) {
        alerts.push(dashSetAlert(" is-fail", t("settings.this-server-process-could-not-reload"),
            esc(t("settings.it-keeps-using-version", { reason: a.lastReloadError, version: uiNum(a.version) }))));
    } else if (a.version < s.version) {
        alerts.push(dashSetAlert(" is-warn", t("settings.this-server-process-still-uses-version", { version: uiNum(a.version) }), esc(t("settings.it-reloads-the-settings-every-15"))));
    }
}

// ---------------------------------------------------------------------------
// The save unit framework: load, draw, saved, conflict, failed, unknown
// ---------------------------------------------------------------------------

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
        var form = document.getElementById(p.ids.form);
        // Never redraw a part the admin works in: a change, the focus, a selection (SV-6)
        if (!st.dirty && !st.saving && !st.conflict && !(form && uiAreaHeld(form))) {
            // A panel keeps its form while the saved values stay the same (an open
            // picker or a focused field survives the 30-second refresh)
            var same = p.formKey && st.baseSnap && p.formKey(st.baseSnap) === p.formKey(d);
            st.baseSnap = d;
            st.unknown = false;
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
    if (info.getAttribute("data-drawn") !== html && !uiTextHeld(info)) {
        info.setAttribute("data-drawn", html);
        uiKeepFocus(info, function () { info.innerHTML = html; });
    }
    if (!document.getElementById(p.ids.form)) p.drawForm();
    p.sync();
    dashSaveBarSync();
}

// A save that went through: the answer is the new base
function dashPanelSaved(p, d) {
    var st = p.st;
    st.snap = d;
    st.baseSnap = d;
    st.draft = p.draftFrom(d);
    st.dirty = false;
    st.conflict = null;
    st.unknown = false;
    st.error = "";
    st.errorField = "";
    p.clearAsks(st);
    p.drawForm();
    p.draw();
}

// Another admin saved first: the draft and its base stay until the admin chooses
function dashPanelConflict(p, current) {
    var st = p.st;
    st.conflict = current;
    st.snap = current;
    p.clearAsks(st);
    p.draw();
}

function dashPanelFail(p, message, field) {
    p.st.error = message;
    p.st.errorField = field || "";
    p.sync();
}

// No answer to a save: it may have been saved or not. The draft stays, nothing is
// sent again by itself, and the saved values are read again (the next save then
// meets a conflict if this one went through)
function dashPanelUnknown(p) {
    p.st.unknown = true;
    p.st.error = t("settings.save.result-unknown");
    p.st.errorField = "";
    p.sync();
    dashPanelLoad(p);
}

function dashPanelDiscard(p) {
    var st = p.st;
    var to = st.conflict || st.snap;
    st.baseSnap = to;
    st.snap = to;
    st.draft = p.draftFrom(to);
    st.dirty = false;
    st.conflict = null;
    st.unknown = false;
    p.clearAsks(st);
    st.error = "";
    st.errorField = "";
    p.drawForm();
    p.draw();
}

// An explicit choice: the draft now changes the version saved by the other admin;
// the confirmations go too
function dashPanelKeep(p) {
    var st = p.st;
    st.baseSnap = st.conflict;
    st.conflict = null;
    p.clearAsks(st);
    p.input();
    p.draw();
}

// The error line, the invalid field and the busy state follow the state
function dashPanelSyncCommon(p, pairs) {
    var st = p.st;
    var err = document.getElementById(p.ids.err);
    if (err) {
        err.textContent = st.error;
        err.classList.toggle("hidden", !st.error);
    }
    pairs.forEach(function (pair) {
        var el = document.getElementById(pair[0]);
        if (!el) return;
        if (st.errorField === pair[1]) el.setAttribute("aria-invalid", "true");
        else if (!el.hasAttribute("data-check-failed")) el.removeAttribute("aria-invalid");
    });
    var fields = document.getElementById(p.ids.fields);
    if (fields) fields.disabled = st.saving || !!dashSettings.lock;
}

// The shared question for a confirmation the server asked for (DS-12)
function dashSetAsk(o) {
    return uiConfirm({ title: o.title, message: o.message, confirmText: o.confirmText, danger: !!o.danger });
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
    if (tz && !tz.disabled) d.timeZone = tz.value;
    if (cookie && !cookie.disabled) d.cookieSecure = cookie.checked;
    DASH_GEN_RETENTION.forEach(function (r) {
        var el = document.getElementById(r[1]);
        if (el && !el.disabled) d[r[0]] = el.value;
    });
    dashGenDraftChanged();
    dashGenSyncForm();
}

function dashGenRetentionControl(f, r, d) {
    var field = f[r[0]];
    return '<input class="ui-input" id="' + r[1] + '" type="number" inputmode="numeric" min="' + esc(field.min) + '" max="' + esc(field.max) +
        '" step="1" required value="' + esc(field.locked ? field.value : d[r[0]]) + '"' + (field.locked ? " disabled" : "") + ' aria-describedby="' + r[1] + '-hint" />';
}

function dashGenDrawForm() {
    var body = document.getElementById("general-body");
    if (!body || !dashGen.baseSnap) return;
    var s = dashGen.baseSnap;
    var f = s.fields;
    var d = dashGen.draft;
    // Only zones this browser can show dates in; the current value stays so it is not changed silently
    var zones = (Array.isArray(s.timeZones) ? s.timeZones : []).filter(function (z) { return uiZoneValid(z); });
    var zone = f.timeZone.locked ? f.timeZone.value : d.timeZone;
    if (zones.indexOf(zone) === -1) zones.unshift(zone);
    var tzControl = '<select class="ui-select" id="gen-tz" aria-describedby="gen-tz-hint"' + (f.timeZone.locked ? " disabled" : "") + ">" + zones.map(function (z) {
        return '<option value="' + esc(z) + '"' + (z === zone ? " selected" : "") + ">" + esc(z) + "</option>";
    }).join("") + "</select>";
    var cookieOn = f.cookieSecure.locked ? f.cookieSecure.value === true : d.cookieSecure;
    var root = s.rootPath || { envName: "ROOT_PATH", value: null };
    var rootField = root.value === null || root.value === undefined
        ? '<p class="ui-hint">' + tHtml("settings.general.is-not-set-so-the-portal", { env: dashCode(root.envName) }) + "</p>"
        : '<p class="ui-locked-note">' + DASH_LOCK_ICON + "<span>" + (root.value
            ? tHtml("settings.general.environment-variable", { env: dashCode(root.envName), value: dashCode(root.value) })
            : tHtml("settings.general.to-an-empty-value-so-the", { env: dashCode(root.envName) })) + "</span></p>";
    body.innerHTML =
        '<form class="ui-form" id="general-form" novalidate autocomplete="off"><fieldset class="ui-settings-fields ui-set-rows" id="general-fields">' +
        dashSetRow({ id: "gen-tz", forId: "gen-tz", label: t("settings.general.time-zone"), locked: f.timeZone.locked, control: tzControl,
            hint: esc(t("settings.general.dates-on-every-page-and-the", { source: dashSetSourceHint(f.timeZone) })) }) +
        DASH_GEN_RETENTION.map(function (r) {
            return dashSetRow({ id: r[1], forId: r[1], label: t(r[2]), locked: f[r[0]].locked, control: dashGenRetentionControl(f, r, d),
                hint: esc(t("settings.general.whole-days-from", { source: dashSetSourceHint(f[r[0]]), min: uiNum(f[r[0]].min), max: uiNum(f[r[0]].max) })) });
        }).join("") +
        // The whole row is the label of its switch (FS-1)
        '<label class="ui-set-row ui-set-bool' + (f.cookieSecure.locked ? " is-locked" : "") + '" id="gen-cookie-row" for="gen-cookie">' +
        '<span class="ui-set-text"><span class="ui-set-label"><span class="ui-label">' + esc(t("settings.general.secure-session-cookie")) + '</span>' +
        '<span class="ui-set-marks" id="gen-cookie-marks"></span></span>' +
        '<span class="ui-hint" id="gen-cookie-hint">' + esc(t("settings.general.send-the-session-cookie-over-https")) + "</span>" +
        '<span class="ui-hint">' + esc(t("settings.general.turn-it-on-when-admins-reach", { source: dashSetSourceHint(f.cookieSecure) })) + "</span></span>" +
        '<span class="ui-set-control"><input type="checkbox" class="ui-switch" id="gen-cookie" role="switch"' + (cookieOn ? " checked" : "") +
        (f.cookieSecure.locked ? " disabled" : "") + ' aria-describedby="gen-cookie-hint" /><span class="ui-set-note hidden" id="gen-cookie-note" role="alert"></span></span></label>' +
        '<div class="ui-set-row" id="gen-root-row"><div class="ui-set-text"><div class="ui-set-label"><span class="ui-label" id="gen-root">' + esc(t("settings.general.url-path-prefix")) + '</span></div></div>' +
        '<div class="ui-set-control">' + rootField + "</div></div>" +
        '<p class="ui-error hidden" id="gen-err" role="alert"></p>' +
        "</fieldset></form>";
    var sel = document.getElementById("gen-tz");
    if (sel && !sel.disabled) uiPicker(sel, { label: t("settings.general.time-zone"), field: true });
    dashGenSyncForm();
}

function dashGenSyncForm() {
    var form = document.getElementById("general-form");
    if (!form) return;
    var s = dashGen.baseSnap;
    var f = s.fields;
    var d = dashGen.draft;
    var marks = function (id, field, html) {
        var box = document.getElementById(id + "-marks");
        if (box && box.getAttribute("data-drawn") !== html) {
            box.setAttribute("data-drawn", html);
            box.innerHTML = html;
        }
    };
    marks("gen-tz", f.timeZone, f.timeZone.locked ? dashSetLockPill(f.timeZone, f.timeZone.envInvalid ? t("settings.general.not-a-time-zone-so-utc") : f.timeZone.value)
        : dashSetMarks("timeZone", d.timeZone !== f.timeZone["default"], f.timeZone["default"]));
    DASH_GEN_RETENTION.forEach(function (r) {
        var field = f[r[0]];
        marks(r[1], field, field.locked ? dashSetLockPill(field, tn("settings.general.count-days", field.value))
            : dashSetMarks(r[0], d[r[0]].trim() !== String(field["default"]), tn("settings.general.count-days", field["default"])));
    });
    marks("gen-cookie", f.cookieSecure, f.cookieSecure.locked ? dashSetLockPill(f.cookieSecure, f.cookieSecure.value ? t("settings.general.on") : t("settings.general.off"))
        : dashSetMarks("cookieSecure", d.cookieSecure !== (f.cookieSecure["default"] === true), f.cookieSecure["default"] ? t("settings.general.on") : t("settings.general.off")));
    dashPanelSyncCommon(DASH_SET_PANELS.general, [["gen-tz-pick", "timeZone"], ["gen-cookie", "cookieSecure"],
        ["gen-activity", "activityLogRetentionDays"], ["gen-access", "accessLogRetentionDays"]]);
    var pick = document.getElementById("gen-tz");
    if (pick && pick.uiPicker) uiPickerSync(pick);
}

function dashGenReset(name) {
    var f = dashGen.baseSnap.fields;
    var d = dashGen.draft;
    if (name === "timeZone") {
        d.timeZone = f.timeZone["default"];
        var sel = document.getElementById("gen-tz");
        if (sel) { sel.value = d.timeZone; if (sel.uiPicker) uiPickerSync(sel); }
    } else if (name === "cookieSecure") {
        d.cookieSecure = f.cookieSecure["default"] === true;
        var box = document.getElementById("gen-cookie");
        if (box) box.checked = d.cookieSecure;
    } else {
        DASH_GEN_RETENTION.forEach(function (r) {
            if (r[0] !== name) return;
            d[name] = String(f[name]["default"]);
            var el = document.getElementById(r[1]);
            if (el) el.value = d[name];
        });
    }
    dashGenDraftChanged();
    dashGenSyncForm();
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
    dashSetAppliedAlerts(s, alerts);
    var saved = dashSetSavedLine(s, esc(t("settings.general.nothing-saved-here-yet-the-values")));
    alerts.push(dashSetAlert("", t("settings.general.saved-settings"), tHtml("settings.general.changes-apply-at-once-in-this", { saved: tRaw(saved) })));
}

function dashGenConflictText(c) {
    var f = c.fields;
    return esc(t("settings.general.saved-now-time-zone", { zone: f.timeZone.value, activity: tn("settings.general.count-days", f.activityLogRetentionDays.value),
        access: tn("settings.general.count-days", f.accessLogRetentionDays.value), cookie: f.cookieSecure.value ? t("settings.general.on") : t("settings.general.off") }));
}

// The fields the page checks before a save (SV-1): the first problem, or null
function dashGenCheck() {
    var f = dashGen.baseSnap.fields;
    var changes = dashGenChanges();
    for (var i = 0; i < DASH_GEN_RETENTION.length; i++) {
        var r = DASH_GEN_RETENTION[i];
        if (!(r[0] in changes)) continue;
        var v = changes[r[0]];
        var n = Number(v);
        if (!/^\d+$/.test(v) || n < f[r[0]].min || n > f[r[0]].max) {
            return { id: r[1], label: t(r[2]), text: t("settings.check.whole-number-from", { min: uiNum(f[r[0]].min), max: uiNum(f[r[0]].max) }) };
        }
    }
    return null;
}

// The question for a shorter retention: the server's words and its estimate
function dashGenPurgeMessage(ask) {
    var e = ask.estimate || {};
    var rows = function (n, unknownKey, countKey) { return n === null || n === undefined ? t(unknownKey) : t("settings.general.about", { count: tn(countKey, n) }); };
    // Only the logs whose retention goes down; the server leaves the other estimate out.
    var parts = [];
    if (dashGenLowered(ask, "activityLogRetentionDays")) parts.push(rows(e.activityLogs, "settings.general.unknown-activity-log-entries", "settings.general.count-activity-log-entries"));
    if (dashGenLowered(ask, "accessLogRetentionDays")) parts.push(rows(e.accessLogs, "settings.general.unknown-access-log-entries", "settings.general.count-access-log-entries"));
    return parts.length ? t("settings.general.older-than-the-new-limit", { detail: ask.detail || "",
        parts: parts.length > 1 ? t("settings.general.and", { first: parts[0], second: parts[1] }) : parts[0] }) : ask.detail || "";
}

// True when the confirmation lowers this retention below the value the page was showing.
function dashGenLowered(ask, name) {
    var snap = dashGen.snap;
    var cur = snap && snap.fields && snap.fields[name] ? snap.fields[name].value : null;
    return !!ask.confirm && typeof cur === "number" && ask.confirm[name] < cur;
}

// Save General; resolves "saved", "none", "conflict", "cancelled", "failed" or "unknown"
function dashGenSave() {
    var st = dashGen;
    var p = DASH_SET_PANELS.general;
    if (st.saving || !st.baseSnap) return Promise.resolve("none");
    var changes = dashGenChanges();
    if (!Object.keys(changes).length) return Promise.resolve("none");
    var body = { baseVersion: st.baseSnap.version };
    if ("timeZone" in changes) body.timeZone = changes.timeZone;
    if ("cookieSecure" in changes) body.cookieSecure = changes.cookieSecure;
    DASH_GEN_RETENTION.forEach(function (r) {
        if (r[0] in changes) body[r[0]] = Number(changes[r[0]]);
    });
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
            dashPanelSaved(p, d);
            var zone = d.fields.timeZone && d.fields.timeZone.value;
            if (zone && zone !== zoneBefore) dashGenZoneSaved(zone);
            return "saved";
        }
        if (res.status === 409 && d.code === "conflict" && d.current) {
            dashPanelConflict(p, d.current);
            dashGenSyncForm();
            return "conflict";
        }
        if (res.status === 409 && d.code === "confirm_purge" && d.confirm) {
            var purge = { confirm: d.confirm, estimate: d.estimate || {}, detail: d.detail || "", ok: false,
                activityLogRetentionDays: sent.activityLogRetentionDays, accessLogRetentionDays: sent.accessLogRetentionDays };
            st.askPurge = purge;
            dashGenSyncForm();
            return dashSetAsk({ title: t("settings.general.lowering-the-retention-deletes-older-entries"), message: dashGenPurgeMessage(purge),
                confirmText: t("settings.confirm.delete-and-save"), danger: true }).then(function (ok) {
                // Cancelled, or the values changed while the question was open: nothing is sent
                if (!ok || st.askPurge !== purge) { if (st.askPurge === purge) st.askPurge = null; return "cancelled"; }
                purge.ok = true;
                return dashGenSave();
            });
        }
        if (res.status === 409 && d.code === "confirm_cookie_secure") {
            var cookie = { detail: d.detail || "", ok: false, cookieSecure: sent.cookieSecure };
            st.askCookie = cookie;
            dashGenSyncForm();
            return dashSetAsk({ title: t("settings.general.this-page-was-opened-over-plain"), message: cookie.detail,
                confirmText: t("settings.general.turn-on-the-secure-cookie-anyway"), danger: true }).then(function (ok) {
                if (!ok || st.askCookie !== cookie) { if (st.askCookie === cookie) st.askCookie = null; return "cancelled"; }
                cookie.ok = true;
                return dashGenSave();
            });
        }
        if (res.status === 0) {
            dashPanelUnknown(p);
            return "unknown";
        }
        if (res.status === 401) return "failed";
        dashPanelFail(p, apiError(res, t("settings.general.could-not-save-the-settings")), d.field || "");
        if (d.code === "locked") dashGenLoad();
        return "failed";
    });
}

// This tab takes the new zone at once; other open tabs keep the old one until reloaded
function dashGenZoneSaved(zone) {
    dash.zoneAsked = zone;
    uiSetAppZone(zone);
    dashRedrawDates();
}

// ---------------------------------------------------------------------------
// Display: Date Format and Time Format, shown in General, saved on their own (FW-9)
// ---------------------------------------------------------------------------

function dashDispLoad(isCurrent) {
    return dashPanelLoad(DASH_SET_PANELS.display, isCurrent);
}

function dashDispDraftFrom(s) {
    return { dateFormat: s.fields.dateFormat.value, timeFormat: s.fields.timeFormat.value };
}

function dashDispChanges() {
    var f = dashDisp.baseSnap.fields;
    var d = dashDisp.draft;
    var out = {};
    ["dateFormat", "timeFormat"].forEach(function (name) {
        if (!f[name].locked && d[name] !== f[name].value) out[name] = d[name];
    });
    return out;
}

function dashDispWord(name, value) {
    var list = name === "dateFormat" ? UI_DATE_FORMATS : UI_TIME_FORMATS;
    var hit = list.filter(function (x) { return x[0] === value; })[0];
    return hit ? t(hit[1]) : String(value);
}

function dashDispDrawForm() {
    var body = document.getElementById("display-body");
    if (!body || !dashDisp.baseSnap) return;
    var f = dashDisp.baseSnap.fields;
    var d = dashDisp.draft;
    var date = f.dateFormat.locked ? f.dateFormat.value : d.dateFormat;
    var time = f.timeFormat.locked ? f.timeFormat.value : d.timeFormat;
    var dateControl = '<select class="ui-select" id="disp-date" aria-describedby="disp-date-hint"' + (f.dateFormat.locked ? " disabled" : "") + ">" +
        UI_DATE_FORMATS.map(function (x) { return '<option value="' + esc(x[0]) + '"' + (x[0] === date ? " selected" : "") + ">" + esc(t(x[1])) + "</option>"; }).join("") + "</select>";
    // A few short choices: a segmented control (FS-14)
    var timeControl = '<div class="ui-seg" role="group" id="disp-time" aria-labelledby="disp-time-label">' + UI_TIME_FORMATS.map(function (x) {
        return '<button type="button" data-disp-time="' + esc(x[0]) + '" aria-pressed="' + (x[0] === time) + '"' + (f.timeFormat.locked ? " disabled" : "") + ">" + esc(t(x[1])) + "</button>";
    }).join("") + "</div>";
    body.innerHTML =
        '<form class="ui-form" id="display-form" novalidate autocomplete="off"><fieldset class="ui-settings-fields ui-set-rows" id="display-fields">' +
        dashSetRow({ id: "disp-date", forId: "disp-date", label: t("shell.date-format"), locked: f.dateFormat.locked, control: dateControl,
            hint: esc(t("shell.select-your-date-format-default-browser")) }) +
        dashSetRow({ id: "disp-time", label: t("shell.time-format"), locked: f.timeFormat.locked, control: timeControl,
            hint: esc(t("shell.select-your-time-format-default-24")) }) +
        '<p class="ui-hint" id="disp-example" aria-live="polite"></p>' +
        '<p class="ui-error hidden" id="disp-err" role="alert"></p>' +
        "</fieldset></form>";
    var sel = document.getElementById("disp-date");
    if (sel && !sel.disabled) uiPicker(sel, { label: t("shell.date-format"), field: true });
    dashDispSyncForm();
}

function dashDispSyncForm() {
    var form = document.getElementById("display-form");
    if (!form) return;
    var f = dashDisp.baseSnap.fields;
    var d = dashDisp.draft;
    ["dateFormat", "timeFormat"].forEach(function (name) {
        var id = name === "dateFormat" ? "disp-date" : "disp-time";
        var field = f[name];
        var html = field.locked ? dashSetLockPill(field, field.envInvalid ? t("settings.display.env-invalid", { value: dashDispWord(name, field.value) }) : dashDispWord(name, field.value))
            : dashSetMarks(name, d[name] !== field["default"], dashDispWord(name, field["default"]));
        var box = document.getElementById(id + "-marks");
        if (box && box.getAttribute("data-drawn") !== html) {
            box.setAttribute("data-drawn", html);
            box.innerHTML = html;
        }
    });
    form.querySelectorAll("[data-disp-time]").forEach(function (b) {
        b.setAttribute("aria-pressed", String(b.getAttribute("data-disp-time") === (f.timeFormat.locked ? f.timeFormat.value : d.timeFormat)));
    });
    // Now, as the picked formats write it, before saving (FW-9)
    var prefs = { date: f.dateFormat.locked ? f.dateFormat.value : d.dateFormat, time: f.timeFormat.locked ? f.timeFormat.value : d.timeFormat };
    var example = t("shell.example", { time: formatTime(Date.now(), { seconds: false, prefs: prefs }) });
    var ex = document.getElementById("disp-example");
    if (ex.textContent !== example) ex.textContent = example;
    dashPanelSyncCommon(DASH_SET_PANELS.display, [["disp-date-pick", "dateFormat"], ["disp-time", "timeFormat"]]);
    var pick = document.getElementById("disp-date");
    if (pick && pick.uiPicker) uiPickerSync(pick);
}

function dashDispInput() {
    var d = dashDisp.draft;
    if (!d) return;
    var sel = document.getElementById("disp-date");
    if (sel && !sel.disabled) d.dateFormat = sel.value;
    dashDisp.dirty = Object.keys(dashDispChanges()).length > 0;
    dashDispSyncForm();
}

function dashDispPickTime(value) {
    if (!dashDisp.draft || dashDisp.baseSnap.fields.timeFormat.locked || dashDisp.saving || dashSettings.lock) return;
    dashDisp.draft.timeFormat = value;
    dashDisp.dirty = Object.keys(dashDispChanges()).length > 0;
    dashDispSyncForm();
    dashSaveBarSync();
}

function dashDispReset(name) {
    var f = dashDisp.baseSnap.fields;
    dashDisp.draft[name] = f[name]["default"];
    if (name === "dateFormat") {
        var sel = document.getElementById("disp-date");
        if (sel) { sel.value = dashDisp.draft.dateFormat; if (sel.uiPicker) uiPickerSync(sel); }
    }
    dashDisp.dirty = Object.keys(dashDispChanges()).length > 0;
    dashDispSyncForm();
}

function dashDispDraw() {
    dashPanelDraw(DASH_SET_PANELS.display);
}

function dashDispAlerts(s, alerts) {
    ["dateFormat", "timeFormat"].forEach(function (name) {
        var field = s.fields[name];
        if (field.envInvalid) {
            alerts.push(dashSetAlert(" is-warn", t("settings.display.env-not-a-choice-title", { env: field.envName }),
                tHtml("settings.display.env-not-a-choice", { env: dashCode(field.envName), value: dashDispWord(name, field["default"]) })));
        }
    });
    if (s.storedInvalid) {
        alerts.push(dashSetAlert(" is-warn", t("settings.display.stored-invalid-title"), esc(t("settings.display.stored-invalid"))));
    }
    dashSetAppliedAlerts(s, alerts);
    var saved = s.version ? tHtml("settings.version", { version: uiNum(s.version), user: tRaw(uiUserText(s.savedBy || "?")), time: tRaw(uiTime(s.savedAt)) })
        : esc(t("settings.general.nothing-saved-here-yet-the-values"));
    alerts.push(dashSetAlert("", t("settings.display.saved-formats"), tHtml("settings.display.changes-apply", { saved: tRaw(saved) })));
}

function dashDispConflictText(c) {
    return esc(t("settings.display.saved-now", { date: dashDispWord("dateFormat", c.fields.dateFormat.value), time: dashDispWord("timeFormat", c.fields.timeFormat.value) }));
}

function dashDispSave() {
    var st = dashDisp;
    var p = DASH_SET_PANELS.display;
    if (st.saving || !st.baseSnap) return Promise.resolve("none");
    var changes = dashDispChanges();
    if (!Object.keys(changes).length) return Promise.resolve("none");
    var body = { baseVersion: st.baseSnap.version };
    Object.keys(changes).forEach(function (k) { body[k] = changes[k]; });
    st.saving = true;
    st.error = "";
    st.errorField = "";
    dashDispSyncForm();
    st.seq += 1;
    uiMutated();
    return api("PUT", "/admin/api/settings/display", body).then(function (res) {
        st.saving = false;
        st.seq += 1;
        var d = res.data || {};
        if (res.ok && d.fields) {
            dashPanelSaved(p, d);
            // This tab writes its dates the new way at once
            uiSetDatePrefs(d.fields.dateFormat.value, d.fields.timeFormat.value);
            dashRedrawDates();
            return "saved";
        }
        if (res.status === 409 && d.code === "conflict" && d.current) {
            dashPanelConflict(p, d.current);
            return "conflict";
        }
        // No answer, or the database did not confirm the write: it may have been saved
        if (res.status === 0 || d.code === "write_unknown") {
            dashPanelUnknown(p);
            return "unknown";
        }
        if (res.status === 401) return "failed";
        // The codes this route added show catalog words; older API errors keep the server's reason
        var known = DASH_DISP_ERRORS[d.code];
        dashPanelFail(p, known ? t(known) : apiError(res, t("settings.display.could-not-save")), d.field || "");
        if (d.code === "locked") dashDispLoad();
        return "failed";
    });
}

// The refusals of PUT /admin/api/settings/display, by code (R2: never the server's words)
var DASH_DISP_ERRORS = {
    busy: "settings.display.save-busy",
    write_refused: "settings.display.save-refused",
    locked: "settings.display.save-locked",
    invalid: "settings.display.save-invalid",
};

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
    var maxControl = '<input class="ui-input" id="set-max" type="number" inputmode="numeric" min="' + esc(f.maxReq.min) + '" max="' + esc(f.maxReq.max) +
        '" step="1" required value="' + esc(f.maxReq.locked ? f.maxReq.value : d.maxReq) + '"' + (f.maxReq.locked ? " disabled" : "") + ' aria-describedby="set-max-hint" />';
    var winControl = '<input class="ui-input" id="set-window" type="number" inputmode="numeric" min="' + esc(f.windowMs.minSeconds) + '" max="' +
        esc(f.windowMs.maxSeconds) + '" step="1" required value="' + esc(f.windowMs.locked ? f.windowMs.value / 1000 : d.windowSeconds) + '"' +
        (f.windowMs.locked ? " disabled" : "") + ' aria-describedby="set-window-hint" />';
    var exemptHint = '<span class="ui-hint-line">' + esc(t("settings.ratelimit.one-per-line-up-to", { max: uiNum(f.exempt.max) })) + '</span><span class="ui-hint-line">' + (own
        ? tHtml("settings.ratelimit.your-request-to-this-page-came", { example: dashCode("172.18.0.0/16"), own: dashCode(own) })
        : tHtml("settings.ratelimit.ipv4-or-ipv6-addresses-or-networks", { example: dashCode("172.18.0.0/16") })) + "</span>";
    var exemptControl = '<textarea class="ui-textarea" id="set-exempt" spellcheck="false" autocapitalize="off" autocomplete="off" dir="ltr" aria-describedby="set-exempt-hint" placeholder="' +
        esc(t("settings.ratelimit.203-0-113-10-172-18")) + '">' + esc(d.exempt) + "</textarea>";
    body.innerHTML =
        '<form class="ui-form" id="settings-form" novalidate><fieldset class="ui-settings-fields ui-set-rows" id="settings-fields">' +
        dashSetRow({ id: "set-max", forId: "set-max", label: t("settings.ratelimit.requests-per-window-from-one-address"), locked: f.maxReq.locked, control: maxControl,
            hint: esc(t("settings.ratelimit.from-1-to", { source: dashSetSourceHint(f.maxReq), max: uiNum(f.maxReq.max), scope: t("settings.ratelimit.an-ipv6-64-network-counts-as") })) }) +
        dashSetRow({ id: "set-window", forId: "set-window", label: t("settings.ratelimit.window-in-seconds"), locked: f.windowMs.locked, control: winControl,
            hint: esc(t("settings.ratelimit.whole-seconds-from-1-to", { source: dashSetSourceHint(f.windowMs), max: uiNum(f.windowMs.maxSeconds) })) }) +
        '<div class="ui-set-row" id="set-proxy-row"><div class="ui-set-text"><div class="ui-set-label"><span class="ui-label" id="set-proxy">' + esc(t("settings.ratelimit.reverse-proxy-addresses")) +
        '</span></div></div><div class="ui-set-control">' + proxyField + "</div></div>" +
        dashSetRow({ id: "set-exempt", forId: "set-exempt", label: t("settings.ratelimit.exempt-addresses-and-networks"), control: exemptControl, hint: exemptHint }) +
        (proxySet && proxy.value && !proxyLoopback ? "" :
            '<div class="ui-alert is-warn" id="set-exempt-warn"><span class="ui-alert-bar" aria-hidden="true"></span><div><p>' + esc(t("settings.ratelimit.an-exempt-address-is-not-limited")) + '</p>' +
            "<small>" + tHtml("settings.ratelimit.if-a-reverse-proxy-sits-in", { env: dashCode(t("settings.ratelimit.forwarded-allow-ips")) }) + "</small></div><span></span></div>") +
        '<p class="ui-error hidden" id="set-err" role="alert"></p>' +
        "</fieldset></form>";
    dashSettingsSyncForm();
}

// Parts of the form that follow the draft and the last answer, without redrawing the inputs
function dashSettingsSyncForm() {
    var form = document.getElementById("settings-form");
    if (!form) return;
    var f = dashSet.baseSnap.fields;
    var d = dashSet.draft;
    var put = function (id, html) {
        var box = document.getElementById(id + "-marks");
        if (box && box.getAttribute("data-drawn") !== html) {
            box.setAttribute("data-drawn", html);
            box.innerHTML = html;
        }
    };
    put("set-max", f.maxReq.locked ? dashSetLockPill(f.maxReq, tn("settings.ratelimit.count-requests", f.maxReq.value))
        : dashSetMarks("maxReq", d.maxReq.trim() !== String(f.maxReq["default"]), tn("settings.ratelimit.count-requests", f.maxReq["default"])));
    put("set-window", f.windowMs.locked ? dashSetLockPill(f.windowMs, t("settings.ratelimit.ms", { n: uiNum(f.windowMs.value) }))
        : dashSetMarks("windowSeconds", d.windowSeconds.trim() !== String(f.windowMs["default"] / 1000), uiNum(f.windowMs["default"] / 1000)));
    dashPanelSyncCommon(DASH_SET_PANELS.rate, [["set-max", "maxReq"], ["set-window", "windowMs"], ["set-exempt", "exempt"]]);
}

function dashSettingsReset(name) {
    var f = dashSet.baseSnap.fields;
    var el = null;
    if (name === "maxReq") {
        dashSet.draft.maxReq = String(f.maxReq["default"]);
        el = document.getElementById("set-max");
        if (el) el.value = dashSet.draft.maxReq;
    } else if (name === "windowSeconds") {
        dashSet.draft.windowSeconds = String(f.windowMs["default"] / 1000);
        el = document.getElementById("set-window");
        if (el) el.value = dashSet.draft.windowSeconds;
    }
    dashSettingsInput();
}

function dashSettingsDraw() {
    dashPanelDraw(DASH_SET_PANELS.rate);
}

function dashSettingsAlerts(s, alerts) {
    dashSetAppliedAlerts(s, alerts);
    var saved = dashSetSavedLine(s, esc(t("settings.ratelimit.nothing-saved-here-yet-the-values")));
    alerts.push(dashSetAlert("", t("settings.ratelimit.saved-settings"), tHtml("settings.ratelimit.changes-apply-at-once-in-this", { saved: tRaw(saved) })));
}

function dashSettingsConflictText(c) {
    var f = c.fields;
    return tHtml("settings.ratelimit.saved-now", { requests: tn("settings.ratelimit.count-requests", f.maxReq.value), seconds: uiNum(f.windowMs.value / 1000),
        exempt: tRaw(f.exempt.value.length ? f.exempt.value.map(function (a) { return uiUserText(a); }).join(", ") : esc(t("settings.ratelimit.none"))) });
}

function dashSettingsInput() {
    var d = dashSet.draft;
    if (!d) return;
    var max = document.getElementById("set-max");
    var win = document.getElementById("set-window");
    var exempt = document.getElementById("set-exempt");
    if (max && !max.disabled) d.maxReq = max.value;
    if (win && !win.disabled) d.windowSeconds = win.value;
    if (exempt) d.exempt = exempt.value;
    // The confirmation for the admin's own address is bound to the list it was given for
    if (dashSet.askOwn && dashSet.askOwn.exempt !== dashSetExemptList(d.exempt).join("\n")) dashSet.askOwn = null;
    dashSet.dirty = Object.keys(dashSetChanges()).length > 0;
    dashSettingsSyncForm();
}

function dashSettingsCheck() {
    var f = dashSet.baseSnap.fields;
    var c = dashSetChanges();
    var whole = /^\d+$/;
    if ("maxReq" in c && (!whole.test(c.maxReq) || Number(c.maxReq) < f.maxReq.min || Number(c.maxReq) > f.maxReq.max)) {
        return { id: "set-max", label: t("settings.ratelimit.requests-per-window-from-one-address"), text: t("settings.check.whole-number-from", { min: uiNum(f.maxReq.min), max: uiNum(f.maxReq.max) }) };
    }
    if ("windowSeconds" in c && (!whole.test(c.windowSeconds) || Number(c.windowSeconds) < f.windowMs.minSeconds || Number(c.windowSeconds) > f.windowMs.maxSeconds)) {
        return { id: "set-window", label: t("settings.ratelimit.window-in-seconds"), text: t("settings.check.whole-number-from", { min: uiNum(f.windowMs.minSeconds), max: uiNum(f.windowMs.maxSeconds) }) };
    }
    if ("exempt" in c && c.exempt.length > f.exempt.max) {
        return { id: "set-exempt", label: t("settings.ratelimit.exempt-addresses-and-networks"), text: t("settings.check.too-many-lines", { max: uiNum(f.exempt.max) }) };
    }
    return null;
}

function dashSettingsSave() {
    var p = DASH_SET_PANELS.rate;
    if (dashSet.saving || !dashSet.baseSnap) return Promise.resolve("none");
    var changes = dashSetChanges();
    if (!Object.keys(changes).length) return Promise.resolve("none");
    var body = { baseVersion: dashSet.baseSnap.version };
    if ("maxReq" in changes) body.maxReq = Number(changes.maxReq);
    if ("windowSeconds" in changes) body.windowSeconds = Number(changes.windowSeconds);
    if ("exempt" in changes) body.exempt = changes.exempt;
    var sentExempt = dashSetExemptList(dashSet.draft.exempt).join("\n");
    if (dashSet.askOwn && dashSet.askOwn.ok && dashSet.askOwn.exempt === sentExempt) body.confirmOwnAddress = true;
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
            dashPanelSaved(p, d);
            return "saved";
        }
        if (res.status === 409 && d.code === "conflict" && d.current) {
            dashPanelConflict(p, d.current);
            return "conflict";
        }
        if (res.status === 409 && d.code === "own_address") {
            var ask = { exempt: sentExempt, ok: false };
            dashSet.askOwn = ask;
            return dashSetAsk({ title: t("settings.ratelimit.confirm-own-title", { own: d.address || "" }), message: d.detail || "",
                confirmText: t("settings.ratelimit.confirm-own-button"), danger: true }).then(function (ok) {
                if (!ok || dashSet.askOwn !== ask) { if (dashSet.askOwn === ask) dashSet.askOwn = null; return "cancelled"; }
                ask.ok = true;
                return dashSettingsSave();
            });
        }
        if (res.status === 0) {
            dashPanelUnknown(p);
            return "unknown";
        }
        if (res.status === 401) return "failed";
        dashSettingsFail(apiError(res, t("settings.ratelimit.could-not-save-the-settings")), d.field || "");
        if (d.code === "locked") dashSettingsLoad();
        return "failed";
    });
}

function dashSettingsFail(message, field) {
    dashPanelFail(DASH_SET_PANELS.rate, message, field);
}

// ---------------------------------------------------------------------------
// The passkey address: one field, saved on top of the version it was read at
// ---------------------------------------------------------------------------

function dashPkSetLoad(isCurrent) {
    return dashPanelLoad(DASH_SET_PANELS.passkeys, isCurrent);
}

// The host name of an address, as the passkeys' rpId is; "" when it is not one
function dashPkHost(value) {
    try { return new URL(value).hostname.toLowerCase(); } catch (e) { return ""; }
}

function dashPkSetChanges() {
    var b = dashPkSet.baseSnap;
    if (!b || b.locked) return {};
    var value = dashPkSet.draft.origin.trim();
    return value !== (b.origin || "") ? { origin: value } : {};
}

function dashPkSetDrawForm() {
    var body = document.getElementById("settings-pk-body");
    if (!body || !dashPkSet.baseSnap) return;
    var b = dashPkSet.baseSnap;
    var here = window.location.origin;
    var control = '<input class="ui-input" id="set-pk-origin" type="url" inputmode="url" spellcheck="false" autocapitalize="off" autocomplete="off" dir="ltr" ' +
        'placeholder="' + esc(here) + '" value="' + esc(b.locked ? b.origin || "" : dashPkSet.draft.origin) + '" aria-describedby="set-pk-origin-hint"' +
        (b.locked ? " disabled" : "") + ' data-inventory="settings.passkeys.origin" />';
    var hint = !b.locked && !b.origin ? tHtml("settings.passkeys.not-set-yet-the-first-passkey", { here: dashCode(here) })
        : !b.locked && b.origin && b.savedBy ? tHtml("settings.passkeys.saved-by", { here: dashCode(here), user: tRaw(uiUserText(b.savedBy)), time: tRaw(uiTime(b.savedAt)) })
        : tHtml("settings.passkeys.the-address-admins-open-this-service", { here: dashCode(here) });
    body.innerHTML = '<form class="ui-form" id="pk-set-form" novalidate><fieldset class="ui-settings-fields ui-set-rows" id="pk-set-fields">' +
        dashSetRow({ id: "set-pk-origin", forId: "set-pk-origin", label: t("settings.passkeys.passkey-address"), locked: b.locked, control: control,
            marks: b.locked ? dashSetLockPill({ envName: b.envName || "PASSKEY_ORIGIN" }, b.origin || "") : "", hint: hint }) +
        '<div class="ui-alert is-warn"><span class="ui-alert-bar" aria-hidden="true"></span><div><p>' + esc(t("settings.passkeys.passkeys-made-for-another-address-stop")) + '</p>' +
        "<small>" + esc(t("settings.passkeys.a-new-host-name-or-no")) + "</small></div><span></span></div>" +
        '<p class="ui-error hidden" id="set-pk-err" role="alert"></p>' +
        "</fieldset></form>";
    dashPkSetSync();
}

function dashPkSetSync() {
    if (!document.getElementById("pk-set-form")) return;
    dashPanelSyncCommon(DASH_SET_PANELS.passkeys, [["set-pk-origin", "origin"]]);
}

function dashPkSetInput() {
    var el = document.getElementById("set-pk-origin");
    if (!el || el.disabled || !dashPkSet.draft) return;
    dashPkSet.draft.origin = el.value;
    dashPkSet.dirty = Object.keys(dashPkSetChanges()).length > 0;
    dashPkSetSync();
}

function dashPkSetDraw() {
    dashPanelDraw(DASH_SET_PANELS.passkeys);
}

function dashPkSetCheck() {
    var c = dashPkSetChanges();
    if (!("origin" in c) || !c.origin) return null;
    var ok = false;
    try { ok = /^https?:$/.test(new URL(c.origin).protocol); } catch (e) { ok = false; }
    return ok ? null : { id: "set-pk-origin", label: t("settings.passkeys.passkey-address"), text: t("settings.check.an-address") };
}

function dashPkSetSave() {
    var st = dashPkSet;
    var p = DASH_SET_PANELS.passkeys;
    if (st.saving || !st.baseSnap || st.baseSnap.locked) return Promise.resolve("none");
    var b = st.baseSnap;
    var changes = dashPkSetChanges();
    if (!("origin" in changes)) return Promise.resolve("none");
    var value = changes.origin;
    var current = b.origin || "";
    // The existing questions: removing the address, or moving it to another host name
    var ask = null;
    if (!value) {
        ask = { title: t("settings.passkeys.remove-the-passkey-address"), message: t("settings.passkeys.no-passkey-works-until-an-address"), confirmText: t("settings.passkeys.remove") };
    } else if (current && b.rpId && dashPkHost(value) && dashPkHost(value) !== b.rpId) {
        ask = { title: t("settings.passkeys.change-the-passkey-address"), message: t("settings.passkeys.passkeys-made-for", { address: b.rpId }), confirmText: t("settings.passkeys.change") };
    }
    if (ask) ask.danger = true;
    return (ask ? uiConfirm(ask) : Promise.resolve(true)).then(function (ok) {
        if (!ok) return "cancelled";
        if (st.saving) return "none";
        st.saving = true;
        st.error = "";
        st.errorField = "";
        st.seq += 1; // a load that started before this save must not draw over it
        dashPkSetSync();
        uiMutated();
        return api("PUT", "/admin/api/settings/passkeys", { baseVersion: b.version, origin: value }).then(function (res) {
            st.saving = false;
            st.seq += 1;
            var d = res.data;
            if (res.ok && d && typeof d.version === "number") {
                dashPanelSaved(p, d);
                dashPk.at = 0; // the Account page and Copy token read the list again
                return "saved";
            }
            if (res.status === 0) {
                dashPanelUnknown(p);
                return "unknown";
            }
            if (res.status === 401) return "failed";
            if (res.status === 409) {
                // Saved by someone else, or set by the environment meanwhile: read it again
                return api("GET", "/admin/api/settings/passkeys").then(function (cur) {
                    var c = cur.data;
                    if (cur.ok && c && typeof c.version === "number" && !c.locked) {
                        dashPanelConflict(p, c);
                        return "conflict";
                    }
                    dashPanelFail(p, apiError(res, t("settings.passkeys.could-not-save-the-passkey-address")), "origin");
                    dashPkSetLoad();
                    return "failed";
                });
            }
            dashPanelFail(p, apiError(res, t("settings.passkeys.could-not-save-the-passkey-address")), "origin");
            return "failed";
        });
    });
}

// The passkey address has no notes of its own above its field
function dashPkSetAlerts() {}

function dashPkSetConflictText(c) {
    return c.origin ? tHtml("settings.passkeys.saved-now", { address: dashCode(c.origin) }) : esc(t("settings.passkeys.saved-now-none"));
}

// ---------------------------------------------------------------------------
// Redis connection: saved only after the server checked it; used after a restart.
// A secret: shown as Stored, the field appears only on Replace (FS-3)
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

function dashRedisChanges() {
    var st = dashRedis;
    if (!st.baseSnap || st.baseSnap.url.locked || !st.replacing) return {};
    var url = st.draft.url.trim();
    return url ? { url: url } : {};
}

function dashRedisDraftChanged() {
    var st = dashRedis;
    if (st.askRestart && st.askRestart.url !== st.draft.url.trim()) st.askRestart = null;
    st.dirty = Object.keys(dashRedisChanges()).length > 0;
}

function dashRedisInput() {
    if (!dashRedis.draft) return;
    var el = document.getElementById("redis-url");
    if (el) dashRedis.draft.url = el.value;
    dashRedisDraftChanged();
    dashRedisSyncForm();
}

// The field the address names after #, or "" when there is none or it cannot be decoded (R4)
function dashSettingsHashField() {
    var raw = window.location.hash ? window.location.hash.slice(1) : "";
    try {
        return decodeURIComponent(raw);
    } catch (e) {
        return "";
    }
}

function dashRedisDrawForm() {
    var body = document.getElementById("redis-body");
    if (!body || !dashRedis.baseSnap) return;
    var u = dashRedis.baseSnap.url;
    var source = tRaw(DASH_REDIS_SOURCE[u.source] ? tHtml(DASH_REDIS_SOURCE[u.source][0], { env: tRaw(DASH_REDIS_SOURCE[u.source][1] ? "<code>" + esc(t(DASH_REDIS_SOURCE[u.source][1])) + "</code>" : "") }) : esc(u.source));
    var now = '<p class="ui-hint">' + (u.saved
        ? tHtml("settings.redis.saved-here", { connected: dashCode(u.connected), saved: dashCode(u.saved), source: source })
        : tHtml("settings.redis.this-server-process-uses", { connected: dashCode(u.connected), source: source })) + "</p>";
    var control = u.locked
        ? '<input class="ui-input" id="redis-url" type="text" disabled dir="ltr" value="' + esc(u.connected || "") + '" />'
        : '<div class="ui-set-secret" id="redis-stored"><span>' + esc(t("settings.secret.stored")) + '</span>' +
          '<button type="button" class="ui-btn ui-btn-sm" data-redis-replace>' + esc(t("settings.secret.replace")) + "</button></div>" +
          '<div class="ui-set-replace hidden" id="redis-replace"><input class="ui-input" id="redis-url" type="password" autocomplete="new-password" spellcheck="false" autocapitalize="off" dir="ltr" ' +
          'placeholder="' + esc(t("settings.redis.redis-password-host-6379-0")) + '" aria-describedby="redis-url-hint" />' +
          '<button type="button" class="ui-btn ui-btn-sm" data-redis-keep>' + esc(t("settings.secret.keep")) + "</button></div>";
    body.innerHTML =
        '<form class="ui-form" id="redis-form" novalidate autocomplete="off"><fieldset class="ui-settings-fields ui-set-rows" id="redis-fields">' +
        '<div class="ui-set-row" id="redis-now-row"><div class="ui-set-text"><div class="ui-set-label"><span class="ui-label">' + esc(t("settings.redis.current-connection")) +
        '</span></div></div><div class="ui-set-control">' + now + "</div></div>" +
        dashSetRow({ id: "redis-url", forId: "redis-url", label: t("settings.redis.new-connection-url"), locked: u.locked, control: control,
            marks: u.locked ? dashSetLockPill(u, u.connected) : "",
            hint: tHtml("settings.redis.the-whole-url-with-its-password", { scheme: dashCode(t("settings.redis.rediss")) }) }) +
        '<p class="ui-error hidden" id="redis-err" role="alert"></p>' +
        "</fieldset></form>";
    dashRedisSyncForm();
}

function dashRedisSyncForm() {
    var form = document.getElementById("redis-form");
    if (!form) return;
    var stored = document.getElementById("redis-stored");
    var replace = document.getElementById("redis-replace");
    if (stored && replace) {
        stored.classList.toggle("hidden", dashRedis.replacing);
        replace.classList.toggle("hidden", !dashRedis.replacing);
    }
    dashPanelSyncCommon(DASH_SET_PANELS.redis, [["redis-url", "url"]]);
}

// Replace shows the field and takes the focus; Keep hides it and drops what was typed
function dashRedisReplace(on) {
    dashRedis.replacing = on;
    if (!on) {
        dashRedis.draft = dashRedisDraftFrom();
        var el = document.getElementById("redis-url");
        if (el) el.value = "";
    }
    dashRedisDraftChanged();
    dashRedisSyncForm();
    dashSaveBarSync();
    var target = on ? document.getElementById("redis-url") : document.querySelector("[data-redis-replace]");
    if (target) target.focus();
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

function dashRedisCheck() {
    var c = dashRedisChanges();
    if (!("url" in c)) return null;
    return /^rediss?:\/\/\S+$/i.test(c.url) ? null : { id: "redis-url", label: t("settings.redis.new-connection-url"), text: t("settings.check.a-redis-url") };
}

function dashRedisSave() {
    var st = dashRedis;
    var p = DASH_SET_PANELS.redis;
    if (st.saving || !st.baseSnap) return Promise.resolve("none");
    var changes = dashRedisChanges();
    if (!("url" in changes)) return Promise.resolve("none");
    var url = changes.url;
    var body = { baseVersion: st.baseSnap.version, url: url };
    if (st.askRestart && st.askRestart.ok && st.askRestart.url === url) body.confirmRestart = true;
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
            st.replacing = false;
            var el = document.getElementById("redis-url");
            if (el) el.value = "";
            dashPanelSaved(p, d);
            return "saved";
        }
        if (res.status === 409 && d.code === "conflict" && d.current) {
            dashPanelConflict(p, d.current);
            return "conflict";
        }
        if (res.status === 409 && d.code === "confirm_restart") {
            var ask = { detail: d.detail || "", ok: false, url: url };
            st.askRestart = ask;
            return dashSetAsk({ title: t("settings.redis.saving-takes-effect-only-after-a"), message: ask.detail,
                confirmText: t("settings.confirm.save-and-restart-later") }).then(function (ok) {
                if (!ok || st.askRestart !== ask) { if (st.askRestart === ask) st.askRestart = null; return "cancelled"; }
                ask.ok = true;
                return dashRedisSave();
            });
        }
        if (d.code === "redis_check_failed") {
            dashPanelFail(p, t("settings.redis.nothing-was-saved", { reason: t(DASH_REDIS_CHECK[d.category] || DASH_REDIS_CHECK.other) }), "url");
            return "failed";
        }
        if (res.status === 0) {
            // A secret is never sent again by itself
            dashPanelUnknown(p);
            return "unknown";
        }
        if (res.status === 401) return "failed";
        dashPanelFail(p, apiError(res, t("settings.redis.could-not-save-the-redis-connection")), d.field || "");
        if (d.code === "locked") dashRedisLoad();
        return "failed";
    });
}

// ---------------------------------------------------------------------------
// The save units, in the order the save bar saves them
// ---------------------------------------------------------------------------

var DASH_SET_PANELS = {
    general: {
        name: "general", section: "general", unit: "settings.general", st: dashGen,
        failedTitle: "settings.could-not-load-the-general-settings", loadingText: "settings.loading-the-general-settings",
        ids: { info: "general-info", body: "general-body", form: "general-form", fields: "general-fields", err: "gen-err" },
        get: function () { return api("GET", "/admin/api/settings/general"); },
        valid: function (d) { return !!(d.fields && d.fields.timeZone); },
        formKey: function (d) { return JSON.stringify([d.version, d.fields, d.rootPath, (d.timeZones || []).length]); },
        draftFrom: dashGenDraftFrom, drawForm: dashGenDrawForm, draw: dashGenDraw, sync: dashGenSyncForm, input: dashGenInput,
        changes: dashGenChanges, check: dashGenCheck, reset: dashGenReset,
        save: dashGenSave, alerts: dashGenAlerts, conflictText: dashGenConflictText, clearAsks: dashGenClearAsks,
    },
    display: {
        name: "display", section: "general", unit: "settings.display", st: dashDisp,
        failedTitle: "settings.display.could-not-load", loadingText: "settings.display.loading",
        ids: { info: "display-info", body: "display-body", form: "display-form", fields: "display-fields", err: "disp-err" },
        get: function () { return api("GET", "/admin/api/settings/display"); },
        valid: function (d) { return !!(d.fields && d.fields.dateFormat && d.fields.timeFormat); },
        formKey: function (d) { return JSON.stringify([d.version, d.fields]); },
        draftFrom: dashDispDraftFrom, drawForm: dashDispDrawForm, draw: dashDispDraw, sync: dashDispSyncForm, input: dashDispInput,
        changes: dashDispChanges, check: null, reset: dashDispReset,
        save: dashDispSave, alerts: dashDispAlerts, conflictText: dashDispConflictText, clearAsks: function () {},
    },
    rate: {
        name: "rate", section: "rate-limit", unit: "settings.rate-limit", st: dashSet,
        failedTitle: "settings.could-not-load-the-settings", loadingText: "settings.loading-settings",
        ids: { info: "settings-info", body: "settings-body", form: "settings-form", fields: "settings-fields", err: "set-err" },
        get: function () { return api("GET", "/admin/api/settings/rate-limit"); },
        valid: function (d) { return !!d.fields; },
        formKey: function (d) { return JSON.stringify([d.version, d.fields, d.yourAddress, d.proxyNetwork]); },
        draftFrom: dashSetDraftFrom, drawForm: dashSettingsDrawForm, draw: dashSettingsDraw, sync: dashSettingsSyncForm, input: dashSettingsInput,
        changes: dashSetChanges, check: dashSettingsCheck, reset: dashSettingsReset,
        save: dashSettingsSave, alerts: dashSettingsAlerts, conflictText: dashSettingsConflictText,
        clearAsks: function (st) { st.askOwn = null; },
    },
    passkeys: {
        name: "passkeys", section: "passkeys", unit: "settings.passkeys.passkey-address", st: dashPkSet,
        failedTitle: "settings.passkeys.could-not-load-the-passkey-address", loadingText: "settings.passkeys.loading-the-passkey-address",
        ids: { info: "settings-pk-info", body: "settings-pk-body", form: "pk-set-form", fields: "pk-set-fields", err: "set-pk-err" },
        get: function () { return api("GET", "/admin/api/settings/passkeys"); },
        valid: function (d) { return typeof d.version === "number"; },
        formKey: function (d) { return JSON.stringify([d.version, d.locked, d.origin]); },
        draftFrom: function (d) { return { origin: d.origin || "" }; }, drawForm: dashPkSetDrawForm, draw: dashPkSetDraw, sync: dashPkSetSync, input: dashPkSetInput,
        changes: dashPkSetChanges, check: dashPkSetCheck, reset: null,
        save: dashPkSetSave, alerts: dashPkSetAlerts, conflictText: dashPkSetConflictText, clearAsks: function () {},
    },
    redis: {
        name: "redis", section: "redis", unit: "settings.redis-connection", st: dashRedis,
        failedTitle: "settings.could-not-load-the-redis-connection", loadingText: "settings.loading-the-redis-connection",
        ids: { info: "redis-info", body: "redis-body", form: "redis-form", fields: "redis-fields", err: "redis-err" },
        get: function () { return api("GET", "/admin/api/settings/redis"); },
        valid: function (d) { return !!d.url; },
        formKey: function (d) { return JSON.stringify([d.version, d.url]); },
        draftFrom: dashRedisDraftFrom, drawForm: dashRedisDrawForm, draw: dashRedisDraw, sync: dashRedisSyncForm, input: dashRedisInput,
        changes: dashRedisChanges, check: dashRedisCheck, reset: null,
        save: dashRedisSave, alerts: dashRedisAlerts, conflictText: dashRedisConflictText, clearAsks: dashRedisClearAsks,
    },
};
var DASH_SET_ORDER = ["general", "display", "rate", "passkeys", "redis"];
var DASH_SET_FORMS = { "general-form": "general", "display-form": "display", "settings-form": "rate", "pk-set-form": "passkeys", "redis-form": "redis" };

// How many fields differ from what was loaded, in one unit or in all of them (SV-2)
function dashSetUnitCount(p) {
    if (!p.st.baseSnap || !p.st.draft) return 0;
    return Object.keys(p.changes()).length;
}
function dashSettingsCount() {
    return DASH_SET_ORDER.reduce(function (sum, name) { return sum + dashSetUnitCount(DASH_SET_PANELS[name]); }, 0);
}

function dashSettingsLoadAll(isCurrent) {
    return Promise.all(DASH_SET_ORDER.map(function (name) { return dashPanelLoad(DASH_SET_PANELS[name], isCurrent); }));
}

// ---------------------------------------------------------------------------
// The save bar (SV-1): the count of unsaved fields, Discard and Save changes
// ---------------------------------------------------------------------------

function dashSaveBarSync() {
    var bar = document.getElementById("settings-savebar");
    if (!bar) return;
    var n = dashSettingsCount();
    var shown = n > 0 || dashSettings.saving;
    bar.classList.toggle("hidden", !shown);
    var count = document.getElementById("settings-savebar-count");
    var text = dashSettings.lock ? t("settings.savebar.locked") : tn("settings.savebar.count", n);
    if (count.textContent !== text) count.textContent = text;
    var save = document.getElementById("settings-save");
    var discard = document.getElementById("settings-discard");
    var off = dashSettings.saving || !!dashSettings.lock;
    save.disabled = off;
    discard.disabled = off;
    bar.setAttribute("aria-busy", String(dashSettings.saving));
    save.textContent = dashSettings.saving ? t("settings.saving") : t("settings.savebar.save");
    // The section list marks each section that holds a change
    SETTINGS_SECTIONS.forEach(function (s) {
        var has = DASH_SET_ORDER.some(function (name) { var p = DASH_SET_PANELS[name]; return p.section === s.id && dashSetUnitCount(p) > 0; });
        document.querySelectorAll('[data-settings-nav="' + s.id + '"]').forEach(function (a) { a.classList.toggle("has-changes", has); });
    });
    var pick = document.getElementById("settings-pick");
    if (pick) {
        Array.prototype.forEach.call(pick.options, function (o) {
            var has = DASH_SET_ORDER.some(function (name) { var p = DASH_SET_PANELS[name]; return p.section === o.value && dashSetUnitCount(p) > 0; });
            var label = has ? t("settings.nav.section-changed", { section: t(dashSettingsInfo(o.value).label) }) : t(dashSettingsInfo(o.value).label);
            if (o.textContent !== label) o.textContent = label;
        });
    }
}

// Discard drops every unsaved field of every unit (SV-3)
function dashSettingsDiscardAll() {
    if (dashSettings.saving || dashSettings.lock) return;
    DASH_SET_ORDER.forEach(function (name) {
        var p = DASH_SET_PANELS[name];
        if (!p.st.baseSnap) return;
        if (name === "redis") dashRedis.replacing = false;
        dashPanelDiscard(p);
    });
    dashSettingsClearChecks();
    dashSaveBarSync();
}

// The names of units as one list ("General, Rate limit and Redis connection")
function dashSettingsNames(names) {
    var words = names.map(function (name) { return t(DASH_SET_PANELS[name].unit); });
    try { return new Intl.ListFormat("en", { style: "long", type: "conjunction" }).format(words); } catch (e) { return words.join(", "); }
}

// One toast for the whole save, naming what was saved and what was not (SV-3)
function dashSettingsReport(results) {
    var saved = [];
    var failed = [];
    var unknown = [];
    var warning = "";
    DASH_SET_ORDER.forEach(function (name) {
        var r = results[name];
        if (r === "saved") {
            saved.push(name);
            if (DASH_SET_PANELS[name].st.snap && DASH_SET_PANELS[name].st.snap.warning) warning = DASH_SET_PANELS[name].st.snap.warning;
        } else if (r === "failed" || r === "conflict") failed.push(name);
        else if (r === "unknown") unknown.push(name);
    });
    if (unknown.length) {
        showToast(t("settings.savebar.result-unknown", { parts: dashSettingsNames(unknown) }), "error");
    } else if (saved.length && failed.length) {
        showToast(t("settings.savebar.saved-and-failed", { saved: dashSettingsNames(saved), failed: dashSettingsNames(failed) }), "error");
    } else if (failed.length) {
        showToast(t("settings.savebar.not-saved", { parts: dashSettingsNames(failed) }), "error");
    } else if (warning) {
        showToast(warning, "error");
    } else if (saved.length) {
        showToast(t("settings.savebar.saved", { parts: dashSettingsNames(saved) }), "success");
    }
}

// Save changes: every unit that holds a change, in a fixed order. A unit that
// fails keeps its draft; the others keep going. A question cancelled stops the run.
function dashSettingsSaveAll() {
    if (dashSettings.saving || dashSettings.lock || !dashSettingsCount()) return Promise.resolve(null);
    if (dashSettingsCheckAll()) return Promise.resolve(null);
    dashSettings.saving = true;
    dashSaveBarSync();
    dashDbDraw(false);
    var results = {};
    var stopped = false;
    var run = Promise.resolve();
    DASH_SET_ORDER.forEach(function (name) {
        run = run.then(function () {
            var p = DASH_SET_PANELS[name];
            // A database change locks the page: nothing more is sent
            if (stopped || dashSettings.lock || !dashSetUnitCount(p)) return null;
            return p.save().then(function (r) {
                results[name] = r;
                if (r === "cancelled") stopped = true;
            });
        });
    });
    return run.then(function () {
        dashSettings.saving = false;
        dashSaveBarSync();
        dashDbDraw(false);
        dashSettingsReport(results);
        return results;
    }, function () {
        dashSettings.saving = false;
        dashSaveBarSync();
        dashDbDraw(false);
        return results;
    });
}

// ---------------------------------------------------------------------------
// Checks before a save (SV-1): the first field that fails opens its section, is
// marked with a note under it, takes the focus, and a toast names it
// ---------------------------------------------------------------------------

function dashSettingsClearChecks() {
    document.querySelectorAll("[data-check-failed]").forEach(function (el) {
        el.removeAttribute("data-check-failed");
        el.removeAttribute("aria-invalid");
        var note = document.getElementById(el.getAttribute("data-check-note") || "");
        if (note) { note.textContent = ""; note.classList.add("hidden"); }
    });
}

function dashSettingsCheckAll() {
    dashSettingsClearChecks();
    var problems = [];
    DASH_SET_ORDER.forEach(function (name) {
        var p = DASH_SET_PANELS[name];
        if (!p.check || !dashSetUnitCount(p)) return;
        var x = p.check();
        if (x) problems.push({ p: p, x: x });
    });
    if (!problems.length) return null;
    problems.forEach(function (item) {
        var el = document.getElementById(item.x.id);
        var note = document.getElementById(item.x.id + "-note");
        if (el) {
            el.setAttribute("aria-invalid", "true");
            el.setAttribute("data-check-failed", "");
            el.setAttribute("data-check-note", item.x.id + "-note");
        }
        if (note) { note.textContent = item.x.text; note.classList.remove("hidden"); }
    });
    var first = problems[0];
    dashSettingsOpen(first.p.section, { push: dashSettings.section !== first.p.section, field: first.x.id });
    showToast(problems.length === 1 ? t("settings.check.field-problem", { label: first.x.label, problem: first.x.text })
        : tn("settings.check.fields-problem", problems.length, { label: first.x.label }), "error");
    return first;
}

// ---------------------------------------------------------------------------
// Sections: the list, the picker on narrow screens, the address (FS-10, NL-3)
// ---------------------------------------------------------------------------

function dashSettingsInfo(id) {
    return SETTINGS_SECTIONS.filter(function (s) { return s.id === id; })[0] || null;
}

function dashSettingsNavHtml() {
    var html = "";
    var group = "";
    SETTINGS_SECTIONS.forEach(function (s) {
        if (s.group !== group) {
            group = s.group;
            html += '<p class="ui-settings-group">' + esc(t(s.group)) + "</p>";
        }
        html += '<a class="ui-settings-link" data-settings-nav="' + s.id + '" href="' + esc(basePath() + PAGES.settings.path + "/" + s.id) + '">' + esc(t(s.label)) + "</a>";
    });
    return html;
}

function dashSettingsPickHtml() {
    var html = "";
    var groups = [];
    SETTINGS_SECTIONS.forEach(function (s) { if (groups.indexOf(s.group) === -1) groups.push(s.group); });
    groups.forEach(function (g) {
        html += '<optgroup label="' + esc(t(g)) + '">' + SETTINGS_SECTIONS.filter(function (s) { return s.group === g; }).map(function (s) {
            return '<option value="' + s.id + '">' + esc(t(s.label)) + "</option>";
        }).join("") + "</optgroup>";
    });
    return html;
}

// Show a section. opts.push adds a Back step; opts.replace corrects the address;
// opts.field takes the focus to one field (a link from the search names it)
function dashSettingsOpen(id, opts) {
    var o = opts || {};
    var info = dashSettingsInfo(id);
    if (!info) {
        info = SETTINGS_SECTIONS[0];
        o.replace = true;
        o.push = false;
    }
    var changed = dashSettings.section !== info.id;
    dashSettings.section = info.id;
    if (o.push || o.replace) uiSetPageSection("settings", info.id, !o.push);
    document.querySelectorAll("[data-settings-section]").forEach(function (el) {
        el.classList.toggle("hidden", el.getAttribute("data-settings-section") !== info.id);
    });
    document.querySelectorAll("[data-settings-nav]").forEach(function (a) {
        if (a.getAttribute("data-settings-nav") === info.id) a.setAttribute("aria-current", "true");
        else a.removeAttribute("aria-current");
    });
    var pick = document.getElementById("settings-pick");
    if (pick && pick.value !== info.id) pick.value = info.id;
    document.title = t("settings.document-title", { section: t(info.label), app: t(uiAppTitle) });
    var main = uiMainScroller();
    var field = o.field ? document.getElementById(o.field) : null;
    if (field && field.offsetParent !== null) {
        // Only the app's own scroll area moves, never the window (MO-1, FS-10)
        if (main) main.scrollTop = Math.max(0, field.getBoundingClientRect().top - main.getBoundingClientRect().top + main.scrollTop - 80);
        field.focus({ preventScroll: true });
    } else if (changed && main && (o.push || o.replace)) {
        // A newly opened section starts at its top
        var content = document.getElementById("settings-content");
        if (content && content.getBoundingClientRect().top < main.getBoundingClientRect().top) {
            main.scrollTop = Math.max(0, content.getBoundingClientRect().top - main.getBoundingClientRect().top + main.scrollTop - 8);
        }
    }
}

// ---------------------------------------------------------------------------
// The page's events
// ---------------------------------------------------------------------------

function dashSettingsBind() {
    if (dashSettings.bound) return;
    dashSettings.bound = true;
    var page = document.querySelector('[data-page="settings"]');
    var nav = document.getElementById("settings-nav");
    if (nav) nav.innerHTML = dashSettingsNavHtml();
    var pick = document.getElementById("settings-pick");
    if (pick) {
        pick.innerHTML = dashSettingsPickHtml();
        pick.addEventListener("change", function () { dashSettingsOpen(pick.value, { push: true }); });
    }
    // Typing, a picked value (change) and a flipped switch (both) update the draft
    var edited = function (event) {
        var target = event.target;
        if (target.hasAttribute("data-check-failed")) {
            target.removeAttribute("data-check-failed");
            target.removeAttribute("aria-invalid");
            var note = document.getElementById(target.getAttribute("data-check-note") || "");
            if (note) { note.textContent = ""; note.classList.add("hidden"); }
        }
        var form = target.closest("form");
        var p = form && DASH_SET_PANELS[DASH_SET_FORMS[form.id]];
        if (p) {
            p.input();
            dashSaveBarSync();
            dashDbDraw(false);
        }
    };
    page.addEventListener("input", edited);
    page.addEventListener("change", edited);
    // Enter in a field saves like the save bar
    page.addEventListener("submit", function (event) {
        if (!DASH_SET_PANELS[DASH_SET_FORMS[event.target.id]]) return;
        event.preventDefault();
        dashSettingsSaveAll();
    });
    page.addEventListener("click", function (event) {
        var target = event.target;
        var link = target.closest("[data-settings-nav]");
        if (link) {
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
            event.preventDefault();
            dashSettingsOpen(link.getAttribute("data-settings-nav"), { push: true });
            return;
        }
        var retry = target.closest("[data-settings-retry]");
        var discard = target.closest("[data-set-discard]");
        var keep = target.closest("[data-set-keep]");
        var reset = target.closest("[data-set-reset]");
        var time = target.closest("[data-disp-time]");
        var p;
        if (retry && (p = DASH_SET_PANELS[retry.getAttribute("data-settings-retry")])) {
            p.st.failed = "";
            p.draw();
            dashPanelLoad(p);
        } else if (discard && (p = DASH_SET_PANELS[discard.getAttribute("data-set-discard")])) {
            dashPanelDiscard(p);
        } else if (keep && (p = DASH_SET_PANELS[keep.getAttribute("data-set-keep")])) {
            dashPanelKeep(p);
        } else if (reset) {
            var form = reset.closest("form");
            p = form && DASH_SET_PANELS[DASH_SET_FORMS[form.id]];
            if (p && p.reset && !dashSettings.lock && !p.st.saving) {
                p.reset(reset.getAttribute("data-set-reset"));
                // The Reset button is gone now: the focus goes back to the field's row
                var row = form.querySelector(".ui-set-row:focus-within") || form;
                (row.querySelector("input:not([disabled]), select:not([disabled]), button") || form).focus({ preventScroll: true });
            }
        } else if (time) {
            dashDispPickTime(time.getAttribute("data-disp-time"));
        } else if (target.closest("[data-redis-replace]")) {
            if (!dashSettings.lock) dashRedisReplace(true);
        } else if (target.closest("[data-redis-keep]")) {
            dashRedisReplace(false);
        } else {
            return;
        }
        dashSaveBarSync();
        dashDbDraw(false);
    });
    document.getElementById("settings-save").addEventListener("click", function () { dashSettingsSaveAll(); });
    document.getElementById("settings-discard").addEventListener("click", dashSettingsDiscardAll);
    // The page's unsaved changes: Settings keeps them across its sections (SV-1)
    uiUnsavedRegister(function () {
        return dashSettingsCount() + (dashDb.dirty ? 1 : 0);
    }, function () {
        DASH_SET_ORDER.forEach(function (name) {
            var p = DASH_SET_PANELS[name];
            if (!p.st.baseSnap) return;
            if (name === "redis") dashRedis.replacing = false;
            p.st.draft = p.draftFrom(p.st.baseSnap);
            p.st.dirty = false;
            p.st.conflict = null;
            p.clearAsks(p.st);
            p.drawForm();
        });
        dashDbDiscard();
        dashSaveBarSync();
    });
}

// The section the address names, else the one left open, else the first (NL-4)
DASH_PAGE_IMPL.settings = function (info) {
    dashSettingsBind();
    dashDbBind();
    var asked = uiPageSection("settings");
    var id = asked || dashSettings.section || SETTINGS_SECTIONS[0].id;
    dashSettingsOpen(id, { replace: !asked || !dashSettingsInfo(asked), field: dashSettingsHashField() });
    DASH_SET_ORDER.forEach(function (name) { DASH_SET_PANELS[name].draw(); });
    dashDbDraw(false);
    dashSaveBarSync();
    if (info && info.fromHistory && dashSettings.loadedOnce) return;
    dashSettings.loadedOnce = true;
    Promise.all([dashSettingsLoadAll(), dashDbLoad()]).then(function () { uiRefreshLoaded("settings"); });
};

registerPageRefresh("settings", 30000, function (isCurrent) {
    // While the database changes, the flow reads by itself; nothing else is read
    if (dashSettings.lock) return Promise.resolve();
    return Promise.all([dashSettingsLoadAll(isCurrent), dashDbLoad(isCurrent)]);
});

// ---------------------------------------------------------------------------
// Database connection: shown here; a PostgreSQL connection saved by setup can be
// changed (host, user, password, TLS) for the same installation. Its own flow,
// outside the save bar (SV-1 exception: a job that may switch to a copy carrying
// other settings): it starts only with nothing unsaved and no save running, locks
// every Settings field before it sends, follows its job, and never sends the change
// or the second factor again. When the answer is lost it reads the settings to find
// the job; when nothing is found the result stays unknown, locked until a reload.
// ---------------------------------------------------------------------------

var dashDb = { snap: null, failed: "", refreshFailed: "", check: null, checkedFor: "", error: "", confirm: false, askConfirm: "",
    busy: "", dirty: false, seq: 0, bound: false, replacing: { password: false, ca: false }, caRemove: false, jobId: "", gen: 0 };

// What the failed check refused, as one sentence per case
var DASH_DB_REFUSED = {
    failed: "settings.database.nothing-saved-connection-failed",
    user: "settings.database.nothing-saved-user-unusable",
    foreign: "settings.database.nothing-saved-other-data",
    incomplete: "settings.database.nothing-saved-tables-missing",
    pending: "settings.database.nothing-saved-unfinished-copy",
    another: "settings.database.nothing-saved-another-installation",
};

function dashDbLoad(isCurrent) {
    var seq = ++dashDb.seq;
    return api("GET", "/admin/api/settings/database").then(function (res) {
        if (seq !== dashDb.seq || dashDb.busy || dashSettings.lock || (isCurrent && !isCurrent())) return;
        var d = res.data;
        if (!res.ok || !d || !d.connection) {
            var why = apiError(res, t("settings.database.the-server-did-not-answer"));
            if (dashDb.snap) dashDb.refreshFailed = why;
            else dashDb.failed = why;
            dashDbDraw(false);
            return;
        }
        // A job that runs or is about to start (another tab, or this one before a reload)
        if (d.job && (d.job.state === "running" || d.job.state === "pending")) {
            dashDb.snap = d;
            dashSettingsLockFor("running");
            if (d.job.state === "running" && d.job.id) dashDbFollow(d.job.id);
            else dashDbRecover();
            return;
        }
        var changed = !dashDb.snap || JSON.stringify(dashDb.snap.connection) !== JSON.stringify(d.connection) || dashDb.snap.editable !== d.editable;
        dashDb.failed = "";
        dashDb.refreshFailed = "";
        dashDb.snap = d;
        var form = document.getElementById("db-form");
        dashDbDraw(changed && !dashDb.dirty && !(form && uiAreaHeld(form)));
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
        uiDbFieldsHtml("dbs-", { host: c.host, port: c.port, database: c.database, user: c.user, sslmode: c.sslmode, caSaved: c.caPem }, true, true) +
        '<div id="dbs-result" aria-live="polite"></div>' +
        '<label class="ui-check-label hidden" id="dbs-confirm-row"><input type="checkbox" class="ui-check" id="dbs-confirm" /> <span id="dbs-confirm-text"></span></label>' +
        '<p class="ui-error hidden" id="dbs-err" role="alert"></p>' +
        '<div class="ui-settings-actions"><button type="button" class="ui-btn" id="dbs-check" data-inventory="settings.database.check">' + esc(t("settings.database.check-connection")) + '</button>' +
        '<button type="submit" class="ui-btn ui-btn-primary" id="dbs-save" data-inventory="settings.database.save" aria-describedby="dbs-blocked">' + esc(t("settings.database.change-connection")) + '</button>' +
        '<button type="button" class="ui-btn hidden" id="dbs-discard">' + esc(t("settings.database.discard-changes")) + '</button></div>' +
        '<p class="ui-hint hidden" id="dbs-blocked"></p>' +
        "</fieldset></form>";
}

// Why the change cannot start now ("" when it can): it never overlaps a save (P23)
function dashDbBlockReason() {
    if (dashSettings.saving) return t("settings.database.wait-for-the-save");
    if (dashSettingsCount() > 0) return t("settings.database.save-other-changes-first");
    return "";
}

// The notice while the database changes, after a lost answer, or when the result is unknown
function dashDbLockHtml() {
    var lock = dashSettings.lock;
    if (!lock) return "";
    if (lock === "unknown") {
        return dashSetAlert(" is-warn", t("settings.database.result-unknown-title"), esc(t("settings.database.result-unknown")),
            '<div class="ui-settings-actions"><button type="button" class="ui-btn ui-btn-sm" data-db-reload>' + esc(t("settings.database.reload-the-page")) + "</button></div>");
    }
    if (lock === "read-failed") {
        return dashSetAlert(" is-fail", t("settings.database.status-unread-title"), esc(t("settings.database.status-unread", { reason: dashDb.error || "" })),
            '<div class="ui-settings-actions"><button type="button" class="ui-btn ui-btn-sm" data-db-read-retry>' + esc(t("settings.database.try-again")) + "</button></div>");
    }
    return '<div class="ui-loading" role="status"><span class="ui-spin" aria-hidden="true"></span><p>' + esc(t("settings.database.change-in-progress")) + "</p></div>";
}

function dashDbDraw(redraw) {
    var body = document.getElementById("db-body");
    if (!body) return;
    var lockBox = document.getElementById("db-lock");
    if (lockBox) {
        var lockHtml = dashDbLockHtml();
        if (lockBox.getAttribute("data-drawn") !== lockHtml) {
            lockBox.setAttribute("data-drawn", lockHtml);
            lockBox.innerHTML = lockHtml;
        }
    }
    if (!dashDb.snap) {
        body.innerHTML = dashDb.failed ? dashFailed(t("settings.database.could-not-load-the-database-connection"), dashDb.failed, "data-db-retry") : dashLoading(t("settings.database.loading-the-database-connection"));
        return;
    }
    if (redraw || !body.firstChild || body.querySelector(".ui-loading, .ui-failed")) {
        dashDb.replacing = { password: false, ca: false };
        dashDb.caRemove = false;
        body.innerHTML = dashDbFormHtml();
    }
    var form = document.getElementById("db-form");
    if (!form) return;
    var blocked = dashDbBlockReason();
    document.getElementById("db-fields").disabled = !!dashDb.busy || !!dashSettings.lock;
    document.getElementById("dbs-check").textContent = dashDb.busy === "check" ? t("settings.database.checking") : t("settings.database.check-connection");
    var save = document.getElementById("dbs-save");
    save.textContent = dashSettings.lock ? t("settings.database.saving") : t("settings.database.change-connection");
    save.disabled = !!blocked;
    var why = document.getElementById("dbs-blocked");
    why.textContent = blocked;
    why.classList.toggle("hidden", !blocked);
    document.getElementById("dbs-discard").classList.toggle("hidden", !dashDb.dirty || !!dashDb.busy || !!dashSettings.lock);
    document.getElementById("dbs-result").innerHTML = uiDbNoteHtml(uiDbCheckNote(dashDb.check, "settings"));
    var row = document.getElementById("dbs-confirm-row");
    row.classList.toggle("hidden", !dashDb.askConfirm);
    document.getElementById("dbs-confirm-text").textContent = dashDb.askConfirm;
    document.getElementById("dbs-confirm").checked = dashDb.confirm;
    var err = document.getElementById("dbs-err");
    var text = dashDb.error || (dashDb.refreshFailed ? t("settings.database.could-not-refresh-the-database-connection", { reason: dashDb.refreshFailed }) : "");
    if (dashSettings.lock === "read-failed") text = "";
    err.textContent = text;
    err.classList.toggle("hidden", !text);
    uiDbSecretsSync("dbs-", dashDb.replacing, dashDb.caRemove);
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

// The fields as the API takes them; a secret goes only when the admin replaced it (FS-3)
function dashDbRead() {
    var body = uiDbFieldsRead("dbs-");
    if (!dashDb.replacing.password) delete body.password;
    var saved = dashDb.snap && dashDb.snap.connection && dashDb.snap.connection.caPem;
    if (body.sslmode === "verify-full") {
        if (dashDb.caRemove) {
            delete body.caPem;
            body.keepCa = false;
        } else if (saved && !dashDb.replacing.ca) {
            delete body.caPem;
            body.keepCa = true;
        }
    }
    return body;
}

function dashDbKey(fields) {
    return JSON.stringify(fields) + "|" + (fields.password ? "1" : "0");
}

function dashDbCheck() {
    var fields = dashDbRead();
    var key = dashDbKey(fields);
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
    if (dashDb.busy || dashSettings.lock || !dashDb.snap || !dashDb.snap.editable) return;
    if (dashDbBlockReason()) {
        dashDbDraw(false);
        return;
    }
    var fields = dashDbRead();
    var key = dashDbKey(fields);
    var checked = dashDb.check && dashDb.checkedFor === key ? Promise.resolve(dashDb.check) : dashDbCheck();
    checked.then(function (r) {
        if (!r) return;
        var note = uiDbCheckNote(r, "settings");
        if (!r.ok || (note && note.tone === " is-fail")) {
            dashDbFail(t((note && DASH_DB_REFUSED[note.kind]) || "settings.database.nothing-saved-check-failed"));
            return;
        }
        if (dashDb.askConfirm && !dashDb.confirm) {
            dashDbFail(t("settings.database.tick-the-box-to-confirm-then"));
            document.getElementById("dbs-confirm").focus();
            return;
        }
        // A save may have started while the check ran
        if (dashDbBlockReason()) {
            dashDbDraw(false);
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
                return dashDbSend(b);
            },
        });
    });
}

// Every field of every section is locked, and what was asked to confirm is cleared
function dashSettingsLockFor(state) {
    dashSettings.lock = state;
    DASH_SET_ORDER.forEach(function (name) {
        var p = DASH_SET_PANELS[name];
        p.clearAsks(p.st);
        if (document.getElementById(p.ids.form)) p.sync();
    });
    dashSaveBarSync();
    dashDbDraw(false);
}

function dashSettingsUnlock() {
    dashSettings.lock = "";
    DASH_SET_ORDER.forEach(function (name) {
        var p = DASH_SET_PANELS[name];
        if (document.getElementById(p.ids.form)) p.sync();
    });
    dashSaveBarSync();
    dashDbDraw(false);
}

// Send the change once. The page is locked before the request leaves, and the page,
// not the dialog, owns what comes back: a late answer is still followed (P8)
function dashDbSend(body) {
    var gen = ++dashDb.gen;
    dashDb.error = "";
    dashSettingsLockFor("sending");
    uiMutated();
    return api("PUT", "/admin/api/settings/database", body).then(function (res) {
        if (gen !== dashDb.gen) return res;
        var d = res.data || {};
        if (res.ok && d.job && d.job.id) {
            dashSettings.lock = "running";
            dashDbFollow(d.job.id);
        } else if (res.status === 409 && d.code === "busy" && d.job) {
            // Another change is on its way: follow it, never send this one again
            dashSettings.lock = "running";
            if (d.job.state === "running" && d.job.id) dashDbFollow(d.job.id);
            else dashDbRecover();
        } else if (res.status === 0) {
            // The answer was lost: the change may have started
            dashDbRecover();
        } else if (res.status === 503 && d.code && d.code !== "database_switching") {
            dashDbToRecovery();
        } else {
            // Refused before it started (a wrong code, a field): nothing changed
            dashSettingsUnlock();
        }
        return res;
    });
}

function dashDbToRecovery() {
    uiUnsavedRelease();
    window.location.href = basePath() + "/recovery";
}

// The operation runs on the server; follow it until it ends
function dashDbFollow(id) {
    var gen = dashDb.gen;
    dashDb.jobId = id;
    if (dashSettings.lock !== "running") dashSettingsLockFor("running");
    else dashDbDraw(false);
    var poll = function () {
        if (gen !== dashDb.gen) return;
        api("GET", "/admin/api/settings/database/jobs/" + encodeURIComponent(id)).then(function (res) {
            if (gen !== dashDb.gen) return;
            var d = res.data || {};
            var j = res.ok && d.job;
            if (j && j.state === "running") { setTimeout(poll, 500); return; }
            if (j) { dashDbEnded(j); return; }
            if (res.status === 401) return;
            // Saved, but the service could not start on the new connection: recovery mode
            if (res.status === 503 && d.code && d.code !== "database_switching") { dashDbToRecovery(); return; }
            if (res.status === 503 || res.status === 0) { setTimeout(poll, 1000); return; }
            // No such job here (another process, or it was forgotten): read where things stand
            dashDbRecover();
        });
    };
    poll();
}

// The settings read tells whether a change runs or is about to start. It never sends
// the change or the second factor again (P24, P26, P29, P30)
function dashDbRecover() {
    var gen = dashDb.gen;
    if (dashSettings.lock !== "recovering") dashSettingsLockFor("recovering");
    api("GET", "/admin/api/settings/database").then(function (res) {
        if (gen !== dashDb.gen) return;
        var d = res.data || {};
        if (res.ok && d.connection) {
            dashDb.snap = d;
            if (d.job && d.job.state === "running" && d.job.id) {
                dashSettings.lock = "running";
                dashDbFollow(d.job.id);
            } else if (d.job && d.job.state === "pending") {
                setTimeout(dashDbRecover, 1000);
            } else {
                dashDbUnknown();
            }
            return;
        }
        if (res.status === 401) return;
        if (res.status === 503 && d.code && d.code !== "database_switching") { dashDbToRecovery(); return; }
        if (res.status === 503 || res.status === 0) { setTimeout(dashDbRecover, 1000); return; }
        // Another error: stay locked, and offer to read again (only the read)
        dashDb.error = apiError(res, t("settings.database.the-server-did-not-answer"));
        dashSettingsLockFor("read-failed");
    });
}

// Nothing runs and nothing says how it ended: the connection read is shown, nothing is
// retried or assumed, and the page stays locked until it is reloaded (owner decision P29)
function dashDbUnknown() {
    dashDb.jobId = "";
    dashSettingsLockFor("unknown");
    dashDbDraw(true);
    dashDbContextChanged();
}

function dashDbEnded(j) {
    dashDb.jobId = "";
    if (j.state === "done") {
        dashDbDiscard();
        dashDb.snap = null;
        dashSettingsUnlock();
        dashDbDraw(true);
        showToast(j.result && j.result.warning ? j.result.warning : t("settings.database.database-connection-saved"), j.result && j.result.warning ? "warning" : "success");
        // Every section from the database now in use; then dates, zone and lists follow it
        dashSettingsReloadAll();
        dashDbLoad();
        dashDbContextChanged();
        return;
    }
    dashSettingsUnlock();
    if (j.code === "confirm_copy") {
        dashDb.askConfirm = t("settings.database.use-it-anyway-changes-made-after");
        dashDb.confirm = false;
    }
    dashDbFail(t("settings.database.nothing-was-changed", { reason: uiDbCodeText(j.code, j.error) }));
}

// After a switch: what each section shows is read again from scratch
function dashSettingsReloadAll() {
    DASH_SET_ORDER.forEach(function (name) {
        var p = DASH_SET_PANELS[name];
        var st = p.st;
        st.seq += 1;
        st.snap = st.baseSnap = st.draft = null;
        st.dirty = false;
        st.conflict = null;
        st.error = "";
        st.errorField = "";
        st.unknown = false;
        p.clearAsks(st);
        var body = document.getElementById(p.ids.body);
        if (body) body.innerHTML = "";
        var info = document.getElementById(p.ids.info);
        if (info) { info.innerHTML = ""; info.removeAttribute("data-drawn"); }
    });
    dashRedis.replacing = false;
    DASH_SET_ORDER.forEach(function (name) { DASH_SET_PANELS[name].draw(); });
    return dashSettingsLoadAll();
}

// The database in use may have changed: the zone and the date formats are read again
// and applied, every shown date is drawn again, then the pages drop what they kept
// from the old database (the ui:db-changed event, P27, P31)
function dashDbContextChanged() {
    return api("GET", "/admin/api/me").then(function (res) {
        var d = res.ok && res.data;
        if (d && d.user) {
            dash.zoneAsked = d.timeZone || "";
            uiSetAppZone(dash.zoneAsked);
            uiSetDatePrefs(d.dateFormat, d.timeFormat);
        }
        dashRedrawDates();
        // One step for every reader (R3): reads that started before the change are stale from here,
        // and each listener then starts its own reads, which no other listener invalidates
        uiDataReset();
        if (typeof window.dispatchEvent === "function" && typeof CustomEvent === "function") window.dispatchEvent(new CustomEvent("ui:db-changed"));
    });
}

function dashDbDiscard() {
    dashDb.dirty = false;
    dashDb.check = null;
    dashDb.askConfirm = "";
    dashDb.confirm = false;
    dashDb.error = "";
    dashDb.replacing = { password: false, ca: false };
    dashDb.caRemove = false;
    if (document.getElementById("db-body")) dashDbDraw(true);
}

// The second check before a sensitive change: an authenticator code or a passkey.
// o: title, text, pkText, submitText, send(proof) -> api promise. The page owns the
// request once sent: a late answer after the dialog closed is followed there, and
// the dialog never sends the proof again.
function dashStepUpDialog(o) {
    var d = null;
    var closed = false;
    var close = function () {
        closed = true;
        if (d) d.backdrop.remove();
    };
    var guardedClose = function () { uiCloseGuarded(d, close); };
    var formId = "stepup-form";
    d = uiDialog({
        title: o.title, size: "sm", close: guardedClose,
        body: '<div class="ui-form"><p id="stepup-text">' + esc(o.text) + "</p>" +
            '<div class="ui-field hidden" id="stepup-pk"><button type="button" class="ui-btn ui-btn-block" id="stepup-pk-btn">' + esc(t("settings.database.use-a-passkey")) + '</button>' +
            '<p class="ui-hint">' + esc(t("settings.database.or-enter-your-6-digit-code")) + '</p></div>' +
            '<div class="ui-field"><label class="ui-label" for="stepup-code">' + esc(t("settings.database.6-digit-code")) + '</label>' +
            '<input id="stepup-code" class="ui-input ui-code" inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="' + esc(t("settings.database.000000")) + '" required></div></div>',
        foot: '<p class="ui-error hidden" id="stepup-err" role="alert"></p><button type="button" class="ui-btn" data-cancel>' + esc(t("settings.database.cancel")) + '</button>' +
            '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">' + esc(o.submitText) + "</button>",
        form: { id: formId, onSubmit: function () { submit(); } },
    });
    uiGuardDialog(d);
    d.foot.querySelector("[data-cancel]").addEventListener("click", guardedClose);
    d.backdrop.addEventListener("ui-drop", function () { closed = true; });
    var code = d.body.querySelector("#stepup-code");
    var err = d.foot.querySelector("#stepup-err");
    var btn = d.foot.querySelector("[type=submit]");
    var pkBtn = d.body.querySelector("#stepup-pk-btn");
    var busy = false;
    var sent = false;
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
        sent = true;
        o.send(proof).then(function (res) {
            var refused = !res.ok && res.status !== 0 && res.status !== 401 && !dashSettings.lock;
            if (!live()) {
                // The dialog was closed meanwhile: the page reports how it ended
                if (refused) showToast(apiError(res, t("settings.database.verification-failed")), "error");
                return;
            }
            setBusy(false);
            if (refused) {
                sent = false;
                fail(apiError(res, t("settings.database.verification-failed")));
                if (withCode) code.select();
                return;
            }
            // Started, or followed by the page while its answer is unknown
            close();
        });
    };
    pkBtn.addEventListener("click", function () {
        if (busy || sent) return;
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
        if (busy || sent) return;
        var value = code.value.trim();
        if (!value) { fail(t("settings.database.enter-the-code-from-your-authenticator")); code.focus(); return; }
        err.classList.add("hidden");
        setBusy(true);
        send({ code: value }, true);
    }
}

function dashDbReplace(which) {
    if (dashSettings.lock || dashDb.busy) return;
    if (which === "ca-remove") {
        dashDb.caRemove = !dashDb.caRemove;
        if (dashDb.caRemove) dashDb.replacing.ca = false;
    } else {
        dashDb.replacing[which] = true;
        if (which === "ca") dashDb.caRemove = false;
    }
    dashDb.dirty = true;
    dashDbEdited();
    var el = document.getElementById(which === "password" ? "dbs-password" : which === "ca" ? "dbs-ca" : "dbs-ca-remove");
    if (el && which !== "ca-remove") el.focus();
}

// A change of the form drops a check that no longer matches it
function dashDbEdited() {
    dashDb.error = "";
    if (dashDb.check) {
        dashDb.check = null;
        dashDb.askConfirm = "";
        dashDb.confirm = false;
    }
    dashDbDraw(false);
    dashSaveBarSync();
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
        // Another server needs the password again: the field opens (FS-5)
        var c = dashDb.snap && dashDb.snap.connection;
        if (event.type === "change" && c && (target.id === "dbs-host" || target.id === "dbs-port") && !dashDb.replacing.password) {
            var f = uiDbFieldsRead("dbs-");
            if (f.host !== (c.host || "") || f.port !== (c.port || 5432)) dashDbReplace("password");
        }
        dashDb.dirty = true;
        dashDbEdited();
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
        var secret = target.closest("[data-db-secret]");
        if (target.closest("[data-db-retry]")) {
            dashDb.failed = "";
            dashDbDraw(false);
            dashDbLoad();
        } else if (target.closest("[data-db-read-retry]")) {
            dashDb.error = "";
            dashDbRecover();
        } else if (target.closest("[data-db-reload]")) {
            window.location.reload();
        } else if (secret) {
            dashDbReplace(secret.getAttribute("data-db-secret"));
        } else if (target.closest("#dbs-check")) {
            if (!dashDb.busy && !dashSettings.lock) dashDbCheck();
        } else if (target.closest("#dbs-discard")) {
            dashDbDiscard();
            dashSaveBarSync();
        }
    });
}

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
    // A typed name or code is asked about before it is dropped (DS-9)
    var guardedClose = function () { uiCloseGuarded(d, close); };
    var formId = "pk-add-form";
    d = uiDialog({
        title: t("account.passkeys.add-a-passkey"), size: "sm", close: guardedClose,
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
    d.foot.querySelector("[data-cancel]").addEventListener("click", guardedClose);
    d.backdrop.addEventListener("ui-drop", function () { closed = true; });
    var nameEl = d.body.querySelector("#pk-name");
    var codeEl = d.body.querySelector("#pk-code");
    var pkBtn = d.body.querySelector("#pk-confirm-pk");
    var go = d.foot.querySelector("#pk-go");
    var cancel = d.foot.querySelector("[data-cancel]");
    var err = d.foot.querySelector("#pk-err");
    var st = { step: 1, busy: false, publicKey: null, ceremony: "", credential: null };
    nameEl.value = uiPasskeyDefaultName(window.navigator);
    uiGuardDialog(d);

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
                // The dialog was closed while the passkey was being saved: the page owns the
                // request and says how it ended; nothing is sent again (P8)
                if (res.ok) {
                    dashPkReload(true);
                    showToast(t("account.passkeys.passkey-added-you-can-now-sign"), "success");
                } else if (res.status !== 401) {
                    showToast(apiError(res, t("account.passkeys.the-passkey-was-not-saved-start")), "error");
                }
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
