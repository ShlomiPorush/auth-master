/*
 * Shared builders and helpers of the admin portal (v3 style).
 *
 * A classic script: every function is a top-level declaration in the one global
 * scope that common.js, ui.js, dashboard.js and auth-pages.js share, so no two
 * files may declare the same name. The file also loads in a bare `vm` context
 * for the unit tests (services/tests/js), so nothing here touches the page
 * while it loads; uiInstallGlobalListeners() does that, and only where a page
 * exists.
 *
 * Every layer over the page comes from one builder here: uiDialog (dialogs,
 * questions, phone sheets), uiPicker (filter drop-downs), uiMenu (row menus),
 * uiSortMenu (the Sort button's panel) and showToast. services/tests/
 * test_ui_guards.py fails on a layer built anywhere else.
 */

// ===========================================================================
// Basics
// ===========================================================================

var UI_CLOSE_ICON = '<svg width="20" height="20" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>';
var UI_COPY_ICON = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>';
var UI_CHECK_PATH = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path>';
var UI_LOCK_ICON = '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z"/></svg>';
var UI_SORT_ICON = '<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M3 7h13M3 12h9M3 17h5M17 10v10m0 0l-3-3m3 3l3-3"/></svg>';
var UI_DOTS_ICON = '<svg fill="currentColor" viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>';

function uiHasDom() {
    return typeof document !== "undefined" && !!document && typeof document.createElement === "function";
}

// The one escape for text that goes into markup: & < > " '
function esc(value) {
    return String(value === null || value === undefined ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// User text keeps its own direction (RT-2)
function uiUserText(value, cls) {
    return '<bdi dir="auto"' + (cls ? ' class="' + esc(cls) + '"' : "") + ">" + esc(value) + "</bdi>";
}

// Counts with thousands separators and the right singular or plural (FW-3).
// uiCountLabel(n, key) is tn(key, n): a count value of the catalog (js/text.js).
function uiNum(n) {
    return Number(n || 0).toLocaleString();
}
function uiCountLabel(n, key) {
    return tn(key, n);
}

// An identifier from the API shown as a word (a log action, a level, a TLS mode):
// the catalog's word for it under prefix, or the identifier itself when the
// catalog has none, so a value the UI does not know yet still shows
function uiIdWord(prefix, id) {
    var key = prefix + String(id);
    return Object.prototype.hasOwnProperty.call(UI_TEXT, key) ? t(key) : String(id);
}

function uiIsPhone() {
    return typeof window !== "undefined" && !!window.matchMedia && window.matchMedia("(max-width: 760px)").matches;
}
function uiTouchOnly() {
    return typeof window !== "undefined" && !!window.matchMedia && window.matchMedia("(hover: none) and (pointer: coarse)").matches;
}

// ===========================================================================
// Dates and times (FW-1, FW-2, FW-9). Every date on a page is written here, in
// the app time zone, in the Date Format and Time Format of Settings > General
// (both from /admin/api/me). Pages never format a date themselves.
// ===========================================================================

var uiAppZone = "UTC";
var uiDatePrefs = { date: "browser", time: "24h" };
// The values the server takes (app/display_settings.py) and the catalog word of each
var UI_DATE_FORMATS = [["browser", "dates.browser-order"], ["DD/MM/YYYY", "dates.dd-mm-yyyy"], ["MM/DD/YYYY", "dates.mm-dd-yyyy"],
    ["DD.MM.YYYY", "dates.dd-mm-yyyy-dot"], ["YYYY-MM-DD", "dates.yyyy-mm-dd"]];
var UI_TIME_FORMATS = [["24h", "dates.24-hour"], ["12h", "dates.12-hour"], ["browser", "dates.browser-clock"]];
// How each fixed Date Format writes the day, the month and the year
var UI_DATE_ORDERS = {
    "DD/MM/YYYY": { order: ["day", "month", "year"], sep: "/" },
    "MM/DD/YYYY": { order: ["month", "day", "year"], sep: "/" },
    "DD.MM.YYYY": { order: ["day", "month", "year"], sep: "." },
    "YYYY-MM-DD": { order: ["year", "month", "day"], sep: "-" },
};
var uiZoneFormatters = {};

function uiZoneValid(tz) {
    if (!tz || typeof tz !== "string") return false;
    try {
        new Intl.DateTimeFormat("en-US", { timeZone: tz });
        return true;
    } catch (e) {
        return false;
    }
}

// The server's zone; one this browser cannot use falls back to UTC
function uiSetAppZone(tz) {
    uiAppZone = uiZoneValid(tz) ? tz : "UTC";
    return uiAppZone;
}

// The formats the server sends; an unknown value gives the default (Browser order, 24-hour)
function uiSetDatePrefs(date, time) {
    var dateOk = UI_DATE_FORMATS.some(function (f) { return f[0] === date; });
    var timeOk = UI_TIME_FORMATS.some(function (f) { return f[0] === time; });
    uiDatePrefs = { date: dateOk ? date : "browser", time: timeOk ? time : "24h" };
    return uiDatePrefs;
}

function uiParseInstant(value) {
    if (value === null || value === undefined || value === "") return NaN;
    if (typeof value === "number") return value;
    if (value instanceof Date) return value.getTime();
    return Date.parse(String(value));
}

function uiPad(n) {
    return n < 10 ? "0" + n : String(n);
}

// The wall clock of an instant in a zone: { year, month, day, hour, minute, second }
function uiZoneParts(ms, tz) {
    var zone = tz || uiAppZone;
    var f = uiZoneFormatters[zone];
    if (!f) {
        f = new Intl.DateTimeFormat("en-US", {
            timeZone: zone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", second: "2-digit",
        });
        uiZoneFormatters[zone] = f;
    }
    var out = {};
    f.formatToParts(new Date(ms)).forEach(function (p) {
        if (p.type !== "literal") out[p.type] = parseInt(p.value, 10);
    });
    if (out.hour === 24) out.hour = 0;
    return { year: out.year, month: out.month, day: out.day, hour: out.hour, minute: out.minute, second: out.second };
}

// Minutes the zone is ahead of UTC at that instant
function uiZoneOffset(ms, tz) {
    var whole = ms - (((ms % 1000) + 1000) % 1000);
    var p = uiZoneParts(whole, tz);
    return Math.round((Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - whole) / 60000);
}

// Date in the order the Date Format names; numbers only, never month names
function writeDate(p, withYear, prefs) {
    var fixed = UI_DATE_ORDERS[(prefs || uiDatePrefs).date];
    var d = uiPad(p.day);
    var m = uiPad(p.month);
    var y = String(p.year);
    if (fixed) {
        var part = { day: d, month: m, year: y };
        return fixed.order.filter(function (k) { return withYear || k !== "year"; }).map(function (k) { return part[k]; }).join(fixed.sep);
    }
    // Browser order: the browser's language decides the order and separators of
    // the same numbers (the day itself was already taken in the app zone)
    var opts = withYear ? { timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit" } : { timeZone: "UTC", month: "2-digit", day: "2-digit" };
    try {
        return new Intl.DateTimeFormat(undefined, opts).format(new Date(Date.UTC(p.year, p.month - 1, p.day, 12)));
    } catch (e) {
        return withYear ? d + "/" + m + "/" + y : d + "/" + m;
    }
}

function writeClock(p, seconds, prefs) {
    var pick = (prefs || uiDatePrefs).time;
    if (pick === "browser") {
        // The clock of the browser's language: 1:05 PM on a 12-hour one, 13:05 on a 24-hour one
        var opts = { timeZone: "UTC", hour: "numeric", minute: "2-digit" };
        if (seconds) opts.second = "2-digit";
        try {
            var cycle = new Intl.DateTimeFormat(undefined, opts).resolvedOptions().hourCycle;
            if (cycle !== "h11" && cycle !== "h12") opts.hour = "2-digit";
            return new Intl.DateTimeFormat(undefined, opts).format(new Date(Date.UTC(2000, 0, 1, p.hour, p.minute, p.second || 0)));
        } catch (e) {
            pick = "24h";
        }
    }
    var tail = ":" + uiPad(p.minute) + (seconds ? ":" + uiPad(p.second) : "");
    if (pick === "12h") {
        var h = p.hour % 12 === 0 ? 12 : p.hour % 12;
        return p.hour < 12 ? t("dates.am", { time: h + tail }) : t("dates.pm", { time: h + tail });
    }
    return uiPad(p.hour) + tail;
}

// Date and time, to the second unless { seconds: false }
function formatTime(value, opts) {
    var ms = uiParseInstant(value);
    if (isNaN(ms)) return value ? String(value) : "-";
    var p = uiZoneParts(ms, (opts && opts.zone) || uiAppZone);
    var seconds = !(opts && opts.seconds === false);
    return writeDate(p, true, opts && opts.prefs) + ", " + writeClock(p, seconds, opts && opts.prefs);
}

function formatDay(value, opts) {
    var ms = uiParseInstant(value);
    if (isNaN(ms)) return value ? String(value) : "-";
    return writeDate(uiZoneParts(ms, (opts && opts.zone) || uiAppZone), true, opts && opts.prefs);
}

// Day and month only, for chart axis labels
function formatShortDay(value, opts) {
    var ms = uiParseInstant(value);
    if (isNaN(ms)) return value ? String(value) : "-";
    return writeDate(uiZoneParts(ms, (opts && opts.zone) || uiAppZone), false, opts && opts.prefs);
}

function formatClock(value, opts) {
    var ms = uiParseInstant(value);
    if (isNaN(ms)) return value ? String(value) : "-";
    return writeClock(uiZoneParts(ms, (opts && opts.zone) || uiAppZone), false, opts && opts.prefs);
}

// A list row: the time today, day and month earlier this year, the whole date before (FW-1)
function formatListTime(value, opts) {
    var ms = uiParseInstant(value);
    if (isNaN(ms)) return value ? String(value) : "-";
    var zone = (opts && opts.zone) || uiAppZone;
    var prefs = opts && opts.prefs;
    var now = uiZoneParts(opts && opts.now !== undefined ? opts.now : Date.now(), zone);
    var p = uiZoneParts(ms, zone);
    if (p.year === now.year && p.month === now.month && p.day === now.day) return writeClock(p, false, prefs);
    if (p.year === now.year) return writeDate(p, false, prefs);
    return writeDate(p, true, prefs);
}

// "just now", "N min ago", "N h ago", "N d ago"; future times clamp to "just now"
function formatAgo(value, now) {
    var ms = uiParseInstant(value);
    if (isNaN(ms)) return "-";
    var diff = Math.max(0, (now === undefined ? Date.now() : now) - ms) / 1000;
    if (diff < 60) return t("dates.just-now");
    if (diff < 3600) return t("dates.min-ago", { n: Math.round(diff / 60) });
    if (diff < 172800) return t("dates.h-ago", { n: Math.round(diff / 3600) });
    return t("dates.d-ago", { n: Math.round(diff / 86400) });
}

// A date cell: <time> with the instant, the full timestamp on hover (in zone, else the app zone)
function uiTime(value, text, zone) {
    var ms = uiParseInstant(value);
    if (isNaN(ms)) return '<span class="ui-muted">-</span>';
    var shown = text === undefined ? formatListTime(ms) : text;
    return '<time class="ui-num" datetime="' + esc(new Date(ms).toISOString()) + '" title="' + esc(formatTime(ms, zone ? { zone: zone } : undefined)) + '">' + esc(shown) + "</time>";
}

// The wall time of an instant for a datetime-local field, in the app zone
function uiToZonedInput(value, tz) {
    var ms = uiParseInstant(value);
    if (isNaN(ms)) return "";
    var p = uiZoneParts(ms, tz || uiAppZone);
    return p.year + "-" + uiPad(p.month) + "-" + uiPad(p.day) + "T" + uiPad(p.hour) + ":" + uiPad(p.minute);
}

// A wall time typed in the app zone back to an instant (ISO string). A time the
// clock passes twice (DST ends) is the earlier instant; a time the clock skips
// (DST starts) moves forward by the gap.
function uiFromZonedInput(text, tz) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(text || "");
    if (!m) return null;
    var zone = tz || uiAppZone;
    var wall = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
    var before = uiZoneOffset(wall - 86400000, zone);
    var after = uiZoneOffset(wall + 86400000, zone);
    var offsets = before === after ? [before] : [before, after];
    var found = [];
    offsets.forEach(function (off) {
        var instant = wall - off * 60000;
        var p = uiZoneParts(instant, zone);
        if (Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) === wall) found.push(instant);
    });
    if (found.length) return new Date(Math.min.apply(null, found)).toISOString();
    // Not on the clock: the offset in force before the change places it the gap later
    return new Date(wall - before * 60000).toISOString();
}

// A short name of the app zone for hints under date fields
function uiZoneName() {
    return uiAppZone;
}

// ===========================================================================
// Log lists: rows from several answers merged in server order
// (created_at DESC, id DESC), deduplicated by id
// ===========================================================================

// A sortable number for an ISO time, to the microsecond
function uiLogInstant(iso) {
    var s = String(iso || "");
    var m = /\.(\d+)/.exec(s);
    var frac = m ? (m[1] + "000000").slice(0, 6) : "000000";
    var ms = Date.parse(s.replace(/\.(\d+)/, ""));
    if (isNaN(ms)) return -Infinity;
    return ms * 1000 + parseInt(frac, 10);
}

function uiLogCompare(a, b) {
    var ia = uiLogInstant(a.createdAt);
    var ib = uiLogInstant(b.createdAt);
    if (ia !== ib) return ib > ia ? 1 : -1;
    var x = String(a.id);
    var y = String(b.id);
    return x < y ? 1 : x > y ? -1 : 0;
}

// The union of two lists; a row in both takes the newer copy (from incoming)
function uiLogMerge(shown, incoming) {
    var byId = {};
    var order = [];
    (shown || []).forEach(function (row) {
        if (!(row.id in byId)) order.push(row.id);
        byId[row.id] = row;
    });
    (incoming || []).forEach(function (row) {
        if (!(row.id in byId)) order.push(row.id);
        byId[row.id] = row;
    });
    return order.map(function (id) { return byId[id]; }).sort(uiLogCompare);
}

// The rows a refresh brings that are new to the list. While older rows are still
// to load (hasMore), a row that sorts below the last row shown belongs to Load
// more, not to the refresh, so it is neither counted nor offered.
function uiLogFresh(shown, collected, hasMore) {
    var ids = {};
    (shown || []).forEach(function (r) { ids[r.id] = true; });
    var last = shown && shown.length ? shown[shown.length - 1] : null;
    return (collected || []).filter(function (r) {
        if (ids[r.id]) return false;
        return !hasMore || !last || uiLogCompare(r, last) < 0;
    });
}

// ===========================================================================
// Presses and selections: a selecting gesture is never an action (TB-8, DS-9)
// ===========================================================================

var uiPress = null;

function uiPressStart(event) {
    uiPress = { x: event.clientX, y: event.clientY, target: event.target, down: true };
}
function uiPressEnd() {
    if (uiPress) uiPress.down = false;
}
function uiPressMoved(event) {
    return !!uiPress && Math.hypot(event.clientX - uiPress.x, event.clientY - uiPress.y) > 3;
}

// A click on a backdrop counts only when the press started on it too
function uiBackdropClick(event, backdrop) {
    var el = backdrop || event.currentTarget;
    if (event.target !== el) return false;
    return !event.detail || !uiPress || uiPress.target === el;
}

// The end of a selecting gesture in a row (a drag, a double click, Shift) opens nothing (TB-8)
function uiRowHeld(event, row) {
    if (!event || !event.detail || !uiHasDom()) return false;
    var sel = window.getSelection && window.getSelection();
    if (!sel || !sel.rangeCount || !String(sel)) return false;
    try { if (!sel.getRangeAt(0).intersectsNode(row)) return false; } catch (e) { return false; }
    return uiPressMoved(event) || event.detail > 1 || event.shiftKey;
}

// Text selected in an element, or a press that started in it still held
function uiTextHeld(el) {
    if (!el || !uiHasDom()) return false;
    if (uiPress && uiPress.down && uiPress.target instanceof Node && el.contains(uiPress.target)) return true;
    var sel = window.getSelection && window.getSelection();
    if (!sel || !sel.rangeCount || !String(sel)) return false;
    try { return sel.getRangeAt(0).intersectsNode(el); } catch (e) { return false; }
}

// ===========================================================================
// Toast (FB-2): one at a time, 4 seconds, waits while hovered or focused, and
// may carry one action
// ===========================================================================

// opts: { action: { label, ariaLabel, run }, duration }
//   action: one button; a click runs run() and closes the toast. When run()
//     returns a promise the toast stays until it settles (further clicks do
//     nothing), so the message that follows takes its place.
//   duration: milliseconds before it fades (4000); Infinity keeps it until closed
function showToast(message, type, opts) {
    if (!uiHasDom()) return null;
    var o = opts || {};
    var action = o.action && o.action.label && typeof o.action.run === "function" ? o.action : null;
    var icons = { success: "✓", error: "✗", warning: "!", info: "i" };
    var kind = icons[type] ? type : "info";
    var old = document.getElementById("ui-toast");
    var focusWasInIt = !!old && old.contains(document.activeElement);
    if (old) old.remove();

    var toast = document.createElement("div");
    toast.id = "ui-toast";
    toast.className = "ui-toast ui-toast-" + kind;
    toast.setAttribute("role", kind === "error" ? "alert" : "status");
    var icon = document.createElement("span");
    icon.className = "ui-toast-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = icons[kind];
    var text = document.createElement("p");
    text.textContent = String(message === null || message === undefined ? "" : message);
    toast.appendChild(icon);
    toast.appendChild(text);
    var actionBtn = null;
    if (action) {
        actionBtn = document.createElement("button");
        actionBtn.type = "button";
        actionBtn.className = "ui-btn ui-btn-sm ui-toast-action";
        actionBtn.textContent = action.label;
        if (action.ariaLabel) actionBtn.setAttribute("aria-label", action.ariaLabel);
        toast.appendChild(actionBtn);
    }
    var x = document.createElement("button");
    x.type = "button";
    x.className = "ui-icon-btn";
    x.setAttribute("data-toast-x", "");
    x.setAttribute("title", t("common.close"));
    x.setAttribute("aria-label", t("common.close"));
    x.innerHTML = UI_CLOSE_ICON;
    toast.appendChild(x);
    x.addEventListener("click", function () { toast.remove(); });
    document.body.appendChild(toast);
    // The keyboard was in the toast this one replaces: it moves to this one
    if (focusWasInIt) (actionBtn || x).focus({ preventScroll: true });

    var duration = typeof o.duration === "number" && o.duration > 0 ? o.duration : 4000;
    var left = duration;
    var started = Date.now();
    var timer = null;
    var fading = false;
    var removing = null;
    var busy = false;
    var hold = function () { return busy || toast.matches(":hover") || toast.contains(document.activeElement); };
    if (actionBtn) {
        actionBtn.addEventListener("click", function () {
            if (busy) return;
            var result;
            try { result = action.run(); } catch (e) { result = null; }
            if (!result || typeof result.then !== "function") {
                toast.remove();
                return;
            }
            // Running: the toast stays and keeps the keyboard; its button says it is busy
            busy = true;
            pause();
            actionBtn.setAttribute("aria-disabled", "true");
            var done = function () {
                busy = false;
                if (toast.isConnected) toast.remove();
            };
            result.then(done, done);
        });
    }
    var fade = function () {
        if (!toast.isConnected) return;
        fading = true;
        toast.classList.add("is-fading");
        removing = setTimeout(function () { toast.remove(); }, 300);
    };
    var run = function () {
        clearTimeout(timer);
        started = Date.now();
        // Infinity: it stays until closed
        timer = isFinite(left) ? setTimeout(fade, left) : null;
    };
    var pause = function () {
        if (fading) {
            clearTimeout(removing);
            fading = false;
            timer = null;
            left = 1500;
            toast.classList.remove("is-fading");
            return;
        }
        if (timer === null) return;
        clearTimeout(timer);
        timer = null;
        left = Math.max(1500, left - (Date.now() - started));
    };
    var resume = function () { if (timer === null && !hold()) run(); };
    toast.addEventListener("mouseenter", pause);
    toast.addEventListener("focusin", pause);
    toast.addEventListener("mouseleave", function () { setTimeout(resume, 0); });
    toast.addEventListener("focusout", function () { setTimeout(resume, 0); });
    if (!hold()) run();
    return toast;
}

// ===========================================================================
// Clipboard (FB-12)
// ===========================================================================

// Without the Clipboard API (plain HTTP): a hidden text field and the copy command
function uiCopyFallback(text) {
    if (!uiHasDom()) return false;
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
    return ok;
}

// Resolves true when the text is on the clipboard
function uiCopyText(text) {
    if (!text) return Promise.resolve(false);
    if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return uiCopyFallback(text); });
    }
    return Promise.resolve(uiCopyFallback(text));
}

