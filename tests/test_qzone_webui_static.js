"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const core = fs.readFileSync(path.join(root, "webui", "static", "app-core.js"), "utf8");
const admin = fs.readFileSync(path.join(root, "webui", "static", "app-admin.js"), "utf8");
const css = fs.readFileSync(path.join(root, "webui", "static", "style.css"), "utf8");

assert.match(core, /personification_qzone_operation_id_v1/);
assert.match(core, /qzoneOperationId:\s*readStoredQzoneOperationId\(\)/);
assert.match(core, /api\("\/qzone\/status", \{cache:"no-store"\}\)/);
assert.match(core, /stopQzoneViewLifecycle/);

const persistAt = admin.indexOf("setQzoneOperationId(globalThis.crypto");
const publishAt = admin.indexOf('api("/qzone/post-now"');
assert.ok(persistAt >= 0 && persistAt < publishAt, "Operation ID must persist before POST");
assert.match(admin, /\["succeeded", "definite_failure"\]/);
assert.match(admin, /\["reserved", "dispatching", "unknown"\]/);
assert.match(admin, /\/qzone\/operations\/\$\{encodeURIComponent\(operationId\)\}/);
assert.match(admin, /q\.auth_by_bot/);
assert.match(admin, /q\.capabilities_by_bot/);
assert.match(admin, /qzone\.cookie_export/);
assert.match(admin, /qzone\.web_read/);
assert.match(admin, /qzone\.web_write/);
assert.match(admin, /read_only/);
assert.match(admin, /risk_blocked/);

assert.match(admin, /setTimeout\(\(\) => refreshQzoneSnapshot\(\), qzoneSnapshotNeedsFastRefresh\(\) \? 3000 : 15000\)/);
assert.doesNotMatch(admin, /setInterval\(/);
assert.match(admin, /new AbortController\(\)/);
assert.match(admin, /document\.addEventListener\("visibilitychange"/);
assert.match(admin, /id="qzone-live-island"/);
assert.match(admin, /details\[data-qzone-detail-key\]\[open\]/);
assert.match(admin, /_qzoneLoginPollController\.abort\(\)/);

for (const endpoint of ["reconcile", "resolve-absent", "reconcile-candidates", "reconcile-history"]) {
  assert.ok(admin.includes(endpoint), `missing QZone endpoint: ${endpoint}`);
}
for (const label of ["检查远端", "确认未发布", "检查漏记动态", "确认并补记"]) {
  assert.ok(admin.includes(label), `missing QZone action: ${label}`);
}
assert.match(admin, /setTimeout\(pollQzoneLogin, 1800\)/);
assert.doesNotMatch(admin, /setTimeout\(_scheduleQzoneLoginPolling, 0\)/);
const loginPollBody = admin.slice(admin.indexOf("async function pollQzoneLogin"), admin.indexOf("async function startQzoneLogin"));
assert.doesNotMatch(loginPollBody, /rememberAdminOperation/);

for (const className of ["qzone-reconciliation-card", "qzone-operation-item", "qzone-candidate-item"]) {
  assert.ok(css.includes(`.${className}`), `missing QZone style: ${className}`);
}

console.log("QZone WebUI static checks passed");
