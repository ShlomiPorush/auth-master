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

import html
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
    ("js/dashboard.js", "dashSearchSheet"): "the phone search over the whole screen (SR-8, a listed DS-24 exception), a layer of uiOpenLayers",
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
    "html.ui-nav-collapsed .ui-fly": "the page name beside a rail icon when the wide sidebar is folded (NL-7, NL-8)",
    ".ui-gs-panel": "the global search results under the top bar search field (SR-3)",
    ".ui-gs.is-sheet": "the phone search over the whole screen (SR-8, a listed DS-24 exception)",
    "html:has(.ui-dialog-backdrop) .ui-gs.is-sheet": "a question asked from the phone search comes over it (SR-7, SR-8)",
    ".ui-savebar": "the Settings save bar (SV-1), part of the page",
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
                    if (name, owner) in BUILDERS:
                        used.add((name, owner))
                    else:
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
    """Code snippets must appear in the pages' code; words live in the catalog
    (js/text.js), so a snippet of words must be the value of a catalog key that
    the code still uses at a call site (t, tn, tHtml, data-t...), not merely sit
    in the catalog. The writer may reword the catalog: a snippet the baseline
    maps to catalog keys ('catalog') is kept while one of those keys still has
    words and a call site, whatever the words are."""
    corpus = "\n".join(text for name, text in sources.items() if name.endswith((".html", ".js")) and name != TEXT_JS)
    catalog = catalog_entries(sources[TEXT_JS])[0]
    used = catalog_uses(sources, set(catalog))[0]
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
        held = item.get("catalog", {})
        for snippet in item.get("expect", []):
            if snippet in corpus:
                continue
            if snippet in held:
                wired = [k for k in held[snippet] if k in used and k in catalog and any(f.strip() for f in catalog_forms(catalog[k]))]
                if not wired:
                    problems.append(f"{key}: none of {', '.join(held[snippet])}, the catalog keys of {snippet!r}, has words and a call site ({item.get('old', '')})")
                continue
            words = html.unescape(snippet).replace('\\"', '"')
            # The old code form of a word: label: "Name", "Active", >Create<
            quoted = re.fullmatch(r'(?:\w+:\s*)?"(.*)"|>(.*)<', words)
            if quoted:
                words = quoted.group(1) if quoted.group(1) is not None else quoted.group(2)
            values = {k: v for k, v in catalog.items() for form in catalog_forms(v) if words in form}
            exact = [k for k, v in values.items() if words in catalog_forms(v) or any(words.strip() == f.strip() for f in catalog_forms(v))]
            wired = [k for k in (exact or list(values)) if k in used]
            if not values:
                problems.append(f"{key}: the new UI has no {snippet!r} ({item.get('old', '')})")
            elif not wired:
                problems.append(f"{key}: {snippet!r} is in the catalog ({', '.join(sorted(exact or values))}) but no code uses it ({item.get('old', '')})")
    return problems


def test_inventory_is_kept():
    assert inventory_problems(SOURCES) == []


def test_inventory_guard_catches():
    assert not any("tokens.row.delete.confirm" in p or "tokens.new" in p for p in inventory_problems(SOURCES))
    # The sentence stays in the catalog, but its call site is gone
    copy = dict(SOURCES)
    assert 't("tokens.delete-this-token-permanently")' in copy["js/dashboard.js"]
    copy["js/dashboard.js"] = copy["js/dashboard.js"].replace('t("tokens.delete-this-token-permanently")', '""')
    assert any("tokens.row.delete.confirm" in p for p in inventory_problems(copy))
    # A page's words lose their element
    copy = dict(SOURCES)
    copy["index.html"] = copy["index.html"].replace('data-t="tokens.new-token"', "")
    copy["js/dashboard.js"] = copy["js/dashboard.js"].replace('t("tokens.new-token")', '""')
    assert any("tokens.new" in p for p in inventory_problems(copy))
    # The words are gone from the catalog too: the key, or its words
    copy = dict(SOURCES)
    assert '"tokens.delete-this-token-permanently":' in copy[TEXT_JS]
    copy[TEXT_JS] = copy[TEXT_JS].replace('"tokens.delete-this-token-permanently":', '"tokens.removed-x":')
    assert any("tokens.row.delete.confirm" in p for p in inventory_problems(copy))
    copy = dict(SOURCES)
    copy[TEXT_JS] = re.sub(r'("tokens\.delete-this-token-permanently": )"[^"]*"', r'\1""', copy[TEXT_JS])
    assert any("tokens.row.delete.confirm" in p for p in inventory_problems(copy))
    copy = dict(SOURCES)
    copy["index.html"] = copy["index.html"].replace('data-inventory="tokens.search"', "")
    assert any("tokens.search" in p for p in inventory_problems(copy))


# ---------------------------------------------------------------------------
# Words: every word the portal shows lives in the catalog (js/text.js)
# ---------------------------------------------------------------------------

TEXT_JS = "js/text.js"
HTML_TEXT_ATTRS = ("aria-label", "title", "placeholder", "alt")