// A value that copies on a click, shown in mono; the full value is copied
function uiCopyable(text, cls) {
    if (!text || text === "-") return '<span class="ui-muted">' + esc(text || "-") + "</span>";
    return '<button type="button" class="ui-copy' + (cls ? " " + esc(cls) : "") + '" data-copy="' + esc(text) + '" title="' + esc(t("common.click-to-copy")) + '">' +
        "<bdi>" + esc(text) + "</bdi>" + UI_COPY_ICON + "</button>";
}

function uiCopyClick(button) {
    var text = button.getAttribute("data-copy") || "";
    uiCopyText(text).then(function (ok) {
        if (!ok) {
            var secure = typeof window !== "undefined" && window.isSecureContext;
            showToast(secure ? t("common.failed-to-copy") : t("common.copying-needs-https-select-the-text"), "error");
            return;
        }
        showToast(t("common.copied", { text: text }), "success");
        var svg = button.querySelector("svg");
        if (!svg) return;
        var before = svg.innerHTML;
        button.classList.add("is-copied");
        svg.innerHTML = UI_CHECK_PATH;
        setTimeout(function () {
            button.classList.remove("is-copied");
            svg.innerHTML = before;
        }, 1500);
    });
}

// ===========================================================================
// Dialogs (DS-16, DS-24) and questions (DS-12)
// ===========================================================================

// Every dialog comes from here: a head with the title and the shared X, a body
// that scrolls, actions in a foot, a set width; on a phone a bottom sheet.
//   size: "sm" (460px), "md" (680px) or "wide" (1100px), required
//   close: called with no argument by the X (and Escape, Back, a pull down);
//     required unless role is "alertdialog" (a question closes by its Cancel)
//   backdropClose: only for windows without typed input (DS-9)
//   form: { id, onSubmit } makes the body a form; foot buttons use form="<id>"
// Returns { backdrop, dialog, body, foot }.
function uiDialog(o) {
    var opts = o || {};
    var sizes = { sm: "ui-dialog-sm", md: "ui-dialog-md", wide: "" };
    if (!(opts.size in sizes)) throw new Error("uiDialog: size must be sm, md or wide");
    var role = opts.role || "dialog";
    if (role !== "alertdialog" && typeof opts.close !== "function") throw new Error("uiDialog: a dialog needs a close function for its X");
    var backdrop = document.createElement("div");
    if (opts.id) backdrop.id = opts.id;
    backdrop.className = "ui-dialog-backdrop" + (opts.backdropClass ? " " + opts.backdropClass : "");
    if (opts.noHistory) backdrop.setAttribute("data-no-history", "");
    var titleId = "ui-dlg-title-" + (++uiDialogCount);
    var form = opts.form ? opts.form : null;
    var bodyTag = form ? "form" : "div";
    var x = typeof opts.close === "function"
        ? '<button type="button" class="ui-icon-btn" data-dialog-x title="' + esc(t("common.close")) + '" aria-label="' + esc(t("common.close")) + '">' + UI_CLOSE_ICON + "</button>" : "";
    backdrop.innerHTML =
        '<div class="ui-dialog ' + (sizes[opts.size] || "") + (opts.dialogClass ? " " + opts.dialogClass : "") + '" role="' + role +
        '" aria-labelledby="' + titleId + '"' + (opts.describedBy ? ' aria-describedby="' + esc(opts.describedBy) + '"' : "") + ">" +
        '<div class="ui-dialog-head"><h3 id="' + titleId + '">' + esc(opts.title || "") + "</h3>" + x + "</div>" +
        "<" + bodyTag + (form ? ' id="' + esc(form.id) + '" novalidate' : "") + ' class="ui-dialog-body' + (opts.bodyClass ? " " + opts.bodyClass : "") + '">' +
        (opts.body || "") + "</" + bodyTag + ">" +
        (opts.foot ? '<div class="ui-dialog-foot">' + opts.foot + "</div>" : "") +
        "</div>";
    var dialog = backdrop.firstElementChild;
    var body = dialog.querySelector(".ui-dialog-body");
    var foot = dialog.querySelector(".ui-dialog-foot");
    if (x) dialog.querySelector("[data-dialog-x]").addEventListener("click", function () { opts.close(); });
    if (typeof opts.backdropClose === "function") {
        backdrop.addEventListener("click", function (event) { if (uiBackdropClick(event, backdrop)) opts.backdropClose(); });
    }
    if (form && typeof form.onSubmit === "function") {
        body.addEventListener("submit", function (event) { event.preventDefault(); form.onSubmit(event); });
    }
    document.body.appendChild(backdrop);
    uiDialogSheet(backdrop);
    return { backdrop: backdrop, dialog: dialog, body: body, foot: foot };
}
var uiDialogCount = 0;

// What the fields of a typed dialog hold, to tell a change from how it opened (DS-9)
function uiFieldsSnapshot(root) {
    var values = [];
    root.querySelectorAll("input, select, textarea").forEach(function (el) {
        if (el.type === "checkbox" || el.type === "radio") values.push(el.checked ? "1" : "0");
        else values.push(el.value);
    });
    return JSON.stringify(values);
}

