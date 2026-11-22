// A stand-in for Plasma's desktop scripting (org.kde.PlasmaShell.evaluateScript), for tests only.
// It is NOT Plasma: it knows only the panel properties, panels(), widgets() and print() that
// forge-appearance-dock uses, and it throws on anything else, so a wrong script fails the test.
// Usage: node fake_plasma.js SCRIPT_FILE PANELS_JSON   (the panels file is updated in place)
"use strict";
const fs = require("fs");
const vm = require("vm");

const [scriptFile, stateFile] = process.argv.slice(2);
const state = JSON.parse(fs.readFileSync(stateFile, "utf8"));
const ENUMS = {
  location: ["top", "bottom", "left", "right"],
  lengthMode: ["fill", "fit", "custom"],
  alignment: ["left", "center", "right"],
  hiding: ["none", "autohide", "dodgewindows", "windowsgobelow"],
  opacity: ["adaptive", "opaque", "translucent"],
};
const INTS = ["height", "maximumLength", "minimumLength", "offset"];
const BOOLS = ["floating", "floatingApplets"];

function check(name, value) {
  if (name in ENUMS) {
    if (!ENUMS[name].includes(value)) throw new Error("bad " + name + ": " + JSON.stringify(value));
  } else if (INTS.includes(name)) {
    if (!Number.isInteger(value)) throw new Error("bad " + name + ": " + JSON.stringify(value));
  } else if (BOOLS.includes(name)) {
    if (typeof value !== "boolean") throw new Error("bad " + name + ": " + JSON.stringify(value));
  } else {
    throw new Error("unknown panel property " + String(name));
  }
}

const objs = state.map(p => new Proxy({}, {
  get(_, name) {
    if (name === "id") return p.id;
    if (name === "widgets") return () => p.widgets.map(t => ({type: t}));
    if (Object.prototype.hasOwnProperty.call(p.props, name)) return p.props[name];
    throw new Error("unknown panel property " + String(name));
  },
  set(_, name, value) {
    check(name, value);
    p.props[name] = value;
    p.writes = (p.writes || 0) + 1;
    return true;
  },
}));

let out = "";
const sandbox = {panels: () => objs, print: s => { out += String(s); }};
vm.runInNewContext(fs.readFileSync(scriptFile, "utf8"), sandbox, {timeout: 2000});
fs.writeFileSync(stateFile, JSON.stringify(state));
process.stdout.write(out);
