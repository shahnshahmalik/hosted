import { h, render } from "preact";
import { useState, useEffect, useRef } from "preact/hooks";
import htm from "htm";
import { api } from "/api.js";
import { Chat } from "/chat.js";

const html = htm.bind(h);
const COLS = 12, GAP = 12, ROW = 80;

const css = (r) => Object.fromEntries(Object.entries(r).map(([k, v]) => [k, v + "px"])); // Preact 11 won't add px

const overlaps = (a, b) =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

// Mirror of store.theme_css(): the server inlines the same rule on first paint.
const themeCss = (vars) =>
  `:root{${Object.entries(vars).map(([k, v]) => `${k}:${v};`).join("")}color-scheme:${vars["--scheme"] || "dark"};}`;

const propsFor = (w, def) => {
  const defaults = Object.fromEntries(Object.entries(def?.props || {}).map(([k, p]) => [k, p.default]));
  return { ...defaults, ...(w.config.props || {}) };
};

function App() {
  const [widgets, setWidgetsState] = useState([]);
  const latest = useRef([]); // always-current list, so rapid actions never use stale state
  const setWidgets = (list) => { latest.current = list; setWidgetsState(list); };
  const [defs, setDefs] = useState({});
  const [secretNames, setSecretNames] = useState([]);
  const [modal, setModal] = useState(null); // "picker" | "secrets" | null
  const [editing, setEditing] = useState(null); // widget id with settings open
  const [frameErrors, setFrameErrors] = useState({}); // wid -> message
  const [drag, setDrag] = useState(null);
  const [theme, setTheme] = useState({ vars: {} }); // {id, name, overrides, vars}
  const [themes, setThemes] = useState([]);
  const [chatOpen, setChatOpen] = useState(localStorage.getItem("hosted.chat") === "1");
  const [nonces, setNonces] = useState({}); // wid -> manual reload counter (part of the frame key)
  const gridRef = useRef();
  const frames = useRef({}); // wid -> iframe element
  const loads = useRef({}); // frame key -> load count (navigation detection)
  const ctx = useRef({}); // always-current data for the message handler
  ctx.current = { defs, secretNames, theme };
  const [width, setWidth] = useState(1000);

  const refresh = async () => {
    const [ws, ds, th, tl] = await Promise.all([api("/widgets"), api("/widget-defs"), api("/theme"), api("/themes")]);
    setThemes(tl);
    for (const k of Object.keys(frames.current)) if (!ws.some((w) => w.id === k)) delete frames.current[k];
    setDefs(Object.fromEntries(ds.map((d) => [d.id, d])));
    setWidgets(ws);
    setTheme(th);
  };
  useEffect(() => {
    refresh();
    api("/secrets").then(setSecretNames);
  }, []);
  useEffect(() => { localStorage.setItem("hosted.chat", chatOpen ? "1" : "0"); }, [chatOpen]);
  useEffect(() => {
    if (!theme.id) return;
    document.getElementById("theme-vars").textContent = themeCss(theme.vars);
    latest.current.forEach(pushUpdate);
  }, [theme]);
  useEffect(() => {
    const ro = new ResizeObserver(() => setWidth(gridRef.current.clientWidth));
    ro.observe(gridRef.current);
    return () => ro.disconnect();
  }, []);

  // ---------- widget runtime bridge ----------
  const framePayload = (w) => ({
    id: w.id,
    props: propsFor(w, ctx.current.defs[w.config.type]),
    secrets: (w.grants?.secrets || []).filter((n) => ctx.current.secretNames.includes(n)),
    theme: ctx.current.theme.vars,
  });
  const post = (wid, msg) => frames.current[wid]?.contentWindow?.postMessage({ hosted: 1, ...msg }, "*");
  const pushUpdate = (w) => post(w.id, { type: "update", payload: framePayload(w) });

  useEffect(() => { latest.current.forEach(pushUpdate); }, [secretNames, defs]);

  const saveWidget = async (w) => {
    const saved = await api("/widgets/" + w.id, "PUT", w);
    setWidgets(latest.current.map((x) => (x.id === w.id ? saved : x)));
    pushUpdate(saved);
    return saved;
  };

  const handlers = {
    fetch: (w, p) => api(`/widgets/${w.id}/proxy`, "POST", p),
    "storage.get": async (w, p) => (await api(`/widgets/${w.id}/storage/${encodeURIComponent(p.key)}`)).value,
    "storage.set": (w, p) => api(`/widgets/${w.id}/storage/${encodeURIComponent(p.key)}`, "PUT", { value: p.value ?? null }),
    "storage.remove": (w, p) => api(`/widgets/${w.id}/storage/${encodeURIComponent(p.key)}`, "DELETE"),
    open: async (w, p) => {
      // A user click inside the frame also activates this page (activation propagates to
      // ancestors), so requiring it means a widget cannot open tabs on its own.
      const url = new URL(p.url);
      if (!["http:", "https:"].includes(url.protocol)) throw new Error("only http(s) links can be opened");
      if (!navigator.userActivation?.isActive) throw new Error("links can only be opened from a click");
      window.open(url.href, "_blank", "noopener,noreferrer");
      return null;
    },
    "props.set": async (w, p) => {
      const props = { ...(w.config.props || {}), ...(p.props || {}) };
      await saveWidget({ ...w, config: { ...w.config, props } });
      return null;
    },
  };

  useEffect(() => {
    const onMessage = async (e) => {
      const m = e.data;
      if (!m || m.hosted !== 1) return;
      // Identify the sender by its window object: a frame cannot impersonate another widget.
      const wid = Object.keys(frames.current).find((k) => frames.current[k]?.contentWindow === e.source);
      const w = wid && latest.current.find((x) => x.id === wid);
      if (!w) return;
      if (m.type === "hello") return post(wid, { type: "init", payload: framePayload(w) });
      if (m.type === "error") return setFrameErrors((s) => ({ ...s, [wid]: String(m.payload?.message || "error") }));
      const handler = handlers[m.type];
      if (!handler) return post(wid, { type: "reply", id: m.id, ok: false, error: `unknown call ${m.type}` });
      try {
        post(wid, { type: "reply", id: m.id, ok: true, result: await handler(w, m.payload || {}) });
      } catch (err) {
        post(wid, { type: "reply", id: m.id, ok: false, error: err.message });
      }
    };
    addEventListener("message", onMessage);
    return () => removeEventListener("message", onMessage);
  }, []);

  const frameKey = (w) => `${w.id}:${defs[w.config.type]?.revision || 0}:${nonces[w.id] || 0}`;
  const onFrameLoad = (wid, key, el) => {
    // Each frame key is a fresh iframe element. Its first load is our document; any later
    // load means the widget navigated itself away.
    loads.current[key] = (loads.current[key] || 0) + 1;
    if (loads.current[key] === 1) setFrameErrors((s) => (s[wid] ? { ...s, [wid]: undefined } : s));
    if (loads.current[key] > 1) {
      el.src = "about:blank";
      setFrameErrors((s) => ({ ...s, [wid]: "Widget was stopped: it tried to navigate away from its frame." }));
    }
  };

  // ---------- layout ----------
  const cw = (width - GAP * (COLS - 1)) / COLS;
  const px = (w) => ({
    left: w.x * (cw + GAP), top: w.y * (ROW + GAP),
    width: w.w * cw + (w.w - 1) * GAP, height: w.h * ROW + (w.h - 1) * GAP,
  });
  const rows = Math.max(6, ...widgets.map((w) => w.y + w.h + 1));

  const queue = useRef(Promise.resolve()); // serialise adds so rapid clicks get distinct slots
  const add = (def) => (queue.current = queue.current.then(() => doAdd(def)).catch((e) => alert(e.message)));
  const doAdd = async (def) => {
    const probe = { w: def.size.w, h: def.size.h };
    const cur = latest.current;
    for (let y = 0; ; y++)
      for (let x = 0; x <= COLS - probe.w; x++) {
        const cand = { ...probe, x, y };
        if (!cur.some((o) => overlaps(cand, o))) {
          const created = await api("/widgets", "POST", { title: def.name, ...cand, config: { type: def.id, props: {} } });
          return setWidgets([...latest.current, created]);
        }
      }
  };

  const remove = async (id) => {
    await api("/widgets/" + id, "DELETE");
    delete frames.current[id];
    delete loads.current[id];
    setWidgets(latest.current.filter((w) => w.id !== id));
  };

  const reload = (id) => setNonces((n) => ({ ...n, [id]: (n[id] || 0) + 1 }));

  const allow = async (id) => {
    const w = await api(`/widgets/${id}/grant`, "POST");
    setWidgets(latest.current.map((x) => (x.id === id ? w : x)));
    reload(id);
  };

  const start = (e, w, mode) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    setDrag({ id: w.id, mode, sx: e.clientX, sy: e.clientY, dx: 0, dy: 0 });
  };

  const slotFor = (w, d) => {
    if (d.mode === "move") {
      const x = Math.min(COLS - w.w, Math.max(0, Math.round((w.x * (cw + GAP) + d.dx) / (cw + GAP))));
      const y = Math.max(0, Math.round((w.y * (ROW + GAP) + d.dy) / (ROW + GAP)));
      return { ...w, x, y };
    }
    const nw = Math.min(COLS - w.x, Math.max(1, Math.round((w.w * (cw + GAP) + d.dx) / (cw + GAP))));
    const nh = Math.max(1, Math.round((w.h * (ROW + GAP) + d.dy) / (ROW + GAP)));
    return { ...w, w: nw, h: nh };
  };

  const move = (e) => {
    if (drag) setDrag({ ...drag, dx: e.clientX - drag.sx, dy: e.clientY - drag.sy });
  };

  const end = async () => {
    if (!drag) return;
    const w = widgets.find((x) => x.id === drag.id);
    const s = slotFor(w, drag);
    setDrag(null);
    const blocked = widgets.some((o) => o.id !== w.id && overlaps(s, o));
    if (blocked || (s.x === w.x && s.y === w.y && s.w === w.w && s.h === w.h)) return;
    setWidgets(widgets.map((x) => (x.id === w.id ? s : x)));
    await api("/widgets/" + w.id, "PUT", s);
  };

  const dragged = drag && widgets.find((w) => w.id === drag.id);
  const slot = dragged && slotFor(dragged, drag);
  const slotOk = slot && !widgets.some((o) => o.id !== slot.id && overlaps(slot, o));

  return html`
    <header>
      <h1>Host<span>ed</span></h1>
      <div class="actions">
        <select class="theme-select" title="Theme" value=${theme.id}
                onChange=${async (e) => setTheme(await api("/theme", "PUT", { id: e.currentTarget.value }))}>
          ${themes.map((t) => html`<option value=${t.id}>${t.name}${t.source !== "builtin" ? " (custom)" : ""}</option>`)}
        </select>
        <button class="ghost" onClick=${() => setModal("secrets")}>Secrets</button>
        <button class="ghost" onClick=${() => setModal("picker")}>+ Add widget</button>
        <button class=${chatOpen ? "ghost active" : ""} onClick=${() => setChatOpen(!chatOpen)}>✦ Assistant</button>
      </div>
    </header>
    <div class="body">
    <main>
    <div class=${"grid" + (drag ? " is-dragging" : "")} ref=${gridRef} style=${css({ height: rows * (ROW + GAP) })}
         onPointerMove=${move} onPointerUp=${end}>
      ${slot && slotOk && html`<div class="ghost-slot" style=${css(px(slot))}></div>`}
      ${widgets.map((w) => {
        const isDrag = drag && drag.id === w.id;
        const style = px(w);
        if (isDrag && drag.mode === "move") { style.left += drag.dx; style.top += drag.dy; }
        if (isDrag && drag.mode === "resize") {
          style.width = Math.max(cw, style.width + drag.dx);
          style.height = Math.max(ROW, style.height + drag.dy);
        }
        const def = defs[w.config.type];
        const pending = w.pending || { hosts: [], secrets: [] };
        const needsAllow = pending.hosts.length + pending.secrets.length > 0;
        const fk = frameKey(w);
        const missing = (w.grants?.secrets || []).filter((n) => !secretNames.includes(n));
        return html`
          <div key=${w.id} class=${"widget framed" + (isDrag ? " dragging" : "")} style=${css(style)} data-wid=${w.id}>
            <div class="widget-head" onPointerDown=${(e) => start(e, w, "move")}>
              <span class="title">${w.title}</span>
              <span class="tools" onPointerDown=${(e) => e.stopPropagation()}>
                ${def && html`<button class="ghost" title="Settings" onClick=${() => setEditing(editing === w.id ? null : w.id)}>⚙</button>`}
                <button class="ghost" title="Remove" onClick=${() => remove(w.id)}>✕</button>
              </span>
            </div>
            ${needsAllow && html`
              <div class="allow-bar">
                <span>Wants access to ${[...pending.hosts, ...pending.secrets.map((s) => "secret " + s)].join(", ")}</span>
                <button onClick=${() => allow(w.id)}>Allow</button>
              </div>`}
            ${frameErrors[w.id] && html`
              <div class="frame-error" title=${frameErrors[w.id]}>
                <span>${frameErrors[w.id]}</span>
                <button class="ghost" onClick=${() => reload(w.id)}>Reload</button>
              </div>`}
            ${editing === w.id && def
              ? html`<${Settings} w=${w} def=${def} missing=${missing} secretNames=${secretNames}
                        onCancel=${() => setEditing(null)}
                        onSave=${async (next) => { await saveWidget(next); setEditing(null); }} />`
              : null}
            <iframe key=${fk} class=${"widget-frame" + (editing === w.id ? " hidden" : "")}
                    src=${"/frame/" + w.id} sandbox="allow-scripts" referrerpolicy="no-referrer"
                    title=${w.title}
                    ref=${(el) => { if (el) frames.current[w.id] = el; }}
                    onLoad=${(e) => onFrameLoad(w.id, fk, e.currentTarget)}></iframe>
            <div class="resize" onPointerDown=${(e) => start(e, w, "resize")}></div>
          </div>`;
      })}
    </div>
    </main>
    ${chatOpen && html`<${Chat} onChanged=${refresh} onClose=${() => setChatOpen(false)}
        frameErrors=${() => Object.fromEntries(Object.entries(frameErrors).filter(([, v]) => v))} />`}
    </div>
    ${modal === "picker" && html`<${Picker} defs=${Object.values(defs)} secretNames=${secretNames} onDefsChanged=${refresh}
        onClose=${() => setModal(null)} onAdd=${(d) => { add(d); setModal(null); }} />`}
    ${modal === "secrets" && html`<${Secrets} names=${secretNames} defs=${Object.values(defs)}
        onChange=${setSecretNames} onClose=${() => setModal(null)} />`}`;
}