var uiGuarded = typeof WeakMap !== "undefined" ? new WeakMap() : null;
var uiGuardedOpen = [];
function uiGuardDialog(d) {
    if (uiGuarded) uiGuarded.set(d.backdrop, uiFieldsSnapshot(d.body));
    uiGuardedOpen.push(d);
}
// Open typed dialogs that hold a change: they count as unsaved changes for the
// router's question and for closing or reloading the browser tab (SV-1, DS-9)
function uiDirtyDialogs() {
    uiGuardedOpen = uiGuardedOpen.filter(function (d) { return d.backdrop.isConnected; });
    return uiGuardedOpen.filter(uiDialogDirty);
}
function uiDialogDirty(d) {
    if (!uiGuarded || !uiGuarded.has(d.backdrop)) return false;
    return uiGuarded.get(d.backdrop) !== uiFieldsSnapshot(d.body);
}
// Close a typed dialog, asking first only when a field differs from how it opened
function uiCloseGuarded(d, close) {
    if (!uiDialogDirty(d)) { close(); return Promise.resolve(true); }
    return uiConfirm({
        title: t("common.discard-your-changes"), message: t("common.what-you-changed-in-this-window"),
        confirmText: t("common.discard"), cancelText: t("common.keep-editing"), danger: true, focusCancel: true,
    }).then(function (ok) { if (ok) close(); return ok; });
}

// A question in the shared frame, resolving true or false. No X: Cancel, Escape,
// Back, a click outside and a pull down on a phone all cancel. The confirm button
// takes the focus (Enter confirms) unless the question protects work (focusCancel).
var uiConfirmClose = null;
function uiConfirm(o) {
    var opts = o || {};
    if (uiConfirmClose) uiConfirmClose(false);
    return new Promise(function (resolve) {
        var d = uiDialog({
            title: opts.title || t("common.confirm"), size: "sm", role: "alertdialog", backdropClass: "ui-confirm",
            noHistory: !!opts.noHistory, describedBy: "ui-confirm-text",
            body: '<p class="ui-confirm-text" id="ui-confirm-text">' + esc(opts.message || "") + "</p>",
            foot: '<button type="button" class="ui-btn" data-cancel>' + esc(opts.cancelText || t("common.cancel")) + "</button>" +
                '<button type="button" class="ui-btn ' + (opts.danger ? "ui-btn-danger-solid" : "ui-btn-primary") + '" data-confirm>' +
                esc(opts.confirmText || t("common.confirm")) + "</button>",
        });
        var settled = false;
        function done(result) {
            if (settled) return;
            settled = true;
            if (uiConfirmClose === done) uiConfirmClose = null;
            d.backdrop.remove();
            resolve(result);
        }
        uiConfirmClose = done;
        d.backdrop.addEventListener("click", function (event) { if (uiBackdropClick(event, d.backdrop)) done(false); });
        d.foot.querySelector("[data-cancel]").addEventListener("click", function () { done(false); });
        d.foot.querySelector("[data-confirm]").addEventListener("click", function () { done(true); });
        setTimeout(function () {
            if (settled) return;
            d.foot.querySelector(opts.focusCancel ? "[data-cancel]" : "[data-confirm]").focus({ preventScroll: true });
        }, 100);
    });
}

// Phones: every dialog is a bottom sheet at four fifths that rises with the
// reader's scroll to the full screen; a drag on its head grows it, a pull down
// past min(120px, a quarter of it) closes it through its X or Cancel (DS-1, DS-3)
function uiDialogSheet(backdrop) {
    var dialog = backdrop.querySelector(".ui-dialog");
    if (!dialog || backdrop.getAttribute("data-sheet")) return;
    backdrop.setAttribute("data-sheet", "1");
    backdrop.addEventListener("scroll", function (event) {
        var body = event.target;
        if (!uiIsPhone() || !(body instanceof Element) || !dialog.contains(body) || body.scrollTop <= 0) return;
        var height = dialog.getBoundingClientRect().height;
        var room = window.innerHeight - height;
        var grow = Math.min(body.scrollTop, room);
        if (grow < 1) return;
        backdrop.classList.add("is-grown");
        dialog.style.height = height + grow + "px";
        body.scrollTop -= grow;
        backdrop.classList.toggle("is-full", room - grow < 1);
    }, { capture: true, passive: true });

    var startY = null;
    var startHeight = 0;
    var fixedPart = false;
    var pulled = 0;
    var ignore = false;
    var closeButton = function () {
        var b = dialog.querySelector(".ui-dialog-head [data-dialog-x]") || dialog.querySelector(".ui-dialog-foot [data-cancel]");
        return b && !b.disabled ? b : null;
    };
    dialog.addEventListener("touchstart", function (event) {
        if (event.touches.length > 1) { ignore = true; return; }
        if (ignore) return;
        startY = null;
        if (!uiIsPhone()) return;
        fixedPart = !event.target.closest(".ui-dialog-body");
        if (!fixedPart) {
            for (var el = event.target; el && el !== dialog; el = el.parentElement) {
                if (el.scrollTop > 0) return;
            }
        }
        startY = event.touches[0].clientY;
        startHeight = dialog.getBoundingClientRect().height;
        pulled = 0;
        dialog.style.transition = "none";
        dialog.classList.toggle("is-held", fixedPart);
    }, { passive: true });
    dialog.addEventListener("touchmove", function (event) {
        if (ignore || startY === null) return;
        var moved = event.touches[0].clientY - startY;
        if (moved < 0 && fixedPart) {
            var h = Math.min(window.innerHeight, startHeight - moved);
            backdrop.classList.add("is-grown");
            dialog.style.height = h + "px";
            backdrop.classList.toggle("is-full", window.innerHeight - h < 1);
        }
        pulled = closeButton() ? Math.max(0, moved) : 0;
        dialog.style.transform = pulled ? "translateY(" + pulled + "px)" : "";
    }, { passive: true });
    var end = function (event) {
        if (ignore) {
            if (!event || !event.touches || event.touches.length === 0) ignore = false;
            return;
        }
        dialog.classList.remove("is-held");
        if (startY === null) return;
        startY = null;
        dialog.style.transition = "transform .2s ease";
        if (pulled > Math.min(120, dialog.offsetHeight / 4)) {
            dialog.style.transform = "translateY(100%)";
            setTimeout(function () {
                var b = closeButton();
                dialog.style.transition = "";
                dialog.style.transform = "";
                if (b) b.click();
            }, 200);
        } else {
            dialog.style.transform = "";
        }
    };
    dialog.addEventListener("touchend", end);
    dialog.addEventListener("touchcancel", end);
}

// ===========================================================================
// Layers: Escape and Back close only the top one, focus moves in and back,
// what lies under the top layer is inert (DS-8, DS-10, DS-11)
// ===========================================================================

var uiLayerOpeners = typeof Map !== "undefined" ? new Map() : null;
var uiLayerInertSet = typeof Set !== "undefined" ? new Set() : null;
var uiLastAction = null;
var uiNamedOpener = null;
var uiOverlayEntry = false;
var uiIgnorePop = false;
var uiLayerQueued = false;

// The phone search over the whole screen (SR-8) is a layer too: Escape and Back
// close it, and what lies under it is inert
function uiOpenLayers() {
    if (!uiHasDom()) return [];
    return Array.prototype.slice.call(document.querySelectorAll(".ui-dialog-backdrop, .ui-gs.is-sheet"))
        .filter(function (el) { return el.getClientRects().length > 0; })
        .sort(function (a, b) {
            var za = parseInt(getComputedStyle(a).zIndex, 10) || 0;
            var zb = parseInt(getComputedStyle(b).zIndex, 10) || 0;
            if (za !== zb) return za - zb;
            return a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
        });
}

// Drop a layer without asking (a jump several steps back, Leave): its owner hears
// "ui-drop" first, so work it waits for (a reveal) knows the dialog is gone
function uiLayerDrop(layer) {
    try { layer.dispatchEvent(new CustomEvent("ui-drop")); } catch (e) { /* old browsers */ }
    layer.remove();
}

// Close a layer the way a user would: its X, else its Cancel
function uiLayerClose(layer) {
    var b = layer.querySelector("[data-dialog-x]") || layer.querySelector("[data-cancel]");
    if (b && !b.disabled) { b.click(); return true; }
    return false;
}

function uiLayerControl(el) {
    return el instanceof Element ? el.closest("button, a[href], input, select, textarea, [tabindex]") : null;
}

// Name the control a layer gives the focus back to, for a layer that opens after a request
function uiLayerOpener(el) {
    uiNamedOpener = uiLayerControl(el);
}

function uiLayerFocusIn(layer) {
    if (layer.contains(document.activeElement)) return;
    var usable = function (el) { return el.getClientRects().length > 0 && !el.disabled; };
    var fields = uiTouchOnly() ? [] : Array.prototype.slice.call(layer.querySelectorAll("[autofocus], input:not([type=hidden]):not([type=checkbox]), select, textarea"));
    var target = fields.filter(usable)[0] || Array.prototype.slice.call(layer.querySelectorAll(".ui-btn-primary")).filter(usable)[0];
    if (!target) {
        target = layer.querySelector(".ui-dialog") || layer;
        if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
        target.setAttribute("data-layer-panel", "");
    }
    target.focus({ preventScroll: true });
}

function uiLayerFocusBack(opener, below) {
    if (opener && opener.isConnected && opener.getClientRects().length && !opener.closest("[inert]") && !opener.disabled) {
        opener.focus({ preventScroll: true });
        return;
    }
    if (below) { uiLayerFocusIn(below); return; }
    var heading = uiPageHeading();
    if (heading) {
        if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
        heading.focus({ preventScroll: true });
    }
}

// Everything beside the path to the top layer is inert, except toasts and,
// beside the phone More sheet, the tab bar
function uiLayerApplyInert(top) {
    if (!uiLayerInertSet) return;
    var targets = new Set();
    if (top) {
        var keep = Array.prototype.slice.call(document.querySelectorAll(".ui-toast, .ui-menu"));
        if (top.classList.contains("ui-more-layer")) keep = keep.concat(Array.prototype.slice.call(document.querySelectorAll(".ui-tabbar")));
        var mark = function (el) {
            if (el === top || el.contains(top) || ["SCRIPT", "STYLE", "TEMPLATE"].indexOf(el.tagName) !== -1) return;
            if (keep.indexOf(el) !== -1) return;
            if (keep.some(function (k) { return el.contains(k); })) { Array.prototype.slice.call(el.children).forEach(mark); return; }
            targets.add(el);
        };
        for (var node = top; node.parentElement && node !== document.body; node = node.parentElement) {
            Array.prototype.slice.call(node.parentElement.children).forEach(function (child) { if (child !== node) mark(child); });
        }
    }
    uiLayerInertSet.forEach(function (el) {
        if (!targets.has(el)) { el.inert = false; uiLayerInertSet.delete(el); }
    });
    targets.forEach(function (el) {
        if (!uiLayerInertSet.has(el) && !el.inert) { el.inert = true; uiLayerInertSet.add(el); }
    });
}

function uiSyncLayers() {
    uiLayerQueued = false;
    if (!uiLayerOpeners) return;
    var open = uiOpenLayers();
    var top = open.length ? open[open.length - 1] : null;
    var closed = [];
    uiLayerOpeners.forEach(function (opener, el) { if (open.indexOf(el) === -1) closed.push([el, opener]); });
    closed.forEach(function (pair) { uiLayerOpeners.delete(pair[0]); });
    var opened = open.filter(function (el) { return !uiLayerOpeners.has(el); });
    opened.forEach(function (el) {
        var dlg = el.querySelector("[role=dialog], [role=alertdialog]");
        if (dlg) dlg.setAttribute("aria-modal", "true");
        var opener = uiNamedOpener || uiLastAction;
        if (opener && el.contains(opener)) opener = null;
        uiLayerOpeners.set(el, opener);
    });
    if (opened.length) uiNamedOpener = null;
    uiLayerApplyInert(top);
    if (top && opened.indexOf(top) !== -1) uiLayerFocusIn(top);
    else if (closed.length && !(top && top.contains(document.activeElement))) uiLayerFocusBack(closed[closed.length - 1][1], top);
    if (document.body) document.body.classList.toggle("ui-layer-on", !!top);

    // One history entry while anything is open, so Back closes the top layer
    var needsEntry = open.some(function (el) { return !el.hasAttribute("data-no-history"); });
    if (needsEntry && !uiOverlayEntry && uiRouterReady) {
        var st = history.state || {};
        uiHistIdx += 1;
        history.pushState({ page: st.page, idx: uiHistIdx, overlay: true }, "", window.location.href);
        uiOverlayEntry = true;
    } else if (!needsEntry && uiOverlayEntry) {
        uiOverlayEntry = false;
        if (history.state && history.state.overlay) {
            uiIgnorePop = true;
            history.back();
        }
    }
}

