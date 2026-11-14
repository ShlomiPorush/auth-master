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

// Counts with thousands separators and the right singular or plural (FW-3)
function uiNum(n) {
    return Number(n || 0).toLocaleString();
}
function uiCountLabel(n, one, many) {
    return uiNum(n) + " " + (Number(n) === 1 ? one : many);
}

function uiIsPhone() {
    return typeof window !== "undefined" && !!window.matchMedia && window.matchMedia("(max-width: 760px)").matches;
}
function uiTouchOnly() {
    return typeof window !== "undefined" && !!window.matchMedia && window.matchMedia("(hover: none) and (pointer: coarse)").matches;
}

// ===========================================================================
// Dates and times (FW-1, FW-2, FW-9). Every date on a page is written here, in
// the app time zone (from /admin/api/me), in the Date Format and Time Format
// the user picked in Preferences. Pages never format a date themselves.
// ===========================================================================

var uiAppZone = "UTC";
var uiDatePrefs = { date: "browser", time: "24" };
var UI_DATE_FORMATS = [["browser", "Browser order"], ["dmy", "DD/MM/YYYY"], ["mdy", "MM/DD/YYYY"], ["ymd", "YYYY-MM-DD"]];
var UI_TIME_FORMATS = [["24", "24-hour"], ["12", "12-hour"]];
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

function uiSetDatePrefs(date, time) {
    var dateOk = UI_DATE_FORMATS.some(function (f) { return f[0] === date; });
    var timeOk = UI_TIME_FORMATS.some(function (f) { return f[0] === time; });
    uiDatePrefs = { date: dateOk ? date : "browser", time: timeOk ? time : "24" };
    return uiDatePrefs;
}

function uiLoadDatePrefs() {
    var date = "browser";
    var time = "24";
    try {
        date = window.localStorage.getItem("uiDateFormat") || date;
        time = window.localStorage.getItem("uiTimeFormat") || time;
    } catch (e) { /* defaults */ }
    return uiSetDatePrefs(date, time);
}