function Modal({ title, onClose, children }) {
  return html`
    <div class="modal-backdrop" onClick=${(e) => e.target === e.currentTarget && onClose()}>
      <div class="modal framed" role="dialog" aria-label=${title}>
        <div class="modal-head"><h2>${title}</h2><button class="ghost" onClick=${onClose}>✕</button></div>
        ${children}
      </div>
    </div>`;
}

function Permissions({ perms, secretNames }) {
  const { hosts = [], secrets = [] } = perms || {};
  if (!hosts.length && !secrets.length) return html`<div class="perm muted">No network access</div>`;
  return html`<div class="perm">
    ${hosts.length > 0 && html`<div>Can contact: ${hosts.map((x) => html`<code>${x}</code> `)}</div>`}
    ${secrets.length > 0 && html`<div>Can use secrets: ${secrets.map((s) => html`
      <code class=${secretNames.includes(s) ? "" : "unset"} title=${secretNames.includes(s) ? "set" : "not set"}>${s}</code> `)}</div>`}
  </div>`;
}

const shortSha = (sha) => (sha || "").slice(0, 7);
const originLabel = (o) => (o ? `${o.owner}/${o.repo}${o.path ? "/" + o.path : ""}` : "");

function DefMeta({ d }) {
  const bits = [d.version && `v${d.version}`, d.author && `by ${d.author}`].filter(Boolean);
  return html`${d.source === "ai" && html`<span class="tag">made by AI</span>`}
    ${d.source === "installed" && html`<span class="tag" title=${"Installed from " + originLabel(d.origin) + " @ " + shortSha(d.origin?.sha)}>
      ${originLabel(d.origin)} @ ${shortSha(d.origin?.sha)}</span>`}
    ${bits.length > 0 && html`<span class="muted small"> ${bits.join(" · ")}</span>`}`;
}