function uiQueueLayerSync() {
    if (uiLayerQueued) return;
    uiLayerQueued = true;
    // A task, not an animation frame: a browser tab in the background runs no frames,
    // and Back must still find the layer's history entry
    setTimeout(uiSyncLayers, 0);
}

function uiOnKeyEscape(event) {
    if (event.key !== "Escape" || event.isComposing) return;
    if (uiPickerState.open) {
        event.preventDefault();
        event.stopImmediatePropagation();
        uiPickerClose(true);
        return;
    }
    try { if (document.querySelector(":popover-open")) return; } catch (e) { /* no popover support */ }
    var open = uiOpenLayers();
    var top = open[open.length - 1];
    if (!top) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    uiLayerClose(top);
}

// ===========================================================================
// Router: one owner of history for pages and layers (NL-3, DS-10)
// ===========================================================================

var uiPages = {};
var uiPageShow = null;
var uiPageCurrent = "";
var uiHistIdx = 0;
var uiRouterReady = false;
var uiPopUndo = false;
var uiPopConfirmed = false;
var uiLeaveAsking = false;
var uiUnsavedSources = [];
var uiScrolls = {};
var uiAppTitle = "common.auth-service";

// Who holds unsaved changes: count() returns how many; reset() puts them back
// to how they were loaded, when the user chose to leave them
function uiUnsavedRegister(count, reset) {
    uiUnsavedSources.push({ count: count, reset: reset });
}
// Nothing can be saved any more (the session ended): stop guarding, so leaving asks nothing
var uiUnsavedReleased = false;
function uiUnsavedRelease() {
    uiUnsavedReleased = true;
}
function uiUnsavedCount() {
    if (uiUnsavedReleased) return 0;
    return uiUnsavedSources.reduce(function (sum, src) {
        try { return sum + (Number(src.count()) || 0); } catch (e) { return sum; }
    }, 0) + uiDirtyDialogs().length;
}
// Leave was chosen: what was unsaved is dropped, so it does not come back and ask again
function uiUnsavedDiscard() {
    uiUnsavedSources.forEach(function (src) {
        try { if (src.count() && typeof src.reset === "function") src.reset(); } catch (e) { /* keep going */ }
    });
    uiDirtyDialogs().forEach(function (d) { uiLayerDrop(d.backdrop); });
}
function uiConfirmLeave() {
    var n = uiUnsavedCount();
    uiLeaveAsking = true;
    return uiConfirm({
        title: t("common.leave-without-saving"),
        message: tn("common.leave-unsaved", n),
        confirmText: t("common.leave"), cancelText: t("common.stay"), danger: true, focusCancel: true, noHistory: true,
    }).then(function (ok) {
        uiLeaveAsking = false;
        if (ok) uiUnsavedDiscard();
        return ok;
    });
}

function uiPagePath(id) {
    return basePath() + uiPages[id].path;
}

// A part of a page's address after its own path (a Settings section, NL-3)
var UI_SUB_ID = /^[a-z0-9_-]+$/;

// The page of an address and the part after the page's path:
// /admin/settings/redis is { id: "settings", sub: "redis" }. A part that is not
// an id gives the page with no part, so the page opens its first section.
function uiParsePath(pathname) {
    var p = pathname;
    var b = basePath();
    if (b && p.indexOf(b) === 0) p = p.slice(b.length);
    if (p.length > 1 && p.charAt(p.length - 1) === "/") p = p.slice(0, -1);
    for (var id in uiPages) {
        if (!Object.prototype.hasOwnProperty.call(uiPages, id)) continue;
        var own = uiPages[id].path;
        if (own === p) return { id: id, sub: "" };
        // Only a page with sections owns the addresses under its path (/admin/settings/<section>)
        if (uiPages[id].sections && p.indexOf(own + "/") === 0) {
            var rest = p.slice(own.length + 1);
            var sub = "";
            try { sub = decodeURIComponent(rest); } catch (e) { sub = ""; }
            return { id: id, sub: UI_SUB_ID.test(sub) ? sub : "" };
        }
    }
    return { id: "", sub: "" };
}

function uiPageFromLocation() {
    return uiParsePath(window.location.pathname).id;
}

// The open page's part of the address ("" when it has none)
function uiPageSub() {
    var at = uiParsePath(window.location.pathname);
    return at.id === uiPageCurrent ? at.sub : "";
}

// The address of a page, a part of it, a query and a fragment
function uiPageUrl(id, sub, query, hash) {
    var qs = query ? (typeof query === "string" ? query : query.toString()) : "";
    return uiPagePath(id) + (sub && UI_SUB_ID.test(sub) ? "/" + encodeURIComponent(sub) : "") + (qs ? "?" + qs : "") +
        (hash ? "#" + encodeURIComponent(hash) : "");
}

// The open page switched to another part (a Settings section): one Back step,
// or a correction of the address without one (replace)
function uiSetPageSub(sub, replace) {
    var url = uiPageUrl(uiPageCurrent, sub, "", "");
    if (url === window.location.pathname + window.location.search) return;
    if (replace || uiOverlayEntry) {
        history.replaceState({ page: uiPageCurrent, idx: uiHistIdx }, "", url);
    } else {
        uiSaveScroll();
        uiHistIdx += 1;
        history.pushState({ page: uiPageCurrent, idx: uiHistIdx }, "", url);
    }
}

function uiPageQuery() {
    try { return new URLSearchParams(window.location.search); } catch (e) { return new URLSearchParams(""); }
}

// The open page's filters live in its address; a change replaces the entry
function uiSetPageQuery(params) {
    var qs = params.toString();
    var url = window.location.pathname + (qs ? "?" + qs : "");
    if (url === window.location.pathname + window.location.search) return;
    history.replaceState(history.state, "", url);
}

// The browser tab names the page, and a level opened in it (the Usage of one
// token, a Settings section) when the page names one with uiSetSubTitle (NL-3)
var uiTitleSub = "";
function uiSetTitle(id) {
    if (!uiPages[id]) { document.title = t(uiAppTitle); return; }
    var withSub = uiTitleSub ? t("common.page-title-sub", { sub: uiTitleSub, page: t(uiPages[id].title), app: t(uiAppTitle) }) : "";
    document.title = withSub || t("common.page-title", { page: t(uiPages[id].title), app: t(uiAppTitle) });
}
function uiSetSubTitle(text) {
    var next = text ? String(text) : "";
    if (next === uiTitleSub) return;
    uiTitleSub = next;
    uiSetTitle(uiPageCurrent);
}

function uiMainScroller() {
    return document.getElementById("ui-main");
}

function uiSaveScroll() {
    var m = uiMainScroller();
    if (m) uiScrolls[uiHistIdx] = m.scrollTop;
}

// Back and Forward bring the page back where it was scrolled; a list that is
// still loading is waited for up to 5 seconds, and the user scrolling stops it
function uiRestoreScroll(idx) {
    var m = uiMainScroller();
    var want = uiScrolls[idx];
    if (!m || !want) { if (m) m.scrollTop = 0; return; }
    var until = Date.now() + 5000;
    var stopped = false;
    var stop = function () {
        stopped = true;
        ["wheel", "touchstart", "pointerdown", "keydown"].forEach(function (type) { window.removeEventListener(type, stop, true); });
    };
    ["wheel", "touchstart", "pointerdown", "keydown"].forEach(function (type) { window.addEventListener(type, stop, true); });
    var step = function () {
        if (stopped) return;
        var max = m.scrollHeight - m.clientHeight;
        var target = Math.min(want, max);
        if (Math.abs(m.scrollTop - target) > 1) m.scrollTop = target;
        if (Date.now() < until) setTimeout(step, max < want ? 150 : 250);
        else stop();
    };
    requestAnimationFrame(step);
}

// pages: { id: { path, title } }; show(id, info) draws a page
function uiRouterStart(pages, show) {
    uiPages = pages;
    uiPageShow = show;
    var id = uiPageFromLocation();
    var known = !!id;
    if (!known) id = Object.keys(pages)[0];
    if (history.state && typeof history.state.idx === "number") uiHistIdx = history.state.idx;
    var url = known ? window.location.pathname + window.location.search + window.location.hash : uiPagePath(id);
    history.replaceState({ page: id, idx: uiHistIdx }, "", url);
    if ("scrollRestoration" in history) history.scrollRestoration = "manual";
    window.addEventListener("popstate", uiOnPopState);
    window.addEventListener("beforeunload", function (event) {
        if (uiUnsavedCount()) {
            event.preventDefault();
            event.returnValue = "";
        }
    });
    uiRouterReady = true;
    uiShowPage(id, { initial: true });
}

function uiShowPage(id, info) {
    if (id !== uiPageCurrent) uiTitleSub = "";
    uiPageCurrent = id;
    uiSetTitle(id);
    if (uiPageShow) uiPageShow(id, info || {});
}

// Open a page from the app: one Back step when the address changes; the current
// page clicked again shows it again without a new entry (NL-4).
// opts.sub: a part of the page (a Settings section); opts.hash: a field in it.
// Resolves true when the page opened, false when the user stayed (unsaved changes).
function uiGo(id, query, opts) {
    var o = opts || {};
    var url = uiPageUrl(id, o.sub, query, "");
    var hash = o.hash ? "#" + encodeURIComponent(o.hash) : "";
    var here = window.location.pathname + window.location.search;
    var proceed = function () {
        if (id === uiPageCurrent && url === here && !o.force) {
            if (hash !== window.location.hash) history.replaceState(history.state, "", url + hash);
            uiShowPage(id, { again: true });
            return true;
        }
        uiSaveScroll();
        if (uiOverlayEntry || o.replace) {
            // Moving from inside a layer takes over its history entry
            uiOverlayEntry = false;
            history.replaceState({ page: id, idx: uiHistIdx }, "", url + hash);
        } else {
            uiHistIdx += 1;
            history.pushState({ page: id, idx: uiHistIdx }, "", url + hash);
        }
        uiScrolls[uiHistIdx] = 0;
        var m = uiMainScroller();
        if (m) m.scrollTop = 0;
        uiShowPage(id, { fresh: true });
        return true;
    };
    if (uiUnsavedCount() && !o.skipGuard) {
        return uiConfirmLeave().then(function (ok) { return ok ? proceed() : false; });
    }
    return Promise.resolve(proceed());
}

function uiOnPopState(event) {
    var st = event.state || {};
    var idx = typeof st.idx === "number" ? st.idx : null;
    var delta = idx === null ? null : idx - uiHistIdx;
    if (uiPopUndo) {
        uiPopUndo = false;
        if (idx !== null) uiHistIdx = idx;
        return;
    }
    if (uiIgnorePop) {
        uiIgnorePop = false;
        if (idx !== null) uiHistIdx = idx;
        return;
    }
    if (uiLeaveAsking && delta) {
        uiPopUndo = true;
        history.go(-delta);
        return;
    }
    if (uiOverlayEntry && (delta === -1 || delta === null)) {
        uiOverlayEntry = false;
        if (idx !== null) uiHistIdx = idx;
        var open = uiOpenLayers();
        if (open.length) uiLayerClose(open[open.length - 1]);
        setTimeout(uiSyncLayers, 0);
        return;
    }
    var target = st.page && uiPages[st.page] ? st.page : uiPageFromLocation() || Object.keys(uiPages)[0];
    // Moving between the sections of one page keeps its unsaved changes (SV-1)
    if (!uiPopConfirmed && delta && target !== uiPageCurrent && uiUnsavedCount()) {
        uiPopUndo = true;
        history.go(-delta);
        uiConfirmLeave().then(function (ok) {
            if (!ok) return;
            uiPopConfirmed = true;
            history.go(delta);
        });
        return;
    }
    uiPopConfirmed = false;
    uiSaveScroll();
    if (idx !== null) uiHistIdx = idx;
    // A step to another page closes whatever is open over this one
    uiOverlayEntry = false;
    for (var i = 0; i < 10; i++) {
        var layers = uiOpenLayers();
        if (!layers.length) break;
        uiLayerDrop(layers[layers.length - 1]);
    }
    uiShowPage(target, { fromHistory: true });
    uiRestoreScroll(uiHistIdx);
}

// ===========================================================================
// Page refresh (SV-10): every page registers how often it reads its data again.
// One scheduler runs the open page's refresher when due, never while the browser
// tab is hidden (one catch-up when shown), one run at a time, and a run that
// takes longer than 20 seconds frees the next one. Background refreshers (the
// Needs attention bell) run on every page.
// ===========================================================================

var PAGE_REFRESH_TICK = 5000;
var PAGE_REFRESH_TIMEOUT = 20000;
var uiPageRefreshers = {};
var uiBackgroundRefreshers = {};
var uiRefreshTimer = null;
var uiMutationGen = 0;

// run(isCurrent): isCurrent() turns false once this run timed out or a newer one
// started, and the run then publishes nothing
function registerPageRefresh(id, every, run) {
    uiPageRefreshers[id] = { every: every, run: run, last: 0, running: false, cycle: 0 };
}

