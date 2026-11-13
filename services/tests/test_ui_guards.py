"""Guards of the admin portal's frontend (v3 style).

- popups: every dialog, sheet, menu, picker and toast comes from its builder in
  js/ui.js; no other code or stylesheet rule makes a layer float (DS-24).
- refresh: every page of the router registers a refresh of its own (SV-10).
- inventory: nothing of the old UI is lost (ui_inventory_baseline.json).
- dates: pages never format a date themselves; js/ui.js does (FW-9).
- tokens: no colour, font size or spacing literal outside the ui.css tokens.
- assets: everything the pages reference is local and served with status 200,
  with ROOT_PATH empty and set; no external URL, no Tailwind class or removed file.

Each guard has self-tests that inject one bypass and expect it to be caught.
The app tests generate every secret per run with ``secrets`` before importing
the app and refuse placeholders; Redis is faked. Requires pytest and httpx.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "static" / "public"
REPO = ROOT.parent
VENDORED_JS = {"qrcode.min.js", "swagger-ui-bundle.js", "redoc.standalone.js"}
VENDORED_CSS = {"swagger-ui.css"}


def read_sources() -> dict[str, str]:
    """The portal's own HTML, JS and CSS, keyed by path relative to static/public."""
    files: dict[str, str] = {}
    for p in sorted(PUBLIC.glob("*.html")):
        files[p.name] = p.read_text(encoding="utf-8")
    for p in sorted((PUBLIC / "js").glob("*.js")):
        if p.name not in VENDORED_JS:
            files[f"js/{p.name}"] = p.read_text(encoding="utf-8")
    for p in sorted((PUBLIC / "css").glob("*.css")):
        if p.name not in VENDORED_CSS:
            files[f"css/{p.name}"] = p.read_text(encoding="utf-8")
    return files


SOURCES = read_sources()


# ---------------------------------------------------------------------------
# A small JavaScript scanner: comments out, strings and regexes known, top-level
# function declarations found by brace depth
# ---------------------------------------------------------------------------

_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^")


def js_lex(src: str) -> tuple[str, str]:
    """Return (code, masked): comments blanked in both; in masked, the contents of
    strings, templates and regex literals are blanked too (quotes kept)."""
    code = list(src)
    masked = list(src)
    i = 0
    n = len(src)
    last = ""  # last significant character outside comments/strings
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                code[k] = masked[k] = " "
            i = j
            continue
        if c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if src[k] != "\n":
                    code[k] = masked[k] = " "
            i = j
            continue
        if c in "'\"`":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif c != "`" and src[j] == "\n":
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                if src[k] != "\n":
                    masked[k] = " "
            i = j + 1
            last = c
            continue
        if c == "/" and (last in _REGEX_BEFORE or last == "" or re.search(r"\b(return|typeof|case|in|of)\s*$", src[max(0, i - 8):i])):
            j = i + 1
            in_class = False
            while j < n and src[j] != "\n":
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                elif src[j] == "/" and not in_class:
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                masked[k] = " "
            i = j + 1
            last = "/"
            continue
        if not c.isspace():
            last = c
        i += 1
    return "".join(code), "".join(masked)


def js_depths(masked: str) -> list[int]:
    """Brace depth before each character."""
    depth = 0
    out = []
    for c in masked:
        out.append(depth)
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
    return out


def js_functions(masked: str) -> list[tuple[str, int, int]]:
    """Top-level `function name(` declarations: (name, start, end)."""
    depths = js_depths(masked)
    found = []
    for m in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", masked):
        if depths[m.start()] != 0:
            continue
        open_brace = masked.find("{", m.end())
        depth = 0
        end = len(masked)
        for k in range(open_brace, len(masked)):
            if masked[k] == "{":
                depth += 1
            elif masked[k] == "}":
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        found.append((m.group(1), m.start(), end))
    return found


def js_owner(functions: list[tuple[str, int, int]], pos: int) -> str:
    for name, start, end in functions:
        if start <= pos < end:
            return name
    return "<top level>"


# ---------------------------------------------------------------------------
# Popups (DS-24)
# ---------------------------------------------------------------------------