function Picker({ defs, secretNames, onClose, onAdd, onDefsChanged }) {
  const [tab, setTab] = useState("library");
  const [updates, setUpdates] = useState(null); // {id: {latest, ref} | {error}}
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState(null);
  const [err, setErr] = useState(null);
  const installed = defs.filter((d) => d.source === "installed");

  const checkUpdates = async () => {
    setChecking(true); setErr(null);
    try { setUpdates(Object.fromEntries((await api("/plugins/updates")).map((u) => [u.id, u]))); }
    catch (x) { setErr(x.message); }
    finally { setChecking(false); }
  };
  const doUpdate = async (d) => {
    setBusy(d.id); setErr(null);
    try {
      await api(`/plugins/${d.id}/update`, "POST");
      setUpdates((u) => ({ ...u, [d.id]: undefined }));
      await onDefsChanged();
    } catch (x) { setErr(x.message); } finally { setBusy(null); }
  };
  const remove = async (d) => {
    if (!confirm(`Remove the widget type “${d.name}”? Widgets using it must be removed first.`)) return;
    setErr(null);
    try { await api(`/widget-defs/${d.id}`, "DELETE"); await onDefsChanged(); } catch (x) { setErr(x.message); }
  };

  return html`<${Modal} title="Add widget" onClose=${onClose}>
    <div class="tabs">
      <button class=${tab === "library" ? "active" : "ghost"} onClick=${() => setTab("library")}>Library</button>
      <button class=${tab === "github" ? "active" : "ghost"} onClick=${() => setTab("github")}>Install from GitHub</button>
    </div>
    ${err && html`<div class="error small">${err}</div>`}
    ${tab === "library" ? html`
      <p class="muted small">Adding a widget grants it the permissions listed. It runs in a sandbox and can only reach these hosts through the server.
        ${installed.length > 0 && html` <button class="ghost small" disabled=${checking} onClick=${checkUpdates}>${checking ? "Checking…" : "Check for updates"}</button>`}</p>
      <div class="def-list">
        ${defs.map((d) => {
          const up = updates?.[d.id];
          return html`
          <div class="def" data-def=${d.id}>
            <div class="def-main">
              <b>${d.name}</b> <${DefMeta} d=${d} />
              <div class="muted small">${d.description}</div>
              <${Permissions} perms=${d.permissions} secretNames=${secretNames} />
              <div class="def-tools">
                <a class="linkbtn" href=${`/api/widget-defs/${d.id}/export`} download title="Download as a package you can publish on GitHub">Download .zip</a>
                ${d.source !== "builtin" && html`<button class="ghost small" onClick=${() => remove(d)}>Remove</button>`}
                ${up && !up.error && html`<button class="small" disabled=${busy === d.id} onClick=${() => doUpdate(d)}>
                  ${busy === d.id ? "Updating…" : `Update to ${shortSha(up.latest)}`}</button>`}
                ${up?.error && html`<span class="error small">${up.error}</span>`}
                ${updates && !up && d.source === "installed" && html`<span class="muted small">Up to date</span>`}
              </div>
            </div>
            <button onClick=${() => onAdd(d)}>Add</button>
          </div>`;
        })}
      </div>`
    : html`<${GithubInstall} secretNames=${secretNames} onInstalled=${onDefsChanged} onAdd=${onAdd} />`}
  <//>`;
}