function registerBackgroundRefresh(name, every, run) {
    uiBackgroundRefreshers[name] = { every: every, run: run, last: 0, running: false, cycle: 0 };
}

// A save, delete or toggle: answers to reads that started before it are dropped
function uiMutated() {
    uiMutationGen += 1;
    return uiMutationGen;
}
function uiMutationMark() {
    return uiMutationGen;
}
function uiStaleSince(mark) {
    return mark !== uiMutationGen;
}

// ===========================================================================
// Shared list data (WP3): tokens, zones and API keys. The pages and the global
// search read the same rows. Every create, rename, edit or delete marks its kind
// stale (uiDataStale), so the next reader reads it again; a database change
// (uiDataReset) marks every kind stale and starts a new data generation, so an
// answer that started before it is ignored when it arrives.
// ===========================================================================

var UI_DATA_KINDS = ["tokens", "zones", "apikeys"];
var uiDataState = {};
var uiDataGen = 0;

function uiDataEntry(kind) {
    if (!Object.prototype.hasOwnProperty.call(uiDataState, kind)) uiDataState[kind] = { version: 0, readAt: 0 };
    return uiDataState[kind];
}

// kind: "tokens" | "zones" | "apikeys"; called after every create, rename, edit or delete
function uiDataStale(kind) {
    var e = uiDataEntry(kind);
    e.version += 1;
    e.readAt = 0;
}

// Never read, marked stale, or read more than maxAge ms ago
function uiDataNeedsRead(kind, maxAge, now) {
    var e = uiDataEntry(kind);
    return !e.readAt || (now === undefined ? Date.now() : now) - e.readAt >= maxAge;
}

// Taken when a read starts; uiDataRead(kind, mark) when its answer was applied
function uiDataMark(kind) {
    return { kind: kind, version: uiDataEntry(kind).version, gen: uiDataGen };
}

// The answer of a read that started at mark is in: the kind counts as fresh only
// when nothing marked it stale and the database did not change meanwhile
function uiDataRead(kind, mark, now) {
    var e = uiDataEntry(kind);
    if (!mark || mark.version !== e.version || mark.gen !== uiDataGen) return false;
    e.readAt = now === undefined ? Date.now() : now;
    return true;
}

// True when the database changed after mark was taken
function uiDataOld(mark) {
    return !mark || mark.gen !== uiDataGen;
}

// The database may have changed (the ui:db-changed event): every kind is stale
// and answers to reads that started before are dropped
function uiDataReset() {
    uiDataGen += 1;
    UI_DATA_KINDS.forEach(uiDataStale);
    uiMutated();
    return uiDataGen;
}

// The page just loaded its data: count its next refresh from now
function uiRefreshLoaded(id) {
    if (uiPageRefreshers[id]) uiPageRefreshers[id].last = Date.now();
}

function uiRefreshRun(entry, force) {
    if (!entry || entry.running) return Promise.resolve(false);
    if (!force && Date.now() - entry.last < entry.every - 2000) return Promise.resolve(false);
    entry.running = true;
    var cycle = ++entry.cycle;
    var timedOut = false;
    var isCurrent = function () { return !timedOut && entry.cycle === cycle; };
    var timer = null;
    var limit = new Promise(function (resolve) {
        timer = setTimeout(function () { timedOut = true; resolve(); }, PAGE_REFRESH_TIMEOUT);
    });
    var work;
    try { work = Promise.resolve(entry.run(isCurrent)); } catch (e) { work = Promise.resolve(); }
    return Promise.race([work.catch(function () {}), limit]).then(function () {
        clearTimeout(timer);
        entry.running = false;
        entry.last = Date.now();
        return true;
    });
}

function uiRefreshTick(force) {
    if (uiHasDom() && document.hidden) return;
    Object.keys(uiBackgroundRefreshers).forEach(function (name) { uiRefreshRun(uiBackgroundRefreshers[name], force); });
    if (uiPageCurrent) uiRefreshRun(uiPageRefreshers[uiPageCurrent], force);
}

// Run a refresher at once (Retry, after a save)
function uiRefreshNow(id) {
    return uiRefreshRun(uiPageRefreshers[id] || uiBackgroundRefreshers[id], true);
}

function uiRefreshStart() {
    if (uiRefreshTimer) return;
    uiRefreshTimer = setInterval(function () { uiRefreshTick(false); }, PAGE_REFRESH_TICK);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) uiRefreshTick(false); });
}

// Is the user working in this area? Then a background refresh leaves it alone (SV-6)
function uiAreaHeld(el) {
    if (!el || !uiHasDom()) return false;
    if (uiOpenLayers().length) return true;
    if (uiPickerState.open) return true;
    try { if (document.querySelector(".ui-menu:popover-open")) return true; } catch (e) { /* no popover support */ }
    var a = document.activeElement;
    if (a && el.contains(a) && a.matches("input, select, textarea")) return true;
    if (el.querySelector("[aria-busy=true]")) return true;
    return uiTextHeld(el);
}

// Draw again and give the focus back to the same control in the same row (SV-13)
function uiKeepFocus(container, draw) {
    var a = document.activeElement;
    var row = a && container.contains(a) ? a.closest("[data-key]") : null;
    var key = row ? row.getAttribute("data-key") : null;
    var what = a && container.contains(a) ? (a.getAttribute("data-action") || a.getAttribute("data-sort") || a.getAttribute("aria-label") || a.textContent) : null;
    draw();
    if (!what) return;
    var scope = key ? container.querySelector('[data-key="' + (window.CSS && CSS.escape ? CSS.escape(key) : key) + '"]') : container;
    if (!scope) return;
    var found = Array.prototype.slice.call(scope.querySelectorAll("button, a[href], input, [tabindex]")).filter(function (el) {
        return (el.getAttribute("data-action") || el.getAttribute("data-sort") || el.getAttribute("aria-label") || el.textContent) === what;
    })[0];
    (found || (key ? scope : null) || { focus: function () {} }).focus({ preventScroll: true });
}

// ===========================================================================
// Picker (MP-1, MP-3, MP-4): a native select upgraded to a named drop-down.
// The select stays the single source of the value and gets a bubbling change.
// ===========================================================================

var uiPickerState = { open: null };

function uiPicker(select, opts) {
    var o = opts || {};
    var host = document.createElement("span");
    host.className = "ui-picker" + (o.field ? " is-field" : "");
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ui-picker-btn";
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-expanded", "false");
    if (select.id) btn.id = select.id + "-pick";
    host.appendChild(btn);
    select.classList.add("hidden");
    select.setAttribute("tabindex", "-1");
    select.setAttribute("aria-hidden", "true");
    select.parentNode.insertBefore(host, select.nextSibling);
    var picker = { select: select, host: host, button: btn, label: o.label || "", panel: null, field: !!o.field };
    select.uiPicker = picker;
    btn.addEventListener("click", function () {
        if (uiPickerState.open === picker) uiPickerClose(true);
        else uiPickerOpen(picker);
    });
    btn.addEventListener("keydown", function (event) {
        if ((event.key === "ArrowDown" || event.key === "ArrowUp") && uiPickerState.open !== picker) {
            event.preventDefault();
            uiPickerOpen(picker);
        }
    });
    uiPickerSync(select);
    return picker;
}

// The button names the filter and its value; a value other than the default fills it (MP-3)
function uiPickerSync(select) {
    var picker = select.uiPicker;
    if (!picker) return;
    var opt = select.options[select.selectedIndex] || select.options[0];
    var text = opt ? opt.textContent : "";
    var def = select.getAttribute("data-default");
    var isDefault = def !== null ? select.value === def : select.selectedIndex <= 0;
    picker.button.innerHTML = "<span>" + (picker.label && !picker.field ? '<span class="ui-picker-name">' + esc(picker.label) + ":</span> " : "") +
        esc(text) + '</span><i class="ui-picker-arrow" aria-hidden="true"></i>';
    picker.button.setAttribute("aria-label", (picker.label ? picker.label + ": " : "") + text);
    picker.button.classList.toggle("is-set", !picker.field && !isDefault);
    picker.button.disabled = select.disabled;
}

function uiPickerOpen(picker) {
    if (uiPickerState.open) uiPickerClose(false);
    var select = picker.select;
    var opts = Array.prototype.slice.call(select.options);
    var withSearch = opts.length > 10;
    var panel = document.createElement("div");
    panel.className = "ui-popover ui-picker-panel";
    panel.id = (select.id || "ui-picker") + "-panel";
    var listId = panel.id + "-list";
    panel.innerHTML = (withSearch ? '<input type="search" class="ui-input ui-picker-search" autocomplete="off" spellcheck="false" aria-autocomplete="list" aria-controls="' +
        listId + '" aria-label="' + esc(t("picker.search", { name: picker.label.toLowerCase() })) + '" placeholder="' + esc(t("picker.search-2")) + '">' : "") +
        '<div class="ui-picker-list" role="listbox" id="' + listId + '" aria-label="' + esc(picker.label) + '">' +
        opts.map(function (opt, i) {
            var on = i === select.selectedIndex;
            var dot = opt.getAttribute("data-dot");
            var hint = opt.getAttribute("data-hint");
            return '<button type="button" class="ui-option" role="option" tabindex="-1" data-index="' + i + '" aria-selected="' + on + '">' +
                (dot !== null ? '<i class="ui-option-dot' + (dot ? " ui-dot-" + esc(dot) : "") + '" aria-hidden="true"></i>' : "") +
                "<span>" + esc(opt.textContent) + "</span>" + (hint ? '<small class="ui-num">' + esc(hint) + "</small>" : "") + "</button>";
        }).join("") + "</div>" +
        (withSearch ? '<p class="ui-picker-none hidden" role="status">' + esc(t("picker.no-matches")) + '</p>' : "");
    picker.host.appendChild(panel);
    picker.panel = panel;
    picker.button.setAttribute("aria-expanded", "true");
    picker.button.setAttribute("aria-controls", panel.id);
    uiPickerState.open = picker;
    uiPickerPlace(picker);

    var list = panel.querySelector(".ui-picker-list");
    var visible = function () {
        return Array.prototype.slice.call(list.querySelectorAll(".ui-option")).filter(function (b) { return !b.hidden; });
    };
    list.addEventListener("click", function (event) {
        var b = event.target.closest(".ui-option");
        if (!b) return;
        uiPickerChoose(picker, Number(b.getAttribute("data-index")));
    });
    panel.addEventListener("keydown", function (event) {
        var items = visible();
        var at = items.indexOf(document.activeElement);
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            if (at === -1) { if (event.key === "ArrowDown" && items[0]) items[0].focus(); return; }
            if (event.key === "ArrowUp" && at === 0 && search) { search.focus(); return; }
            var next = items[Math.max(0, Math.min(items.length - 1, at + (event.key === "ArrowDown" ? 1 : -1)))];
            if (next) next.focus();
        } else if (event.key === "Home" && at !== -1) {
            event.preventDefault();
            items[0].focus();
        } else if (event.key === "End" && at !== -1) {
            event.preventDefault();
            items[items.length - 1].focus();
        } else if (event.key === "Enter" && document.activeElement === search) {
            event.preventDefault();
            if (items[0]) uiPickerChoose(picker, Number(items[0].getAttribute("data-index")));
        } else if (event.key === "Tab") {
            uiPickerClose(false);
        }
    });
    var search = panel.querySelector(".ui-picker-search");
    if (search) {
        search.addEventListener("input", function () {
            var q = search.value.trim().toLowerCase().replace(/_/g, " ");
            var any = false;
            list.querySelectorAll(".ui-option").forEach(function (b) {
                var hit = !q || b.textContent.toLowerCase().replace(/_/g, " ").indexOf(q) !== -1;
                b.hidden = !hit;
                if (hit) any = true;
            });
            panel.querySelector(".ui-picker-none").classList.toggle("hidden", any);
        });
    }
    var chosen = list.querySelector('[aria-selected="true"]') || list.querySelector(".ui-option");
    if (search && !uiTouchOnly()) search.focus({ preventScroll: true });
    else if (chosen) {
        chosen.focus({ preventScroll: true });
        if (chosen.scrollIntoView) chosen.scrollIntoView({ block: "nearest" });
    }
}

// The room a floating panel may use (MP-5): the screen below a shown top bar
// (.ui-mtop) and above a shown tab bar (.ui-tabbar), cut down by every box around
// the element whose overflow is not visible, 8px in from each edge
function uiFloatRoom(el) {
    var room = { top: 8, bottom: window.innerHeight - 8, left: 8, right: window.innerWidth - 8 };
    var bar = document.querySelector(".ui-mtop");
    if (bar && bar.getClientRects().length) room.top = Math.max(room.top, bar.getBoundingClientRect().bottom + 8);
    var tabs = document.querySelector(".ui-tabbar");
    if (tabs && tabs.getClientRects().length) room.bottom = Math.min(room.bottom, tabs.getBoundingClientRect().top - 8);
    for (var box = el ? el.parentElement : null; box && box !== document.body; box = box.parentElement) {
        var cs = getComputedStyle(box);
        if (cs.overflowX === "visible" && cs.overflowY === "visible") continue;
        var r = box.getBoundingClientRect();
        // The visible inside of the box, without its scrollbars
        var top = r.top + (box.clientTop || 0), left = r.left + (box.clientLeft || 0);
        var bottom = box.clientHeight ? top + box.clientHeight : r.bottom;
        var right = box.clientWidth ? left + box.clientWidth : r.right;
        room.top = Math.max(room.top, top + 8);
        room.bottom = Math.min(room.bottom, bottom - 8);
        room.left = Math.max(room.left, left + 8);
        room.right = Math.min(room.right, right - 8);
    }
    return room;
}

