"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "nonebot_plugin_personification", "webui", "static", "app-mcp.js"),
  "utf8",
);
const documentListeners = new Map();
const windowListeners = new Map();
const actions = [];
let releaseFirstMove = null;
let firstMoveBlocked = true;

class FakeElement {}

const marker = {
  style: {},
  classList: {
    values: new Set(),
    add(value) { this.values.add(value); },
    remove(value) { this.values.delete(value); },
    toggle(value, enabled) { if (enabled) this.values.add(value); else this.values.delete(value); },
  },
};
const screen = {querySelector: selector => selector.includes("pointer-marker") ? marker : null};

class FakeImage extends FakeElement {
  constructor() {
    super();
    this.naturalWidth = 1280;
    this.naturalHeight = 900;
    this.attributes = new Map([
      ["data-platform", "douyin"],
      ["data-session-id", "session-live"],
      ["data-viewport-width", "1280"],
      ["data-viewport-height", "900"],
    ]);
    this.captured = new Set();
  }
  closest(selector) {
    if (selector === "[data-mcp-interactive-frame]") return this;
    if (selector === ".mcp-interactive-screen") return screen;
    return null;
  }
  getAttribute(name) { return this.attributes.get(name) || ""; }
  getBoundingClientRect() { return {left:0, top:0, width:640, height:450}; }
  setPointerCapture(pointerId) { this.captured.add(pointerId); }
  releasePointerCapture(pointerId) { this.captured.delete(pointerId); }
  hasPointerCapture(pointerId) { return this.captured.has(pointerId); }
}

const documentStub = {
  activeElement: null,
  addEventListener(type, listener) {
    const listeners = documentListeners.get(type) || [];
    listeners.push(listener);
    documentListeners.set(type, listeners);
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
const windowStub = {
  addEventListener(type, listener) {
    const listeners = windowListeners.get(type) || [];
    listeners.push(listener);
    windowListeners.set(type, listeners);
  },
};

const sandbox = {
  API: "/personification/api",
  ApiError: class ApiError extends Error {},
  CSS: {escape: value => String(value)},
  Element: FakeElement,
  URL: {createObjectURL: () => "blob:test", revokeObjectURL() {}},
  Uint8Array,
  alertFlash() {},
  api: async (_path, options={}) => {
    const action = JSON.parse(String(options.body || "{}")).action;
    actions.push(action);
    if (action.type === "pointer_move" && firstMoveBlocked) {
      firstMoveBlocked = false;
      await new Promise(resolve => { releaseFirstMove = resolve; });
    }
    return {
      status: "manual_verification_required",
      session_id: "session-live",
      interactive_frame_revision: actions.length,
      interactive_pointer_active: !["pointer_end", "pointer_cancel"].includes(action.type),
      interactive_pointer_error_code: "",
      action_applied: true,
      action_duplicate: false,
    };
  },
  clearInMemorySensitiveState() {},
  document: documentStub,
  escapeAttr: value => String(value),
  escapeHtml: value => String(value),
  fetch: async () => ({ok:true, status:204, headers:{get:() => "0"}}),
  globalThis: null,
  operationDiagnosticFromError: (_error, message) => ({message}),
  performance,
  render() {},
  sessionStorage: {getItem:() => null, setItem() {}, removeItem() {}},
  setInterval,
  clearInterval,
  setTimeout,
  clearTimeout,
  state: {view:"mcp", mcpTab:"builtin", mcpAuth:{}, logged:true},
  window: windowStub,
};
sandbox.globalThis = sandbox;
sandbox.crypto = crypto.webcrypto;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, {filename:"app-mcp.js"});

function pointerEvent(image, clientX, clientY, extras={}) {
  return {
    target:image,
    pointerId:1,
    button:0,
    isPrimary:true,
    clientX,
    clientY,
    preventDefault() {},
    ...extras,
  };
}

async function waitFor(predicate, timeoutMs=1500) {
  const started = performance.now();
  while (!predicate()) {
    if (performance.now() - started > timeoutMs) throw new Error("timed out waiting for pointer state");
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

(async () => {
  const image = new FakeImage();
  const pointerDown = documentListeners.get("pointerdown")[0];
  const pointerMove = documentListeners.get("pointermove")[0];
  const pointerUp = documentListeners.get("pointerup")[0];

  pointerDown(pointerEvent(image, 100, 200));
  assert.equal(actions[0].type, "pointer_start", "pointer_start must be sent before pointerup");
  assert.equal(actions[0].x, 200);
  assert.equal(actions[0].y, 400);

  await new Promise(resolve => setTimeout(resolve, 0));
  pointerMove(pointerEvent(image, 200, 200, {
    getCoalescedEvents: () => [pointerEvent(image, 160, 200), pointerEvent(image, 180, 201)],
  }));
  await waitFor(() => actions.some(action => action.type === "pointer_move"));

  for (let index = 0; index < 100; index += 1) {
    pointerMove(pointerEvent(image, 200 + index, 200 + (index % 3)));
  }
  const pendingLength = vm.runInContext("_mcpInteractivePointer.pendingPoints.length", sandbox);
  assert.ok(pendingLength <= 6, `pending move batch must stay bounded, got ${pendingLength}`);

  releaseFirstMove();
  await waitFor(() => actions.filter(action => action.type === "pointer_move").length >= 2);
  assert.equal(actions.some(action => action.type === "pointer_end"), false, "multiple move batches must be sent while still held");
  pointerUp(pointerEvent(image, 500, 210));
  await waitFor(() => actions.some(action => action.type === "pointer_end"));
  await waitFor(() => vm.runInContext("_mcpInteractivePointer === null", sandbox));

  const finalAction = actions.findLast(action => action.type === "pointer_end");
  assert.equal(finalAction.x, 1000, "final release x must not be replaced by an old bounded sample");
  assert.equal(finalAction.y, 420, "final release y must be preserved");
  assert.ok(actions.findIndex(action => action.type === "pointer_move") < actions.indexOf(finalAction));
  assert.equal(image.captured.size, 0, "pointer capture must be released after the remote end action");

  image.naturalHeight = 720;
  const blackBar = vm.runInContext("interactivePoint", sandbox)(image, pointerEvent(image, 20, 10));
  const topEdge = vm.runInContext("interactivePoint", sandbox)(image, pointerEvent(image, 20, 45));
  assert.equal(blackBar, null, "letterbox clicks must be rejected");
  assert.equal(topEdge.y, 0, "the rendered bitmap edge must map to viewport zero");

  const pointerCancel = documentListeners.get("pointercancel")[0];
  pointerDown(pointerEvent(image, 120, 220));
  await waitFor(() => actions.filter(action => action.type === "pointer_start").length === 2);
  pointerCancel(pointerEvent(image, 140, 220));
  await waitFor(() => actions.some(action => action.type === "pointer_cancel"));
  await waitFor(() => vm.runInContext("_mcpInteractivePointer === null", sandbox));
  assert.equal(image.captured.size, 0, "pointer cancellation must release pointer capture");

  process.stdout.write("mcp pointer frontend tests passed\n");
})().catch(error => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