function GithubInstall({ secretNames, onInstalled, onAdd }) {
  const [url, setUrl] = useState("");
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  const [done, setDone] = useState({}); // path -> installed def

  const load = async (e) => {
    e.preventDefault();
    setLoading(true); setErr(null); setPreview(null); setDone({});
    try { setPreview(await api("/plugins/preview", "POST", { url })); }
    catch (x) { setErr(x.message); } finally { setLoading(false); }
  };
  const install = async (p) => {
    setErr(null);
    try {
      const d = await api("/plugins/install", "POST", { url, sha: preview.sha, path: p.path });
      setDone((s) => ({ ...s, [p.path]: d }));
      await onInstalled();
    } catch (x) { setErr(x.message); }
  };

  return html`<div class="gh-install">
    <p class="muted small">Paste a GitHub link to a widget folder (with <code>manifest.json</code> and <code>index.html</code>) or to a repository containing several. Installed widgets run in the same sandbox as every other widget, and you approve their network access before they can use it.</p>
    <form class="row" onSubmit=${load}>
      <input style="flex:1" placeholder="https://github.com/owner/repo/tree/main/my-widget" value=${url}
             onInput=${(e) => setUrl(e.currentTarget.value)} />
      <button type="submit" disabled=${!url.trim() || loading}>${loading ? "Loading…" : "Preview"}</button>
    </form>
    ${err && html`<div class="error small">${err}</div>`}
    ${preview && html`
      <div class="muted small gh-src">${preview.owner}/${preview.repo} · ${preview.ref} @ <code>${shortSha(preview.sha)}</code>
        · ${preview.packages.length} package${preview.packages.length === 1 ? "" : "s"}</div>
      <div class="def-list">
        ${preview.packages.map((p) => {
          const m = p.manifest, inst = done[p.path];
          return html`<div class="def">
            <div class="def-main">
              ${m ? html`<b>${m.name}</b> <${DefMeta} d=${m} />` : html`<b>${p.path || "/"}</b>`}
              ${m && html`<div class="muted small">${m.description}</div>`}
              ${m && html`<${Permissions} perms=${m.permissions} secretNames=${secretNames} />`}
              <div class="muted small">${p.path || "(repository root)"} · ${m ? Math.ceil(p.code_bytes / 1024) + " KB" : ""}
                · <a href=${p.source_url} target="_blank" rel="noopener noreferrer">view source</a>
                ${p.installed && html` · installed @ <code>${shortSha(p.installed.sha)}</code>`}</div>
              ${p.error && html`<div class="error small">${p.error}</div>`}
            </div>
            ${m && (inst
              ? html`<button onClick=${() => onAdd(inst)}>Add to dashboard</button>`
              : html`<button onClick=${() => install(p)}>
                  ${p.installed ? (p.installed.sha === preview.sha ? "Reinstall" : "Update") : "Install"}</button>`)}
          </div>`;
        })}
      </div>`}
  </div>`;
}