def js_strings(src: str) -> list[tuple[int, str]]:
    """(position, raw text) of every string literal, and of every static part of
    a template literal, outside comments and regex literals."""
    code, masked = js_lex(src)
    out: list[tuple[int, str]] = []
    i, n = 0, len(src)
    while i < n:
        c = code[i]
        if c in "'\"" and masked[i] == c:
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            out.append((i, src[i + 1:j]))
            i = j + 1
            continue
        if c == "`" and masked[i] == c:
            j = i + 1
            start = j
            while j < n and src[j] != "`":
                if src[j] == "\\":
                    j += 2
                    continue
                if src.startswith("${", j):
                    out.append((start, src[start:j]))
                    depth = 0
                    while j < n:
                        if src[j] == "{":
                            depth += 1
                        elif src[j] == "}":
                            depth -= 1
                            if depth == 0:
                                break
                        j += 1
                    start = j + 1
                j += 1
            out.append((start, src[start:j]))
            i = j + 1
            continue
        i += 1
    return out


def js_unescape(raw: str) -> str:
    return re.sub(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] in "ux" and len(m.group(1)) > 1
                  else {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), raw)


_WORD = r"[A-Za-z][A-Za-z']*"
# Two words in a row: "Save failed", "API keys", "Verify & save"; not class lists,
# selectors, paths or attribute names (ui-btn ui-btn-sm, a[href], data-x y=)
_TWO_WORDS = re.compile(rf"(?<![\w.#\[\-:/$@=%]){_WORD}[,.:;!?)]?(?:\s+&)?\s+\(?{_WORD}(?![\w\-(=/\[])")
# One capitalised word, as visible text: "Delete", "Name:", "Expired"
_CAPITALISED = re.compile(r"^\s*[A-Z][a-z]+[.!?:…\"']*\s*$")
# A word joined to other text by a space: " rows", "Exp: ", "the "
_EDGE_WORD = re.compile(r"^\s+[A-Za-z]+|[A-Za-z]{2,}[:,.]?\s+$")


def looks_like_prose(text: str) -> bool:
    if _TWO_WORDS.search(text) or _CAPITALISED.match(text):
        return True
    return bool(_EDGE_WORD.search(text)) and not re.search(r"\w-\w", text)


def markup_parts(value: str) -> tuple[list[str], list[str]] | None:
    """For a string that holds a piece of HTML: (visible text between tags,
    words inside attributes). None when the string is not markup. A piece may
    start inside a tag or an attribute value that another string opened."""
    if not re.search(r"<[A-Za-z/]|=\"|^\"|\">", value):
        return None
    texts = []
    # Text: after each '>' up to the next '<'; before the first tag when no tag is open
    for m in re.finditer(r">([^<>]*)", value):
        texts.append(m.group(1))
    lt, gt = value.find("<"), value.find(">")
    if lt > 0 and (gt == -1 or lt < gt) and '"' not in value[:lt] and "=" not in value[:lt]:
        texts.append(value[:lt])
    words = [m.group(2) for m in re.finditer(r"\b(aria-label|title|placeholder|alt)=\"([^\"]*)", value)]
    # The end of an attribute value another string opened: words only when they read as prose
    first_quote = value.find('"')
    if first_quote > 0 and (lt == -1 or first_quote < lt) and (gt == -1 or first_quote < gt):
        tail = value[:first_quote]
        if not tail.rstrip().endswith("=") and looks_like_prose(tail):
            words.append(tail)
    return texts, words


# (file, exact string) -> why it is not words the portal shows. Keep it short:
# the heuristic above should tell code from words.
JS_TEXT_ALLOWED: dict[tuple[str, str], str] = {
    ("js/ui.js", "(hover: none) and (pointer: coarse)"): "a media query",
    ("js/ui.js", "input, select, textarea"): "a CSS selector list",
    ("js/ui.js", "button, a[href], input, select, textarea, [tabindex]"): "a CSS selector list",
    ("js/ui.js", "[autofocus], input:not([type=hidden]):not([type=checkbox]), select, textarea"): "a CSS selector list",
    ("js/ui.js", "button, a[href], input, [tabindex]"): "a CSS selector list",
    ("js/dashboard.js", "button, a, input, select, label"): "a CSS selector list",
    ("js/ui.js", "uiDialog: size must be sm, md or wide"): "an Error thrown at a programming mistake, never shown",
    ("js/ui.js", "uiDialog: a dialog needs a close function for its X"): "an Error thrown at a programming mistake, never shown",
    ("js/auth-pages.js", "Bearer "): "the HTTP Authorization scheme",
}
# Code words that look like a capitalised word or a joined word
JS_CODE_WORDS = {
    "Escape", "Enter", "Tab", "Home", "End",  # KeyboardEvent.key names
    " checked", " selected", " disabled", " hidden", " readonly", " required",  # boolean attributes added to a tag
}


