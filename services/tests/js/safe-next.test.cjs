// cleanNext / safeNext in js/common.js: the return path after signing in is
// always a page under <base>/admin/ of this site (plan section 2).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadScripts } = require("./_load.cjs");

const ORIGIN = "https://auth.example.test";
const ctx = loadScripts(["js/common.js"]);

test("pages under /admin/ are kept, with their query", () => {
    assert.equal(ctx.cleanNext("/admin/zones", "", ORIGIN), "/admin/zones");
    assert.equal(ctx.cleanNext("/admin/tokens?status=expiring&q=a%20b", "", ORIGIN), "/admin/tokens?status=expiring&q=a%20b");
    assert.equal(ctx.cleanNext("/admin/logs/access", "/auth", ORIGIN), "/admin/logs/access");
});

test("bypass attempts fall back to the default page", () => {
    const bad = [
        "/%09/evil.example", "/\t/evil.example", "//evil", "//evil.example/admin/x", "/\\evil", "/%5Cevil", "https://evil",
        "https://evil.example/admin/tokens", "javascript:alert(1)", "/%2F%2Fevil", "%2F%2Fevil", "/admin%2F..%2F..%2Flogin",
        "", "admin/tokens", "/login", "/", "/docs",
    ];
    for (const value of bad) assert.equal(ctx.cleanNext(value, "", ORIGIN), "", JSON.stringify(value));
});

test("a path outside the base path is refused", () => {
    assert.equal(ctx.cleanNext("/../admin/tokens", "/auth", ORIGIN), "");
    assert.equal(ctx.cleanNext("/admin/../../other/admin/x", "/auth", ORIGIN), "");
    assert.equal(ctx.cleanNext("/auth/admin/tokens", "/auth", ORIGIN), "", "no double prefix");
    assert.equal(ctx.cleanNext("/%2e%2e/admin/tokens", "/auth", ORIGIN), "");
});

test("safeNext reads ?next= of the page", () => {
    const page = loadScripts(["js/common.js"], {
        __BASE_PATH__: "/auth",
        location: { search: "?next=%2Fadmin%2Fzones%3Fq%3Dx", origin: ORIGIN },
    });
    assert.equal(page.safeNext(), "/admin/zones?q=x");
    const evil = loadScripts(["js/common.js"], { location: { search: "?next=%2F%09%2Fevil.example", origin: ORIGIN } });
    assert.equal(evil.safeNext(), "");
});