function uiSaveDatePrefs(date, time) {
    var prefs = uiSetDatePrefs(date, time);
    try {
        window.localStorage.setItem("uiDateFormat", prefs.date);
        window.localStorage.setItem("uiTimeFormat", prefs.time);
    } catch (e) { /* applied for this visit only */ }
    return prefs;
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

// Date in the order the user picked; numbers only, never month names
function writeDate(p, withYear, prefs) {
    var pick = (prefs || uiDatePrefs).date;
    var d = uiPad(p.day);
    var m = uiPad(p.month);
    var y = String(p.year);
    if (pick === "dmy") return withYear ? d + "/" + m + "/" + y : d + "/" + m;
    if (pick === "mdy") return withYear ? m + "/" + d + "/" + y : m + "/" + d;
    if (pick === "ymd") return withYear ? y + "-" + m + "-" + d : m + "-" + d;
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
    var tail = ":" + uiPad(p.minute) + (seconds ? ":" + uiPad(p.second) : "");
    if ((prefs || uiDatePrefs).time === "12") {
        var h = p.hour % 12 === 0 ? 12 : p.hour % 12;
        return h + tail + (p.hour < 12 ? " AM" : " PM");
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
    if (diff < 60) return "just now";
    if (diff < 3600) return Math.round(diff / 60) + " min ago";
    if (diff < 172800) return Math.round(diff / 3600) + " h ago";
    return Math.round(diff / 86400) + " d ago";
}

// A date cell: <time> with the instant, the full timestamp on hover
function uiTime(value, text) {
    var ms = uiParseInstant(value);
    if (isNaN(ms)) return '<span class="ui-muted">-</span>';
    var shown = text === undefined ? formatListTime(ms) : text;
    return '<time class="ui-num" datetime="' + esc(new Date(ms).toISOString()) + '" title="' + esc(formatTime(ms)) + '">' + esc(shown) + "</time>";
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
        var t = wall - off * 60000;
        var p = uiZoneParts(t, zone);
        if (Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) === wall) found.push(t);
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
// Toast (FB-2): one at a time, 4 seconds, waits while hovered or focused
// ===========================================================================

function showToast(message, type) {
    if (!uiHasDom()) return null;
    var icons = { success: "✓", error: "✗", warning: "!", info: "i" };
    var kind = icons[type] ? type : "info";
    var old = document.getElementById("ui-toast");
    var focusWasInIt = !!old && old.contains(document.activeElement);
    if (old) old.remove();

    var toast = document.createElement("div");
    toast.id = "ui-toast";
    toast.className = "ui-toast ui-toast-" + kind;
    toast.setAttribute("role", kind === "error" ? "alert" : "status");
    toast.innerHTML = '<span class="ui-toast-icon" aria-hidden="true">' + icons[kind] + "</span>" +
        "<p>" + esc(message) + "</p>" +
        '<button type="button" class="ui-icon-btn" data-toast-x title="Close" aria-label="Close">' + UI_CLOSE_ICON + "</button>";
    toast.querySelector("[data-toast-x]").addEventListener("click", function () { toast.remove(); });
    document.body.appendChild(toast);
    if (focusWasInIt) toast.querySelector("button").focus({ preventScroll: true });

    var left = 4000;
    var started = Date.now();
    var timer = null;
    var fading = false;
    var removing = null;
    var hold = function () { return toast.matches(":hover") || toast.contains(document.activeElement); };
    var fade = function () {
        if (!toast.isConnected) return;
        fading = true;
        toast.classList.add("is-fading");
        removing = setTimeout(function () { toast.remove(); }, 300);
    };
    var run = function () {
        clearTimeout(timer);
        started = Date.now();
        timer = setTimeout(fade, left);
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
    return '<button type="button" class="ui-copy' + (cls ? " " + esc(cls) : "") + '" data-copy="' + esc(text) + '" title="Click to copy">' +
        "<bdi>" + esc(text) + "</bdi>" + UI_COPY_ICON + "</button>";
}

function uiCopyClick(button) {
    var text = button.getAttribute("data-copy") || "";
    uiCopyText(text).then(function (ok) {
        if (!ok) {
            var secure = typeof window !== "undefined" && window.isSecureContext;
            showToast(secure ? "Failed to copy" : "Copying needs HTTPS. Select the text and copy it yourself.", "error");
            return;
        }
        showToast("Copied: " + text, "success");
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
        ? '<button type="button" class="ui-icon-btn" data-dialog-x title="Close" aria-label="Close">' + UI_CLOSE_ICON + "</button>" : "";
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
        title: "Discard your changes?", message: "What you changed in this window is not saved.",
        confirmText: "Discard", cancelText: "Keep editing", danger: true, focusCancel: true,
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
            title: opts.title || "Confirm", size: "sm", role: "alertdialog", backdropClass: "ui-confirm",
            noHistory: !!opts.noHistory, describedBy: "ui-confirm-text",
            body: '<p class="ui-confirm-text" id="ui-confirm-text">' + esc(opts.message || "") + "</p>",
            foot: '<button type="button" class="ui-btn" data-cancel>' + esc(opts.cancelText || "Cancel") + "</button>" +
                '<button type="button" class="ui-btn ' + (opts.danger ? "ui-btn-danger-solid" : "ui-btn-primary") + '" data-confirm>' +
                esc(opts.confirmText || "Confirm") + "</button>",
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

function uiOpenLayers() {
    if (!uiHasDom()) return [];
    return Array.prototype.slice.call(document.querySelectorAll(".ui-dialog-backdrop"))
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
    var heading = document.querySelector(".ui-page:not(.hidden) h1");
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
var uiAppTitle = "Auth Service";

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
        title: "Leave without saving?",
        message: "You have " + uiCountLabel(n, "unsaved change", "unsaved changes") + ". Leaving drops " + (n === 1 ? "it" : "them") + ".",
        confirmText: "Leave", cancelText: "Stay", danger: true, focusCancel: true, noHistory: true,
    }).then(function (ok) {
        uiLeaveAsking = false;
        if (ok) uiUnsavedDiscard();
        return ok;
    });
}

function uiPagePath(id) {
    return basePath() + uiPages[id].path;
}

function uiPageFromLocation() {
    var p = window.location.pathname;
    var b = basePath();
    if (b && p.indexOf(b) === 0) p = p.slice(b.length);
    if (p.length > 1 && p.charAt(p.length - 1) === "/") p = p.slice(0, -1);
    for (var id in uiPages) {
        if (Object.prototype.hasOwnProperty.call(uiPages, id) && uiPages[id].path === p) return id;
    }
    return "";
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

function uiSetTitle(id) {
    document.title = (uiPages[id] ? uiPages[id].title + " · " : "") + uiAppTitle;
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
        ["wheel", "touchstart", "pointerdown", "keydown"].forEach(function (t) { window.removeEventListener(t, stop, true); });
    };
    ["wheel", "touchstart", "pointerdown", "keydown"].forEach(function (t) { window.addEventListener(t, stop, true); });
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
    var url = known ? window.location.pathname + window.location.search : uiPagePath(id);
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
    uiPageCurrent = id;
    uiSetTitle(id);
    if (uiPageShow) uiPageShow(id, info || {});
}

// Open a page from the app: one Back step when the address changes; the current
// page clicked again shows it again without a new entry (NL-4)
function uiGo(id, query, opts) {
    var o = opts || {};
    var qs = query ? (typeof query === "string" ? query : query.toString()) : "";
    var url = uiPagePath(id) + (qs ? "?" + qs : "");
    var here = window.location.pathname + window.location.search;
    var proceed = function () {
        if (id === uiPageCurrent && url === here && !o.force) {
            uiShowPage(id, { again: true });
            return true;
        }
        uiSaveScroll();
        if (uiOverlayEntry || o.replace) {
            // Moving from inside a layer takes over its history entry
            uiOverlayEntry = false;
            history.replaceState({ page: id, idx: uiHistIdx }, "", url);
        } else {
            uiHistIdx += 1;
            history.pushState({ page: id, idx: uiHistIdx }, "", url);
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
    if (!uiPopConfirmed && delta && uiUnsavedCount()) {
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
        listId + '" aria-label="Search ' + esc(picker.label.toLowerCase()) + '" placeholder="Search">' : "") +
        '<div class="ui-picker-list" role="listbox" id="' + listId + '" aria-label="' + esc(picker.label) + '">' +
        opts.map(function (opt, i) {
            var on = i === select.selectedIndex;
            var dot = opt.getAttribute("data-dot");
            var hint = opt.getAttribute("data-hint");
            return '<button type="button" class="ui-option" role="option" tabindex="-1" data-index="' + i + '" aria-selected="' + on + '">' +
                (dot !== null ? '<i class="ui-option-dot' + (dot ? " ui-dot-" + esc(dot) : "") + '" aria-hidden="true"></i>' : "") +
                "<span>" + esc(opt.textContent) + "</span>" + (hint ? '<small class="ui-num">' + esc(hint) + "</small>" : "") + "</button>";
        }).join("") + "</div>" +
        (withSearch ? '<p class="ui-picker-none hidden" role="status">No matches</p>' : "");
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
    menu.setAttribute("aria-label", label || "Actions");
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
    state.anchor.setAttribute("aria-expanded", "false");
    state.anchor.removeAttribute("aria-controls");
    try { if (state.supported && state.menu.matches(":popover-open")) state.menu.hidePopover(); } catch (e) { /* closed already */ }
    state.menu.remove();
    if (returnFocus && state.anchor.isConnected) state.anchor.focus({ preventScroll: true });
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
function uiSortToggle(state, col) {
    if (state.key === col.key) state.dir = state.dir === "asc" ? "desc" : "asc";
    else {
        state.key = col.key;
        state.dir = col.kind === "text" ? "asc" : "desc";
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

function uiSortAria(state, key) {
    return state.key === key ? (state.dir === "asc" ? "ascending" : "descending") : "none";
}

function uiSortWords(kind, dir) {
    if (kind === "text") return dir === "asc" ? "A to Z" : "Z to A";
    if (kind === "date") return dir === "asc" ? "Oldest first" : "Newest first";
    if (kind === "number") return dir === "asc" ? "Low to high" : "High to low";
    return dir === "asc" ? "Ascending" : "Descending";
}

// The Sort button's panel: Sort by and Order, applied at once (TB-28)
function uiSortMenu(anchor, name, cols, state, onChange) {
    if (uiMenuOpen && uiMenuOpen.anchor === anchor) { uiMenuDismiss(true); return; }
    uiMenuDismiss(false);
    var panel = document.createElement("div");
    panel.className = "ui-menu ui-sort-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Sort " + name);
    panel.id = "ui-sort-" + (++uiDialogCount);
    var supported = typeof panel.showPopover === "function";
    if (supported) panel.setAttribute("popover", "auto");
    var draw = function () {
        var col = cols.filter(function (c) { return c.key === state.key; })[0];
        panel.innerHTML = '<fieldset><legend>Sort by</legend>' +
            cols.map(function (c) {
                return '<label><input type="radio" class="ui-check" name="' + panel.id + '-by" value="' + esc(c.key) + '"' + (state.key === c.key ? " checked" : "") + "> " + esc(c.label) + "</label>";
            }).join("") + "</fieldset>" +
            '<fieldset' + (col ? "" : " disabled") + "><legend>Order</legend>" +
            ["asc", "desc"].map(function (d) {
                return '<label><input type="radio" class="ui-check" name="' + panel.id + '-dir" value="' + d + '"' + (state.dir === d ? " checked" : "") + "> " +
                    esc(uiSortWords(col ? col.kind : "", d)) + "</label>";
            }).join("") + "</fieldset>";
    };
    draw();
    document.body.appendChild(panel);
    var st = { menu: panel, anchor: anchor, supported: supported };
    uiMenuOpen = st;
    anchor.setAttribute("aria-expanded", "true");
    panel.addEventListener("change", function (event) {
        var input = event.target;
        if (input.name === panel.id + "-by") {
            var col = cols.filter(function (c) { return c.key === input.value; })[0];
            state.key = col.key;
            state.dir = col.kind === "text" ? "asc" : "desc";
        } else {
            state.dir = input.value;
        }
        var focusName = input.name;
        var focusValue = state.key && input.name.slice(-3) === "-by" ? state.key : state.dir;
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