def js_text_problems(sources: dict[str, str]) -> list[str]:
    problems = []
    seen_allowed = set()
    for name, src in sources.items():
        if not name.endswith(".js") or name == TEXT_JS:
            continue
        for pos, raw in js_strings(src):
            value = js_unescape(raw)
            if not re.search(r"[A-Za-z]", value):
                continue
            if (name, value) in JS_TEXT_ALLOWED:
                seen_allowed.add((name, value))
                continue
            if value in JS_CODE_WORDS:
                continue
            parts = markup_parts(value)
            if parts is None:
                bad = looks_like_prose(value)
            else:
                texts, attrs = parts
                bad = any(re.search(r"[A-Za-z]{2}", t) for t in texts) or any(re.search(r"[A-Za-z]", a) for a in attrs)
            if bad:
                line = src.count("\n", 0, pos) + 1
                problems.append(f"{name}:{line}: words outside the catalog: {value[:80]!r}")
    for key in JS_TEXT_ALLOWED:
        if key not in seen_allowed:
            problems.append(f"stale JS_TEXT_ALLOWED entry: {key}")
    return problems


class _PageText(HTMLParser):
    """Text nodes and user-facing attributes of a page that are not in the catalog."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict]] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.stack and self.stack[-1][1].get("data-t") is not None:
            self.problems.append(f"<{tag}> inside an element with data-t={self.stack[-1][1]['data-t']!r}: data-t sets the whole content")
        for name in HTML_TEXT_ATTRS:
            if a.get(name):
                self.problems.append(f"<{tag} {name}={a[name]!r}>: the attribute is not from the catalog (use data-t-attr)")
        if tag == "meta" and (a.get("name") or "").lower() == "description" and a.get("content"):
            self.problems.append(f"<meta name=description content={a['content']!r}>: use data-t-attr=\"content:key\"")
        if tag == "title" and not a.get("data-t"):
            self.problems.append("<title> without data-t")
        if a.get("data-t-slot") is not None and not (self.stack and self.stack[-1][1].get("data-t-tpl")):
            self.problems.append(f"<{tag} data-t-slot> outside a data-t-tpl element")
        if tag not in ("meta", "link", "img", "input", "br", "hr", "path", "circle", "source"):
            self.stack.append((tag, a))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def handle_data(self, data):
        if not data.strip() or (self.stack and self.stack[-1][0] in ("style", "script")):
            return
        where = self.stack[-1][0] if self.stack else "#root"
        self.problems.append(f"<{where}>: text {data.strip()[:60]!r} is not from the catalog (use data-t)")


def html_text_problems(sources: dict[str, str]) -> list[str]:
    problems = []
    for name, src in sources.items():
        if not name.endswith(".html"):
            continue
        parser = _PageText()
        parser.feed(src)
        problems += [f"{name}: {p}" for p in parser.problems]
        if f'src="/js/{TEXT_JS.split("/")[1]}"' not in src:
            problems.append(f"{name}: does not load {TEXT_JS}")
        else:
            first = re.search(r"<script\b[^>]*\bsrc=\"([^\"]+)\"", src)
            if not first or not first.group(1).endswith("/" + TEXT_JS):
                problems.append(f"{name}: {TEXT_JS} is not the first script")
    return problems


def catalog_forms(value) -> list[str]:
    return [value] if isinstance(value, str) else [value.get("one", ""), value.get("other", "")]


def catalog_entries(text_js: str) -> tuple[dict, list[str]]:
    """UI_TEXT of js/text.js as a dict, and the problems of its shape."""
    m = re.search(r"\bvar\s+UI_TEXT\s*=\s*(\{.*?\n\});", text_js, re.S)
    if not m:
        return {}, ["js/text.js has no var UI_TEXT = {...};"]
    problems = []

    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                problems.append(f"UI_TEXT repeats the key {k!r}")
            out[k] = v
        return out

    # Whole-line // comments (the writer's briefs above new keys) are not data
    body = re.sub(r"(?m)^[ \t]*//[^\n]*\n", "", m.group(1))
    try:
        catalog = json.loads(re.sub(r",(\s*[}\]])", r"\1", body), object_pairs_hook=pairs)
    except ValueError as e:
        return {}, [f"UI_TEXT is not plain data: {e}"]
    for k, v in catalog.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+", k):
            problems.append(f"UI_TEXT key {k!r} is not a dotted lowercase key")
        # A value is a whole unit the writer can rewrite alone: no piece of a sentence
        for form in catalog_forms(v) if isinstance(v, (str, dict)) else []:
            if form != form.strip() or re.match(r"[.,;:)]", form):
                problems.append(f"UI_TEXT {k!r} = {form!r} is a piece of a sentence; use one value with {{placeholders}}")
        if isinstance(v, dict):
            if set(v) != {"one", "other"} or not all(isinstance(x, str) for x in v.values()):
                problems.append(f"UI_TEXT {k!r}: a count value is {{one, other}}")
        elif not isinstance(v, str):
            problems.append(f"UI_TEXT {k!r}: a value is a string or {{one, other}}")
    return catalog, problems


# Keys the code builds from a value: prefix -> why, and the code that builds it
DYNAMIC_KEYS: dict[str, str] = {
    "tokens.flag.": "the permission checkboxes of the token dialog name each of TOKEN_FLAGS (read, write, delete)",
    "tokens.level.": "the grant tags of the token list name each flag of a grant level",
    # uiIdWord(prefix, id): an identifier from the API shown as a word, the raw id when unknown
    "logs.action.": "the Action column of the Activity Log names each audit action the server writes",
    "logs.entity.": "the Entity Type column of the Activity Log names each entity type the server writes",
    "logs.level.": "Access Logs, Usage and grant changes name each level a /validate call or a grant can carry",
    "logs.actor.": "the Actor column names the system actors (system, setup, master_api_key, environment) besides user names",
    "db.sslmode.": "the database summary and the recovery page name the saved TLS mode",
}


def catalog_uses(sources: dict[str, str], keys: set[str]) -> tuple[set[str], list[str]]:
    """Keys used by code and pages, and calls of keys that do not exist."""
    used: set[str] = set()
    problems = []
    for name, src in sources.items():
        if name == TEXT_JS:
            continue
        if name.endswith(".js"):
            code = js_lex(src)[0]
            for m in re.finditer(r"\b(t|tn|tHtml)\(\s*([\"'])([^\"'\n]*)\2\s*([+),])", code):
                key = m.group(3)
                if m.group(4) == "+":
                    if key not in DYNAMIC_KEYS:
                        problems.append(f"{name}: {m.group(1)}({key!r} + ...) builds a key with a prefix not in DYNAMIC_KEYS")
                    used.add(key)
                    continue
                used.add(key)
                if key not in keys:
                    problems.append(f"{name}: {m.group(1)}({key!r}) names a key that UI_TEXT does not have")
            for m in re.finditer(r"\buiIdWord\(\s*([\"'])([^\"'\n]*)\1", code):
                if m.group(2) not in DYNAMIC_KEYS:
                    problems.append(f"{name}: uiIdWord({m.group(2)!r}, ...) uses a prefix not in DYNAMIC_KEYS")
                used.add(m.group(2))
            # Label tables hold keys; their readers pass them to t()
            for _pos, raw in js_strings(src):
                if raw in keys:
                    used.add(raw)
        elif name.endswith(".html"):
            for m in re.finditer(r"\bdata-t(?:-tpl)?=\"([^\"]*)\"", src):
                used.add(m.group(1))
                if m.group(1) not in keys:
                    problems.append(f"{name}: data-t {m.group(1)!r} is not in UI_TEXT")
            for m in re.finditer(r"\bdata-t-attr=\"([^\"]*)\"", src):
                for pair in m.group(1).split(";"):
                    attr, _sep, key = pair.partition(":")
                    key = key.strip()
                    used.add(key)
                    if key not in keys or attr.strip() not in HTML_TEXT_ATTRS + ("content",):
                        problems.append(f"{name}: data-t-attr {pair!r} names no key or a wrong attribute")
    return used, problems


def catalog_problems(sources: dict[str, str]) -> list[str]:
    catalog, problems = catalog_entries(sources[TEXT_JS])
    keys = set(catalog)
    used, use_problems = catalog_uses(sources, keys)
    problems += use_problems
    for prefix, reason in DYNAMIC_KEYS.items():
        if prefix not in used:
            problems.append(f"DYNAMIC_KEYS lists {prefix!r}, which no code builds")
        if not reason:
            problems.append(f"DYNAMIC_KEYS {prefix!r} has no reason")
        used |= {k for k in keys if k.startswith(prefix)}
    for key in sorted(keys - used):
        problems.append(f"UI_TEXT {key!r} is used nowhere")
    # Every value is written by the writer before it ships: an empty one shows nothing
    for key, value in sorted(catalog.items()):
        forms = value.values() if isinstance(value, dict) else [value]
        if any(not isinstance(v, str) or not v.strip() for v in forms):
            problems.append(f"UI_TEXT {key!r} has no text")
    return problems


# Where words reach the user. Each must take a t()/tn()/tHtml() result, a
# variable or a value helper, never a literal with letters.
_LITERAL = r"([\"'`])(?=[^\"'`\n]*[A-Za-z])"
SINK_PATTERNS = {
    "text property": rf"\.(?:textContent|innerText)\s*=\s*{_LITERAL}",
    "document.title": rf"\bdocument\.title\s*=\s*{_LITERAL}",
    "attribute": rf"\.setAttribute\(\s*[\"'](?:aria-label|title|placeholder|alt)[\"']\s*,\s*{_LITERAL}",
    "builder text": rf"\b(?:showToast|authShowError|dashLoading|dashFailed|dashEmpty|dashTag|dashMenuButton|uiPasskeyName)\(\s*{_LITERAL}",
    "builder text (second argument)": rf"\b(?:authSetError|authFieldError|apiError|dashPanelFail|dashSetAlert|dashLimitAlert)\([^,()]*,\s*{_LITERAL}",
    "builder text (third argument)": rf"\b(?:authBusy|dashSortButtonSync)\([^,()]*,[^,()]*,\s*{_LITERAL}",
    "sort menu name": rf"\buiSortMenu\([^,()]*,\s*{_LITERAL}",
}
# Object properties that builders show (uiConfirm, uiDialog, uiMenu, uiPicker...)
SINK_PROPERTIES = ("title", "message", "confirmText", "cancelText", "label", "reason", "hint", "text", "sub", "loadingText", "failedTitle",
                   "emptyText", "failedText", "pkText", "submitText")


def sink_problems(sources: dict[str, str]) -> list[str]:
    keys = set(catalog_entries(sources[TEXT_JS])[0])
    problems = []
    for name, src in sources.items():
        if not name.endswith(".js") or name == TEXT_JS:
            continue
        code = js_lex(src)[0]
        for kind, pattern in SINK_PATTERNS.items():
            for m in re.finditer(pattern, code):
                line = code.count("\n", 0, m.start()) + 1
                problems.append(f"{name}:{line}: {kind} gets a literal: {code[m.start():m.start() + 70]!r}")
        for m in re.finditer(rf"[{{,]\s*({'|'.join(SINK_PROPERTIES)})\s*:\s*([\"'`])((?:\\.|(?!\2).)*)\2", code):
            value = js_unescape(m.group(3))
            # a label table holds a catalog key, and its reader passes it to t()
            if re.search(r"[A-Za-z]", value) and value not in keys:
                line = code.count("\n", 0, m.start()) + 1
                problems.append(f"{name}:{line}: {m.group(1)}: {value[:60]!r} is a literal, not a catalog key or t() result")
    return problems


# One catalog value is one whole unit the writer can rewrite on its own: code
# never joins two catalog results, or a catalog result and words or punctuation,
# into one visible string. A template with {placeholders} (or tHtml) does that.
_CATALOG_CALL = re.compile(r"(?:\besc\(\s*)?\b(?:t|tn|tHtml)\(")


def _close(masked: str, i: int) -> int:
    """Index after the bracket that closes the one at i."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = []
    j = i
    while j < len(masked):
        c = masked[j]
        if c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return j + 1
        elif c in "'\"`":
            k = masked.find(c, j + 1)
            j = k if k != -1 else len(masked)
        j += 1
    return len(masked)


