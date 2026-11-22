// Hosted widget SDK — inlined into every sandboxed widget frame.
// The frame has an opaque origin and no network access; everything goes via postMessage
// to the dashboard, which enforces this widget's granted permissions.
(() => {
  "use strict";
  const parentWin = window.parent;
  const pending = new Map();
  const propListeners = new Set();
  let seq = 0, initResolve, state = { props: {}, secrets: [], theme: {}, id: null };
  const ready = new Promise((r) => (initResolve = r));

  const send = (msg) => parentWin.postMessage({ hosted: 1, ...msg }, "*");
  const call = (type, payload) =>
    new Promise((resolve, reject) => {
      const id = ++seq;
      pending.set(id, { resolve, reject });
      send({ id, type, payload });
    });

  const applyTheme = (theme) => {
    const root = document.documentElement.style;
    for (const [k, v] of Object.entries(theme || {})) root.setProperty(k, v);
    if (theme && theme["--scheme"]) root.colorScheme = theme["--scheme"];
  };

  addEventListener("message", (e) => {
    if (e.source !== parentWin) return;
    const m = e.data;
    if (!m || m.hosted !== 1) return;
    if (m.type === "reply") {
      const p = pending.get(m.id);
      if (!p) return;
      pending.delete(m.id);
      m.ok ? p.resolve(m.result) : p.reject(new Error(m.error || "request failed"));
    } else if (m.type === "init" || m.type === "update") {
      state = { ...state, ...m.payload };
      applyTheme(state.theme);
      if (m.type === "init") initResolve(api);
      else propListeners.forEach((fn) => { try { fn(state.props); } catch (err) { report(err); } });
    }
  });

  const b64ToBytes = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));

  function wrapResponse(r) {
    const ctype = r.headers["content-type"] || "";
    return {
      status: r.status,
      ok: r.status >= 200 && r.status < 300,
      headers: r.headers,
      text: async () => (r.body != null ? r.body : new TextDecoder().decode(b64ToBytes(r.bodyBase64 || ""))),
      json: async () => JSON.parse(r.body != null ? r.body : new TextDecoder().decode(b64ToBytes(r.bodyBase64 || ""))),
      dataUrl: async () =>
        r.bodyBase64 != null
          ? `data:${ctype};base64,${r.bodyBase64}`
          : `data:${ctype || "text/plain"};charset=utf-8,${encodeURIComponent(r.body)}`,
    };
  }

  const api = {
    ready,
    get id() { return state.id; },
    get props() { return state.props; },
    /** Names of granted secrets that are currently set. Values are never exposed. */
    hasSecret: (name) => state.secrets.includes(name),
    /** Use "{{secret.NAME}}" in url/headers/body; the server fills it in. */
    fetch: async (url, opts = {}) => {
      let { method = "GET", headers = {}, body } = opts;
      if (body != null && typeof body === "object") {
        body = JSON.stringify(body);
        if (!Object.keys(headers).some((h) => h.toLowerCase() === "content-type"))
          headers = { ...headers, "Content-Type": "application/json" };
      }
      return wrapResponse(await call("fetch", { url: String(url), method, headers, body }));
    },
    storage: {
      get: (key) => call("storage.get", { key }),
      set: (key, value) => call("storage.set", { key, value }),
      remove: (key) => call("storage.remove", { key }),
    },
    setProps: (props) => call("props.set", { props }),
    /** Open an http(s) link in a new tab. Only works from a user click (e.g. inside a click handler). */
    openUrl: (url) => call("open", { url: String(url) }),
    onProps: (fn) => { propListeners.add(fn); return () => propListeners.delete(fn); },
  };

  // Browser extensions (MetaMask, password managers, ...) inject scripts into every frame and
  // their failures inside the sandbox ("Failed to connect to MetaMask") are not the widget's.
  const EXTENSION = /(chrome|moz|safari-web|ms-browser)-extension:\/\/|^webkit-masked-url:/;
  const EXTENSION_MSG = /metamask|ethereum provider|inpage\.js|contentscript/i;
  const fromExtension = (err, filename) =>
    EXTENSION.test(filename || "") || EXTENSION.test((err && err.stack) || "") ||
    EXTENSION_MSG.test((err && err.message) || String(err || ""));

  function report(err) {
    const msg = err && err.message ? err.message : String(err);
    send({ type: "error", payload: { message: msg.slice(0, 500) } });
  }
  addEventListener("error", (e) => {
    if (!fromExtension(e.error, e.filename)) report(e.error || e.message);
  });
  addEventListener("unhandledrejection", (e) => {
    if (!fromExtension(e.reason)) report(e.reason);
  });

  window.hosted = api;
  send({ type: "hello" });
})();