JS_LAYER_PATTERNS = {
    "dialog role": r"role\s*=\s*\\?[\"'](?:alert)?dialog|[\"']role[\"']\s*,\s*[\"'](?:alert)?dialog|\brole\s*:\s*[\"'](?:alert)?dialog|\|\|\s*[\"']dialog[\"']",
    "dialog element": r"<dialog\b|createElement\(\s*[\"']dialog|\bshowModal\s*\(",
    "popover": r"\bpopover\s*=|[\"']popover[\"']|\bshowPopover\s*\(",
    "layer class": r"[\"'][^\"'\n]*\bui-(?:dialog|sheet|menu|toast|popover)(?:-backdrop)?\b",
    "native dialog": r"(?<![\w.$])(?:window\.)?(?:confirm|alert|prompt)\s*\(",
    "position in JS": r"\bstyle\.(?:position|zIndex)\s*=|setProperty\(\s*[\"'](?:position|z-index)[\"']|\bcssText\b|setAttribute\(\s*[\"']style[\"']",
}

# (file, function) -> why it may build a layer
BUILDERS = {
    ("js/ui.js", "uiDialog"): "the dialog builder: frame, X, foot, width, phone sheet",
    ("js/ui.js", "uiConfirm"): "the question builder (alertdialog, no X), built on uiDialog",
    ("js/ui.js", "uiDialogSheet"): "the phone sheet behaviour of every dialog (grow, drag, pull to close)",
    ("js/ui.js", "showToast"): "the toast builder",
    ("js/ui.js", "uiCopyFallback"): "a hidden, fixed text field for the copy command without the Clipboard API",
    ("js/ui.js", "uiPickerOpen"): "the picker builder (listbox panel)",
    ("js/ui.js", "uiMenu"): "the row menu builder on the native popover layer",
    ("js/ui.js", "uiSortMenu"): "the Sort button's panel on the native popover layer",
    ("js/ui.js", "uiOpenLayers"): "the layer manager lists open dialogs for Escape, Back, focus and inert",
    ("js/ui.js", "uiLayerFocusIn"): "the layer manager moves the focus into the top dialog",
    ("js/ui.js", "uiLayerApplyInert"): "the layer manager keeps toasts and menus usable above the inert page",
    ("js/ui.js", "uiAreaHeld"): "a refresh waits while a menu is open",
}

# CSS selectors that may float (position: fixed, or z-index of 10 or more)
FLOATING_CSS = {
    ".ui-dialog-backdrop": "every dialog and phone sheet (uiDialog)",
    ".ui-dialog-backdrop.ui-confirm": "a question sits above the dialog it was asked from",
    ".ui-toast": "the toast (showToast)",
    ".ui-menu[popover]": "row menus and the Sort panel on the native popover layer",
    ".ui-popover": "picker panels (uiPicker)",
    ".ui-tabbar": "the phone tab bar (NL-9), part of the page",
    ".ui-fly": "the page name beside a rail icon (NL-8)",
}

HTML_LAYER_PATTERNS = {
    "dialog role": r"role\s*=\s*[\"'](?:alert)?dialog",
    "dialog element": r"<dialog\b",
    "popover": r"\bpopover\s*=",
    "layer class": r"class\s*=\s*[\"'][^\"']*\bui-(?:dialog|sheet|menu|toast|popover)\b",
    "fixed style": r"style\s*=\s*[\"'][^\"']*(?:position\s*:\s*fixed|z-index)",
    "native dialog": r"(?<![\w.$])(?:window\.)?(?:confirm|alert|prompt)\s*\(",
}