def _operand(code: str, masked: str, i: int) -> tuple[str, str, int] | None:
    """The operand of a + chain at i: (kind, text, end); kind is lit, cat, group or val."""
    n = len(masked)
    while i < n and masked[i].isspace():
        i += 1
    if i >= n:
        return None
    c = masked[i]
    if c in "'\"`":
        j = masked.find(c, i + 1)
        j = n if j == -1 else j
        return "lit", js_unescape(code[i + 1:j]), j + 1
    if c == "(":
        j = _close(masked, i)
        return "group", code[i:j], j
    m = re.match(r"[\w$.!]+", masked[i:])
    if not m:
        return None
    j = i + m.end()
    while j < n and (masked[j] in "([" or (masked[j] == "." and re.match(r"[\w$]", masked[j + 1:j + 2]))):
        if masked[j] == ".":
            j = j + 1 + re.match(r"[\w$]+", masked[j + 1:]).end()
        else:
            j = _close(masked, j)
    text = code[i:j]
    return ("cat" if _CATALOG_CALL.match(text) else "val"), text, j


def _visible(literal: str) -> str:
    """The words and punctuation of a string piece, without markup."""
    s = re.sub(r"<[^<>]*>", "", literal)
    head = s.split(">", 1)
    if len(head) == 2 and "<" not in head[0]:
        s = head[1]
    s = re.sub(r"<[^>]*$", "", s)
    if ("=" in s or '"' in s) and re.fullmatch(r"[\s\"]*(?:[\w:-]+(?:=\"[^\"]*\"?)?[\s\"]*)*", s):
        return ""
    return s.replace("&nbsp;", " ").strip()


