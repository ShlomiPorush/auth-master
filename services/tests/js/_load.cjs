// Loads the portal's classic scripts into a bare vm context (no page), the way
// a browser shares one global scope between them. Used by the *.test.cjs files.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PUBLIC = path.join(__dirname, "..", "..", "static", "public");

// UI_TEXT_FILE: load this file in place of js/text.js, such as a copy of the
// catalog with every value changed, to prove that no test depends on the words
function scriptPath(file) {
    if (file === "js/text.js" && process.env.UI_TEXT_FILE) return path.resolve(process.env.UI_TEXT_FILE);
    return path.join(PUBLIC, file);
}

function loadScripts(files, extra) {
    const context = {
        console, Intl, Date, URL, URLSearchParams, Promise, Math, JSON, Number, String, Array, Object,
        setTimeout, clearTimeout, setInterval, clearInterval, CustomEvent,
        ...(extra || {}),
    };
    context.window = context;
    vm.createContext(context);
    for (const file of files) {
        const code = fs.readFileSync(scriptPath(file), "utf8");
        vm.runInContext(code, context, { filename: file });
    }
    return context;
}

module.exports = { loadScripts, PUBLIC };
