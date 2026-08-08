/*
 * 热更新兼容入口。
 *
 * 2026-08-04 以前已经打开的 WebUI 标签页仍会把所有管理页面映射到
 * app-admin.js。新 Shell 已改为按页面加载拆分资源，但删除旧文件会让这些
 * 长生命周期标签页在更新后首次进入管理页面时收到 404，只剩空白 Shell。
 *
 * 新版 app-core.js 不会请求本文件；只有旧标签页会付出一次加载全部旧管理
 * 页面能力的成本。至少保留一个兼容周期，确保热更新不要求管理员先清缓存。
 */
(function bootstrapLegacyAdminBundle() {
  "use strict";

  const rendererNames = [
    "renderDashboard",
    "renderHealth",
    "renderQzone",
    "renderPersonas",
    "renderGroups",
    "renderGroupSwitch",
    "renderPersonaBuilder",
    "renderQQ",
    "renderUserPolicy",
    "renderOutbound",
  ];
  const loadingMarkup = '<div class="card"><h2>正在加载管理页面</h2><p class="muted">WebUI 已更新，正在兼容当前已打开的旧标签页…</p></div>';

  for (const name of rendererNames) {
    if (typeof window[name] !== "function") window[name] = () => loadingMarkup;
  }

  if (window.__personificationLegacyAdminReady) return;

  const assets = [
    "app-admin-common.js",
    "app-dashboard.js",
    "app-health-qq.js",
    "app-qzone.js",
    "app-identity-policy.js",
    "app-persona-builder.js",
    "app-groups.js",
  ];

  function loadLegacyAsset(filename) {
    if (typeof ensureAsset === "function") return ensureAsset(filename);
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      const version = (window.PERSONIFICATION_ASSET_VERSIONS || {})[filename] || "";
      script.src = `/personification/static/${filename}${version ? `?v=${encodeURIComponent(version)}` : ""}`;
      script.onload = resolve;
      script.onerror = () => reject(new Error(`页面资源加载失败：${filename}`));
      document.head.appendChild(script);
    });
  }

  window.__personificationLegacyAdminReady = loadLegacyAsset(assets[0])
    .then(() => Promise.all(assets.slice(1).map(loadLegacyAsset)))
    .then(() => {
      if (typeof render === "function") render();
      if (typeof enterViewLifecycle === "function" && typeof state !== "undefined") {
        enterViewLifecycle(state.view);
      }
    })
    .catch(error => {
      const message = `旧页面兼容资源加载失败：${error && error.message ? error.message : "未知错误"}`;
      if (typeof alertFlash === "function") alertFlash("err", message);
      else if (typeof state !== "undefined") {
        state.alert = {kind: "err", text: message};
        if (typeof render === "function") render();
      }
    });
})();