# Tags that sit inside a sentence: they do not end it
_INLINE_TAGS = {"code", "bdi", "a", "em", "strong", "time", "kbd"}


def _markup(literal: str) -> bool:
    """Markup that ends a visible unit: a block tag, the end of a tag, or a move
    to another attribute. Inline tags (code, bdi, a...) sit inside a sentence."""
    tags = re.findall(r"<\s*/?\s*([a-zA-Z][\w-]*)", literal)
    if tags:
        return any(tag.lower() not in _INLINE_TAGS for tag in tags)
    return bool(re.search(r"[<>]|=\"|^\"|\"$", literal))


def _group_has(text: str, what: str) -> bool:
    if what == "cat":
        # a catalog result in the group, unless markup comes first and starts a new unit
        first = _CATALOG_CALL.search(js_lex(text)[0])
        if not first:
            return False
        return not any(p < first.start() and _markup(js_unescape(raw)) for p, raw in js_strings(text))
    for p, raw in js_strings(text):
        value = js_unescape(raw)
        if re.search(r"\b(?:t|tn|tHtml|uiIdWord)\(\s*$", text[:p]):
            continue  # a catalog key, not words
        if _visible(value):
            return True
        if _markup(value):
            return False
    return False


def concat_problems(sources: dict[str, str]) -> list[str]:
    problems = []
    for name, src in sources.items():
        if not name.endswith(".js") or name == TEXT_JS:
            continue
        code, masked = js_lex(src)
        starts = {m.start() for m in _CATALOG_CALL.finditer(code)}
        starts |= {p for p, raw in js_strings(src) if masked[p] in "'\"" and _visible(js_unescape(raw))}
        reported = set()
        for start in sorted(starts):
            first = _operand(code, masked, start)
            if not first or first[0] in ("val", "group"):
                continue
            kind, text, end = first
            # Along the + chain to the right, up to markup that ends the visible unit
            j = end
            while True:
                k = j
                while k < len(masked) and masked[k].isspace():
                    k += 1
                if k >= len(masked) or masked[k] != "+" or masked[k + 1:k + 2] in ("+", "="):
                    break
                nxt = _operand(code, masked, k + 1)
                if not nxt:
                    break
                nkind, ntext, nend = nxt
                bad = ""
                if nkind == "lit":
                    if kind == "cat" and _visible(ntext):
                        bad = f"joins {text[:45]!r} and the words {ntext[:40]!r}"
                elif nkind == "cat" or (nkind == "group" and _group_has(ntext, "cat")):
                    bad = f"joins {text[:45]!r} and {ntext[:45]!r}"
                elif nkind == "group" and kind == "cat" and _group_has(ntext, "lit"):
                    bad = f"joins {text[:45]!r} and words in {ntext[:45]!r}"
                if bad:
                    line = code.count("\n", 0, start) + 1
                    if (line, bad) not in reported:
                        reported.add((line, bad))
                        problems.append(f"{name}:{line}: {bad}")
                    break
                if nkind == "lit" and _markup(ntext):
                    break
                j = nend
        for m in re.finditer(r"`[^`]*`", code):
            body = m.group(0)[1:-1]
            if re.search(r"\$\{\s*(?:esc\()?\s*(?:t|tn|tHtml)\(", body) and _visible(re.sub(r"\$\{[^}]*\}", "", body)):
                problems.append(f"{name}:{code.count(chr(10), 0, m.start()) + 1}: a template literal joins catalog text and words")
    return problems