def popup_problems(sources: dict[str, str]) -> list[str]:
    problems: list[str] = []
    used: set[tuple[str, str]] = set()
    for name, src in sources.items():
        if name.endswith(".js"):
            code, masked = js_lex(src)
            funcs = js_functions(masked)
            for kind, pattern in JS_LAYER_PATTERNS.items():
                for m in re.finditer(pattern, code):
                    owner = js_owner(funcs, m.start())
                    if (name, owner) in BUILDERS:
                        used.add((name, owner))
                        continue
                    problems.append(f"{name}:{owner}: {kind} outside a builder: {m.group(0)!r}")
            # Adding a floating class from page code
            for m in re.finditer(r"classList\.(?:add|toggle)\(\s*[\"']([\w-]+)", code):
                cls = "." + m.group(1)
                if any(cls == sel or sel.endswith(cls) for sel in FLOATING_CSS):
                    owner = js_owner(funcs, m.start())
                    if (name, owner) not in BUILDERS:
                        problems.append(f"{name}:{owner}: adds the floating class {cls}")
            # Every uiDialog call names its size, and a close function unless it is a question
            for m in re.finditer(r"\buiDialog\(\s*\{", masked):
                start = m.end() - 1
                depth = 0
                for k in range(start, len(masked)):
                    if masked[k] == "{":
                        depth += 1
                    elif masked[k] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                args = code[start:k + 1]
                owner = js_owner(funcs, m.start())
                if not re.search(r"\bsize\s*:", args):
                    problems.append(f"{name}:{owner}: uiDialog without size")
                if not re.search(r"\bclose\s*:", args) and not re.search(r"role\s*:\s*[\"']alertdialog", args):
                    problems.append(f"{name}:{owner}: uiDialog without a close function")
        elif name.endswith(".html"):
            body = re.sub(r"<!--.*?-->", "", src, flags=re.S)
            for kind, pattern in HTML_LAYER_PATTERNS.items():
                for m in re.finditer(pattern, body):
                    problems.append(f"{name}: {kind} in the markup: {m.group(0)!r}")
            for block in re.findall(r"<style[^>]*>(.*?)</style>", body, flags=re.S):
                problems += css_float_problems(block, f"{name} <style>")
        elif name.endswith(".css"):
            problems += css_float_problems(src, name)
    for key in BUILDERS:
        name, func = key
        if name not in sources:
            problems.append(f"stale builder entry: {name} is gone")
            continue
        names = {f for f, _s, _e in js_functions(js_lex(sources[name])[1])}
        if func not in names:
            problems.append(f"stale builder entry: {name}:{func} no longer exists")
        elif key not in used:
            problems.append(f"stale builder entry: {name}:{func} builds no layer")
    return problems


