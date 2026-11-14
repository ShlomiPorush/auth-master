/*
 * The admin dashboard: the shell (navigation, Needs attention, preferences) and
 * its pages: Tokens, Zones, API Keys, Activity Log, Access Logs, Usage and Settings.
 *
 * Builds on js/ui.js (builders, router, refresh scheduler, date helpers) and
 * js/common.js (requests). Everything from the server is escaped with esc() or
 * set as text; handlers are attached here, never written inline.
 */

// The router's pages: every one registers a refresh below (services/tests/test_ui_guards.py)
var PAGES = {
    tokens: { path: "/admin/tokens", title: "Tokens" },
    zones: { path: "/admin/zones", title: "Zones" },
    apikeys: { path: "/admin/api-keys", title: "API Keys" },
    activity: { path: "/admin/logs/activity", title: "Activity Log" },
    access: { path: "/admin/logs/access", title: "Access Logs" },
    usage: { path: "/admin/usage", title: "Usage" },
    settings: { path: "/admin/settings", title: "Settings" },
};

var TOKEN_FLAGS = ["read", "write", "delete"];
var TOKEN_STATUS = {
    active: { label: "Active", tone: "ok" },
    disabled: { label: "Disabled", tone: "" },
    expired: { label: "Expired", tone: "warn" },
};
var TOKEN_EXPIRY_SOON_MS = 7 * 24 * 3600 * 1000;
var TOKEN_LOCKED_REASON = "Created before stored tokens were encrypted, so it cannot be copied.";
var API_KEY_LOCKED_REASON = "Created before stored keys were encrypted, so it cannot be copied.";
var API_KEY_SCOPES = [
    { value: "validate", text: "Validate tokens" },
    { value: "tokens:read", text: "List tokens &amp; zones" },
    { value: "tokens:write", text: "Create/edit/delete tokens" },
    { value: "zones:read", text: "List zones" },
    { value: "zones:write", text: "Create zones" },
];
var API_KEY_DEFAULT_SCOPES = ["validate", "tokens:read", "zones:read"];
var LOG_PAGE_SIZE = 30;
var LOG_SINCE_LIMIT = 500;
var LOG_REFRESH_MAX_PAGES = 20;
var SESSION_CHANGED_TEXT = "Your session changed. Reload the page.";

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
        else if (res.status === 403 && res.data && res.data.detail === "CSRF") showToast(SESSION_CHANGED_TEXT, "error");
        return res;
    });
}

// The server's reason, else the fallback the old UI showed
function apiError(res, fallback) {
    var d = res && res.data;
    if (d && typeof d.detail === "string" && d.detail) return d.detail;
    if (d && Array.isArray(d.detail)) {
        return d.detail.map(function (e) {
            var loc = Array.isArray(e.loc) ? e.loc.filter(function (x) { return x !== "body"; }).join(".") : "";
            return (loc ? loc + ": " : "") + (e.msg || "");
        }).join("; ");
    }
    if (d && typeof d.error === "string" && d.error) return d.error;
    if (res && res.error === "timeout") return "The server did not answer in time. Try again in a moment.";
    if (res && res.error === "network") return "Could not reach the server. Check your connection and try again.";
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
    showToast("Your session expired. Sign in again.", "error");
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
            store.failed = apiError(res, "Failed");
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
            dash.zonesFailed = apiError(res, "Failed");
            return dash.zones;
        }
        dash.zonesFailed = "";
        dash.zones = res.data;
        dash.zonesLoaded = true;
        if (res.data.length) {
            dash.areas = res.data.map(function (z) { return z.name; });
            return dash.zones;
        }
        // No zones: the areas from ALLOWED_AREAS, read again from /me
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
                dashBootFailed(apiError(me, "Failed"));
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
    box.innerHTML = '<div class="ui-failed" role="alert"><b>Could not load the portal</b><p class="ui-muted">' + esc(reason) +
        '</p><button type="button" class="ui-btn" id="boot-retry">Try again</button></div>';
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
        if (id === "activity" || id === "access" || id === "usage" || id === "settings") more.setAttribute("aria-current", "page");
        else more.removeAttribute("aria-current");
    }
    var title = document.getElementById("mtop-page");
    if (title) title.textContent = PAGES[id].title;
    if (DASH_PAGE_IMPL[id]) DASH_PAGE_IMPL[id](info || {});
}

