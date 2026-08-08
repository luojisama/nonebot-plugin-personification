const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "nonebot_plugin_personification", "webui", "static", "app-admin.js"),
  "utf8",
);
const requested = [];
let renderCount = 0;
let enteredView = "";

const sandbox = {
  window: {},
  state: {view: "user_policy"},
  document: {
    createElement() { throw new Error("ensureAsset should be reused"); },
    head: {appendChild() { throw new Error("ensureAsset should be reused"); }},
  },
  ensureAsset: async filename => {
    requested.push(filename);
    if (filename === "app-identity-policy.js") {
      sandbox.window.renderUserPolicy = () => "ready";
    }
  },
  render: () => { renderCount += 1; },
  enterViewLifecycle: view => { enteredView = view; },
  alertFlash: (_kind, message) => { throw new Error(message); },
  Promise,
  Error,
  encodeURIComponent,
};

vm.runInNewContext(source, sandbox, {filename: "app-admin.js"});
assert.match(sandbox.window.renderUserPolicy(), /正在兼容当前已打开的旧标签页/);
assert.ok(sandbox.window.__personificationLegacyAdminReady instanceof Promise);

sandbox.window.__personificationLegacyAdminReady.then(() => {
  assert.deepStrictEqual(requested, [
    "app-admin-common.js",
    "app-dashboard.js",
    "app-health-qq.js",
    "app-qzone.js",
    "app-identity-policy.js",
    "app-persona-builder.js",
    "app-groups.js",
  ]);
  assert.strictEqual(sandbox.window.renderUserPolicy(), "ready");
  assert.strictEqual(renderCount, 1);
  assert.strictEqual(enteredView, "user_policy");
  console.log("Legacy WebUI admin hot-update compatibility checks passed");
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