// Under its button; above it only when there is more room there; scrolls inside
// when taller than its side; shifted back inside the edges (MP-5)
function uiPickerPlace(picker) {
    var panel = picker.panel;
    if (!panel) return;
    panel.style.maxHeight = "";
    panel.style.transform = "";
    panel.classList.remove("is-up");
    var room = uiFloatRoom(picker.host);
    var rect = picker.button.getBoundingClientRect();
    var below = room.bottom - rect.bottom - 6;
    var above = rect.top - room.top - 6;
    var up = panel.offsetHeight > below && above > below;
    panel.classList.toggle("is-up", up);
    // Never taller than the room on its side, so it cannot run under a bar
    panel.style.maxHeight = Math.max(0, up ? above : below) + "px";
    var pr = panel.getBoundingClientRect();
    var fits = function (d) { return pr.left + d >= room.left && pr.right + d <= room.right; };
    var dx = 0;
    // Past an edge: line the panel up with the button's other edge when that fits, else keep it 8px inside
    if (pr.right > room.right) dx = fits(rect.right - pr.right) ? rect.right - pr.right : room.right - pr.right;
    else if (pr.left < room.left) dx = fits(rect.left - pr.left) ? rect.left - pr.left : room.left - pr.left;
    if (pr.left + dx < room.left) dx = room.left - pr.left;
    if (dx) panel.style.transform = "translateX(" + Math.round(dx) + "px)";
}

function uiPickerChoose(picker, index) {
    var select = picker.select;
    var changed = select.selectedIndex !== index;
    select.selectedIndex = index;
    uiPickerClose(true);
    uiPickerSync(select);
    if (changed) select.dispatchEvent(new Event("change", { bubbles: true }));
}

function uiPickerClose(returnFocus) {
    var picker = uiPickerState.open;
    if (!picker) return;
    uiPickerState.open = null;
    if (picker.panel) picker.panel.remove();
    picker.panel = null;
    picker.button.setAttribute("aria-expanded", "false");
    picker.button.removeAttribute("aria-controls");
    if (returnFocus) requestAnimationFrame(function () { if (picker.button.isConnected) picker.button.focus({ preventScroll: true }); });
}

function uiPickerOutside(event) {
    var picker = uiPickerState.open;
    if (picker && !picker.host.contains(event.target)) uiPickerClose(false);
}

// ===========================================================================
// Row menu (MP-7) on the native popover layer, placed under its button,
// closed by a choice, a click outside, Escape or a scroll
// ===========================================================================

var uiMenuOpen = null;

// items: [{ label, hint, danger, disabled, reason, action, run }]
function uiMenu(anchor, items, label) {
    if (uiMenuOpen && uiMenuOpen.anchor === anchor) { uiMenuDismiss(); return null; }
    uiMenuDismiss();
    var menu = document.createElement("div");
    menu.className = "ui-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", label || t("common.actions"));
    menu.id = "ui-menu-" + (++uiDialogCount);
    var supported = typeof menu.showPopover === "function";
    if (supported) menu.setAttribute("popover", "auto");
    menu.innerHTML = items.map(function (item, i) {
        return '<button type="button" role="menuitem" tabindex="-1" class="ui-menu-item' + (item.danger ? " is-danger" : "") + '" data-index="' + i + '"' +
            (item.action ? ' data-action="' + esc(item.action) + '"' : "") + (item.disabled ? ' aria-disabled="true"' : "") + ">" +
            "<span>" + esc(item.label) + "</span>" + (item.disabled && item.reason ? "<small>" + esc(item.reason) + "</small>" : item.hint ? "<small>" + esc(item.hint) + "</small>" : "") +
            "</button>";
    }).join("");
    document.body.appendChild(menu);
    var state = { menu: menu, anchor: anchor, supported: supported };
    uiMenuOpen = state;
    anchor.setAttribute("aria-expanded", "true");
    anchor.setAttribute("aria-controls", menu.id);
    menu.addEventListener("click", function (event) {
        var b = event.target.closest(".ui-menu-item");
        if (!b || b.getAttribute("aria-disabled") === "true") return;
        var item = items[Number(b.getAttribute("data-index"))];
        uiMenuDismiss(false);
        if (item && typeof item.run === "function") item.run();
    });
    menu.addEventListener("keydown", function (event) {
        var list = Array.prototype.slice.call(menu.querySelectorAll(".ui-menu-item"));
        var at = list.indexOf(document.activeElement);
        var to = -1;
        if (event.key === "ArrowDown") to = at < list.length - 1 ? at + 1 : 0;
        else if (event.key === "ArrowUp") to = at > 0 ? at - 1 : list.length - 1;
        else if (event.key === "Home") to = 0;
        else if (event.key === "End") to = list.length - 1;
        else if (event.key === "Escape" && !supported) { event.preventDefault(); uiMenuDismiss(true); return; }
        else if (event.key === "Tab") { uiMenuDismiss(false); return; }
        if (to !== -1) { event.preventDefault(); list[to].focus(); }
    });
    if (supported) {
        menu.addEventListener("toggle", function (event) {
            if (event.newState === "closed" && uiMenuOpen === state) uiMenuDismiss(menu.contains(document.activeElement) || document.activeElement === document.body);
        });
        menu.showPopover();
    }
    uiMenuPlace(menu, anchor);
    var first = menu.querySelector(".ui-menu-item");
    if (first) first.focus({ preventScroll: true });
    return menu;
}

// Under the button, aligned with its end edge; above when there is no room; 8px inside the screen
function uiMenuPlace(menu, anchor) {
    var room = uiFloatRoom(null);
    var r = anchor.getBoundingClientRect();
    menu.style.maxHeight = "";
    var w = menu.offsetWidth;
    var h = menu.offsetHeight;
    var below = room.bottom - (r.bottom + 4);
    var above = (r.top - 4) - room.top;
    var up = h > below && above > below;
    // Never taller than the room on its side, so it cannot run under a bar
    var fit = Math.max(0, up ? above : below);
    if (h > fit) {
        menu.style.maxHeight = fit + "px";
        h = fit;
    }
    var rtl = getComputedStyle(anchor).direction === "rtl";
    var left = rtl ? r.left : r.right - w;
    left = Math.max(room.left, Math.min(left, room.right - w));
    menu.style.left = left + "px";
    menu.style.top = (up ? r.top - 4 - h : r.bottom + 4) + "px";
}

function uiMenuDismiss(returnFocus) {
    var state = uiMenuOpen;
    if (!state) return;
    uiMenuOpen = null;
    // A list drawn again while its menu was open has a new button with the same id
    var anchor = state.anchor.isConnected || !state.anchor.id ? state.anchor : document.getElementById(state.anchor.id) || state.anchor;
    anchor.setAttribute("aria-expanded", "false");
    anchor.removeAttribute("aria-controls");
    try { if (state.supported && state.menu.matches(":popover-open")) state.menu.hidePopover(); } catch (e) { /* closed already */ }
    state.menu.remove();
    if (returnFocus && anchor.isConnected) anchor.focus({ preventScroll: true });
}

var uiPickerScrollQueued = false;
function uiPickerFollow() {
    uiPickerScrollQueued = false;
    var picker = uiPickerState.open;
    if (!picker) return;
    var room = uiFloatRoom(picker.host);
    var r = picker.button.getBoundingClientRect();
    if (r.bottom < room.top || r.top > room.bottom || r.right < room.left || r.left > room.right) {
        uiPickerClose(false);
        return;
    }
    uiPickerPlace(picker);
}
function uiPickerOnScroll(event) {
    var picker = uiPickerState.open;
    if (!picker || uiPickerScrollQueued) return;
    var target = event.target;
    // The panel's own list scrolling moves nothing
    if (target instanceof Node && picker.panel && picker.panel.contains(target)) return;
    if (target instanceof Node && target !== document && !target.contains(picker.host)) return;
    uiPickerScrollQueued = true;
    requestAnimationFrame(uiPickerFollow);
}

function uiMenuOnScroll(event) {
    if (uiMenuOpen && !(event.target instanceof Node && uiMenuOpen.menu.contains(event.target))) uiMenuDismiss(false);
}

function uiMenuOutside(event) {
    if (!uiMenuOpen || uiMenuOpen.supported) return;
    if (!uiMenuOpen.menu.contains(event.target) && !uiMenuOpen.anchor.contains(event.target)) uiMenuDismiss(false);
}

// ===========================================================================
// Sorting a list in the page (TB-1, TB-3, TB-28): header buttons and one Sort
// button share one state; it lives in memory and survives refreshes
// ===========================================================================

// cols: [{ key, label, kind: "text" | "number" | "date" }]
// def: the column and order a list arrives in ({ key, dir }), for a list that has
// one; state.key "" is then that order, and a sort that lands on it puts the
// arrival order back exactly
function uiSortToggle(state, col, def) {
    var cur = uiSortEffective(state, def);
    if (cur.key === col.key) {
        state.key = col.key;
        state.dir = cur.dir === "asc" ? "desc" : "asc";
    } else {
        state.key = col.key;
        state.dir = col.kind === "text" ? "asc" : "desc";
    }
    return uiSortReset(state, def);
}

// The order the list shows: the one picked, else the list's own default
function uiSortEffective(state, def) {
    return state.key ? state : def && def.key ? def : state;
}

function uiSortReset(state, def) {
    if (def && state.key === def.key && state.dir === def.dir) {
        state.key = "";
        state.dir = "";
    }
    return state;
}

// value(row, key) gives a number, a string or null; empty values stay last
function uiSortRows(rows, state, value) {
    if (!state.key) return rows.slice();
    var dir = state.dir === "asc" ? 1 : -1;
    return rows.map(function (row, i) { return { row: row, i: i, v: value(row, state.key) }; })
        .sort(function (a, b) {
            var x = a.v;
            var y = b.v;
            var xe = x === null || x === undefined || x === "" || (typeof x === "number" && isNaN(x));
            var ye = y === null || y === undefined || y === "" || (typeof y === "number" && isNaN(y));
            if (xe || ye) return xe && ye ? a.i - b.i : xe ? 1 : -1;
            var c = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), undefined, { sensitivity: "base" });
            return c ? c * dir : a.i - b.i;
        })
        .map(function (e) { return e.row; });
}

function uiSortAria(state, key, def) {
    var cur = uiSortEffective(state, def);
    return cur.key === key ? (cur.dir === "asc" ? "ascending" : "descending") : "none";
}

function uiSortWords(kind, dir) {
    if (kind === "text") return dir === "asc" ? t("sort.a-to-z") : t("sort.z-to-a");
    if (kind === "date") return dir === "asc" ? t("sort.oldest-first") : t("sort.newest-first");
    if (kind === "number") return dir === "asc" ? t("sort.low-to-high") : t("sort.high-to-low");
    return dir === "asc" ? t("sort.ascending") : t("sort.descending");
}