function dashBindShell() {
    document.querySelectorAll("[data-page-link]").forEach(function (a) {
        var page = PAGES[a.getAttribute("data-page-link")];
        a.setAttribute("href", basePath() + page.path);
        // The rail hides the labels; the name stays for screen readers and as a tooltip
        if (a.classList.contains("ui-nav-item")) {
            a.setAttribute("aria-label", page.title);
            a.setAttribute("title", page.title);
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

var DASH_THEMES = [["system", "System"], ["light", "Light"], ["dark", "Dark"]];

function dashThemeLabel() {
    var pref = typeof window.uiThemeGet === "function" ? window.uiThemeGet() : "system";
    var name = DASH_THEMES.filter(function (t) { return t[0] === pref; })[0][1];
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
        var label = b.querySelector(".ui-tool-label");
        if (label) label.textContent = "Theme: " + name;
        var fly = b.querySelector(".ui-fly");
        if (fly) fly.textContent = "Theme: " + name;
        b.setAttribute("aria-label", "Theme: " + name + ". Click to change");
    });
}

function dashThemeCycle() {
    var pref = window.uiThemeGet();
    var i = DASH_THEMES.map(function (t) { return t[0]; }).indexOf(pref);
    window.uiThemeSet(DASH_THEMES[(i + 1) % DASH_THEMES.length][0]);
}

function dashLogout() {
    var go = function () {
        api("POST", "/admin/api/logout").then(function (res) {
            if (!res.ok && res.status !== 401) {
                showToast("Could not sign out. Try again in a moment.", "error");
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
            (uiPageCurrent === id ? ' aria-current="page"' : "") + '><span class="ui-nav-label">' + esc(PAGES[id].title) + "</span></a>";
    };
    d = uiDialog({
        title: "More", size: "sm", backdropClass: "ui-more-layer", close: close, backdropClose: close,
        body: '<nav class="ui-more-list" aria-label="All pages">' + link("tokens") + link("zones") + link("apikeys") + link("activity") + link("access") + link("usage") + link("settings") +
            '<button type="button" class="ui-tool" data-more-prefs><span class="ui-tool-label">Preferences</span></button>' +
            '<button type="button" class="ui-tool" data-more-theme><span class="ui-tool-label"></span></button>' +
            '<p class="ui-side-user">Signed in as&nbsp;<bdi dir="auto">' + esc(dash.user) + "</bdi></p>" +
            '<button type="button" class="ui-tool" data-more-logout><span class="ui-tool-label">Log out</span></button></nav>',
    });
    dashMore = d;
    var themeBtn = d.body.querySelector("[data-more-theme]");
    var themeText = function () {
        var pref = window.uiThemeGet();
        themeBtn.firstChild.textContent = "Theme: " + DASH_THEMES.filter(function (t) { return t[0] === pref; })[0][1];
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
        ? "The server's time zone (" + dash.zoneAsked + ") is not one this browser knows, so times are shown in UTC."
        : "Times are shown in " + uiAppZone + ", the server's time zone.";
    var seg = function (name, items, current) {
        return '<div class="ui-seg" role="group" aria-label="' + esc(name) + '">' + items.map(function (it) {
            return '<button type="button" data-value="' + esc(it[0]) + '" aria-pressed="' + (it[0] === current) + '">' + esc(it[1]) + "</button>";
        }).join("") + "</div>";
    };
    d = uiDialog({
        title: "Preferences", size: "sm", close: close, backdropClose: close,
        body: '<div class="ui-form">' +
            '<div class="ui-field"><span class="ui-label">Theme</span>' + seg("Theme", DASH_THEMES, window.uiThemeGet()) + "</div>" +
            '<div class="ui-field"><label class="ui-label" for="pref-date">Date Format</label><select id="pref-date" class="ui-select">' +
            UI_DATE_FORMATS.map(function (f) { return '<option value="' + f[0] + '"' + (uiDatePrefs.date === f[0] ? " selected" : "") + ">" + esc(f[1]) + "</option>"; }).join("") +
            '</select><p class="ui-hint">Select your date format. Default: Browser order.</p></div>' +
            '<div class="ui-field"><span class="ui-label">Time Format</span>' + seg("Time Format", UI_TIME_FORMATS, uiDatePrefs.time) +
            '<p class="ui-hint">Select your time format. Default: 24-hour.</p></div>' +
            '<p class="ui-hint" id="pref-example" aria-live="polite"></p>' +
            '<p class="ui-hint">' + esc(zoneNote) + "</p></div>",
        foot: '<button type="button" class="ui-btn" data-cancel>Close</button>',
    });
    d.foot.querySelector("[data-cancel]").addEventListener("click", close);
    var dateSel = d.body.querySelector("#pref-date");
    uiPicker(dateSel, { label: "Date Format", field: true });
    var segs = d.body.querySelectorAll(".ui-seg");
    var example = function () { d.body.querySelector("#pref-example").textContent = "Example: " + formatTime(Date.now(), { seconds: false }); };
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
}

// ===========================================================================
// Needs attention (NL-19): the bell on every page, its own refresher
// ===========================================================================

var dashAttn = { health: null, healthFailed: false, checked: false, dialog: null, bell: null, drawn: "" };

function dashAttnItems() {
    var items = [];
    var h = dashAttn.health;
    if (h && h.db === false) items.push({ tone: "fail", text: "Database is not responding", sub: "Tokens cannot be checked or changed until it answers." });
    if (h && h.redis === false) items.push({ tone: "fail", text: "Redis is not responding", sub: "Sessions and the token cache need it." });
    var n = dashExpiringCount();
    if (n > 0) {
        items.push({
            tone: "warn", text: n === 1 ? "1 token expires within 7 days" : uiNum(n) + " tokens expire within 7 days",
            sub: "Replace or extend them before they stop working.", go: "tokens", query: "status=expiring", action: "Open tokens",
        });
    }
    return items;
}

function dashExpiringCount() {
    var rows = dashTokenStore.rows || [];
    var now = Date.now();
    return rows.filter(function (t) { return dashTokStatus(t) === "active" && dashTokExpiring(t, now); }).length;
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
    if (items.length) label = "Needs attention, " + uiCountLabel(items.length, "item", "items");
    else if (unknown) label = "Needs attention, some checks could not run";
    else if (!dashAttn.checked) label = "Needs attention, not checked yet";
    else label = "Needs attention, nothing open";
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
        if (!h) return dashAttn.healthFailed ? ["Unknown", ""] : ["Not checked yet", ""];
        return v ? ["Connected", "ok"] : ["Not responding", "fail"];
    };
    [["db", h ? h.db : null], ["redis", h ? h.redis : null]].forEach(function (pair) {
        var w = word(pair[1]);
        document.querySelectorAll('[data-status="' + pair[0] + '"]').forEach(function (row) {
            row.querySelector(".ui-dot").className = "ui-dot" + (w[1] ? " ui-dot-" + w[1] : "");
            row.querySelector("b").textContent = w[0];
            row.setAttribute("title", (pair[0] === "db" ? "Database" : "Redis") + ": " + w[0]);
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
        note = '<p class="ui-attn-note" role="status">Could not check the service. <button type="button" class="ui-btn-link" data-attn-retry>Retry</button></p>';
    } else if (!dashAttn.checked) {
        note = '<p class="ui-attn-note" role="status">Not checked yet</p>';
    } else if (!items.length) {
        note = '<p class="ui-attn-note"><b>Nothing needs attention right now.</b></p>';
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
    var d = uiDialog({ title: "Needs attention", size: "sm", backdropClass: "ui-attn-layer", close: close, backdropClose: close });
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
        '<button type="button" class="ui-btn ui-btn-sm" ' + retryAttr + ">Try again</button></div>";
}

function dashEmpty(title, line) {
    return '<div class="ui-empty"><b>' + esc(title) + "</b>" + (line ? "<p>" + line + "</p>" : "") + "</div>";
}

function dashMenuButton(name) {
    return '<button type="button" class="ui-icon-btn" data-row-menu aria-haspopup="menu" aria-expanded="false" aria-label="Actions for ' + esc(name) +
        '" title="Actions">' + UI_DOTS_ICON + "</button>";
}

function dashTag(text, tone) {
    return '<span class="ui-tag' + (tone ? " ui-tag-" + tone : "") + '">' + esc(text) + "</span>";
}

// A header label that sorts: the cell carries aria-sort, the label is a button (TB-1)
function dashSortHead(state, col) {
    return '<div class="ui-td" role="columnheader" aria-sort="' + uiSortAria(state, col.key) + '"><button type="button" class="ui-sort" data-sort="' +
        esc(col.key) + '">' + esc(col.label) + "</button></div>";
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
        uiSortToggle(cfg.state, col);
        cfg.redraw();
        var again = head.querySelector('[data-sort="' + col.key + '"]');
        if (again) again.focus({ preventScroll: true });
    });
    if (sortBtn) {
        sortBtn.addEventListener("click", function () {
            uiSortMenu(sortBtn, cfg.name, cfg.cols, cfg.state, cfg.redraw);
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

// The Sort button's dot: an order other than the default (TB-28)
function dashSortButtonSync(id, state, name, cols) {
    var b = document.getElementById(id);
    if (!b) return;
    var col = cols.filter(function (c) { return c.key === state.key; })[0];
    var text = col ? "Sort: " + col.label + ", " + uiSortWords(col.kind, state.dir) : "Sort: Default order";
    b.classList.toggle("is-set", !!col);
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
    { key: "name", label: "Name", kind: "text" },
    { key: "status", label: "Status", kind: "text" },
    { key: "lastUsed", label: "Last Used", kind: "date" },
];

function dashTokStatus(t) {
    if (!t.isActive) return "disabled";
    if (t.isExpired) return "expired";
    return "active";
}

function dashTokExpiring(t, now) {
    var ms = uiParseInstant(t.expiresAt);
    return !isNaN(ms) && ms > now && ms - now <= TOKEN_EXPIRY_SOON_MS;
}

// One filter function for the list and every count (each count ignores its own filter)
function dashTokMatch(t, f, skip, now) {
    if (skip !== "q" && f.q) {
        var q = f.q.toLowerCase();
        if (String(t.name || "").toLowerCase().indexOf(q) === -1 && String(t.description || "").toLowerCase().indexOf(q) === -1) return false;
    }
    if (skip !== "status" && f.status !== "all") {
        var s = dashTokStatus(t);
        if (f.status === "expiring") { if (s !== "active" || !dashTokExpiring(t, now)) return false; }
        else if (s !== f.status) return false;
    }
    var grants = t.grants || [];
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
    if (!grants || !grants.length) return dashTag("No grants", "");
    return '<div class="ui-tags">' + grants.map(function (g) {
        var flags = String(g.level || "").split(",");
        var all = TOKEN_FLAGS.every(function (x) { return flags.indexOf(x) !== -1; });
        return '<span class="ui-tag ui-tag-line"><bdi dir="auto">' + esc(g.area) + "</bdi>: " + esc(all ? "all" : flags.join("+")) + "</span>";
    }).join("") + "</div>";
}

function dashTokRow(t, now) {
    var s = dashTokStatus(t);
    var st = TOKEN_STATUS[s];
    var soon = s === "active" && dashTokExpiring(t, now);
    var exp = t.expiresAt ? '<span class="ui-td-sub' + (soon ? " ui-text-warn" : "") + '">Exp: ' + uiTime(t.expiresAt) + "</span>" : "";
    return '<div class="ui-tr' + (s === "disabled" ? " is-off" : "") + '" role="row" tabindex="0" data-key="' + esc(t.id) + '" aria-label="' + esc(t.name) + '">' +
        '<div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + uiUserText(t.name) + "</span>" +
        (t.description ? '<span class="ui-td-sub">' + uiUserText(t.description) + "</span>" : "") + "</div>" +
        '<div class="ui-td ui-td-full ui-td-wrap" role="cell">' + dashGrantTags(t.grants) + "</div>" +
        '<div class="ui-td ui-td-status" role="cell">' + dashTag(st.label, st.tone) + (soon ? " " + dashTag("Expiring soon", "warn") : "") + "</div>" +
        '<div class="ui-td" role="cell"><span class="ui-unit">Last used </span>' + (t.lastUsedAt ? uiTime(t.lastUsedAt) : '<span class="ui-muted">-</span>') + exp + "</div>" +
        '<div class="ui-td ui-td-end" role="cell">' + dashMenuButton(t.name) + "</div></div>";
}

function dashTokFind(id) {
    return (dashTokenStore.rows || []).filter(function (t) { return t.id === id; })[0] || null;
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
    var want = '<option value="">All zones</option>' + areas.map(function (a) { return '<option value="' + esc(a) + '">' + esc(a) + "</option>"; }).join("");
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
    if (quiet && uiAreaHeld(rowsEl)) return;
    var rows = dashTokenStore.rows;
    var html;
    var now = Date.now();
    var f = dashTok.filters;
    dashTokSyncControls();
    if (!rows) {
        html = dashTokenStore.failed ? dashFailed("Could not load the tokens", dashTokenStore.failed, "data-tok-retry") : dashLoading("Loading tokens...");
        document.getElementById("tok-count").textContent = "";
    } else {
        var shown = uiSortRows(rows.filter(function (t) { return dashTokMatch(t, f, "", now); }), dashTok.sort, dashTokSortValue);
        document.getElementById("tok-count").textContent = uiCountLabel(shown.length, "token", "tokens");
        if (!rows.length) html = dashEmpty("No tokens yet. Click New Token to create one.", "A token lets a service call the zones you grant it.");
        else if (!shown.length) html = dashEmpty("No tokens match your search.", "Change the search or clear the filters.");
        else html = shown.map(function (t) { return dashTokRow(t, now); }).join("");
        // Each status option names how many tokens it lists, with the other filters (MP-1 hint)
        Array.prototype.forEach.call(document.getElementById("tok-status").options, function (opt) {
            var n = rows.filter(function (t) { return dashTokMatch(t, { q: f.q, status: opt.value, zone: f.zone, level: f.level }, "", now); }).length;
            opt.setAttribute("data-hint", uiNum(n));
        });
    }
    var filtered = f.q || f.status !== "all" || f.zone || f.level;
    document.getElementById("tok-clear").classList.toggle("hidden", !filtered);
    var head = document.getElementById("tok-head");
    var headHtml = DASH_TOK_COLS.slice(0, 1).map(function (c) { return dashSortHead(dashTok.sort, c); }).join("") +
        '<div class="ui-td" role="columnheader">Grants</div>' +
        DASH_TOK_COLS.slice(1).map(function (c) { return dashSortHead(dashTok.sort, c); }).join("") +
        '<div class="ui-td" role="columnheader"><span class="ui-sr">Actions</span></div>';
    if (head.innerHTML !== headHtml) head.innerHTML = headHtml;
    dashSortButtonSync("tok-sort", dashTok.sort, "tokens", DASH_TOK_COLS);
    if (html === dashTok.drawn) return;
    dashTok.drawn = html;
    uiKeepFocus(rowsEl, function () { rowsEl.innerHTML = html; });
}

function dashTokSortValue(t, key) {
    if (key === "name") return String(t.name || "");
    if (key === "status") return TOKEN_STATUS[dashTokStatus(t)].label;
    if (key === "lastUsed") return uiParseInstant(t.lastUsedAt);
    return null;
}

function dashTokLoad(force) {
    return Promise.all([dashTokensLoad(force), dashZonesLoad()]).then(function () { dashTokDraw(!force); });
}

function dashTokBind() {
    if (dashTok.bound) return;
    dashTok.bound = true;
    uiPicker(document.getElementById("tok-status"), { label: "Status" });
    uiPicker(document.getElementById("tok-zone"), { label: "Zone" });
    uiPicker(document.getElementById("tok-level"), { label: "Level" });
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
                showToast("Could not load the zones: " + (dash.zonesFailed || "Failed") + ". Try again in a moment.", "error");
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
            showToast(ok ? "Token copied to clipboard" : "Failed to copy token", ok ? "success" : "error");
        });
    });
    document.getElementById("tok-rows").addEventListener("click", function (event) {
        if (event.target.closest("[data-tok-retry]")) dashTokLoad(true);
    });
    dashBindList({
        head: "tok-head", rows: "tok-rows", sortButton: "tok-sort", name: "tokens", cols: DASH_TOK_COLS, state: dashTok.sort,
        redraw: function () { dashTokDraw(false); },
        open: function (id) { var t = dashTokFind(id); if (t) dashTokenDialog(t); },
        menu: dashTokMenu,
    });
}

function dashTokMenu(id, button) {
    var t = dashTokFind(id);
    if (!t) return;
    uiMenu(button, [
        { label: "Edit", action: "token-edit", run: function () { dashTokenDialog(t); } },
        { label: "Usage", action: "token-usage", run: function () { uiGo("usage", "token=" + encodeURIComponent(t.id)); } },
        { label: "Copy token", action: "token-copy", disabled: !t.canReveal, reason: TOKEN_LOCKED_REASON, run: function () { dashMfaDialog("token", t); } },
        { label: t.isActive ? "Disable" : "Enable", action: "token-toggle", run: function () { dashTokToggle(t, button); } },
        { label: "Delete", action: "token-delete", danger: true, run: function () { dashTokDelete(t); } },
    ], "Actions for " + t.name);
}

// One click, no question and no Undo (owner decision, DS-20): the row's button
// waits while it runs and the toast names the result
function dashTokToggle(t, button) {
    var next = !t.isActive;
    if (button) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
    }
    uiMutated();
    api("PATCH", "/admin/api/tokens/" + encodeURIComponent(t.id), { isActive: next }).then(function (res) {
        if (button) {
            button.disabled = false;
            button.removeAttribute("aria-busy");
        }
        if (!res.ok) {
            if (res.status !== 401) showToast("Could not " + (next ? "enable " : "disable ") + t.name + ": " + apiError(res, "Save failed"), "error");
            return;
        }
        showToast(t.name + (next ? " is enabled" : " is disabled"), "success");
        uiMutated();
        dashTokLoad(true);
    });
}

function dashTokDelete(t) {
    uiConfirm({ title: "Delete token", message: "Delete this token permanently?", confirmText: "Delete", danger: true }).then(function (ok) {
        if (!ok) return;
        uiMutated();
        api("DELETE", "/admin/api/tokens/" + encodeURIComponent(t.id)).then(function (res) {
            if (!res.ok) {
                if (res.status !== 401) showToast("Delete failed", "error");
                return;
            }
            showToast(t.name + " deleted", "success");
            uiMutated();
            dashTokLoad(true);
        });
    });
}

// The zone permission rows of the token dialog
function dashPermRows(selected) {
    return dash.areas.map(function (a) {
        var flags = selected[a] ? String(selected[a]).split(",") : [];
        var all = TOKEN_FLAGS.every(function (x) { return flags.indexOf(x) !== -1; });
        return '<div class="ui-perm" data-area="' + esc(String(a).toLowerCase()) + '" data-area-name="' + esc(a) + '"><b><bdi dir="auto">' + esc(a) + "</bdi></b>" +
            TOKEN_FLAGS.map(function (fl) {
                return '<label><input type="checkbox" class="ui-check" data-flag="' + fl + '"' + (flags.indexOf(fl) !== -1 ? " checked" : "") +
                    ' aria-label="' + esc(fl.charAt(0).toUpperCase() + fl.slice(1) + ": " + a) + '"> ' + fl.charAt(0).toUpperCase() + fl.slice(1) + "</label>";
            }).join("") +
            '<label><input type="checkbox" class="ui-check" data-area-all' + (all ? " checked" : "") + ' aria-label="' + esc("All: " + a) + '"> All</label></div>';
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
function dashTokenDialog(t) {
    var editing = !!t;
    var d = null;
    var close = function () { if (d) d.backdrop.remove(); };
    var guardedClose = function () { uiCloseGuarded(d, close); };
    var selected = {};
    (editing ? t.grants || [] : []).forEach(function (g) { selected[g.area] = g.level; });
    var expiresInitial = editing && t.expiresAt ? uiToZonedInput(t.expiresAt) : "";
    var formId = "tok-form";
    var body = '<div class="ui-form">' +
        '<div class="ui-field"><label class="ui-label" for="tf-name">Name</label><input id="tf-name" class="ui-input" name="name" required dir="auto" autocomplete="off" placeholder="My API token" data-inventory="tokens.form.name"></div>' +
        '<div class="ui-field"><label class="ui-label" for="tf-desc">Description <small>(optional)</small></label><input id="tf-desc" class="ui-input" name="description" dir="auto" autocomplete="off" placeholder="' +
        (editing ? "Token description" : "What this token is used for") + '" data-inventory="tokens.form.description"></div>' +
        (editing
            ? '<div class="ui-field"><label class="ui-label" for="tf-token">Replace token (optional)</label><input id="tf-token" class="ui-input ui-mono" name="token" autocomplete="off" spellcheck="false" placeholder="Leave empty to keep current token" data-inventory="tokens.form.replace"></div>'
            : '<div class="ui-field"><label class="ui-label" for="tf-token">Existing token (optional)</label><input id="tf-token" class="ui-input ui-mono" name="token" autocomplete="off" spellcheck="false" placeholder="Leave empty to auto-generate" data-inventory="tokens.form.existing"></div>') +
        '<div class="ui-field"><span class="ui-label" id="tf-perm-label">Zone permissions</span>' +
        '<div class="ui-perm-tools"><label class="ui-search">' + DASH_SEARCH_ICON + '<input type="search" id="tf-filter" placeholder="Filter zones…" aria-label="Filter zones" data-inventory="tokens.form.zonefilter"></label>' +
        '<div class="ui-chips" role="group" aria-label="Set every zone to">' +
        '<button type="button" class="ui-btn ui-btn-sm" data-bulk="none">None</button>' +
        '<button type="button" class="ui-btn ui-btn-sm" data-bulk="read">Read</button>' +
        '<button type="button" class="ui-btn ui-btn-sm" data-bulk="write">Write</button>' +
        '<button type="button" class="ui-btn ui-btn-sm" data-bulk="delete">Delete</button>' +
        '<button type="button" class="ui-btn ui-btn-sm" data-bulk="all">All</button></div></div>' +
        '<div class="ui-perms" role="group" aria-labelledby="tf-perm-label">' + dashPermRows(selected) + "</div>" +
        '<p class="ui-picker-none hidden" id="tf-nomatch" role="status">No zones match the filter.</p></div>' +
        '<div class="ui-field"><label class="ui-label" for="tf-exp">Expires (optional)</label><input id="tf-exp" class="ui-input" type="datetime-local" name="expires" data-inventory="tokens.form.expires">' +
        '<p class="ui-hint">In ' + esc(uiZoneName()) + ". Leave empty for a token that does not expire. " +
        '<button type="button" class="ui-btn-link" id="tf-exp-clear">Clear</button></p></div></div>';
    var foot = '<p class="ui-error hidden" id="tf-err" role="alert"></p>' +
        '<button type="button" class="ui-btn" data-cancel>Cancel</button>' +
        (editing ? '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">Save</button>'
            : '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">Create</button>');
    d = uiDialog({
        title: editing ? "Edit Token" : "New Token", size: "md", close: guardedClose, body: body, foot: foot,
        form: { id: formId, onSubmit: function () { submit(); } },
    });
    var f = d.body;
    var nameEl = f.querySelector("#tf-name");
    var descEl = f.querySelector("#tf-desc");
    var tokEl = f.querySelector("#tf-token");
    var expEl = f.querySelector("#tf-exp");
    var err = d.foot.querySelector("#tf-err");
    if (editing) {
        nameEl.value = t.name || "";
        descEl.value = t.description || "";
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
        if (!name) { showErr("Name is required", nameEl); return; }
        var grants = dashPermGrants(f);
        if (!grants.length) { showErr("Select at least one area", null); return; }
        var payload = { name: name, description: descEl.value.trim(), grants: grants };
        var tokenValue = tokEl.value.trim();
        if (tokenValue) payload.token = tokenValue;
        if (editing) {
            // Sent only when changed, so the stored instant stays exactly as it was
            if (expEl.value !== expiresInitial) payload.expiresAt = expEl.value ? uiFromZonedInput(expEl.value) : "";
        } else if (expEl.value) {
            payload.expiresAt = uiFromZonedInput(expEl.value);
        }
        if (payload.expiresAt === null) { showErr("Expires: enter a date and time", expEl); return; }
        busy = true;
        var submitBtn = d.foot.querySelector("[type=submit]");
        submitBtn.disabled = true;
        uiMutated();
        var req = editing
            ? api("PATCH", "/admin/api/tokens/" + encodeURIComponent(t.id), payload)
            : api("POST", "/admin/api/tokens", payload);
        req.then(function (res) {
            busy = false;
            submitBtn.disabled = false;
            if (!res.ok) {
                if (res.status !== 401) showErr(editing ? apiError(res, "Save failed") : apiError(res, "Failed"), null);
                return;
            }
            close();
            uiMutated();
            if (editing) {
                showToast("Token updated", "success");
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
    d = uiDialog({
        title: "MFA Verification", size: "sm", close: close,
        body: '<div class="ui-form"><p>' + (kind === "token" ? "Enter your 6-digit authenticator code to copy the token." : "Enter your 6-digit authenticator code to copy the key.") + "</p>" +
            '<p class="ui-hint">' + (kind === "token" ? "Token" : "API key") + ': <bdi dir="auto">' + esc(item.name) + "</bdi></p>" +
            '<div class="ui-field"><label class="ui-label" for="mfa-code">6-digit code</label>' +
            '<input id="mfa-code" class="ui-input ui-code" inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="000000" required data-inventory="mfa.code"></div></div>',
        foot: '<p class="ui-error hidden" id="mfa-err" role="alert"></p><button type="button" class="ui-btn" data-cancel>Cancel</button>' +
            '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">Verify &amp; copy</button>',
        form: { id: formId, onSubmit: function () { submit(); } },
    });
    d.foot.querySelector("[data-cancel]").addEventListener("click", close);
    d.backdrop.addEventListener("ui-drop", function () { closed = true; });
    var code = d.body.querySelector("#mfa-code");
    var err = d.foot.querySelector("#mfa-err");
    var btn = d.foot.querySelector("[type=submit]");
    function submit() {
        var value = code.value.trim();
        if (!value) { err.textContent = "Enter the code from your authenticator app."; err.classList.remove("hidden"); code.focus(); return; }
        err.classList.add("hidden");
        btn.disabled = true;
        var path = kind === "token"
            ? api("POST", "/admin/api/tokens/" + encodeURIComponent(item.id) + "/reveal", { code: value })
            : api("POST", "/admin/api/api-keys/" + encodeURIComponent(item.id) + "/reveal", { code: value });
        path.then(function (res) {
            if (!dashRevealCurrent(d, closed, seq)) return;
            btn.disabled = false;
            if (!res.ok) {
                if (res.status === 401) return;
                err.textContent = apiError(res, "Verification failed");
                err.classList.remove("hidden");
                code.select();
                return;
            }
            var secret = kind === "token" ? (res.data && res.data.token) || "" : (res.data && res.data.key) || "";
            close();
            uiCopyText(secret).then(function (ok) {
                if (kind === "token") showToast(ok ? "Token copied to clipboard" : "Failed to copy token", ok ? "success" : "error");
                else showToast(ok ? "API key copied to clipboard" : "Failed to copy", ok ? "success" : "error");
            });
        });
    }
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

var dashZones = { q: "", sort: { key: "", dir: "" }, drawn: "", bound: false, loaded: false };
var DASH_ZONE_COLS = [{ key: "name", label: "Name", kind: "text" }, { key: "createdAt", label: "Created", kind: "date" }];

function dashZoneFind(id) {
    return dash.zones.filter(function (z) { return z.id === id; })[0] || null;
}

function dashZonesDraw(quiet) {
    if (!dashZones.bound) return;
    var rowsEl = document.getElementById("zone-rows");
    if (quiet && uiAreaHeld(rowsEl)) return;
    var html;
    var q = dashZones.q.toLowerCase();
    if (!dashZones.loaded) {
        html = dash.zonesFailed ? dashFailed("Could not load the zones", dash.zonesFailed, "data-zone-retry") : dashLoading("Loading zones...");
        document.getElementById("zone-count").textContent = "";
    } else {
        var shown = uiSortRows(dash.zones.filter(function (z) {
            return !q || String(z.name).toLowerCase().indexOf(q) !== -1 || String(z.description || "").toLowerCase().indexOf(q) !== -1;
        }), dashZones.sort, function (z, key) { return key === "name" ? String(z.name) : uiParseInstant(z.createdAt); });
        document.getElementById("zone-count").textContent = uiCountLabel(shown.length, "zone", "zones");
        if (!dash.zones.length) html = dashEmpty("No zones yet.", "Until you add one, tokens use the areas from <code>ALLOWED_AREAS</code>.");
        else if (!shown.length) html = dashEmpty("No zones match your search.", "");
        else html = shown.map(function (z) {
            return '<div class="ui-tr" role="row" tabindex="0" data-key="' + esc(z.id) + '" aria-label="' + esc(z.name) + '">' +
                '<div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + uiUserText(z.name) + "</span></div>" +
                '<div class="ui-td ui-td-full" role="cell">' + (z.description ? uiUserText(z.description) : '<span class="ui-muted">-</span>') + "</div>" +
                '<div class="ui-td" role="cell"><span class="ui-unit">Created </span>' + uiTime(z.createdAt) + "</div>" +
                '<div class="ui-td ui-td-end" role="cell">' + dashMenuButton(z.name) + "</div></div>";
        }).join("");
    }
    document.getElementById("zone-clear").classList.toggle("hidden", !dashZones.q);
    var head = document.getElementById("zone-head");
    var headHtml = dashSortHead(dashZones.sort, DASH_ZONE_COLS[0]) + '<div class="ui-td" role="columnheader">Description</div>' +
        dashSortHead(dashZones.sort, DASH_ZONE_COLS[1]) + '<div class="ui-td" role="columnheader"><span class="ui-sr">Actions</span></div>';
    if (head.innerHTML !== headHtml) head.innerHTML = headHtml;
    dashSortButtonSync("zone-sort", dashZones.sort, "zones", DASH_ZONE_COLS);
    if (html === dashZones.drawn) return;
    dashZones.drawn = html;
    uiKeepFocus(rowsEl, function () { rowsEl.innerHTML = html; });
}

function dashZonesLoadPage(quiet) {
    return dashZonesLoad().then(function () {
        if (dash.zonesLoaded) dashZones.loaded = true;
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
            err.textContent = "Zone name: enter a name";
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
                    err.textContent = apiError(res, "Failed to add zone");
                    err.classList.remove("hidden");
                }
                return;
            }
            form.reset();
            showToast("Zone " + name + " added", "success");
            uiMutated();
            dashZonesLoadPage(false);
        });
    });
    document.getElementById("zone-rows").addEventListener("click", function (event) {
        if (event.target.closest("[data-zone-retry]")) dashZonesLoadPage(false);
    });
    dashBindList({
        head: "zone-head", rows: "zone-rows", sortButton: "zone-sort", name: "zones", cols: DASH_ZONE_COLS, state: dashZones.sort,
        redraw: function () { dashZonesDraw(false); },
        open: function (id) { var z = dashZoneFind(id); if (z) dashZoneDialog(z); },
        menu: function (id, button) {
            var z = dashZoneFind(id);
            if (!z) return;
            uiMenu(button, [
                { label: "Edit", action: "zone-edit", run: function () { dashZoneDialog(z); } },
                { label: "Delete", action: "zone-delete", danger: true, run: function () { dashZoneDelete(z); } },
            ], "Actions for " + z.name);
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
    uiConfirm({ title: "Delete zone", message: "Delete this zone? Tokens referencing it may need updating.", confirmText: "Delete", danger: true }).then(function (ok) {
        if (!ok) return;
        uiMutated();
        api("DELETE", "/admin/api/zones/" + encodeURIComponent(z.id)).then(function (res) {
            if (!res.ok) {
                if (res.status !== 401) showToast(apiError(res, "Delete failed"), "error");
                return;
            }
            showToast("Zone " + z.name + " deleted", "success");
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
        title: "Edit Zone", size: "sm", close: guardedClose,
        body: '<div class="ui-form"><div class="ui-field"><label class="ui-label" for="ze-name">Name</label><input id="ze-name" class="ui-input" required autocomplete="off" data-inventory="zones.edit.name"></div>' +
            '<div class="ui-field"><label class="ui-label" for="ze-desc">Description</label><input id="ze-desc" class="ui-input" dir="auto" autocomplete="off" placeholder="What this zone controls" data-inventory="zones.edit.description"></div></div>',
        foot: '<p class="ui-error hidden" id="ze-err" role="alert"></p><button type="button" class="ui-btn" data-cancel>Cancel</button>' +
            '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">Save</button>',
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
        if (!name) { err.textContent = "Name is required"; err.classList.remove("hidden"); nameEl.focus(); return; }
        var btn = d.foot.querySelector("[type=submit]");
        btn.disabled = true;
        uiMutated();
        api("PATCH", "/admin/api/zones/" + encodeURIComponent(z.id), { name: name, description: descEl.value.trim() }).then(function (res) {
            btn.disabled = false;
            if (!res.ok) {
                if (res.status !== 401) { err.textContent = apiError(res, "Save failed"); err.classList.remove("hidden"); }
                return;
            }
            close();
            uiMutated();
            showToast("Zone updated", "success");
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

var dashKeys = { rows: null, failed: "", sort: { key: "", dir: "" }, drawn: "", bound: false };
var DASH_KEY_COLS = [{ key: "name", label: "Name", kind: "text" }, { key: "createdAt", label: "Created", kind: "date" }];

function dashKeyFind(id) {
    return (dashKeys.rows || []).filter(function (k) { return k.id === id; })[0] || null;
}

function dashKeysLoad(quiet) {
    var mark = uiMutationMark();
    return api("GET", "/admin/api/api-keys").then(function (res) {
        if (uiStaleSince(mark)) return;
        if (res.ok && Array.isArray(res.data)) {
            dashKeys.rows = res.data;
            dashKeys.failed = "";
        } else {
            dashKeys.failed = apiError(res, "Failed");
        }
        dashKeysDraw(quiet);
    });
}

function dashKeysDraw(quiet) {
    if (!dashKeys.bound) return;
    var rowsEl = document.getElementById("key-rows");
    if (quiet && uiAreaHeld(rowsEl)) return;
    var html;
    if (!dashKeys.rows) {
        html = dashKeys.failed ? dashFailed("Could not load the API keys", dashKeys.failed, "data-key-retry") : dashLoading("Loading API keys...");
        document.getElementById("key-count").textContent = "";
    } else {
        var rows = uiSortRows(dashKeys.rows, dashKeys.sort, function (k, key) { return key === "name" ? String(k.name) : uiParseInstant(k.createdAt); });
        document.getElementById("key-count").textContent = uiCountLabel(rows.length, "API key", "API keys");
        if (!rows.length) html = dashEmpty("No API keys yet", "Create one with New Key.");
        else html = rows.map(function (k) {
            var scopes = String(k.scopes || "").split(",").filter(Boolean);
            return '<div class="ui-tr" role="row" tabindex="0" data-key="' + esc(k.id) + '" aria-label="' + esc(k.name) + '">' +
                '<div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + uiUserText(k.name) + "</span></div>" +
                '<div class="ui-td ui-td-full ui-td-wrap" role="cell"><div class="ui-tags">' +
                scopes.map(function (s) { return '<span class="ui-tag ui-tag-line ui-mono">' + esc(s) + "</span>"; }).join("") + "</div></div>" +
                '<div class="ui-td" role="cell"><span class="ui-unit">Created </span>' + uiTime(k.createdAt) + "</div>" +
                '<div class="ui-td ui-td-end" role="cell">' + dashMenuButton(k.name) + "</div></div>";
        }).join("");
    }
    var head = document.getElementById("key-head");
    var headHtml = dashSortHead(dashKeys.sort, DASH_KEY_COLS[0]) + '<div class="ui-td" role="columnheader">Scopes</div>' +
        dashSortHead(dashKeys.sort, DASH_KEY_COLS[1]) + '<div class="ui-td" role="columnheader"><span class="ui-sr">Actions</span></div>';
    if (head.innerHTML !== headHtml) head.innerHTML = headHtml;
    dashSortButtonSync("key-sort", dashKeys.sort, "API keys", DASH_KEY_COLS);
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
    dashBindList({
        head: "key-head", rows: "key-rows", sortButton: "key-sort", name: "API keys", cols: DASH_KEY_COLS, state: dashKeys.sort,
        redraw: function () { dashKeysDraw(false); },
        open: function (id) { var k = dashKeyFind(id); if (k) dashKeyDialog(k); },
        menu: function (id, button) {
            var k = dashKeyFind(id);
            if (!k) return;
            uiMenu(button, [
                { label: "Edit", action: "apikey-edit", run: function () { dashKeyDialog(k); } },
                { label: "Copy key", action: "apikey-copy", disabled: k.canReveal === false, reason: API_KEY_LOCKED_REASON, run: function () { dashMfaDialog("apikey", k); } },
                { label: "Delete", action: "apikey-delete", danger: true, run: function () { dashKeyDelete(k); } },
            ], "Actions for " + k.name);
        },
    });
}

function dashKeyDelete(k) {
    uiConfirm({ title: "Delete API key", message: "Delete API key \"" + k.name + "\"?", confirmText: "Delete", danger: true }).then(function (ok) {
        if (!ok) return;
        uiMutated();
        api("DELETE", "/admin/api/api-keys/" + encodeURIComponent(k.id)).then(function (res) {
            if (res.ok) {
                showToast("API key deleted", "success");
                uiMutated();
                dashKeysLoad(false);
            } else if (res.status !== 401) {
                showToast("Failed to delete key", "error");
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
        title: editing ? "Edit API Key" : "New API Key", size: "sm", close: guardedClose,
        body: '<div class="ui-form"><div class="ui-field"><label class="ui-label" for="kf-name">Name</label><input id="kf-name" class="ui-input" required dir="auto" autocomplete="off" placeholder="e.g. n8n integration" data-inventory="apikeys.form.name"></div>' +
            '<div class="ui-field"><span class="ui-label" id="kf-scopes-label">Scopes</span><div class="ui-switch-list" role="group" aria-labelledby="kf-scopes-label">' +
            API_KEY_SCOPES.map(function (s, i) {
                return '<label class="ui-switch-row" for="kf-scope-' + i + '"><span><b class="ui-mono">' + esc(s.value) + "</b><small>" + s.text + "</small></span>" +
                    '<input type="checkbox" role="switch" class="ui-switch" id="kf-scope-' + i + '" value="' + esc(s.value) + '"' + (has.indexOf(s.value) !== -1 ? " checked" : "") + "></label>";
            }).join("") + "</div></div></div>",
        foot: '<p class="ui-error hidden" id="kf-err" role="alert"></p><button type="button" class="ui-btn" data-cancel>Cancel</button>' +
            (editing ? '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">Save</button>'
                : '<button type="submit" class="ui-btn ui-btn-primary" form="' + formId + '">Create</button>'),
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
        if (!name) { err.textContent = "Name is required"; err.classList.remove("hidden"); nameEl.focus(); return; }
        var scopes = Array.prototype.slice.call(d.body.querySelectorAll(".ui-switch:checked")).map(function (c) { return c.value; });
        if (!scopes.length) { err.textContent = "Select at least one scope"; err.classList.remove("hidden"); return; }
        var btn = d.foot.querySelector("[type=submit]");
        btn.disabled = true;
        uiMutated();
        var req = editing
            ? api("PATCH", "/admin/api/api-keys/" + encodeURIComponent(k.id), { name: name, scopes: scopes })
            : api("POST", "/admin/api/api-keys", { name: name, scopes: scopes });
        req.then(function (res) {
            btn.disabled = false;
            if (!res.ok) {
                if (res.status !== 401) { err.textContent = apiError(res, editing ? "Save failed" : "Failed"); err.classList.remove("hidden"); }
                return;
            }
            close();
            uiMutated();
            if (editing) showToast("API key updated", "success");
            else dashKeyCreated((res.data && res.data.key) || "");
            dashKeysLoad(false);
        });
    }
}

function dashKeyCreated(key) {
    var d = null;
    var close = function () { if (d) d.backdrop.remove(); };
    d = uiDialog({
        title: "API Key Created", size: "sm", close: close,
        body: '<div class="ui-form"><p>Copy the key. You can also copy it later from the API Keys table.</p><code class="ui-secret" id="kc-key"></code></div>',
        foot: '<button type="button" class="ui-btn" data-cancel>Close</button><button type="button" class="ui-btn ui-btn-primary" data-inventory="apikeys.created.copy">Copy to clipboard</button>',
    });
    d.body.querySelector("#kc-key").textContent = key;
    d.foot.querySelector("[data-cancel]").addEventListener("click", close);
    d.foot.querySelector(".ui-btn-primary").addEventListener("click", function () {
        uiCopyText(key).then(function (ok) { showToast(ok ? "Copied to clipboard" : "Failed to copy", ok ? "success" : "error"); });
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
        loadingText: "Loading activity...", emptyText: "No activity logs found.", failedText: "Failed to load activity logs",
    },
    access: {
        id: "access", endpoint: "/admin/api/logs/access", filterParam: "result", queryKey: "result", tokenParam: "token_id",
        loadingText: "Loading access logs...", emptyText: "No access logs found.", failedText: "Failed to load access logs",
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
            var reason = apiError(res, s.failedText);
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
        s.heads = {};
        s.nexts = {};
        dashLogCursors(s, res.data);
        s.rows = uiLogMerge([], res.data.logs || []);
        s.rowsFor = key;
        s.total = res.data.total || 0;
        s.hasMore = (res.data.logs || []).length >= LOG_PAGE_SIZE;
        s.pending = [];
        dashLogDraw(s, false);
    });
}

// An update that failed keeps the rows, says so under the list, and tells once
// per run of failures with the old wording
function dashLogRefreshFailed(s, reason) {
    if (!s.refreshFailed) showToast(s.failedText, "error");
    s.refreshFailed = reason || s.failedText;
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
            s.moreFailed = apiError(res, s.failedText);
            dashLogDraw(s, false);
            return;
        }
        dashLogCursors(s, res.data);
        var logs = res.data.logs || [];
        s.rows = uiLogMerge(s.rows, logs);
        s.hasMore = logs.length >= LOG_PAGE_SIZE;
        dashLogDraw(s, false);
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
            if (!res.ok || !res.data) return { failed: apiError(res, s.failedText) };
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
            '<div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + uiUserText(log.actor) + "</span></div>" +
            '<div class="ui-td" role="cell">' + dashTag(log.action, "") + "</div>" +
            '<div class="ui-td" role="cell"><span class="ui-unit">Entity Type </span><span class="ui-muted">' + esc(log.entityType || "-") + "</span></div>" +
            '<div class="ui-td" role="cell"><span class="ui-unit">Entity Name </span>' + (log.entityName ? uiUserText(log.entityName) : '<span class="ui-muted">-</span>') + "</div>" +
            '<div class="ui-td ui-td-full ui-td-wrap" role="cell">' + dashLogDetails(log) + "</div>" +
            '<div class="ui-td" role="cell"><span class="ui-unit">IP Address </span>' + uiCopyable(log.ipAddress) + "</div></div>";
    }
    return '<div class="ui-tr" role="row" data-key="' + esc(log.id) + '">' +
        '<div class="ui-td" role="cell">' + uiTime(log.createdAt) + "</div>" +
        '<div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + uiUserText(log.tokenName) + "</span></div>" +
        '<div class="ui-td" role="cell"><span class="ui-unit">Area (Zone) </span><span class="ui-tag ui-tag-line"><bdi dir="auto">' + esc(log.area) + "</bdi></span></div>" +
        '<div class="ui-td" role="cell"><span class="ui-unit">Level </span><span class="ui-mono">' + esc(log.level) + "</span></div>" +
        '<div class="ui-td" role="cell">' + (log.result ? dashTag("Allowed", "ok") : dashTag("Denied", "fail")) + "</div>" +
        '<div class="ui-td" role="cell"><span class="ui-unit">IP Address </span>' + uiCopyable(log.ipAddress) + "</div></div>";
}

// What an activity changed, in words; every value escaped
function dashLogDetails(log) {
    var det = log.details;
    if (!det) return '<span class="ui-muted">-</span>';
    var act = log.action;
    var grantsText = function (grants) {
        if (!grants || !grants.length) return "None";
        return grants.map(function (g) { return esc(g.area) + ": " + esc(String(g.level || "").replace(/,/g, "+")); }).join(", ");
    };
    var updated = function (items) {
        if (!items.length) return '<span class="ui-muted">No modifications</span>';
        return "<b>Updated:</b> " + items.join(", ");
    };
    try {
        if (act === "token_create") {
            return "<b>Grants:</b> " + grantsText(det.grants) + "<br><b>Expires:</b> " + (det.expiresAt ? esc(formatTime(det.expiresAt, { seconds: false })) : "Never");
        }
        if (act === "token_patch" || act === "zone_patch" || act === "api_key_patch") {
            var u = det.updates || {};
            var items = [];
            if (u.name !== undefined && u.name !== null) items.push('Name: "' + esc(u.name) + '"');
            if (u.description !== undefined && u.description !== null) items.push('Description: "' + esc(u.description) + '"');
            if (u.isActive !== undefined && u.isActive !== null) items.push("Status: " + (u.isActive ? "Enabled" : "Disabled"));
            if (u.grants !== undefined && u.grants !== null) items.push("Grants: " + grantsText(u.grants));
            if (u.expiresAt !== undefined && u.expiresAt !== null) items.push("Expires: " + (u.expiresAt ? esc(formatTime(u.expiresAt, { seconds: false })) : "Never"));
            if (u.scopes !== undefined && u.scopes !== null) items.push("Scopes: " + esc([].concat(u.scopes).join(", ")));
            return updated(items);
        }
        if (act === "zone_create") return det.description ? "<b>Description:</b> " + esc(det.description) : '<span class="ui-muted">-</span>';
        if (act === "api_key_create") return det.scopes ? "<b>Scopes:</b> " + esc(det.scopes) : '<span class="ui-muted">-</span>';
    } catch (e) { /* shown as data below */ }
    return '<code class="ui-mono">' + esc(JSON.stringify(det)) + "</code>";
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
    pillEl.textContent = s.capped ? "More new entries than can be merged. Show newest"
        : n === 1 ? "1 new entry" : uiNum(n) + " new entries";
    var moreHtml = "";
    if (s.moreFailed) {
        moreHtml = '<span role="alert">Could not load more entries: ' + esc(s.moreFailed) + '</span> <button type="button" class="ui-btn-link" data-log-more>Try again</button>';
    } else if (s.rows && s.rows.length && s.hasMore) {
        moreHtml = s.loading ? '<span class="ui-spin" aria-hidden="true"></span><span role="status">Loading more...</span>'
            : '<button type="button" class="ui-btn ui-btn-sm" data-log-more data-inventory="' + s.id + '.loadmore">Load more</button>';
    } else if (s.rows && s.rows.length > LOG_PAGE_SIZE) {
        moreHtml = '<span role="status">All ' + uiNum(s.rows.length) + " entries shown</span>";
    }
    // A failed update keeps the rows and Load more, and says so above them (R7)
    if (s.refreshFailed && s.rows) {
        moreHtml = '<span role="status">' + esc(s.failedText) + ": " + esc(s.refreshFailed) +
            '. The list shows what was loaded before.</span> <button type="button" class="ui-btn-link" data-log-retry-new>Try again</button>' +
            (moreHtml ? " " + moreHtml : "");
    }
    if (moreEl.innerHTML !== moreHtml) moreEl.innerHTML = moreHtml;
    moreEl.classList.toggle("hidden", !moreHtml);
    var filtered = s.applied.search || s.applied.filter;
    if (s.rows) countEl.textContent = uiCountLabel(s.total, "entry", "entries") + (filtered ? " match the filters" : "");
    if (quiet && uiAreaHeld(rowsEl)) return;
    var html;
    if (s.rows === null) html = s.failed ? dashFailed(s.failedText, s.failed, "data-log-retry") : dashLoading(s.loadingText);
    else if (!s.rows.length) html = dashEmpty(s.emptyText, filtered ? "Change the filters or reset them." : "");
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
    uiPicker(sel, { label: s.id === "activity" ? "Action" : "Result" });
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
        else if (event.target.closest("[data-log-retry-new]")) dashLogRefresh(s);
    });
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
        var t = (dashTokenStore.rows || []).filter(function (r) { return r.id === id; })[0];
        chip.innerHTML = '<span class="ui-picker-name">Token</span><span>' + (t ? uiUserText(t.name) : "Deleted token") + "</span>" +
            '<span aria-hidden="true">&times;</span>';
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
            dashLimit.failed = apiError(res, "The server did not answer.");
        }
        dashLimitDraw();
    });
}

function dashLimitHtml() {
    var d = dashLimit.data;
    var stale = dashLimit.failed ? "Could not refresh these counts: " + esc(dashLimit.failed) + " They are checked again every 30 seconds." : "";
    if (!d) return dashLimitAlert(" is-fail", "Could not load the rate limit status", [stale]);
    if (stale) stale += " The counts below are from " + uiTime(new Date(dashLimit.at).toISOString()) + ".";
    var seconds = d.windowMs / 1000;
    var rule = "Limit: " + esc(uiCountLabel(d.limit, "request", "requests")) + " per " +
        esc(seconds === 60 ? "minute" : uiCountLabel(seconds, "second", "seconds")) + " from one address, or from one /64 network for IPv6.";
    var since = "Counted by this server process since " + uiTime(d.since) + "; a restart resets the count.";
    var lines = stale ? [stale] : [];
    if (!d.rejectedTotal) {
        return dashLimitAlert(stale ? " is-fail" : "", "No requests were rejected by the rate limit", lines.concat([rule, since]));
    }
    var tops = (d.topAddresses || []).map(function (a) {
        return uiUserText(a.address) + " (" + esc(uiNum(a.rejected)) + ")";
    }).join(", ");
    lines.push(
        "They were answered with <code>429 Too many requests</code> and a <code>Retry-After</code> header " +
            "and are not in the list below. Last one " + uiTime(d.lastRejectedAt) + ".",
        (d.untrackedRejected ? "Most rejected among the first " + esc(uiNum(d.maxTrackedAddresses)) + " addresses: " : "Most rejected: ") + tops +
            (d.untrackedRejected ? "; " + esc(uiCountLabel(d.untrackedRejected, "more rejection", "more rejections")) + " from other addresses, so this ranking is partial." : "."),
        rule,
        since
    );
    var title = uiCountLabel(d.rejectedTotal, "request was", "requests were") + " rejected by the rate limit";
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

var USAGE_RANGES = { "24h": "the last 24 hours", "7d": "the last 7 days", "30d": "the last 30 days" };
var USAGE_STATUS = {
    active: { label: "Active", tone: "ok" },
    disabled: { label: "Disabled", tone: "" },
    expired: { label: "Expired", tone: "warn" },
    deleted: { label: "Deleted", tone: "" },
};
var DASH_USE_COLS = [
    { key: "name", label: "Token", kind: "text" },
    { key: "calls", label: "Calls", kind: "number" },
    { key: "denied", label: "Denied", kind: "number" },
    { key: "ipCount", label: "Addresses", kind: "number" },
    { key: "lastCallAt", label: "Last call", kind: "date" },
];
var USAGE_NOTE = "Counts come from the access log. Requests rejected by the rate limit and requests with an invalid level are not logged, " +
    "so they are not counted. Grants checks (<code>/validate/grants</code>) are counted apart from calls.";

// data: the last answer; shown: the answer the page now draws (a refresh skipped while the
// user works in the page leaves the older one on screen, and the chart reads that one)
var dashUse = { range: "7d", token: "", data: null, dataFor: "", notFound: false, failed: "", seq: 0,
    sort: { key: "", dir: "" }, sel: -1, drawn: "", shown: null, bound: false };
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
            dashUse.notFound = false;
            dashUse.failed = "";
        } else if (res.status === 404 && dashUse.token) {
            dashUse.data = null;
            dashUse.dataFor = key;
            dashUse.notFound = true;
            dashUse.failed = "";
        } else {
            dashUse.failed = apiError(res, "The server did not answer.");
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
    if (p > 0 && p < 1) return "under 1%";
    return Math.round(p) + "%";
}

function dashUseBucketName(d, b) {
    return d.range === "24h" ? formatTime(b.start, { seconds: false }) : formatDay(b.start);
}

// One bucket in words: the chart's readout, each bar's name and the table behind the chart
function dashUseBucketWords(d, b) {
    if (b.coverage === "none") return "No data: access logs older than " + uiCountLabel(d.retentionDays, "day", "days") + " are deleted";
    var text = uiCountLabel(b.allowed + b.denied, "call", "calls") + ", " + uiNum(b.allowed) + " allowed, " + uiNum(b.denied) + " denied";
    if (b.coverage === "partial") text += " (counted from " + formatTime(d.dataFrom, { seconds: false }) + "; older logs are deleted)";
    return text;
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
        return "<span" + minor + ' style="grid-column:' + (i + 1) + '">' + esc(hourly ? formatClock(b.start) : formatShortDay(b.start)) + "</span>";
    }).join("");
    var rows = series.map(function (b) {
        return "<tr><th scope=\"row\">" + esc(dashUseBucketName(d, b)) + "</th><td>" +
            (b.coverage === "none" ? "No data" : esc(uiNum(b.allowed))) + "</td><td>" + (b.coverage === "none" ? "No data" : esc(uiNum(b.denied))) + "</td></tr>";
    }).join("");
    return '<div class="ui-chart">' +
        '<p class="ui-chart-read" id="usage-read" aria-live="polite"></p>' +
        '<div class="ui-chart-plot">' +
        '<div class="ui-chart-y" aria-hidden="true"><span style="top:0%">' + esc(uiNum(top)) + '</span><span style="top:50%">' + esc(uiNum(top / 2)) +
        '</span><span style="top:100%">0</span></div>' +
        '<div class="ui-chart-bars" role="group" aria-label="' + (hourly ? "Calls per hour" : "Calls per day") + '. Arrow keys move between bars." style="--ui-chart-n:' + n + '">' + cols + "</div>" +
        '<div class="ui-chart-x" aria-hidden="true" style="--ui-chart-n:' + n + '">' + ticks + "</div></div>" +
        '<p class="ui-chart-key"><span><i class="is-base"></i>Allowed</span><span><i class="is-fail"></i>Denied</span>' +
        (gaps ? '<span><i class="is-none"></i>Logs deleted</span>' : "") + "</p>" +
        '<table class="ui-sr"><caption>' + (hourly ? "Calls per hour" : "Calls per day") + "</caption><thead><tr><th scope=\"col\">" +
        (hourly ? "Hour" : "Day") + '</th><th scope="col">Allowed</th><th scope="col">Denied</th></tr></thead><tbody>' + rows + "</tbody></table></div>";
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
    if (d.partial) {
        out += '<div class="ui-banner ui-banner-warn" role="status"><div><b>Part of this period is no longer in the log</b>Access logs are kept for ' +
            esc(uiCountLabel(d.retentionDays, "day", "days")) + ", so these figures start at " + uiTime(d.dataFrom, formatTime(d.dataFrom, { seconds: false })) + ".</div></div>";
    }
    if (d.areasIncomplete) {
        out += '<div class="ui-banner ui-banner-warn" role="status"><div><b>Area figures are incomplete for this period</b>' +
            "The calls name more different zones than the page can count, so zone figures are not shown. Call counts are complete.</div></div>";
    }
    if (dashUse.failed) {
        out += '<div class="ui-banner ui-banner-fail" role="alert"><div><b>Could not refresh these figures</b>' + esc(dashUse.failed) +
            " They are tried again every minute.</div></div>";
    }
    return out;
}

function dashUseChartPanel(d) {
    var t = d.totals;
    return '<div class="ui-panel"><div class="ui-panel-head">' + (d.range === "24h" ? "Calls per hour" : "Calls per day") +
        ' <span class="ui-count">' + esc(uiCountLabel(t.calls, "call", "calls")) + "</span></div>" + dashUseChartHtml(d) + "</div>";
}

function dashUseStatusTag(status) {
    if (status === "unknown") return "";
    var st = USAGE_STATUS[status] || USAGE_STATUS.active;
    return dashTag(st.label, st.tone);
}

function dashUseAreaTags(list) {
    if (list === null) return '<span class="ui-muted">-</span>';
    if (!list.length) return '<span class="ui-muted">No calls</span>';
    return '<div class="ui-tags">' + list.map(function (a) {
        return '<span class="ui-tag ui-tag-line">' + uiUserText(a.area + (a.truncated ? "…" : "")) + " " + esc(uiNum(a.calls)) + "</span>";
    }).join("") + "</div>";
}

function dashUseSortValue(t, key) {
    if (key === "name") return String(t.name || "");
    if (key === "lastCallAt") return uiParseInstant(t.lastCallAt);
    return t[key];
}

function dashUseOverviewHtml(d) {
    var t = d.totals;
    var html = dashUseNotices(d) + '<div class="ui-kpis">' +
        dashUseKpi("Calls", esc(uiNum(t.calls)), "in " + esc(USAGE_RANGES[d.range])) +
        dashUseKpi("Denied", esc(uiNum(t.denied)), esc(dashUsePct(t.denied, t.calls)) + " of calls") +
        dashUseKpi("Tokens with calls", esc(uiNum(t.tokensUsed)) + ' <span class="ui-muted">of ' + esc(uiNum(t.tokensTotal)) + "</span>", "tokens that called in this period") +
        dashUseKpi("Unknown token", esc(uiNum(t.unknownCalls)), "calls with a token the service does not know") +
        "</div>" + dashUseChartPanel(d);
    // Busiest first; the header buttons and the Sort order re-sort from here
    var base = d.tokens.slice().sort(function (a, b) {
        return b.calls - a.calls || b.grantsChecks - a.grantsChecks || String(a.name).localeCompare(String(b.name), undefined, { sensitivity: "base" });
    });
    var rows = uiSortRows(base, dashUse.sort, dashUseSortValue);
    var head = DASH_USE_COLS.slice(0, 3).map(function (c) { return dashSortHead(dashUse.sort, c); }).join("") +
        '<div class="ui-td" role="columnheader">Top zones</div>' +
        DASH_USE_COLS.slice(3).map(function (c) { return dashSortHead(dashUse.sort, c); }).join("");
    var body = rows.length ? rows.map(function (r) {
        var key = r.tokenId || "unknown";
        return '<div class="ui-tr' + (r.calls ? "" : " is-off") + '" role="row" tabindex="0" data-key="' + esc(key) + '" aria-label="' + esc(r.name) + '">' +
            '<div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + uiUserText(r.name) + "</span>" +
            (r.status !== "active" ? " " + dashUseStatusTag(r.status) : "") +
            (r.grantsChecks ? '<span class="ui-td-sub">' + esc(uiCountLabel(r.grantsChecks, "grants check", "grants checks")) + "</span>" : "") + "</div>" +
            '<div class="ui-td ui-num" role="cell"><span class="ui-unit">Calls </span>' + esc(uiNum(r.calls)) + "</div>" +
            '<div class="ui-td ui-num" role="cell"><span class="ui-unit">Denied </span><span' + (r.denied ? ' class="ui-text-fail"' : "") + ">" + esc(uiNum(r.denied)) + "</span>" +
            (r.calls ? '<span class="ui-td-sub">' + esc(dashUsePct(r.denied, r.calls)) + "</span>" : "") + "</div>" +
            '<div class="ui-td ui-td-full ui-td-wrap" role="cell">' + (r.calls ? dashUseAreaTags(r.topAreas) : '<span class="ui-muted">No calls in this period</span>') + "</div>" +
            '<div class="ui-td ui-num" role="cell"><span class="ui-unit">Addresses </span>' + esc(uiNum(r.ipCount)) + "</div>" +
            '<div class="ui-td" role="cell"><span class="ui-unit">Last call </span>' + (r.lastCallAt ? uiTime(r.lastCallAt) : '<span class="ui-muted">-</span>') + "</div></div>";
    }).join("") : dashEmpty("No tokens yet.", "Create a token on the Tokens page; its calls show here.");
    html += '<div class="ui-panel"><div class="ui-panel-head">Tokens <span class="ui-count">' + esc(uiCountLabel(rows.length, "row", "rows")) + "</span></div>" +
        '<div class="ui-table ui-stack" id="usage-table" role="table" aria-label="Calls per token">' +
        '<div class="ui-tr ui-tr-head" role="row" id="usage-head">' + head + "</div>" +
        '<div class="ui-table-body" role="rowgroup">' + body + "</div></div></div>" +
        '<p class="ui-usage-note">' + USAGE_NOTE + (t.grantsChecks ? " This period has " + esc(uiCountLabel(t.grantsChecks, "grants check", "grants checks")) + "." : "") + "</p>";
    return html;
}

function dashUseBackLink() {
    var q = dashUseQuery("", dashUse.range);
    return '<a data-go="usage" data-query="' + esc(q) + '" href="' + esc(basePath() + PAGES.usage.path + (q ? "?" + q : "")) + '">&larr; All tokens</a>';
}

function dashUseLevels(levels) {
    return '<div class="ui-tags">' + Object.keys(levels).map(function (lv) {
        var v = levels[lv];
        return (v.allowed ? '<span class="ui-tag ui-tag-line">' + esc(lv + " " + uiNum(v.allowed)) + "</span>" : "") +
            (v.denied ? '<span class="ui-tag ui-tag-fail">' + esc(lv + " " + uiNum(v.denied) + " denied") + "</span>" : "");
    }).join("") + "</div>";
}

function dashUseTokenHtml(d) {
    var t = d.totals;
    var tok = d.token;
    var logs = tok.id ? '<a class="ui-btn ui-btn-sm" data-go="access" data-query="token=' + esc(encodeURIComponent(tok.id)) + '" href="' +
        esc(basePath() + PAGES.access.path + "?token=" + encodeURIComponent(tok.id)) + '">Open in Access Logs</a>' : "";
    var html = '<div class="ui-usage-title">' + dashUseBackLink() + "<h2>" + uiUserText(tok.name) + " " + dashUseStatusTag(tok.status) + "</h2>" + logs + "</div>" +
        dashUseNotices(d) + '<div class="ui-kpis">' +
        dashUseKpi("Calls", esc(uiNum(t.calls)), "in " + esc(USAGE_RANGES[d.range])) +
        dashUseKpi("Denied", esc(uiNum(t.denied)), esc(dashUsePct(t.denied, t.calls)) + " of calls") +
        dashUseKpi("Zones", d.areasCount === null ? "-" : esc(uiNum(d.areasCount)), "called at least once") +
        dashUseKpi("Addresses", esc(uiNum(t.ipCount)), "the calls came from") +
        dashUseKpi("Last call", t.lastCallAt ? uiTime(t.lastCallAt) : '<span class="ui-muted">-</span>', t.grantsChecks ? esc(uiCountLabel(t.grantsChecks, "grants check", "grants checks")) : "") +
        "</div>" + dashUseChartPanel(d);

    var areas = d.areas;
    var areasBody;
    if (areas === null) areasBody = dashEmpty("Area figures are incomplete for this period.", "Call counts above are complete.");
    else if (!areas.length) areasBody = dashEmpty("No calls in this period.", "");
    else {
        var most = areas[0].calls;
        areasBody = '<div class="ui-table ui-stack" id="usage-areas" role="table" aria-label="Calls per zone">' +
            '<div class="ui-tr ui-tr-head" role="row"><div class="ui-td" role="columnheader">Zone</div><div class="ui-td" role="columnheader">Calls</div>' +
            '<div class="ui-td" role="columnheader">Allowed and denied</div><div class="ui-td" role="columnheader">Levels</div></div>' +
            '<div class="ui-table-body" role="rowgroup">' + areas.map(function (a) {
                var width = Math.max(2, (a.calls / most) * 100).toFixed(1);
                return '<div class="ui-tr" role="row"><div class="ui-td ui-td-main" role="cell"><span class="ui-td-name">' + uiUserText(a.area + (a.truncated ? "…" : "")) + "</span></div>" +
                    '<div class="ui-td ui-num" role="cell"><span class="ui-unit">Calls </span>' + esc(uiNum(a.calls)) +
                    (a.denied ? '<span class="ui-td-sub"><span class="ui-text-fail">' + esc(uiNum(a.denied) + " denied") + "</span></span>" : "") + "</div>" +
                    '<div class="ui-td ui-td-full" role="cell"><div class="ui-bar" style="width:' + width + '%" aria-hidden="true">' +
                    (a.allowed ? '<i class="is-base" style="flex:' + a.allowed + '"></i>' : "") + (a.denied ? '<i class="is-fail" style="flex:' + a.denied + '"></i>' : "") +
                    '</div><span class="ui-sr">' + esc(uiNum(a.allowed) + " allowed, " + uiNum(a.denied) + " denied") + "</span></div>" +
                    '<div class="ui-td ui-td-full ui-td-wrap" role="cell">' + dashUseLevels(a.levels) + "</div></div>";
            }).join("") + "</div></div>";
    }
    var shownAreas = areas ? areas.length : 0;
    html += '<div class="ui-panel"><div class="ui-panel-head">Zones' +
        (d.areasCount !== null ? ' <span class="ui-count">' + esc(d.areasCount > shownAreas ? "the " + uiNum(shownAreas) + " busiest of " + uiNum(d.areasCount) : uiCountLabel(d.areasCount, "zone", "zones")) + "</span>" : "") +
        "</div>" + areasBody + "</div>";

    var addrs = d.addresses;
    var addrBody = !addrs.length ? dashEmpty("No calls in this period.", "") :
        '<div class="ui-table ui-stack" id="usage-addrs" role="table" aria-label="Calls per address">' +
        '<div class="ui-tr ui-tr-head" role="row"><div class="ui-td" role="columnheader">Address</div><div class="ui-td" role="columnheader">Calls</div>' +
        '<div class="ui-td" role="columnheader">Denied</div><div class="ui-td" role="columnheader">Last call</div></div>' +
        '<div class="ui-table-body" role="rowgroup">' + addrs.map(function (a) {
            return '<div class="ui-tr" role="row"><div class="ui-td ui-td-main" role="cell">' + uiCopyable(a.address + (a.truncated ? "…" : "")) + "</div>" +
                '<div class="ui-td ui-num" role="cell"><span class="ui-unit">Calls </span>' + esc(uiNum(a.calls)) + "</div>" +
                '<div class="ui-td ui-num" role="cell"><span class="ui-unit">Denied </span><span' + (a.denied ? ' class="ui-text-fail"' : "") + ">" + esc(uiNum(a.denied)) + "</span></div>" +
                '<div class="ui-td" role="cell"><span class="ui-unit">Last call </span>' + uiTime(a.lastCallAt) + "</div></div>";
        }).join("") + "</div></div>";
    html += '<div class="ui-panel"><div class="ui-panel-head">Addresses <span class="ui-count">' +
        esc(t.ipCount > addrs.length ? "the " + uiNum(addrs.length) + " busiest of " + uiNum(t.ipCount) : uiCountLabel(t.ipCount, "address", "addresses")) +
        "</span></div>" + addrBody + "</div>";

    html += '<p class="ui-usage-note">' + USAGE_NOTE + "</p>";
    return html;
}

function dashUseNotFoundHtml() {
    return '<div class="ui-usage-title">' + dashUseBackLink() + "</div>" +
        '<div class="ui-panel">' + dashEmpty("No usage to show for this token.", "It is not a token any more and made no calls in this period. Pick a longer period or go back to all tokens.") + "</div>";
}

function dashUseSyncRange() {
    document.querySelectorAll("#usage-range [data-range]").forEach(function (b) {
        b.setAttribute("aria-pressed", b.getAttribute("data-range") === dashUse.range ? "true" : "false");
    });
    // The Sort button orders the token list, which only the overview shows
    var sort = document.getElementById("usage-sort");
    if (sort) sort.classList.toggle("hidden", !!dashUse.token);
    dashSortButtonSync("usage-sort", dashUse.sort, "tokens", DASH_USE_COLS);
}

// A background refresh (quiet) leaves the page alone while the user works in it
function dashUseDraw(quiet) {
    var body = document.getElementById("usage-body");
    if (!body) return;
    dashUseSyncRange();
    var mine = dashUse.dataFor === dashUseKey();
    var html;
    if (mine && dashUse.notFound) html = dashUseNotFoundHtml();
    else if (mine && dashUse.data) html = dashUse.token ? dashUseTokenHtml(dashUse.data) : dashUseOverviewHtml(dashUse.data);
    else html = '<div class="ui-panel">' + (dashUse.failed ? dashFailed("Could not load the usage", dashUse.failed, "data-usage-retry") : dashLoading("Loading usage...")) + "</div>";
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
    var sortBtn = document.getElementById("usage-sort");
    sortBtn.addEventListener("click", function () {
        uiSortMenu(sortBtn, "tokens", DASH_USE_COLS, dashUse.sort, function () { dashUseDraw(false); });
    });
    var body = document.getElementById("usage-body");
    body.addEventListener("click", function (event) {
        var t = event.target;
        if (t.closest("[data-usage-retry]")) {
            dashUse.failed = "";
            dashUseDraw(false);
            dashUseLoad();
            return;
        }
        var sort = t.closest("#usage-head [data-sort]");
        if (sort) {
            var col = DASH_USE_COLS.filter(function (c) { return c.key === sort.getAttribute("data-sort"); })[0];
            uiSortToggle(dashUse.sort, col);
            dashUseDraw(false);
            var again = body.querySelector('#usage-head [data-sort="' + col.key + '"]');
            if (again) again.focus({ preventScroll: true });
            return;
        }
        var col2 = t.closest("[data-chart-i]");
        if (col2) {
            dashUseSelect(Number(col2.getAttribute("data-chart-i")), false);
            return;
        }
        var row = t.closest("#usage-table [data-key]");
        if (!row || t.closest("button, a, input, select, label") || uiRowHeld(event, row)) return;
        dashUseOpen(row.getAttribute("data-key"));
    });
    body.addEventListener("keydown", function (event) {
        var t = event.target;
        if (!(t instanceof Element)) return;
        var c = t.closest("[data-chart-i]");
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
        if (t.matches("#usage-table [data-key]") && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            dashUseOpen(t.getAttribute("data-key"));
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
// Settings: the rate limit and the addresses exempt from it. The form edits a
// draft based on one saved version (base); a refresh never touches a changed
// draft, and the base only moves on a save or an explicit choice after a conflict.
// ===========================================================================

var DASH_LOCK_ICON = '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z"/></svg>';

var dashSet = {
    snap: null, baseSnap: null, draft: null, dirty: false, saving: false, failed: "", refreshFailed: "",
    conflict: null, error: "", errorField: "", askOwn: false, seq: 0, bound: false,
};

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
    var seq = ++dashSet.seq;
    return api("GET", "/admin/api/settings/rate-limit").then(function (res) {
        // A newer load, a save in flight, or a save that finished after this load began owns the page
        if (seq !== dashSet.seq || dashSet.saving || (isCurrent && !isCurrent())) return;
        var d = res.data;
        if (!res.ok || !d || !d.fields) {
            var why = apiError(res, "The server did not answer.");
            if (dashSet.snap) dashSet.refreshFailed = why;
            else dashSet.failed = why;
            dashSettingsDraw();
            return;
        }
        dashSet.failed = "";
        dashSet.refreshFailed = "";
        dashSet.snap = d;
        if (!dashSet.dirty && !dashSet.saving && !dashSet.conflict) {
            dashSet.baseSnap = d;
            dashSet.draft = dashSetDraftFrom(d);
            dashSet.error = "";
            dashSet.errorField = "";
            dashSet.askOwn = false;
            dashSettingsDrawForm();
        }
        dashSettingsDraw();
    });
}

function dashSetSourceHint(field) {
    if (field.source === "stored") return "Saved here.";
    if (field.source === "default") return "Default value.";
    return "";
}

function dashSetLocked(field, shown) {
    return '<p class="ui-locked-note">' + DASH_LOCK_ICON + "<span>Set by the <code>" + esc(field.envName) + "</code> environment variable (" +
        esc(shown) + "). Remove it there to change the value here.</span></p>";
}

function dashSettingsDrawForm() {
    var body = document.getElementById("settings-body");
    if (!body || !dashSet.baseSnap) return;
    var f = dashSet.baseSnap.fields;
    var d = dashSet.draft;
    var own = dashSet.snap.yourAddress;
    var proxy = dashSet.snap.proxyNetwork || { envName: "FORWARDED_ALLOW_IPS", value: null };
    var proxySet = proxy.value !== null && proxy.value !== undefined;
    var proxyField = !proxySet
        ? '<p class="ui-hint"><code>' + esc(proxy.envName) + "</code> is not set, so uvicorn trusts <code>X-Forwarded-For</code> only from " +
          "<code>127.0.0.1</code> and <code>::1</code>. Behind a reverse proxy at another address, every caller then has the proxy's address. " +
          "Set it to the proxy's address or network.</p>"
        : proxy.value === ""
        ? '<p class="ui-locked-note">' + DASH_LOCK_ICON + "<span>Set by the <code>" + esc(proxy.envName) + "</code> environment variable to an empty value, " +
          "so uvicorn trusts <code>X-Forwarded-For</code> from no address: every caller has the address of the connection it arrives on.</span></p>"
        : proxy.value
        ? '<p class="ui-locked-note">' + DASH_LOCK_ICON + "<span>Set by the <code>" + esc(proxy.envName) + "</code> environment variable: <code>" +
          esc(proxy.value) + "</code>. uvicorn takes the caller's address from <code>X-Forwarded-For</code> on connections from these addresses, " +
          "unless its start command sets another value.</span></p>"
        : "";
    var maxField = f.maxReq.locked
        ? dashSetLocked(f.maxReq, uiCountLabel(f.maxReq.value, "request", "requests"))
        : '<input class="ui-input" id="set-max" type="number" inputmode="numeric" min="' + esc(f.maxReq.min) + '" max="' + esc(f.maxReq.max) +
          '" step="1" required value="' + esc(d.maxReq) + '" aria-describedby="set-max-hint" />' +
          '<span class="ui-hint" id="set-max-hint">' + esc(dashSetSourceHint(f.maxReq)) + " From 1 to " + esc(uiNum(f.maxReq.max)) + ". An IPv6 /64 network counts as one address.</span>";
    var winField = f.windowMs.locked
        ? dashSetLocked(f.windowMs, uiNum(f.windowMs.value) + " ms")
        : '<input class="ui-input" id="set-window" type="number" inputmode="numeric" min="' + esc(f.windowMs.minSeconds) + '" max="' +
          esc(f.windowMs.maxSeconds) + '" step="1" required value="' + esc(d.windowSeconds) + '" aria-describedby="set-window-hint" />' +
          '<span class="ui-hint" id="set-window-hint">' + esc(dashSetSourceHint(f.windowMs)) + " Whole seconds, from 1 to " + esc(uiNum(f.windowMs.maxSeconds)) + ".</span>";
    body.innerHTML =
        '<form class="ui-form" id="settings-form" novalidate><fieldset class="ui-settings-fields" id="settings-fields">' +
        '<div class="ui-form-row">' +
        '<div class="ui-field"><label class="ui-label" for="set-max">Requests per window, from one address</label>' + maxField + "</div>" +
        '<div class="ui-field"><label class="ui-label" for="set-window">Window, in seconds</label>' + winField + "</div>" +
        "</div>" +
        '<div class="ui-field"><span class="ui-label">Reverse proxy addresses</span>' + proxyField + "</div>" +
        '<div class="ui-field"><label class="ui-label" for="set-exempt">Exempt addresses and networks <small>(one per line, up to ' + esc(uiNum(f.exempt.max)) + ")</small></label>" +
        '<textarea class="ui-textarea" id="set-exempt" spellcheck="false" autocapitalize="off" autocomplete="off" dir="ltr" aria-describedby="set-exempt-hint" placeholder="203.0.113.10&#10;172.18.0.0/16">' +
        esc(d.exempt) + "</textarea>" +
        '<span class="ui-hint" id="set-exempt-hint">IPv4 or IPv6 addresses, or networks such as <code>172.18.0.0/16</code>. Requests from them are never limited or counted.' +
        (own ? " Your request to this page came from <code>" + esc(own) + "</code>." : "") + "</span>" +
        (proxySet && proxy.value ? "" :
            '<div class="ui-alert is-warn" id="set-exempt-warn"><span class="ui-alert-bar" aria-hidden="true"></span><div><p>An exempt address is not limited at all</p>' +
            "<small>If a reverse proxy sits in front of this service and <code>FORWARDED_ALLOW_IPS</code> is not set, every caller arrives from the proxy's address, " +
            "and exempting it removes the limit for everyone.</small></div><span></span></div>") +
        '<label class="ui-check-label hidden" id="set-own-wrap"><input type="checkbox" class="ui-check" id="set-own" /> <span>Exempt <code>' + esc(own || "") +
        "</code>, the address my own request came from, anyway</span></label>" +
        "</div>" +
        '<p class="ui-error hidden" id="set-err" role="alert"></p>' +
        '<div class="ui-settings-actions"><button type="submit" class="ui-btn ui-btn-primary" id="set-save">Save</button>' +
        '<button type="button" class="ui-btn hidden" id="set-discard">Discard changes</button></div>' +
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
    var err = document.getElementById("set-err");
    err.textContent = dashSet.error;
    err.classList.toggle("hidden", !dashSet.error);
    [["set-max", "maxReq"], ["set-window", "windowMs"], ["set-exempt", "exempt"]].forEach(function (pair) {
        var el = document.getElementById(pair[0]);
        if (!el) return;
        if (dashSet.errorField === pair[1]) el.setAttribute("aria-invalid", "true");
        else el.removeAttribute("aria-invalid");
    });
    document.getElementById("settings-fields").disabled = dashSet.saving;
    document.getElementById("set-save").textContent = dashSet.saving ? "Saving..." : "Save";
    document.getElementById("set-discard").classList.toggle("hidden", !dashSet.dirty);
}

function dashSettingsDraw() {
    var info = document.getElementById("settings-info");
    var body = document.getElementById("settings-body");
    if (!info || !body) return;
    if (!dashSet.snap) {
        info.innerHTML = "";
        body.innerHTML = dashSet.failed ? dashFailed("Could not load the settings", dashSet.failed, "data-settings-retry") : dashLoading("Loading settings...");
        return;
    }
    var s = dashSet.snap;
    var alerts = [];
    var addAlert = function (tone, title, sub, actions) {
        alerts.push('<div class="ui-alert' + tone + '"><span class="ui-alert-bar" aria-hidden="true"></span><div><p>' + esc(title) + "</p><small>" + sub +
            "</small></div>" + (actions || "<span></span>") + "</div>");
    };
    if (dashSet.conflict) {
        var c = dashSet.conflict;
        var f = c.fields;
        addAlert(" is-warn", "Someone else saved these settings after you opened the page",
            "Saved now: " + esc(uiCountLabel(f.maxReq.value, "request", "requests")) + " per " + esc(uiNum(f.windowMs.value / 1000)) + " s; exempt: " +
            (f.exempt.value.length ? f.exempt.value.map(function (a) { return uiUserText(a); }).join(", ") : "none") +
            ". Your changes are still in the form.",
            '<div class="ui-settings-actions"><button type="button" class="ui-btn ui-btn-sm" data-set-discard>Discard my changes</button>' +
            '<button type="button" class="ui-btn ui-btn-sm" data-set-keep>Keep my changes</button></div>');
    }
    if (dashSet.refreshFailed) {
        addAlert(" is-fail", "Could not refresh the settings", esc(dashSet.refreshFailed) + " They are checked again every 30 seconds.");
    }
    var a = s.applied || {};
    if (a.lastReloadError) {
        addAlert(" is-fail", "This server process could not reload the settings",
            esc(a.lastReloadError) + " It keeps using version " + esc(uiNum(a.version)) + ".");
    } else if (a.version < s.version) {
        addAlert(" is-warn", "This server process still uses version " + uiNum(a.version), "It reloads the settings every 15 seconds.");
    }
    var applied = "This server process enforces version " + esc(uiNum(a.version || 0)) +
        (a.lastReload ? ", checked " + uiTime(a.lastReload) : "") + ".";
    var saved = s.version
        ? "Version " + esc(uiNum(s.version)) + ", saved by " + uiUserText(s.savedBy || "?") + (s.savedFrom ? " from <code>" + esc(s.savedFrom) + "</code>" : "") +
          " " + uiTime(s.savedAt) + "."
        : "Nothing saved here yet. The values come from the defaults and the environment.";
    addAlert("", "Saved settings", saved + " " + applied + " Changes apply at once in this server process and within about 15 seconds in any other.");
    var html = alerts.join("");
    if (!uiTextHeld(info)) uiKeepFocus(info, function () { info.innerHTML = html; });
    if (!document.getElementById("settings-form")) dashSettingsDrawForm();
    dashSettingsSyncForm();
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
        if (!whole.test(changes.maxReq)) return dashSettingsFail("Requests per window must be a whole number.", "maxReq");
        body.maxReq = Number(changes.maxReq);
    }
    if ("windowSeconds" in changes) {
        if (!whole.test(changes.windowSeconds)) return dashSettingsFail("The window must be a whole number of seconds.", "windowMs");
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
            dashSet.snap = d;
            dashSet.baseSnap = d;
            dashSet.draft = dashSetDraftFrom(d);
            dashSet.dirty = false;
            dashSet.conflict = null;
            dashSet.askOwn = false;
            dashSettingsDrawForm();
            dashSettingsDraw();
            showToast(d.warning || "Settings saved", d.warning ? "error" : "success");
            return;
        }
        if (res.status === 409 && d.code === "conflict" && d.current) {
            dashSet.conflict = d.current;
            dashSet.snap = d.current;
            dashSettingsDraw();
            return;
        }
        if (res.status === 409 && d.code === "own_address") dashSet.askOwn = true;
        dashSettingsFail(apiError(res, "Could not save the settings."), d.field || "");
        if (d.code === "locked") dashSettingsLoad();
    });
}

function dashSettingsFail(message, field) {
    dashSet.error = message;
    dashSet.errorField = field;
    dashSettingsSyncForm();
}

function dashSettingsBind() {
    if (dashSet.bound) return;
    dashSet.bound = true;
    var page = document.querySelector('[data-page="settings"]');
    page.addEventListener("input", function (event) {
        if (event.target.closest("#settings-form") && event.target.id !== "set-own") dashSettingsInput();
    });
    page.addEventListener("submit", function (event) {
        if (event.target.id !== "settings-form") return;
        event.preventDefault();
        dashSettingsSave();
    });
    page.addEventListener("click", function (event) {
        var t = event.target;
        if (t.closest("[data-settings-retry]")) {
            dashSet.failed = "";
            dashSettingsDraw();
            dashSettingsLoad();
        } else if (t.closest("#set-discard") || t.closest("[data-set-discard]")) {
            var to = dashSet.conflict || dashSet.snap;
            dashSet.baseSnap = to;
            dashSet.snap = to;
            dashSet.draft = dashSetDraftFrom(to);
            dashSet.dirty = false;
            dashSet.conflict = null;
            dashSet.askOwn = false;
            dashSet.error = "";
            dashSet.errorField = "";
            dashSettingsDrawForm();
            dashSettingsDraw();
        } else if (t.closest("[data-set-keep]")) {
            // An explicit choice: the draft now changes the version saved by the other admin
            dashSet.baseSnap = dashSet.conflict;
            dashSet.conflict = null;
            dashSettingsInput();
            dashSettingsDraw();
        }
    });
    // A changed draft is an unsaved change
    uiUnsavedRegister(function () {
        return dashSet.dirty ? 1 : 0;
    }, function () {
        if (!dashSet.baseSnap) return;
        dashSet.draft = dashSetDraftFrom(dashSet.baseSnap);
        dashSet.dirty = false;
        dashSet.conflict = null;
        dashSettingsDrawForm();
    });
}

DASH_PAGE_IMPL.settings = function () {
    dashSettingsBind();
    dashSettingsDraw();
    dashSettingsLoad().then(function () { uiRefreshLoaded("settings"); });
};

registerPageRefresh("settings", 30000, function (isCurrent) { return dashSettingsLoad(isCurrent); });

var DASH_SEARCH_ICON ='<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"/></svg>';

if (typeof document !== "undefined" && document.readyState !== undefined) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", dashBoot);
    else dashBoot();
}
