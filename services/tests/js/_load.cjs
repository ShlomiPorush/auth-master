// Loads the portal's classic scripts into a bare vm context (no page), the way
// a browser shares one global scope between them. Used by the *.test.cjs files.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PUBLIC = path.join(__dirname, "..", "..", "static", "public");

function loadScripts(files, extra) {
    const context = {
        console, Intl, Date, URL, URLSearchParams, Promise, Math, JSON, Number, String, Array, Object,
        setTimeout, clearTimeout, setInterval, clearInterval, CustomEvent,
        ...(extra || {}),
    };
    context.window = context;
    vm.createContext(context);
    for (const file of files) {
        const code = fs.readFileSync(path.join(PUBLIC, file), "utf8");
        vm.runInContext(code, context, { filename: file });
    }
    return context;
}

module.exports = { loadScripts, PUBLIC };