// The Sort button's panel: Sort by and Order, applied at once (TB-28). A list
// without a default column and order (def) leads Sort by with Default order,
// which brings back the order it arrives in; a list with one shows that column
// and order while nothing else is picked.
function uiSortMenu(anchor, name, cols, state, onChange, def) {
    if (uiMenuOpen && (uiMenuOpen.anchor === anchor || (anchor.id && uiMenuOpen.anchor.id === anchor.id))) { uiMenuDismiss(true); return; }
    uiMenuDismiss(false);
    var panel = document.createElement("div");
    panel.className = "ui-menu ui-sort-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", t("sort.sort", { name: name }));
    panel.id = "ui-sort-" + (++uiDialogCount);
    var supported = typeof panel.showPopover === "function";
    if (supported) panel.setAttribute("popover", "auto");
    var hasDefault = !!(def && def.key);
    var draw = function () {
        var cur = uiSortEffective(state, def);
        var col = cols.filter(function (c) { return c.key === cur.key; })[0];
        var radio = function (group, value, checked, label) {
            return '<label><input type="radio" class="ui-check" name="' + panel.id + "-" + group + '" value="' + esc(value) + '"' + (checked ? " checked" : "") + "> " + esc(label) + "</label>";
        };
        panel.innerHTML = '<fieldset><legend>' + esc(t("sort.sort-by")) + '</legend>' +
            (hasDefault ? "" : radio("by", "", !cur.key, t("sort.default-order"))) +
            cols.map(function (c) { return radio("by", c.key, cur.key === c.key, t(c.label)); }).join("") + "</fieldset>" +
            '<fieldset' + (col ? "" : " disabled") + "><legend>" + esc(t("sort.order")) + "</legend>" +
            ["asc", "desc"].map(function (d) { return radio("dir", d, !!col && cur.dir === d, uiSortWords(col ? col.kind : "", d)); }).join("") + "</fieldset>";
    };
    draw();
    document.body.appendChild(panel);
    var st = { menu: panel, anchor: anchor, supported: supported };
    uiMenuOpen = st;
    anchor.setAttribute("aria-expanded", "true");
    panel.addEventListener("change", function (event) {
        var input = event.target;
        var byGroup = input.name === panel.id + "-by";
        if (byGroup) {
            var col = cols.filter(function (c) { return c.key === input.value; })[0];
            // A column picked starts in its own first order; Default order is no column
            state.key = col ? col.key : "";
            state.dir = col ? (col.kind === "text" ? "asc" : "desc") : "";
        } else {
            var cur = uiSortEffective(state, def);
            state.key = cur.key;
            state.dir = input.value;
        }
        uiSortReset(state, def);
        var after = uiSortEffective(state, def);
        var focusName = input.name;
        var focusValue = byGroup ? after.key : after.dir;
        draw();
        var again = panel.querySelector('input[name="' + focusName + '"][value="' + focusValue + '"]');
        if (again) again.focus({ preventScroll: true });
        onChange();
    });
    if (supported) {
        panel.addEventListener("toggle", function (event) { if (event.newState === "closed" && uiMenuOpen === st) uiMenuDismiss(true); });
        panel.showPopover();
    }
    uiMenuPlace(panel, anchor);
    var checked = panel.querySelector("input:checked") || panel.querySelector("input");
    if (checked) checked.focus({ preventScroll: true });
}

// ===========================================================================
// Passkeys (WebAuthn): what this browser can do, its errors in words, and the
// name a new passkey gets. The pages use only the browser's own JSON helpers
// (parseRequestOptionsFromJSON, parseCreationOptionsFromJSON, toJSON()).
// ===========================================================================

var UI_PASSKEY_INSECURE = "passkey.passkeys-need-https-or-localhost";
var UI_PASSKEY_OLD_BROWSER = "passkey.this-browser-cannot-use-passkeys-here";
var UI_PASSKEY_NAME_MAX = 64;

// Why this page cannot use passkeys: "" when it can, "insecure" outside a secure
// context, "browser" without WebAuthn or its JSON helpers. create: also adding one.
// win is the window, or a stand-in in the unit tests.
function uiPasskeyBlocked(win, create) {
    if (!win || !win.isSecureContext) return "insecure";
    var pkc = win.PublicKeyCredential;
    if (!pkc || typeof pkc.parseRequestOptionsFromJSON !== "function") return "browser";
    if (!pkc.prototype || typeof pkc.prototype.toJSON !== "function") return "browser";
    if (create && typeof pkc.parseCreationOptionsFromJSON !== "function") return "browser";
    if (!win.navigator || !win.navigator.credentials) return "browser";
    return "";
}

// The words for a browser passkey call that failed; "" when the person closed the
// browser's window or it timed out, which needs no message. create: adding one.
function uiPasskeyErrorText(err, create) {
    var name = err && err.name;
    if (name === "NotAllowedError" || name === "AbortError") return "";
    if (name === "InvalidStateError" && create) return t("passkey.this-device-already-has-a-passkey");
    if (name === "SecurityError") return t("passkey.the-browser-refused-a-passkey-for");
    if (name === "NotSupportedError") {
        return create ? t("passkey.this-device-cannot-create-this-kind")
            : t("passkey.this-device-cannot-use-this-kind");
    }
    return create ? t("passkey.the-passkey-could-not-be-created") : t("passkey.the-passkey-could-not-be-used");
}

// A passkey name as the server keeps it: runs of spaces become one, ends trimmed
function uiPasskeyName(text) {
    return String(text === null || text === undefined ? "" : text).split(/\s+/).filter(Boolean).join(" ");
}

// What is wrong with a name, or ""
function uiPasskeyNameProblem(text) {
    var name = uiPasskeyName(text);
    if (!name) return t("passkey.give-the-passkey-a-name");
    if (Array.from(name).length > UI_PASSKEY_NAME_MAX) return t("passkey.the-name-can-have-at-most", { max: UI_PASSKEY_NAME_MAX });
    return "";
}

// The name proposed for a new passkey: "Passkey on <system>" when the browser says which
function uiPasskeyDefaultName(nav) {
    var platform = String((nav && nav.userAgentData && nav.userAgentData.platform) || "");
    var ua = String((nav && nav.userAgent) || "");
    var hint = platform === "macOS" ? t("passkey.mac") : platform;
    if (!hint) {
        if (/iPhone/.test(ua)) hint = t("passkey.iphone");
        else if (/iPad/.test(ua)) hint = t("passkey.ipad");
        else if (/Android/.test(ua)) hint = t("passkey.android");
        else if (/CrOS/.test(ua)) hint = t("passkey.chromeos");
        else if (/Macintosh|Mac OS X/.test(ua)) hint = t("passkey.mac");
        else if (/Windows/.test(ua)) hint = t("passkey.windows");
        else if (/Linux/.test(ua)) hint = t("passkey.linux");
    }
    return hint ? uiPasskeyName(t("passkey.passkey-on", { device: hint })) : t("passkey.passkey");
}

// ===========================================================================
// The PostgreSQL connection fields of setup, the recovery page and Settings.
// The password is never filled in: the server never sends it back.
// ===========================================================================

var UI_DB_SSLMODES = [
    ["prefer", "db.prefer-tls-if-offered-default"],
    ["disable", "db.disable-no-tls"],
    ["require", "db.require-always-tls"],
    ["verify-full", "db.verify-full-tls-and-certificate-check"],
];
var UI_DB_SSL_HINT = {
    prefer: "db.encrypted-when-the-server-supports-tls",
    disable: "db.not-encrypted-use-it-only-on",
    require: "db.always-encrypted-but-the-server-s",
    "verify-full": "db.always-encrypted-and-the-server-must",
};
// Short texts for the server's fixed codes, when it sends no text of its own
var UI_DB_CODES = {
    unreachable: "db.the-database-server-cannot-be-reached",
    timeout: "db.the-database-server-did-not-answer",
    tls_failed: "db.the-tls-connection-failed-if-the",
    auth_failed: "db.the-server-refused-the-user-name",
    database_missing: "db.this-database-does-not-exist-on",
    permission_denied: "db.the-database-user-lacks-a-permission",
    read_only: "db.this-server-is-a-standby-or",
    other: "db.the-connection-failed",
};

function uiDbCodeText(code, detail) {
    return detail || t(UI_DB_CODES[code] || UI_DB_CODES.other);
}

// p: id prefix; v: { host, port, database, user, sslmode, caSaved }; keepPassword: a saved password exists;
// onDemand (Settings): a saved password and CA certificate read Stored, and their field
// appears only on Replace (FS-3); the CA certificate can also be removed
function uiDbFieldsHtml(p, v, keepPassword, onDemand) {
    v = v || {};
    var mode = v.sslmode || "prefer";
    var opts = UI_DB_SSLMODES.map(function (m) {
        return '<option value="' + m[0] + '"' + (m[0] === mode ? " selected" : "") + ">" + esc(t(m[1])) + "</option>";
    }).join("");
    var input = function (id, label, value, extra) {
        return '<div class="ui-field"><label class="ui-label" for="' + p + id + '">' + esc(label) + "</label>" +
            '<input class="ui-input" id="' + p + id + '" value="' + esc(value == null ? "" : String(value)) + '" autocomplete="off" spellcheck="false" autocapitalize="off" dir="ltr"' +
            (extra || "") + " /></div>";
    };
    return '<div class="ui-form-row">' + input("host", t("db.host"), v.host, ' placeholder="' + esc(t("db.postgres")) + '"') +
        input("port", t("db.port"), v.port || 5432, ' inputmode="numeric" maxlength="5"') + "</div>" +
        '<div class="ui-form-row">' + input("database", t("db.database"), v.database, ' placeholder="' + esc(t("db.authdb")) + '"') +
        input("user", t("db.user"), v.user, ' placeholder="' + esc(t("db.auth")) + '"') + "</div>" +
        '<div class="ui-field"><label class="ui-label" for="' + p + 'password">' + esc(t("db.password")) + '</label>' +
        (onDemand && keepPassword ? uiDbStoredHtml(p, "password", false) : "") +
        '<input class="ui-input' + (onDemand && keepPassword ? " hidden" : "") + '" type="password" id="' + p + 'password" autocomplete="new-password" dir="ltr"' +
        (keepPassword ? ' placeholder="' + esc(onDemand ? t("settings.secret.new-value") : t("db.empty-keeps-the-saved-password")) + '"' : "") + " /></div>" +
        '<div class="ui-field"><label class="ui-label" for="' + p + 'sslmode">' + esc(t("db.tls")) + '</label>' +
        '<select class="ui-select" id="' + p + 'sslmode" aria-describedby="' + p + 'ssl-hint">' + opts + "</select>" +
        '<span class="ui-hint" id="' + p + 'ssl-hint">' + esc(UI_DB_SSL_HINT[mode] ? t(UI_DB_SSL_HINT[mode]) : "") + "</span></div>" +
        '<div class="ui-field' + (mode === "verify-full" ? "" : " hidden") + '" id="' + p + 'ca-field"><label class="ui-label" for="' + p + 'ca">' + esc(t("db.ca-certificate-pem-optional")) + '</label>' +
        (onDemand && v.caSaved ? uiDbStoredHtml(p, "ca", true) : "") +
        '<textarea class="ui-textarea' + (onDemand && v.caSaved ? " hidden" : "") + '" id="' + p + 'ca" spellcheck="false" dir="ltr" placeholder="' + esc(t("db.begin-certificate")) + '"></textarea>' +
        '<span class="ui-hint">' + esc(v.caSaved ? t("db.ca-hint-saved") : t("db.only-when-the-server-s-certificate")) + "</span></div>";
}

// A stored secret: Stored, Replace, and for the CA certificate Remove (FS-3)
function uiDbStoredHtml(p, which, removable) {
    return '<div class="ui-set-secret" id="' + p + which + '-stored"><span id="' + p + which + '-state">' + esc(t("settings.secret.stored")) + "</span>" +
        '<button type="button" class="ui-btn ui-btn-sm" data-db-secret="' + which + '">' + esc(t("settings.secret.replace")) + "</button>" +
        (removable ? '<button type="button" class="ui-btn ui-btn-sm" id="' + p + which + '-remove" data-db-secret="' + which + '-remove">' + esc(t("settings.secret.remove")) + "</button>" : "") +
        "</div>";
}

// Stored or replaced: which of a secret's states shows
function uiDbSecretsSync(p, replacing, caRemove) {
    var pairs = [["password", !!replacing.password], ["ca", !!replacing.ca]];
    pairs.forEach(function (pair) {
        var stored = document.getElementById(p + pair[0] + "-stored");
        var field = document.getElementById(p + pair[0]);
        if (!stored || !field) return;
        stored.classList.toggle("hidden", pair[1]);
        field.classList.toggle("hidden", !pair[1]);
    });
    var state = document.getElementById(p + "ca-state");
    var remove = document.getElementById(p + "ca-remove");
    if (state && remove) {
        var text = caRemove ? t("settings.secret.removed-when-saved") : t("settings.secret.stored");
        if (state.textContent !== text) state.textContent = text;
        state.classList.toggle("is-removing", !!caRemove);
        var label = caRemove ? t("settings.secret.keep") : t("settings.secret.remove");
        if (remove.textContent !== label) remove.textContent = label;
    }
}

// The TLS hint and the CA field follow the chosen mode
function uiDbFieldsSync(p) {
    var sel = document.getElementById(p + "sslmode");
    if (!sel) return;
    document.getElementById(p + "ssl-hint").textContent = UI_DB_SSL_HINT[sel.value] ? t(UI_DB_SSL_HINT[sel.value]) : "";
    document.getElementById(p + "ca-field").classList.toggle("hidden", sel.value !== "verify-full");
}

// The fields as the API takes them (postgres object)
function uiDbFieldsRead(p) {
    var val = function (id) { var el = document.getElementById(p + id); return el ? el.value : ""; };
    var port = parseInt(val("port"), 10);
    var body = {
        host: val("host").trim(),
        port: isNaN(port) ? 0 : port,
        database: val("database").trim(),
        user: val("user").trim(),
        sslmode: val("sslmode") || "prefer",
    };
    var password = val("password");
    if (password) body.password = password;
    var ca = val("ca").trim();
    if (body.sslmode === "verify-full") {
        if (ca) body.caPem = ca;
        else body.keepCa = true;
    }
    return body;
}