function Settings({ w, def, missing, secretNames, onSave, onCancel }) {
  const [title, setTitle] = useState(w.title);
  const [props, setProps] = useState(propsFor(w, def));
  const [err, setErr] = useState(null);
  const set = (k, v) => setProps({ ...props, [k]: v });
  const submit = async (e) => {
    e.preventDefault();
    try { await onSave({ ...w, title: title || def.name, config: { ...w.config, props } }); }
    catch (x) { setErr(x.message); }
  };
  return html`<form class="settings" onSubmit=${submit}>
    <label>Title<input value=${title} onInput=${(e) => setTitle(e.currentTarget.value)} /></label>
    ${Object.entries(def.props || {}).map(([k, p]) => {
      const label = p.label || k;
      if (p.type === "boolean")
        return html`<label class="check"><input type="checkbox" checked=${!!props[k]}
          onChange=${(e) => set(k, e.currentTarget.checked)} /> ${label}</label>`;
      if (p.type === "select")
        return html`<label>${label}<select value=${props[k]} onChange=${(e) => set(k, e.currentTarget.value)}>
          ${(p.options || []).map((o) => html`<option value=${o}>${o}</option>`)}</select></label>`;
      return html`<label>${label}<input type=${p.type === "number" ? "number" : "text"} value=${props[k] ?? ""}
        onInput=${(e) => set(k, p.type === "number" ? Number(e.currentTarget.value) : e.currentTarget.value)} /></label>`;
    })}
    <${Permissions} perms=${w.grants} secretNames=${secretNames} />
    ${missing.length > 0 && html`<div class="muted small">Missing secrets: ${missing.join(", ")} (set them under Secrets)</div>`}
    ${err && html`<div class="error small">${err}</div>`}
    <div class="row"><button type="submit">Save</button><button type="button" class="ghost" onClick=${onCancel}>Cancel</button></div>
  </form>`;
}