# Identifiers the server writes and the UI shows as words: each has its key
APP = ROOT / "app"


def read_app_sources() -> dict[str, str]:
    return {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8") for p in sorted(APP.rglob("*.py"))}


def server_identifiers(app_sources: dict[str, str]) -> dict[str, set[str]]:
    """prefix -> the values the server can emit for it, read from services/app."""
    code = "\n".join(app_sources.values())
    found: dict[str, set[str]] = {
        "logs.action.": set(re.findall(r"\baction\s*=\s*\"([\w.]+)\"", code)),
        "logs.entity.": set(re.findall(r"\bentity_type\s*=\s*\"([\w.]+)\"", code)),
        "logs.actor.": {"system"} | set(re.findall(r"\bactor\s*=\s*\"(\w+)\"", code))
        | set(re.findall(r"await _log\([^,]+,\s*\"(\w+)\"", code)) | set(re.findall(r"\b\w*_ACTOR\s*=\s*\"(\w+)\"", code)),
        "db.sslmode.": set(),
        "logs.level.": {"grants"},
    }
    # Levels a /validate call may name (routers/validate.py), and the grants check
    m = re.search(r"if level not in \(([^)]*)\)", code)
    if m:
        found["logs.level."] |= set(re.findall(r"\"(\w+)\"", m.group(1)))
    # TLS modes a saved PostgreSQL connection may have
    m = re.search(r"\bSSLMODES\s*=\s*\(([^)]*)\)", code)
    if m:
        found["db.sslmode."] |= set(re.findall(r"\"([\w-]+)\"", m.group(1)))
    return found


def server_identifier_problems(app_sources: dict[str, str], catalog: dict) -> list[str]:
    problems = []
    for prefix, values in server_identifiers(app_sources).items():
        if not values:
            problems.append(f"found no values for {prefix!r} in services/app; the reader of server_identifiers is out of date")
        for value in sorted(values):
            key = prefix + value
            if key not in catalog:
                problems.append(f"the server can emit {value!r}, but UI_TEXT has no {key!r}")
            elif not any(f.strip() for f in catalog_forms(catalog[key])):
                problems.append(f"UI_TEXT {key!r} has no words for {value!r}")
    return problems


def test_every_identifier_the_server_emits_has_a_word():
    catalog = catalog_entries(SOURCES[TEXT_JS])[0]
    assert server_identifier_problems(read_app_sources(), catalog) == []


def test_identifier_guard_catches_a_new_server_value():
    catalog = catalog_entries(SOURCES[TEXT_JS])[0]
    app = read_app_sources()
    app["app/new.py"] = 'await log_activity(db, actor=actor, action="token_rotate", entity_type="gadget")'
    problems = server_identifier_problems(app, catalog)
    assert any("'logs.action.token_rotate'" in p for p in problems)
    assert any("'logs.entity.gadget'" in p for p in problems)
    # A known identifier whose word is empty
    assert "logs.action.token_create" in catalog
    problems = server_identifier_problems(read_app_sources(), {**catalog, "logs.action.token_create": ""})
    assert any("'logs.action.token_create'" in p for p in problems)


def test_catalog_text_is_never_joined_into_one_string():
    assert concat_problems(SOURCES) == []


CONCAT_BYPASSES = [
    'function j1() { return t("tokens.name") + t("tokens.status"); }',
    'function j2(n) { return t("tokens.name") + ": " + n; }',
    'function j3(n) { return esc(t("tokens.name")) + " <code>" + esc(n) + "</code>" + esc(t("tokens.status")); }',
    'function j4(n) { return "Total: " + n + t("tokens.name"); }',
    'function j5(a) { return t("tokens.name") + (a ? t("tokens.status") : ""); }',
    'function j6(a) { return esc(t("tokens.name")) + (a ? " (more)" : ""); }',
    "function j7(a) { return `${t(\"tokens.name\")} and more`; }",
    'function j8(n) { return tn("tokens.count-tokens", n) + " match"; }',
]


@pytest.mark.parametrize("extra", CONCAT_BYPASSES)
def test_concat_guard_catches(extra):
    assert concat_problems(_with("js/dashboard.js", extra)), f"not caught: {extra}"


def test_concat_guard_lets_separate_units_through():
    code = ('function ok1(n, w) { return "<b>" + esc(t("tokens.name")) + "</b> " + esc(n) + "<br><b>" + esc(t("tokens.status")) + "</b> " + esc(w)'
            ' + \'<span class="ui-unit">\' + esc(t("tokens.name")) + " </span>" + uiTime(n); }')
    assert concat_problems(_with("js/dashboard.js", code)) == []


@pytest.mark.parametrize("value", [" Nothing was saved.", "Exp: ", ". Use an empty database.", "; older logs are deleted)"])
def test_catalog_guard_catches_a_piece_of_a_sentence(value):
    copy = dict(SOURCES)
    copy[TEXT_JS] = copy[TEXT_JS].replace('"tokens.name": ', '"tokens.name": ' + json.dumps(value) + ', "tokens.unused-x": ', 1)
    assert any("piece of a sentence" in p for p in catalog_problems(copy))


def test_pages_take_their_words_from_the_catalog():
    assert html_text_problems(SOURCES) == []


def test_scripts_take_their_words_from_the_catalog():
    assert js_text_problems(SOURCES) == []


def test_every_place_that_shows_words_takes_catalog_text():
    assert sink_problems(SOURCES) == []


def test_catalog_keys_exist_are_used_and_unique():
    assert catalog_problems(SOURCES) == []


def test_js_strings_reads_literals_and_template_parts():
    found = [js_unescape(raw) for _p, raw in js_strings("var a = 'x\\'y'; // 'no'\nvar b = `p ${q + 1} s`; var r = /'re'/;")]
    assert found == ["x'y", "p ", " s"]


HTML_TEXT_BYPASSES = [
    ("index.html", "<p>Hello there</p>"),
    ("login.html", '<button type="button" aria-label="Close"></button>'),
    ("login.html", '<input placeholder="Your name" />'),
    ("index.html", '<img src="/images/favicon.svg" alt="Logo" />'),
    ("index.html", '<span title="More">x</span>'),
    ("setup.html", '<b data-t="setup.continue">Continue</b>'),
    ("setup.html", '<p data-t="setup.continue"><span></span></p>'),
    ("login-mfa.html", '<meta name="description" content="Sign in" />'),
]


@pytest.mark.parametrize("name,extra", HTML_TEXT_BYPASSES)
def test_page_word_guard_catches(name, extra):
    assert html_text_problems(_with(name, extra)), f"not caught: {extra}"


def test_page_word_guard_catches_a_title_and_a_late_catalog():
    copy = dict(SOURCES)
    copy["login.html"] = re.sub(r"<title[^>]*></title>", "<title>Sign In</title>", copy["login.html"])
    assert any("<title>" in p for p in html_text_problems(copy))
    copy = dict(SOURCES)
    copy["login.html"] = copy["login.html"].replace('<script src="/js/text.js"></script>', "").replace(
        '<script src="/js/auth-pages.js"></script>', '<script src="/js/auth-pages.js"></script><script src="/js/text.js"></script>')
    assert any("not the first script" in p for p in html_text_problems(copy))


JS_TEXT_BYPASSES = [
    'function w1() { showToast("Token saved", "success"); }',
    'function w2(el) { el.innerHTML = "<p>No tokens yet</p>"; }',
    "function w3(el) { el.innerHTML = '<button title=\"Close\">x</button>'; }",
    'function w4() { return "Delete"; }',
    'function w5(n) { return n + " rows"; }',
    "function w6(n) { return `Showing ${n} of many`; }",
    'function w7(el) { return \'<span class="ui-unit">Created</span>\'; }',
    'function w8(name) { return \'" aria-label="Search \' + name; }',
]


@pytest.mark.parametrize("extra", JS_TEXT_BYPASSES)
def test_script_word_guard_catches(extra):
    assert js_text_problems(_with("js/dashboard.js", extra)), f"not caught: {extra}"


def test_script_word_guard_lets_code_through_and_reports_stale_entries():
    code = ('function c1(el) { el.classList.add("ui-btn"); el.className = "ui-btn ui-btn-sm"; el.setAttribute("data-x", "1"); el.querySelector("[data-x]");'
            ' return \'<div class="ui-td ui-td-main" role="cell">\' + esc(t("tokens.name")) + "</div>"; }')
    assert js_text_problems(_with("js/dashboard.js", code)) == []
    copy = dict(SOURCES)
    copy["js/auth-pages.js"] = copy["js/auth-pages.js"].replace('"Bearer "', '"Token "')
    assert any("stale JS_TEXT_ALLOWED" in p for p in js_text_problems(copy))


SINK_BYPASSES = [
    'function s1(el) { el.textContent = "Saved"; }',
    'function s2(el) { el.setAttribute("aria-label", "close"); }',
    'function s3() { showToast("saved", "success"); }',
    'function s4() { uiConfirm({ title: "Sure?", message: t("tokens.delete-this-token-permanently") }); }',
    'function s5() { return [{ label: "copy", action: "x" }]; }',
    'function s6() { document.title = "Tokens"; }',
    'function s7(b) { authBusy(b, true, "saving"); }',
    'function s8(el) { el.innerText = `x`; }',
    'function s9(b) { uiSortMenu(b, "tokens", [], {}, null); }',
    'function s10() { dashSortButtonSync("x", {}, "zones", []); }',
]


@pytest.mark.parametrize("extra", SINK_BYPASSES)
def test_sink_guard_catches(extra):
    assert sink_problems(_with("js/dashboard.js", extra)), f"not caught: {extra}"


def test_sink_guard_lets_keys_values_and_t_through():
    code = ('function s9(el, name) { el.textContent = t("tokens.name"); el.textContent = name; el.textContent = "";'
            ' el.setAttribute("aria-label", t("tokens.name")); showToast(t("tokens.token-updated"), "success");'
            ' return [{ label: "tokens.name", action: "x" }]; }')
    assert sink_problems(_with("js/dashboard.js", code)) == []


CATALOG_BYPASSES = [
    ("js/dashboard.js", 'function k1() { return t("tokens.no-such-key"); }', "names a key"),
    ("js/dashboard.js", 'function k2(x) { return t("tokens.made-up." + x); }', "DYNAMIC_KEYS"),
    ("index.html", '<p data-t="shell.no-such-key"></p>', "not in UI_TEXT"),
    ("index.html", '<p data-t-attr="title:shell.no-such-key"></p>', "names no key"),
]


@pytest.mark.parametrize("name,extra,expect", CATALOG_BYPASSES)
def test_catalog_guard_catches(name, extra, expect):
    assert any(expect in p for p in catalog_problems(_with(name, extra))), f"not caught: {extra}"


def test_catalog_guard_catches_unused_and_repeated_keys():
    copy = dict(SOURCES)
    copy[TEXT_JS] = copy[TEXT_JS].replace("var UI_TEXT = {", 'var UI_TEXT = {\n    "tokens.never-used": "x",\n    "tokens.name": "Twice",', 1)
    problems = catalog_problems(copy)
    assert any("'tokens.never-used' is used nowhere" in p for p in problems)
    assert any("repeats the key 'tokens.name'" in p for p in problems)


def test_catalog_guard_catches_an_empty_value():
    copy = dict(SOURCES)
    copy[TEXT_JS] = re.sub(r'("tokens\.name": )"[^"\n]*"', r'\1""', copy[TEXT_JS], count=1)
    assert any("'tokens.name' has no text" in p for p in catalog_problems(copy))


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
    ("js/ui.js", "writeClock"): "writes a time on the browser's own clock for Time Format 'Browser'",
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
RADIUS_PROPS = re.compile(r"^border(?:-[a-z-]+)?-radius$")


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
            if RADIUS_PROPS.match(prop):
                rest = re.sub(r"var\([^()]*\)", " ", value)
                if any(tok not in ("0", "inherit") for tok in rest.replace("/", " ").split()):
                    problems.append(f"{selector}: radius literal in {prop}: {value}")
    return problems


def test_ui_css_uses_only_tokens():
    assert token_problems(SOURCES["css/ui.css"]) == []


@pytest.mark.parametrize("extra", [
    ".x { color: #7c3aed; }",
    ".x { font-size: 13px; }",
    ".x { padding: 7px 10px; }",
    ".x { margin-inline-start: calc(var(--ui-sp-2) + 3px); }",
    ".x { border-radius: 14px; }",
    ".x { border-start-end-radius: var(--ui-radius-xs) 3px; }",
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
                "/admin/logs/activity", "/admin/logs/access", "/admin/settings", "/admin/account"]


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