// Mark the field the server named
function uiDbFieldError(p, field) {
    ["host", "port", "database", "user", "password", "sslmode", "ca"].forEach(function (id) {
        var el = document.getElementById(p + id);
        if (el) el.removeAttribute("aria-invalid");
    });
    var id = field === "caPem" ? "ca" : field;
    var el = id && document.getElementById(p + id);
    if (el) {
        el.setAttribute("aria-invalid", "true");
        el.focus();
    }
}

// What a check found, as a note: tone "" | " is-warn" | " is-fail", title, text; a note that
// refuses the connection names its case in kind (failed, user, foreign, incomplete, pending, another)
function uiDbCheckNote(r, where) {
    if (!r) return null;
    if (!r.ok) return { kind: "failed", tone: " is-fail", title: t("db.the-connection-failed-2"), text: uiDbCodeText(r.code, r.detail) };
    if (r.problems && r.problems.length) {
        return { kind: "user", tone: " is-fail", title: t("db.the-database-user-cannot-be-used"), text: r.problems.join(" ") };
    }
    if (r.state === "new") return { tone: "", title: t("db.a-new-database-file"), text: t("db.the-service-creates-data-auth-db") };
    if (r.state === "empty") return { tone: "", title: t("db.the-database-is-empty"), text: t("db.the-service-creates-its-tables-in") };
    if (r.state === "foreign") {
        return { kind: "foreign", tone: " is-fail", title: t("db.this-database-holds-other-data"), text: r.others && r.others.length
            ? t("db.it-has-tables-that-are-not-named", { tables: r.others.join(", ") }) : t("db.it-has-tables-that-are-not") };
    }
    if (r.state === "incomplete") {
        return { kind: "incomplete", tone: " is-fail", title: t("db.tables-are-missing"), text: t("db.this-database-holds-this-service-s", { tables: (r.others || []).join(", ") }) };
    }
    if (r.state === "older") {
        return { tone: " is-warn", title: t("db.this-service-s-data-from-an"),
            text: where === "setup" ? t("db.the-service-can-adopt-it-it") : t("db.the-service-can-adopt-it-copy") };
    }
    if (r.pending) return { kind: "pending", tone: " is-fail", title: t("db.an-unfinished-copy"), text: t("db.this-database-holds-a-copy-that") };
    if (where === "settings") {
        if (r.identity === "same") return { tone: "", title: t("db.the-same-database"), text: t("db.the-new-connection-reaches-the-database") };
        if (r.identity === "different_copy") {
            return { tone: " is-warn", title: t("db.a-different-copy-of-this-database"), text: t("db.for-example-a-restored-backup-changes") };
        }
        return { kind: "another", tone: " is-fail", title: t("db.another-installation"), text: t("db.this-database-holds-another-installation-of") };
    }
    if (where === "setup") {
        return { tone: "", title: t("db.an-existing-installation-of-this-service"), text: r.hasAdmin
            ? t("db.the-service-uses-it-as-it")
            : t("db.the-service-uses-it-as-it-2") };
    }
    return { tone: "", title: t("db.an-installation-of-this-service"), text: t("db.the-service-can-use-this-database") };
}

function uiDbNoteHtml(n) {
    if (!n) return "";
    return '<div class="ui-alert' + n.tone + '"><span class="ui-alert-bar" aria-hidden="true"></span><div><p>' + esc(n.title) + "</p><small>" +
        esc(n.text) + "</small></div><span></span></div>";
}

// ===========================================================================
// Shell (WP3): the top bar on wide screens (NL-6), the sidebar folded into the
// rail (NL-7), the information dialog with its reading gauge (DS-5), showing
// where an opened search result landed (SR-6) and the window pan the phone
// keyboard leaves (MO-1)
// ===========================================================================

var uiTopbarMoves = [];
var uiTopbarPage = "";

function uiTopbarMove(el, target) {
    if (!el || !target || !el.parentNode) return;
    var mark = document.createComment("topbar");
    el.parentNode.replaceChild(mark, el);
    target.appendChild(el);
    uiTopbarMoves.push([el, mark]);
}

function uiTopbarRestore() {
    uiTopbarMoves.reverse().forEach(function (pair) {
        if (pair[1].parentNode) pair[1].parentNode.replaceChild(pair[0], pair[1]);
    });
    uiTopbarMoves = [];
}

// Wide screens: the open page's title (with its help button) and its own action
// buttons move into the top bar while the page is open. They are the page's own
// elements, moved and put back when the page is left or the width crosses 760px,
// so every id, button and listener stays as it was. Phones keep the page head.
function uiPlaceTopbar(id) {
    if (id !== undefined) uiTopbarPage = id;
    if (!uiHasDom() || !document.getElementById("ui-topbar")) return;
    uiTopbarRestore();
    var on = !uiIsPhone();
    document.documentElement.classList.toggle("ui-has-topbar", on);
    if (!on || !uiTopbarPage) return;
    var page = document.querySelector('.ui-page[data-page="' + uiTopbarPage + '"]');
    var head = page ? page.querySelector(".ui-page-head") : null;
    var title = head ? head.querySelector(".ui-h1") : null;
    if (!title) return;
    uiTopbarMove(title, document.getElementById("ui-topbar-title"));
    Array.prototype.slice.call(head.children).forEach(function (el) {
        if (el.classList.contains("ui-page-actions")) uiTopbarMove(el, document.getElementById("ui-topbar-actions"));
    });
}

// The open page's title, wherever it is now (in its page or in the top bar)
function uiPageHeading() {
    var bar = document.querySelector("#ui-topbar-title .ui-h1");
    if (bar && bar.getClientRects().length) return bar;
    return document.querySelector(".ui-page:not(.hidden) h1");
}

// NL-7: the toggle by the brand folds the sidebar into the rail, remembered in
// this browser (js/head.js applies it before the first paint)
function uiNavCollapsed() {
    return uiHasDom() && document.documentElement.classList.contains("ui-nav-collapsed");
}
function uiNavToggleSync() {
    var b = uiHasDom() ? document.getElementById("ui-nav-toggle") : null;
    if (!b) return;
    var folded = uiNavCollapsed();
    var label = folded ? t("shell.expand-menu") : t("shell.collapse-menu");
    b.setAttribute("aria-label", label);
    b.setAttribute("title", label);
    b.setAttribute("aria-expanded", String(!folded));
}
function uiNavToggle() {
    var folded = document.documentElement.classList.toggle("ui-nav-collapsed");
    try { window.localStorage.setItem("uiNavCollapsed", folded ? "1" : "0"); } catch (e) { /* storage blocked: for this visit only */ }
    uiNavToggleSync();
}

// DS-5: the top of an information dialog (the grip on a phone and the head)
// fills from the start side as the text is read; a text that does not scroll
// shows no gauge
function uiReadProgress(d) {
    // The head sits right above the body that scrolls
    var head = d.body.previousElementSibling;
    var scroller = d.body;
    if (!head || !scroller) return;
    var bar = document.createElement("span");
    bar.className = "ui-read-bar";
    bar.setAttribute("aria-hidden", "true");
    bar.innerHTML = "<i></i>";
    d.dialog.appendChild(bar);
    var update = function () {
        var room = scroller.scrollHeight - scroller.clientHeight;
        bar.classList.toggle("hidden", room <= 1);
        bar.style.height = (head.offsetTop + head.offsetHeight) + "px";
        bar.firstElementChild.style.width = (room > 1 ? Math.min(100, scroller.scrollTop / room * 100) : 0) + "%";
    };
    scroller.addEventListener("scroll", update, { passive: true });
    update();
    requestAnimationFrame(update);
}

// A text to read (help): the shared dialog, closed by its X, Escape, Back, a
// click outside or a pull down; every opening starts at the top (DS-6).
// paragraphs: plain texts, one paragraph each.
function uiInfoDialog(o) {
    var d = null;
    var close = function () { if (d) d.backdrop.remove(); };
    d = uiDialog({
        title: o.title, size: "md", close: close, backdropClose: close, bodyClass: "ui-help",
        body: (o.paragraphs || []).map(function (p) { return "<p>" + esc(p) + "</p>"; }).join(""),
    });
    d.body.scrollTop = 0;
    uiReadProgress(d);
    return d;
}

// SR-6: wait for what a page draws after it opens (every 100ms, up to 8 seconds)
function uiWaitFor(find, timeout) {
    var limit = timeout === undefined ? 8000 : timeout;
    return new Promise(function (resolve) {
        var started = Date.now();
        var look = function () {
            var found = null;
            try { found = find(); } catch (e) { found = null; }
            if (found || Date.now() - started > limit) resolve(found || null);
            else setTimeout(look, 100);
        };
        look();
    });
}

// SR-6: bring what was opened into view inside the page's own scroll area (not
// with scrollIntoView, which can pan the phone window, MO-1) and ring it for a moment
function uiReveal(el) {
    if (!el || !el.isConnected) return;
    var main = uiMainScroller();
    if (main && main.contains(el)) {
        var box = main.getBoundingClientRect();
        var r = el.getBoundingClientRect();
        var target = main.scrollTop + (r.top - box.top) - Math.max(0, (main.clientHeight - r.height) / 2);
        main.scrollTop = Math.max(0, target);
    }
    el.classList.add("ui-gs-target");
    el.classList.remove("ui-gs-flash");
    void el.offsetWidth;
    el.classList.add("ui-gs-flash");
    setTimeout(function () { el.classList.remove("ui-gs-flash"); }, 2000);
}

// MO-1: the app frame never scrolls the window. A phone keyboard can pan the
// window and leave it panned; once no field is being edited, it goes back.
function uiResetWindowPan() {
    var el = document.activeElement;
    if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
    var se = document.scrollingElement;
    if (window.scrollX || window.scrollY || (se && se.scrollTop)) window.scrollTo(0, 0);
}

// ===========================================================================
// WP2 shared helpers: a page's own sections, each with an address under the
// page's path (NL-3: /admin/settings/<section>, the router knows such a page by
// its sections flag)
// ===========================================================================

// The section the address names on page id, or "" (ids match /^[a-z0-9_-]+$/)
function uiPageSection(id) {
    if (!uiPages[id] || typeof window === "undefined" || !window.location) return "";
    var at = uiParsePath(window.location.pathname);
    return at.id === id ? at.sub : "";
}

// A typed dialog whose fields were replaced (a later step): what it holds now is
// the new starting point for the question before dropping changes (DS-9)
function uiGuardReset(d) {
    if (uiGuarded && uiGuarded.has(d.backdrop)) uiGuarded.set(d.backdrop, uiFieldsSnapshot(d.body));
}

// Put a section in the address: a Back step when the admin chose it, else the
// address is only corrected (replace)
function uiSetPageSection(id, section, replace) {
    if (!uiPages[id]) return;
    var url = uiPagePath(id) + (section ? "/" + section : "");
    if (url === window.location.pathname) return;
    if (replace) {
        history.replaceState({ page: id, idx: uiHistIdx }, "", url + window.location.hash);
        return;
    }
    uiSaveScroll();
    uiHistIdx += 1;
    history.pushState({ page: id, idx: uiHistIdx }, "", url);
}

// ===========================================================================
// Page-wide listeners, installed once by the page
// ===========================================================================

function uiInstallGlobalListeners() {
    document.addEventListener("mousedown", uiPressStart, true);
    document.addEventListener("mouseup", uiPressEnd, true);
    document.addEventListener("mousemove", function (event) { if (!event.buttons) uiPressEnd(); }, true);
    window.addEventListener("blur", uiPressEnd);
    document.addEventListener("pointerdown", function (event) {
        uiLastAction = uiLayerControl(event.target);
        uiPickerOutside(event);
        uiMenuOutside(event);
    }, true);
    document.addEventListener("keydown", function (event) { uiLastAction = uiLayerControl(event.target) || uiLastAction; }, true);
    document.addEventListener("focusin", function (event) {
        var picker = uiPickerState.open;
        if (picker && !picker.host.contains(event.target)) uiPickerClose(false);
    });
    window.addEventListener("keydown", uiOnKeyEscape, true);
    window.addEventListener("scroll", uiMenuOnScroll, true);
    window.addEventListener("scroll", uiPickerOnScroll, true);
    window.addEventListener("resize", function () { uiMenuDismiss(false); if (uiPickerState.open) uiPickerPlace(uiPickerState.open); });
    // MO-1: the window pan the phone keyboard leaves is reset
    window.addEventListener("scroll", uiResetWindowPan, { passive: true });
    document.addEventListener("focusout", function () { setTimeout(uiResetWindowPan, 300); });
    if (window.visualViewport) window.visualViewport.addEventListener("resize", function () { setTimeout(uiResetWindowPan, 100); });
    // Click to copy anywhere; inside a clickable row it does not open the row
    document.addEventListener("click", function (event) {
        var b = event.target instanceof Element ? event.target.closest("[data-copy]") : null;
        if (!b) return;
        event.preventDefault();
        event.stopPropagation();
        uiCopyClick(b);
    });
    if (typeof MutationObserver !== "undefined") {
        new MutationObserver(uiQueueLayerSync).observe(document.body, { childList: true, subtree: false, attributes: true, attributeFilter: ["class"] });
    }
}