function Secrets({ names, defs, onChange, onClose }) {
  const wanted = [...new Set(defs.flatMap((d) => d.permissions.secrets))].filter((n) => !names.includes(n));
  const [name, setName] = useState(wanted[0] || "");
  const [value, setValue] = useState("");
  const [err, setErr] = useState(null);
  const save = async (e) => {
    e.preventDefault();
    try {
      await api("/secrets/" + encodeURIComponent(name), "PUT", { value });
      setValue(""); setErr(null);
      onChange(await api("/secrets"));
    } catch (x) { setErr(x.message); }
  };
  const del = async (n) => { await api("/secrets/" + encodeURIComponent(n), "DELETE"); onChange(await api("/secrets")); };
  return html`<${Modal} title="Secrets" onClose=${onClose}>
    <p class="muted small">Secret values are stored on the server and never sent to the browser or to widgets. Widgets reference them as <code>{{secret.NAME}}</code> and the proxy fills them in for granted hosts only.</p>
    <ul class="secret-list">
      ${names.length === 0 && html`<li class="muted">No secrets yet</li>`}
      ${names.map((n) => html`<li><code>${n}</code> <span class="muted">••••••</span>
        <button class="ghost" onClick=${() => del(n)}>Delete</button></li>`)}
    </ul>
    <form class="secret-form" onSubmit=${save}>
      <input placeholder="NAME" value=${name} list="wanted-secrets" required pattern="[A-Z][A-Z0-9_]{0,63}"
             onInput=${(e) => setName(e.currentTarget.value.toUpperCase())} />
      <datalist id="wanted-secrets">${wanted.map((n) => html`<option value=${n} />`)}</datalist>
      <input type="password" placeholder="value" value=${value} required autocomplete="off"
             onInput=${(e) => setValue(e.currentTarget.value)} />
      <button type="submit">Save</button>
    </form>
    ${err && html`<div class="error small">${err}</div>`}
  <//>`;
}

render(html`<${App} />`, document.getElementById("app"));