def css_rules(css: str) -> list[tuple[str, str]]:
    """(selector, declarations) for every rule, inside @media and @supports too."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: list[tuple[str, str]] = []

    def walk(text: str) -> None:
        i = 0
        while i < len(text):
            open_brace = text.find("{", i)
            if open_brace == -1:
                return
            head = text[i:open_brace].strip()
            depth = 0
            for k in range(open_brace, len(text)):
                if text[k] == "{":
                    depth += 1
                elif text[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
            inner = text[open_brace + 1:k]
            if head.startswith("@media") or head.startswith("@supports"):
                walk(inner)
            elif not head.startswith("@"):
                rules.append((head, inner))
            i = k + 1

    walk(css)
    return rules


def css_float_problems(css: str, where: str) -> list[str]:
    problems = []
    used = set()
    for selector, decls in css_rules(css):
        for decl in decls.split(";"):
            prop, _sep, value = decl.partition(":")
            prop = prop.strip().lower()
            value = value.strip().lower()
            floats = (prop == "position" and value.startswith("fixed")) or (
                prop == "z-index" and re.match(r"-?\d+", value) and int(re.match(r"-?\d+", value).group(0)) >= 10
            )
            if not floats:
                continue
            for sel in (s.strip() for s in selector.split(",")):
                if sel in FLOATING_CSS:
                    used.add(sel)
                else:
                    problems.append(f"{where}: '{sel}' floats ({prop}: {value}) and is not in FLOATING_CSS")
    return problems


def test_popups_come_from_their_builders():
    assert popup_problems(SOURCES) == []


def test_floating_css_list_is_current():
    css = SOURCES["css/ui.css"]
    floating = set()
    for selector, decls in css_rules(css):
        for decl in decls.split(";"):
            prop, _s, value = decl.partition(":")
            prop, value = prop.strip().lower(), value.strip().lower()
            if (prop == "position" and value.startswith("fixed")) or (prop == "z-index" and value.isdigit() and int(value) >= 10):
                floating.update(s.strip() for s in selector.split(","))
    assert sorted(set(FLOATING_CSS) - floating) == [], "FLOATING_CSS lists selectors that no longer float"


def _with(name: str, extra: str) -> dict[str, str]:
    copy = dict(SOURCES)
    copy[name] = copy[name] + "\n" + extra
    return copy


POPUP_BYPASSES = [
    ("js/dashboard.js", 'function evilA() { var d = document.createElement("div"); d.setAttribute("role", "dialog"); }'),
    ("js/dashboard.js", "function evilB() { document.body.innerHTML += '<div role=\"alertdialog\">x</div>'; }"),
    ("js/dashboard.js", 'function evilC() { document.createElement("dialog").showModal(); }'),
    ("js/dashboard.js", 'function evilD() { var m = document.createElement("div"); m.setAttribute("popover", "auto"); }'),
    ("js/dashboard.js", "function evilE() { return '<div class=\"ui-dialog-backdrop\"></div>'; }"),
    ("js/dashboard.js", 'function evilF() { if (confirm("Sure?")) return 1; }'),
    ("js/dashboard.js", 'function evilG() { window.alert("x"); }'),
    ("js/dashboard.js", 'function evilH() { var v = prompt("x"); }'),
    ("js/dashboard.js", 'function evilI(el) { el.style.position = "fixed"; }'),
    ("js/dashboard.js", 'function evilJ(el) { el.style.zIndex = "999"; }'),
    ("js/dashboard.js", 'function evilK(el) { el.style.setProperty("position", "fixed"); }'),
    ("js/dashboard.js", 'function evilL(el) { el.style.cssText = "position:fixed"; }'),
    ("js/dashboard.js", 'function evilM(el) { el.setAttribute("style", "position:fixed;inset:0"); }'),
    ("js/dashboard.js", 'function evilN(el) { el.classList.add("ui-toast"); }'),
    ("js/dashboard.js", 'function evilO() { uiDialog({ title: "x", close: function () {} }); }'),
    ("js/dashboard.js", 'function evilP() { uiDialog({ title: "x", size: "sm" }); }'),
    ("css/ui.css", ".my-overlay { position: fixed; inset: 0; }"),
    ("css/ui.css", ".x-layer { position: absolute; }\n.x-layer.is-open { z-index: 20; }"),
    ("css/ui.css", "@media (max-width: 760px) { .phone-float { z-index: 99; } }"),
    ("index.html", '<div role="dialog" aria-label="x"></div>'),
    ("index.html", '<div style="position: fixed; inset: 0"></div>'),
    ("index.html", "<style>.sneaky { position: fixed; }</style>"),
    ("login.html", "<dialog open>x</dialog>"),
]


@pytest.mark.parametrize("name,extra", POPUP_BYPASSES)
def test_popup_guard_catches(name, extra):
    assert popup_problems(_with(name, extra)), f"not caught: {extra}"


def test_popup_guard_reports_stale_entries():
    copy = dict(SOURCES)
    copy["js/ui.js"] = copy["js/ui.js"].replace("function uiCopyFallback(", "function uiCopyFallbackRenamed(")
    assert any("stale builder entry" in p for p in popup_problems(copy))


def test_js_scanner_ignores_comments_and_reads_regexes():
    code, masked = js_lex('var a = /"{/g; // role="dialog"\nfunction f() { return "}"; }\n/* confirm( */')
    assert "role=" not in code
    assert "confirm(" not in code
    assert [n for n, _s, _e in js_functions(masked)] == ["f"]


# ---------------------------------------------------------------------------
# Page refresh (SV-10)
# ---------------------------------------------------------------------------

# Pages without a refresh of their own, each with the reason
NO_REFRESH: dict[str, str] = {}


def refresh_problems(dashboard: str) -> list[str]:
    code, masked = js_lex(dashboard)
    m = re.search(r"\bvar\s+PAGES\s*=\s*\{", masked)
    if not m:
        return ["dashboard.js has no PAGES table"]
    start = m.end() - 1
    depth = 0
    for k in range(start, len(masked)):
        if masked[k] == "{":
            depth += 1
        elif masked[k] == "}":
            depth -= 1
            if depth == 0:
                break
    table = code[start:k + 1]
    pages = set(re.findall(r"([\w-]+)\s*:\s*\{\s*path\s*:", table))
    depths = js_depths(masked)
    registered = set()
    for call in re.finditer(r"\bregisterPageRefresh\(\s*", masked):
        if depths[call.start()] != 0:
            continue
        arg = re.match(r"[\"']([\w-]+)[\"']", code[call.end():])
        if arg:
            registered.add(arg.group(1))
    problems = []
    for page in sorted(pages - registered - set(NO_REFRESH)):
        problems.append(f"page '{page}' has no top-level registerPageRefresh and is not in NO_REFRESH")
    for page in sorted(registered - pages):
        problems.append(f"a refresh is registered for '{page}', which is not a page")
    for page, reason in NO_REFRESH.items():
        if page not in pages:
            problems.append(f"NO_REFRESH lists '{page}', which is not a page")
        if page in registered:
            problems.append(f"NO_REFRESH lists '{page}', which now refreshes")
        if not reason:
            problems.append(f"NO_REFRESH '{page}' has no reason")
    if not pages:
        problems.append("PAGES is empty")
    return problems


def test_every_page_refreshes_itself():
    assert refresh_problems(SOURCES["js/dashboard.js"]) == []


def test_refresh_guard_catches():
    src = SOURCES["js/dashboard.js"]
    # A page whose refresh is gone
    assert refresh_problems(re.sub(r'registerPageRefresh\("zones"[^\n]*\n', "\n", src, count=1))
    # A refresh inside a function that is never called is not a refresh
    nested = src.replace('registerPageRefresh("zones",', 'function neverCalled() { registerPageRefresh("zones",', 1)
    nested = nested.replace("return dashZonesLoadPage(true); });", "return dashZonesLoadPage(true); }); }", 1)
    assert any("zones" in p for p in refresh_problems(nested))
    # A refresh for a page the router does not know
    assert refresh_problems(src + '\nregisterPageRefresh("ghost", 30000, function () {});\n')


# ---------------------------------------------------------------------------
# Inventory: nothing of the old UI is lost
# ---------------------------------------------------------------------------

BASELINE = json.loads((Path(__file__).parent / "ui_inventory_baseline.json").read_text(encoding="utf-8"))


def inventory_problems(sources: dict[str, str]) -> list[str]:
    corpus = "\n".join(text for name, text in sources.items() if name.endswith((".html", ".js")))
    problems = []
    keys = set()
    for item in BASELINE["items"]:
        key = item.get("key", "")
        if not key or key in keys:
            problems.append(f"inventory key missing or repeated: {key!r}")
        keys.add(key)
        has_expect = bool(item.get("expect"))
        has_dropped = bool(str(item.get("dropped", "")).strip())
        if has_expect == has_dropped:
            problems.append(f"{key}: needs either 'expect' or 'dropped' with a reason")
            continue
        for snippet in item.get("expect", []):
            if snippet not in corpus:
                problems.append(f"{key}: the new UI has no {snippet!r} ({item.get('old', '')})")
    return problems


def test_inventory_is_kept():
    assert inventory_problems(SOURCES) == []


def test_inventory_guard_catches():
    copy = dict(SOURCES)
    copy["js/dashboard.js"] = copy["js/dashboard.js"].replace("Delete this token permanently?", "Remove it?")
    assert any("tokens.row.delete.confirm" in p for p in inventory_problems(copy))
    copy = dict(SOURCES)
    copy["index.html"] = copy["index.html"].replace('data-inventory="tokens.search"', "")
    assert any("tokens.search" in p for p in inventory_problems(copy))


# ---------------------------------------------------------------------------
# Dates (FW-9): only the helpers in js/ui.js format dates
# ---------------------------------------------------------------------------

DATE_PATTERNS = {
    "toLocaleDateString": r"\.toLocaleDateString\s*\(",
    "toLocaleTimeString": r"\.toLocaleTimeString\s*\(",
    "date getter": r"\.get(?:UTC)?(?:Hours|Minutes|Seconds|Date|Day|Month|FullYear)\s*\(",
    "Intl.DateTimeFormat": r"\bIntl\s*\.\s*DateTimeFormat\b",
    "format option": r"\b(?:hour12|hourCycle|dateStyle|timeStyle)\b",
    "toLocaleString on a date": r"\.toLocaleString\s*\(",
}
# (file, function) -> why it may
DATE_HELPERS = {
    ("js/ui.js", "uiZoneValid"): "checks that a zone name works in this browser",
    ("js/ui.js", "uiZoneParts"): "reads an instant's wall clock in the app zone",
    ("js/ui.js", "writeDate"): "writes a date in the browser's order for Date Format 'Browser order'",
}
NUMBER_LOCALE = re.compile(r"Number\((?:[^()]|\([^()]*\))*\)\.toLocaleString\s*\(")


def date_problems(sources: dict[str, str]) -> list[str]:
    problems = []
    used = set()
    for name, src in sources.items():
        if not name.endswith(".js"):
            continue
        code, masked = js_lex(src)
        funcs = js_functions(masked)
        allowed_numbers = {m.end() - len(".toLocaleString(") for m in NUMBER_LOCALE.finditer(code)}
        for kind, pattern in DATE_PATTERNS.items():
            for m in re.finditer(pattern, code):
                if kind == "toLocaleString on a date" and any(abs(m.start() - a) <= 1 for a in allowed_numbers):
                    continue
                owner = js_owner(funcs, m.start())
                if (name, owner) in DATE_HELPERS:
                    used.add((name, owner))
                    continue
                problems.append(f"{name}:{owner}: {kind} outside the date helpers")
    for key in DATE_HELPERS:
        if key not in used:
            problems.append(f"stale date helper entry: {key}")
    return problems


def test_pages_never_format_dates_themselves():
    assert date_problems(SOURCES) == []


DATE_BYPASSES = [
    "function d1(t) { return new Date(t).toLocaleDateString(); }",
    "function d2(t) { return new Date(t).toLocaleTimeString(); }",
    "function d3(t) { return new Date(t).getHours(); }",
    "function d4(t) { return new Date(t).getUTCFullYear(); }",
    "function d5(t) { return new Intl.DateTimeFormat('en').format(t); }",
    "function d6(t) { return Intl.DateTimeFormat('en').format(t); }",
    "function d7(t) { return new Date(t).toLocaleString('en', { hour12: false }); }",
    "function d8(t) { return new Date(t).toLocaleString(); }",
]


@pytest.mark.parametrize("extra", DATE_BYPASSES)
def test_date_guard_catches(extra):
    assert date_problems(_with("js/dashboard.js", extra))


def test_date_guard_allows_number_formatting():
    assert date_problems(_with("js/dashboard.js", "function n1(x) { return Number(x || 0).toLocaleString(); }")) == []


# ---------------------------------------------------------------------------
# Tokens: no colour, font size or spacing literal outside ui.css's tokens
# ---------------------------------------------------------------------------

SPACING_PROPS = re.compile(r"^(?:padding|margin|gap|row-gap|column-gap)(?:-[a-z-]+)?$")


def token_problems(css: str) -> list[str]:
    problems = []
    for selector, decls in css_rules(css):
        token_block = selector in (":root", ".dark")
        for decl in decls.split(";"):
            prop, _s, value = decl.partition(":")
            prop, value = prop.strip().lower(), value.strip()
            if not prop or token_block:
                continue
            if re.search(r"#[0-9a-fA-F]{3,8}\b", value):
                problems.append(f"{selector}: colour literal in {prop}: {value}")
            if prop == "font-size" and not re.fullmatch(r"var\(--[\w-]+\)|inherit|0", value):
                problems.append(f"{selector}: font size outside the tokens: {value}")
            if SPACING_PROPS.match(prop):
                rest = value.replace("!important", "")
                for _ in range(5):
                    rest = re.sub(r"(?:var|env)\([^()]*\)", " ", rest)
                rest = re.sub(r"calc\(|\)|[+*/]|\s-\s", " ", rest)
                for tok in rest.split():
                    if tok not in ("0", "auto", "-1") and not re.fullmatch(r"-?0(?:px)?", tok):
                        problems.append(f"{selector}: spacing literal in {prop}: {value}")
                        break
    return problems


def test_ui_css_uses_only_tokens():
    assert token_problems(SOURCES["css/ui.css"]) == []


@pytest.mark.parametrize("extra", [
    ".x { color: #7c3aed; }",
    ".x { font-size: 13px; }",
    ".x { padding: 7px 10px; }",
    ".x { margin-inline-start: calc(var(--ui-sp-2) + 3px); }",
])
def test_token_guard_catches(extra):
    assert token_problems(SOURCES["css/ui.css"] + "\n" + extra)


def test_no_purple():
    css = SOURCES["css/ui.css"].lower()
    assert not re.search(r"purple|violet|#(?:7c3aed|8b5cf6|a78bfa|6d28d9|9333ea|a855f7)", css)


# ---------------------------------------------------------------------------
# No external URLs, no Tailwind classes, no removed files
# ---------------------------------------------------------------------------

REMOVED_FILES = ("tailwind.css", "shared.css", "inter.css", "services/styles")
CLASS_ATTR = re.compile(r"class\s*=\s*(?:\"([\w\s-]*)\"|'([\w\s-]*)')")
ALLOWED_CLASS = re.compile(r"^(?:ui|ui-[\w-]+|is-[\w-]+|hidden|dark)$")


def hygiene_problems(sources: dict[str, str]) -> list[str]:
    problems = []
    for name, src in sources.items():
        for m in re.finditer(r"(?:https?:)?//[\w.-]+\.[a-z]{2,}[/\w.-]*", src):
            url = m.group(0)
            if url.startswith(("http://www.w3.org/", "//www.w3.org/")):
                continue
            # "//" inside a JavaScript comment or regex is not a URL
            if not url.startswith("http") and name.endswith(".js"):
                continue
            problems.append(f"{name}: external URL {url}")
        for removed in REMOVED_FILES:
            if removed in src:
                problems.append(f"{name}: references the removed {removed}")
        if name.endswith(".html"):
            # All page code lives in js/*.js, where the popup and date scans read it
            for m in re.finditer(r"<script\b(?![^>]*\bsrc=)[^>]*>", src, re.IGNORECASE):
                problems.append(f"{name}: inline <script> without src: {m.group(0)}")
        if name.endswith((".html", ".js")):
            for m in CLASS_ATTR.finditer(src):
                for token in (m.group(1) or m.group(2) or "").split():
                    if not ALLOWED_CLASS.match(token):
                        problems.append(f"{name}: class '{token}' is not a ui- class (Tailwind leftover?)")
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    for removed in ("tailwind", "services/styles", "node:"):
        if removed in dockerfile:
            problems.append(f"Dockerfile still mentions {removed}")
    return problems


def test_no_external_urls_tailwind_or_removed_files():
    assert hygiene_problems(SOURCES) == []
    assert not (ROOT / "styles").exists(), "services/styles was removed with Tailwind"
    assert not (PUBLIC / "css" / "shared.css").exists()
    assert not list((PUBLIC / "fonts").glob("UcC73*.woff2")), "the Inter fonts were removed"


@pytest.mark.parametrize("name,extra", [
    ("index.html", '<div class="flex items-center gap-2">x</div>'),
    ("index.html", '<script src="https://cdn.example.com/x.js"></script>'),
    ("index.html", '<link href="/css/tailwind.css" rel="stylesheet">'),
    ("css/ui.css", "@import url('https://fonts.example.com/x.css');"),
    ("js/dashboard.js", "var x = '<span class=\"text-slate-400\">x</span>';"),
    ("login.html", "<script>document.title = new Date().toLocaleString();</script>"),
    ("login.html", "<SCRIPT>document.title = 'x';</SCRIPT>"),
])
def test_hygiene_guard_catches(name, extra):
    assert hygiene_problems(_with(name, extra))


def test_font_licences_ship_with_the_fonts():
    fonts = list((PUBLIC / "fonts").glob("*.woff2"))
    assert fonts
    licences = {p.name for p in (PUBLIC / "fonts" / "licenses").glob("*.txt")}
    assert {"OFL-Onest.txt", "OFL-NotoSansHebrew.txt", "OFL-IBMPlexMono.txt"} <= licences
    for p in (PUBLIC / "fonts" / "licenses").glob("*.txt"):
        assert "SIL Open Font License" in p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Assets are served by the app, with and without ROOT_PATH
# ---------------------------------------------------------------------------

_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_PLACEHOLDERS = {
    "",
    "change-me-in-production",
    "generate-a-long-random-string",
    "min-32-chars-secret-change-me-for-production-use",
    "min-32-chars-secret-change-me!!",
    "at-least-32-characters-long-secret-key",
    "0123456789abcdef0123456789abcdef",
}


def _app_module():
    """Import the app with secrets generated for this run (none from the repo)."""
    if "app.main" not in sys.modules:
        values = {
            "ADMIN_API_KEY": secrets.token_urlsafe(32),
            "SESSION_SECRET": secrets.token_urlsafe(48),
            "APP_ENCRYPTION_KEY": secrets.token_hex(16),
        }
        for name, value in values.items():
            if value in _PLACEHOLDERS:
                raise RuntimeError(f"{name} is empty or a placeholder")
            os.environ.setdefault(name, value)
        os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379")
        os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DIR.name}/ui-guards.db")
        os.environ.setdefault("TZ", "UTC")
        sys.path.insert(0, str(ROOT))
    import app.main as main_mod  # noqa: E402

    return main_mod


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, k):
        return self.data.get(k)

    async def setex(self, k, _ttl, v):
        self.data[k] = v

    async def delete(self, k):
        self.data.pop(k, None)

    async def ping(self):
        return True

    async def aclose(self):
        pass


@pytest.fixture(params=["", "/auth"], ids=["no-root-path", "root-path"])
def served(request, monkeypatch):
    main_mod = _app_module()
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: _FakeRedis())
    monkeypatch.setattr(main_mod, "ROOT_PATH", request.param)
    monkeypatch.setattr(main_mod.app, "root_path", request.param)
    with TestClient(main_mod.app) as client:
        yield client, request.param


class _Refs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []
        self.base = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "base":
            self.base = a.get("href", "")
        for key in ("src", "href"):
            v = a.get(key)
            if v and (tag in ("script", "link", "img") or re.search(r"\.(?:css|js|svg|png|woff2|ico)(?:\?|$)", v)):
                self.refs.append(v)


PAGES_SERVED = ["/login", "/login-mfa", "/setup", "/setup-mfa", "/admin/tokens", "/admin/zones", "/admin/api-keys",
                "/admin/logs/activity", "/admin/logs/access"]


def test_assets_are_local_and_served(served):
    client, root = served
    wanted: set[str] = set()
    for page in PAGES_SERVED:
        r = client.get(root + page)
        assert r.status_code == 200, page
        parser = _Refs()
        parser.feed(r.text)
        if root:
            assert parser.base == root + "/", f"{page} has no <base href> under ROOT_PATH"
        base_url = "http://testserver" + (parser.base or "/")
        for ref in parser.refs:
            absolute = urljoin(urljoin("http://testserver" + root + page, base_url), ref)
            assert urlparse(absolute).netloc == "testserver", f"{page}: external {ref}"
            wanted.add(urlparse(absolute).path)
    # What head.js writes
    head = client.get(root + "/js/head.js")
    assert head.status_code == 200
    assert head.text.startswith(f"window.__BASE_PATH__ = {json.dumps(root)};"), "head.js lacks the base path line"
    for literal in re.findall(r"[\"'](/(?:css|images|js|fonts)/[\w.-]+)[\"']", SOURCES["js/head.js"]):
        wanted.add(root + literal)
    # What ui.css loads, relative to the stylesheet
    for ref in re.findall(r"url\(\s*['\"]?([^'\")]+)", SOURCES["css/ui.css"]):
        wanted.add(urlparse(urljoin("http://testserver" + root + "/css/ui.css", ref)).path)
    assert any(p.endswith("/css/ui.css") for p in wanted)
    assert any(p.endswith(".woff2") for p in wanted)
    for path in sorted(wanted):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"


def test_legacy_addresses_still_redirect(served):
    client, root = served
    for old, new in (("/admin", "/admin/tokens"), ("/admin/index.html", "/admin/tokens"), ("/admin/login.html", "/login"),
                     ("/admin/setup.html", "/setup"), ("/", "/admin/tokens")):
        r = client.get(root + old, follow_redirects=False)
        assert r.status_code in (302, 307), old
        assert r.headers["location"] == root + new, old
